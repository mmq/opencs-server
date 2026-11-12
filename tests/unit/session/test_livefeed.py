"""Live feeds (``session.livefeed``, ADR 16): the records the recorder is given, as epochs that read
like segments; a cap that drops and starts again; the game never waits."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fixtures.lobbies import city
from fixtures.pods import report
from opencs.clock import ManualClock
from opencs.config import RecordingConfig
from opencs.messages import ServerMessage, client, decode_client, decode_server, server
from opencs.recording import Kind, Record, Recorder, Segment, read_segment, read_segment_file
from opencs.routing import Router
from opencs.session import recorder as hooks
from opencs.session.livefeed import Feed
from opencs.session.recorder import Recording
from opencs.world.city import City
from opencs.world.seating import JoinResult

WALL = 1_800_000_000.0


class Sink:
    def __init__(self) -> None:
        self.got: list[ServerMessage] = []

    def send(self, msg: ServerMessage) -> None:
        self.got.append(msg)

    def congested(self) -> bool:
        return False

    def backlog(self) -> int:
        return 0

    def shed(self) -> None:
        pass


class Game:
    """A city with the hooks wired as the app wires them, a recorder optional."""

    def __init__(self, directory: Path | None = None, **changes: object) -> None:
        self.clock = ManualClock(100.0)
        settings = replace(RecordingConfig(directory=directory, min_free_bytes=0), **changes)  # type: ignore[arg-type]
        self.recorder = (
            Recorder(settings, wall=lambda: WALL + self.clock.now(), flush_seconds=0.01)
            if directory is not None
            else None
        )
        self.recording = Recording(
            self.recorder, self.clock, settings=settings, wall=lambda: WALL + self.clock.now()
        )
        self.router = Router(self.recording)
        self.city = City(city("A"))
        self.sinks: dict[int, Sink] = {}
        if self.recorder is not None:
            self.recorder.start()

    def join(self, callsign: str, team: int = 0, *, resumed: bool = False) -> int:
        joined = self.city.join(callsign, team, self.clock.now())
        assert isinstance(joined, JoinResult)
        self.sinks[joined.player_id] = Sink()
        self.router.register("A", joined.player_id, self.sinks[joined.player_id])
        self.recording.joined(self.city, joined.player_id, resumed=resumed)
        return joined.player_id

    def send(self, player_id: int, message: client.ClientMessage) -> None:
        self.recording.client(self.city, player_id, message)
        self.router.deliver(self.city, self.city.client_message(player_id, message, self.now))

    def leave(self, player_id: int) -> None:
        self.router.unregister("A", player_id)
        self.router.deliver(self.city, self.city.leave(player_id, self.now))
        self.recording.left(self.city, player_id)

    def tick(self, seconds: float = 0.2) -> None:
        self.clock.advance(seconds)
        self.router.deliver(self.city, self.city.tick(self.now))
        self.recording.after_tick(self.city)

    @property
    def now(self) -> float:
        return self.clock.now()


@pytest.fixture
def game() -> Iterator[Game]:
    yield Game()


def epochs(lines: list[dict[str, Any]]) -> list[Segment]:
    """Each epoch's lines joined and read as a segment."""
    out: dict[int, bytes] = {}
    for line in lines:
        out[line["epoch"]] = out.get(line["epoch"], b"") + base64.b64decode(line["data"])
    return [read_segment(data) for _, data in sorted(out.items())]


def drain(feed: Feed) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    while feed.queued:
        lines += feed.lines()
    return lines


def body(segment: Segment) -> list[Record]:
    """The records after the first keyframe."""
    end = next(i for i, r in enumerate(segment.records) if r.kind is Kind.KEYFRAME_END)
    return list(segment.records[end + 1 :])


def test_a_feed_starts_with_a_header_and_the_keyframe_a_late_joiner_is_sent(game: Game) -> None:
    a = game.join("ALPHA")
    game.send(a, report(4))
    game.send(a, report(0, x_off=-40))
    feed = game.recording.open_feed(game.city)
    lines = drain(feed)
    assert lines[0]["start"] == "subscribe" and lines[0]["dropped"] == 0
    [segment] = epochs(lines)
    assert segment.header["city"] == "A"
    assert segment.header["started_utc_ms"] == round((WALL + game.now) * 1000)
    assert segment.header["players"] == [{"id": a, "callsign": "ALPHA", "team": 0, "bot": False}]
    assert segment.header["config"]["letter"] == "A"
    snapshot = [decode_server(r.payload)[0] for r in segment.records if r.kind is Kind.SNAPSHOT]
    assert snapshot == hooks._snapshot(game.city, game.now)
    assert any(isinstance(m, server.Introduce) and m.id == a for m in snapshot)
    assert segment.records[0].state()["reason"] == "subscribe"


def test_a_feed_holds_the_records_the_recorder_writes(tmp_path: Path) -> None:
    game = Game(tmp_path, chat="public")
    a = game.join("ALPHA")
    game.send(a, report(4))
    feed = game.recording.open_feed(game.city)
    b = game.join("BRAVO", team=1)
    game.send(b, report(4, x_off=300))
    game.send(a, report(0, x_off=-40))
    game.send(a, client.Chat(0, b"open"))
    game.send(a, client.Chat(1, b"team, not kept"))
    game.send(b, client.Shot(b, report(0).pos, 450, 0, 1))
    game.tick()
    game.leave(b)
    game.tick()
    [live] = epochs(drain(feed))
    assert game.recorder is not None
    game.recorder.close()
    [recorded] = [read_segment_file(p) for p in tmp_path.glob("*.ocsrec")]
    kept = body(live)
    assert kept, "the feed carried records"
    tail = [r for r in recorded.records if r.kind is not Kind.END][-len(kept) :]
    assert [(r.kind, r.player, r.payload) for r in kept] == [
        (r.kind, r.player, r.payload) for r in tail
    ]
    assert [r.at_ms - kept[0].at_ms for r in kept] == [r.at_ms - tail[0].at_ms for r in tail]
    said = [decode_client(r.payload) for r in kept if r.kind is Kind.CLIENT]
    assert client.Chat(0, b"open") in said
    assert not [m for m in said if isinstance(m, client.Chat) and m.style == 1]


def test_a_feed_works_without_a_recorder_and_stops_with_its_subscriber(game: Game) -> None:
    assert game.recorder is None
    a = game.join("ALPHA")
    feed = game.recording.open_feed(game.city)
    game.send(a, report(4))
    assert feed.queued > 1
    game.recording.close_feed(feed)
    assert feed.closed and feed.queued == 0 and game.recording.feeds == {}
    game.send(a, report(0))
    assert feed.queued == 0


def test_a_reader_that_falls_behind_loses_the_records_then_gets_a_new_epoch(game: Game) -> None:
    a = game.join("ALPHA")
    game.send(a, report(4))
    feed = game.recording.open_feed(game.city, capacity=50)
    first = feed.lines()  # the reader took the first epoch's start, then stopped reading
    feed.waiting = True
    for i in range(200):
        game.send(a, report(0, x_off=i))
        assert feed.queued <= 50  # the cap holds whatever the reader does
    assert feed.gaps >= 1
    game.tick()
    assert feed.queued == 0, "no new epoch while the reader is still behind"
    feed.waiting = False
    game.tick()  # caught up: a new epoch from the world as it is now
    later = drain(feed)
    assert later[0]["start"] == "gap" and later[0]["dropped"] > 0
    assert later[0]["epoch"] == first[0]["epoch"] + 1
    [_, again] = epochs(first + later)
    introduced = [
        m
        for m in (decode_server(r.payload)[0] for r in again.records if r.kind is Kind.SNAPSHOT)
        if isinstance(m, server.Introduce)
    ]
    assert introduced and introduced[0].pos == report(0, x_off=199).pos


def test_lines_encode_a_batch_at_a_time(game: Game) -> None:
    a = game.join("ALPHA")
    feed = game.recording.open_feed(game.city)
    for i in range(30):
        game.send(a, report(0, x_off=i))
    assert feed.queued > 30
    feed.lines(max_items=10)
    assert feed.queued > 20


def test_a_resume_adds_a_keyframe_inside_the_epoch(game: Game) -> None:
    a = game.join("ALPHA")
    game.send(a, report(4))
    feed = game.recording.open_feed(game.city)
    game.join("BRAVO", team=1, resumed=True)
    [segment] = epochs(drain(feed))
    kinds = [r.kind for r in segment.records]
    assert kinds.count(Kind.KEYFRAME) == 2
    assert [r for r in segment.records if r.kind is Kind.KEYFRAME][1].state()["reason"] == "resume"


def test_a_rebuilt_city_starts_a_new_epoch(game: Game) -> None:
    game.join("ALPHA")
    feed = game.recording.open_feed(game.city)
    drain(feed)
    game.city = City(city("A"))
    game.tick()
    [line] = drain(feed)
    assert line["start"] == "rebuilt"


def test_a_bug_in_feeding_ends_the_citys_feeds_and_never_reaches_the_game(
    game: Game, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    a = game.join("ALPHA")
    game.send(a, report(4))
    feed = game.recording.open_feed(game.city)

    def broken(*_: object) -> list[ServerMessage]:
        raise RuntimeError("bug")

    monkeypatch.setattr(hooks, "_snapshot", broken)
    game.join("BRAVO", resumed=True)
    assert feed.closed and game.recording.feeds == {}
    game.send(a, client.Chat(0, b"still playing"))
    assert server.Chat(a, 0, b"still playing") in game.sinks[a].got
    assert "a bug in the live feed of city A" in caplog.text
