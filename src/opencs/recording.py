"""The game recorder: what happens in a city, appended to segment files (the web plan's Phase 2).

The loop only appends to a bounded queue (``Recorder``'s methods); a writer thread turns the
queue into records and appends them to one open segment file per city, so a slow or full disk never
stalls a game. A full queue or any write failure is logged once and ends recording for the run;
nothing here ever raises into the game. Standard library only (ADR 1).

A segment file is a header and records (the format is ADR 14; the hook points and what is left out
are the recorder design note's)::

    magic "\\x89OCSREC\\n", varint format version, varint header length, header (JSON)
    record: varint ms since the previous record, kind byte, varint source (0 the server, else
            player id + 1), varint payload length, payload

A payload is a wire payload without its frame header or terminator (a server message carries the
tag ``BYTE_MIN``), or JSON for the player table and keyframe state. Records are only ever
appended, so a segment cut short by a crash reads up to its last whole record (``read_segment``).

Files are ``<letter>-<UTC time>.ocsrec.part`` while open and ``.ocsrec`` once finished. The
archiver deletes a segment it has stored, or renames it ``.ocsrec.done``; only ``.done`` files are
removed to keep the directory under its cap, so a recording not yet archived is never lost to it.
A segment is made mode ``RECORDING_FILE_MODE`` (0640) whatever the umask, so the archiver can read
it as a member of the server's group.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, BinaryIO

from opencs import __version__, policy
from opencs.config import CityConfig, RecordingConfig
from opencs.messages import (
    ClientMessage,
    DecodeError,
    ServerMessage,
    client,
    client_payload,
    decode_client,
    server_payload,
)
from opencs.messages.limits import CHAT_STYLE_OPEN
from opencs.wire.codec import BYTE_MIN

log = logging.getLogger("opencs.recording")

#: The first bytes of every segment: a high byte, so a text-mode copy is caught, and a newline.
MAGIC = b"\x89OCSREC\n"

#: The version of the segment format written; a reader refuses a newer one.
FORMAT_VERSION = 1

#: The file name of a segment: city letter, UTC start, a counter if the second was taken.
SUFFIX = ".ocsrec"
PART = ".part"
DONE = ".done"
#: Every ending a segment's name may have after ``SUFFIX``.
ENDINGS = (PART, "", DONE)
_OWN_FILE = re.compile(r"[A-Z]-(\d{8}T\d{6}Z)(?:-(\d+))?\.ocsrec(\.part|\.done)?")

#: A varint holds at most this many bytes (a 64-bit value).
_VARINT_MAX_BYTES = 10


class Kind(IntEnum):
    """What a record holds. 0 is never a kind, so a zero-filled tail ends the reading."""

    CLIENT = 1  # a client's message, as it reached the city; source = the sender
    SERVER = 2  # a server event, once, not per recipient; source = its one recipient, or 0
    PLAYER = 3  # the player table changed: JSON for a join, empty for a leave; source = the player
    KEYFRAME = 4  # a keyframe begins: JSON state; then SNAPSHOT records, then KEYFRAME_END
    SNAPSHOT = 5  # a server payload that recreates part of the city (a late joiner's sync)
    KEYFRAME_END = 6
    END = 7  # the segment was closed cleanly: JSON {"reason"}


class SegmentError(ValueError):
    """Bytes that are not a segment this reader can read: bad magic, a newer format, a header cut
    short. Records cut short are not an error (``Segment.complete``)."""


# --- the format ------------------------------------------------------------------------------


def varint(value: int) -> bytes:
    """``value`` (0 or more) as an unsigned LEB128 varint."""
    if value < 0:
        raise ValueError(f"varint of a negative value {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int] | None:
    """The varint at ``pos`` and the position after it, or None if ``data`` ends inside it or it
    runs past ``_VARINT_MAX_BYTES``."""
    value = shift = 0
    for i in range(pos, min(len(data), pos + _VARINT_MAX_BYTES)):
        byte = data[i]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i + 1
        shift += 7
    return None


def _source(player: int | None) -> int:
    return 0 if player is None else player + 1


def encode_record(delta_ms: int, kind: Kind, player: int | None, payload: bytes) -> bytes:
    """One record: ``delta_ms`` since the previous one, from ``player`` (None: the server)."""
    return (
        varint(delta_ms) + bytes([kind]) + varint(_source(player)) + varint(len(payload)) + payload
    )


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def encode_header(header: Mapping[str, Any]) -> bytes:
    """The start of a segment file: magic, format version and the JSON header."""
    body = _json(header)
    return MAGIC + varint(FORMAT_VERSION) + varint(len(body)) + body


def segment_header(
    letter: str, wall: float, config: CityConfig, players: Iterable[PilotInfo]
) -> dict[str, Any]:
    """A segment's JSON header: the server version, the city, the start (UTC, from ``wall``), the
    city's configuration and the player table."""
    return {
        "server_version": __version__,
        "city": letter,
        "started_utc_ms": round(wall * 1000),
        "config": asdict(config),
        "players": [p.row() for p in players],
    }


def encode_payload(payload: bytes | ServerMessage | ClientMessage) -> bytes:
    """A record's payload: a server message with the tag ``BYTE_MIN``, a client message, or bytes
    as given. Raises ``ValueError`` for a message that does not encode."""
    if isinstance(payload, ServerMessage):
        return server_payload(payload, BYTE_MIN)
    if isinstance(payload, ClientMessage):
        return client_payload(payload)
    return payload


def keyframe_records(
    delta_ms: int,
    reason: str,
    snapshot: Iterable[bytes],
    state: Mapping[str, Any],
) -> bytes:
    """A keyframe: a KEYFRAME record of ``state`` (and ``reason``) ``delta_ms`` after the record
    before, a SNAPSHOT record for each encoded payload, then KEYFRAME_END."""
    out = [encode_record(delta_ms, Kind.KEYFRAME, None, _json({**state, "reason": reason}))]
    out += [encode_record(0, Kind.SNAPSHOT, None, data) for data in snapshot]
    out.append(encode_record(0, Kind.KEYFRAME_END, None, b""))
    return b"".join(out)


def keeps_chat(config: RecordingConfig, letter: str, style: int) -> bool:
    """Whether a chat line of this style is recorded in city ``letter`` (``[recording] chat``)."""
    mode = config.chat_for(letter)
    return mode == "all" or (mode == "public" and style == CHAT_STYLE_OPEN)


@dataclass(frozen=True, slots=True)
class Record:
    """One record as read: ``at_ms`` is milliseconds since the segment's start."""

    at_ms: int
    kind: Kind
    player: int | None
    payload: bytes

    def state(self) -> Any:
        """The JSON payload of a PLAYER, KEYFRAME or END record (None for an empty one)."""
        return json.loads(self.payload) if self.payload else None


@dataclass(frozen=True, slots=True)
class Segment:
    """A segment as read: its header, every whole record, whether it was closed cleanly (an END
    was read) and how many bytes after the last whole record were ignored."""

    header: Mapping[str, Any]
    records: tuple[Record, ...]
    complete: bool
    ignored: int


def read_segment(data: bytes) -> Segment:
    """Read a segment, a whole file or one cut short. Raises ``SegmentError`` only when the
    header cannot be read; records stop at the first one that is incomplete or not a record."""
    if not data.startswith(MAGIC):
        raise SegmentError("not a recording segment (bad magic)")
    pos = len(MAGIC)
    version = _read_varint(data, pos)
    if version is None:
        raise SegmentError("header cut short")
    if version[0] != FORMAT_VERSION:
        raise SegmentError(f"segment format {version[0]}, this reader knows {FORMAT_VERSION}")
    length = _read_varint(data, version[1])
    if length is None or length[1] + length[0] > len(data):
        raise SegmentError("header cut short")
    pos = length[1] + length[0]
    try:
        header = json.loads(data[length[1] : pos])
    except ValueError as exc:
        raise SegmentError(f"header is not JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise SegmentError("header is not a JSON object")
    records: list[Record] = []
    at = 0
    complete = False
    while pos < len(data):
        parsed = _read_record(data, pos)
        if parsed is None:
            break
        delta, kind, player, payload, pos = parsed
        at += delta
        records.append(Record(at, kind, player, payload))
        if kind is Kind.END:
            complete = True
            break
    return Segment(header, tuple(records), complete, len(data) - pos)


def _read_record(data: bytes, pos: int) -> tuple[int, Kind, int | None, bytes, int] | None:
    delta = _read_varint(data, pos)
    if delta is None or delta[1] >= len(data):
        return None
    try:
        kind = Kind(data[delta[1]])
    except ValueError:
        return None  # 0 (a zero-filled tail) or not a kind of this format
    source = _read_varint(data, delta[1] + 1)
    if source is None:
        return None
    length = _read_varint(data, source[1])
    if length is None:
        return None
    end = length[1] + length[0]
    if end > len(data):
        return None
    player = None if source[0] == 0 else source[0] - 1
    return delta[0], kind, player, data[length[1] : end], end


def read_segment_file(path: Path) -> Segment:
    return read_segment(path.read_bytes())


# --- what the loop hands over ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PilotInfo:
    """A row of the player table."""

    id: int
    callsign: str
    team: int
    bot: bool = False

    def row(self) -> dict[str, Any]:
        return {"id": self.id, "callsign": self.callsign, "team": self.team, "bot": self.bot}


type Payload = bytes | ServerMessage


@dataclass(frozen=True, slots=True)
class _Begin:
    config: CityConfig
    players: tuple[PilotInfo, ...]
    snapshot: tuple[Payload, ...]
    state: Mapping[str, Any]
    reason: str
    wall: float  # when the loop began it, for the name and header


@dataclass(frozen=True, slots=True)
class _Keyframe:
    snapshot: tuple[Payload, ...]
    state: Mapping[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class _Record:
    kind: Kind
    player: int | None
    payload: bytes | ServerMessage | ClientMessage


@dataclass(frozen=True, slots=True)
class _End:
    reason: str


type _Op = _Begin | _Keyframe | _Record | _End


# --- disk space ------------------------------------------------------------------------------


def make_room(directory: Path, room: int, limit: int) -> bool:
    """Remove the oldest segments the archiver has finished with (``.done``) until ``room`` more
    bytes fit under ``limit``, counting every segment file in ``directory``. False if they do not
    fit even so. Raises ``OSError`` if the directory cannot be read or a file not removed."""
    files = [f for f in directory.iterdir() if _OWN_FILE.fullmatch(f.name)]
    total = sum(f.stat().st_size for f in files)
    done = sorted((f for f in files if f.name.endswith(DONE)), key=segment_age)
    for old in done:
        if total + room <= limit:
            break
        size = old.stat().st_size
        old.unlink()
        total -= size
        log.info("recordings over their %d-byte cap: removed %s", limit, old.name)
    return total + room <= limit


def segment_age(path: Path) -> tuple[str, int]:
    """Oldest first: by the UTC start in the name, then its counter, whatever the city."""
    match = _OWN_FILE.fullmatch(path.name)
    assert match is not None
    return match[1], int(match[2] or 1)


def directory_bytes(directory: Path) -> int:
    """Bytes of every segment file in ``directory``."""
    return sum(f.stat().st_size for f in directory.iterdir() if _OWN_FILE.fullmatch(f.name))


# --- the recorder ----------------------------------------------------------------------------


class _Open:
    """One city's open segment, on the writer thread."""

    def __init__(self, path: Path, handle: BinaryIO, base: float) -> None:
        self.path = path
        self.handle = handle
        self.base = base  # the loop's clock at the segment's start
        self.at_ms = 0  # time of the last record written, ms since ``base``

    def delta(self, now: float) -> int:
        at = max(self.at_ms, round((now - self.base) * 1000))
        delta, self.at_ms = at - self.at_ms, at
        return delta


class Recorder:
    """Records the cities that ``config`` says to. Methods without a leading underscore are for
    the loop and never block or raise; the writer thread does the rest.

    ``wall`` is the wall clock for segment names and headers (tests pass a fixed one).
    ``rotate_seconds``, ``flush_seconds`` and ``buffer_items`` default to the ``policy`` values.
    """

    def __init__(
        self,
        config: RecordingConfig,
        *,
        wall: Callable[[], float] = time.time,
        rotate_seconds: float = policy.RECORDING_ROTATE_SECONDS,
        flush_seconds: float = policy.RECORDING_FLUSH_SECONDS,
        buffer_items: int = policy.RECORDING_BUFFER_ITEMS,
        segment_reserve: int = policy.RECORDING_SEGMENT_RESERVE,
    ) -> None:
        assert config.directory is not None, "a recorder needs [recording] dir"
        self.config = config
        self._directory = config.directory
        self._bots = frozenset(config.bots)
        self._wall = wall
        self._rotate = rotate_seconds
        self._flush = flush_seconds
        self._max_items = buffer_items
        self._reserve = segment_reserve
        self._queue: deque[tuple[str, float, _Op]] = deque()
        #: Letter -> when its segment began (the loop's clock), for the cities being recorded.
        self._began: dict[str, float] = {}
        self._stopped = False
        self._failed = threading.Lock()
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        # The writer thread's own state.
        self._open: dict[str, _Open] = {}
        self._dir_bytes = 0
        self._short_of_room = False
        self._unencodable = 0

    # --- the loop's side: choices ------------------------------------------------------------

    def is_bot(self, callsign: str) -> bool:
        """Whether a guest pilot with this callsign is a bot (``[recording] bots``)."""
        return callsign.upper() in self._bots

    def wants(self, letter: str, callsigns: Iterable[str]) -> bool:
        """Whether city ``letter`` should be recorded with these pilots in it (``when``)."""
        mode = self.config.when_for(letter)
        if mode == "off":
            return False
        if mode == "any":
            return any(True for _ in callsigns)
        return any(not self.is_bot(c) for c in callsigns)

    def keeps_chat(self, letter: str, style: int) -> bool:
        """Whether a chat line of this style is recorded in city ``letter`` (``chat``)."""
        return keeps_chat(self.config, letter, style)

    # --- the loop's side: state --------------------------------------------------------------

    @property
    def stopped(self) -> bool:
        """True once recording has stopped for the run (a full queue or a write failure)."""
        return self._stopped

    def recording(self, letter: str) -> bool:
        """Whether city ``letter`` has a segment open (as far as the loop knows)."""
        return not self._stopped and letter in self._began

    def rotation_due(self, letter: str, now: float) -> bool:
        """Whether city ``letter``'s segment is old enough to be replaced by a new one, which the
        caller starts with ``begin`` and a fresh keyframe."""
        began = self._began.get(letter)
        return not self._stopped and began is not None and now - began >= self._rotate

    # --- the loop's side: records ------------------------------------------------------------

    def begin(
        self,
        letter: str,
        now: float,
        *,
        config: CityConfig,
        players: Sequence[PilotInfo],
        snapshot: Sequence[Payload],
        state: Mapping[str, Any],
        reason: str = "start",
    ) -> None:
        """Start a new segment for city ``letter`` (closing the one it has): a header with the city
        config and ``players``, then a keyframe of ``snapshot`` (the server payloads, or messages,
        a late joiner would be sent) and ``state`` (JSON: scores and the like). ``reason`` goes in
        the keyframe: ``"start"``, ``"rotate"``, ``"resume"``."""
        op = _Begin(config, tuple(players), tuple(snapshot), dict(state), reason, self._wall())
        if self._put(letter, now, op):
            self._began[letter] = now

    def keyframe(
        self,
        letter: str,
        now: float,
        snapshot: Sequence[Payload],
        state: Mapping[str, Any],
        reason: str = "resume",
    ) -> None:
        """A keyframe inside the open segment (a ``/RESUME``)."""
        if letter in self._began:
            self._put(letter, now, _Keyframe(tuple(snapshot), dict(state), reason))

    def end(self, letter: str, now: float, reason: str) -> None:
        """Close city ``letter``'s segment: nobody left to record, or recording turned off."""
        if self._began.pop(letter, None) is not None:
            self._put(letter, now, _End(reason))

    def client(
        self, letter: str, player_id: int, message: ClientMessage | bytes, now: float
    ) -> None:
        """A message from ``player_id`` that is about to reach the city. Chat the city's ``chat``
        setting leaves out is dropped here."""
        if letter not in self._began:
            return
        style = chat_style(message)
        if style is not None and not self.keeps_chat(letter, style):
            return
        self._put(letter, now, _Record(Kind.CLIENT, player_id, message))

    def server(
        self, letter: str, message: ServerMessage | bytes, now: float, to: int | None = None
    ) -> None:
        """A server event, once; ``to`` is its one recipient if it has only one."""
        if letter in self._began:
            self._put(letter, now, _Record(Kind.SERVER, to, message))

    def player(self, letter: str, now: float, player_id: int, info: PilotInfo | None) -> None:
        """A player joined (``info``) or left (None) city ``letter`` during its segment."""
        if letter in self._began:
            payload = b"" if info is None else _json(info.row())
            self._put(letter, now, _Record(Kind.PLAYER, player_id, payload))

    def _put(self, letter: str, now: float, op: _Op) -> bool:
        if self._stopped:
            return False
        if len(self._queue) >= self._max_items:
            self._fail(f"the writer fell {self._max_items} records behind")
            return False
        self._queue.append((letter, now, op))
        return True

    # --- running -----------------------------------------------------------------------------

    def start(self) -> None:
        """Start the writer thread (it first finishes any segment a crash left open)."""
        self._thread = threading.Thread(target=self._run, name="opencs-recorder", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        """Write what is queued, close every segment cleanly and stop the writer. Waits at most
        ``timeout`` seconds; the thread is a daemon, so a stuck disk never holds up the exit."""
        self._began.clear()  # the writer ends each open segment with reason "shutdown"
        self._closing.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("the recording writer did not finish within %.0f s", timeout)

    def stop(self, why: str, exc: BaseException | None = None) -> None:
        """Stop recording for the run (the caller hit a bug of its own): the open segments are
        left readable, and every later call is a no-op."""
        self._fail(why, exc)

    def _fail(self, why: str, exc: BaseException | None = None) -> None:
        """Stop recording for the run, saying why once."""
        with self._failed:
            if self._stopped:
                return
            self._stopped = True
        self._began.clear()
        log.error("recording stopped: %s", why, exc_info=exc)

    # --- the writer thread -------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._finish_leftovers()
            while not self._stopped:
                closing = self._closing.wait(self._flush)
                self._drain()
                if closing:
                    break
        except OSError as exc:
            self._fail(f"cannot write to {self._directory}", exc)
        except Exception as exc:  # a bug: still never the game's problem
            self._fail("the writer failed", exc)
        finally:
            self._queue.clear()
            self._close_all()

    def _finish_leftovers(self) -> None:
        """A ``.part`` left by a crash is readable to its last whole record: make it a finished
        segment for the archiver."""
        for path in sorted(self._directory.glob(f"*{SUFFIX}{PART}")):
            if _OWN_FILE.fullmatch(path.name):
                path.rename(path.with_name(path.name.removesuffix(PART)))
                log.info("finished %s, left open by an earlier run", path.name)

    def _drain(self) -> None:
        while not self._stopped:
            try:
                letter, now, op = self._queue.popleft()
            except IndexError:
                break
            self._apply(letter, now, op)
        for segment in self._open.values():
            segment.handle.flush()

    def _apply(self, letter: str, now: float, op: _Op) -> None:
        match op:
            case _Begin():
                self._finish(letter, now, "rotate")
                if self._start(letter, now, op):
                    self._write_keyframe(letter, now, op.reason, op.snapshot, op.state)
            case _Keyframe():
                self._write_keyframe(letter, now, op.reason, op.snapshot, op.state)
            case _End(reason):
                self._finish(letter, now, reason)
            case _Record(kind, player, payload):
                data = self._encode(payload)
                if data is not None:
                    self._write(letter, now, kind, player, data)

    def _encode(self, payload: bytes | ServerMessage | ClientMessage) -> bytes | None:
        try:
            return encode_payload(payload)
        except ValueError:
            self._unencodable += 1
            if self._unencodable == 1:
                log.warning("left out a message that does not encode: %r", payload, exc_info=True)
            return None

    def _start(self, letter: str, now: float, begin: _Begin) -> bool:
        """Open a new segment for ``letter``; False (and nothing recorded for the city until its
        next ``begin``) if there is no room for it."""
        if not self._has_room():
            return False
        wall = begin.wall
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(wall))
        path = self._directory / f"{letter}-{stamp}{SUFFIX}{PART}"
        counter = 1
        while any(path.with_name(path.name.removesuffix(PART) + end).exists() for end in ENDINGS):
            counter += 1
            path = self._directory / f"{letter}-{stamp}-{counter}{SUFFIX}{PART}"
        handle = _create(path)
        self._open[letter] = _Open(path, handle, now)
        header = segment_header(letter, wall, begin.config, begin.players)
        self._append(self._open[letter], encode_header(header))
        log.info("recording city %s in %s", letter, path.name)
        return True

    def _has_room(self) -> bool:
        fits = make_room(self._directory, self._reserve, self.config.max_bytes)
        free = shutil.disk_usage(self._directory).free
        fits = fits and free >= self.config.min_free_bytes + self._reserve
        self._dir_bytes = directory_bytes(self._directory)
        if not fits and not self._short_of_room:
            log.warning(
                "no room for a recording in %s (%d bytes of segments, cap %d; %d bytes free, "
                "%d kept free); recording paused until there is",
                self._directory,
                self._dir_bytes,
                self.config.max_bytes,
                free,
                self.config.min_free_bytes,
            )
        elif fits and self._short_of_room:
            log.info("room for recordings again in %s", self._directory)
        self._short_of_room = not fits
        return fits

    def _write_keyframe(
        self,
        letter: str,
        now: float,
        reason: str,
        snapshot: Sequence[Payload],
        state: Mapping[str, Any],
    ) -> None:
        segment = self._open.get(letter)
        if segment is None:
            return
        encoded = (self._encode(item) for item in snapshot)
        data = keyframe_records(
            segment.delta(now), reason, (d for d in encoded if d is not None), state
        )
        self._append(segment, data, letter=letter, now=now)

    def _write(
        self, letter: str, now: float, kind: Kind, player: int | None, payload: bytes
    ) -> None:
        segment = self._open.get(letter)
        if segment is not None:
            record = encode_record(segment.delta(now), kind, player, payload)
            self._append(segment, record, letter=letter, now=now)

    def _append(
        self, segment: _Open, data: bytes, *, letter: str | None = None, now: float = 0.0
    ) -> None:
        segment.handle.write(data)
        self._dir_bytes += len(data)
        if letter is not None and self._dir_bytes > self.config.max_bytes:
            log.warning(
                "recordings reached their %d-byte cap; city %s paused until its next segment",
                self.config.max_bytes,
                letter,
            )
            self._finish(letter, now, "full")

    def _finish(self, letter: str, now: float, reason: str) -> None:
        """Close ``letter``'s segment with an END record and give it its finished name."""
        segment = self._open.pop(letter, None)
        if segment is None:
            return
        try:
            record = encode_record(segment.delta(now), Kind.END, None, _json({"reason": reason}))
            segment.handle.write(record)
            self._dir_bytes += len(record)
        finally:
            segment.handle.close()
        segment.path.rename(segment.path.with_name(segment.path.name.removesuffix(PART)))

    def _close_all(self) -> None:
        """At the writer's end: close cleanly when closing, else (stopped) just close and give
        each segment its finished name, readable as far as it got. Never raises."""
        for letter in list(self._open):
            try:
                if self._stopped:
                    segment = self._open.pop(letter)
                    segment.handle.close()
                    segment.path.rename(
                        segment.path.with_name(segment.path.name.removesuffix(PART))
                    )
                else:
                    self._finish(letter, self._open[letter].base, "shutdown")
            except OSError:
                self._open.pop(letter, None)
                log.warning("could not close the recording of city %s", letter, exc_info=True)


def _create(path: Path) -> BinaryIO:
    """A new segment file with mode ``RECORDING_FILE_MODE``, whatever the umask: ``fchmod`` is not
    masked, as the mode given to ``open`` is."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, policy.RECORDING_FILE_MODE)
    try:
        os.fchmod(fd, policy.RECORDING_FILE_MODE)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def chat_style(message: ClientMessage | bytes) -> int | None:
    """The style of a client chat message, None for anything else (or chat that does not
    decode, which the city would not take either)."""
    if isinstance(message, bytes):
        if not message.startswith(b"U"):
            return None
        try:
            message = decode_client(message)
        except DecodeError:
            return None
    return message.style if isinstance(message, client.Chat) else None
