"""One-minute chunks of a segment and the record transform: both must give back the exact bytes.

The fixture is the first 30 seconds of a 16-bot game's second segment (a keyframe with 16 pods,
then movement). Set ``OPENCS_RECORDINGS`` to a directory of ``.ocsrec`` files to also check every
chunk of every segment there (the compression benchmark's corpus)."""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from opencs.messages import client_payload
from opencs.messages.client import PodState
from opencs.messages.fields import WirePos
from opencs.recording import Kind, encode_record, read_segment
from opencs.wire.codec import BIAS88_FORBIDDEN, BIAS88_MAX, BIAS88_WIRE_MIN
from opencs_web.recordings.chunks import chunks, read_records, records_start
from opencs_web.recordings.transform import (
    MODE_COLUMNS,
    MODE_PREDICT,
    MODE_VERBATIM,
    SINE_TABLE_CRC,
    TransformError,
    decode,
    encode,
    sine_table_crc,
)

SPEEDS = sorted(set(range(BIAS88_WIRE_MIN, BIAS88_MAX + 1)) - BIAS88_FORBIDDEN)
FIXTURE = Path(__file__).parent / "fixtures" / "B-20261111T194024Z-30s.ocsrec"


def _corpus() -> list[Path]:
    paths = [FIXTURE]
    extra = os.environ.get("OPENCS_RECORDINGS")
    if extra:
        paths += sorted(Path(extra).rglob("*.ocsrec"))
    return paths


def _pod(x: int, z: int, orient: int = 900, fwd: int = 30, y: int = 10, **kw: int) -> bytes:
    fields = {"fwd_target": fwd, "v1": 0, "v2": 0, "state": 0, "power": 3000, "weight": 50}
    fields |= {"armor0": 100, "armor1": 100} | kw
    return client_payload(PodState(WirePos.from_world(x, y, z), orient, fwd, **fields))


def _records(*records: tuple[int, Kind, int | None, bytes]) -> bytes:
    return b"".join(encode_record(*r) for r in records)


def _round_trip(chunk: bytes) -> bytes:
    for predict in (False, True):
        assert decode(encode(chunk, predict)) == chunk
    return encode(chunk)


def test_the_sine_table_is_the_one_archives_were_written_with() -> None:
    assert sine_table_crc() == SINE_TABLE_CRC


@pytest.mark.parametrize("path", _corpus(), ids=lambda p: p.name)
def test_every_chunk_of_a_segment_comes_back_byte_for_byte(path: Path) -> None:
    segment = path.read_bytes()
    parts = chunks(segment)
    whole = read_segment(segment)
    assert (
        b"".join(c.data for c in parts)
        == segment[records_start(segment) : len(segment) - whole.ignored]
    )
    assert sum(c.records for c in parts) == len(whole.records)
    for chunk in parts:
        encoded = _round_trip(chunk.data)
        assert len(encoded) <= len(chunk.data) + 1
        if chunk.records > 100:
            assert encoded[0] == MODE_PREDICT


def test_chunks_are_minutes_of_segment_time_and_read_on_their_own() -> None:
    segment = FIXTURE.read_bytes()
    parts = chunks(segment, chunk_ms=10_000)
    assert [c.index for c in parts] == [0, 1, 2, 3]  # the last holds the record at 30.0 s
    whole = read_segment(segment).records
    seen = []
    for chunk in parts:
        records = list(read_records(chunk.data, chunk.start_ms))
        assert all(r.at_ms // 10_000 == chunk.index for r in records)
        assert (records[0].at_ms, records[-1].at_ms) == (chunk.first_ms, chunk.last_ms)
        seen += [(r.at_ms, r.kind, r.payload) for r in records]
    assert seen == [(r.at_ms, r.kind, r.payload) for r in whole]


def test_pod_reports_are_coded_and_other_records_kept() -> None:
    chunk = _records(
        (5, Kind.KEYFRAME, None, b'{"reason":"start"}'),
        (0, Kind.SNAPSHOT, None, b"N\x10\x11\x12"),
        (0, Kind.KEYFRAME_END, None, b""),
        (100, Kind.CLIENT, 3, _pod(1000, 2000)),
        (20, Kind.CLIENT, 4, _pod(3000, 100, orient=0, fwd=0)),
        (480, Kind.CLIENT, 3, _pod(1015, 2000)),
        (500, Kind.CLIENT, 3, _pod(1030, 2001)),
        (3, Kind.SERVER, 0, b"E\x10\x11"),
        (497, Kind.CLIENT, 3, _pod(1045, 2002, orient=-5)),
        (0, Kind.CLIENT, 4, _pod(3000, 100, orient=7300, fwd=-20, v1=-400, state=1)),
        (1, Kind.PLAYER, 4, b""),
        *((500, Kind.CLIENT, 9, _pod(100, 15 * i, orient=0)) for i in range(20)),
        (0, Kind.END, None, b'{"reason":"empty"}'),
    )
    for predict in (False, True):
        encoded = encode(chunk, predict)
        assert encoded[0] == (MODE_PREDICT if predict else MODE_COLUMNS)
        assert decode(encoded) == chunk


def test_a_steady_pilot_costs_almost_nothing_once_predicted() -> None:
    steady = [(500, Kind.CLIENT, 0, _pod(1000 + 15 * i, 2000)) for i in range(100)]
    chunk = _records(*steady)
    plain, predicted = encode(chunk, predict=False), encode(chunk)
    assert decode(predicted) == chunk
    # After the first report every field is guessed exactly: a zero byte in each field's stream.
    assert predicted.count(0) >= 99 * 13 > plain.count(0)
    assert len(predicted) < len(plain)


def test_reports_that_do_not_encode_back_exactly_are_kept_as_they_are() -> None:
    good = _pod(1000, 2000)
    zero_as_minus_zero = good[:-1] + bytes([good[-1] | 1])  # armor1 0 as "-0"
    with_junk = good + b"\x99"  # the client's trailing junk is never recorded, but be sure
    chunk = _records(
        (0, Kind.CLIENT, 1, good),
        (0, Kind.CLIENT, 1, good[:5]),  # cut short: undecodable
        (0, Kind.CLIENT, 1, with_junk),
        (0, Kind.CLIENT, 1, b"b"),
        (0, Kind.SERVER, 0, good),  # a 'b' that is not a client's
        (0, Kind.CLIENT, 1, b""),
        (0, Kind.CLIENT, 1, _pod(1000, 2000, armor1=0)),
    )
    assert zero_as_minus_zero != good
    _round_trip(chunk)
    _round_trip(_records((0, Kind.CLIENT, 1, zero_as_minus_zero)))


def test_positions_off_the_map_and_odd_values_round_trip() -> None:
    chunk = _records(
        (0, Kind.CLIENT, 2, _pod(-50, -30, orient=3599, fwd=-136)),
        (1000, Kind.CLIENT, 2, _pod(5000, 4900, orient=0, fwd=119, y=-5)),
        (60_000, Kind.CLIENT, 2, _pod(0, 0, orient=-3600, fwd=0, power=-1, v1=10**9)),
        (0, Kind.CLIENT, 2, _pod(4799, 4799, orient=12345)),
    )
    _round_trip(chunk)


def test_bytes_that_are_not_canonical_records_are_kept_whole() -> None:
    record = encode_record(5, Kind.CLIENT, 1, _pod(1, 2))
    non_canonical = b"\x85\x00" + record[1:]  # delta 5 as a two-byte varint
    tiny = encode_record(0, Kind.END, None, b"")  # smaller as it is than as streams
    for chunk in (b"", record + b"\x07", record + b"\x00\x00", non_canonical, b"\xff" * 20, tiny):
        encoded = encode(chunk)
        if chunk:
            assert encoded[0] == MODE_VERBATIM
        assert decode(encoded) == chunk


def test_random_records_round_trip() -> None:
    rng = random.Random(14)
    for _ in range(50):
        records = []
        for _ in range(rng.randrange(1, 60)):
            kind = rng.choice(list(Kind))
            if kind is Kind.CLIENT and rng.random() < 0.7:
                payload = _pod(
                    rng.randrange(-100, 5000),
                    rng.randrange(-100, 5000),
                    orient=rng.randrange(-4000, 8000),
                    fwd=rng.choice(SPEEDS),
                    y=rng.randrange(-20, 400),
                    v1=rng.randrange(-(10**7), 10**7),
                )
            else:
                payload = rng.randbytes(rng.randrange(0, 40))
            records.append((rng.randrange(0, 3000), kind, rng.choice([None, 0, 1, 15]), payload))
        _round_trip(_records(*records))


@pytest.mark.parametrize(
    "data", [b"", b"\x09", b"\x02", b"\x02\x01" + b"\x00" * 19, b"\x02\x05" + b"\x01" * 19]
)
def test_garbage_is_refused(data: bytes) -> None:
    with pytest.raises(TransformError):
        decode(data)
