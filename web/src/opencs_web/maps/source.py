"""Where a city's records come from for a time: the archive (ADR 15) and the segments the archiver
has not taken yet, finished or still being written (ADR 14). Read-only on both.

:meth:`Recordings.items` gives the records of a city from the keyframe at or before a time up to
another, each segment opened by its header, so a :class:`~opencs_web.maps.track.CityTrack` built
from them knows the city at the first time (seeking) and what happened after it.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencs.recording import Kind, SegmentError, read_segment, segment_age
from opencs_web.recordings.archive import (
    ANY_SEGMENT_NAME,
    SCHEMA_VERSION,
    ArchiveError,
    decompress,
)
from opencs_web.recordings.chunks import read_records

#: A segment that began this long before a time may still hold the city's state at it: segments
#: are rotated about every five minutes.
LOOKBACK_MS = 15 * 60_000
#: Recorded spans of a city closer than this are shown as one.
JOIN_MS = 60_000


@dataclass(frozen=True, slots=True)
class SegmentRef:
    """A segment of a city: in the archive (``archive_id``) or a file (``path``). ``open`` is a
    ``.part``, still being written, so what is known of it may grow."""

    name: str
    city: str
    start_ms: int
    end_ms: int
    archive_id: int | None = None
    path: Path | None = None
    open: bool = False


@dataclass(frozen=True, slots=True)
class Header:
    """A segment (or a live feed's epoch) begins: its JSON header."""

    header: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Item:
    """A record at ``at`` (UTC ms)."""

    at: int
    kind: Kind
    player: int | None
    payload: bytes


@dataclass(frozen=True, slots=True)
class _File:
    header: Mapping[str, Any]
    items: tuple[Item, ...]
    complete: bool


@functools.lru_cache(maxsize=16)
def _read_file(path: Path, size: int, mtime_ns: int) -> _File:
    """A segment file, read once per size and change time (a ``.part`` grows)."""
    segment = read_segment(path.read_bytes())
    t0 = int(segment.header.get("started_utc_ms", 0))
    items = tuple(Item(t0 + r.at_ms, r.kind, r.player, r.payload) for r in segment.records)
    return _File(segment.header, items, segment.complete)


def read_file(path: Path) -> _File | None:
    try:
        stat = path.stat()
        return _read_file(path, stat.st_size, stat.st_mtime_ns)
    except (OSError, SegmentError):
        return None


class Recordings:
    """The archive at ``archive`` and the recorder's directory ``directory`` (either may be None
    or missing). Each call opens what it reads, so it may be used from any thread."""

    def __init__(self, archive: Path | None, directory: Path | None) -> None:
        self.archive = archive
        self.directory = directory

    # --- what there is -----------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection | None:
        if self.archive is None or not Path(self.archive).is_file():
            return None
        try:
            db = sqlite3.connect(f"file:{self.archive}?mode=ro", uri=True)
            row = db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        except sqlite3.Error:
            return None
        if row is None or int(row[0]) != SCHEMA_VERSION:
            db.close()
            return None
        return db

    def segments(self, city: str | None = None) -> list[SegmentRef]:
        """Every segment (of ``city``), oldest first: the archive's, and the files not in it."""
        refs: dict[str, SegmentRef] = {}
        db = self._db()
        if db is not None:
            try:
                query = "SELECT id, name, city, start_ms, end_ms FROM segments"
                rows = (
                    db.execute(query + " WHERE city = ?", (city,))
                    if city is not None
                    else db.execute(query)
                ).fetchall()
            finally:
                db.close()
            for sid, name, letter, start, end in rows:
                refs[name] = SegmentRef(name, letter, start, end, archive_id=sid)
        for path in self._files(city):
            name = path.name.removesuffix(".part").removesuffix(".done")
            if name in refs:
                continue
            got = read_file(path)
            if got is None or not got.items:
                continue
            refs[name] = SegmentRef(
                name,
                str(got.header.get("city", name[:1])),
                got.items[0].at,
                got.items[-1].at,
                path=path,
                open=path.name.endswith(".part"),
            )
        return sorted(refs.values(), key=lambda r: (r.start_ms, r.name))

    def _files(self, city: str | None) -> list[Path]:
        if self.directory is None:
            return []
        try:
            found = [
                p
                for p in Path(self.directory).iterdir()
                if ANY_SEGMENT_NAME.fullmatch(p.name) and (city is None or p.name[0] == city)
            ]
        except OSError:
            return []
        return sorted(found, key=segment_age)

    def spans(self) -> dict[str, list[tuple[int, int]]]:
        """Each city's recorded spans, ``(start, end)`` in UTC ms, oldest first."""
        out: dict[str, list[tuple[int, int]]] = {}
        for ref in self.segments():
            spans = out.setdefault(ref.city, [])
            if spans and ref.start_ms - spans[-1][1] <= JOIN_MS:
                spans[-1] = (spans[-1][0], max(spans[-1][1], ref.end_ms))
            else:
                spans.append((ref.start_ms, ref.end_ms))
        return out

    def header(self, ref: SegmentRef) -> Mapping[str, Any] | None:
        if ref.archive_id is not None:
            db = self._db()
            if db is None:
                return None
            try:
                row = db.execute(
                    "SELECT header FROM segments WHERE id = ?", (ref.archive_id,)
                ).fetchone()
            finally:
                db.close()
            return None if row is None else dict(json.loads(row[0]))
        got = read_file(ref.path) if ref.path is not None else None
        return None if got is None else got.header

    def latest_header(self, city: str) -> Mapping[str, Any] | None:
        """The header of the city's newest segment: its configuration as last recorded."""
        for ref in reversed(self.segments(city)):
            header = self.header(ref)
            if header is not None:
                return header
        return None

    # --- records -----------------------------------------------------------------------------

    def items(self, city: str, start: int, end: int) -> tuple[list[Header | Item], bool]:
        """The records of ``city`` that make its track over ``[start, end)``: from the keyframe at
        or before ``start`` in the segment that holds ``start`` (if any), then every later segment
        that begins before ``end``, each after its header. Also whether they are final: no segment
        read is still being written."""
        refs = [r for r in self.segments(city) if r.end_ms >= start - LOOKBACK_MS]
        before = [r for r in refs if r.start_ms <= start]
        chosen = ([before[-1]] if before else []) + [r for r in refs if start < r.start_ms < end]
        out: list[Header | Item] = []
        final = True
        for i, ref in enumerate(chosen):
            header = self.header(ref)
            if header is None:
                continue
            final = final and not ref.open
            out.append(Header(header))
            out += self._records(ref, start if i == 0 else ref.start_ms, end)
        return out, final

    def _records(self, ref: SegmentRef, start: int, end: int) -> Iterator[Item]:
        """``ref``'s records before ``end`` from its last keyframe at or before ``start``."""
        if ref.archive_id is not None:
            yield from self._archived(ref.archive_id, start, end)
            return
        got = read_file(ref.path) if ref.path is not None else None
        if got is None:
            return
        first = 0
        for i, item in enumerate(got.items):
            if item.at > start:
                break
            if item.kind is Kind.KEYFRAME:
                first = i
        for item in got.items[first:]:
            if item.at >= end:
                break
            yield item

    def _archived(self, segment_id: int, start: int, end: int) -> Iterator[Item]:
        db = self._db()
        if db is None:
            return
        try:
            rows = db.execute(
                "SELECT id, base_ms, first_ms, keyframe, codec FROM chunks "
                "WHERE segment_id = ? ORDER BY first_ms, id",
                (segment_id,),
            ).fetchall()

            def data(chunk_id: int, base: int, codec: int) -> list[Item]:
                (blob,) = db.execute("SELECT data FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
                raw = decompress(codec, blob)
                return [
                    Item(r.at_ms, r.kind, None if r.source == 0 else r.source - 1, r.payload)
                    for r in read_records(raw, base)
                ]

            # The last chunk holding a keyframe at or before ``start``, and where in it.
            first_chunk, first_item, loaded = 0, 0, None
            for index in range(len(rows) - 1, -1, -1):
                chunk_id, base, first_ms, keyframe, codec = rows[index]
                if not keyframe or first_ms > start:
                    continue
                items = data(chunk_id, base, codec)
                at = [
                    i for i, it in enumerate(items) if it.kind is Kind.KEYFRAME and it.at <= start
                ]
                if at:
                    first_chunk, first_item, loaded = index, at[-1], items
                    break
            for index in range(first_chunk, len(rows)):
                chunk_id, base, first_ms, _, codec = rows[index]
                if first_ms >= end:
                    break
                items = (
                    loaded
                    if index == first_chunk and loaded is not None
                    else data(chunk_id, base, codec)
                )
                for item in items[first_item if index == first_chunk else 0 :]:
                    if item.at >= end:
                        return
                    yield item
        except (sqlite3.Error, ArchiveError):
            return
        finally:
            db.close()
