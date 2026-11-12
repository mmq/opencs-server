"""The service channel: JSON lines over a Unix socket, for the web process (ADR 12).

It sits beside ``admin.py`` and reaches the server the same way, through ``Control`` and
``Identity``, so the web front end can do nothing an operator cannot and never touches the
database (ADR 10). Who may connect is decided by the socket file's owner and mode
(``policy.SERVICE_SOCKET_MODE``): the web process runs as another user in the server's group.

A connection starts with a hello naming a protocol version (``policy.SERVICE_PROTOCOL_VERSIONS``)
within ``SERVICE_HELLO_TIMEOUT``. After that each line is a request, ``{"id": ..., "op": ...}``
plus the op's arguments, answered in order by one line, ``{"id": ..., "ok": true, "result": ...}``
or ``{"id": ..., "ok": false, "error": <code>, "message": <text>}``. Input is bounded like any
other: a line over ``SERVICE_MAX_LINE`` bytes closes the connection, requests over
``SERVICE_REQUEST_LIMIT`` are refused, and so are connections past ``SERVICE_MAX_CONNECTIONS``.
The requests that hash a password are also metered over all connections
(``SERVICE_HASH_LIMIT``), and their hashes run on the hasher, off the loop. A request whose work on
the loop is slow is logged. A password made here is in its reply only, never in the log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencs import __version__, policy
from opencs.clock import Clock
from opencs.connlog import STREAM_CHAMBER, STREAM_GAME
from opencs.control import Control, hash_off_loop, parse_team
from opencs.lobby.identity import NewPassword, Refused
from opencs.ratelimit import TokenBucket
from opencs.session.phases import Services

log = logging.getLogger("opencs.service")

HELLO = "opencs-service"

#: Ops that hash a password, metered over every connection (``policy.SERVICE_HASH_LIMIT``).
_HASHING_OPS = frozenset({"register", "reset-password"})

type _Handler = Callable[[dict[str, Any]], Any]


class RequestError(Exception):
    """A request that cannot be served: ``code`` for the program, the message for a person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ServiceServer:
    """The listener and its connections."""

    def __init__(
        self,
        services: Services,
        control: Control,
        clock: Clock,
        *,
        hello_timeout: float = policy.SERVICE_HELLO_TIMEOUT,
        max_connections: int = policy.SERVICE_MAX_CONNECTIONS,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._services = services
        self._control = control
        self._clock = clock
        self._hello_timeout = hello_timeout
        self._max = max_connections
        self._timer = timer  # real time, for the slow-request warning, whatever ``clock`` is
        self._hashes = TokenBucket(*policy.SERVICE_HASH_LIMIT, clock.now())
        self._server: asyncio.Server | None = None
        self._path: Path | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._slow_unlogged = 0
        self._next_slow_warning = 0.0
        #: Each op with the arguments it takes; any other is refused.
        self._ops: dict[str, tuple[tuple[str, ...], _Handler]] = {
            "snapshot": ((), self._snapshot),
            "account": (("name",), self._account),
            "register": (
                ("name", "password", "callsign"),
                lambda r: self._password("register", r),
            ),
            "reset-password": (("name", "password"), lambda r: self._password("reset-password", r)),
            "status": ((), self._status),
            "announce": (("text",), lambda r: self._admin(f"announce {_text(r, 'text')}")),
            "kick": (("callsign", "reason"), self._kick),
            "guppy-home": (("team",), lambda r: self._admin(f"guppy {_team(r)} home")),
            "shutdown": (("seconds", "cancel"), self._shutdown),
            "reload": ((), lambda _: self._admin("reload")),
            "reset-progress": (("name",), lambda r: self._admin(f"reset-progress {_name(r)}")),
            "delete-account": (("name",), lambda r: self._admin(f"delete-account {_name(r)}")),
            "set-callsign": (("name", "callsign", "force"), self._set_callsign),
        }

    async def start(self, path: Path) -> None:
        """Listen on the socket at ``path``, replacing a socket left by a server that died; any
        other file there is an error (``OSError``), not something to delete."""
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("the service channel needs Unix sockets, which this platform lacks")
        with contextlib.suppress(FileNotFoundError):
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise FileExistsError(f"{path} exists and is not a socket")
            path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle, path, limit=policy.SERVICE_MAX_LINE
        )
        self._path = path
        os.chmod(path, policy.SERVICE_SOCKET_MODE)
        log.info("service channel on %s", path)

    async def stop(self) -> None:
        """Stop listening, close every connection and remove the socket file."""
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        if self._server is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), policy.SHUTDOWN_ABORT_GRACE)
        if self._path is not None:
            with contextlib.suppress(OSError):
                self._path.unlink()

    # --- one connection ----------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._writers) >= self._max:
            log.warning("service connection refused: %d already open", len(self._writers))
            writer.close()
            return
        self._writers.add(writer)
        try:
            if await self._hello(reader, writer):
                await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            log.info("service connection ended")
        except Exception:
            log.exception("service connection failed")
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _hello(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        try:
            line = await asyncio.wait_for(reader.readuntil(b"\n"), self._hello_timeout)
        except TimeoutError:
            log.warning("service connection sent no hello in time")
            return False
        hello = _parse(line)
        version = hello.get("version") if isinstance(hello, dict) else None
        if not isinstance(hello, dict) or hello.get("hello") != HELLO:
            await _send(writer, {"ok": False, "error": "hello", "message": "not a hello"})
            return False
        if version not in policy.SERVICE_PROTOCOL_VERSIONS:
            await _send(
                writer,
                {
                    "ok": False,
                    "error": "version",
                    "message": f"protocol version {version!r} is not spoken here",
                    "versions": list(policy.SERVICE_PROTOCOL_VERSIONS),
                },
            )
            return False
        await _send(writer, {"hello": HELLO, "version": version, "server": __version__})
        log.info("service connection opened (protocol %s)", version)
        return True

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests = TokenBucket(*policy.SERVICE_REQUEST_LIMIT, self._clock.now())
        while line := await _read_line(reader):
            request = _parse(line)
            rid = request.get("id") if isinstance(request, dict) else None
            try:
                if not isinstance(request, dict) or not isinstance(request.get("op"), str):
                    raise RequestError("bad-request", "a request is an object with an op")
                if not requests.take(self._clock.now()):
                    raise RequestError("rate", "too many requests: slow down")
                result = await self._request(request)
                reply: dict[str, Any] = {"id": rid, "ok": True, "result": result}
            except RequestError as exc:
                reply = {"id": rid, "ok": False, "error": exc.code, "message": str(exc)}
            except Exception:
                log.exception("service request %s failed", _op_name(request))
                reply = {"id": rid, "ok": False, "error": "failed", "message": "see the log"}
            await _send(writer, reply)

    async def _request(self, request: dict[str, Any]) -> Any:
        op = request["op"]
        if op not in self._ops:
            raise RequestError("unknown-op", f"no op {_op_name(request)!r}")
        keys, handler = self._ops[op]
        _only(request, *keys)
        if op in _HASHING_OPS:  # its wait for the hash is not time on the loop
            return await handler(request)
        started = self._timer()
        try:
            result = handler(request)
            if asyncio.iscoroutine(result):
                result = await result
        finally:
            self._timed(op, self._timer() - started)
        return result

    def _timed(self, op: str, took: float) -> None:
        """Log a request that held up the loop, at most once per ``SLOW_SERVICE_LOG_SECONDS``."""
        if took < policy.SLOW_SERVICE_REQUEST_SECONDS:
            return
        self._slow_unlogged += 1
        now = self._timer()
        if now < self._next_slow_warning:
            return
        log.warning(
            "service request %s held up the server for %.0f ms (%d slow since the last warning)",
            op,
            took * 1000,
            self._slow_unlogged,
        )
        self._slow_unlogged = 0
        self._next_slow_warning = now + policy.SLOW_SERVICE_LOG_SECONDS

    # --- requests ----------------------------------------------------------------------------

    def _snapshot(self, _: dict[str, Any]) -> dict[str, Any]:
        """What is happening, for the web's status pages: no addresses, no account details beyond
        the count, no chat."""
        services = self._services
        identity = services.identity
        streams = [s.log.context.stream for s in services.sessions.all()]
        cities = []
        for letter, city in sorted(services.cities.items()):
            players = [
                {
                    "callsign": m.callsign,
                    "team": policy.TEAM_NAMES[team],
                    "guest": m.account is None,
                }
                for team in range(policy.TEAM_COUNT)
                for m in services.lobby.team_members(letter, team)
            ]
            cities.append(
                {
                    "letter": letter,
                    "name": city.config.name,
                    "capacity": city.config.capacity,
                    "open": services.lobby.city(letter) is not None,
                    "players": players,
                }
            )
        return {
            "server": __version__,
            "uptime": round(self._control.uptime(), 1),
            "accounts": {
                "count": identity.account_count(),
                "max": identity.max_accounts,
                "registration": "open" if identity.registration_open else "closed",
            },
            "connections": {
                "total": len(streams),
                "game": streams.count(STREAM_GAME),
                "chamber": streams.count(STREAM_CHAMBER),
            },
            "cities": cities,
            "held": len(services.held.live()),
        }

    async def _status(self, _: dict[str, Any]) -> dict[str, Any]:
        """The operator's ``status`` lines, and for the web's admin pages each city's health as
        numbers: its failures and rebuilds, slow calls, and odd hit reports (hits with no shot
        on record, hits from too far away; applied all the same, a detection signal). Admin-only,
        like every op here but ``snapshot``: still no addresses."""
        services = self._services
        health = services.city_health
        cities = [
            {
                "letter": letter,
                "name": city.config.name,
                "players": services.lobby.count(letter),
                "failures": health.failures(letter),
                "rebuilds": health.rebuilds(letter),
                "slow_calls": health.slow_calls(letter),
                "worst_call_ms": round(health.worst_call(letter) * 1000),
                "hits_without_a_shot": city.hits_without_a_shot,
                "distant_hits": city.distant_hits,
            }
            for letter, city in sorted(services.cities.items())
        ]
        return {"lines": await self._control.run("status"), "cities": cities}

    def _account(self, request: dict[str, Any]) -> dict[str, Any]:
        name = _name(request)
        identity = self._services.identity
        times = identity.account_times(name)
        return {
            "name": name,
            "exists": times is not None,
            "online": identity.account_online(name),
            "callsign": identity.callsign_of(name),
            # When it was made: the web tells its account from a later one of the same name.
            "created": None if times is None else times.created,
            "last_login": None if times is None else times.last_login,
        }

    def _set_callsign(self, request: dict[str, Any]) -> dict[str, Any]:
        """A callsign for an offline account, in the case given, under the rules a player's
        ``/CALLSIGN`` meets; ``force`` (the web's admin pages) skips the wait between changes."""
        name = _name(request)
        callsign = _callsign(request)
        force = request.get("force", False)
        if not isinstance(force, bool):
            raise RequestError("bad-request", "force must be true or false")
        result = self._control.change_callsign(name, callsign, force=force)
        if isinstance(result, Refused):
            raise RequestError("refused", result.reason)
        log.info("callsign of %s set to %s over the service channel", name, result)
        return {"name": name, "callsign": result}

    async def _password(self, op: str, request: dict[str, Any]) -> dict[str, Any]:
        """``register`` or ``reset-password``: the password the web chose, or a made-up one,
        hashed off the loop and returned once. Whether players may choose is the web's setting;
        the server only holds a chosen one to the rules for a new password. Refusals cost no hash
        and no allowance."""
        name = _name(request)
        chosen = request.get("password")
        if chosen is not None and not isinstance(chosen, str):
            raise RequestError("bad-request", "password must be text")
        callsign = _callsign(request) if "callsign" in request else None  # register only
        identity = self._services.identity
        begun: NewPassword | Refused = (
            identity.begin_register(name, chosen, callsign)
            if op == "register"
            else identity.begin_reset_password(name, chosen)
        )
        if isinstance(begun, Refused):
            raise RequestError("refused", begun.reason)
        if not self._hashes.take(self._clock.now()):
            raise RequestError("rate", "too many new passwords: try again later")
        try:
            digest = await hash_off_loop(self._services.hasher, begun)
        except TimeoutError:
            log.warning("a password hash for %s did not finish in time", begun.name)
            raise RequestError("busy", "the password hash did not finish in time") from None
        done = identity.complete_password(begun, digest)
        if isinstance(done, Refused):
            raise RequestError("refused", done.reason)
        account, password = done
        if op == "register":
            log.info("account %s created over the service channel", account.name)
            # None: the player is asked for a callsign at the first login.
            times = identity.account_times(account.name)
            return {
                "name": account.name,
                "password": password,
                "callsign": callsign,
                "created": None if times is None else times.created,
            }
        # As the operator's reset does: the guesses that locked the owner out are forgotten.
        self._services.login_limits.forget_account(account.name)
        log.info("password of %s reset over the service channel", account.name)
        return {"name": account.name, "password": password}

    async def _admin(self, line: str) -> dict[str, Any]:
        """An operator's command, built from checked arguments: its reply lines."""
        return {"lines": await self._control.run(line)}

    async def _kick(self, request: dict[str, Any]) -> dict[str, Any]:
        callsign = _word(request, "callsign")
        reason = request.get("reason", "")
        if not isinstance(reason, str) or not _printable(reason):
            raise RequestError("bad-request", "reason must be printable text")
        return await self._admin(f"kick {callsign} {reason}".rstrip())

    async def _shutdown(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("cancel") is True:
            return await self._admin("shutdown cancel")
        seconds = request.get("seconds", 0)
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
            raise RequestError("bad-request", "seconds must be a whole number, 0 or more")
        return await self._admin(f"shutdown {seconds}")


# --- arguments ----------------------------------------------------------------------------------


def _printable(text: str) -> bool:
    return all(" " <= ch <= "~" for ch in text)


def _word(request: dict[str, Any], key: str) -> str:
    """A one-word argument: a name or a callsign, which a command line could not tell apart from
    the words after it if it held a space."""
    value = request.get(key)
    if not isinstance(value, str) or not value or not _printable(value) or " " in value:
        raise RequestError("bad-request", f"{key} must be one word of printable text")
    if len(value) > policy.MAX_ACCOUNT_NAME_LENGTH:
        raise RequestError("bad-request", f"{key} is too long")
    return value.upper()


def _name(request: dict[str, Any]) -> str:
    return _word(request, "name")


def _callsign(request: dict[str, Any]) -> str:
    """A callsign in the case given: ``Identity`` holds it to the rules and says why not."""
    value = request.get("callsign")
    if not isinstance(value, str) or not value or not _printable(value) or " " in value:
        raise RequestError("bad-request", "callsign must be one word of printable text")
    if len(value) > policy.MAX_ACCOUNT_NAME_LENGTH:
        raise RequestError("bad-request", "callsign is too long")
    return value


def _text(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip() or not _printable(value):
        raise RequestError("bad-request", f"{key} must be printable text")
    return value


def _team(request: dict[str, Any]) -> str:
    value = request.get("team")
    team = parse_team(value) if isinstance(value, str) else None
    if team is None:
        raise RequestError("bad-request", f"team must be one of {', '.join(policy.TEAM_NAMES)}")
    return policy.TEAM_NAMES[team]


def _only(request: dict[str, Any], *keys: str) -> None:
    extra = set(request) - {"id", "op", *keys}
    if extra:
        raise RequestError("bad-request", f"unexpected {', '.join(sorted(extra))}")


def _op_name(request: object) -> str:
    """The op of a request, for the log, only if it is one this channel knows."""
    op = request.get("op") if isinstance(request, dict) else None
    return op if isinstance(op, str) and op.isascii() and op.replace("-", "").isalpha() else "?"


# --- the wire -----------------------------------------------------------------------------------


def _parse(line: bytes) -> object:
    try:
        return json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None


async def _read_line(reader: asyncio.StreamReader) -> bytes | None:
    """One line, None at end of stream. A line over the limit raises ``LimitOverrunError`` (the
    connection is closed)."""
    try:
        return await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError:
        return None


async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(json.dumps(message, separators=(",", ":")).encode("ascii") + b"\n")
    with contextlib.suppress(ConnectionError):
        await writer.drain()
