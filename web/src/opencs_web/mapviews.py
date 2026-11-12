"""The maps' JSON and streams (ADR 16), for the map viewer: the cities and the times recorded, a
city's map, its track over a minute, and its track as it comes.

Every request has an *audience*: an administrator logged in to the admin pages, or the public. Each
has a delay (``OPENCS_WEB_MAP_DELAY_ADMIN``, ``..._PUBLIC``): nothing newer than now minus it is
ever sent, whatever is asked; the track is cut here, on the server. With a delay of 0 the track
comes from the game server's live feed, shared by every viewer of the city; otherwise from the
recordings (the archive, and the segments not yet archived). Chat is in an administrator's track
only, and only with ``OPENCS_WEB_MAP_ADMIN_CHAT``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_safe

from opencs import policy
from opencs.messages import limits
from opencs.net.listener import source_of
from opencs.ratelimit import KeyedBuckets
from opencs.wire.codec import CELL_SIZE, CITY_SIZE
from opencs_web import live, staff
from opencs_web.maps.live import PIECE_SECONDS, LiveHub
from opencs_web.maps.source import Recordings
from opencs_web.maps.track import FORMAT_VERSION, dumps
from opencs_web.maps.windows import WINDOW_MS, WindowCache, window_start

#: Requests for JSON per address (an IPv6 /64): a burst, then a rate a second. A viewer asks for a
#: window a minute while it plays, more while it seeks.
REQUESTS_PER_SOURCE = (60, 2.0)
#: How long the list of cities and their recorded spans is kept, in seconds.
CITIES_TTL = 5.0
#: How long a browser may keep a city's map.json, in seconds.
MAP_MAX_AGE = 300


@dataclass(frozen=True)
class Audience:
    delay_ms: int
    chat: bool
    admin: bool


async def audience(request: HttpRequest) -> Audience:
    """Who asks: an administrator (with a log-in to the admin pages) or the public."""
    if settings.OPENCS_ADMIN and await staff.current(request) is not None:
        return Audience(
            settings.OPENCS_MAP_DELAY_ADMIN * 1000, settings.OPENCS_MAP_ADMIN_CHAT, True
        )
    return Audience(settings.OPENCS_MAP_DELAY_PUBLIC * 1000, False, False)


def now_ms() -> int:
    return round(time.time() * 1000)


def limit_for(delay_ms: int) -> int:
    """The newest time an audience with this delay may see: now less the delay, to the second."""
    limit = now_ms() - delay_ms
    return limit - limit % 1000


# --- the process's shared state ------------------------------------------------------------------

_recordings: Recordings | None = None
_cache: WindowCache | None = None
_hub: LiveHub | None = None
_streams: live.Streams | None = None
_requests: KeyedBuckets[str] | None = None
_cities: tuple[float, dict[str, Any]] | None = None


def recordings() -> Recordings:
    global _recordings  # noqa: PLW0603
    if _recordings is None:
        _recordings = Recordings(settings.OPENCS_ARCHIVE, settings.OPENCS_RECORDINGS)
    return _recordings


def window_cache() -> WindowCache:
    global _cache  # noqa: PLW0603
    if _cache is None:
        _cache = WindowCache(recordings)
    return _cache


def hub() -> LiveHub:
    global _hub  # noqa: PLW0603
    if _hub is None:
        _hub = LiveHub(settings.OPENCS_SERVICE_SOCKET)
    return _hub


def map_streams() -> live.Streams:
    global _streams  # noqa: PLW0603
    if _streams is None:
        _streams = live.Streams(
            settings.OPENCS_MAP_MAX_STREAMS, settings.OPENCS_MAP_MAX_STREAMS_PER_ADDRESS
        )
    return _streams


def reset() -> None:
    """Forget the shared state (tests, after changing the settings)."""
    global _recordings, _cache, _hub, _streams, _requests, _cities  # noqa: PLW0603
    _recordings = _cache = _hub = _streams = _requests = _cities = None


def _allowed(request: HttpRequest) -> bool:
    global _requests  # noqa: PLW0603
    if _requests is None:
        _requests = KeyedBuckets(*REQUESTS_PER_SOURCE)
    return _requests.take(source_of(request.META.get("REMOTE_ADDR") or "?"), time.monotonic())


def _error(code: str, status: int, **more: Any) -> JsonResponse:
    return JsonResponse({"error": code, **more}, status=status)


def _json(data: bytes | dict[str, Any]) -> HttpResponse:
    body = data if isinstance(data, bytes) else dumps(data)
    response = HttpResponse(body, content_type="application/json")
    response["Cache-Control"] = "no-store"
    return response


def _letter(letter: str) -> str | None:
    letter = letter.upper()
    return letter if len(letter) == 1 and "A" <= letter <= "Z" else None


# --- the viewer's pages -------------------------------------------------------------------------


def delay_words(seconds: int) -> str:
    """A delay as a player reads it: "one hour", "1 hour and 30 minutes", "45 seconds"."""
    units = (("hour", 3600), ("minute", 60), ("second", 1))
    parts = []
    for name, size in units:
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"one {name}" if count == 1 else f"{count} {name}s")
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def viewer(delay: int, *, chat: bool, admin: bool) -> dict[str, Any]:
    """What a page with the map viewer gives the script (``maps.html``'s ``data-`` attributes):
    the endpoints, and the delay it plays at, the audience's, which the script never goes below."""
    return {
        "delay": delay,
        "delay_words": delay_words(delay),
        "chat": chat,
        "admin": admin,
        "base": reverse("map-cities").rsplit("/", 1)[0] + "/",
    }


@require_safe
async def page(request: HttpRequest) -> HttpResponse:
    """The public maps page: the viewer at the public's delay, never with chat (an administrator
    logged in has their own page, under the admin pages)."""
    context = {"viewer": viewer(settings.OPENCS_MAP_DELAY_PUBLIC, chat=False, admin=False)}
    response = render(request, "opencs_web/maps.html", context)
    response["Cache-Control"] = "no-cache"
    return response


# --- the cities ----------------------------------------------------------------------------------


def _recorded() -> dict[str, Any]:
    """Each recorded city's name and spans, read from the recordings (in a thread)."""
    source = recordings()
    out: dict[str, Any] = {}
    for city, spans in source.spans().items():
        header = source.latest_header(city)
        config = header.get("config") if header else None
        name = config.get("name") if isinstance(config, dict) else None
        out[city] = {"name": name, "spans": spans}
    return out


async def _recorded_cached() -> dict[str, Any]:
    global _cities  # noqa: PLW0603
    if _cities is None or time.monotonic() - _cities[0] > CITIES_TTL:
        _cities = (time.monotonic(), await asyncio.to_thread(_recorded))
    return _cities[1]


@require_safe
async def cities(request: HttpRequest) -> HttpResponse:
    """The cities with a map: each one's name and the spans recorded (UTC ms), cut at the
    audience's limit, and whether it can be watched as it is played (a delay of 0)."""
    if not _allowed(request):
        return _error("rate", 429)
    who = await audience(request)
    limit = limit_for(who.delay_ms)
    live_now = who.delay_ms == 0 and settings.OPENCS_SERVICE_SOCKET is not None
    found: dict[str, dict[str, Any]] = {}
    for city, held in (await _recorded_cached()).items():
        spans = [[s, min(e, limit)] for s, e in held["spans"] if s < limit]
        found[city] = {"city": city, "name": held["name"], "spans": spans, "live": False}
    if live_now:
        status = await live.status_cache().get()
        for c in status.cities:
            entry = found.setdefault(c.letter, {"city": c.letter, "name": c.name, "spans": []})
            entry["name"] = c.name
            entry["live"] = True
    return _json(
        {
            "v": FORMAT_VERSION,
            "now": now_ms(),
            "limit": limit,
            "delay": who.delay_ms // 1000,
            "cities": [found[c] for c in sorted(found)],
        }
    )


# --- a city's map --------------------------------------------------------------------------------


@require_safe
async def city_map(request: HttpRequest, letter: str) -> HttpResponse:
    """The static map: the city's size, its blocks' classes (``blocks``, rows of cell Z, or null
    for the client's default, all worth 1) and what each class is worth, the teams and their
    home corners. From the city's configuration as last recorded, or its live feed."""
    city = _letter(letter)
    if city is None:
        return _error("no-city", 404)
    if not _allowed(request):
        return _error("rate", 429)
    config: Any = None
    held = hub().cities.get(city)
    if held is not None:
        config = held.track.config
    if config is None:
        header = await asyncio.to_thread(recordings().latest_header, city)
        config = header.get("config") if header else None
    if not isinstance(config, dict):
        return _error("no-city", 404)
    response = _json(
        {
            "v": FORMAT_VERSION,
            "city": city,
            "name": config.get("name"),
            "size": CITY_SIZE,
            "block": CELL_SIZE,
            "blocks": config.get("blocks"),
            "values": list(limits.BLOCK_VALUES),
            "teams": list(policy.TEAM_NAMES),
            "homes": [list(cell) for cell in policy.TEAM_CORNER_CELLS],
        }
    )
    # A city's map changes only when its file does (and is reloaded): a viewer may keep it a while.
    response["Cache-Control"] = f"max-age={MAP_MAX_AGE}"
    return response


# --- a window of the track -----------------------------------------------------------------------


@require_safe
async def track(request: HttpRequest, letter: str) -> HttpResponse:
    """The minute holding ``t`` (UTC ms): the city at its start and what happened in it, cut at
    the audience's limit. A minute that begins at or after the limit is refused (403)."""
    city = _letter(letter)
    if city is None:
        return _error("no-city", 404)
    try:
        at = int(request.GET.get("t", ""))
    except ValueError:
        return _error("bad-time", 400)
    if not _allowed(request):
        return _error("rate", 429)
    who = await audience(request)
    limit = limit_for(who.delay_ms)
    start = window_start(at)
    if start >= limit or at < 0:
        return _error("too-recent", 403, limit=limit)
    cache = window_cache()
    upto = min(start + WINDOW_MS, limit)
    window = await cache.window(city, start, upto=upto)
    return _json(cache.encoded(window, upto, chat=who.chat))


# --- the track as it comes -----------------------------------------------------------------------


def _event(data: bytes) -> bytes:
    return b"event: track\ndata: " + data + b"\n\n"


async def track_events(
    city: str,
    delay_ms: int,
    chat: bool,
    address: str,
    *,
    heartbeat: float = live.HEARTBEAT,
    lifetime: float = live.LIFETIME,
) -> AsyncGenerator[bytes]:
    """One viewer's stream: ``track`` events, each a window (the first with the city's state,
    the rest pieces that follow on), never newer than now less ``delay_ms``."""
    with map_streams().place(address) as admitted:
        if not admitted:
            yield f"retry: {live.BUSY_RETRY_MS}\n".encode() + live.event("busy", "")
            return
        yield f"retry: {live.RETRY_MS}\n".encode()
        loop_time = asyncio.get_running_loop().time
        deadline = loop_time() + lifetime
        if delay_ms == 0:
            async for data in _live(city, chat, heartbeat, deadline):
                yield data
        else:
            async for data in _delayed(city, delay_ms, chat, deadline):
                yield data


async def _live(city: str, chat: bool, heartbeat: float, deadline: float) -> AsyncGenerator[bytes]:
    loop_time = asyncio.get_running_loop().time
    async with hub().watch(city) as watched:
        seq, state = await watched.join()
        yield _event(state)
        while (left := deadline - loop_time()) > 0:
            if not await watched.wait(seq, min(heartbeat, left)):
                yield b": heartbeat\n\n"
                continue
            pieces = watched.after(seq)
            if pieces is None:  # too far behind: the city again
                seq, state = await watched.join()
                yield _event(state)
                continue
            for piece in pieces:
                yield _event(watched.piece_bytes(piece, chat))
                seq = piece.seq


async def _delayed(city: str, delay_ms: int, chat: bool, deadline: float) -> AsyncGenerator[bytes]:
    loop_time = asyncio.get_running_loop().time
    cache = window_cache()
    limit = limit_for(delay_ms)
    start = window_start(limit)
    window = await cache.window(city, start, upto=limit)
    yield _event(cache.encoded(window, limit, chat=chat))
    sent = limit
    while loop_time() < deadline:
        await asyncio.sleep(PIECE_SECONDS)
        limit = limit_for(delay_ms)
        while sent < limit:
            start = window_start(sent)
            upto = min(limit, start + WINDOW_MS)
            window = await cache.window(city, start, upto=upto)
            yield _event(cache.piece(window, sent, upto, chat=chat))
            sent = upto


@require_safe
async def track_stream(request: HttpRequest, letter: str) -> HttpResponse | StreamingHttpResponse:
    """The track as it comes, as server-sent events, at the audience's delay or a longer one
    asked for (``?delay=`` seconds)."""
    city = _letter(letter)
    if city is None:
        return _error("no-city", 404)
    who = await audience(request)
    delay_ms = who.delay_ms
    asked = request.GET.get("delay")
    if asked is not None:
        try:
            delay_ms = max(delay_ms, int(asked) * 1000)
        except ValueError:
            return _error("bad-delay", 400)
    stream = track_events(city, delay_ms, who.chat, request.META.get("REMOTE_ADDR") or "?")
    response = StreamingHttpResponse(stream, content_type="text/event-stream")
    response["Cache-Control"] = "no-store"
    response["X-Accel-Buffering"] = "no"
    return response
