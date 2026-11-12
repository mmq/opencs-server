"""What the game recorder is told, from the cities and the sessions (the web plan's Phase 2).

``opencs.recording`` writes segments and knows nothing of the world; this is the other half. It
decides when a city is recorded (``[recording] when``: a qualifying pilot is in it), starts each
segment with a keyframe built from the world, and passes on what happens: each client message once
as it reaches the city, and each server event once, before the router fans it out. A relayed copy
of a client's message (``Event.relayed``) is left out, since the client's message is the record,
and so are the per-recipient copies of one event, folded into one record.

A keyframe is what a late joiner is sent (``podreport.go_live``), built from the same
``creating_messages`` and ``state_messages`` but never through ``introduce`` or ``inform``, which
would mark the entities as held by a client.

The same hooks feed the live feeds (``session.livefeed``, the service channel's ``subscribe``, ADR
16): while the web watches a city, each record the recorder is given goes to its feeds too, whether
or not the city is being recorded, so a feed holds exactly what a recording would.

Nothing here may hold up or break a game: every call is cheap (a check and a queue append), and a
bug in it stops recording for the run (or ends the city's feeds) instead of reaching the caller.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from opencs import policy
from opencs.clock import Clock, MonotonicClock
from opencs.config import RecordingConfig
from opencs.messages import ClientMessage, ServerMessage
from opencs.recording import Kind, PilotInfo, Recorder, chat_style, keeps_chat
from opencs.session.livefeed import Feed
from opencs.world.city import City
from opencs.world.events import Event, ToPlayer
from opencs.world.player import Player

log = logging.getLogger("opencs.recording")


class Recording:
    """The recorder's hooks, and the live feeds'. Without a ``Recorder`` (``[recording]`` off) and
    without a feed every call returns at once. ``settings`` is ``[recording]`` (the recorder's own
    if there is one): the feeds keep chat and name bots as it says."""

    def __init__(
        self,
        recorder: Recorder | None = None,
        clock: Clock | None = None,
        *,
        settings: RecordingConfig | None = None,
        wall: Callable[[], float] | None = None,
    ) -> None:
        self.recorder = recorder
        self._clock = clock or MonotonicClock()
        self.settings = recorder.config if recorder else (settings or RecordingConfig())
        self._bots = frozenset(self.settings.bots)
        #: The wall clock for a feed's start time (``time.time`` in the app; tests' clocks).
        self._wall = wall or self._clock.now
        #: City letter -> its live feeds.
        self.feeds: dict[str, list[Feed]] = {}

    # --- live feeds --------------------------------------------------------------------------

    def open_feed(self, city: City, capacity: int = policy.SERVICE_FEED_ITEMS) -> Feed:
        """A new live feed of ``city``, its first epoch begun at once."""
        feed = Feed(city.config.letter, capacity)
        self.feeds.setdefault(feed.letter, []).append(feed)
        self._start_feed(feed, city, self._clock.now())
        return feed

    def close_feed(self, feed: Feed) -> None:
        feed.close()
        feeds = self.feeds.get(feed.letter, [])
        if feed in feeds:
            feeds.remove(feed)
        if not feeds:
            self.feeds.pop(feed.letter, None)

    def _start_feed(self, feed: Feed, city: City, now: float) -> None:
        feed.start(
            now,
            self._wall(),
            city=city,
            config=city.config,
            players=[self._info(p) for p in city.players()],
            snapshot=_snapshot(city, now),
            state=_state(city),
        )

    def _feeds(self, letter: str) -> list[Feed]:
        return self.feeds.get(letter, [])

    def _feed_failed(self, letter: str, exc: Exception) -> None:
        """A bug in feeding city ``letter``: its feeds end (the subscriber sees them close), the
        game goes on."""
        log.error("a bug in the live feed of city %s: its feeds end", letter, exc_info=exc)
        for feed in list(self._feeds(letter)):
            self.close_feed(feed)

    # --- running -----------------------------------------------------------------------------

    def start(self) -> None:
        if self.recorder is not None:
            log.info("recording games in %s", self.recorder.config.directory)
            self.recorder.start()

    def close(self) -> None:
        """Finish every open segment and stop the writer (blocks for at most a few seconds: call
        it off the loop)."""
        if self.recorder is not None:
            self.recorder.close()

    # --- joins and leaves --------------------------------------------------------------------

    def joined(self, city: City, player_id: int, *, resumed: bool = False) -> None:
        """A player was seated in ``city``: start a segment if it now has a pilot to record, or
        note the join in the open one (with a keyframe for a ``/RESUME``)."""
        if self._feeds(city.config.letter):
            self._feed_joined(city, player_id, resumed)
        rec = self.recorder
        if rec is None:
            return
        try:
            letter = city.config.letter
            now = self._clock.now()
            if not rec.recording(letter):
                self._begin(rec, city, now, "resume" if resumed else "start")
                return
            player = city.player(player_id)
            if player is not None:
                rec.player(letter, now, player_id, self._info(player))
            if resumed:
                rec.keyframe(letter, now, _snapshot(city, now), _state(city), "resume")
        except Exception as exc:
            rec.stop("a bug in recording a join", exc)

    def _feed_joined(self, city: City, player_id: int, resumed: bool) -> None:
        letter = city.config.letter
        try:
            now = self._clock.now()
            player = city.player(player_id)
            payload = b"" if player is None else _row(self._info(player))
            snapshot = _snapshot(city, now) if resumed else []
            state = _state(city) if resumed else {}
            for feed in self._feeds(letter):
                feed.put(now, Kind.PLAYER, player_id, payload)
                if resumed:
                    feed.keyframe(now, snapshot, state, "resume")
        except Exception as exc:
            self._feed_failed(letter, exc)

    def left(self, city: City, player_id: int) -> None:
        """A player is out of ``city``: note it, and end the segment if no pilot to record is
        left."""
        letter = city.config.letter
        if feeds := self._feeds(letter):
            now = self._clock.now()
            for feed in feeds:
                feed.put(now, Kind.PLAYER, player_id, b"")
        rec = self.recorder
        if rec is None:
            return
        try:
            if not rec.recording(letter):
                return
            now = self._clock.now()
            rec.player(letter, now, player_id, None)
            callsigns = [p.callsign for p in city.players() if p.id != player_id]
            if not rec.wants(letter, callsigns):
                rec.end(letter, now, "empty")
        except Exception as exc:
            rec.stop("a bug in recording a leave", exc)

    def after_tick(self, city: City) -> None:
        """After a tick of ``city``: replace a segment that is old enough with a new one, and
        start a new epoch of each feed that owes one (after a gap, or the city was rebuilt)."""
        if feeds := self._feeds(city.config.letter):
            try:
                for feed in feeds:
                    if feed.city is not city and not feed.owes_start:
                        feed.restart("rebuilt")
                    if feed.owes_start:
                        self._start_feed(feed, city, self._clock.now())
            except Exception as exc:
                self._feed_failed(city.config.letter, exc)
        rec = self.recorder
        if rec is None:
            return
        try:
            now = self._clock.now()
            if rec.rotation_due(city.config.letter, now):
                self._begin(rec, city, now, "rotate")
        except Exception as exc:
            rec.stop("a bug in rotating a recording", exc)

    def _begin(self, rec: Recorder, city: City, now: float, reason: str) -> None:
        letter = city.config.letter
        players = city.players()
        if not rec.wants(letter, [p.callsign for p in players]):
            return
        rec.begin(
            letter,
            now,
            config=city.config,
            players=[self._info(p) for p in players],
            snapshot=_snapshot(city, now),
            state=_state(city),
            reason=reason,
        )

    def _info(self, player: Player) -> PilotInfo:
        bot = player.identity is None and player.callsign.upper() in self._bots
        return PilotInfo(player.id, player.callsign, player.team, bot)

    # --- what happens ------------------------------------------------------------------------

    def client(self, city: City, player_id: int, message: ClientMessage) -> None:
        """A message from ``player_id`` about to reach ``city`` (it passed the relay budgets)."""
        if feeds := self._feeds(city.config.letter):
            letter = city.config.letter
            try:
                style = chat_style(message)
                if style is None or keeps_chat(self.settings, letter, style):
                    now = self._clock.now()
                    for feed in feeds:
                        feed.put(now, Kind.CLIENT, player_id, message)
            except Exception as exc:
                self._feed_failed(letter, exc)
        rec = self.recorder
        if rec is None:
            return
        try:
            letter = city.config.letter
            if rec.recording(letter):
                rec.client(letter, player_id, message, self._clock.now())
        except Exception as exc:
            rec.stop("a bug in recording a client message", exc)

    def to_player(self, city: City, player_id: int, message: ServerMessage) -> None:
        """A message sent to one player outside the router (the kick or shutdown notice)."""
        if feeds := self._feeds(city.config.letter):
            now = self._clock.now()
            for feed in feeds:
                feed.put(now, Kind.SERVER, player_id, message)
        rec = self.recorder
        if rec is None:
            return
        try:
            letter = city.config.letter
            if rec.recording(letter):
                rec.server(letter, message, self._clock.now(), to=player_id)
        except Exception as exc:
            rec.stop("a bug in recording a server message", exc)

    def deliver(self, city: City, events: Sequence[Event]) -> None:
        """Server events about to be delivered (``Router``'s tape): each once, without relayed
        copies; a message sent alike to several single players in this call is one record for
        everyone."""
        letter = city.config.letter
        feeds = self._feeds(letter)
        rec = self.recorder
        recording = rec is not None and rec.recording(letter)
        if not feeds and not recording:
            return
        try:
            folded = _fold(events)
        except Exception as exc:
            if rec is not None:
                rec.stop("a bug in recording server events", exc)
            self._feed_failed(letter, exc)
            return
        now = self._clock.now()
        for feed in feeds:
            for message, to in folded:
                feed.put(now, Kind.SERVER, to, message)
        if rec is None or not recording:
            return
        try:
            for message, to in folded:
                rec.server(letter, message, now, to=to)
        except Exception as exc:
            rec.stop("a bug in recording server events", exc)


def _fold(events: Sequence[Event]) -> list[tuple[ServerMessage, int | None]]:
    """``(message, its one recipient or None)`` for each event worth recording, in order. Events
    to one player each with the same message are one record for everyone (``City.spawn`` and the
    leave paths make one per client); the same message twice to the same player, or to anything
    but one player, stays as many records as it was events."""
    out: list[tuple[ServerMessage, int | None]] = []
    #: message -> (its index in ``out``, the players it went to), for the ``ToPlayer`` events.
    single: dict[ServerMessage, tuple[int, set[int]]] = {}
    for event in events:
        if event.relayed:
            continue
        message, audience = event.message, event.audience
        if not isinstance(audience, ToPlayer):
            out.append((message, None))
            continue
        try:
            seen = single.get(message)
        except TypeError:  # a message that does not hash is never folded
            out.append((message, audience.player_id))
            continue
        if seen is not None and audience.player_id not in seen[1]:
            seen[1].add(audience.player_id)
            out[seen[0]] = (message, None)
            continue
        single[message] = (len(out), {audience.player_id})
        out.append((message, audience.player_id))
    return out


def _row(info: PilotInfo) -> bytes:
    """A PLAYER record's payload for a join, as the recorder writes it."""
    return json.dumps(info.row(), separators=(",", ":"), sort_keys=True).encode("utf-8")


def _snapshot(city: City, now: float) -> list[ServerMessage]:
    """What a client joining now is sent to catch up with the city (``podreport.go_live``): the
    territory grid, the weather, then every live entity's creating packets."""
    out = [*city.territory.state_messages(now), *city.weather.state_messages(now)]
    for entity in city.live.values():
        out += entity.creating_messages(now)
    return out


def _state(city: City) -> dict[str, Any]:
    """What a keyframe holds besides the packets: each player's progression and the city's
    multiplier floor. Who controls which block follows from the towers, so it is not stored."""
    return {
        "multiplier_floor": city.multiplier_floor,
        "scores": [
            {
                "id": p.id,
                "score": p.score.score,
                "multiplier": p.score.multiplier,
                "bay_points": p.score.bay_points,
                "bays": list(p.score.bays),
                "marker": p.score.marker,
            }
            for p in city.players()
        ],
    }
