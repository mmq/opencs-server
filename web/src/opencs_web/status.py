"""The game's public status, and the cache that keeps the game server's work for it to one
``snapshot`` a second however many people look.

:func:`public_status` keeps from a snapshot only what the public pages show: the account count,
the uptime, each city's letter, name, capacity and pilots (callsign, team, guest or not) and the
held-game count. :class:`StatusCache` asks the game server for a snapshot about once a second
while someone watches the live status (a *viewer*), and otherwise only when a page needs one and
the last is over a second old. Django under ASGI has no startup or shutdown hooks, so the update
task starts with the first viewer and stops with the last: with nobody looking, the game server
is asked nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from opencs_web.channel import ChannelError

log = logging.getLogger("opencs_web.status")

#: How old a snapshot may be before it is fetched again, and how often viewers are updated.
INTERVAL = 1.0
#: The team order of the game (``opencs.policy.TEAM_NAMES``).
TEAMS = ("RED", "BLUE", "GREEN", "BLACK")


@dataclass(frozen=True)
class Pilot:
    callsign: str
    guest: bool


@dataclass(frozen=True)
class Team:
    name: str
    pilots: tuple[Pilot, ...]


@dataclass(frozen=True)
class City:
    letter: str
    name: str
    capacity: int
    teams: tuple[Team, ...]

    @property
    def pilot_count(self) -> int:
        return sum(len(t.pilots) for t in self.teams)


@dataclass(frozen=True)
class Status:
    """What the public pages show. Equal statuses look the same, so the uptime is in minutes."""

    online: bool
    accounts: int = 0
    uptime_minutes: int = 0
    cities: tuple[City, ...] = ()
    held: int = 0

    @property
    def pilot_count(self) -> int:
        return sum(c.pilot_count for c in self.cities)

    @property
    def uptime(self) -> str:
        days, rest = divmod(self.uptime_minutes, 24 * 60)
        hours, minutes = divmod(rest, 60)
        parts = [(days, "day"), (hours, "hour"), (minutes, "minute")]
        shown = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in parts if n][:2]
        return ", ".join(shown) or "under a minute"


OFFLINE = Status(online=False)


def public_status(snapshot: object) -> Status:
    """The public part of a ``snapshot`` result. Raises ValueError if it is not one."""
    if not isinstance(snapshot, dict):
        raise ValueError("a snapshot is an object")
    accounts = snapshot.get("accounts")
    cities = snapshot.get("cities")
    if not isinstance(accounts, dict) or not isinstance(cities, list):
        raise ValueError("a snapshot has accounts and cities")
    uptime = snapshot.get("uptime")
    if not isinstance(uptime, int | float) or isinstance(uptime, bool) or uptime < 0:
        raise ValueError("uptime must be a number")
    return Status(
        online=True,
        accounts=_whole(accounts.get("count")),
        uptime_minutes=int(uptime // 60),
        cities=tuple(_city(c) for c in cities),
        held=_whole(snapshot.get("held")),
    )


def _city(city: object) -> City:
    if not isinstance(city, dict) or not isinstance(city.get("players"), list):
        raise ValueError("a city is an object with players")
    by_team: dict[str, list[Pilot]] = {}
    for p in city["players"]:
        if not isinstance(p, dict):
            raise ValueError("a pilot is an object")
        callsign, team, guest = p.get("callsign"), p.get("team"), p.get("guest")
        if not isinstance(callsign, str) or not isinstance(guest, bool) or team not in TEAMS:
            raise ValueError("a pilot has a callsign, a team and whether it is a guest")
        by_team.setdefault(team, []).append(Pilot(callsign, guest))
    return City(
        letter=_text(city.get("letter")),
        name=_text(city.get("name")),
        capacity=_whole(city.get("capacity")),
        teams=tuple(Team(t, tuple(by_team[t])) for t in TEAMS if t in by_team),
    )


def _whole(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("a count must be a whole number")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("a name must be text")
    return value


@dataclass
class _LoopState:
    """What belongs to one event loop: under uvicorn there is only one."""

    loop: asyncio.AbstractEventLoop
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    inflight: asyncio.Future[Status] | None = None
    updater: asyncio.Task[None] | None = None
    viewers: int = 0


class StatusCache:
    """The last status, shared by every page and viewer. ``fetch`` asks the game server for a
    snapshot (``ServiceChannel.request("snapshot")``)."""

    def __init__(
        self,
        fetch: Callable[[], Awaitable[object]],
        *,
        interval: float = INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._interval = interval
        self._clock = clock
        self._state: _LoopState | None = None
        self._status: Status | None = None
        self._fetched_at = 0.0
        #: Goes up each time the status changes, for a viewer to tell what it has seen.
        self.version = 0
        #: Snapshots asked for, for tests and the load check.
        self.fetches = 0

    @property
    def status(self) -> Status:
        return self._status or OFFLINE

    @property
    def viewers(self) -> int:
        return self._state.viewers if self._state else 0

    @property
    def updating(self) -> bool:
        state = self._state
        return state is not None and state.updater is not None and not state.updater.done()

    async def get(self) -> Status:
        """The status for a page: the cached one while it is fresh or viewers keep it so, else a
        new one (one fetch, however many pages ask at once)."""
        state = self._bind()
        if self._status is not None and (
            state.updater is not None or self._clock() - self._fetched_at < self._interval
        ):
            return self._status
        return await self._refresh(state)

    async def wait_for_change(self, version: int, timeout: float) -> bool:
        """Wait until the status is newer than ``version``; False if ``timeout`` passed first."""
        state = self._bind()
        if self.version != version:
            return True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(state.changed.wait(), timeout)
            return True
        return False

    @contextlib.asynccontextmanager
    async def viewer(self) -> AsyncIterator[None]:
        """While held, the status is fetched about once a second."""
        state = self._bind()
        state.viewers += 1
        if state.updater is None:
            state.updater = asyncio.create_task(self._update(state), name="status updates")
            log.info("status updates started")
        try:
            yield
        finally:
            state.viewers -= 1
            if state.viewers == 0 and state.updater is not None:
                state.updater.cancel()
                state.updater = None
                log.info("status updates stopped: no viewers")

    # --- inside ------------------------------------------------------------------------------

    def _bind(self) -> _LoopState:
        loop = asyncio.get_running_loop()
        if self._state is None or self._state.loop is not loop:
            self._state = _LoopState(loop)
        return self._state

    async def _update(self, state: _LoopState) -> None:
        while True:
            if self._status is None or self._clock() - self._fetched_at >= self._interval:
                await self._refresh(state)
            wait = self._interval - (self._clock() - self._fetched_at)
            await asyncio.sleep(max(wait, self._interval / 10))

    async def _refresh(self, state: _LoopState) -> Status:
        if state.inflight is None:
            state.inflight = asyncio.ensure_future(self._fetch_once(state))
        # Shielded: a page whose reader went away, or the last viewer leaving, does not cancel a
        # request the game server is already answering (the channel would have to reconnect).
        return await asyncio.shield(state.inflight)

    async def _fetch_once(self, state: _LoopState) -> Status:
        self.fetches += 1
        try:
            status = public_status(await self._fetch())
        except ChannelError:
            status = OFFLINE  # the channel has logged why
        except ValueError as e:
            log.warning("the game server's snapshot was not understood: %s", e)
            status = OFFLINE
        except Exception:
            log.exception("the status could not be fetched")
            status = OFFLINE
        finally:
            state.inflight = None
        self._fetched_at = self._clock()
        if status != self._status:
            self._status = status
            self.version += 1
            changed, state.changed = state.changed, asyncio.Event()
            changed.set()
        return status
