"""The service channel (ADR 12): JSON lines over a Unix socket for the web process. Real sockets,
no sleeping: a reply is read when the server sends it."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import socket
import stat
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fixtures.loopback import TIMEOUT, frame, local_config, start
from fixtures.loopback import connect as connect_game
from fixtures.pods import report
from fixtures.sessions import World, enter_lobby, make_world, play
from fixtures.sessions import connect as connect_session
from opencs import __version__, policy
from opencs.app import Seams, StartupError, serve
from opencs.cities import CityManager
from opencs.config import ServiceConfig
from opencs.control import Control
from opencs.service import HELLO, ServiceServer
from opencs.store.base import ARGON2ID
from opencs.store.memory import memory_stores

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    """A directory with a short path: a Unix socket's path is limited to about 100 bytes, and
    pytest's ``tmp_path`` on macOS can be longer."""
    path = Path(tempfile.mkdtemp(prefix="ocs", dir="/tmp" if Path("/tmp").is_dir() else None))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class ServiceClient:
    """A scripted web process: a request out, its reply in."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._next_id = 0

    async def send_line(self, data: bytes) -> None:
        self.writer.write(data)
        await asyncio.wait_for(self.writer.drain(), TIMEOUT)

    async def receive(self) -> dict[str, Any]:
        line = await asyncio.wait_for(self.reader.readline(), TIMEOUT)
        assert line, "the server closed the connection"
        reply: dict[str, Any] = json.loads(line)
        return reply

    async def send(self, message: object) -> dict[str, Any]:
        await self.send_line(json.dumps(message).encode() + b"\n")
        return await self.receive()

    async def ask(self, op: str, **args: object) -> dict[str, Any]:
        """The reply to one request, checked to answer it."""
        self._next_id += 1
        reply = await self.send({"id": self._next_id, "op": op, **args})
        assert reply["id"] == self._next_id
        return reply

    async def result(self, op: str, **args: object) -> Any:
        reply = await self.ask(op, **args)
        assert reply["ok"], reply
        return reply["result"]

    async def closed(self) -> bool:
        try:
            return await asyncio.wait_for(self.reader.read(), TIMEOUT) == b""
        except ConnectionResetError:
            return True


async def dial(path: Path) -> ServiceClient:
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), TIMEOUT)
    return ServiceClient(reader, writer)


async def hello(path: Path) -> ServiceClient:
    c = await dial(path)
    reply = await c.send({"hello": HELLO, "version": 1})
    assert reply == {"hello": HELLO, "version": 1, "server": __version__}
    return c


@asynccontextmanager
async def running(
    sock_dir: Path, world: World | None = None, **kwargs: Any
) -> AsyncIterator[tuple[Path, World, asyncio.Event]]:
    world = world or make_world()
    stop = asyncio.Event()
    manager = CityManager(world.services, world.clock, random.Random(1))
    control = Control(world.services, manager, stop, None)
    service = ServiceServer(world.services, control, world.clock, **kwargs)
    path = sock_dir / "service.sock"
    await service.start(path)
    try:
        yield path, world, stop
    finally:
        await service.stop()
        control.close()


def run(coro: object) -> None:
    asyncio.run(asyncio.wait_for(coro, 4 * TIMEOUT))  # type: ignore[arg-type]


# --- the hello ---------------------------------------------------------------------------------


def test_a_hello_naming_a_spoken_version_opens_the_channel(sock_dir: Path) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, _, _):
            c = await hello(path)
            assert (await c.result("snapshot"))["server"] == __version__

    run(scenario())


@pytest.mark.parametrize(
    ("first", "error"),
    [
        ({"hello": HELLO, "version": 99}, "version"),
        ({"hello": "something else", "version": 1}, "hello"),
        ({"op": "snapshot"}, "hello"),
        ("not json", "hello"),
    ],
)
def test_a_wrong_hello_is_answered_and_closed(sock_dir: Path, first: object, error: str) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, _, _):
            c = await dial(path)
            line = first.encode() if isinstance(first, str) else json.dumps(first).encode()
            await c.send_line(line + b"\n")
            reply = await c.receive()
            assert (reply["ok"], reply["error"]) == (False, error)
            if error == "version":
                assert reply["versions"] == list(policy.SERVICE_PROTOCOL_VERSIONS)
            assert await c.closed()

    run(scenario())


def test_no_hello_in_time_closes_without_a_reply(sock_dir: Path) -> None:
    async def scenario() -> None:
        async with running(sock_dir, hello_timeout=0.01) as (path, _, _):
            c = await dial(path)
            assert await c.closed()

    run(scenario())


# --- the socket --------------------------------------------------------------------------------


def test_the_socket_is_for_the_servers_user_and_group_only_and_goes_at_stop(
    sock_dir: Path,
) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, _, _):
            mode = path.stat().st_mode
            assert stat.S_ISSOCK(mode)
            assert stat.S_IMODE(mode) == policy.SERVICE_SOCKET_MODE
        assert not path.exists()

    run(scenario())


def test_a_socket_left_by_a_dead_server_is_replaced_but_another_file_is_not(
    sock_dir: Path,
) -> None:
    path = sock_dir / "service.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # the file stays, as after a crash

    async def scenario() -> None:
        async with running(sock_dir) as (live, _, _):
            await hello(live)
        path.write_text("not a socket")
        world = make_world()
        manager = CityManager(world.services, world.clock, random.Random(1))
        control = Control(world.services, manager, asyncio.Event(), None)
        with pytest.raises(FileExistsError):
            await ServiceServer(world.services, control, world.clock).start(path)

    run(scenario())
    assert path.read_text() == "not a socket"


def test_more_connections_than_the_limit_are_closed_at_once(sock_dir: Path) -> None:
    async def scenario() -> None:
        async with running(sock_dir, max_connections=1) as (path, _, _):
            first = await hello(path)
            second = await dial(path)
            assert await second.closed()
            assert (await first.result("snapshot"))["accounts"]["count"] == 0

    run(scenario())


# --- requests ----------------------------------------------------------------------------------


def test_the_snapshot_shows_who_plays_where_and_nothing_private(sock_dir: Path) -> None:
    world = make_world()
    play(world, "A", 1, "ALICE,tangerine2")
    play(world, "A", 0)  # a guest, JOINGENIE
    enter_lobby(world, "CAROL,tangerine2")

    async def scenario() -> None:
        async with running(sock_dir, world) as (path, _, _):
            world.clock.advance(42)
            snapshot = await (await hello(path)).result("snapshot")
            assert snapshot["uptime"] == 42.0
            assert snapshot["accounts"] == {
                "count": 2,
                "max": policy.MAX_ACCOUNTS,
                "registration": "open",
            }
            assert snapshot["connections"] == {"total": 3, "game": 2, "chamber": 1}
            a = snapshot["cities"][0]
            assert (a["letter"], a["name"], a["open"]) == ("A", "CITY A", True)
            assert a["capacity"] == world.services.cities["A"].config.capacity
            assert a["players"] == [
                {"callsign": "JOINGENIE", "team": "RED", "guest": True},
                {"callsign": "ALICE", "team": "BLUE", "guest": False},
            ]
            assert [c["players"] for c in snapshot["cities"][1:]] == [[], []]
            assert snapshot["held"] == 0
            text = json.dumps(snapshot)
            assert "127.0.0.1" not in text and "tangerine2" not in text.lower()

    run(scenario())


def test_an_account_registered_over_the_channel_logs_in_and_its_password_is_never_logged(
    sock_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, world, _):
            c = await hello(path)
            with caplog.at_level(logging.DEBUG):
                made = await c.result("register", name="newpilot")
            assert made["name"] == "NEWPILOT"
            assert made["password"] not in caplog.text
            h = enter_lobby(world, f"NEWPILOT,{made['password']}")
            assert h.session.pilot is not None
            found = await c.result("account", name="newpilot")
            assert found == {
                "name": "NEWPILOT",
                "exists": True,
                "online": True,
                "callsign": "NEWPILOT",
                "created": made["created"],
                "last_login": found["last_login"],
            }
            assert isinstance(made["created"], float)
            assert found["last_login"] >= made["created"]
            h.session.closed()
            again = await c.result("reset-password", name="NEWPILOT")
            assert again["password"] != made["password"]
            assert again["password"] not in caplog.text
            assert enter_lobby(world, f"NEWPILOT,{again['password']}").session.pilot is not None

    run(scenario())


def test_the_web_may_choose_the_password_within_the_servers_rules(
    sock_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, world, _):
            c = await hello(path)
            with caplog.at_level(logging.DEBUG):
                made = await c.result("register", name="chooser", password="PICK,ME!1")
                assert made == {
                    "name": "CHOOSER",
                    "password": "PICK,ME!1",
                    "callsign": None,
                    "created": made["created"],
                }
                again = await c.result("reset-password", name="CHOOSER", password="SECOND2")
                assert again["password"] == "SECOND2"
            assert "PICK,ME!1" not in caplog.text and "SECOND2" not in caplog.text
            assert enter_lobby(world, "CHOOSER,SECOND2").session.pilot is not None
            for password, reason in (
                ("X" * 13, "A PASSWORD MAY BE AT MOST 12"),
                ("ABC", "A NEW"),
                ("secret", "A PASSWORD MAY HAVE NO LOWER CASE"),
            ):
                reply = await c.ask("register", name="OTHER", password=password)
                assert (reply["error"], reply["message"][: len(reason)]) == ("refused", reason)
            reply = await c.ask("reset-password", name="CHOOSER", password=12345678)
            assert reply["error"] == "bad-request"

    run(scenario())


def test_a_reset_over_the_channel_opens_an_account_the_guessers_locked(sock_dir: Path) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, world, _):
            enter_lobby(world, "ALICE,oldsecret").session.closed()
            for n in range(policy.FAILED_LOGINS_PER_ACCOUNT[0]):
                connect_session(world, address=f"10.7.{n}.1").feed("HHH\rALICE,guess\r")
            locked = connect_session(world, address="10.6.0.1")
            locked.feed("HHH\rALICE,oldsecret\r")
            assert b"TOO MANY WRONG PASSWORDS" in locked.take()
            c = await hello(path)
            password = (await c.result("reset-password", name="alice"))["password"]
            again = enter_lobby(world, f"ALICE,{password}", address="10.6.0.1")
            assert again.session.pilot is not None

    run(scenario())


def test_a_password_the_web_chooses_meets_every_rule_and_is_hashed_with_argon2id(
    sock_dir: Path,
) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, world, _):
            c = await hello(path)
            await c.result("register", name="chooser", password="FIRST1")
            for op in ("register", "reset-password"):
                name = "NEWCOMER" if op == "register" else "CHOOSER"
                for password, reason in (
                    ("PASSWORD1", "THAT PASSWORD IS TOO COMMON"),
                    (name, "A PASSWORD MUST NOT BE THE ACCOUNT NAME"),
                ):
                    reply = await c.ask(op, name=name, password=password)
                    assert reply["error"] == "refused" and reply["message"].startswith(reason)
            first = world.services.identity._store.get("CHOOSER")
            await c.result("reset-password", name="CHOOSER")
            again = world.services.identity._store.get("CHOOSER")
            assert first is not None and again is not None
            assert first.kdf.algorithm == again.kdf.algorithm == ARGON2ID
            assert again.password_hash != first.password_hash

    run(scenario())


def test_the_web_sets_a_callsign_under_the_players_rules_unless_forced(sock_dir: Path) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, _, _):
            c = await hello(path)
            await c.result("register", name="alice")
            named = await c.result("register", name="bob", callsign="B0b")
            assert named["callsign"] == "B0b"
            taken = await c.ask("register", name="carol", callsign="b0B")
            assert (taken["error"], taken["message"]) == ("refused", "THAT CALLSIGN IS TAKEN")
            assert (await c.result("account", name="carol"))["exists"] is False
            made = await c.result("set-callsign", name="alice", callsign="Al:ce")
            assert made == {"name": "ALICE", "callsign": "Al:ce"}
            assert (await c.result("account", name="ALICE"))["callsign"] == "Al:ce"
            again = await c.ask("set-callsign", name="alice", callsign="Ace")
            assert (again["error"], again["message"]) == (
                "refused",
                "YOU MAY CHOOSE A NEW CALLSIGN IN 7 DAYS",
            )
            forced = await c.result("set-callsign", name="alice", callsign="Ace", force=True)
            assert forced["callsign"] == "Ace"
            for bad in ({"callsign": "two words"}, {"callsign": 7}, {"force": "yes"}):
                request = {"name": "alice", "callsign": "Ok1", **bad}
                assert (await c.ask("set-callsign", **request))["error"] == "bad-request"
            reply = await c.ask("set-callsign", name="nobody", callsign="Ace2")
            assert reply["error"] == "refused"

    run(scenario())


@pytest.mark.parametrize(
    ("op", "name", "reason"),
    [
        ("register", "ADMIN", "THAT NAME IS RESERVED"),
        ("register", "JOINGENIE2", "THAT NAME IS RESERVED"),
        ("register", "AB", "A NEW NAME MUST BE"),
        ("reset-password", "NOBODY", "NO SUCH ACCOUNT"),
    ],
)
def test_the_servers_rules_refuse_a_name_and_the_web_is_told_why(
    sock_dir: Path, op: str, name: str, reason: str
) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, _, _):
            reply = await (await hello(path)).ask(op, name=name)
            assert (reply["ok"], reply["error"]) == (False, "refused")
            assert reply["message"].startswith(reason)

    run(scenario())


def test_new_passwords_are_metered_over_every_connection_and_refusals_cost_nothing(
    sock_dir: Path,
) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, world, _):
            first, second = await hello(path), await hello(path)
            burst = policy.SERVICE_HASH_LIMIT[0]
            for _ in range(burst):
                assert not (await first.ask("register", name="ADMIN"))["ok"]  # refused: free
            for i in range(burst):
                await (first if i % 2 else second).result("register", name=f"PILOT{i}")
            over = await second.ask("register", name="LATE")
            assert (over["ok"], over["error"]) == (False, "rate")
            assert not world.identity.account_exists("LATE")
            world.clock.advance(1 / policy.SERVICE_HASH_LIMIT[1])
            await first.result("register", name="LATE")

    run(scenario())


def test_requests_over_the_rate_are_refused_and_the_connection_stays(sock_dir: Path) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, world, _):
            c = await hello(path)
            for _ in range(policy.SERVICE_REQUEST_LIMIT[0]):
                await c.result("snapshot")
            over = await c.ask("snapshot")
            assert (over["ok"], over["error"]) == (False, "rate")
            world.clock.advance(1)
            await c.result("snapshot")

    run(scenario())


@pytest.mark.parametrize(
    ("request_", "error"),
    [
        ({"id": 1, "op": "nonsense"}, "unknown-op"),
        ({"id": 1}, "bad-request"),
        ({"id": 1, "op": 7}, "bad-request"),
        ([1, 2], "bad-request"),
        ({"id": 1, "op": "snapshot", "extra": True}, "bad-request"),
        ({"id": 1, "op": "register"}, "bad-request"),
        ({"id": 1, "op": "register", "name": "TWO WORDS"}, "bad-request"),
        ({"id": 1, "op": "register", "name": "X" * 33}, "bad-request"),
        ({"id": 1, "op": "register", "name": "BELL\u0007"}, "bad-request"),
        ({"id": 1, "op": "register", "name": "NEW", "password": ["mine"]}, "bad-request"),
        ({"id": 1, "op": "announce", "text": " "}, "bad-request"),
        ({"id": 1, "op": "announce", "text": "line\nbreak"}, "bad-request"),
        ({"id": 1, "op": "kick", "callsign": "ALICE", "reason": 5}, "bad-request"),
        ({"id": 1, "op": "guppy-home", "team": "PURPLE"}, "bad-request"),
        ({"id": 1, "op": "shutdown", "seconds": -1}, "bad-request"),
        ({"id": 1, "op": "shutdown", "seconds": True}, "bad-request"),
    ],
)
def test_a_bad_request_is_refused_and_the_connection_stays(
    sock_dir: Path, request_: object, error: str
) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, _, stop):
            c = await hello(path)
            reply = await c.send(request_)
            assert (reply["ok"], reply["error"]) == (False, error), reply
            assert (await c.result("snapshot"))["accounts"]["count"] == 0
            assert not stop.is_set()

    run(scenario())


def test_a_line_that_is_not_json_is_refused_and_the_connection_stays(sock_dir: Path) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, _, _):
            c = await hello(path)
            await c.send_line(b"{oops\n")
            reply = await c.receive()
            assert (reply["id"], reply["ok"], reply["error"]) == (None, False, "bad-request")
            await c.result("snapshot")

    run(scenario())


def test_an_overlong_line_closes_the_connection(sock_dir: Path) -> None:
    async def scenario() -> None:
        async with running(sock_dir) as (path, _, _):
            c = await hello(path)
            await c.send_line(b"x" * (policy.SERVICE_MAX_LINE + 1) + b"\n")
            assert await c.closed()

    run(scenario())


def test_the_operators_commands_work_over_the_channel(sock_dir: Path) -> None:
    world = make_world()
    flier = play(world, "A", 0, "ALICE,tangerine2")
    flier.feed(frame(report(4)))  # reporting: in the game proper, so an announcement reaches it
    flier.feed(frame(report(0)))
    enter_lobby(world, "BOB,tangerine2").session.closed()

    async def scenario() -> None:
        async with running(sock_dir, world) as (path, _, stop):
            c = await hello(path)
            assert (await c.result("status"))["lines"][0] == "uptime: 0 s"
            assert (await c.result("announce", text="hello pilots"))["lines"] == ["announced to 1"]
            kicked = await c.result("kick", callsign="alice", reason="testing")
            assert kicked["lines"] == ["kicked ALICE (1 connection(s))"]
            assert (await c.result("guppy-home", team="red"))["lines"][0].startswith("no RED")
            assert (await c.result("reset-progress", name="bob"))["lines"] == [
                "progress of BOB was empty"
            ]
            assert (await c.result("delete-account", name="bob"))["lines"] == ["deleted BOB"]
            assert await c.result("account", name="bob") == {
                "name": "BOB",
                "exists": False,
                "online": False,
                "callsign": None,
                "created": None,
                "last_login": None,
            }
            assert (await c.result("reload"))["lines"] == [
                "no config file is known: nothing to reload"
            ]
            assert (await c.result("shutdown", seconds=60))["lines"] == ["shutting down in 60 s"]
            assert (await c.result("shutdown", cancel=True))["lines"] == ["shutdown canceled"]
            assert not stop.is_set()
            assert (await c.result("shutdown"))["lines"] == ["shutting down now"]
            assert stop.is_set()

    run(scenario())


def test_status_gives_each_citys_health_as_numbers_for_the_admin_pages(sock_dir: Path) -> None:
    world = make_world()
    play(world, "A", 0, "ALICE,tangerine2")
    city = world.services.cities["A"]
    city.hits.hits_without_a_shot = 3
    city.hits.distant_hits = 1

    async def scenario() -> None:
        async with running(sock_dir, world) as (path, _, _):
            c = await hello(path)
            status = await c.result("status")
            assert any(
                "[odd hit reports: 3 without a shot, 1 from far away]" in line
                for line in status["lines"]
            )
            by_letter = {entry["letter"]: entry for entry in status["cities"]}
            assert by_letter["A"] == {
                "letter": "A",
                "name": city.config.name,
                "players": 1,
                "failures": 0,
                "rebuilds": 0,
                "slow_calls": 0,
                "worst_call_ms": 0,
                "hits_without_a_shot": 3,
                "distant_hits": 1,
            }
            assert len(by_letter) == len(world.services.cities)
            # The public snapshot has none of it.
            snapshot = json.dumps(await c.result("snapshot"))
            assert "hits" not in snapshot and "failures" not in snapshot

    run(scenario())


def test_a_request_that_holds_up_the_loop_is_logged_once_a_minute(
    sock_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = [0.0]

    def timer() -> float:  # every reading is 50 ms after the last: each request looks slow
        now[0] += 0.05
        return now[0]

    async def scenario() -> None:
        async with running(sock_dir, timer=timer) as (path, _, _):
            c = await hello(path)
            with caplog.at_level(logging.WARNING, "opencs.service"):
                for _ in range(3):
                    await c.result("snapshot")
                now[0] += policy.SLOW_SERVICE_LOG_SECONDS
                await c.result("snapshot")
            warnings = [r.getMessage() for r in caplog.records if "held up" in r.getMessage()]
            assert len(warnings) == 2
            assert "snapshot" in warnings[0] and "(1 slow" in warnings[0]
            assert "(3 slow" in warnings[1]  # the two not logged, and this one

    run(scenario())


# --- in the running server ---------------------------------------------------------------------


def test_a_server_with_a_service_socket_serves_it_and_removes_it_at_stop(sock_dir: Path) -> None:
    path = sock_dir / "service.sock"
    config = local_config()
    config = replace(config, server=replace(config.server, service=ServiceConfig(path)))

    async def scenario() -> None:
        stop, task, port = await start(config)
        c = await hello(path)
        made = await c.result("register", name="webpilot")
        game = await connect_game(port)  # the account logs in to the game with its password
        await game.send(f"HHH\rWEBPILOT,{made['password']}\r<Not Telling!>\rM 1380;3\r")
        await game.expect(b"\x05PRESS RETURN\r")
        game.writer.close()
        stop.set()
        await asyncio.wait_for(task, TIMEOUT)
        assert not path.exists()

    run(scenario())


def test_a_service_socket_that_cannot_be_made_stops_the_server_at_start(sock_dir: Path) -> None:
    path = sock_dir / "missing" / "service.sock"  # no such directory
    config = local_config()
    config = replace(config, server=replace(config.server, service=ServiceConfig(path)))

    async def scenario() -> None:
        await serve(config, stop=asyncio.Event(), seams=Seams(stores=memory_stores()))

    with pytest.raises(StartupError, match="service channel"):
        run(scenario())
