"""The game recorder in a real server on loopback (the web plan's Phase 2): what a game writes, that
an empty or bots-only city writes nothing, that a recordings directory the server cannot use never
stops a game, and that the loop stays quick while recording."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from pathlib import Path

import pytest

from fixtures.loopback import (
    TIMEOUT,
    Client,
    LogWatch,
    TickGate,
    connect,
    enter,
    frame,
    local_config,
    start,
)
from fixtures.pods import report
from opencs.app import build_services
from opencs.clock import ManualClock
from opencs.config import AppConfig, RecordingConfig
from opencs.control import SHUTDOWN_NOTICE
from opencs.messages import (
    ClientMessage,
    ServerMessage,
    client,
    decode_client,
    decode_server,
    server,
)
from opencs.messages.limits import SYSTEM_SENDER
from opencs.recording import Kind, Recorder, Segment, read_segment_file, segment_age
from opencs.session.phases import Services


def recording_config(directory: Path, **changes: object) -> AppConfig:
    config = local_config()
    recording = replace(
        RecordingConfig(directory=directory, max_bytes=100_000_000, min_free_bytes=0),
        **changes,  # type: ignore[arg-type]
    )
    return replace(config, server=replace(config.server, recording=recording))


def recording_services(
    config: AppConfig, clock: ManualClock | None, rotate_seconds: float = 300.0
) -> Services:
    recorder = Recorder(config.server.recording, rotate_seconds=rotate_seconds, flush_seconds=0.01)
    return build_services(config, clock=clock, recorder=recorder)


def segments(directory: Path) -> list[Segment]:
    assert not list(directory.glob("*.part"))  # every segment was finished
    return [read_segment_file(p) for p in sorted(directory.glob("*.ocsrec"), key=segment_age)]


def server_messages(segment: Segment, kind: Kind = Kind.SERVER) -> list[ServerMessage]:
    return [decode_server(r.payload)[0] for r in segment.records if r.kind is kind]


def client_messages(segment: Segment) -> list[ClientMessage]:
    return [decode_client(r.payload) for r in segment.records if r.kind is Kind.CLIENT]


async def chat(c: Client, text: bytes) -> None:
    """Say something on the open channel and wait for it to come back: the server has handled
    everything sent before it."""
    await c.send(frame(client.Chat(0, text)))
    while await c.expect_frame("a") != server.Chat(_id(c), 0, text):
        pass


def _id(c: Client) -> int:
    return c.player_id  # type: ignore[attr-defined, no-any-return]


async def play(port: int, team: str, account: str = "JOINGENIE") -> tuple[Client, int]:
    c, hello = await enter(port, team, account)
    c.player_id = hello.self_id  # type: ignore[attr-defined]
    return c, hello.self_id


async def quit_game(c: Client) -> None:
    with LogWatch("opencs.session", "quit requested") as seen:
        await c.send(frame(client.Quit()))
        await seen.reached(1)
    c.writer.close()


async def until(condition: object, what: str) -> None:
    """Wait for a condition another thread makes true (the writer), checking every 10 ms."""
    for _ in range(int(TIMEOUT / 0.01)):
        if condition():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never {what}")


def test_a_game_is_recorded_and_its_keyframe_is_what_a_late_joiner_is_sent(tmp_path: Path) -> None:
    async def scenario() -> list[ServerMessage]:
        clock, gate = ManualClock(1000.0), TickGate()
        config = recording_config(tmp_path)
        services = recording_services(config, clock, rotate_seconds=10.0)
        stop, task, port = await start(
            config, clock=clock, services=services, tick_sleep=gate.sleep
        )
        try:
            alice, _ = await play(port, "RED", "ALICE,sesame")
            await alice.send(frame(report(4)))
            await alice.expect_frame("A")
            await alice.send(frame(report(0, x_off=-40)))
            await chat(alice, b"hello")
            clock.advance(10.0)  # less than a silent line's SILENCE_SECONDS
            gate.pulse()  # a tick: the segment is old enough, so a new one starts
            await chat(alice, b"after the tick")
            late, _ = await play(port, "BLUE")
            await late.send(frame(report(4, x_off=300)))
            await late.send(frame(client.Chat(0, b"synced")))
            synced = await late.frames_until("a")
            await quit_game(late)
            await quit_game(alice)
        finally:
            stop.set()
            await asyncio.wait_for(task, TIMEOUT)
        return synced

    synced = asyncio.run(asyncio.wait_for(scenario(), 6 * TIMEOUT))
    first, second = segments(tmp_path)
    for path in tmp_path.iterdir():  # nothing of the logon is ever recorded
        data = path.read_bytes()
        assert b"sesame" not in data and b"HHH" not in data and b"Not Telling" not in data
        assert b"1380" not in data

    check_first(first)
    check_second(second, synced)


def check_first(first: Segment) -> None:
    """ALICE alone, from her join to the rotation."""
    assert first.complete and first.header["city"] == "A"
    assert [p["callsign"] for p in first.header["players"]] == ["ALICE"]
    assert first.records[0].kind is Kind.KEYFRAME
    assert first.records[0].state()["reason"] == "start"
    assert client_messages(first) == [
        report(4),
        report(0, x_off=-40),
        client.Chat(0, b"hello"),
    ]
    assert {r.player for r in first.records if r.kind is Kind.CLIENT} == {0}
    kept = server_messages(first)
    assert server.ReleaseDrop(0) in kept
    assert any(isinstance(m, server.SetStatus) for m in kept)
    assert not any(isinstance(m, server.Chat | server.PodState | server.Session) for m in kept)
    assert first.records[-1].state() == {"reason": "rotate"}


def check_second(second: Segment, synced: list[ServerMessage]) -> None:
    """A keyframe with ALICE's pod, the late joiner's whole visit, and both leaving."""
    assert second.complete and second.records[0].state()["reason"] == "rotate"
    snapshot = server_messages(second, Kind.SNAPSHOT)
    assert any(isinstance(m, server.Introduce) and m.callsign == b"ALICE" for m in snapshot)
    assert [m for m in synced if m in snapshot] == snapshot  # the late joiner got it, in order
    # What else the late joiner got is its own: its hello, its pod's drop and status, chat.
    own = [m for m in synced if m not in snapshot]
    assert {type(m) for m in own} <= {
        server.Session,
        server.ReleaseDrop,
        server.SetBays,
        server.SetStatus,
        server.Introduce,
        server.Chat,
        server.SetBounty,
    }, own
    assert all(m.id == 1 for m in own if isinstance(m, server.ReleaseDrop | server.Introduce))
    assert client.Chat(0, b"after the tick") in client_messages(second)
    assert client.Chat(0, b"synced") in client_messages(second)
    players = [(r.player, r.state()) for r in second.records if r.kind is Kind.PLAYER]
    assert players == [
        (1, {"id": 1, "callsign": "JOINGENIE", "team": 1, "bot": False}),
        (1, None),
        (0, None),
    ]
    assert second.records[-1].state() == {"reason": "empty"}


def test_an_empty_city_writes_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, gate = ManualClock(1000.0), TickGate()
        config = recording_config(tmp_path)
        services = recording_services(config, clock, rotate_seconds=60.0)
        stop, task, port = await start(
            config, clock=clock, services=services, tick_sleep=gate.sleep
        )
        try:
            c = await connect(port)  # in the Chamber, never in a game
            await c.send("HHH\rALICE,sesame\r<Not Telling!>\rM 1380;3\r")
            await c.expect(b"\x05PRESS RETURN\r")
            await c.enter_chamber()
            for _ in range(10):
                clock.advance(60.0)
                gate.pulse()
                await asyncio.sleep(0)
            c.writer.close()
        finally:
            stop.set()
            await asyncio.wait_for(task, TIMEOUT)

    asyncio.run(asyncio.wait_for(scenario(), 4 * TIMEOUT))
    assert list(tmp_path.iterdir()) == []


def test_bots_alone_write_nothing_under_humans(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, gate = ManualClock(1000.0), TickGate()
        config = recording_config(tmp_path, bots=("JOINGENIE",))
        services = recording_services(config, clock)
        stop, task, port = await start(
            config, clock=clock, services=services, tick_sleep=gate.sleep
        )
        try:
            bot, _ = await play(port, "RED")  # the guest named JOINGENIE is a bot
            await bot.send(frame(report(4)))
            await chat(bot, b"beep")
            await quit_game(bot)
        finally:
            stop.set()
            await asyncio.wait_for(task, TIMEOUT)

    asyncio.run(asyncio.wait_for(scenario(), 4 * TIMEOUT))
    assert list(tmp_path.iterdir()) == []


async def keep_playing(c: Client, rounds: int) -> None:
    for n in range(rounds):
        await c.send(frame(report(0, x_off=n)))
        await chat(c, f"still here {n}".encode())


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to a read-only directory")
def test_a_read_only_directory_stops_the_recorder_and_never_the_game(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "recordings"
    directory.mkdir()
    directory.chmod(0o555)

    async def scenario() -> bool:
        config = recording_config(directory)
        services = recording_services(config, None)
        stop, task, port = await start(config, services=services)
        try:
            c, _ = await play(port, "RED")
            await c.send(frame(report(4)))
            assert services.recording.recorder is not None
            recorder = services.recording.recorder
            await until(lambda: recorder.stopped, "stopped")
            await keep_playing(c, 5)
            await quit_game(c)
        finally:
            stop.set()
            await asyncio.wait_for(task, TIMEOUT)
        return recorder.stopped

    try:
        with caplog.at_level(logging.INFO, "opencs"):
            assert asyncio.run(asyncio.wait_for(scenario(), 4 * TIMEOUT))
    finally:
        directory.chmod(0o755)
    assert "recording stopped: cannot write to" in caplog.text
    assert list(directory.iterdir()) == []


def test_a_full_disk_pauses_recording_and_never_the_game(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> bool:
        # More kept free than any disk has: no segment may start.
        config = recording_config(tmp_path, min_free_bytes=10**18)
        services = recording_services(config, None)
        stop, task, port = await start(config, services=services)
        try:
            c, _ = await play(port, "RED")
            await c.send(frame(report(4)))
            await until(lambda: "no room for a recording" in caplog.text, "warned")
            await keep_playing(c, 5)
            await quit_game(c)
        finally:
            stop.set()
            await asyncio.wait_for(task, TIMEOUT)
        assert services.recording.recorder is not None
        return services.recording.recorder.stopped

    with caplog.at_level(logging.INFO, "opencs"):
        assert not asyncio.run(asyncio.wait_for(scenario(), 4 * TIMEOUT))  # paused, not stopped
    assert list(tmp_path.iterdir()) == []


async def login(port: int, account: str) -> Client:
    c = await connect(port)
    await c.send(f"HHH\r{account}\r<Not Telling!>\rM 1380;3\r")
    await c.expect(b"\x05PRESS RETURN\r")
    await c.enter_chamber()
    await c.expect(b"\x05D0\r")
    return c


async def play_b(c: Client) -> int:
    await c.send("/PLAY B\r")
    await c.expect(b"\x05E ")
    await c.send("/GO 0\r")
    await c.expect(b"\x05C\r")
    hello = await c.expect_frame("B")
    assert isinstance(hello, server.Session)
    await c.send(frame(report(4)))
    await c.expect_frame("D")
    c.player_id = hello.self_id  # type: ignore[attr-defined]
    return hello.self_id


def test_a_resume_writes_a_keyframe(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, gate = ManualClock(1000.0), TickGate()
        config = recording_config(tmp_path)
        services = recording_services(config, clock)
        stop, task, port = await start(
            config, clock=clock, services=services, tick_sleep=gate.sleep
        )
        try:
            other = await login(port, "BOB,sesame")
            await play_b(other)
            with LogWatch("opencs.session", "held for /RESUME") as dropped:
                alice = await login(port, "ALICE,sesame")
                await play_b(alice)
                alice.writer.close()  # the line drops
                await dropped.reached(1)
            alice = await login(port, "ALICE,sesame")
            await alice.send("/RESUME\r")
            await play_b(alice)
            await chat(alice, b"back")
            await quit_game(alice)
            await quit_game(other)
        finally:
            stop.set()
            await asyncio.wait_for(task, TIMEOUT)

    asyncio.run(asyncio.wait_for(scenario(), 6 * TIMEOUT))
    [segment] = segments(tmp_path)
    keyframes = [r.state()["reason"] for r in segment.records if r.kind is Kind.KEYFRAME]
    assert keyframes == ["start", "resume"]
    joins = [r.state()["callsign"] for r in segment.records if r.kind is Kind.PLAYER and r.payload]
    assert joins == ["ALICE", "ALICE"]


async def barrier(c: Client) -> None:
    """Wait until the server has handled everything ``c`` sent: a ping is answered in order."""
    await c.send(frame(client.Ping()))
    while not isinstance(await c.expect_frame("E"), server.PingReply):
        pass  # other 'E' messages (the bounty) may come first


def test_the_loop_stays_quick_while_recording(tmp_path: Path) -> None:
    rounds = 16

    async def scenario() -> Services:
        config = recording_config(tmp_path)
        services = recording_services(config, None)
        stop, task, port = await start(config, services=services)  # real clock and tickers
        try:
            with LogWatch("opencs.session", " took ") as slow:
                one, _ = await play(port, "RED")
                two, _ = await play(port, "BLUE")
                await one.send(frame(report(4)))
                await two.send(frame(report(4, x_off=300)))
                for n in range(rounds):  # at a client's pace, within the relay budgets
                    await one.send(frame(report(0, x_off=n)))
                    await two.send(frame(report(0, x_off=300 - n)))
                    if n % 4 == 0:
                        await one.send(frame(client.Chat(0, f"one {n}".encode())))
                        await two.send(frame(client.Chat(1, f"two {n}".encode())))
                    await asyncio.sleep(1 / 8)
                await barrier(one)
                await barrier(two)
                await quit_game(one)
                await quit_game(two)
                assert slow.count == 0
        finally:
            stop.set()
            await asyncio.wait_for(task, TIMEOUT)
        return services

    services = asyncio.run(asyncio.wait_for(scenario(), 6 * TIMEOUT))
    assert services.city_health.slow_calls("A") == 0
    [segment] = segments(tmp_path)
    assert len(client_messages(segment)) == 2 + 2 * rounds + 2 * (rounds // 4)


def test_a_shutdown_records_its_notice_and_finishes_the_segment(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, gate = ManualClock(1000.0), TickGate()
        config = recording_config(tmp_path)
        services = recording_services(config, clock)
        stop, task, port = await start(
            config, clock=clock, services=services, tick_sleep=gate.sleep
        )
        c, _ = await play(port, "RED")
        await c.send(frame(report(4)))
        await chat(c, b"until the end")
        stop.set()  # the player is still in the game
        await asyncio.wait_for(task, TIMEOUT)
        c.writer.close()

    asyncio.run(asyncio.wait_for(scenario(), 4 * TIMEOUT))
    [segment] = segments(tmp_path)
    notice = server.Chat(SYSTEM_SENDER, 0, SHUTDOWN_NOTICE.encode())
    last = [r for r in segment.records if r.kind is Kind.SERVER][-1]
    assert (last.player, decode_server(last.payload)[0]) == (0, notice)
    assert segment.complete and segment.records[-1].state() == {"reason": "empty"}
