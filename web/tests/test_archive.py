"""The recording archive: segments in, the same bytes out; games, pilots and battle events; the
archiver's run over a directory; retention; forgetting a callsign; the management commands.

Set ``OPENCS_RECORDINGS`` to a directory of ``.ocsrec`` files to also round-trip every segment
there (the compression benchmark's corpus)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from opencs.messages import (
    ServerMessage,
    client,
    client_payload,
    decode_client,
    decode_server,
    server,
    server_payload,
)
from opencs.messages.client import PodState
from opencs.messages.fields import WirePos
from opencs.messages.limits import SYSTEM_SENDER
from opencs.recording import Kind, encode_header, encode_record, read_segment
from opencs.wire.codec import BYTE_MIN
from opencs_web.recordings import archive as archive_module
from opencs_web.recordings.archive import (
    CODEC_XZ,
    Archive,
    ArchiveError,
    archive_directory,
    blank,
    decompress,
    fold_tail,
    oldest_unarchived_ms,
)
from opencs_web.recordings.chunks import chunks, records_start

FIXTURE = Path(__file__).parent / "fixtures" / "B-20261111T194024Z-30s.ocsrec"
T0 = 1_800_000_000_000  # 2027-01-15T08:00:00Z
DAY = 86_400_000


def _corpus() -> list[Path]:
    paths = [FIXTURE]
    extra = os.environ.get("OPENCS_RECORDINGS")
    if extra:
        paths += sorted(Path(extra).rglob("*.ocsrec"))
    return paths


def _stamp(ms: int) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ms / 1000))


def _row(pid: int, callsign: str, team: int, bot: bool = False) -> dict[str, Any]:
    return {"id": pid, "callsign": callsign, "team": team, "bot": bot}


def _s(message: ServerMessage) -> bytes:
    return server_payload(message, BYTE_MIN)


def _pod(x: int) -> bytes:
    return client_payload(
        PodState(WirePos.from_world(x, 10, 2000), 900, 30, 30, 0, 0, 0, 3000, 50, 100, 100)
    )


def _join(pid: int, callsign: str, team: int) -> tuple[int, Kind, int, bytes]:
    payload = json.dumps(_row(pid, callsign, team), separators=(",", ":"), sort_keys=True)
    return (0, Kind.PLAYER, pid, payload.encode())


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def segment(
    start_ms: int = T0,
    *,
    city: str = "A",
    players: list[dict[str, Any]] | None = None,
    reason: str = "start",
    end: str | None = "empty",
    body: list[tuple[int, Kind, int | None, bytes]] | None = None,
    minutes: int = 0,
) -> bytes:
    """A small segment: a keyframe (a tower, an introduction), ``body``, ``minutes`` of pod reports
    (one every 500 ms from ALPHA), and an END with ``end`` (None: cut short)."""
    if players is None:
        players = [_row(0, "ALPHA", 0), _row(1, "Bravo", 1)]
    header = {
        "server_version": "test",
        "city": city,
        "started_utc_ms": start_ms,
        "config": {"letter": city, "name": "TESTVILLE"},
        "players": players,
    }
    tower = server.SpawnTower(0, 9000, 1, WirePos.from_world(100, 0, 100), 0)
    intro = server.Introduce(1, b"Bravo", 1, 1, WirePos.from_world(200, 0, 200), 0)
    records: list[tuple[int, Kind, int | None, bytes]] = [
        (0, Kind.KEYFRAME, None, _json({"reason": reason, "scores": []})),
        (0, Kind.SNAPSHOT, None, _s(tower)),
        (0, Kind.SNAPSHOT, None, _s(intro)),
        (0, Kind.KEYFRAME_END, None, b""),
        *(body or []),
        *((500, Kind.CLIENT, 0, _pod(1000 + i)) for i in range(minutes * 120)),
    ]
    if end is not None:
        records.append((200, Kind.END, None, _json({"reason": end})))
    return encode_header(header) + b"".join(encode_record(*r) for r in records)


BATTLE: list[tuple[int, Kind, int | None, bytes]] = [
    (1000, Kind.CLIENT, 0, client_payload(client.Chat(0, b"hi bravo, I am ALPHA"))),
    (0, Kind.CLIENT, 1, client_payload(client.Chat(1, b"team: get ALPHA"))),
    (0, Kind.SERVER, None, _s(server.Chat(SYSTEM_SENDER, 0, b"ALPHA ENTERED"))),
    _join(2, "CHARLIE", 0),
    (100, Kind.SERVER, 0, _s(server.KillNotice(True))),  # to ALPHA (player 0)
    (0, Kind.SERVER, None, _s(server.Destroy(1))),  # Bravo dies
    (100, Kind.SERVER, None, _s(server.Destroy(9000))),  # the tower
    (100, Kind.SERVER, None, _s(server.Destroy(5000))),  # a Sentinel: no event
    (100, Kind.PLAYER, 1, b""),  # Bravo leaves
]


@pytest.fixture
def archive(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Archive]:
    with Archive(tmp_path_factory.mktemp("archive") / "archive.db") as a:
        yield a


def put(directory: Path, data: bytes, n: str = "") -> Path:
    """``data`` as a finished segment in ``directory``, named as the server would."""
    header = read_segment(data).header
    path = directory / f"{header['city']}-{_stamp(header['started_utc_ms'])}{n}.ocsrec"
    path.write_bytes(data)
    return path


def run(a: Archive, directory: Path, now_ms: int = T0 + DAY, **kw: int) -> Any:
    kw.setdefault("max_days", 365)
    kw.setdefault("max_bytes", 10**10)
    return archive_directory(a, directory, now_ms=now_ms, **kw)


def records_of(data: bytes) -> list[tuple[int, Kind, int, bytes]]:
    seg = read_segment(data)
    t0 = seg.header["started_utc_ms"]
    return [
        (t0 + r.at_ms, r.kind, 0 if r.player is None else r.player + 1, r.payload)
        for r in seg.records
    ]


# --- round trip ------------------------------------------------------------------------------


@pytest.mark.parametrize("path", _corpus(), ids=lambda p: p.name)
def test_a_segment_comes_back_byte_for_byte(path: Path, archive: Archive) -> None:
    data = path.read_bytes()
    name = path.name
    if not archive_module.SEGMENT_NAME.fullmatch(name):
        name = "B-20261111T194024Z.ocsrec"  # the fixture's
    assert archive.add_segment(name, data, now_ms=0)
    seg = read_segment(data)
    assert archive.segment_bytes(name) == data[: len(data) - seg.ignored]
    assert archive.segment_header(name) == seg.header
    t0 = seg.header["started_utc_ms"]
    got = list(archive.records(seg.header["city"], t0, t0 + seg.records[-1].at_ms + 1))
    assert [(r.at_ms, r.kind, r.source, r.payload) for r in got] == records_of(data)
    assert b"".join(r.raw for r in got) == data[records_start(data) : len(data) - seg.ignored]


def test_a_window_is_cut_from_the_chunks_that_cover_it(archive: Archive) -> None:
    data = segment(minutes=3)
    archive.add_segment(f"A-{_stamp(T0)}.ocsrec", data, now_ms=0)
    everything = records_of(data)
    start, end = T0 + 61_000, T0 + 125_000
    got = list(archive.records("A", start, end))
    assert [(r.at_ms, r.kind, r.source, r.payload) for r in got] == [
        r for r in everything if start <= r[0] < end
    ]
    assert list(archive.records("B", T0, end)) == []
    assert list(archive.records("A", T0 - 10_000, T0)) == []


def test_chunks_are_minutes_compressed_with_xz_after_the_transform(archive: Archive) -> None:
    archive.add_segment(f"A-{_stamp(T0)}.ocsrec", segment(minutes=3), now_ms=0)
    rows = archive.db.execute(
        "SELECT base_ms, first_ms, last_ms, keyframe, codec, transform, records, record_bytes, "
        "length(data) FROM chunks ORDER BY first_ms"
    ).fetchall()
    assert len(rows) == 3  # the 4th minute's END is folded into the 3rd
    assert [r[3] for r in rows] == [1, 0, 0]
    assert {r[4] for r in rows} == {CODEC_XZ}
    assert all(r[5] == 2 and r[8] < r[7] for r in rows)  # predicted, and smaller
    with pytest.raises(ArchiveError, match="codec"):
        decompress(99, b"")


def test_a_tiny_tail_is_folded_into_the_chunk_before() -> None:
    parts = chunks(segment(minutes=5))
    assert parts[-1].index == 5 and parts[-1].records == 2  # the last report and the END
    folded = fold_tail(parts)
    assert len(folded) == 5
    assert folded[-1].data == parts[-2].data + parts[-1].data
    assert folded[-1].records == parts[-2].records + 2
    assert (folded[-1].first_ms, folded[-1].last_ms) == (parts[-2].first_ms, parts[-1].last_ms)
    assert fold_tail(parts[:-1]) == parts[:-1]  # a last chunk with a minute of records stays
    assert fold_tail(parts[:1]) == parts[:1]


# --- games, pilots and events ----------------------------------------------------------------


def test_rotated_segments_are_one_game_and_a_new_start_another(
    tmp_path: Path, archive: Archive
) -> None:
    second = T0 + 300_200
    put(tmp_path, segment(T0, end="rotate", minutes=5))
    put(tmp_path, segment(second, reason="rotate", end="empty", body=BATTLE))
    put(tmp_path, segment(second + 3_600_000, players=[_row(0, "ALPHA", 0)]))
    put(tmp_path, segment(T0, city="B", players=[_row(5, "ECHO", 2)], end=None), n="-2")
    result = run(archive, tmp_path)
    assert len(result.archived) == 4 and not result.failed
    assert list(tmp_path.iterdir()) == []
    games = archive.db.execute(
        "SELECT city, start_ms, segments, end_reason, open FROM games ORDER BY city, start_ms"
    ).fetchall()
    assert games == [
        ("A", T0, 2, "empty", 0),
        ("A", second + 3_600_000, 1, "empty", 0),
        ("B", T0, 1, None, 0),
    ]
    pilots = archive.db.execute(
        "SELECT callsign, team, joined_ms, left_ms FROM pilots WHERE game_id = 1 ORDER BY id"
    ).fetchall()
    battle_end = second + 1000 + 300
    end = second + 1600
    assert pilots == [
        ("ALPHA", 0, T0, end),  # in both segments: one row
        ("Bravo", 1, T0, battle_end + 100),
        ("CHARLIE", 0, second + 1000, end),
    ]
    found = archive.db.execute(
        "SELECT game_id FROM pilots WHERE callsign = 'alpha' COLLATE NOCASE ORDER BY game_id"
    ).fetchall()
    assert found == [(1,), (3,)]  # game 2 is city B's, archived second: its name is older


def test_deaths_kills_and_towers_are_events(archive: Archive) -> None:
    archive.add_segment(f"A-{_stamp(T0)}.ocsrec", segment(body=BATTLE), now_ms=0)
    events = archive.db.execute(
        "SELECT at_ms, kind, player_id, callsign, team, other_id, other_callsign, earned "
        "FROM events ORDER BY id"
    ).fetchall()
    at = T0 + 1100
    assert events == [
        (at, "death", 1, "Bravo", 1, None, None, None),
        (at, "kill", 0, "ALPHA", 0, 1, "Bravo", 1),
        (at + 100, "tower", None, None, 1, 9000, None, None),
    ]


# --- the archiver's run ----------------------------------------------------------------------


def test_only_finished_segments_are_archived_oldest_first(tmp_path: Path, archive: Archive) -> None:
    later = put(tmp_path, segment(T0 + 60_000, city="B"))
    first = put(tmp_path, segment(T0))
    part = tmp_path / f"A-{_stamp(T0 + 120_000)}.ocsrec.part"
    done = tmp_path / f"A-{_stamp(T0 - 120_000)}.ocsrec.done"
    other = tmp_path / "notes.txt"
    for path in (part, done, other):
        path.write_bytes(segment())
    result = run(archive, tmp_path)
    assert result.archived == [first.name, later.name]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([part.name, done.name, other.name])
    assert oldest_unarchived_ms(tmp_path) == T0 + 120_000


def test_a_segment_finished_during_the_run_is_archived_too(
    tmp_path: Path, archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(tmp_path, segment(T0))
    real = Archive.add_segment

    def finishing(self: Archive, name: str, data: bytes, *, now_ms: int) -> bool:
        put(tmp_path, segment(T0 + 300_000, reason="rotate"))  # once: same name after
        return real(self, name, data, now_ms=now_ms)

    monkeypatch.setattr(Archive, "add_segment", finishing)
    assert len(run(archive, tmp_path).archived) == 2
    assert list(tmp_path.iterdir()) == []


def test_a_segment_archived_but_not_deleted_is_deleted_not_archived_again(
    tmp_path: Path, archive: Archive
) -> None:
    path = put(tmp_path, segment())
    assert archive.add_segment(path.name, path.read_bytes(), now_ms=0)  # then a crash
    assert not archive.add_segment(path.name, path.read_bytes(), now_ms=0)
    result = run(archive, tmp_path)
    assert result.archived == [] and result.removed == [path.name]
    assert not path.exists()
    assert archive.db.execute("SELECT count(*) FROM segments").fetchone() == (1,)
    assert archive.db.execute("SELECT count(*) FROM pilots").fetchone() == (2,)


def test_a_different_file_under_an_archived_name_is_left_alone(
    tmp_path: Path, archive: Archive
) -> None:
    path = put(tmp_path, segment())
    archive.add_segment(path.name, segment(end="shutdown"), now_ms=0)
    result = run(archive, tmp_path)
    assert result.failed == [path.name] and path.exists()


def test_an_unreadable_segment_is_left_and_the_rest_archived(
    tmp_path: Path, archive: Archive
) -> None:
    bad = tmp_path / f"A-{_stamp(T0)}.ocsrec"
    bad.write_bytes(b"not a segment")
    good = put(tmp_path, segment(T0 + 1000))
    result = run(archive, tmp_path)
    assert result.failed == [bad.name] and result.archived == [good.name]
    assert bad.exists() and not good.exists()


def test_a_failed_segment_leaves_nothing_behind(
    tmp_path: Path, archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = put(tmp_path, segment())
    real = archive_module._Game.finish

    def broken(self: Any, at: int, rotated: bool) -> None:
        real(self, at, rotated)
        raise ArchiveError("disk on fire")

    with monkeypatch.context() as patch:
        patch.setattr(archive_module._Game, "finish", broken)
        assert run(archive, tmp_path).failed == [path.name]
    for table in ("files", "games", "segments", "chunks", "pilots", "events"):
        assert archive.db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert path.exists()
    assert run(archive, tmp_path).archived == [path.name]


def test_an_archive_of_a_newer_schema_is_refused(tmp_path: Path) -> None:
    with Archive(tmp_path / "a.db") as a:
        a.db.execute("UPDATE meta SET value = '99' WHERE key = 'schema'")
    with pytest.raises(ArchiveError, match="schema 99"):
        Archive(tmp_path / "a.db")


# --- retention -------------------------------------------------------------------------------


def test_games_past_the_age_limit_are_removed(tmp_path: Path, archive: Archive) -> None:
    for day in range(4):
        put(tmp_path, segment(T0 + day * DAY))
    result = run(archive, tmp_path, now_ms=T0 + 3 * DAY + 1, max_days=2)
    assert result.pruned_games == 1
    starts = [r[0] for r in archive.db.execute("SELECT start_ms FROM games ORDER BY start_ms")]
    assert starts == [T0 + DAY, T0 + 2 * DAY, T0 + 3 * DAY]
    assert archive.db.execute("SELECT count(*) FROM segments").fetchone() == (3,)
    assert list(archive.records("A", T0, T0 + DAY)) == []


def test_the_oldest_games_go_to_keep_under_the_size_cap(tmp_path: Path, archive: Archive) -> None:
    for hour in range(5):
        put(tmp_path, segment(T0 + hour * 3_600_000, minutes=1))
    run(archive, tmp_path)
    sizes = [r[0] for r in archive.db.execute("SELECT stored_bytes FROM games ORDER BY start_ms")]
    assert archive.stored_bytes() == sum(sizes)
    removed = archive.prune(now_ms=T0, max_days=365, max_bytes=sum(sizes[2:]))
    assert removed == 2
    assert archive.stored_bytes() == sum(sizes[2:])
    for table in ("segments", "chunks", "pilots", "events"):
        orphans = archive.db.execute(
            f"SELECT count(*) FROM {table} WHERE "
            + (
                "segment_id NOT IN (SELECT id FROM segments)"
                if table == "chunks"
                else "game_id NOT IN (SELECT id FROM games)"
            )
        ).fetchone()
        assert orphans == (0,)


# --- forgetting a callsign -------------------------------------------------------------------


def _text(archive: Archive) -> bytes:
    """Every byte the archive would give back for city A, headers included."""
    names = [r[0] for r in archive.db.execute("SELECT name FROM segments ORDER BY start_ms")]
    return b"".join(archive.segment_bytes(n) for n in names)


def test_forgetting_a_callsign_takes_it_out_of_its_games(tmp_path: Path, archive: Archive) -> None:
    put(tmp_path, segment(body=BATTLE, minutes=2))
    other = segment(
        T0 + DAY,
        players=[_row(0, "DELTA", 0)],
        body=[(0, Kind.CLIENT, 0, client_payload(client.Chat(0, b"where is bravo")))],
    )
    put(tmp_path, other)
    run(archive, tmp_path)
    before = list(archive.records("A", T0, T0 + 2 * DAY))
    done = archive.forget("BRAVO", now_ms=T0 + 2 * DAY)
    assert (done.games, done.segments, done.pilots, done.events) == (1, 1, 1, 2)
    assert not done.pending
    first = archive.segment_bytes(f"A-{_stamp(T0)}.ocsrec")
    assert b"bravo" not in first.lower()
    assert b"ALPHA" in first and b"CHARLIE" in first
    header = archive.segment_header(f"A-{_stamp(T0)}.ocsrec")
    assert [p["callsign"] for p in header["players"]] == ["ALPHA", "#####"]
    after = list(archive.records("A", T0, T0 + 2 * DAY))
    assert len(after) == len(before)
    changed = [(b, a) for b, a in zip(before, after, strict=True) if b.payload != a.payload]
    assert [(b.at_ms, b.kind) for b, _ in changed] == [
        (T0, Kind.SNAPSHOT),  # the introduction
        (T0 + 1000, Kind.CLIENT),  # ALPHA's chat naming Bravo
        (T0 + 1000, Kind.CLIENT),  # Bravo's own chat, emptied
    ]
    said = {decode_client(a.payload) for b, a in changed if a.kind is Kind.CLIENT}
    assert said == {client.Chat(0, b"hi #####, I am ALPHA"), client.Chat(1, b"")}
    intro = next(a for _, a in changed if a.kind is Kind.SNAPSHOT)
    assert decode_server(intro.payload)[0].callsign == b"#####"  # type: ignore[attr-defined]
    pilots = [r[0] for r in archive.db.execute("SELECT callsign FROM pilots ORDER BY id")]
    assert "Bravo" not in pilots and "#####" in pilots
    assert archive.db.execute(
        "SELECT count(*) FROM events WHERE callsign = 'Bravo' OR other_callsign = 'Bravo'"
    ).fetchone() == (0,)
    # A game it did not fly in keeps a mention, unless asked to look everywhere.
    assert b"where is bravo" in _text(archive)
    done = archive.forget("bravo", now_ms=T0 + 2 * DAY, everywhere=True)
    assert done.segments == 1
    assert b"bravo" not in _text(archive).lower()
    assert archive.stored_bytes() == sum(
        len(h) for (h,) in archive.db.execute("SELECT header FROM segments")
    ) + sum(n for (n,) in archive.db.execute("SELECT length(data) FROM chunks"))


def test_a_callsign_forgotten_before_its_segment_is_archived_is_taken_out_then(
    tmp_path: Path, archive: Archive
) -> None:
    open_part = tmp_path / f"A-{_stamp(T0)}.ocsrec.part"
    open_part.write_bytes(segment(body=BATTLE))
    done = archive.forget("Bravo", now_ms=T0 + 60_000, pending=True)
    assert done.pending and done.games == 0
    run(archive, tmp_path)  # still open: nothing archived, the callsign kept
    assert archive.db.execute("SELECT count(*) FROM forgotten").fetchone() == (1,)
    open_part.rename(tmp_path / f"A-{_stamp(T0)}.ocsrec")  # finished
    put(tmp_path, segment(T0 + 120_000))  # began after: left as it is
    result = run(archive, tmp_path)
    assert len(result.archived) == 2
    first = archive.segment_bytes(f"A-{_stamp(T0)}.ocsrec")
    assert b"bravo" not in first.lower()
    assert b"Bravo" in archive.segment_bytes(f"A-{_stamp(T0 + 120_000)}.ocsrec")
    assert archive.db.execute("SELECT count(*) FROM forgotten").fetchone() == (0,)
    assert blank("Bravo") == "#####"


# --- the commands ----------------------------------------------------------------------------


@pytest.fixture
def configured(tmp_path: Path, settings: Any) -> Path:
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    settings.OPENCS_RECORDINGS = recordings
    settings.OPENCS_ARCHIVE = tmp_path / "archive.db"
    settings.OPENCS_ARCHIVE_MAX_DAYS = 100_000
    settings.OPENCS_ARCHIVE_MAX_BYTES = 10**10
    return recordings


def test_the_archive_command(configured: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = put(configured, FIXTURE.read_bytes())
    call_command("archive")
    assert f"archived {path.name}" in capsys.readouterr().out
    assert not path.exists()
    with Archive(configured.parent / "archive.db") as a:
        assert a.segment_bytes(path.name) == FIXTURE.read_bytes()
    bad = configured / "A-20260101T000000Z.ocsrec"
    bad.write_bytes(b"junk")
    with pytest.raises(CommandError, match="1 segments left"):
        call_command("archive")
    assert bad.exists()


def test_the_archive_command_needs_a_directory(configured: Path, settings: Any) -> None:
    settings.OPENCS_RECORDINGS = None
    with pytest.raises(CommandError, match="OPENCS_WEB_RECORDINGS"):
        call_command("archive")
    with pytest.raises(CommandError, match="not a directory"):
        call_command("archive", recordings=str(configured / "none"))


def test_the_forget_command_archives_first(
    configured: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(configured, segment(body=BATTLE))
    call_command("forget_callsign", "bravo")
    out = capsys.readouterr().out
    assert "archived 1 segments first" in out
    assert "bravo: 1 games, 1 segments" in out
    with Archive(configured.parent / "archive.db") as a:
        assert b"bravo" not in _text(a).lower()
    with pytest.raises(CommandError, match="printable"):
        call_command("forget_callsign", " ")
