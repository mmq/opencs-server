"""The game recorder's core: the segment format, its reader, the writer thread and its limits."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from opencs.config import CityConfig, RecordingConfig, RecordingRules
from opencs.messages import client, client_payload, decode_client, decode_server, server
from opencs.messages.limits import SYSTEM_SENDER
from opencs.recording import (
    FORMAT_VERSION,
    MAGIC,
    Kind,
    PilotInfo,
    Record,
    Recorder,
    SegmentError,
    encode_header,
    encode_record,
    read_segment,
    read_segment_file,
    varint,
)
from opencs.wire.codec import BYTE_MIN

WALL = 1_800_000_000.0  # 2027-01-15T08:00:00Z
STAMP = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(WALL))

CITY = CityConfig(
    letter="A",
    name="TESTVILLE",
    map=1,
    max_multiplier=8,
    tower_output=1,
    max_module_level=2,
    capacity=16,
    team_policy="free",
    balance_teams=(0, 1, 2, 3),
    banned_modules=(),
    enabled=True,
)

ALPHA = PilotInfo(1, "ALPHA", 0)
BOT = PilotInfo(2, "BOT01", 1, bot=True)


def config(directory: Path, **changes: object) -> RecordingConfig:
    base = RecordingConfig(directory=directory, max_bytes=10_000_000, min_free_bytes=0)
    return replace(base, **changes)  # type: ignore[arg-type]


def recorder(directory: Path, **kwargs: object) -> Recorder:
    changes = {k: kwargs.pop(k) for k in list(kwargs) if k in RecordingConfig.__dataclass_fields__}
    kwargs.setdefault("flush_seconds", 0.01)
    kwargs.setdefault("segment_reserve", 1000)
    return Recorder(config(directory, **changes), wall=lambda: WALL, **kwargs)  # type: ignore[arg-type]


def begin(rec: Recorder, now: float, letter: str = "A", reason: str = "start") -> None:
    rec.begin(
        letter,
        now,
        config=replace(CITY, letter=letter),
        players=[ALPHA],
        snapshot=[server.Delete(7), b"H\x20\x28"],
        state={"floor": 2},
        reason=reason,
    )


def segments(directory: Path, suffix: str = ".ocsrec") -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.name.endswith(suffix))


# --- the format ------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 16_383, 16_384, 2**35, 2**63])
def test_a_varint_reads_back(value: int) -> None:
    data = encode_header({}) + encode_record(value, Kind.CLIENT, value, b"x")
    [record] = read_segment(data).records
    assert (record.at_ms, record.player, record.payload) == (value, value, b"x")


def test_the_header_and_records_read_back() -> None:
    header = {"city": "A", "players": [ALPHA.row()]}
    data = (
        encode_header(header)
        + encode_record(5, Kind.CLIENT, 0, b"Ub hi")
        + encode_record(0, Kind.SERVER, None, b"a!!x")
        + encode_record(1000, Kind.END, None, b'{"reason":"done"}')
    )
    assert data.startswith(MAGIC + varint(FORMAT_VERSION))
    segment = read_segment(data)
    assert segment.header == header
    assert segment.records == (
        Record(5, Kind.CLIENT, 0, b"Ub hi"),
        Record(5, Kind.SERVER, None, b"a!!x"),
        Record(1005, Kind.END, None, b'{"reason":"done"}'),
    )
    assert segment.complete and segment.ignored == 0
    assert segment.records[2].state() == {"reason": "done"}


def test_a_segment_cut_anywhere_reads_up_to_its_last_whole_record() -> None:
    head = encode_header({"city": "A"})
    records = [encode_record(i, Kind.SERVER, i, bytes(range(i * 40))) for i in range(1, 6)]
    data = head + b"".join(records)
    for cut in range(len(head), len(data) + 1):
        segment = read_segment(data[:cut])
        whole = 0
        while whole < len(records) and len(head) + sum(map(len, records[: whole + 1])) <= cut:
            whole += 1
        assert len(segment.records) == whole, cut
        assert not segment.complete
        assert segment.ignored == cut - len(head) - sum(map(len, records[:whole]))


def test_a_zero_filled_tail_ends_the_records() -> None:
    data = encode_header({}) + encode_record(1, Kind.CLIENT, 3, b"b") + bytes(4096)
    segment = read_segment(data)
    assert len(segment.records) == 1 and segment.ignored == 4096


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b"OCSREC\n", "bad magic"),
        (MAGIC, "cut short"),
        (MAGIC + varint(FORMAT_VERSION) + varint(10) + b"{}", "cut short"),
        (MAGIC + varint(FORMAT_VERSION + 1) + varint(2) + b"{}", "format 2"),
        (MAGIC + varint(FORMAT_VERSION) + varint(2) + b"[]", "not a JSON object"),
        (MAGIC + varint(FORMAT_VERSION) + varint(2) + b"{x", "not JSON"),
    ],
)
def test_an_unreadable_header_is_an_error(data: bytes, problem: str) -> None:
    with pytest.raises(SegmentError, match=problem):
        read_segment(data)


# --- choices ---------------------------------------------------------------------------------


def test_humans_any_and_off_with_a_per_city_override(tmp_path: Path) -> None:
    rec = recorder(
        tmp_path,
        bots=("BOT01",),
        cities=(("B", RecordingRules(when="any")), ("C", RecordingRules(when="off"))),
    )
    assert rec.is_bot("bot01") and not rec.is_bot("ALPHA")
    assert not rec.wants("A", []) and not rec.wants("A", ["BOT01"])
    assert rec.wants("A", ["BOT01", "ALPHA"])
    assert rec.wants("B", ["BOT01"]) and not rec.wants("B", [])
    assert not rec.wants("C", ["ALPHA"])


def test_chat_all_public_or_none_with_a_per_city_override(tmp_path: Path) -> None:
    rec = recorder(
        tmp_path,
        chat="public",
        cities=(("B", RecordingRules(chat="all")), ("C", RecordingRules(chat="none"))),
    )
    assert rec.keeps_chat("A", 0) and not rec.keeps_chat("A", 1)
    assert rec.keeps_chat("B", 0) and rec.keeps_chat("B", 1)
    assert not rec.keeps_chat("C", 0)


def test_recording_is_on_only_with_a_directory_and_a_city_to_record(tmp_path: Path) -> None:
    assert not RecordingConfig().enabled
    assert config(tmp_path).enabled
    assert not config(tmp_path, when="off").enabled
    assert config(tmp_path, when="off", cities=(("B", RecordingRules(when="any")),)).enabled


# --- the writer ------------------------------------------------------------------------------


def test_a_recorded_segment_reads_back(tmp_path: Path) -> None:
    rec = recorder(tmp_path, chat="public")
    rec.start()
    assert not rec.recording("A")
    rec.client("A", 1, client.Chat(0, b"lost"), 0.5)  # before the segment: not recorded
    begin(rec, 1.0)
    assert rec.recording("A")
    rec.client("A", 1, client.Chat(0, b"hello"), 1.25)
    rec.client("A", 1, client.Chat(1, b"team secret"), 1.3)  # chat = "public"
    rec.client("A", 1, client_payload(client.Chat(1, b"team too")), 1.3)
    rec.client("A", 1, client.Died(), 1.5)
    rec.server("A", server.Chat(SYSTEM_SENDER, 0, b"NOTICE"), 1.5)
    rec.server("A", server.Delete(9), 2.0, to=1)
    rec.player("A", 2.0, 2, BOT)
    rec.keyframe("A", 3.0, [server.Delete(7)], {"floor": 3})
    rec.player("A", 4.0, 2, None)
    rec.end("A", 5.0, "empty")
    assert not rec.recording("A")
    rec.close()
    assert not rec.stopped
    [path] = segments(tmp_path)
    assert path.name == f"A-{STAMP}.ocsrec"
    segment = read_segment_file(path)
    assert segment.complete
    assert segment.header["city"] == "A"
    assert segment.header["started_utc_ms"] == WALL * 1000
    assert segment.header["players"] == [ALPHA.row()]
    assert segment.header["config"]["name"] == "TESTVILLE"
    kinds = [(r.at_ms, r.kind, r.player) for r in segment.records]
    assert kinds == [
        (0, Kind.KEYFRAME, None),
        (0, Kind.SNAPSHOT, None),
        (0, Kind.SNAPSHOT, None),
        (0, Kind.KEYFRAME_END, None),
        (250, Kind.CLIENT, 1),
        (500, Kind.CLIENT, 1),
        (500, Kind.SERVER, None),
        (1000, Kind.SERVER, 1),
        (1000, Kind.PLAYER, 2),
        (2000, Kind.KEYFRAME, None),
        (2000, Kind.SNAPSHOT, None),
        (2000, Kind.KEYFRAME_END, None),
        (3000, Kind.PLAYER, 2),
        (4000, Kind.END, None),
    ]
    r = segment.records
    assert r[0].state() == {"floor": 2, "reason": "start"}
    assert decode_server(r[1].payload) == (server.Delete(7), BYTE_MIN)
    assert r[2].payload == b"H\x20\x28"
    assert decode_client(r[4].payload) == client.Chat(0, b"hello")
    assert decode_client(r[5].payload) == client.Died()
    assert decode_server(r[6].payload)[0] == server.Chat(SYSTEM_SENDER, 0, b"NOTICE")
    assert r[8].state() == BOT.row()
    assert r[9].state() == {"floor": 3, "reason": "resume"}
    assert r[12].payload == b""
    assert r[13].state() == {"reason": "empty"}


def test_rotation_starts_a_new_segment_with_a_keyframe(tmp_path: Path) -> None:
    rec = recorder(tmp_path, rotate_seconds=10.0)
    rec.start()
    assert not rec.rotation_due("A", 0.0)
    begin(rec, 0.0)
    rec.server("A", server.Delete(9), 4.0)
    assert not rec.rotation_due("A", 9.9)
    assert rec.rotation_due("A", 10.0)
    begin(rec, 10.0, reason="rotate")
    assert not rec.rotation_due("A", 19.0)
    begin(rec, 0.0, letter="B")
    rec.close()  # ends both open segments
    first, second, other = (
        tmp_path / f"{n}.ocsrec" for n in (f"A-{STAMP}", f"A-{STAMP}-2", f"B-{STAMP}")
    )
    assert len(segments(tmp_path)) == 3
    one, two = read_segment_file(first), read_segment_file(second)
    assert one.complete and one.records[-1].state() == {"reason": "rotate"}
    assert one.records[-1].at_ms == 10_000
    assert two.complete and two.records[0].state() == {"floor": 2, "reason": "rotate"}
    assert two.records[-1].state() == {"reason": "shutdown"}
    assert read_segment_file(other).complete


def test_a_segment_left_open_by_a_crash_is_finished_at_start(tmp_path: Path) -> None:
    left = tmp_path / "A-20260101T000000Z.ocsrec.part"
    left.write_bytes(encode_header({}) + encode_record(1, Kind.CLIENT, 1, b"b")[:-1])
    (tmp_path / "notes.part").write_text("not ours")
    rec = recorder(tmp_path)
    rec.start()
    rec.close()
    assert not left.exists() and (tmp_path / "notes.part").exists()
    segment = read_segment_file(tmp_path / "A-20260101T000000Z.ocsrec")
    assert segment.records == () and not segment.complete


def test_an_open_segment_is_readable_while_it_is_written(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec.start()
    begin(rec, 0.0)
    rec.client("A", 1, client.Died(), 0.1)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        parts = segments(tmp_path, ".part")
        if parts and len(read_segment_file(parts[0]).records) == 5:
            break
        time.sleep(0.01)
    else:
        pytest.fail("the writer did not write the records")
    rec.close()


# --- room on the disk ------------------------------------------------------------------------


def done_file(directory: Path, name: str, size: int) -> Path:
    path = directory / f"{name}.ocsrec.done"
    path.write_bytes(bytes(size))
    return path


def test_the_oldest_archived_segments_are_removed_to_make_room(tmp_path: Path) -> None:
    # Oldest by the time in the name, whatever the city.
    oldest = done_file(tmp_path, "B-20260101T000000Z", 4000)
    older = done_file(tmp_path, "A-20260101T000000Z-2", 4000)
    newer = done_file(tmp_path, "A-20260103T000000Z", 4000)
    unarchived = tmp_path / "A-20251231T000000Z.ocsrec"
    unarchived.write_bytes(bytes(1000))
    foreign = tmp_path / "keep.ocsrec.done"
    foreign.write_bytes(bytes(50_000))
    rec = recorder(tmp_path, max_bytes=8_000)
    rec.start()
    begin(rec, 0.0)
    rec.close()
    assert not oldest.exists() and not older.exists()
    assert newer.exists() and unarchived.exists() and foreign.exists()
    assert len(segments(tmp_path)) == 2


def test_without_room_a_city_is_paused_not_the_recorder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    unarchived = tmp_path / "A-20251231T000000Z.ocsrec"
    unarchived.write_bytes(bytes(9500))
    rec = recorder(tmp_path, max_bytes=10_000)
    rec.start()
    with caplog.at_level(logging.WARNING, "opencs.recording"):
        begin(rec, 0.0)
        rec.client("A", 1, client.Died(), 0.1)
        begin(rec, 10.0, reason="rotate")
        rec.close()
    assert unarchived.exists() and segments(tmp_path) == [unarchived]
    assert not rec.stopped
    assert [r.message for r in caplog.records].count(caplog.records[0].message) == 1
    assert "no room" in caplog.records[0].message


def test_free_space_is_kept(tmp_path: Path) -> None:
    rec = recorder(tmp_path, min_free_bytes=10**15)  # more than any test disk has
    rec.start()
    begin(rec, 0.0)
    rec.close()
    assert segments(tmp_path) == [] and not rec.stopped


def test_reaching_the_cap_while_writing_finishes_the_segment(tmp_path: Path) -> None:
    rec = recorder(tmp_path, max_bytes=3000, segment_reserve=100)
    rec.start()
    begin(rec, 0.0)
    for i in range(200):
        rec.client("A", 1, client.Chat(0, b"x" * 30), i / 10)
    rec.close()
    [path] = segments(tmp_path)
    segment = read_segment_file(path)
    assert segment.complete and segment.records[-1].state() == {"reason": "full"}
    assert 3000 < path.stat().st_size < 3100


# --- failures --------------------------------------------------------------------------------


def test_a_full_queue_stops_recording_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rec = recorder(tmp_path, buffer_items=3)  # never started: nothing drains the queue
    with caplog.at_level(logging.ERROR, "opencs.recording"):
        begin(rec, 0.0)
        rec.server("A", server.Delete(9), 0.1)
        rec.server("A", server.Delete(9), 0.2)
        assert not rec.stopped
        rec.server("A", server.Delete(9), 0.3)
        assert rec.stopped and not rec.recording("A") and not rec.rotation_due("A", 1e9)
        rec.server("A", server.Delete(9), 0.4)
        begin(rec, 1.0)
    assert not rec.recording("A")
    assert len(caplog.records) == 1 and "3 records behind" in caplog.records[0].message
    rec.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to a read-only directory")
def test_a_read_only_directory_stops_recording_and_nothing_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "recordings"
    directory.mkdir()
    directory.chmod(0o500)
    try:
        rec = recorder(directory)
        with caplog.at_level(logging.ERROR, "opencs.recording"):
            rec.start()
            begin(rec, 0.0)
            deadline = time.monotonic() + 5
            while not rec.stopped and time.monotonic() < deadline:
                time.sleep(0.01)
            rec.client("A", 1, client.Died(), 0.1)
            rec.close()
    finally:
        directory.chmod(0o700)
    assert rec.stopped and not rec.recording("A")
    assert [r.message for r in caplog.records] == [
        f"recording stopped: cannot write to {directory}"
    ]
    assert list(directory.iterdir()) == []


def test_a_directory_that_cannot_be_made_stops_recording(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("")
    rec = recorder(tmp_path / "file" / "recordings")
    rec.start()
    rec.close()
    assert rec.stopped
