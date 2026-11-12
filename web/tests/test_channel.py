import asyncio
import time
from collections.abc import Awaitable, Iterator
from pathlib import Path
from typing import Any

import pytest

from fakeservice import FakeService, socket_dir
from opencs import policy, service
from opencs_web import channel
from opencs_web.channel import RefusedError, ServiceChannel, UnavailableError


@pytest.fixture
def sock() -> Iterator[Path]:
    with socket_dir() as d:
        yield d / "service.sock"


def run(scenario: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(scenario, 10))


def test_the_client_speaks_a_version_the_server_speaks() -> None:
    assert channel.HELLO == service.HELLO
    assert channel.PROTOCOL_VERSION in policy.SERVICE_PROTOCOL_VERSIONS


def test_a_snapshot_after_the_hello(sock: Path) -> None:
    async def scenario() -> None:
        fake = await FakeService(sock).start()
        client = ServiceChannel(sock)
        assert await client.request("snapshot") == fake.snapshot
        assert await client.request("snapshot") == fake.snapshot
        assert fake.hellos == [{"hello": "opencs-service", "version": 1}]
        assert fake.connections == 1  # the connection is kept
        assert [r["op"] for r in fake.requests] == ["snapshot", "snapshot"]
        assert client.server_version == "1.0.0"
        await client.close()
        await fake.stop()

    run(scenario())


def test_without_a_socket_configured_the_game_is_unavailable() -> None:
    with pytest.raises(UnavailableError, match="no service socket"):
        run(ServiceChannel(None).request("snapshot"))


def test_a_missing_socket_is_unavailable_and_retried_only_after_a_backoff(sock: Path) -> None:
    now = [100.0]
    client = ServiceChannel(sock, backoff_first=1.0, clock=lambda: now[0])

    async def scenario() -> None:
        with pytest.raises(UnavailableError, match="no service socket"):
            await client.request("snapshot")
        fake = await FakeService(sock).start()
        with pytest.raises(UnavailableError):  # still backing off: no attempt
            await client.request("snapshot")
        assert fake.connections == 0
        now[0] += 1.0
        assert await client.request("snapshot") == fake.snapshot
        assert fake.connections == 1
        await client.close()
        await fake.stop()

    run(scenario())


def test_the_backoff_doubles_up_to_its_ceiling(sock: Path) -> None:
    now = [0.0]
    client = ServiceChannel(sock, backoff_first=0.5, backoff_max=2.0, clock=lambda: now[0])
    waits = []

    async def scenario() -> None:
        for _ in range(5):
            with pytest.raises(UnavailableError):
                await client.request("snapshot")
            waits.append(client._retry_at - now[0])
            now[0] = client._retry_at

    run(scenario())
    assert waits == [0.5, 1.0, 2.0, 2.0, 2.0]


@pytest.mark.parametrize(
    "reply",
    [
        {"ok": False, "error": "version", "message": "protocol version 1 is not spoken here"},
        {"ok": False, "error": "hello", "message": "not a hello"},
        {"hello": "something-else", "version": 1},
    ],
)
def test_a_refused_hello_is_unavailable(sock: Path, reply: dict[str, Any]) -> None:
    async def scenario() -> None:
        fake = await FakeService(sock).start()
        fake.hello_reply = reply
        client = ServiceChannel(sock)
        with pytest.raises(UnavailableError, match="refused the hello"):
            await client.request("snapshot")
        assert not client.connected
        await fake.stop()

    run(scenario())


def test_a_server_that_closes_at_once_is_unavailable(sock: Path) -> None:
    """As the server does past its connection limit."""

    async def scenario() -> None:
        async def close(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()

        server = await asyncio.start_unix_server(close, sock)
        with pytest.raises(UnavailableError, match="closed the connection"):
            await ServiceChannel(sock).request("snapshot")
        server.close()
        await server.wait_closed()

    run(scenario())


def test_a_refused_request_keeps_the_connection(sock: Path) -> None:
    async def scenario() -> None:
        fake = await FakeService(sock).start()
        client = ServiceChannel(sock)
        fake.refuse = "rate"
        with pytest.raises(RefusedError) as refused:
            await client.request("snapshot")
        assert refused.value.code == "rate"
        fake.refuse = None
        assert await client.request("snapshot") == fake.snapshot
        assert fake.connections == 1
        await client.close()
        await fake.stop()

    run(scenario())


def test_a_server_that_does_not_answer_times_out(sock: Path) -> None:
    async def scenario() -> None:
        fake = await FakeService(sock).start()
        fake.silent = True
        client = ServiceChannel(sock, timeout=0.2)
        started = time.monotonic()
        with pytest.raises(UnavailableError, match="in time"):
            await client.request("snapshot")
        assert time.monotonic() - started < 1.0
        assert not client.connected  # its late reply would be out of step
        await fake.stop()

    run(scenario())


def test_a_restarted_server_is_reached_again_at_once(sock: Path) -> None:
    async def scenario() -> None:
        fake = await FakeService(sock).start()
        client = ServiceChannel(sock)
        await client.request("snapshot")
        await fake.stop()
        fake = await FakeService(sock).start()
        assert await client.request("snapshot") == fake.snapshot
        await client.close()
        await fake.stop()

    run(scenario())


def test_a_stopped_server_is_unavailable_until_it_is_back(sock: Path) -> None:
    now = [0.0]

    async def scenario() -> None:
        fake = await FakeService(sock).start()
        client = ServiceChannel(sock, clock=lambda: now[0])
        await client.request("snapshot")
        await fake.stop()
        with pytest.raises(UnavailableError):
            await client.request("snapshot")
        fake = await FakeService(sock).start()
        now[0] += channel.BACKOFF_MAX
        assert await client.request("snapshot") == fake.snapshot
        await client.close()
        await fake.stop()

    run(scenario())
