"""The real-time map: the game server's live feed of a city (the service channel's ``subscribe``,
ADR 16), decoded once and shared by every viewer.

:class:`FeedClient` is a connection of its own to the service channel (the request channel answers
one request at a time, and a feed's lines come unasked). It subscribes to the cities someone
watches and nothing else, connects only while there is one, and resubscribes after the game server
comes back. :class:`LiveCity` decodes a city's feed into a :class:`~opencs_web.maps.track.CityTrack`
and, once a second while someone watches, cuts what happened into a *piece* encoded once for all
of them; a viewer that joins gets the city as it was at the end of the last piece, then the pieces
after it. A viewer too slow for ``PIECES_KEPT`` pieces gets the city again.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opencs.recording import SegmentError, read_segment
from opencs_web.channel import (
    BACKOFF_FIRST,
    BACKOFF_MAX,
    HELLO,
    MAX_REPLY,
    PROTOCOL_VERSION,
    TIMEOUT,
)
from opencs_web.maps.track import CityTrack, Collected, dumps, encode
from opencs_web.recordings.chunks import read_records

log = logging.getLogger("opencs_web.maps")

#: Seconds between pieces sent to the viewers of a city.
PIECE_SECONDS = 1.0
#: Pieces kept for viewers that are behind.
PIECES_KEPT = 30


class FeedClient:
    """The connection for live feeds to the service socket at ``path``; ``on_line`` is given each
    feed line (a dict with ``feed``), and ``{"feed": letter, "end": "lost"}`` for each city when
    the connection is lost."""

    def __init__(
        self,
        path: Path | None,
        on_line: Callable[[dict[str, Any]], None],
        *,
        backoff_first: float = BACKOFF_FIRST,
        backoff_max: float = BACKOFF_MAX,
    ) -> None:
        self._path = path
        self._on_line = on_line
        self._backoff_first = backoff_first
        self._backoff_max = backoff_max
        self.wanted: set[str] = set()
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._next_id = 0
        self.connected = asyncio.Event()

    def want(self, letter: str) -> None:
        self.wanted.add(letter)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="live feeds")
        elif self._writer is not None:
            self._request("subscribe", letter)

    def drop(self, letter: str) -> None:
        self.wanted.discard(letter)
        if self._writer is not None:
            self._request("unsubscribe", letter)
        if not self.wanted and self._task is not None:
            self._task.cancel()
            self._task = None

    async def close(self) -> None:
        self.wanted.clear()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _request(self, op: str, letter: str) -> None:
        assert self._writer is not None
        self._next_id += 1
        line = {"id": self._next_id, "op": op, "city": letter}
        self._writer.write(json.dumps(line).encode() + b"\n")

    async def _run(self) -> None:
        failures = 0
        while self.wanted:
            try:
                await self._session()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                if failures == 1:
                    log.warning("live feeds: no connection to the game server (%s)", e)
            finally:
                self._writer = None
                self.connected.clear()
                for letter in list(self.wanted):
                    self._on_line({"feed": letter, "end": "lost"})
            delay = min(self._backoff_max, self._backoff_first * 2 ** max(failures - 1, 0))
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        if self._path is None:
            raise ConnectionError("no service socket is configured")
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self._path, limit=MAX_REPLY), TIMEOUT
        )
        try:
            writer.write(json.dumps({"hello": HELLO, "version": PROTOCOL_VERSION}).encode() + b"\n")
            hello = json.loads(await asyncio.wait_for(reader.readuntil(b"\n"), TIMEOUT))
            if not isinstance(hello, dict) or hello.get("hello") != HELLO:
                raise ConnectionError(f"the game server refused the hello: {hello!r}")
            self._writer = writer
            self.connected.set()
            for letter in sorted(self.wanted):
                self._request("subscribe", letter)
            log.info("live feeds connected: %s", ", ".join(sorted(self.wanted)))
            while True:
                line = await reader.readuntil(b"\n")
                message = json.loads(line)
                if not isinstance(message, dict):
                    continue
                if "feed" in message:
                    self._on_line(message)
                elif message.get("ok") is False:
                    log.warning("live feeds: the game server refused: %s", message.get("message"))
        except asyncio.IncompleteReadError:
            raise ConnectionError("the game server closed the connection") from None
        finally:
            writer.close()


@dataclass
class _Piece:
    seq: int
    start: int
    end: int
    collected: Collected
    encoded: dict[bool, bytes] = field(default_factory=dict)


class LiveCity:
    """One city's live feed, decoded, and its pieces for viewers."""

    def __init__(self, letter: str, wall: Callable[[], float] = time.time) -> None:
        self.letter = letter
        self._wall = wall
        self.track = CityTrack(letter)
        self.track.collecting = True
        self.online = False
        self.epoch: int | None = None
        self._at = 0  # the time of the feed's last record
        self.pieces: deque[_Piece] = deque(maxlen=PIECES_KEPT)
        self.seq = 0
        self._end = self.now_ms()
        self._joining = 0
        #: The city at the end of piece ``seq``, encoded, for viewers who joined meanwhile.
        self._state: tuple[int, bytes] | None = None
        self._changed = asyncio.Event()
        self.viewers = 0
        self.ticker: asyncio.Task[None] | None = None

    def now_ms(self) -> int:
        return round(self._wall() * 1000)

    # --- the feed ----------------------------------------------------------------------------

    def line(self, line: dict[str, Any]) -> None:
        """A feed line from the game server."""
        if "end" in line:
            self._offline(str(line["end"]))
            return
        try:
            data = base64.b64decode(line["data"], validate=True)
        except (KeyError, TypeError, binascii.Error):
            log.warning("a live feed line that is not one, for city %s", self.letter)
            return
        if "start" in line:
            self._start(line, data)
        elif self.epoch is not None and line.get("epoch") == self.epoch:
            for r in read_records(data, self._at):
                self._at = r.at_ms
                player = None if r.source == 0 else r.source - 1
                self.track.record(r.at_ms, r.kind, player, r.payload)

    def _start(self, line: dict[str, Any], data: bytes) -> None:
        try:
            segment = read_segment(data)
        except SegmentError as e:
            log.warning("a live feed of city %s began with no header: %s", self.letter, e)
            return
        if line.get("start") == "gap":
            log.info(
                "the live feed of city %s fell behind and starts again (%s records lost)",
                self.letter,
                line.get("dropped"),
            )
        self.epoch = line.get("epoch")
        self.online = True
        t0 = int(segment.header.get("started_utc_ms", self.now_ms()))
        self.track.begin_segment(segment.header)  # a new epoch: nothing carries over
        for r in segment.records:
            self._at = t0 + r.at_ms
            self.track.record(self._at, r.kind, r.player, r.payload)

    def _offline(self, why: str) -> None:
        if self.online:
            self.track.collected.events.append([self.now_ms(), "end", why])
        self.online = False
        self.epoch = None
        self.track.begin_segment({})  # nothing known until the feed starts again

    # --- pieces ------------------------------------------------------------------------------

    def tick(self) -> None:
        """Cut what happened since the last piece into a new one; give joiners the city."""
        collected = self.track.take()
        times = [r[0] for r in collected.reports] + [e[0] for e in collected.events]
        start = min([self._end, *times])
        end = max([self._end, self.now_ms(), *(t + 1 for t in times)])
        self.seq += 1
        self.pieces.append(_Piece(self.seq, start, end, collected))
        self._end = end
        if self._joining:
            state = self.track.state(end)
            self._state = (self.seq, dumps(encode(self.letter, end, end, state, Collected())))
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def piece_bytes(self, piece: _Piece, chat: bool) -> bytes:
        data = piece.encoded.get(chat)
        if data is None:
            data = piece.encoded[chat] = dumps(
                encode(self.letter, piece.start, piece.end, None, piece.collected, chat=chat)
            )
        return data

    async def join(self) -> tuple[int, bytes]:
        """Wait for the next piece: its seq, and the city at its end as a window with a state
        (which never holds chat)."""
        self._joining += 1
        try:
            while self._state is None or self._state[0] != self.seq:
                await self._changed.wait()
        finally:
            self._joining -= 1
        return self._state

    async def wait(self, seq: int, timeout: float) -> bool:
        """Wait until a piece after ``seq`` is out; False if ``timeout`` passed first."""
        if self.seq > seq:
            return True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout)
            return True
        return False

    def after(self, seq: int) -> list[_Piece] | None:
        """The pieces after ``seq``, or None if some were dropped (the viewer must join again)."""
        if not self.pieces or self.pieces[0].seq > seq + 1:
            return None if self.seq > seq else []
        return [p for p in self.pieces if p.seq > seq]


class LiveHub:
    """Every city watched live, one feed each, while anyone watches it."""

    def __init__(self, path: Path | None, *, piece_seconds: float = PIECE_SECONDS) -> None:
        self.cities: dict[str, LiveCity] = {}
        self.client = FeedClient(path, self._line)
        self._piece_seconds = piece_seconds
        self._loop: asyncio.AbstractEventLoop | None = None

    def _line(self, line: dict[str, Any]) -> None:
        city = self.cities.get(str(line.get("feed")))
        if city is not None:
            city.line(line)

    def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        if loop is not self._loop:  # tests run each request in a loop of its own
            self._loop = loop
            self.cities.clear()
            self.client = FeedClient(self.client._path, self._line)

    @contextlib.asynccontextmanager
    async def watch(self, letter: str) -> AsyncIterator[LiveCity]:
        """While held, city ``letter``'s feed is subscribed and its pieces made."""
        self._bind()
        city = self.cities.get(letter)
        if city is None:
            city = self.cities[letter] = LiveCity(letter)
        city.viewers += 1
        if city.ticker is None:
            self.client.want(letter)
            city.ticker = asyncio.create_task(self._tick(city), name=f"live map {letter}")
            log.info("live map of city %s started", letter)
        try:
            yield city
        finally:
            city.viewers -= 1
            if city.viewers == 0:
                if city.ticker is not None:
                    city.ticker.cancel()
                    city.ticker = None
                self.client.drop(letter)
                self.cities.pop(letter, None)
                log.info("live map of city %s stopped: no viewers", letter)

    async def _tick(self, city: LiveCity) -> None:
        while True:
            await asyncio.sleep(self._piece_seconds)
            city.tick()
