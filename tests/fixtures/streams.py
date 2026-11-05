"""In-memory stand-ins for an asyncio stream pair, and loop helpers that never sleep."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

TIMEOUT = 5.0


class FakeTransport:
    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


class FakeReader:
    """``read`` returns what was pushed; ``eof`` ends the stream."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    def push(self, data: bytes) -> None:
        self._queue.put_nowait(data)

    def eof(self) -> None:
        self._queue.put_nowait(b"")

    async def read(self, n: int, /) -> bytes:
        return await self._queue.get()


class FakeWriter:
    """Records writes; ``drain`` blocks while ``gate`` is cleared (a client not reading)."""

    def __init__(self) -> None:
        self.transport = FakeTransport()
        self.written: list[bytes] = []
        self.closed = False
        self.gate = asyncio.Event()
        self.gate.set()
        self.drain_blocked = asyncio.Event()

    def write(self, data: bytes, /) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        if not self.gate.is_set():
            self.drain_blocked.set()
            await self.gate.wait()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    @property
    def data(self) -> bytes:
        return b"".join(self.written)


async def until(predicate: Callable[[], Any], what: str = "condition") -> None:
    """Yield to the loop until ``predicate`` holds; fail after ``TIMEOUT`` seconds. The bound is
    time, not a count of yields: some conditions wait on a real timer (an ack stall), and a yield
    costs a microsecond on one platform and many more on another."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"never became true: {what}")
        await asyncio.sleep(0)


async def wait(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, TIMEOUT)
