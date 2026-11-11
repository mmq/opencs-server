"""A segment's records as raw bytes, cut into one-minute chunks: the archive's unit.

A chunk is the records' bytes exactly as the segment holds them, so the chunks of a segment, joined,
are the segment after its header. A record's time is a delta from the record before it; a chunk's
first record keeps its delta, and the archive's index holds each chunk's start time, so a chunk is
read on its own (``read_records(data, base_ms)``) without the ones before it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from opencs.recording import MAGIC, Kind, SegmentError

#: The archive's unit: one chunk holds the records of one minute of a segment.
CHUNK_MS = 60_000

_VARINT_MAX_BYTES = 10


def read_varint(data: bytes, pos: int) -> tuple[int, int] | None:
    """The unsigned LEB128 varint at ``pos`` and the position after it, or None if ``data`` ends
    inside it or it runs past 10 bytes."""
    value = shift = 0
    for i in range(pos, min(len(data), pos + _VARINT_MAX_BYTES)):
        byte = data[i]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i + 1
        shift += 7
    return None


@dataclass(frozen=True, slots=True)
class RawRecord:
    """A record as it lies in the bytes: ``start``/``end`` are its offsets, ``at_ms`` its time
    (the base given plus every delta so far), ``source`` 0 for the server, else player id + 1."""

    start: int
    end: int
    delta_ms: int
    at_ms: int
    kind: Kind
    source: int
    payload: bytes


def read_records(data: bytes, base_ms: int = 0, pos: int = 0) -> Iterator[RawRecord]:
    """Every whole record from ``pos``, as the recorder's reader takes them: it stops at the first
    one cut short or not a record, and after an END."""
    at = base_ms
    while pos < len(data):
        delta = read_varint(data, pos)
        if delta is None or delta[1] >= len(data):
            return
        try:
            kind = Kind(data[delta[1]])
        except ValueError:
            return
        source = read_varint(data, delta[1] + 1)
        if source is None:
            return
        length = read_varint(data, source[1])
        if length is None or length[1] + length[0] > len(data):
            return
        end = length[1] + length[0]
        at += delta[0]
        yield RawRecord(pos, end, delta[0], at, kind, source[0], data[length[1] : end])
        pos = end
        if kind is Kind.END:
            return


def records_start(segment: bytes) -> int:
    """The offset of a segment's first record, after its magic, version and JSON header."""
    if not segment.startswith(MAGIC):
        raise SegmentError("not a recording segment (bad magic)")
    version = read_varint(segment, len(MAGIC))
    length = None if version is None else read_varint(segment, version[1])
    if length is None or length[1] + length[0] > len(segment):
        raise SegmentError("header cut short")
    return length[1] + length[0]


@dataclass(frozen=True, slots=True)
class Chunk:
    """The records of one minute: ``start_ms`` is the segment time the deltas count from (the time
    of the record before the first, or 0), ``first_ms``/``last_ms`` the first and last record's."""

    index: int
    start_ms: int
    first_ms: int
    last_ms: int
    records: int
    data: bytes


def chunks(segment: bytes, chunk_ms: int = CHUNK_MS) -> list[Chunk]:
    """A segment's whole records in chunks of ``chunk_ms`` of segment time (a record belongs to the
    chunk its time falls in; empty minutes have no chunk). Bytes after the last whole record are
    left out, as the reader leaves them."""
    out: list[Chunk] = []
    current: list[RawRecord] = []
    base = 0

    def close() -> None:
        first, last = current[0], current[-1]
        out.append(
            Chunk(
                first.at_ms // chunk_ms,
                base,
                first.at_ms,
                last.at_ms,
                len(current),
                segment[first.start : last.end],
            )
        )

    previous_at = 0
    for record in read_records(segment, 0, records_start(segment)):
        if current and record.at_ms // chunk_ms != current[0].at_ms // chunk_ms:
            close()
            current = []
        if not current:
            base = previous_at
        current.append(record)
        previous_at = record.at_ms
    if current:
        close()
    return out
