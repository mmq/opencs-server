"""A client for the game server's service channel (ADR 12, ``opencs.service``): JSON lines over a
Unix socket.

One connection, opened on the first request and kept: requests go one at a time, each answered in
order. A request never waits longer than ``timeout`` in all, connecting included; past that, or if
the server cannot be reached, refuses the hello or closes the connection, it raises
:class:`UnavailableError` and the page says the game is offline. After a failure the channel
does not try again before a backoff (``BACKOFF_FIRST`` doubling up to ``BACKOFF_MAX``) has passed,
so a server that is down costs a refused request, not a connection attempt per page.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("opencs_web.channel")

#: The hello and the protocol version this client speaks (``opencs.service.HELLO``,
#: ``policy.SERVICE_PROTOCOL_VERSIONS``).
HELLO = "opencs-service"
PROTOCOL_VERSION = 1
#: The longest a request waits, connecting and the hello included.
TIMEOUT = 2.0
BACKOFF_FIRST = 0.5
BACKOFF_MAX = 10.0
#: The longest reply line read. A snapshot of 26 full cities is about 100 kB.
MAX_REPLY = 1 << 20


class ChannelError(Exception):
    """A request that got no result."""


class UnavailableError(ChannelError):
    """The game server could not be reached, or did not answer in time."""


class RefusedError(ChannelError):
    """The server answered the request with a refusal: ``code`` for the program."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _BrokenError(Exception):
    """The connection ended or went out of step."""


class ServiceChannel:
    """The web process's connection to the service channel at ``path`` (None: not configured,
    every request is unavailable)."""

    def __init__(
        self,
        path: Path | None,
        *,
        timeout: float = TIMEOUT,
        backoff_first: float = BACKOFF_FIRST,
        backoff_max: float = BACKOFF_MAX,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._timeout = timeout
        self._backoff_first = backoff_first
        self._backoff_max = backoff_max
        self._clock = clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 0
        self._failures = 0
        self._retry_at = 0.0
        self._problem = "not connected yet"
        #: The game server's version, from its hello.
        self.server_version: str | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def request(self, op: str, **args: Any) -> Any:
        """The result of one request. Raises :class:`RefusedError` if the server refused it and
        :class:`UnavailableError` if there was no answer."""
        self._bind()
        if self._path is None:
            raise UnavailableError("no service socket is configured")
        try:
            async with asyncio.timeout(self._timeout):
                async with self._lock:
                    try:
                        return await self._request(op, args)
                    except ChannelError:
                        raise
                    except BaseException:
                        # Cancelled or timed out mid-exchange: its reply may still come, so the
                        # connection is out of step.
                        self._drop()
                        raise
        except TimeoutError:
            self._failed("the game server did not answer in time")
            raise UnavailableError(self._problem) from None

    async def close(self) -> None:
        writer = self._writer
        self._drop()
        if writer is not None:
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # --- inside the lock ---------------------------------------------------------------------

    async def _request(self, op: str, args: dict[str, Any]) -> Any:
        if self._writer is not None:
            try:
                return await self._exchange(op, args)
            except _BrokenError:
                # A connection kept from earlier, ended by a restart of the game server: one fresh
                # try at once, without the backoff.
                self._drop()
        await self._connect()
        try:
            return await self._exchange(op, args)
        except _BrokenError as e:
            self._drop()
            self._failed(str(e))
            raise UnavailableError(self._problem) from None

    async def _connect(self) -> None:
        if self._clock() < self._retry_at:
            raise UnavailableError(self._problem)
        assert self._path is not None
        try:
            reader, writer = await asyncio.open_unix_connection(self._path, limit=MAX_REPLY)
        except FileNotFoundError:
            self._failed(f"no service socket at {self._path}")
            raise UnavailableError(self._problem) from None
        except OSError as e:
            self._failed(f"cannot connect to {self._path}: {e.strerror or e}")
            raise UnavailableError(self._problem) from None
        self._reader, self._writer = reader, writer
        try:
            hello = await self._send({"hello": HELLO, "version": PROTOCOL_VERSION})
        except _BrokenError as e:
            self._drop()
            self._failed(f"the hello failed: {e}")
            raise UnavailableError(self._problem) from None
        if hello.get("hello") != HELLO or hello.get("version") != PROTOCOL_VERSION:
            self._drop()
            message = hello.get("message", "not a hello")
            self._failed(f"the game server refused the hello: {message}")
            raise UnavailableError(self._problem)
        server = hello.get("server")
        self.server_version = server if isinstance(server, str) else None
        if self._failures:
            log.info("service channel connected again (game server %s)", self.server_version)
        self._failures = 0
        self._retry_at = 0.0

    async def _exchange(self, op: str, args: dict[str, Any]) -> Any:
        self._next_id += 1
        rid = self._next_id
        reply = await self._send({"id": rid, "op": op, **args})
        if reply.get("id") != rid:
            raise _BrokenError("a reply out of step")
        if reply.get("ok") is True:
            return reply.get("result")
        code = reply.get("error")
        message = reply.get("message")
        raise RefusedError(
            code if isinstance(code, str) else "?",
            message if isinstance(message, str) else "refused",
        )

    async def _send(self, message: dict[str, Any]) -> dict[str, Any]:
        """Write one line and read the one that answers it."""
        reader, writer = self._reader, self._writer
        assert reader is not None and writer is not None
        try:
            writer.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
            line = await reader.readuntil(b"\n")
        except (asyncio.IncompleteReadError, ConnectionResetError):
            # Linux resets, rather than ends, a stream closed with what was sent to it unread.
            raise _BrokenError("the game server closed the connection") from None
        except asyncio.LimitOverrunError:
            raise _BrokenError("a reply too long") from None
        except OSError as e:
            raise _BrokenError(f"the connection failed: {e.strerror or e}") from None
        try:
            reply = json.loads(line)
        except ValueError:
            raise _BrokenError("a reply that is not JSON") from None
        if not isinstance(reply, dict):
            raise _BrokenError("a reply that is not an object")
        return reply

    # --- state -------------------------------------------------------------------------------

    def _bind(self) -> None:
        """Asyncio objects belong to one event loop. Under uvicorn there is only one; anything
        that runs each request in a loop of its own (Django's test client) gets a fresh start."""
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._drop()
            self._loop = loop
            self._lock = asyncio.Lock()

    def _drop(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()

    def _failed(self, problem: str) -> None:
        if self._failures == 0 or problem != self._problem:
            log.warning("service channel unavailable: %s", problem)
        self._problem = problem
        self._failures = min(self._failures + 1, 32)
        delay = min(self._backoff_max, self._backoff_first * 2 ** (self._failures - 1))
        self._retry_at = self._clock() + delay
