"""``opencs --web``: the web site (the ``opencs-web`` package) started as a child process of the
game server, for trying OpenCS on one machine with one command.

The site stays a process of its own, as in production (ADR 12): it talks to the game only over
the service channel, so a slow page can never hold up a game. The server only starts it once it
is listening, passes it the socket and the recordings directory, and stops it when it stops. The
server never imports the web package: the site is found by name and run with this Python.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace

from opencs.config import ConfigError, ServerConfig, ServiceConfig

log = logging.getLogger("opencs.app")

WEB_PACKAGE = "opencs_web"
DEFAULT_WEB_PORT = 8000
WEB_HOST = "127.0.0.1"
#: Where under the data directory ``--web`` puts what the config file leaves unset.
SERVICE_SOCKET_NAME = "service.sock"
RECORDINGS_DIR_NAME = "recordings"
WEB_STATE_DIR_NAME = "web"
ENV_PREFIX = "OPENCS_WEB_"
#: The longest Unix socket path every supported system takes (macOS: 103 bytes; Linux: 107).
MAX_SOCKET_PATH = 103
#: How long the site has to stop after SIGTERM before it is killed.
STOP_SECONDS = 10.0


def web_installed() -> bool:
    """Whether the web package is importable by this Python (``uv sync`` installs it)."""
    return importlib.util.find_spec(WEB_PACKAGE) is not None


def with_web_defaults(server: ServerConfig) -> ServerConfig:
    """``server`` with the service channel and recording turned on in the data directory, where
    the config file leaves them unset. What the file sets is kept, ``when = "off"`` included."""
    service = server.service
    if service.socket is None:
        socket = server.data_dir / SERVICE_SOCKET_NAME
        if len(os.fsencode(socket)) > MAX_SOCKET_PATH:
            raise ConfigError(
                f"--web: {socket} is too long for a Unix socket's path; "
                "set [service] socket to a shorter one"
            )
        service = ServiceConfig(socket)
    recording = server.recording
    if recording.directory is None:
        recording = replace(recording, directory=server.data_dir / RECORDINGS_DIR_NAME)
    return replace(server, service=service, recording=recording)


def web_command(
    server: ServerConfig, port: int, environ: Mapping[str, str]
) -> tuple[list[str], dict[str, str]]:
    """The site's command line and environment. ``server`` has been through
    ``with_web_defaults``. The ``[web]`` table becomes ``OPENCS_WEB_<NAME>`` variables, except
    where the environment already has one: the environment wins, as it does for the server."""
    assert server.service.socket is not None
    argv = [
        sys.executable,
        "-m",
        WEB_PACKAGE,
        "local",
        "--host",
        WEB_HOST,
        "--port",
        str(port),
        "--state-dir",
        str(server.data_dir / WEB_STATE_DIR_NAME),
        "--service-socket",
        str(server.service.socket),
    ]
    if server.recording.enabled and server.recording.directory is not None:
        argv += ["--recordings", str(server.recording.directory)]
    env = dict(environ)
    for name, value in server.web:
        env.setdefault(ENV_PREFIX + name.upper(), value)
    return argv, env


class WebSite:
    """The site's process: started once the game listens, stopped when it stops. If it dies on
    its own (its port is taken, a bad ``[web]`` value), that is logged and the game goes on."""

    def __init__(self, argv: Sequence[str], env: Mapping[str, str], port: int) -> None:
        self._argv = list(argv)
        self._env = dict(env)
        self._port = port
        self._proc: asyncio.subprocess.Process | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(*self._argv, env=self._env)
        except OSError as exc:
            log.error("cannot start the web site: %s", exc.strerror or exc)
            return
        log.info("web site starting at http://%s:%d/", WEB_HOST, self._port)
        self._watcher = asyncio.create_task(self._watch(self._proc))

    async def _watch(self, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        if self._stopping or code in (0, -signal.SIGINT, -signal.SIGTERM):
            log.info("web site stopped")
        else:
            log.error("web site stopped (exit code %d); the game goes on without it", code)

    async def stop(self) -> None:
        self._stopping = True
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), STOP_SECONDS)
            except TimeoutError:
                log.warning("web site did not stop in %.0f s: killed", STOP_SECONDS)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        if self._watcher is not None:
            await self._watcher
