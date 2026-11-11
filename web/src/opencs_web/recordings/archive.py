"""The recording archive: finished segments (ADR 14) stored as compressed one-minute chunks in a
SQLite file of their own, with an index by city, time and game, and tables of each game's pilots and
battle events (ADR 15 for the chunks).

**A game** is a run of one city's segments recorded without a break: it begins with a segment whose
keyframe is not a rotation (the first pilot to record joined, or a ``/RESUME`` into an unrecorded
city) and takes in each next segment of the city that begins with a ``rotate`` keyframe within
``GAME_GAP_MS`` of the last one's end, after that one ended with ``rotate``. It ends with a segment
that ends otherwise: ``empty`` (the last pilot left), ``shutdown``, ``full``, or cut short by a
crash. So a game is the time a city had a pilot to record in it.

**Archiving** (``Archive.add_segment``) is one transaction per segment. Its file name and SHA-256
go in ``files``, so a segment archived but not yet deleted (a crash between the commit and the
unlink) is deleted by the next run, not archived again. The database is synchronous ``FULL``:
a commit is on the disk before the segment is deleted.

**Reading** (``Archive.records``): the records of a city between two times, from the chunks that
cover the window, byte for byte as the segment held them.

**Retention** (``Archive.prune``): whole games, oldest first, past an age or a size.

**Forgetting a callsign** (``Archive.forget``): see its description and OPERATING.
"""

from __future__ import annotations

import calendar
import dataclasses
import hashlib
import json
import logging
import lzma
import re
import sqlite3
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opencs import policy
from opencs.messages import (
    DecodeError,
    client,
    client_payload,
    decode_client,
    decode_server,
    server,
    server_payload,
)
from opencs.recording import MAGIC, Kind, SegmentError, read_segment, segment_age, varint
from opencs.wire.codec import BYTE_MIN
from opencs_web.recordings import transform
from opencs_web.recordings.chunks import (
    Chunk,
    RawRecord,
    chunks,
    read_records,
    read_varint,
    records_start,
)

log = logging.getLogger("opencs_web.archive")

SCHEMA_VERSION = 1

#: The compressor of a chunk (its ``codec``): xz, LZMA2 preset 6 with a 256 KiB dictionary (ADR 15).
CODEC_XZ = 1
XZ_FILTERS: list[Mapping[str, Any]] = [{"id": lzma.FILTER_LZMA2, "preset": 6, "dict_size": 1 << 18}]

#: A segment's last chunk is folded into the one before when all of it lies within this much of
#: the start of its minute: segments run a little over five minutes, so it holds only the last
#: records (the END, a rotation's last reports).
FOLD_MS = 10_000

#: A rotated segment continues its city's game if it starts within this of the game's end.
GAME_GAP_MS = 60_000

#: A finished segment's file name, as the server makes it (``opencs.recording``).
SEGMENT_NAME = re.compile(r"[A-Z]-\d{8}T\d{6}Z(?:-\d+)?\.ocsrec")
#: Any segment file of the server's, finished, open (``.part``) or marked done.
ANY_SEGMENT_NAME = re.compile(r"[A-Z]-\d{8}T\d{6}Z(?:-\d+)?\.ocsrec(?:\.part|\.done)?")

_DAY_MS = 86_400_000

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Times are UTC milliseconds since the epoch.
CREATE TABLE games (
    id INTEGER PRIMARY KEY,
    city TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    segments INTEGER NOT NULL,
    stored_bytes INTEGER NOT NULL,      -- headers and chunks, for the size cap
    end_reason TEXT,                    -- the last segment's END reason; NULL if it was cut short
    open INTEGER NOT NULL               -- 1 while the last segment ended with a rotation
);
CREATE INDEX games_city_time ON games (city, start_ms);
CREATE INDEX games_time ON games (start_ms);
CREATE TABLE segments (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games (id),
    name TEXT NOT NULL UNIQUE,
    city TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    version INTEGER NOT NULL,           -- the segment format (ADR 14)
    header BLOB NOT NULL,               -- the JSON header, as the file held it
    records INTEGER NOT NULL,
    record_bytes INTEGER NOT NULL,      -- the records' bytes before compression
    ignored INTEGER NOT NULL,           -- bytes after the last whole record, not kept
    complete INTEGER NOT NULL,          -- whether it has an END
    end_reason TEXT
);
CREATE INDEX segments_game ON segments (game_id);
CREATE INDEX segments_city_time ON segments (city, start_ms);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES segments (id),
    city TEXT NOT NULL,
    base_ms INTEGER NOT NULL,           -- the time the first record's delta counts from
    first_ms INTEGER NOT NULL,
    last_ms INTEGER NOT NULL,
    records INTEGER NOT NULL,
    record_bytes INTEGER NOT NULL,
    keyframe INTEGER NOT NULL,          -- whether a keyframe begins in it
    codec INTEGER NOT NULL,             -- CODEC_XZ
    transform INTEGER NOT NULL,         -- the transform's mode (also the first byte decompressed)
    data BLOB NOT NULL
);
CREATE INDEX chunks_city_time ON chunks (city, first_ms);
CREATE INDEX chunks_segment ON chunks (segment_id);
CREATE TABLE pilots (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games (id),
    player_id INTEGER NOT NULL,
    callsign TEXT NOT NULL,
    team INTEGER NOT NULL,
    bot INTEGER NOT NULL,
    joined_ms INTEGER NOT NULL,
    left_ms INTEGER NOT NULL,           -- or the end of the game so far
    present INTEGER NOT NULL            -- 1 if still in at the end of the game's last segment
);
CREATE INDEX pilots_callsign ON pilots (callsign COLLATE NOCASE);
CREATE INDEX pilots_game ON pilots (game_id);
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games (id),
    at_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,                 -- 'death', 'kill' or 'tower'
    player_id INTEGER,                  -- death: who died; kill: who was paid (NULL if not known)
    callsign TEXT,
    team INTEGER,                       -- tower: the tower's team
    other_id INTEGER,                   -- kill: who died; tower: the tower's id
    other_callsign TEXT,
    earned INTEGER                      -- kill: an earned kill
);
CREATE INDEX events_game ON events (game_id, at_ms);
CREATE INDEX events_callsign ON events (callsign COLLATE NOCASE);
CREATE INDEX events_other ON events (other_callsign COLLATE NOCASE);
-- Every segment file archived, so one is never archived twice.
CREATE TABLE files (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, archived_ms INTEGER NOT NULL);
-- Callsigns forgotten while segments that may hold them were still unarchived (Archive.forget).
CREATE TABLE forgotten (callsign TEXT PRIMARY KEY COLLATE NOCASE, forgotten_ms INTEGER NOT NULL);
"""


#: The server messages the battle events are read from: kill notices, destroys, towers.
_EVENT_TYPES = (b"E", b"J", b"M")


class ArchiveError(Exception):
    """The archive cannot be used: a newer schema, a chunk it cannot decode."""


def compress(data: bytes) -> tuple[int, int, bytes]:
    """A chunk's records as stored: ``(codec, transform mode, bytes)``."""
    coded = transform.encode(data)
    return CODEC_XZ, coded[0], lzma.compress(coded, format=lzma.FORMAT_XZ, filters=XZ_FILTERS)


def decompress(codec: int, data: bytes) -> bytes:
    """A chunk's records, from what ``compress`` stored."""
    if codec != CODEC_XZ:
        raise ArchiveError(f"unknown chunk codec {codec}")
    try:
        return transform.decode(lzma.decompress(data, format=lzma.FORMAT_XZ))
    except (lzma.LZMAError, transform.TransformError) as exc:
        raise ArchiveError(f"a chunk does not decode: {exc}") from exc


def fold_tail(parts: list[Chunk], fold_ms: int = FOLD_MS, chunk_ms: int = 60_000) -> list[Chunk]:
    """``parts`` with the last chunk joined to the one before it if all of it lies within
    ``fold_ms`` of the start of its minute. Chunks are contiguous, so the bytes simply join."""
    if len(parts) < 2:
        return parts
    *rest, before, last = parts
    if last.last_ms - last.index * chunk_ms >= fold_ms:
        return parts
    joined = Chunk(
        before.index,
        before.start_ms,
        before.first_ms,
        last.last_ms,
        before.records + last.records,
        before.data + last.data,
    )
    return [*rest, joined]


@dataclass(frozen=True, slots=True)
class ArchivedRecord:
    """A record read back: ``at_ms`` its UTC time, ``source`` 0 for the server, else player id + 1,
    ``raw`` its bytes as the segment held them."""

    segment: str
    at_ms: int
    kind: Kind
    source: int
    payload: bytes
    raw: bytes

    @property
    def player(self) -> int | None:
        return None if self.source == 0 else self.source - 1


@dataclass
class ArchiveResult:
    """What one ``archive_directory`` run did."""

    archived: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # deleted after an earlier run archived them
    failed: list[str] = field(default_factory=list)
    pruned_games: int = 0


@dataclass
class ForgetResult:
    games: int = 0
    segments: int = 0
    chunks: int = 0
    pilots: int = 0
    events: int = 0
    pending: bool = False  # kept in ``forgotten`` for segments not yet archived


# --- reading a segment's players and battle events --------------------------------------------


@dataclass(slots=True)
class _Pilot:
    row: int  # pilots.id
    player_id: int
    callsign: str
    team: int


class _Game:
    """The pilots and events of a game while one of its segments is archived."""

    def __init__(self, db: sqlite3.Connection, game_id: int) -> None:
        self.db = db
        self.id = game_id
        self.present: dict[int, _Pilot] = {}
        rows = db.execute(
            "SELECT id, player_id, callsign, team FROM pilots WHERE game_id = ? AND present = 1",
            (game_id,),
        )
        for row, player_id, callsign, team in rows:
            self.present[player_id] = _Pilot(row, player_id, callsign, team)
        self.towers: dict[int, int] = {}  # tower id -> team
        self.kills: list[tuple[int, int | None, bool]] = []  # at, payee, earned: before a death

    def start(self, at: int, players: list[Mapping[str, Any]]) -> None:
        """A segment begins with these players (its header's table)."""
        keep: dict[int, _Pilot] = {}
        for p in players:
            pid, callsign = int(p["id"]), str(p["callsign"])
            old = self.present.pop(pid, None)
            if old is not None and old.callsign == callsign and old.team == int(p["team"]):
                keep[pid] = old
            else:
                if old is not None:
                    self._close(old, at)
                keep[pid] = self._join(at, pid, p)
        for old in self.present.values():
            self._close(old, at)
        self.present = keep

    def _join(self, at: int, pid: int, info: Mapping[str, Any]) -> _Pilot:
        callsign, team = str(info["callsign"]), int(info["team"])
        cursor = self.db.execute(
            "INSERT INTO pilots (game_id, player_id, callsign, team, bot, joined_ms, left_ms, "
            "present) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (self.id, pid, callsign, team, int(bool(info.get("bot"))), at, at),
        )
        assert cursor.lastrowid is not None
        return _Pilot(cursor.lastrowid, pid, callsign, team)

    def _close(self, pilot: _Pilot, at: int) -> None:
        self.db.execute("UPDATE pilots SET left_ms = ?, present = 0 WHERE id = ?", (at, pilot.row))

    def record(self, at: int, record: RawRecord) -> None:
        if record.kind is Kind.PLAYER:
            self._flush_kills()
            pid = record.source - 1
            old = self.present.pop(pid, None)
            if old is not None:
                self._close(old, at)
            if record.payload:
                try:
                    info = json.loads(record.payload)
                    self.present[pid] = self._join(at, pid, info)
                except (ValueError, KeyError, TypeError):
                    log.warning("a player record that is not a player: %r", record.payload)
        elif record.kind in (Kind.SERVER, Kind.SNAPSHOT) and record.payload[:1] in _EVENT_TYPES:
            try:
                message, _ = decode_server(record.payload)
            except DecodeError:
                return
            self._server(at, record, message)

    def _server(self, at: int, record: RawRecord, message: object) -> None:
        if isinstance(message, server.SpawnTower):
            self.towers[message.id] = message.team
        elif isinstance(message, server.KillNotice):
            payee = record.source - 1 if record.source else None
            self.kills.append((at, payee, message.earned))
        elif isinstance(message, server.Destroy):
            low, high = policy.PLAYER_IDS
            victim = self.present.get(message.id) if low <= message.id <= high else None
            if victim is not None:
                self._event(at, "death", victim.player_id, victim.callsign, victim.team)
                for kill_at, payee, earned in self.kills:
                    if kill_at == at:
                        self._kill(at, payee, victim, earned)
                    else:
                        self._kill(kill_at, payee, None, earned)
                self.kills.clear()
            elif message.id in self.towers:
                team = self.towers.pop(message.id)
                self._event(at, "tower", None, None, team, other_id=message.id)

    def _flush_kills(self) -> None:
        for kill_at, payee, earned in self.kills:
            self._kill(kill_at, payee, None, earned)
        self.kills.clear()

    def _kill(self, at: int, payee: int | None, victim: _Pilot | None, earned: bool) -> None:
        paid = self.present.get(payee) if payee is not None else None
        self._event(
            at,
            "kill",
            payee,
            paid.callsign if paid else None,
            paid.team if paid else None,
            other_id=victim.player_id if victim else None,
            other_callsign=victim.callsign if victim else None,
            earned=earned,
        )

    def _event(
        self,
        at: int,
        kind: str,
        player_id: int | None,
        callsign: str | None,
        team: int | None,
        *,
        other_id: int | None = None,
        other_callsign: str | None = None,
        earned: bool | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO events (game_id, at_ms, kind, player_id, callsign, team, other_id, "
            "other_callsign, earned) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.id,
                at,
                kind,
                player_id,
                callsign,
                team,
                other_id,
                other_callsign,
                None if earned is None else int(earned),
            ),
        )

    def finish(self, at: int, rotated: bool) -> None:
        """The segment ended at ``at``: by a rotation (the pilots stay present for the next one)
        or otherwise (the game is over)."""
        self._flush_kills()
        for pilot in self.present.values():
            self.db.execute(
                "UPDATE pilots SET left_ms = ?, present = ? WHERE id = ?",
                (at, int(rotated), pilot.row),
            )


# --- forgetting a callsign --------------------------------------------------------------------


def _word(callsign: str) -> re.Pattern[bytes]:
    """The callsign as a word in chat, in any case."""
    return re.compile(
        rb"(?<![A-Za-z0-9])" + re.escape(callsign.encode("ascii", "replace")) + rb"(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def blank(callsign: str) -> str:
    """What a forgotten callsign becomes: as many ``#`` as it has characters."""
    return "#" * len(callsign)


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


class _Scrub:
    """Rewrites one segment's header and records without a callsign: its PLAYER rows, its
    ``'G'`` introductions, its pilot's own chat (emptied), and the callsign as a word in any
    chat line."""

    def __init__(self, callsign: str) -> None:
        self.key = callsign.lower()
        self.blank = blank(callsign).encode()
        self.word = _word(callsign)
        self.players: dict[int, str] = {}  # player id -> original callsign, as the records go

    def header(self, raw: bytes) -> bytes | None:
        """The header without the callsign, or None if it has none."""
        header = json.loads(raw)
        self.players = {int(p["id"]): str(p["callsign"]) for p in header.get("players", [])}
        changed = False
        for p in header.get("players", []):
            if str(p["callsign"]).lower() == self.key:
                p["callsign"] = self.blank.decode()
                changed = True
        return _json(header) if changed else None

    def chunk(self, data: bytes) -> bytes | None:
        """The chunk's records without the callsign, or None if they hold none."""
        out = bytearray()
        changed = False
        for record in read_records(data):
            payload = self._payload(record)
            if payload is None:
                out += data[record.start : record.end]
            else:
                changed = True
                out += varint(record.delta_ms) + bytes([record.kind]) + varint(record.source)
                out += varint(len(payload)) + payload
        return bytes(out) if changed else None

    def _payload(self, record: RawRecord) -> bytes | None:
        """The record's payload without the callsign, or None if it is unchanged."""
        if record.kind is Kind.PLAYER:
            return self._player(record)
        if record.kind is Kind.CLIENT and record.payload[:1] == b"U":
            return self._chat(record)
        if record.kind in (Kind.SERVER, Kind.SNAPSHOT) and record.payload[:1] in (b"G", b"a"):
            return self._server(record.payload)
        return None

    def _player(self, record: RawRecord) -> bytes | None:
        pid = record.source - 1
        if not record.payload:
            self.players.pop(pid, None)
            return None
        try:
            info = json.loads(record.payload)
            self.players[pid] = str(info["callsign"])
        except (ValueError, KeyError, TypeError):
            return None
        if self.players[pid].lower() != self.key:
            return None
        info["callsign"] = self.blank.decode()
        return _json(info)

    def _chat(self, record: RawRecord) -> bytes | None:
        try:
            said = decode_client(record.payload)
        except DecodeError:
            return None
        if not isinstance(said, client.Chat):
            return None
        mine = self.players.get(record.source - 1, "").lower() == self.key
        text = b"" if mine else self.word.sub(self.blank, said.text)
        if text == said.text:
            return None
        return client_payload(dataclasses.replace(said, text=text))

    def _server(self, payload: bytes) -> bytes | None:
        try:
            message, tag = decode_server(payload)
        except DecodeError:
            return None
        tag = BYTE_MIN if tag is None else tag
        if isinstance(message, server.Introduce):
            if message.callsign.decode("ascii", "replace").lower() != self.key:
                return None
            return server_payload(dataclasses.replace(message, callsign=self.blank), tag)
        if isinstance(message, server.Chat):
            text = self.word.sub(self.blank, message.text)
            if text == message.text:
                return None
            return server_payload(dataclasses.replace(message, text=text), tag)
        return None


# --- the archive ------------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stamp_ms(name: str) -> int:
    """The UTC start in a segment's name, in ms."""
    stamp, _ = segment_age(Path(name))
    return calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ")) * 1000


class Archive:
    """The archive in ``path`` (made if missing). Use as a context manager, or ``close()``."""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA secure_delete = ON")  # a forgotten callsign leaves no free page
        self.db.execute("PRAGMA synchronous = FULL")  # committed before a segment is deleted
        version = self._meta("schema")
        if version is None:
            self.db.execute("PRAGMA auto_vacuum = INCREMENTAL")  # before the first table
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.executescript(
                f"BEGIN IMMEDIATE; {_SCHEMA} "
                f"INSERT INTO meta (key, value) VALUES ('schema', '{SCHEMA_VERSION}'); COMMIT;"
            )
        elif int(version) != SCHEMA_VERSION:
            self.db.close()
            raise ArchiveError(f"archive schema {version}, this code knows {SCHEMA_VERSION}")

    def _meta(self, key: str) -> str | None:
        try:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None  # no tables yet
        return None if row is None else str(row[0])

    def _transaction(self) -> _Transaction:
        return _Transaction(self.db)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Archive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- archiving --------------------------------------------------------------------------

    def archived(self, name: str) -> str | None:
        """The SHA-256 of the segment archived under ``name``, or None."""
        row = self.db.execute("SELECT sha256 FROM files WHERE name = ?", (name,)).fetchone()
        return None if row is None else str(row[0])

    def add_segment(self, name: str, data: bytes, *, now_ms: int) -> bool:
        """Archive a segment's bytes, in one transaction. False if a segment of that name was
        already archived (then nothing changes). Raises ``SegmentError`` for bytes that are not a
        segment this code reads."""
        segment = read_segment(data)  # checks the magic and the format version
        start = records_start(data)
        header_raw = _header_bytes(data, start)
        parts = fold_tail(chunks(data))
        header = segment.header
        city = str(header.get("city", name[:1]))
        t0 = int(header.get("started_utc_ms", _stamp_ms(name)))
        records = segment.records
        end = t0 + (records[-1].at_ms if records else 0)
        end_reason = None
        if segment.complete:
            state = records[-1].state()
            end_reason = str(state.get("reason")) if isinstance(state, dict) else None
        first_reason = None
        if records and records[0].kind is Kind.KEYFRAME:
            state = records[0].state()
            first_reason = state.get("reason") if isinstance(state, dict) else None
        stored = [(c, *compress(c.data)) for c in parts]
        stored_bytes = len(header_raw) + sum(len(blob) for *_, blob in stored)
        with self._transaction():
            if self.archived(name) is not None:
                return False
            self.db.execute(
                "INSERT INTO files (name, sha256, archived_ms) VALUES (?, ?, ?)",
                (name, _sha256(data), now_ms),
            )
            game_id = self._game_for(city, t0, first_reason)
            game = _Game(self.db, game_id)
            game.start(t0, list(header.get("players", [])))
            cursor = self.db.execute(
                "INSERT INTO segments (game_id, name, city, start_ms, end_ms, version, header, "
                "records, record_bytes, ignored, complete, end_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    game_id,
                    name,
                    city,
                    t0,
                    end,
                    _format_version(data),
                    header_raw,
                    len(records),
                    sum(len(c.data) for c in parts),
                    segment.ignored,
                    int(segment.complete),
                    end_reason,
                ),
            )
            segment_id = cursor.lastrowid
            assert segment_id is not None
            for chunk, codec, mode, blob in stored:
                rows = list(read_records(chunk.data, t0 + chunk.start_ms))
                for record in rows:
                    game.record(record.at_ms, record)
                self.db.execute(
                    "INSERT INTO chunks (segment_id, city, base_ms, first_ms, last_ms, records, "
                    "record_bytes, keyframe, codec, transform, data) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        segment_id,
                        city,
                        t0 + chunk.start_ms,
                        t0 + chunk.first_ms,
                        t0 + chunk.last_ms,
                        chunk.records,
                        len(chunk.data),
                        int(any(r.kind is Kind.KEYFRAME for r in rows)),
                        codec,
                        mode,
                        blob,
                    ),
                )
            rotated = end_reason == "rotate"
            game.finish(end, rotated)
            self.db.execute(
                "UPDATE games SET end_ms = max(end_ms, ?), segments = segments + 1, "
                "stored_bytes = stored_bytes + ?, end_reason = ?, open = ? WHERE id = ?",
                (end, stored_bytes, end_reason, int(rotated), game_id),
            )
            self._apply_forgotten(segment_id, t0)
        return True

    def _game_for(self, city: str, t0: int, first_reason: object) -> int:
        """The game a segment of ``city`` starting at ``t0`` belongs to: its city's open game if
        the segment is a rotation soon after it, else a new one (closing the open one)."""
        row = self.db.execute(
            "SELECT id, end_ms FROM games WHERE city = ? AND open = 1 "
            "ORDER BY start_ms DESC LIMIT 1",
            (city,),
        ).fetchone()
        if row is not None:
            game_id, end_ms = row
            if first_reason == "rotate" and abs(t0 - end_ms) <= GAME_GAP_MS:
                return int(game_id)
            self.db.execute("UPDATE games SET open = 0 WHERE id = ?", (game_id,))
            self.db.execute(
                "UPDATE pilots SET present = 0 WHERE game_id = ? AND present = 1", (game_id,)
            )
        cursor = self.db.execute(
            "INSERT INTO games (city, start_ms, end_ms, segments, stored_bytes, open) "
            "VALUES (?, ?, ?, 0, 0, 0)",
            (city, t0, t0),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    # --- reading ----------------------------------------------------------------------------

    def records(self, city: str, start_ms: int, end_ms: int) -> Iterator[ArchivedRecord]:
        """Every record of ``city`` from ``start_ms`` up to (not including) ``end_ms``, UTC ms,
        in order: the covering chunks decompressed and cut to the window."""
        rows = self.db.execute(
            "SELECT c.base_ms, c.codec, c.data, s.name FROM chunks c "
            "JOIN segments s ON s.id = c.segment_id "
            "WHERE c.city = ? AND c.first_ms < ? AND c.last_ms >= ? "
            "ORDER BY c.first_ms, c.id",
            (city, end_ms, start_ms),
        ).fetchall()
        for base, codec, blob, name in rows:
            data = decompress(codec, blob)
            for r in read_records(data, base):
                if start_ms <= r.at_ms < end_ms:
                    yield ArchivedRecord(
                        name, r.at_ms, r.kind, r.source, r.payload, data[r.start : r.end]
                    )

    def segment_header(self, name: str) -> dict[str, Any]:
        """A segment's header (config, player table), as JSON."""
        row = self.db.execute("SELECT header FROM segments WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        header: dict[str, Any] = json.loads(row[0])
        return header

    def segment_bytes(self, name: str) -> bytes:
        """A segment file rebuilt: magic, version, header and every whole record (bytes after the
        last whole record were not kept)."""
        row = self.db.execute(
            "SELECT id, version, header FROM segments WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise KeyError(name)
        segment_id, version, header = row
        out = bytearray(MAGIC + varint(version) + varint(len(header)) + header)
        for codec, blob in self.db.execute(
            "SELECT codec, data FROM chunks WHERE segment_id = ? ORDER BY first_ms, id",
            (segment_id,),
        ):
            out += decompress(codec, blob)
        return bytes(out)

    def stored_bytes(self) -> int:
        row = self.db.execute("SELECT coalesce(sum(stored_bytes), 0) FROM games").fetchone()
        return int(row[0])

    # --- retention --------------------------------------------------------------------------

    def prune(self, *, now_ms: int, max_days: int, max_bytes: int) -> int:
        """Remove whole games, oldest first: those that ended over ``max_days`` ago, then until
        the archive holds at most ``max_bytes``. The number of games removed."""
        cutoff = now_ms - max_days * _DAY_MS
        removed = 0
        old = self.db.execute(
            "SELECT id FROM games WHERE end_ms < ? ORDER BY start_ms", (cutoff,)
        ).fetchall()
        for (game_id,) in old:
            self._remove_game(game_id)
            removed += 1
        total = self.stored_bytes()
        while total > max_bytes:
            row = self.db.execute(
                "SELECT id, stored_bytes FROM games ORDER BY start_ms, id LIMIT 1"
            ).fetchone()
            if row is None:
                break
            self._remove_game(row[0])
            total -= row[1]
            removed += 1
        with self._transaction():
            self.db.execute("DELETE FROM files WHERE archived_ms < ?", (cutoff,))
        if removed:
            self.db.execute("PRAGMA incremental_vacuum")
        return removed

    def _remove_game(self, game_id: int) -> None:
        with self._transaction():
            self.db.execute(
                "DELETE FROM chunks WHERE segment_id IN "
                "(SELECT id FROM segments WHERE game_id = ?)",
                (game_id,),
            )
            for table in ("segments", "pilots", "events"):
                self.db.execute(f"DELETE FROM {table} WHERE game_id = ?", (game_id,))
            self.db.execute("DELETE FROM games WHERE id = ?", (game_id,))

    # --- forgetting -------------------------------------------------------------------------

    def forget(
        self,
        callsign: str,
        *,
        now_ms: int,
        everywhere: bool = False,
        pending: bool = False,
    ) -> ForgetResult:
        """Remove ``callsign`` from the archive, in one transaction: in each game it flew in (with
        ``everywhere``, in every game), it becomes ``blank(callsign)`` in the pilot and event
        tables, in the segment headers' player tables, in the PLAYER records and in the ``'G'``
        introductions; the pilot's own chat lines are emptied, and the callsign as a word in
        anyone's chat (and in system lines) is blanked. The chunks are rewritten and recompressed.

        With ``pending`` (segments that may hold it are not archived yet), the callsign is kept in
        ``forgotten`` and taken out of each segment archived later that began before now, until
        ``settle_forgotten`` finds none left."""
        result = ForgetResult()
        with self._transaction():
            if everywhere:
                games = [r[0] for r in self.db.execute("SELECT id FROM games")]
            else:
                games = [
                    r[0]
                    for r in self.db.execute(
                        "SELECT DISTINCT game_id FROM pilots WHERE callsign = ? COLLATE NOCASE",
                        (callsign,),
                    )
                ]
            for game_id in games:
                touched = False
                segments = self.db.execute(
                    "SELECT id FROM segments WHERE game_id = ? ORDER BY start_ms, id", (game_id,)
                ).fetchall()
                for (segment_id,) in segments:
                    changed = self._scrub_segment(segment_id, callsign)
                    if changed:
                        result.segments += 1
                        result.chunks += changed - 1
                        touched = True
                result.games += touched
            blanked = blank(callsign)
            result.pilots = self.db.execute(
                "UPDATE pilots SET callsign = ? WHERE callsign = ? COLLATE NOCASE",
                (blanked, callsign),
            ).rowcount
            result.events = self.db.execute(
                "UPDATE events SET callsign = ? WHERE callsign = ? COLLATE NOCASE",
                (blanked, callsign),
            ).rowcount
            result.events += self.db.execute(
                "UPDATE events SET other_callsign = ? WHERE other_callsign = ? COLLATE NOCASE",
                (blanked, callsign),
            ).rowcount
            if pending:
                self.db.execute(
                    "INSERT OR REPLACE INTO forgotten (callsign, forgotten_ms) VALUES (?, ?)",
                    (callsign, now_ms),
                )
                result.pending = True
        return result

    def _scrub_segment(self, segment_id: int, callsign: str) -> int:
        """Take ``callsign`` out of one segment; 0 if it was not there, else 1 plus the number of
        chunks rewritten. Inside the caller's transaction."""
        scrub = _Scrub(callsign)
        (header,) = self.db.execute(
            "SELECT header FROM segments WHERE id = ?", (segment_id,)
        ).fetchone()
        new_header = scrub.header(header)
        delta = 0
        changed = 0
        if new_header is not None:
            self.db.execute("UPDATE segments SET header = ? WHERE id = ?", (new_header, segment_id))
            delta += len(new_header) - len(header)
        rows = self.db.execute(
            "SELECT id, codec, data FROM chunks WHERE segment_id = ? ORDER BY first_ms, id",
            (segment_id,),
        ).fetchall()
        for chunk_id, old_codec, blob in rows:
            records = scrub.chunk(decompress(old_codec, blob))
            if records is None:
                continue
            codec, mode, new = compress(records)
            self.db.execute(
                "UPDATE chunks SET codec = ?, transform = ?, data = ?, record_bytes = ? "
                "WHERE id = ?",
                (codec, mode, new, len(records), chunk_id),
            )
            delta += len(new) - len(blob)
            changed += 1
        if new_header is None and not changed:
            return 0
        self.db.execute(
            "UPDATE games SET stored_bytes = stored_bytes + ? "
            "WHERE id = (SELECT game_id FROM segments WHERE id = ?)",
            (delta, segment_id),
        )
        return 1 + changed

    def _apply_forgotten(self, segment_id: int, start_ms: int) -> None:
        for (callsign,) in self.db.execute(
            "SELECT callsign FROM forgotten WHERE forgotten_ms > ?", (start_ms,)
        ).fetchall():
            if self._scrub_segment(segment_id, callsign):
                blanked = blank(callsign)
                for column in ("callsign", "other_callsign"):
                    self.db.execute(
                        f"UPDATE events SET {column} = ? WHERE {column} = ? COLLATE NOCASE "
                        "AND game_id = (SELECT game_id FROM segments WHERE id = ?)",
                        (blanked, callsign, segment_id),
                    )
                self.db.execute(
                    "UPDATE pilots SET callsign = ? WHERE callsign = ? COLLATE NOCASE "
                    "AND game_id = (SELECT game_id FROM segments WHERE id = ?)",
                    (blanked, callsign, segment_id),
                )

    def settle_forgotten(self, oldest_unarchived_ms: int | None) -> int:
        """Drop the forgotten callsigns no unarchived segment can hold any more: every segment
        left began after they were forgotten (``oldest_unarchived_ms`` is the earliest start of
        those left, None if none). The number dropped."""
        with self._transaction():
            if oldest_unarchived_ms is None:
                return self.db.execute("DELETE FROM forgotten").rowcount
            return self.db.execute(
                "DELETE FROM forgotten WHERE forgotten_ms <= ?", (oldest_unarchived_ms,)
            ).rowcount


class _Transaction:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``, or ``ROLLBACK`` on an exception; nests as a no-op."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.outer = False

    def __enter__(self) -> None:
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
            self.outer = True

    def __exit__(self, kind: object, *exc: object) -> None:
        if not self.outer:
            return
        self.db.execute("ROLLBACK" if kind is not None else "COMMIT")


def _format_version(data: bytes) -> int:
    got = read_varint(data, len(MAGIC))
    assert got is not None  # read_segment has read it
    return got[0]


def _header_bytes(data: bytes, start: int) -> bytes:
    """The JSON header of a segment whose records start at ``start``."""
    version = read_varint(data, len(MAGIC))
    assert version is not None
    length = read_varint(data, version[1])
    assert length is not None
    return data[length[1] : start]


# --- the directory ----------------------------------------------------------------------------


def pending_segments(directory: Path) -> list[Path]:
    """The finished segments in ``directory``, oldest first (by the start in their names)."""
    found = [p for p in directory.iterdir() if SEGMENT_NAME.fullmatch(p.name)]
    return sorted(found, key=segment_age)


def oldest_unarchived_ms(directory: Path) -> int | None:
    """The earliest start of any segment left in ``directory`` (open or finished), or None."""
    starts = [
        _stamp_ms(p.name.removesuffix(".part"))
        for p in directory.iterdir()
        if ANY_SEGMENT_NAME.fullmatch(p.name) and not p.name.endswith(".done")
    ]
    return min(starts, default=None)


def archive_directory(
    archive: Archive, directory: Path, *, now_ms: int, max_days: int, max_bytes: int
) -> ArchiveResult:
    """Archive every finished segment in ``directory``, oldest first, deleting each once its
    transaction has committed; then prune the archive. Never touches a ``.part``. A segment that
    cannot be read is left where it is and named in ``failed``. Segments finished while it runs
    are archived too: it looks again until a look finds nothing new (the path unit may not trigger
    for a change made while this runs)."""
    result = ArchiveResult()
    seen: set[str] = set()
    while batch := [p for p in pending_segments(directory) if p.name not in seen]:
        seen.update(p.name for p in batch)
        _archive_batch(archive, batch, result, now_ms)
    archive.settle_forgotten(oldest_unarchived_ms(directory))
    result.pruned_games = archive.prune(now_ms=now_ms, max_days=max_days, max_bytes=max_bytes)
    return result


def _archive_batch(archive: Archive, batch: list[Path], result: ArchiveResult, now_ms: int) -> None:
    for path in batch:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue  # removed meanwhile (another run)
        except OSError as exc:
            log.error("cannot read %s: %s", path.name, exc)
            result.failed.append(path.name)
            continue
        earlier = archive.archived(path.name) is not None
        if not earlier:
            try:
                earlier = not archive.add_segment(path.name, data, now_ms=now_ms)
            except (SegmentError, ArchiveError) as exc:
                log.error("cannot archive %s: %s", path.name, exc)
                result.failed.append(path.name)
                continue
        if archive.archived(path.name) != _sha256(data):
            log.error(
                "%s differs from the segment archived under its name; left where it is", path.name
            )
            result.failed.append(path.name)
            continue
        (result.removed if earlier else result.archived).append(path.name)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.error("archived %s but cannot delete it: %s", path.name, exc)
            result.failed.append(path.name)
