"""Only the newest report of a pod waits in the outbox; nothing else is ever replaced (I §8.24)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fixtures.streams import FakeReader, FakeWriter, until, wait
from opencs.clock import ManualClock
from opencs.connlog import ConnectionContext
from opencs.messages import ServerMessage, decode_server, server
from opencs.messages.fields import WirePos
from opencs.net.connection import Connection
from opencs.wire.frame import FrameReader


def report(pod: int, x_off: int = 0, *, fwd: int = 0) -> server.PodState:
    return server.PodState(pod, WirePos(1, 2, x_off, 0, 0), 0, fwd, fwd, 0, 0, 0, 0)


class Quiet:
    def feed(self, data: bytes) -> None:
        pass

    def tick(self, now: float) -> None:
        pass

    def closed(self) -> None:
        pass


def written_after(queue: Callable[[Connection], None]) -> tuple[list[ServerMessage], Connection]:
    """What is sent of what ``queue`` puts in the outbox before the pump gets to run."""

    async def scenario() -> tuple[bytes, Connection]:
        reader, writer = FakeReader(), FakeWriter()
        connection = Connection(
            reader, writer, lambda io, context: Quiet(), ManualClock(), ConnectionContext(1)
        )
        queue(connection)
        connection.close("done")
        await wait(connection.run())
        return writer.data, connection

    data, connection = asyncio.run(scenario())
    return [decode_server(f.payload)[0] for f in FrameReader().feed(data)], connection


def test_a_newer_report_replaces_the_queued_one_where_it_sits() -> None:
    def queue(c: Connection) -> None:
        c.send(report(1, 10))
        c.send(report(2, 20))
        c.send(report(1, 11))
        c.send(report(1, 12))

    sent, connection = written_after(queue)
    assert sent == [report(1, 12), report(2, 20)]
    assert connection.traffic.replaced == 2


def test_the_close_summary_shows_how_far_behind_the_client_fell() -> None:
    def queue(c: Connection) -> None:
        c.send(report(1, 10))
        c.send(report(2, 20))
        c.send(server.PingReply())
        c.send(report(1, 11))

    _, connection = written_after(queue)
    assert connection.traffic.summary(1.0).endswith("; peak outbox 3, 1 replaced")


def test_a_report_of_stopping_is_never_lost() -> None:
    """With ``state`` 0 the client keeps a remote pod walking at its last speed (I §8.24)."""

    def queue(c: Connection) -> None:
        c.send(report(1, 10, fwd=50))
        c.send(report(1, 11, fwd=0))

    assert written_after(queue)[0] == [report(1, 11, fwd=0)]


def test_a_report_after_a_barrier_stays_behind_it() -> None:
    for barrier in (
        server.Delete(1),
        server.Destroy(1),
        server.ReleaseDrop(1),
    ):

        def queue(c: Connection, barrier: ServerMessage = barrier) -> None:
            c.send(report(1, 10))
            c.send(barrier)
            c.send(report(1, 11))
            c.send(report(1, 12))

        sent, _ = written_after(queue)
        assert sent == [report(1, 10), barrier, report(1, 12)]


def test_introducing_a_pod_is_a_barrier() -> None:
    introduce = server.Introduce(1, b"RED", 0, 0, report(1).pos, 0)
    assert introduce.barrier_id() == 1


def test_a_barrier_for_one_pod_leaves_the_others_replaceable() -> None:
    def queue(c: Connection) -> None:
        c.send(report(2, 20))
        c.send(server.Delete(1))
        c.send(report(2, 21))

    assert written_after(queue)[0] == [report(2, 21), server.Delete(1)]


def test_tagged_messages_and_shots_are_never_replaced_and_tags_stay_in_sequence() -> None:
    shot = server.Shot(1, report(1).pos, 0, 0, 1)

    def queue(c: Connection) -> None:
        for _ in range(3):
            c.send(server.PingReply())
            c.send(shot)
            c.send(report(1))

    async def scenario() -> list[bytes]:
        reader, writer = FakeReader(), FakeWriter()
        connection = Connection(
            reader, writer, lambda io, context: Quiet(), ManualClock(), ConnectionContext(1)
        )
        queue(connection)
        connection.close("done")
        await wait(connection.run())
        return [f.payload for f in FrameReader().feed(writer.data)]

    payloads = asyncio.run(scenario())
    assert [p[0:1] for p in payloads] == [b"E", b"c", b"b", b"E", b"c", b"E", b"c"]
    assert [p[1] for p in payloads if p[0:1] == b"E"] == [0x10, 0x11, 0x12]


def test_a_replacement_needs_no_room_in_a_full_outbox() -> None:
    async def scenario() -> tuple[bool, int]:
        reader, writer = FakeReader(), FakeWriter()
        connection = Connection(
            reader,
            writer,
            lambda io, context: Quiet(),
            ManualClock(),
            ConnectionContext(1),
            outbox_limit=2,
        )
        connection.send(report(1, 10))
        connection.send(report(2, 20))
        connection.send(report(1, 11))  # replaces: not an overflow
        aborted_early = connection._aborted
        connection.send(report(3, 30))  # a third pod: is one
        return aborted_early, connection.backlog()

    assert asyncio.run(scenario()) == (False, 0)


def test_a_report_queues_anew_once_the_last_has_been_written() -> None:
    async def scenario() -> list[ServerMessage]:
        reader, writer = FakeReader(), FakeWriter()
        connection = Connection(
            reader, writer, lambda io, context: Quiet(), ManualClock(), ConnectionContext(1)
        )
        running = asyncio.create_task(connection.run())
        connection.send(report(1, 10))
        first = len(server.PingReply().body())
        await until(lambda: len(writer.data) > first)
        connection.send(report(1, 11))
        connection.close("done")
        await wait(running)
        return [decode_server(f.payload)[0] for f in FrameReader().feed(writer.data)]

    assert asyncio.run(scenario()) == [report(1, 10), report(1, 11)]


def test_fifteen_moving_pods_over_a_real_socket_stay_fresh_and_are_not_cut() -> None:
    """A brawl at about three times what 960 B/s carries, read by a client that keeps up with that
    rate: the backlog stays one report a pod and the last positions arrive within a second. Without
    replacement it grows until the outbox limit cuts this client. Real sockets, a manual clock."""
    pods, rate = 15, 960
    gap, rounds = 1 / 8, 480  # 120 reports a second, about 2.8 KB/s, for a minute

    async def scenario() -> tuple[int, int, float, bool]:
        clock = ManualClock()

        async def pace(seconds: float) -> None:
            until_then = clock.now() + seconds
            while clock.now() < until_then:
                await asyncio.sleep(0)

        served: list[Connection] = []

        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            connection = Connection(
                reader,
                writer,
                lambda io, context: Quiet(),
                clock,
                ConnectionContext(1),
                bytes_per_second=rate,
                pace_sleep=pace,
            )
            served.append(connection)
            await connection.run()

        listening = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = listening.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        last: dict[int, int] = {}

        async def read_all() -> None:
            frames = FrameReader()
            while data := await reader.read(65536):
                for found in frames.feed(data):
                    message, _ = decode_server(found.payload)
                    assert isinstance(message, server.PodState)
                    last[message.id] = message.pos.x_off

        reading = asyncio.create_task(read_all())
        await until(lambda: bool(served))
        connection = served[0]
        final = (rounds - 1) % 100
        for n in range(rounds):
            for pod in range(pods):
                connection.send(report(pod, n % 100))
            clock.advance(gap)
            await asyncio.sleep(0)
        finished_at = clock.now()
        while sum(v == final for v in last.values()) < pods and not reading.done():
            clock.advance(0.01)
            await asyncio.sleep(0.001)
        delay = clock.now() - finished_at
        result = connection.traffic.peak_outbox, connection.traffic.replaced
        aborted = connection._aborted
        connection.abort("done")
        writer.close()
        reading.cancel()
        listening.close()
        return *result, delay, aborted

    peak, replaced, delay, aborted = asyncio.run(asyncio.wait_for(scenario(), 30))
    assert not aborted
    assert peak <= pods
    assert replaced > rounds * pods // 2
    assert delay < 1.0
