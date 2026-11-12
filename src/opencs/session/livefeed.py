"""A live feed: one city's records as the game recorder writes them, for a web process that
watches the city as it is played (the service channel's ``subscribe``, ADR 16).

A feed is a run of *epochs*. Each epoch reads like a segment file (ADR 14): a header (the city's
configuration and its players), a keyframe (what a late joiner is sent), then the records, each
once, exactly as ``session.recorder`` hands them to the recorder. So the web decodes a feed with the
same code as a recording, and a feed shows nothing a recording does not: no addresses, nothing from
the logon, and chat only as ``[recording] chat`` keeps it.

The loop only appends to a bounded queue (``put``); the service connection's sender drains it
(``lines``), encoding the records there in batches, and waits on the reader. A reader too slow
for ``SERVICE_FEED_ITEMS`` records loses them: the queue is emptied, nothing more is queued, and
once the reader has caught up (the queue is empty and the sender is not waiting on it) the city
starts a new epoch (``owes_start``) with ``"start": "gap"`` and the count dropped. The game never
waits for a reader, and memory never grows past the cap.
"""

from __future__ import annotations

import base64
import logging
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from opencs import policy
from opencs.config import CityConfig
from opencs.messages import ClientMessage, ServerMessage
from opencs.recording import (
    Kind,
    PilotInfo,
    encode_header,
    encode_payload,
    encode_record,
    keyframe_records,
    segment_header,
)

log = logging.getLogger("opencs.service")

type Payload = bytes | ServerMessage | ClientMessage


@dataclass(frozen=True, slots=True)
class _Start:
    now: float
    wall: float
    config: CityConfig
    players: tuple[PilotInfo, ...]
    snapshot: tuple[ServerMessage, ...]
    state: Mapping[str, Any]
    reason: str  # "subscribe", "gap", "rebuilt"
    dropped: int


@dataclass(frozen=True, slots=True)
class _Keyframe:
    now: float
    snapshot: tuple[ServerMessage, ...]
    state: Mapping[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class _Record:
    now: float
    kind: Kind
    player: int | None
    payload: Payload


type _Item = _Start | _Keyframe | _Record


class Feed:
    """One subscriber's feed of city ``letter``. The loop's methods only check and append."""

    def __init__(self, letter: str, capacity: int = policy.SERVICE_FEED_ITEMS) -> None:
        self.letter = letter
        self._capacity = capacity
        self._queue: deque[_Item] = deque()
        self._owed = True  # a start is owed before any record is queued
        self._reason = "subscribe"
        #: The city object the epoch began with: a rebuilt city starts a new epoch.
        self.city: object = None
        #: Records lost since the last start, and gaps since the subscription.
        self.dropped = 0
        self.gaps = 0
        #: True while the sender waits for the reader to take what it wrote (set by the sender).
        self.waiting = False
        #: True once unsubscribed (or failed): nothing more is queued or sent.
        self.closed = False
        # The sender's side: where the epoch's times count from.
        self._epoch = 0
        self._base = 0.0
        self._at_ms = 0
        self._unencodable = 0

    # --- the loop's side ---------------------------------------------------------------------

    @property
    def owes_start(self) -> bool:
        """Whether the city should start a new epoch now: one is owed (a new subscription, or a
        gap) and the reader has taken everything before it."""
        return self._owed and not self.closed and not self._queue and not self.waiting

    @property
    def queued(self) -> int:
        return len(self._queue)

    def start(
        self,
        now: float,
        wall: float,
        *,
        city: object,
        config: CityConfig,
        players: Sequence[PilotInfo],
        snapshot: Sequence[ServerMessage],
        state: Mapping[str, Any],
        reason: str | None = None,
    ) -> None:
        """A new epoch: a header (``config``, ``players``, the time ``wall``) and a keyframe."""
        if self.closed:
            return
        self._queue.append(
            _Start(
                now,
                wall,
                config,
                tuple(players),
                tuple(snapshot),
                dict(state),
                reason or self._reason,
                self.dropped,
            )
        )
        self.city = city
        self._owed = False
        self.dropped = 0

    def restart(self, reason: str) -> None:
        """Start a new epoch at the next chance (the city was rebuilt)."""
        self._owed = True
        self._reason = reason

    def keyframe(
        self,
        now: float,
        snapshot: Sequence[ServerMessage],
        state: Mapping[str, Any],
        reason: str = "resume",
    ) -> None:
        """A keyframe inside the epoch (a ``/RESUME``), as the recorder writes one."""
        self._put(_Keyframe(now, tuple(snapshot), dict(state), reason))

    def put(self, now: float, kind: Kind, player: int | None, payload: Payload) -> None:
        """A record (CLIENT, SERVER or PLAYER), as the recorder is given it."""
        self._put(_Record(now, kind, player, payload))

    def _put(self, item: _Item) -> None:
        if self.closed:
            return
        if self._owed:
            self.dropped += 1
            return
        if len(self._queue) >= self._capacity:
            self._overflow()
            return
        self._queue.append(item)

    def _overflow(self) -> None:
        lost = len(self._queue) + 1
        self._queue.clear()
        self.dropped += lost
        self.gaps += 1
        self._owed = True
        self._reason = "gap"
        level = logging.WARNING if self.gaps == 1 else logging.DEBUG
        log.log(
            level,
            "the live feed of city %s fell %d records behind: dropped, starting again (gap %d)",
            self.letter,
            self._capacity,
            self.gaps,
        )

    def close(self) -> None:
        self.closed = True
        self._queue.clear()

    # --- the sender's side -------------------------------------------------------------------

    def lines(
        self,
        max_items: int = policy.SERVICE_FEED_BATCH,
        max_bytes: int = policy.SERVICE_FEED_LINE_BYTES,
    ) -> list[dict[str, Any]]:
        """Take up to ``max_items`` queued items and encode them as feed lines (JSON objects): a
        start line opens each epoch (``start``, ``dropped``) with the header and keyframe, then
        lines of records. ``data`` is base64 of the bytes, which read, joined over an epoch, as a
        segment file."""
        out: list[dict[str, Any]] = []
        chunk = bytearray()

        def flush() -> None:
            if chunk:
                out.append(self._line(bytes(chunk)))
                chunk.clear()

        for _ in range(max_items):
            if not self._queue:
                break
            item = self._queue.popleft()
            match item:
                case _Start():
                    flush()
                    out.append(self._start_line(item))
                case _Keyframe(now, snapshot, state, reason):
                    snap = self._encoded(snapshot)
                    chunk += keyframe_records(self._delta(now), reason, snap, state)
                case _Record(now, kind, player, payload):
                    data = self._encode(payload)
                    if data is not None:
                        chunk += encode_record(self._delta(now), kind, player, data)
            if len(chunk) >= max_bytes:
                flush()
        flush()
        return out

    def _start_line(self, start: _Start) -> dict[str, Any]:
        self._epoch += 1
        self._base = start.now
        self._at_ms = 0
        header = segment_header(self.letter, start.wall, start.config, start.players)
        data = encode_header(header) + keyframe_records(
            0, start.reason, self._encoded(start.snapshot), start.state
        )
        line = self._line(data)
        line["start"] = start.reason
        line["dropped"] = start.dropped
        return line

    def _line(self, data: bytes) -> dict[str, Any]:
        return {
            "feed": self.letter,
            "epoch": self._epoch,
            "data": base64.b64encode(data).decode("ascii"),
        }

    def _delta(self, now: float) -> int:
        at = max(self._at_ms, round((now - self._base) * 1000))
        delta, self._at_ms = at - self._at_ms, at
        return delta

    def _encoded(self, payloads: Sequence[Payload]) -> list[bytes]:
        return [d for d in (self._encode(p) for p in payloads) if d is not None]

    def _encode(self, payload: Payload) -> bytes | None:
        try:
            return encode_payload(payload)
        except ValueError:
            self._unencodable += 1
            if self._unencodable == 1:
                log.warning("a live feed left out a message that does not encode: %r", payload)
            return None
