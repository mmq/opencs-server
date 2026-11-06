"""What a city says: a server message and who should receive it (I App. B, DESIGN §3.2).

The world never sends. A city method returns a list of ``Event``; the router (``opencs.routing``)
turns each audience into the connections that receive the message. Audiences are data, so the
spec's A.4 table is one small module, not logic spread over the rules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from opencs.messages import ServerMessage
from opencs.messages.fields import WirePos


@dataclass(frozen=True, slots=True)
class ToPlayer:
    """That client only (``'B'``, ``'I'`` to a victim, ``'E'``-``'N'``...)."""

    player_id: int


@dataclass(frozen=True, slots=True)
class ToPlayers:
    """Exactly these clients, named by the city when it makes the event. For messages that only a
    client past its load can take, e.g. chat and relayed shots: a client that has not sent its
    first ``'b'`` is still filling its receive ring with the ``'B'`` repeat (I §7.1, II §7). Also
    the clients holding an entity (``City.holders``)."""

    player_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class ToAll:
    """Every client in the city (``'J'``, chat, ``'M'``...)."""


@dataclass(frozen=True, slots=True)
class ToAllExcept:
    """Every client in the city but one."""

    player_id: int


@dataclass(frozen=True, slots=True)
class ToTeam:
    """Every client whose player is on this team (team chat)."""

    team: int


type Audience = ToPlayer | ToPlayers | ToAll | ToAllExcept | ToTeam

#: The clients holding an entity as they are now (``City.holders``), for what has no city at hand.
type Holders = Callable[[int], ToPlayers]


@dataclass(frozen=True, slots=True)
class Event:
    """A server message and its audience. A ``droppable`` one (a player's chat) is not sent to a
    player whose connection is already far behind: losing it costs nothing, while queueing it could
    overflow the outbox and cut that player off (DESIGN §5). ``shot_from`` marks a player's relayed
    shot, which is shed sooner, the farther from the player it started (``routing``, ADR 11)."""

    message: ServerMessage
    audience: Audience
    droppable: bool = False
    shot_from: WirePos | None = None
