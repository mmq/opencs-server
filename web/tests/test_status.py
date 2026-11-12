import asyncio
import copy
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from typing import Any

import pytest

from fakeservice import SNAPSHOT
from opencs_web.channel import UnavailableError
from opencs_web.live import Streams, event, status_events
from opencs_web.status import OFFLINE, City, Pilot, Status, StatusCache, Team, public_status

INTERVAL = 0.05


def run(scenario: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(scenario, 10))


class Game:
    """A fetch that counts its calls and answers with ``snapshot``, or fails."""

    def __init__(self) -> None:
        self.snapshot: Any = copy.deepcopy(SNAPSHOT)
        self.calls = 0
        self.down = False
        self.delay = 0.0

    async def __call__(self) -> object:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.down:
            raise UnavailableError("down")
        return self.snapshot


# --- the public status --------------------------------------------------------------------------


def test_the_public_status_keeps_only_what_the_pages_show() -> None:
    assert public_status(SNAPSHOT) == Status(
        online=True,
        accounts=12,
        uptime_minutes=62,
        cities=(
            City(
                "B",
                "BOSTON",
                16,
                (Team("RED", (Pilot("ALPHA", False),)), Team("BLUE", (Pilot("BOT01", True),))),
            ),
        ),
        held=1,
    )


def test_teams_are_in_the_games_order() -> None:
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["cities"][0]["players"] = [
        {"callsign": c, "team": t, "guest": False}
        for c, t in [("D", "BLACK"), ("A", "RED"), ("C", "GREEN"), ("E", "RED")]
    ]
    teams = public_status(snapshot).cities[0].teams
    assert [(t.name, [p.callsign for p in t.pilots]) for t in teams] == [
        ("RED", ["A", "E"]),
        ("GREEN", ["C"]),
        ("BLACK", ["D"]),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"cities": []},
        {**SNAPSHOT, "uptime": "long"},
        {**SNAPSHOT, "held": -1},
        {**SNAPSHOT, "cities": [{"letter": "B", "name": "X", "capacity": 4, "players": [7]}]},
        {
            **SNAPSHOT,
            "cities": [
                {
                    "letter": "B",
                    "name": "X",
                    "capacity": 4,
                    "players": [{"callsign": "A", "team": "PINK", "guest": False}],
                }
            ],
        },
    ],
)
def test_a_snapshot_not_understood_is_refused(bad: object) -> None:
    with pytest.raises(ValueError):
        public_status(bad)


@pytest.mark.parametrize(
    ("minutes", "text"),
    [
        (0, "under a minute"),
        (1, "1 minute"),
        (62, "1 hour, 2 minutes"),
        (24 * 60 + 5, "1 day, 5 minutes"),
        (3 * 24 * 60 + 2 * 60 + 7, "3 days, 2 hours"),
    ],
)
def test_the_uptime_reads_as_text(minutes: int, text: str) -> None:
    assert Status(online=True, uptime_minutes=minutes).uptime == text


# --- the cache ----------------------------------------------------------------------------------


def test_with_no_viewer_and_no_page_the_game_is_asked_nothing() -> None:
    game = Game()

    async def scenario() -> None:
        StatusCache(game, interval=INTERVAL)
        await asyncio.sleep(10 * INTERVAL)

    run(scenario())
    assert game.calls == 0


def test_a_page_uses_the_cached_status_while_it_is_fresh() -> None:
    game = Game()
    now = [0.0]
    cache = StatusCache(game, interval=1.0, clock=lambda: now[0])

    async def scenario() -> None:
        assert (await cache.get()).accounts == 12
        now[0] += 0.5
        await cache.get()
        assert game.calls == 1
        now[0] += 0.6
        await cache.get()
        assert game.calls == 2

    run(scenario())


def test_pages_asking_at_once_share_one_fetch() -> None:
    game = Game()
    game.delay = 0.05
    cache = StatusCache(game, interval=INTERVAL)

    async def scenario() -> None:
        statuses = await asyncio.gather(*(cache.get() for _ in range(50)))
        assert len(set(statuses)) == 1

    run(scenario())
    assert game.calls == 1


def test_a_page_whose_reader_leaves_does_not_cancel_the_fetch() -> None:
    game = Game()
    game.delay = 0.05
    cache = StatusCache(game, interval=INTERVAL)

    async def scenario() -> None:
        page = asyncio.create_task(cache.get())
        await asyncio.sleep(0.01)
        page.cancel()
        await asyncio.sleep(0.1)
        assert cache.status.online

    run(scenario())


def test_viewers_start_updates_once_a_second_and_the_last_one_stops_them() -> None:
    game = Game()
    cache = StatusCache(game, interval=INTERVAL)

    async def scenario() -> None:
        async with cache.viewer():
            async with cache.viewer():
                assert cache.viewers == 2
                await asyncio.sleep(10.5 * INTERVAL)
            assert cache.updating  # one viewer still watches
        assert not cache.updating
        calls = game.calls
        await asyncio.sleep(10 * INTERVAL)
        assert game.calls == calls

    run(scenario())
    # About one a second (here every INTERVAL), however many viewers.
    assert 8 <= game.calls <= 13


def test_a_viewer_after_a_fresh_page_waits_for_the_interval() -> None:
    game = Game()
    cache = StatusCache(game, interval=1.0)

    async def scenario() -> None:
        await cache.get()
        async with cache.viewer():
            await asyncio.sleep(0.1)

    run(scenario())
    assert game.calls == 1


def test_while_viewers_watch_pages_ask_nothing_more() -> None:
    game = Game()
    cache = StatusCache(game, interval=1.0)

    async def scenario() -> None:
        async with cache.viewer():
            await asyncio.sleep(0.01)
            for _ in range(100):
                await cache.get()

    run(scenario())
    assert game.calls == 1


def test_the_game_down_is_offline_and_its_return_is_seen() -> None:
    game = Game()
    now = [0.0]
    cache = StatusCache(game, interval=1.0, clock=lambda: now[0])

    async def scenario() -> None:
        game.down = True
        assert await cache.get() == OFFLINE
        game.down = False
        now[0] += 1.0
        assert (await cache.get()).online

    run(scenario())


def test_a_snapshot_not_understood_is_offline() -> None:
    game = Game()
    game.snapshot = {"cities": "no"}
    assert run(StatusCache(game).get()) == OFFLINE


def test_a_failing_fetch_is_offline_not_an_error() -> None:
    async def broken() -> object:
        raise RuntimeError("a bug")

    assert run(StatusCache(broken).get()) == OFFLINE


# --- the stream ---------------------------------------------------------------------------------


def render(status: Status) -> str:
    return f"{status.accounts if status.online else 'offline'}\nline two"


async def take(stream: AsyncIterator[bytes], n: int) -> list[bytes]:
    return [await anext(stream) for _ in range(n)]


def test_an_event_puts_each_line_in_its_own_data_field() -> None:
    assert event("status", "a\nb") == b"event: status\ndata: a\ndata: b\n\n"
    assert event("busy", "") == b"event: busy\ndata: \n\n"


def test_a_stream_sends_the_status_then_each_change_then_heartbeats() -> None:
    game = Game()
    cache = StatusCache(game, interval=INTERVAL)
    streams = Streams(10, 2)

    async def scenario() -> None:
        stream = status_events(cache, streams, "192.0.2.1", render, heartbeat=3 * INTERVAL)
        first = await take(stream, 2)
        assert first[0].startswith(b"retry: ")
        assert first[1] == b"event: status\ndata: 12\ndata: line two\n\n"
        game.snapshot["accounts"]["count"] = 13
        assert await anext(stream) == b"event: status\ndata: 13\ndata: line two\n\n"
        assert await anext(stream) == b": heartbeat\n\n"  # nothing changed
        game.down = True
        assert await anext(stream) == b"event: status\ndata: offline\ndata: line two\n\n"
        assert cache.viewers == 1 and streams.open == 1
        await stream.aclose()  # the reader went away
        assert cache.viewers == 0 and streams.open == 0
        assert not cache.updating

    run(scenario())


def test_a_stream_ends_after_its_lifetime() -> None:
    cache = StatusCache(Game(), interval=INTERVAL)
    streams = Streams(10, 2)

    async def scenario() -> list[bytes]:
        stream = status_events(cache, streams, "192.0.2.1", render, lifetime=2 * INTERVAL)
        return [chunk async for chunk in stream]

    chunks = run(scenario())
    assert chunks[0].startswith(b"retry: ") and chunks[1].startswith(b"event: status")
    assert streams.open == 0 and cache.viewers == 0


def test_streams_past_the_caps_are_told_to_come_back_later() -> None:
    cache = StatusCache(Game(), interval=INTERVAL)
    streams = Streams(3, 2)

    async def scenario() -> None:
        def open_(address: str) -> AsyncGenerator[bytes]:
            return status_events(cache, streams, address, render)

        a1, a2, b1 = open_("192.0.2.1"), open_("192.0.2.1"), open_("2001:db8::1")
        for s in (a1, a2, b1):
            await take(s, 2)
        a3 = open_("192.0.2.1")  # per address
        assert await take(a3, 1) == [b"retry: 30000\nevent: busy\ndata: \n\n"]
        with pytest.raises(StopAsyncIteration):
            await anext(a3)
        c1 = open_("198.51.100.1")  # in all
        assert b"event: busy" in (await take(c1, 1))[0]
        await b1.aclose()
        b2 = open_("2001:db8::2")  # the same /64 as b1, whose place is free again
        assert (await take(b2, 2))[1].startswith(b"event: status")
        assert streams.open == 3
        for s in (a1, a2, b2):
            await s.aclose()
        assert streams.open == 0

    run(scenario())


def test_one_snapshot_fans_out_to_every_stream() -> None:
    game = Game()
    cache = StatusCache(game, interval=INTERVAL)
    streams = Streams(500, 500)

    async def scenario() -> None:
        all_ = [status_events(cache, streams, "192.0.2.1", render) for _ in range(200)]
        for s in all_:
            await take(s, 2)
        game.snapshot["held"] = 5
        game.snapshot["accounts"]["count"] = 99
        news = await asyncio.gather(*(anext(s) for s in all_))
        assert set(news) == {b"event: status\ndata: 99\ndata: line two\n\n"}
        for s in all_:
            await s.aclose()

    run(scenario())
    assert game.calls <= 3
