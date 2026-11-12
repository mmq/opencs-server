"""A stand-in for the game server's service channel, on a real Unix socket: the hello, then
``snapshot`` and refusals, with knobs for the ways the real one can fail."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SNAPSHOT: dict[str, Any] = {
    "server": "1.0.0",
    "uptime": 3725.4,
    "accounts": {"count": 12, "max": 1000, "registration": "closed"},
    "connections": {"total": 3, "game": 2, "chamber": 1},
    "cities": [
        {
            "letter": "B",
            "name": "BOSTON",
            "capacity": 16,
            "open": True,
            "players": [
                {"callsign": "ALPHA", "team": "RED", "guest": False},
                {"callsign": "BOT01", "team": "BLUE", "guest": True},
            ],
        }
    ],
    "held": 1,
}


@contextlib.contextmanager
def socket_dir() -> Iterator[Path]:
    """A directory with a short path: a Unix socket's path is limited to about 100 bytes, and
    pytest's temporary directories on macOS are longer."""
    path = Path(tempfile.mkdtemp(prefix="ocs", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeService:
    def __init__(self, path: Path, snapshot: dict[str, Any] | None = None) -> None:
        self.path = path
        self.snapshot = snapshot if snapshot is not None else SNAPSHOT
        self.connections = 0
        self.hellos: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        #: Knobs: answer the hello with this instead; never answer requests; refuse every one.
        self.hello_reply: dict[str, Any] | None = None
        self.silent = False
        self.refuse: str | None = None
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> FakeService:
        self._server = await asyncio.start_unix_server(self._handle, self.path)
        return self

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        for writer in list(self._writers):
            writer.close()
        await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.add(writer)
        try:
            hello = json.loads(await reader.readuntil(b"\n"))
            self.hellos.append(hello)
            reply = self.hello_reply or {"hello": "opencs-service", "version": 1, "server": "1.0.0"}
            await _send(writer, reply)
            if self.hello_reply is not None:
                return
            while line := await reader.readline():
                request = json.loads(line)
                self.requests.append(request)
                if self.silent:
                    continue
                if self.refuse:
                    await _send(
                        writer,
                        {"id": request["id"], "ok": False, "error": self.refuse, "message": "no"},
                    )
                elif request["op"] == "snapshot":
                    await _send(writer, {"id": request["id"], "ok": True, "result": self.snapshot})
                else:
                    await _send(
                        writer,
                        {"id": request["id"], "ok": False, "error": "unknown-op", "message": "?"},
                    )
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()


async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(json.dumps(message).encode() + b"\n")
    await writer.drain()
