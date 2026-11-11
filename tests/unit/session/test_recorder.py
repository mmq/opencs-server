"""The recorder's hooks (``session.recorder``): when a city is recorded, what is left out, folding,
the chat setting and keyframes, with a real city, router and writer."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from fixtures.lobbies import city
from fixtures.pods import report
from opencs.clock import ManualClock
from opencs.config import RecordingConfig, RecordingRules
from opencs.messages import (
    ClientMessage,
    ServerMessage,
    WirePos,
    client,
    decode_client,
    decode_server,
    server,
)
from opencs.messages.limits import SYSTEM_SENDER
from opencs.recording import Kind, Recorder, Segment, read_segment_file, segment_age
from opencs.routing import Router
from opencs.session import recorder as hooks
from opencs.session.recorder import Recording
from opencs.world.chat import system_line
from opencs.world.city import City
from opencs.world.events import Event, ToAll, ToPlayer, ToTeam
from opencs.world.seating import JoinResult

WALL = 1_800_000_000.0


@dataclass
class Sink:
    got: list[ServerMessage]

    def send(self, msg: ServerMessage) -> None:
        self.got.append(msg)

    def congested(self) -> bool:
        return False

    def backlog(self) -> int:
        return 0

    def shed(self) -> None:
        pass


class Game:
    """A city with the recorder's hooks wired as the app wires them: the router's tape, and the
    session's calls on joins, leaves, client messages and ticks."""

    def __init__(self, directory: Path, rotate: float = 300.0, **changes: object) -> None:
        settings = replace(
            RecordingConfig(directory=directory, max_bytes=10_000_000, min_free_bytes=0),
            **changes,  # type: ignore[arg-type]
        )
        self.directory = directory
        self.clock = ManualClock(100.0)
        self.recorder = Recorder(
            settings,
            wall=lambda: WALL + self.clock.now(),
            rotate_seconds=rotate,
            flush_seconds=0.01,
        )
        self.recording = Recording(self.recorder, self.clock)
        self.router = Router(self.recording)
        self.city = City(city("A"))
        self.sinks: dict[int, Sink] = {}
        self.recorder.start()

    def join(self, callsign: str, team: int = 0, *, resumed: bool = False) -> int:
        joined = self.city.join(callsign, team, self.clock.now())
        assert isinstance(joined, JoinResult)
        self.sinks[joined.player_id] = Sink([])
        self.router.register("A", joined.player_id, self.sinks[joined.player_id])
        self.recording.joined(self.city, joined.player_id, resumed=resumed)
        return joined.player_id

    def send(self, player_id: int, message: ClientMessage) -> None:
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

    def segments(self) -> list[Segment]:
        """Close the recorder and read every segment it wrote, oldest first."""
        self.recorder.close()
        assert not list(self.directory.glob("*.part"))
        return [
            read_segment_file(p) for p in sorted(self.directory.glob("*.ocsrec"), key=segment_age)
        ]


@pytest.fixture
def game(tmp_path: Path) -> Iterator[Game]:
    g = Game(tmp_path)
    yield g
    g.recorder.close()


def server_messages(segment: Segment, kind: Kind = Kind.SERVER) -> list[ServerMessage]:
    return [decode_server(r.payload)[0] for r in segment.records if r.kind is kind]


def client_messages(segment: Segment) -> list[ClientMessage]:
    return [decode_client(r.payload) for r in segment.records if r.kind is Kind.CLIENT]


def test_relayed_copies_are_left_out_and_the_client_messages_kept(game: Game) -> None:
    a, b = game.join("ALPHA"), game.join("BRAVO", team=1)
    game.send(a, report(4))
    game.send(b, report(4, x_off=300))
    game.send(a, report(0, x_off=-40))
    game.send(a, client.Chat(0, b"hello"))
    thrown = client.Grenade(WirePos(2, 2, 100, 72, 300), 869, 3332)
    game.send(b, thrown)
    fired = client.Shot(a, WirePos(0, 0, 145, 240, 5), 450, 0, 1)
    game.send(a, fired)
    assert server.Chat(a, 0, b"hello") in game.sinks[b].got  # the copies were delivered
    assert server.GrenadeInFlight(thrown.pos, 869, 3332) in game.sinks[a].got
    [segment] = game.segments()

    kept = server_messages(segment)
    assert not [
        m for m in kept if isinstance(m, server.PodState | server.GrenadeInFlight | server.Shot)
    ]
    assert not [m for m in kept if isinstance(m, server.Chat)]
    assert server.ReleaseDrop(a) in kept and server.ReleaseDrop(b) in kept
    assert client_messages(segment) == [
        report(4),
        report(4, x_off=300),
        report(0, x_off=-40),
        client.Chat(0, b"hello"),
        thrown,
        fired,
    ]
    sources = [r.player for r in segment.records if r.kind is Kind.CLIENT]
    assert sources == [a, b, a, a, b, a]


def test_system_lines_are_kept_whatever_the_chat_setting(tmp_path: Path) -> None:
    game = Game(tmp_path, chat="none")
    a = game.join("ALPHA")
    game.send(a, report(4))
    game.send(a, client.Chat(0, b"not kept"))
    game.router.deliver(game.city, [system_line("SERVER GOING DOWN")])
    [segment] = game.segments()
    assert client.Chat(0, b"not kept") not in client_messages(segment)
    assert server.Chat(SYSTEM_SENDER, 0, b"SERVER GOING DOWN") in server_messages(segment)


@pytest.mark.parametrize(
    ("chat", "kept"),
    [("all", [b"open", b"team"]), ("public", [b"open"]), ("none", [])],
)
def test_the_chat_setting_picks_the_chat_kept(tmp_path: Path, chat: str, kept: list[bytes]) -> None:
    game = Game(tmp_path, chat=chat)
    a = game.join("ALPHA")
    game.send(a, report(4))
    game.send(a, client.Chat(0, b"open"))
    game.send(a, client.Chat(1, b"team"))
    [segment] = game.segments()
    assert [m.text for m in client_messages(segment) if isinstance(m, client.Chat)] == kept


def test_a_per_city_chat_override_wins(tmp_path: Path) -> None:
    game = Game(tmp_path, chat="all", cities=(("A", RecordingRules(chat="none")),))
    a = game.join("ALPHA")
    game.send(a, client.Chat(0, b"open"))
    [segment] = game.segments()
    assert client_messages(segment) == []


def test_per_recipient_copies_fold_into_one_record_for_everyone() -> None:
    x, y, z = server.Delete(1), server.Delete(2), server.Delete(3)
    events = [
        Event(x, ToPlayer(1)),
        Event(x, ToPlayer(2)),
        Event(y, ToPlayer(1)),
        Event(z, ToAll()),
        Event(z, ToAll()),  # two events to everyone are two
        Event(y, ToPlayer(1)),  # and the same to the same player twice is twice
        Event(server.Delete(4), ToTeam(1)),
        Event(server.Delete(5), ToPlayer(3), relayed=True),
    ]
    assert hooks._fold(events) == [
        (x, None),
        (y, 1),
        (z, None),
        (z, None),
        (y, 1),
        (server.Delete(4), None),
    ]


def test_a_delete_sent_to_each_client_is_one_record_for_everyone(game: Game) -> None:
    a, b, c = game.join("ALPHA"), game.join("BRAVO", team=1), game.join("CHARLIE", team=2)
    for n, pid in enumerate((a, b, c)):
        game.send(pid, report(4, x_off=100 * n))
    game.leave(c)
    assert server.Delete(c) in game.sinks[a].got and server.Delete(c) in game.sinks[b].got
    [segment] = game.segments()
    deletes = [
        (r.player, decode_server(r.payload)[0])
        for r in segment.records
        if r.kind is Kind.SERVER and r.payload[:1] == b"H"
    ]
    assert deletes == [(None, server.Delete(c))]
    # An introduction to one client is recorded to that player; CHARLIE's, to both, once.
    intros = [r.player for r in segment.records if r.kind is Kind.SERVER and r.payload[:1] == b"G"]
    assert intros == [b, a, c, c, None]


def test_bots_alone_under_humans_write_nothing(tmp_path: Path) -> None:
    game = Game(tmp_path, bots=("BOT01", "BOT02"))
    one, two = game.join("BOT01"), game.join("BOT02", team=1)
    game.send(one, report(4))
    game.send(two, client.Chat(0, b"beep"))
    for _ in range(10):
        game.tick(60.0)
    game.leave(one)
    game.leave(two)
    assert game.segments() == []
    assert list(tmp_path.iterdir()) == []


def test_bots_count_under_any(tmp_path: Path) -> None:
    game = Game(tmp_path, when="any", bots=("BOT01",))
    one = game.join("BOT01")
    game.send(one, report(4))
    [segment] = game.segments()
    assert segment.header["players"] == [{"id": one, "callsign": "BOT01", "team": 0, "bot": True}]


def test_a_city_set_off_writes_nothing(tmp_path: Path) -> None:
    game = Game(tmp_path, when="any", cities=(("A", RecordingRules(when="off")),))
    a = game.join("ALPHA")
    game.send(a, report(4))
    assert game.segments() == []


def test_a_segment_runs_from_the_first_human_to_the_last(tmp_path: Path) -> None:
    game = Game(tmp_path, bots=("BOT01",))
    bot = game.join("BOT01")
    assert not game.recorder.recording("A")
    human = game.join("ALPHA", team=1)
    assert game.recorder.recording("A")
    other = game.join("BRAVO", team=2)
    game.leave(other)
    game.leave(human)  # only the bot is left
    assert not game.recorder.recording("A")
    game.send(bot, client.Chat(0, b"after"))
    [segment] = game.segments()
    assert [p["callsign"] for p in segment.header["players"]] == ["BOT01", "ALPHA"]
    players = [(r.player, r.state()) for r in segment.records if r.kind is Kind.PLAYER]
    assert players == [
        (other, {"id": other, "callsign": "BRAVO", "team": 2, "bot": False}),
        (other, None),
        (human, None),
    ]
    assert segment.complete and segment.records[-1].state() == {"reason": "empty"}
    assert client_messages(segment) == []


def test_a_keyframe_is_what_a_late_joiner_is_sent_and_changes_no_view(tmp_path: Path) -> None:
    game = Game(tmp_path, rotate=60.0)
    a = game.join("ALPHA")
    game.send(a, report(4))
    game.send(a, report(0, x_off=-40))
    held = set(game.city.player(a).view.held)  # type: ignore[union-attr]
    game.tick(60.0)  # rotates: the new segment starts with a keyframe of the city as it is
    assert game.city.player(a).view.held == held  # type: ignore[union-attr]
    late = game.join("BRAVO", team=1)
    game.send(late, report(4, x_off=300))
    first, second = game.segments()
    assert first.records[-1].state() == {"reason": "rotate"}
    keyframe = second.records[0]
    assert keyframe.kind is Kind.KEYFRAME
    state = keyframe.state()
    assert state["reason"] == "rotate"
    assert state["multiplier_floor"] == game.city.multiplier_floor
    assert [s["id"] for s in state["scores"]] == [a]
    snapshot = server_messages(second, Kind.SNAPSHOT)
    synced = game.sinks[late].got
    assert snapshot and [m for m in synced if m in snapshot] == snapshot
    assert isinstance(snapshot[-2], server.Introduce) and snapshot[-2].id == a
    assert second.records[len(snapshot) + 1].kind is Kind.KEYFRAME_END


def test_a_resume_writes_a_keyframe_inside_the_segment(game: Game) -> None:
    a = game.join("ALPHA")
    game.send(a, report(4))
    b = game.join("BRAVO", team=1, resumed=True)
    [segment] = game.segments()
    kinds = [r.kind for r in segment.records]
    assert kinds.count(Kind.KEYFRAME) == 2
    resumed = [r.state() for r in segment.records if r.kind is Kind.KEYFRAME][1]
    assert resumed["reason"] == "resume"
    assert {s["id"] for s in resumed["scores"]} == {a, b}


def test_a_resume_into_an_unrecorded_city_starts_a_segment_with_its_keyframe(game: Game) -> None:
    game.join("ALPHA", resumed=True)
    [segment] = game.segments()
    assert segment.records[0].state()["reason"] == "resume"


def test_a_bug_in_the_hooks_stops_recording_and_never_reaches_the_game(
    game: Game, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    a = game.join("ALPHA")
    game.send(a, report(4))

    def broken(*_: object) -> list[ServerMessage]:
        raise RuntimeError("bug")

    monkeypatch.setattr(hooks, "_snapshot", broken)
    game.join("BRAVO", resumed=True)  # wants a keyframe: the bug
    assert game.recorder.stopped
    game.send(a, client.Chat(0, b"still playing"))
    assert server.Chat(a, 0, b"still playing") in game.sinks[a].got
    assert "recording stopped: a bug in recording a join" in caplog.text


def test_without_a_recorder_the_hooks_do_nothing() -> None:
    recording = Recording()
    c = City(city("A"))
    joined = c.join("ALPHA", 0, 0.0)
    assert isinstance(joined, JoinResult)
    recording.joined(c, joined.player_id)
    recording.client(c, joined.player_id, client.Chat(0, b"x"))
    recording.deliver(c, [system_line("x")])
    recording.after_tick(c)
    recording.left(c, joined.player_id)
    recording.close()
