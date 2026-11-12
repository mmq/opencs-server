"""Track windows: a city's track over one minute, decoded once however many viewers ask (ADR 16).

A window starts on a whole minute (UTC). :func:`build_window` seeks to its start (the keyframe at
or before it, played forward) and keeps what happens until its end. :class:`WindowCache` holds the
windows decoded lately (a bounded LRU), decodes each off the event loop, once even when many ask at
the same moment, and keeps a window that may still grow (its data is in a segment being written)
only briefly. What a viewer may see of a window is cut by its audience's delay when it is encoded
(``track.encode``), never when it is decoded.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from opencs_web.maps.source import Header, Recordings
from opencs_web.maps.track import CityTrack, Collected, dumps, encode

#: A window's length, in ms.
WINDOW_MS = 60_000
#: Windows held decoded, and their JSON for each cut asked for.
CACHE_WINDOWS = 64
CACHE_ENCODED = 256
#: How long a window whose data may still grow is used before it is decoded again, in seconds.
OPEN_TTL = 5.0
#: The recorder writes about twice a second: data this old is on the disk.
WRITTEN_MS = 1500


def window_start(at: int) -> int:
    return at - at % WINDOW_MS


@dataclass(frozen=True)
class Window:
    """A city's track over ``[start, end)``: the city at ``start`` and what was collected.
    ``final`` if nothing read can still change; else ``known_until`` is how far it is complete."""

    city: str
    start: int
    end: int
    state: dict[str, Any]
    collected: Collected
    final: bool
    known_until: int
    made: float  # monotonic time it was decoded

    def json(self, upto: int, *, chat: bool = False) -> dict[str, Any]:
        """The window as a viewer gets it, cut at ``upto``."""
        return encode(
            self.city, self.start, min(upto, self.end), self.state, self.collected, chat=chat
        )

    def piece(self, start: int, end: int, *, chat: bool = False) -> dict[str, Any]:
        """``[start, end)`` of it without the state: the next piece, for a viewer with the rest."""
        return encode(self.city, start, end, None, self.collected, chat=chat)


def build_window(recordings: Recordings, city: str, start: int, end: int, now_ms: int) -> Window:
    """Decode ``city``'s track over ``[start, end)`` from what was recorded."""
    items, final = recordings.items(city, start, end)
    track = CityTrack(city)
    state: dict[str, Any] | None = None
    for item in items:
        if isinstance(item, Header):
            began = int(item.header.get("started_utc_ms", 0))
            if state is None and began >= start:
                state = track.state(start)
                track.collecting = True
            track.begin_segment(item.header)
            continue
        if state is None and item.at >= start:
            state = track.state(start)
            track.collecting = True
        track.record(item.at, item.kind, item.player, item.payload)
    if state is None:
        state = track.state(start)
    final = final and end <= now_ms - WRITTEN_MS
    return Window(
        city, start, end, state, track.take(), final, now_ms - WRITTEN_MS, time.monotonic()
    )


class WindowCache:
    """Decoded windows, shared by every viewer. ``recordings`` gives the reader to use."""

    def __init__(
        self,
        recordings: Callable[[], Recordings],
        *,
        size: int = CACHE_WINDOWS,
        ttl: float = OPEN_TTL,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._recordings = recordings
        self._size = size
        self._ttl = ttl
        self._wall = wall
        self._windows: OrderedDict[tuple[str, int], Window] = OrderedDict()
        self._encoded: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
        self._inflight: dict[tuple[str, int], asyncio.Future[Window]] = {}
        #: Windows decoded, for tests and the load check.
        self.decodes = 0

    def now_ms(self) -> int:
        return round(self._wall() * 1000)

    async def window(self, city: str, start: int, *, upto: int | None = None) -> Window:
        """The window of ``city`` starting at ``start`` (a whole minute), complete at least to
        ``upto`` if it can be."""
        key = (city, start)
        held = self._windows.get(key)
        if held is not None and self._usable(held, upto):
            self._windows.move_to_end(key)
            return held
        running = self._inflight.get(key)
        if running is None:
            running = asyncio.ensure_future(self._decode(city, start))
            self._inflight[key] = running
        return await asyncio.shield(running)

    def _usable(self, window: Window, upto: int | None) -> bool:
        if window.final:
            return True
        if time.monotonic() - window.made > self._ttl:
            return False
        return upto is None or upto <= window.known_until

    async def _decode(self, city: str, start: int) -> Window:
        key = (city, start)
        try:
            self.decodes += 1
            window = await asyncio.to_thread(
                build_window, self._recordings(), city, start, start + WINDOW_MS, self.now_ms()
            )
        finally:
            self._inflight.pop(key, None)
        self._windows[key] = window
        self._windows.move_to_end(key)
        while len(self._windows) > self._size:
            self._windows.popitem(last=False)
        return window

    def encoded(self, window: Window, upto: int, *, chat: bool = False) -> bytes:
        """The window's JSON cut at ``upto``, made once per window, cut and audience."""
        key = (window.city, window.start, window.made, None, min(upto, window.end), chat)
        return self._held(key, lambda: window.json(upto, chat=chat))

    def piece(self, window: Window, start: int, end: int, *, chat: bool = False) -> bytes:
        """``[start, end)`` of the window without its state, made once for every viewer."""
        key = (window.city, window.start, window.made, start, end, chat)
        return self._held(key, lambda: window.piece(start, end, chat=chat))

    def _held(self, key: tuple[Any, ...], make: Callable[[], dict[str, Any]]) -> bytes:
        data = self._encoded.get(key)
        if data is None:
            data = dumps(make())
            self._encoded[key] = data
            while len(self._encoded) > CACHE_ENCODED:
                self._encoded.popitem(last=False)
        else:
            self._encoded.move_to_end(key)
        return data
