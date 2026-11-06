"""Audience to connections (I App. B, DESIGN §3.2, ADR 4).

A city says who should hear a message as an ``Audience``; the router knows which in-game session
speaks for each player and turns the one into the other. It knows nothing of rules: it reads the
city's roster (id, team, what each client holds) and nothing else.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from typing import Protocol

from opencs import policy
from opencs.messages import ServerMessage
from opencs.messages.fields import WirePos
from opencs.world.city import City
from opencs.world.events import (
    Audience,
    Event,
    ToAll,
    ToAllExcept,
    ToPlayer,
    ToPlayers,
    ToTeam,
)

log = logging.getLogger("opencs.routing")


class Sink(Protocol):
    """Where a player's messages go: its in-game session."""

    def send(self, msg: ServerMessage) -> None: ...

    def congested(self) -> bool:
        """True if the player is so far behind that droppable events are skipped."""
        ...

    def backlog(self) -> int:
        """Items queued for the player and not yet written."""
        ...

    def shed(self) -> None:
        """A shot was not sent to this player: for its connection's log."""
        ...


class Router:
    """The directory of ``(city letter, player id) -> sink`` and the audience resolver."""

    def __init__(self) -> None:
        self._sinks: dict[tuple[str, int], Sink] = {}
        #: Droppable events (chat) not sent to a congested player.
        self.dropped = 0
        #: Relayed shots not sent to a player who is behind (``_sheds``).
        self.shots_shed = 0

    def register(self, letter: str, player_id: int, sink: Sink) -> None:
        self._sinks[(letter, player_id)] = sink

    def unregister(self, letter: str, player_id: int) -> None:
        """Stop delivering to this player (it quit, or its line dropped)."""
        self._sinks.pop((letter, player_id), None)

    def resolve(self, city: City, audience: Audience) -> list[int]:
        """The ids of the players in ``city`` that ``audience`` names, in id order."""
        players = city.players()
        match audience:
            case ToPlayer(player_id):
                return [p.id for p in players if p.id == player_id]
            case ToPlayers(player_ids):
                return [p.id for p in players if p.id in player_ids]
            case ToAll():
                return [p.id for p in players]
            case ToAllExcept(player_id):
                return [p.id for p in players if p.id != player_id]
            case ToTeam(team):
                return [p.id for p in players if p.team == team]

    def deliver(self, city: City, events: Iterable[Event]) -> None:
        """Send each event to the sinks of its audience. A player with no sink (not yet seated in
        a session, or gone) is skipped, and so is a droppable event to a congested sink and a shot
        to a sink that is behind; one sink failing never stops the others."""
        letter = city.config.letter
        for event in events:
            for player_id in self.resolve(city, event.audience):
                sink = self._sinks.get((letter, player_id))
                if sink is None:
                    continue
                try:
                    if event.droppable and sink.congested():
                        self.dropped += 1
                        continue
                    if event.shot_from is not None and self._sheds(
                        city, player_id, sink, event.shot_from
                    ):
                        self.shots_shed += 1
                        sink.shed()
                        continue
                    sink.send(event.message)
                except Exception:
                    log.exception("delivery to player %d of city %s failed", player_id, letter)

    @staticmethod
    def _sheds(city: City, player_id: int, sink: Sink, origin: WirePos) -> bool:
        """True if this player is too far behind for a shot from ``origin``. The farthest go first,
        and a player who has not reported a position has nothing near it."""
        behind = sink.backlog()
        if behind < policy.SHOTS_SHED_FAR_BACKLOG:
            return False
        player = city.player(player_id)
        placed = None if player is None else player.pod.placed
        if behind >= policy.SHOTS_SHED_ALL_BACKLOG or placed is None:
            return True
        across = math.hypot(
            placed.pos.world_x - origin.world_x, placed.pos.world_z - origin.world_z
        )
        return across > policy.SHOT_KEEP_FEET
