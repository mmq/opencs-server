"""The live status stream: server-sent events from the shared :class:`StatusCache`.

One snapshot a second fans out to every stream; a stream sends the status when it changes and a
comment line as a heartbeat otherwise. Streams are capped in all and per address (an IPv6 /64).
A stream past a cap is told to come back later (an ``event: busy`` with a long ``retry``), and
every stream ends after ``LIFETIME`` and is reopened by the browser, so no one holds a place for
ever. The page itself works without them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from collections.abc import AsyncGenerator, Callable, Iterator

from django.conf import settings

from opencs.net.listener import source_of
from opencs_web.channel import ServiceChannel
from opencs_web.status import Status, StatusCache

#: Seconds between heartbeats while nothing changes: well under the idle timeouts of proxies.
HEARTBEAT = 15.0
#: Seconds a stream lasts before the browser is asked to reconnect.
LIFETIME = 600.0
#: What the browser waits before reconnecting: after a stream ends, and when turned away.
RETRY_MS = 2000
BUSY_RETRY_MS = 30000


class Streams:
    """Places for live streams: ``total`` in all, ``per_address`` from one source."""

    def __init__(self, total: int, per_address: int) -> None:
        self.total = total
        self.per_address = per_address
        self._open: Counter[str] = Counter()

    @property
    def open(self) -> int:
        return self._open.total()

    @contextlib.contextmanager
    def place(self, address: str) -> Iterator[bool]:
        """Whether a stream from ``address`` may open; its place is held until the block ends."""
        source = source_of(address)
        if self.open >= self.total or self._open[source] >= self.per_address:
            yield False
            return
        self._open[source] += 1
        try:
            yield True
        finally:
            self._open[source] -= 1
            if not self._open[source]:
                del self._open[source]


def event(name: str, data: str) -> bytes:
    lines = "".join(f"data: {line}\n" for line in data.splitlines() or [""])
    return f"event: {name}\n{lines}\n".encode()


async def status_events(
    cache: StatusCache,
    streams: Streams,
    address: str,
    render: Callable[[Status], str],
    *,
    heartbeat: float = HEARTBEAT,
    lifetime: float = LIFETIME,
) -> AsyncGenerator[bytes]:
    """One viewer's stream: the status now, then each change, until ``lifetime`` is up or the
    reader goes away (the ASGI handler then closes this generator)."""
    with streams.place(address) as admitted:
        if not admitted:
            yield f"retry: {BUSY_RETRY_MS}\n".encode() + event("busy", "")
            return
        async with cache.viewer():
            yield f"retry: {RETRY_MS}\n".encode()
            status = await cache.get()
            version = cache.version
            yield event("status", render(status))
            loop_time = asyncio.get_running_loop().time
            deadline = loop_time() + lifetime
            while (left := deadline - loop_time()) > 0:
                if await cache.wait_for_change(version, min(heartbeat, left)):
                    version = cache.version
                    yield event("status", render(cache.status))
                else:
                    yield b": heartbeat\n\n"


# --- the process's one cache and its streams ------------------------------------------------

_cache: StatusCache | None = None
_streams: Streams | None = None


def status_cache() -> StatusCache:
    global _cache  # noqa: PLW0603
    if _cache is None:
        channel = ServiceChannel(settings.OPENCS_SERVICE_SOCKET)
        _cache = StatusCache(lambda: channel.request("snapshot"))
    return _cache


def streams() -> Streams:
    global _streams  # noqa: PLW0603
    if _streams is None:
        _streams = Streams(settings.OPENCS_MAX_STREAMS, settings.OPENCS_MAX_STREAMS_PER_ADDRESS)
    return _streams


def reset() -> None:
    """Forget the cache and the stream counts (tests, after changing the settings)."""
    global _cache, _streams  # noqa: PLW0603
    _cache = None
    _streams = None
