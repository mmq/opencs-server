"""The client's 4096-byte ring never holds more than the window (ADR 11, net/flow.py)."""

from __future__ import annotations

import asyncio
from typing import Any

from fixtures.streams import FakeReader, FakeWriter, until, wait
from opencs import policy
from opencs.clock import ManualClock
from opencs.connlog import STREAM_GAME, ConnectionContext
from opencs.messages import ServerMessage, decode_server, server
from opencs.messages.fields import WirePos
from opencs.net.connection import Connection
from opencs.net.flow import AckWindow
from opencs.wire.frame import FrameReader

LIMIT = 200
PROBE = policy.ACK_PROBE_BYTES


# --- the window ------------------------------------------------------------------------------


def test_an_ack_confirms_everything_written_up_to_its_frame() -> None:
    w = AckWindow(1000, PROBE)
    w.wrote(100, 0x10, 0.0)
    w.wrote(50, None, 0.0)
    w.wrote(100, 0x11, 0.0)
    w.wrote(50, None, 0.0)
    assert w.unacked == 300
    assert w.acked(bytes([0x10]), 1.0)
    assert w.unacked == 200
    assert w.acked(bytes([0x11]), 1.0)
    assert w.unacked == 50  # what follows the last tag is not vouched for


def test_a_later_tag_vouches_for_an_earlier_one_that_was_lost() -> None:
    w = AckWindow(1000, PROBE)
    w.wrote(100, 0x10, 0.0)
    w.wrote(100, 0x11, 0.0)
    assert w.acked(bytes([0x11]), 1.0)
    assert w.unacked == 0
    assert not w.acked(bytes([0x10]), 2.0)  # already vouched for: no use, and no harm


def test_an_ack_for_a_tag_never_sent_confirms_nothing() -> None:
    w = AckWindow(1000, PROBE)
    w.wrote(100, 0x10, 0.0)
    assert not w.acked(bytes([0x77]), 1.0)
    assert w.unacked == 100


def test_a_tag_seen_again_after_wrapping_means_the_new_frame() -> None:
    w = AckWindow(100_000, PROBE)
    for i in range(240 * 3):  # each tag many times over; only the newest are outstanding
        w.wrote(10, 0x10 + i % 240, float(i))
    last = 0x10 + (240 * 3 - 1) % 240
    assert w.acked(bytes([last]), 999.0)
    assert w.unacked == 0
    assert w.worst_delay == 999.0 - (240 * 3 - 1)


def test_the_peak_and_the_worst_delay_are_kept() -> None:
    w = AckWindow(1000, PROBE)
    w.wrote(300, 0x10, 5.0)
    w.acked(bytes([0x10]), 7.5)
    w.wrote(100, 0x11, 8.0)
    w.acked(bytes([0x11]), 8.5)
    assert (w.peak, w.worst_delay) == (300, 2.5)


def test_it_wants_a_probe_after_many_bytes_with_no_tag() -> None:
    w = AckWindow(100_000, PROBE)
    w.wrote(PROBE - 1, None, 0.0)
    assert not w.wants_probe
    w.wrote(1, None, 0.0)
    assert w.wants_probe
    w.wrote(10, 0x10, 0.0)
    assert not w.wants_probe


# --- the pump --------------------------------------------------------------------------------


class Quiet:
    def feed(self, data: bytes) -> None:
        pass

    def tick(self, now: float) -> None:
        pass

    def closed(self) -> None:
        pass


def build(
    stall: float = 60.0, stream: str = STREAM_GAME
) -> tuple[Connection, FakeReader, FakeWriter]:
    reader, writer = FakeReader(), FakeWriter()
    context = ConnectionContext(1)
    context.stream = stream
    connection = Connection(
        reader,
        writer,
        lambda io, ctx: Quiet(),
        ManualClock(),
        context,
        unacked_bytes=LIMIT,
        ack_stall=stall,
    )
    return connection, reader, writer


def sent(writer: FakeWriter) -> list[tuple[ServerMessage, int | None]]:
    return [decode_server(f.payload) for f in FrameReader().feed(writer.data)]


def tags(writer: FakeWriter) -> list[int]:
    return [tag for _, tag in sent(writer) if tag is not None]


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def test_the_pump_stops_at_the_limit_and_an_ack_lets_it_go_on() -> None:
    async def scenario() -> tuple[int, int, int]:
        connection, reader, writer = build()
        running = asyncio.create_task(connection.run())
        for _ in range(100):
            connection.send(server.PingReply())
        await settle()
        held, queued = len(writer.data), connection.backlog()
        connection.acked(bytes(tags(writer)))
        await settle()
        after = len(writer.data)
        reader.eof()
        await wait(running)
        return held, queued, after

    held, queued, after = asyncio.run(scenario())
    assert LIMIT <= held < LIMIT + 16  # one frame's overshoot at most
    assert queued > 0
    assert LIMIT <= after - held < LIMIT + 16


def test_while_held_a_newer_report_still_replaces_a_queued_one() -> None:
    pos = server.PodState(1, _pos(10), 0, 0, 0, 0, 0, 0, 0)
    newer = server.PodState(1, _pos(11), 0, 0, 0, 0, 0, 0, 0)

    async def scenario() -> tuple[int, int]:
        connection, reader, _ = build()
        running = asyncio.create_task(connection.run())
        for _ in range(100):
            connection.send(server.PingReply())
        connection.send(pos)
        await settle()
        connection.send(newer)
        replaced = connection.traffic.replaced
        queued = connection.backlog()
        reader.eof()
        await wait(running)
        return replaced, queued

    replaced, queued = asyncio.run(scenario())
    assert replaced == 1
    assert queued > 1


def _pos(x_off: int) -> Any:
    return WirePos(1, 2, x_off, 0, 0)


def test_acks_that_stop_coming_do_not_stop_the_pump_for_ever() -> None:
    """A frame can be lost, so the ack being waited for may never come."""

    async def scenario() -> tuple[int, int]:
        connection, reader, writer = build(stall=0.01)
        running = asyncio.create_task(connection.run())
        for _ in range(100):
            connection.send(server.PingReply())
        await until(lambda: connection.backlog() == 0)
        stalls = connection.traffic.ack_stalls
        reader.eof()
        await wait(running)
        return stalls, len(tags(writer))

    stalls, sent_tags = asyncio.run(scenario())
    assert stalls > 0
    assert sent_tags == 100


def test_a_close_is_not_held_for_acks() -> None:
    async def scenario() -> int:
        connection, _, writer = build()
        running = asyncio.create_task(connection.run())
        for _ in range(100):
            connection.send(server.PingReply())
        await settle()
        connection.close("bye")
        await wait(running)
        return len(tags(writer))

    assert asyncio.run(scenario()) == 100


def test_an_abort_ends_the_wait_at_once() -> None:
    async def scenario() -> int:
        connection, _, _ = build()
        running = asyncio.create_task(connection.run())
        for _ in range(100):
            connection.send(server.PingReply())
        await settle()
        connection.abort("test")
        await wait(running)
        return connection.backlog()

    assert asyncio.run(scenario()) == 0


def test_chamber_text_is_not_held_because_nothing_acknowledges_it() -> None:
    async def scenario() -> int:
        connection, reader, writer = build(stream="chamber")
        running = asyncio.create_task(connection.run())
        for _ in range(100):
            connection.send_raw(b"x" * 100)
        await settle()
        sent_bytes = len(writer.data)
        reader.eof()
        await wait(running)
        return sent_bytes

    assert asyncio.run(scenario()) == 10_000


def test_a_long_run_of_untagged_frames_gets_a_probe_to_acknowledge() -> None:
    report = server.PodState(1, _pos(10), 0, 0, 0, 0, 0, 0, 0)

    async def scenario() -> list[ServerMessage]:
        connection, reader, writer = build(stall=0.01)
        running = asyncio.create_task(connection.run())
        for pod in range(40):
            connection.send(server.PodState(pod, report.pos, 0, 0, 0, 0, 0, 0, 0))
        await until(lambda: connection.backlog() == 0)
        reader.eof()
        await wait(running)
        return [m for m, _ in sent(writer)]

    messages = asyncio.run(scenario())
    assert server.EchoProbe(0) in messages
    assert sum(isinstance(m, server.PodState) for m in messages) == 40


def test_the_pump_never_has_more_than_the_limit_unacknowledged_while_the_client_acks() -> None:
    """A game reading slowly: however much is queued, the ring holds the limit at most, nearly."""

    async def scenario() -> int:
        connection, reader, writer = build()
        running = asyncio.create_task(connection.run())
        acked = 0
        for _ in range(500):
            connection.send(server.PingReply())
        while connection.backlog():
            await settle()
            found = tags(writer)
            connection.acked(bytes(found[acked : acked + 5]))
            acked = min(len(found), acked + 5)
            await settle()
        reader.eof()
        await wait(running)
        return connection._window.peak if connection._window else 0

    assert asyncio.run(scenario()) < LIMIT + 16
