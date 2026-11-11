"""The write pump keeps to the client's rate, so the backlog waits in the outbox (DESIGN §3.3)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from fixtures.loopback import Client, enter, local_config, start
from fixtures.streams import FakeReader, FakeWriter, until, wait
from opencs import policy
from opencs.app import build_services
from opencs.clock import ManualClock, MonotonicClock
from opencs.connlog import ConnectionContext
from opencs.messages import server
from opencs.net.connection import Connection, ConnectionIO

RATE = 1000
BURST = policy.PACE_BURST_BYTES


class Pacer:
    """The pump's sleep: notes each wait and moves the clock, by more if the test asks. With the
    gate cleared it blocks, which is a pump holding back."""

    def __init__(self, clock: ManualClock, overrun: float = 0.0) -> None:
        self.clock = clock
        self.overrun = overrun
        self.waits: list[float] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.waiting = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.waiting.set()
        await self.gate.wait()
        self.clock.advance(seconds + self.overrun)
        await asyncio.sleep(0)


class Quiet:
    """A session that only holds its io, for the test to write with."""

    def __init__(self, io: ConnectionIO) -> None:
        self.io = io

    def feed(self, data: bytes) -> None:
        pass

    def tick(self, now: float) -> None:
        pass

    def closed(self) -> None:
        pass


def build(pacer: Pacer, rate: int = RATE) -> tuple[Connection, FakeReader, FakeWriter]:
    reader, writer = FakeReader(), FakeWriter()

    def factory(io: ConnectionIO, context: ConnectionContext) -> Quiet:
        return Quiet(io)

    connection = Connection(
        reader,
        writer,
        factory,
        pacer.clock,
        ConnectionContext(1),
        bytes_per_second=rate,
        pace_sleep=pacer.sleep,
    )
    return connection, reader, writer


def test_a_long_backlog_goes_out_at_the_rate() -> None:
    async def scenario() -> tuple[float, int]:
        pacer = Pacer(ManualClock())
        connection, reader, writer = build(pacer)
        running = asyncio.create_task(connection.run())
        for _ in range(100):
            connection.send_raw(b"x" * 100)
        await until(lambda: len(writer.data) == 10_000)
        reader.eof()
        await wait(running)
        return pacer.clock.now(), len(writer.written)

    elapsed, writes = asyncio.run(scenario())
    assert abs(elapsed - (10_000 - BURST) / RATE) < 1e-6
    assert writes == 100


def test_a_sleep_that_runs_long_does_not_lower_the_rate() -> None:
    async def scenario() -> float:
        pacer = Pacer(ManualClock(), overrun=0.002)  # a sleep long enough to matter at this rate
        connection, reader, writer = build(pacer)
        running = asyncio.create_task(connection.run())
        for _ in range(100):
            connection.send_raw(b"x" * 100)
        await until(lambda: len(writer.data) == 10_000)
        reader.eof()
        await wait(running)
        return pacer.clock.now()

    ideal = (10_000 - BURST) / RATE
    assert abs(asyncio.run(scenario()) - ideal) < 0.01  # the overrun came off the next wait


def test_a_large_item_goes_out_whole_and_is_paid_for_afterwards() -> None:
    async def scenario() -> tuple[list[bytes], list[float]]:
        pacer = Pacer(ManualClock())
        connection, reader, writer = build(pacer)
        running = asyncio.create_task(connection.run())
        connection.send_raw(b"x" * 5000)
        await until(lambda: bool(pacer.waits))
        reader.eof()
        await wait(running)
        return writer.written, pacer.waits

    written, waits = asyncio.run(scenario())
    assert [len(w) for w in written] == [5000]
    assert waits == [(5000 - BURST) / RATE]


def test_a_quiet_line_never_waits() -> None:
    async def scenario() -> list[float]:
        pacer = Pacer(ManualClock())
        connection, reader, writer = build(pacer)
        running = asyncio.create_task(connection.run())
        for n in range(1, 21):
            connection.send_raw(b"x" * BURST)
            await until(lambda n=n: len(writer.data) == n * BURST)  # type: ignore[misc]
            pacer.clock.advance(BURST / RATE)  # idle long enough to have paid for it
        reader.eof()
        await wait(running)
        return pacer.waits

    assert asyncio.run(scenario()) == []


def test_while_the_pump_holds_back_the_rest_is_a_backlog_the_server_can_see() -> None:
    async def scenario() -> tuple[int, int]:
        pacer = Pacer(ManualClock())
        pacer.gate.clear()
        connection, reader, writer = build(pacer)
        running = asyncio.create_task(connection.run())
        for _ in range(10):
            connection.send_raw(b"x" * 100)
        await pacer.waiting.wait()
        seen = (len(writer.written), connection.backlog())
        pacer.gate.set()
        reader.eof()
        await wait(running)
        return seen

    assert asyncio.run(scenario()) == (3, 7)  # past the burst after the third


def test_no_rate_means_no_pacing() -> None:
    async def scenario() -> list[float]:
        pacer = Pacer(ManualClock())
        connection, reader, writer = build(pacer, rate=0)
        running = asyncio.create_task(connection.run())
        connection.send_raw(b"x" * 100_000)
        await until(lambda: len(writer.data) == 100_000)
        reader.eof()
        await wait(running)
        return pacer.waits

    assert asyncio.run(scenario()) == []


def test_send_first_overtakes_what_the_pump_is_holding_back() -> None:
    async def scenario() -> bytes:
        pacer = Pacer(ManualClock())
        pacer.gate.clear()
        connection, reader, writer = build(pacer)
        running = asyncio.create_task(connection.run())
        connection.send_raw(b"x" * 300)
        connection.send_raw(b"later")
        await pacer.waiting.wait()
        connection.send_first(server.PingReply())
        pacer.gate.set()
        await until(lambda: writer.data.endswith(b"later"))
        reader.eof()
        await wait(running)
        return writer.written[1]

    assert b"later" not in asyncio.run(scenario())


def test_a_close_is_not_paced_so_a_long_backlog_still_says_goodbye() -> None:
    async def scenario() -> tuple[int, list[float]]:
        pacer = Pacer(ManualClock())
        pacer.gate.clear()
        connection, _, writer = build(pacer)
        running = asyncio.create_task(connection.run())
        for _ in range(200):  # 20 KB: over 20 s at the rate, against a 5 s flush timeout
            connection.send_raw(b"x" * 100)
        await pacer.waiting.wait()
        connection.close("bye")
        pacer.gate.set()
        await wait(running)
        return len(writer.data), pacer.waits

    sent, waits = asyncio.run(scenario())
    assert sent == 20_000
    assert len(waits) == 1  # the wait it was already in, and none after the close


def test_an_abort_ends_a_pacing_wait_at_once() -> None:
    async def scenario() -> bool:
        pacer = Pacer(ManualClock())
        pacer.gate.clear()  # never set: only the abort can end the wait
        connection, _, writer = build(pacer)
        running = asyncio.create_task(connection.run())
        connection.send_raw(b"x" * 1000)
        connection.send_raw(b"never sent")
        await pacer.waiting.wait()
        connection.abort("test")
        await wait(running)
        return b"never sent" in writer.data

    assert asyncio.run(scenario()) is False


@pytest.mark.judge
def test_the_real_clock_and_sleep_keep_to_the_rate_over_a_real_socket() -> None:
    """Without pacing the kernel would take all of this at once."""
    rate, size = 40_000, 20_000  # half a second

    async def scenario() -> tuple[float, int, int]:
        clock = MonotonicClock()
        connections: list[Connection] = []
        held: list[int] = []

        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            connection = Connection(
                reader,
                writer,
                lambda io, context: Quiet(io),
                clock,
                ConnectionContext(1),
                bytes_per_second=rate,
            )
            connections.append(connection)
            running = asyncio.create_task(connection.run())
            for _ in range(size // 100):
                connection.send_raw(b"x" * 100)
            await asyncio.sleep(0.1)
            held.append(connection.backlog())
            await running

        server_ = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server_.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        start_at = clock.now()
        got = 0
        while got < size:
            data = await asyncio.wait_for(reader.read(65536), 5)
            assert data
            got += len(data)
        elapsed = clock.now() - start_at
        writer.close()
        connections[0].abort("done")
        server_.close()
        return elapsed, got, held[0]

    elapsed, got, held = asyncio.run(scenario())
    assert got == size
    assert elapsed >= (size - BURST) / rate * 0.9
    assert held > 0


@pytest.mark.judge
def test_the_configured_rate_reaches_a_real_servers_connections() -> None:
    rate = 8000

    async def scenario() -> float:
        base = local_config()
        limits = replace(base.server.limits, client_bytes_per_second=rate)
        config = replace(base, server=replace(base.server, limits=limits))
        clock = ManualClock(1000.0)
        services = build_services(config, clock=clock)
        stop, task, port = await start(config, clock=clock, services=services)
        watcher: Client
        watcher, _ = await enter(port, "RED", "WATCHER,sesame")
        real = MonotonicClock()
        begun = real.now()
        size = 0
        for sink in services.router._sinks.values():
            for _ in range(100):
                sink.send(server.Chat(100, 0, b"x" * 70))
                size += 75
        got = 0
        while got < size - 300:
            data = await asyncio.wait_for(watcher.reader.read(65536), 10)
            assert data
            got += len(data)
        elapsed = real.now() - begun
        watcher.writer.close()
        stop.set()
        await asyncio.wait_for(task, 10)
        return elapsed

    assert asyncio.run(scenario()) >= 0.5  # 7.5 KB at 8 KB/s, against loopback's milliseconds
