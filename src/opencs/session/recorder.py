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

Nothing here may hold up or break a game: every call is cheap (a check and a queue append), and a
bug in it stops recording for the run instead of reaching the caller.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from opencs.clock import Clock, MonotonicClock
from opencs.messages import ClientMessage, ServerMessage
from opencs.recording import PilotInfo, Recorder
from opencs.world.city import City
from opencs.world.events import Event, ToPlayer
from opencs.world.player import Player

log = logging.getLogger("opencs.recording")


class Recording:
    """The recorder's hooks. Without a ``Recorder`` (``[recording]`` off) every call returns at
    once."""

    def __init__(self, recorder: Recorder | None = None, clock: Clock | None = None) -> None:
        self.recorder = recorder
        self._clock = clock or MonotonicClock()

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
                rec.player(letter, now, player_id, self._info(rec, player))
            if resumed:
                rec.keyframe(letter, now, _snapshot(city, now), _state(city), "resume")
        except Exception as exc:
            rec.stop("a bug in recording a join", exc)

    def left(self, city: City, player_id: int) -> None:
        """A player is out of ``city``: note it, and end the segment if no pilot to record is
        left."""
        rec = self.recorder
        if rec is None:
            return
        try:
            letter = city.config.letter
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
        """After a tick of ``city``: replace a segment that is old enough with a new one."""
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
            players=[self._info(rec, p) for p in players],
            snapshot=_snapshot(city, now),
            state=_state(city),
            reason=reason,
        )

    @staticmethod
    def _info(rec: Recorder, player: Player) -> PilotInfo:
        bot = player.identity is None and rec.is_bot(player.callsign)
        return PilotInfo(player.id, player.callsign, player.team, bot)

    # --- what happens ------------------------------------------------------------------------

    def client(self, city: City, player_id: int, message: ClientMessage) -> None:
        """A message from ``player_id`` about to reach ``city`` (it passed the relay budgets)."""
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
        rec = self.recorder
        if rec is None:
            return
        try:
            letter = city.config.letter
            if not rec.recording(letter):
                return
            now = self._clock.now()
            for message, to in _fold(events):
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
