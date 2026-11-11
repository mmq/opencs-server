"""The router: each audience reaches exactly its players (I App. B)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from fixtures.lobbies import city
from fixtures.pods import report
from opencs import policy
from opencs.messages import ServerMessage, server
from opencs.messages.fields import WirePos
from opencs.routing import Router
from opencs.world.city import City
from opencs.world.events import Event, ToAll, ToAllExcept, ToPlayer, ToPlayers, ToTeam
from opencs.world.seating import JoinResult


@dataclass
class Recorder:
    got: list[ServerMessage] = field(default_factory=list)
    behind: bool = False
    queued: int = 0
    shed_count: int = 0

    def send(self, msg: ServerMessage) -> None:
        self.got.append(msg)

    def congested(self) -> bool:
        return self.behind

    def backlog(self) -> int:
        return self.queued

    def shed(self) -> None:
        self.shed_count += 1


@dataclass
class Table:
    city: City
    router: Router
    sinks: dict[int, Recorder]


def table() -> Table:
    """Players 0 and 1 on team 0, 2 on team 1, 3 on team 2; player 3 also holds pod 2."""
    c = City(city("A"))
    router = Router()
    sinks = {}
    for callsign, team in (("A", 0), ("B", 0), ("C", 1), ("D", 2)):
        result = c.join(callsign, team, 0.0)
        assert isinstance(result, JoinResult)
        sinks[result.player_id] = Recorder()
        router.register("A", result.player_id, sinks[result.player_id])
    player = c.player(3)
    assert player is not None
    player.view.hold(2)
    return Table(c, router, sinks)


MSG = server.ReleaseDrop(2)


def who(t: Table, audience: object) -> list[int]:
    t.router.deliver(t.city, [Event(MSG, audience)])  # type: ignore[arg-type]
    hit = [pid for pid, sink in t.sinks.items() if sink.got]
    for sink in t.sinks.values():
        sink.got.clear()
    return hit


def test_to_player_reaches_only_that_player() -> None:
    assert who(table(), ToPlayer(1)) == [1]


def test_to_players_reaches_exactly_the_named_players_in_id_order() -> None:
    t = table()
    t.router.deliver(t.city, [Event(server.QuitAck(), ToPlayers(frozenset({3, 0, 9})))])
    assert [i for i, s in sorted(t.sinks.items()) if s.got] == [0, 3]


def test_to_all_reaches_everyone() -> None:
    assert who(table(), ToAll()) == [0, 1, 2, 3]


def test_to_all_except_skips_exactly_one() -> None:
    assert who(table(), ToAllExcept(2)) == [0, 1, 3]


def test_to_team_reaches_only_that_team() -> None:
    t = table()
    assert who(t, ToTeam(0)) == [0, 1]
    assert who(t, ToTeam(3)) == []


def test_to_holders_reaches_those_whose_view_holds_the_entity() -> None:
    t = table()
    assert who(t, t.city.holders(2)) == [2, 3]  # its owner and the client that was introduced
    assert who(t, t.city.holders(0)) == [0]
    assert who(t, t.city.holders(200)) == []


def test_to_holders_except_leaves_out_one_holder() -> None:
    t = table()
    assert who(t, t.city.holders(2, but=2)) == [3]  # the owner is left out
    assert who(t, t.city.holders(2, but=3)) == [2]
    assert who(t, t.city.holders(2, but=0)) == [2, 3]  # not a holder: changes nothing


def test_holders_are_those_when_the_event_is_made_not_when_it_is_delivered() -> None:
    t = table()
    told = t.city.holders(2)
    t.city.player(3).view.forget(2)  # type: ignore[union-attr]  # the rest of the call goes on
    assert who(t, told) == [2, 3]  # the client that held it still hears about it
    assert who(t, t.city.holders(2)) == [2]
    t.city.player(0).view.hold(2)  # type: ignore[union-attr]
    assert who(t, told) == [2, 3]  # and one told it exists later does not


def test_to_a_player_who_is_not_in_the_city_nobody_is_reached() -> None:
    assert who(table(), ToPlayer(9)) == []


def test_unregistered_players_are_skipped_and_the_rest_still_receive() -> None:
    t = table()
    t.router.unregister("A", 1)
    assert who(t, ToAll()) == [0, 2, 3]


def test_a_sink_registered_for_another_city_is_not_reached() -> None:
    t = table()
    other = Recorder()
    t.router.register("B", 0, other)
    who(t, ToAll())
    assert other.got == []


def test_a_failing_sink_does_not_stop_the_others() -> None:
    t = table()

    class Broken:
        def send(self, msg: ServerMessage) -> None:
            raise RuntimeError("boom")

    t.router.register("A", 0, Broken())
    assert who(t, ToAll()) == [1, 2, 3]


def test_events_are_delivered_in_order() -> None:
    t = table()
    first, second = server.ReleaseDrop(0), server.Destroy(0)
    t.router.deliver(t.city, [Event(first, ToPlayer(0)), Event(second, ToPlayer(0))])
    assert t.sinks[0].got == [first, second]


def test_a_droppable_event_skips_a_congested_player_and_nothing_else_does() -> None:
    t = table()
    t.sinks[1].behind = True
    t.router.deliver(t.city, [Event(MSG, ToAll(), droppable=True), Event(MSG, ToAll())])
    assert [len(t.sinks[pid].got) for pid in sorted(t.sinks)] == [2, 1, 2, 2]
    assert t.router.dropped == 1


NEAR, FAR = WirePos.from_world(1000, 10, 1000), WirePos.from_world(1000, 10, 3000)
SHOT = server.Shot(2, NEAR, 0, 0, 1)
FAR_BEHIND = policy.SHOTS_SHED_FAR_BACKLOG
ALL_BEHIND = policy.SHOTS_SHED_ALL_BACKLOG


def at(t: Table, player_id: int, where: WirePos) -> None:
    player = t.city.player(player_id)
    assert player is not None
    player.pod.placed = replace(report(0), pos=where)


def shot_to(t: Table, player_id: int, origin: WirePos) -> int:
    """How many of one shot from ``origin`` this player was sent."""
    before = len(t.sinks[player_id].got)
    t.router.deliver(t.city, [Event(SHOT, ToPlayer(player_id), shot_from=origin)])
    return len(t.sinks[player_id].got) - before


def test_a_player_keeping_up_is_sent_every_shot_however_far() -> None:
    t = table()
    at(t, 1, NEAR)
    t.sinks[1].queued = FAR_BEHIND - 1
    assert shot_to(t, 1, FAR) == 1
    assert t.router.shots_shed == 0


def test_a_player_behind_is_not_sent_far_shots_but_is_sent_near_ones() -> None:
    t = table()
    at(t, 1, NEAR)
    t.sinks[1].queued = FAR_BEHIND
    assert shot_to(t, 1, FAR) == 0
    assert shot_to(t, 1, NEAR) == 1
    assert (t.router.shots_shed, t.sinks[1].shed_count) == (1, 1)


def test_a_shot_exactly_at_the_range_is_kept() -> None:
    t = table()
    at(t, 1, WirePos.from_world(1000, 10, 1000))
    t.sinks[1].queued = FAR_BEHIND
    assert shot_to(t, 1, WirePos.from_world(1000, 10, 1000 + policy.SHOT_KEEP_FEET)) == 1
    assert shot_to(t, 1, WirePos.from_world(1000, 10, 1001 + policy.SHOT_KEEP_FEET)) == 0


def test_a_player_far_behind_is_not_sent_even_near_shots() -> None:
    t = table()
    at(t, 1, NEAR)
    t.sinks[1].queued = ALL_BEHIND
    assert shot_to(t, 1, NEAR) == 0


def test_a_player_behind_with_no_position_yet_has_nothing_near_it() -> None:
    t = table()
    t.sinks[1].queued = FAR_BEHIND
    assert shot_to(t, 1, NEAR) == 0


def test_only_a_shot_marked_as_a_players_is_shed() -> None:
    """A server-made shot (a guppy's grenade) and every other message always go."""
    t = table()
    t.sinks[1].queued = ALL_BEHIND
    t.router.deliver(t.city, [Event(SHOT, ToPlayer(1)), Event(MSG, ToPlayer(1))])
    assert len(t.sinks[1].got) == 2


def test_the_tape_hears_each_event_once_before_it_is_delivered() -> None:
    t = table()
    heard: list[list[Event]] = []

    class Tape:
        def deliver(self, city: City, events: Sequence[Event]) -> None:
            assert not any(s.got for s in t.sinks.values())  # before the fan-out
            heard.append(list(events))

    router = Router(Tape())
    for pid, sink in t.sinks.items():
        router.register("A", pid, sink)
    events = [Event(MSG, ToAll()), Event(server.QuitAck(), ToPlayer(9))]  # 9: nobody
    router.deliver(t.city, iter(events))  # a one-pass iterable still reaches everyone
    assert heard == [events]
    assert [len(s.got) for s in t.sinks.values()] == [1, 1, 1, 1]
