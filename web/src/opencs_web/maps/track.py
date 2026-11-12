"""The viewer track: what a map shows of a city, worked out from its records (ADR 16).

A recording (ADR 14) and a live feed hold the same records: what each client sent the city, and each
server event once. :class:`CityTrack` applies them in order and keeps the city as a map draws it:
the pilots, where each pod stands, the towers, mines and NPCs (Sentinels, guppies, Munchers), who
controls which block, and the scores. While it *collects*, it also keeps what happened: each pod
report, and events (shots, deaths, towers placed and destroyed, NPC moves, joins and leaves,
territory changes, scores; chat too, which only an administrator's track may carry).

It follows the server's own rules where the records leave a choice:

- After a death (the client's ``'J'`` or the server's ``'J'`` naming the pod) a client flies a
  camera over the city and its reports describe the camera. As ``Pod.placed`` does on the server,
  those reports are not the pod's place: it stands again only after its bare ``'A'``, or once
  ``DROP_REQUEST_TIMEOUT`` has passed.
- A late joiner is sent the city's creating packets (``podreport.go_live``), and the recorder keeps
  them as messages to that one player. Creates of things already known are copies, never moves.
- A keyframe is what a late joiner is sent: the state is rebuilt from it.

:func:`encode` makes the JSON a viewer gets for a time window; the format is in ADR 16 and the web
README.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from opencs import policy
from opencs.messages import DecodeError, client, decode_client, decode_server, server
from opencs.recording import Kind
from opencs.wire.codec import CELL_SIZE
from opencs.world.territory import Territory

log = logging.getLogger("opencs_web.maps")

#: The version of the track format (``"v"`` in every window).
FORMAT_VERSION = 1

#: A late joiner's creating packets reach it in the same call as its first report, so within this
#: many ms of it (the recorder's times are whole ms of the loop's clock).
SYNC_MS = 5
#: A kill notice and the death it pays for come in the same call.
KILL_MS = 5
#: After a death, a report this long after it is the pod's place again even without its ``'A'``.
DROP_TIMEOUT_MS = round(policy.DROP_REQUEST_TIMEOUT * 1000)

_PLAYERS = policy.PLAYER_IDS
_GUPPIES = policy.GUPPY_IDS
_NO_MAP = Territory(None)


def _is_pod(entity: int) -> bool:
    return _PLAYERS[0] <= entity <= _PLAYERS[1]


def heading(orient: int) -> int:
    """Whole degrees, 0-359, from tenths: 0 is north (+z), 90 east (+x)."""
    return round(orient / 10) % 360


@dataclass(slots=True)
class _Pilot:
    id: int
    callsign: str
    team: int
    bot: bool


@dataclass(slots=True)
class _Pod:
    x: int
    z: int
    h: int = 0
    s: int = 0
    r: int = 0
    f: int = 0
    alive: bool = True
    died_at: int = 0
    released: bool = False  # its bare 'A' since the death: the next report is its place


@dataclass(slots=True)
class _Npc:
    kind: str  # "s" Sentinel, "g" guppy, "m" Muncher
    x: int
    z: int
    dest: tuple[int, int] | None = None
    since: int = 0  # when it set off for ``dest``


#: A pod report kept for the track: time, pod, x, z, heading, speed, turn, state.
type Report = tuple[int, int, int, int, int, int, int, int]


@dataclass
class Collected:
    """What a :class:`CityTrack` kept while collecting: reports, and events ``[at, code, ...]``
    with ``at`` in UTC ms."""

    reports: list[Report] = field(default_factory=list)
    events: list[list[Any]] = field(default_factory=list)


class CityTrack:
    """A city as a map shows it, built from records in order. ``collecting`` keeps the reports
    and events in ``collected`` as well."""

    def __init__(self, city: str) -> None:
        self.city = city
        self.pilots: dict[int, _Pilot] = {}
        self.pods: dict[int, _Pod] = {}
        self.towers: dict[int, tuple[int, int, int, int]] = {}  # team, x, z, mega
        self.mines: dict[int, tuple[int, int, int]] = {}  # team, x, z
        self.npcs: dict[int, _Npc] = {}
        self.scores: dict[int, tuple[int, int]] = {}  # score, multiplier
        self.floor = 0
        self.control: dict[tuple[int, int], int] = {}
        self.collecting = False
        self.collected = Collected()
        #: The city's configuration from the last segment header (or feed epoch).
        self.config: Mapping[str, Any] | None = None
        self.at = 0  # the time of the last record
        self._keyframe = False
        self._rotating = False  # the last segment ended with a rotation: the next continues it
        self._kills: list[tuple[int, int | None, bool]] = []
        self._syncing: dict[int, int] = {}  # player -> the time of its first report
        self.undecodable = 0

    # --- input -------------------------------------------------------------------------------

    def begin_segment(self, header: Mapping[str, Any]) -> None:
        """A segment (or a feed's epoch) begins with this header: its player table. Unless the
        last one ended with a rotation, nothing carries over."""
        if not self._rotating:
            self._clear()
        self._rotating = False
        config = header.get("config")
        if isinstance(config, Mapping):
            self.config = config
        pilots: dict[int, _Pilot] = {}
        for row in header.get("players", []):
            try:
                pilot = _Pilot(
                    int(row["id"]), str(row["callsign"]), int(row["team"]), bool(row.get("bot"))
                )
            except (KeyError, TypeError, ValueError):
                continue
            pilots[pilot.id] = pilot
        self.pilots = pilots

    def take(self) -> Collected:
        """What was collected so far, and start afresh."""
        out, self.collected = self.collected, Collected()
        return out

    def record(self, at: int, kind: Kind, player: int | None, payload: bytes) -> None:
        """Apply one record at ``at`` (UTC ms) from ``player`` (None: the server, or everyone)."""
        self.at = at
        try:
            match kind:
                case Kind.CLIENT:
                    if player is not None:
                        self._client(at, player, decode_client(payload))
                case Kind.SERVER:
                    self._server(at, player, decode_server(payload)[0])
                case Kind.SNAPSHOT:
                    self._snapshot(at, decode_server(payload)[0])
                case Kind.PLAYER:
                    if player is not None:
                        self._player(at, player, payload)
                case Kind.KEYFRAME:
                    self._begin_keyframe(at, payload)
                case Kind.KEYFRAME_END:
                    self._end_keyframe(at)
                case Kind.END:
                    self._end(at, payload)
        except (DecodeError, ValueError, KeyError, TypeError):
            self.undecodable += 1

    # --- keyframes and segments ----------------------------------------------------------------

    def _clear(self) -> None:
        self.pilots.clear()
        self._clear_world()
        self.scores.clear()

    def _clear_world(self) -> None:
        self.pods.clear()
        self.towers.clear()
        self.mines.clear()
        self.npcs.clear()
        self.control.clear()
        self._kills.clear()
        self._syncing.clear()

    def _begin_keyframe(self, at: int, payload: bytes) -> None:
        state = json.loads(payload) if payload else {}
        self._clear_world()
        self._keyframe = True
        self.floor = int(state.get("multiplier_floor", self.floor))
        self.scores = {
            int(s["id"]): (int(s["score"]), int(s["multiplier"])) for s in state.get("scores", [])
        }

    def _snapshot(self, at: int, message: object) -> None:
        match message:
            case server.Introduce(id=pod_id, pos=pos, orient=orient):
                # Without an 'A' after it, the pod is dead and waiting to drop.
                self.pods[pod_id] = _Pod(
                    pos.world_x, pos.world_z, heading(orient), alive=False, died_at=at
                )
            case server.ReleaseDrop(id=pod_id) if pod_id in self.pods:
                self.pods[pod_id].alive = True
            case _:
                self._create(at, None, message)

    def _end_keyframe(self, at: int) -> None:
        self._keyframe = False
        self._update_control(at, quiet=True)
        if self.collecting:
            self._event(at, "key", self.state(at))

    def _end(self, at: int, payload: bytes) -> None:
        reason = str(json.loads(payload).get("reason")) if payload else "?"
        if reason == "rotate":
            self._rotating = True
            return
        self._clear()
        self._event(at, "end", reason)

    # --- players -----------------------------------------------------------------------------

    def _player(self, at: int, player: int, payload: bytes) -> None:
        if payload:
            row = json.loads(payload)
            pilot = _Pilot(player, str(row["callsign"]), int(row["team"]), bool(row.get("bot")))
            self.pilots[player] = pilot
            self.pods.pop(player, None)
            self._event(at, "join", player, pilot.callsign, pilot.team, int(pilot.bot))
        else:
            self.pilots.pop(player, None)
            self.pods.pop(player, None)
            self.scores.pop(player, None)
            self._syncing.pop(player, None)
            self._event(at, "leave", player)

    # --- what the clients sent ---------------------------------------------------------------

    def _client(self, at: int, player: int, message: object) -> None:
        match message:
            case client.PodState():
                self._report(at, player, message)
            case client.Died():
                pod = self.pods.get(player)
                if pod is not None and pod.alive:
                    self._die(pod, at)
            case client.DropMe():
                pod = self.pods.get(player)
                if pod is not None and not pod.alive:
                    pod.released = True
            case client.Shot(shooter=shooter, pos=pos, orient=orient, kind=kind):
                self._event(at, "shot", shooter, pos.world_x, pos.world_z, heading(orient), kind)
            case client.Grenade(pos=pos, yaw=yaw):
                self._event(at, "gren", player, pos.world_x, pos.world_z, heading(yaw))
            case client.Chat(style=style, text=text):
                self._event(at, "chat", player, style, text.decode("ascii", "replace"))
            case client.Status(score=score, multiplier=multiplier):
                self._score(at, player, score, multiplier)
            case client.LegArrived(id=npc_id):
                npc = self.npcs.get(npc_id)
                if npc is not None and npc.dest is not None:
                    npc.x, npc.z = npc.dest
                    npc.dest = None
                    self._event(at, "arr", npc_id, npc.x, npc.z)

    def _report(self, at: int, player: int, report: client.PodState) -> None:
        pod = self.pods.get(player)
        if pod is None:
            # Its first report: the city sends it the city's creating packets now.
            self._syncing[player] = at
            pod = self.pods[player] = _Pod(0, 0)
            self._event(at, "live", player)
        elif not pod.alive:
            if not pod.released and at - pod.died_at < DROP_TIMEOUT_MS:
                return  # the fly-over's camera, not the pod
            pod.alive = True
            pod.released = False
            self._event(at, "live", player)
        pos = report.pos
        pod.x, pod.z = pos.world_x, pos.world_z
        pod.h = heading(report.orient)
        pod.s = report.fwd
        pod.r = 0 if report.state == policy.POD_STATE_FALLING else round(report.v1 / 10)
        pod.f = report.state
        if self.collecting:
            self.collected.reports.append((at, player, pod.x, pod.z, pod.h, pod.s, pod.r, pod.f))

    def _die(self, pod: _Pod, at: int) -> None:
        pod.alive = False
        pod.released = False
        pod.died_at = at

    def _score(self, at: int, player: int, score: int, multiplier: int) -> None:
        if self.scores.get(player) != (score, multiplier):
            self.scores[player] = (score, multiplier)
            self._event(at, "score", player, score, multiplier)

    # --- what the server sent ----------------------------------------------------------------

    def _server(self, at: int, player: int | None, message: object) -> None:
        match message:
            case server.Destroy(id=entity):
                self._destroy(at, entity)
            case server.Delete(id=pod_id):
                self.pods.pop(pod_id, None)
            case server.KillNotice(earned=earned):
                self._kills.append((at, player, earned))
            case server.SetStatus(score=score, multiplier=multiplier) if player is not None:
                self._score(at, player, score, multiplier)
            case server.Shot(id=shooter, pos=pos, orient=orient, kind=kind):
                self._event(at, "shot", shooter, pos.world_x, pos.world_z, heading(orient), kind)
            case server.Chat(style=style, text=text):
                self._event(at, "chat", None, style, text.decode("ascii", "replace"))
            case _:
                self._create(at, player, message)

    def _syncing_now(self, at: int, player: int | None) -> bool:
        """Whether a message to ``player`` alone is part of its sync as a late joiner."""
        if player is None:
            return False
        first = self._syncing.get(player)
        return first is not None and at - first <= SYNC_MS

    def _create(self, at: int, player: int | None, message: object) -> None:
        """A message that makes something, or moves an NPC. Inside a keyframe it is quiet."""
        copy = self._syncing_now(at, player)
        match message:
            case server.SpawnSentinel(id=npc_id, pos=pos) if npc_id not in self.npcs:
                self.npcs[npc_id] = _Npc("s", pos.world_x, pos.world_z)
                self._event(at, "npc", npc_id, "s", pos.world_x, pos.world_z)
            case server.SpawnTower(id=tower, team=team, pos=pos, mega=mega) if (
                tower not in self.towers
            ):
                self.towers[tower] = (team, pos.world_x, pos.world_z, mega)
                self._event(at, "tower", tower, team, pos.world_x, pos.world_z, mega)
                self._update_control(at)
            case server.SpawnMine(id=mine, variant=team, pos=pos) if mine not in self.mines:
                self.mines[mine] = (team, pos.world_x, pos.world_z)
                self._event(at, "mine", mine, team, pos.world_x, pos.world_z)
            case server.MobileCreate(id=npc_id, pos=pos):
                npc = self.npcs.get(npc_id)
                if npc is None:
                    kind = "g" if _GUPPIES[0] <= npc_id <= _GUPPIES[1] else "m"
                    self.npcs[npc_id] = _Npc(kind, pos.world_x, pos.world_z)
                    self._event(at, "npc", npc_id, kind, pos.world_x, pos.world_z)
                elif not copy:
                    self._send(at, npc_id, npc, pos.world_x, pos.world_z)
            case server.MobileWarp(id=npc_id, pos=pos) if npc_id in self.npcs:
                npc = self.npcs[npc_id]
                if npc.kind == "g":
                    npc.x, npc.z, npc.dest = pos.world_x, pos.world_z, None
                    self._event(at, "npc", npc_id, "g", npc.x, npc.z)
                else:
                    self._send(at, npc_id, npc, pos.world_x, pos.world_z)

    def _send(self, at: int, npc_id: int, npc: _Npc, x: int, z: int) -> None:
        if npc.dest == (x, z):
            return
        npc.dest, npc.since = (x, z), at
        self._event(at, "go", npc_id, x, z)

    def _destroy(self, at: int, entity: int) -> None:
        if _is_pod(entity):
            pod = self.pods.get(entity)
            if pod is not None and pod.alive:
                self._die(pod, at)
            kills = [k for k in self._kills if abs(k[0] - at) <= KILL_MS]
            self._kills = [k for k in self._kills if at - k[0] <= KILL_MS]
            killer = next((payee for _, payee, _ in kills if payee is not None), None)
            earned = any(e for _, _, e in kills)
            if entity in self.pilots or pod is not None:
                self._event(at, "die", entity, killer, int(earned))
            return
        if self.towers.pop(entity, None) is not None:
            self._event(at, "gone", entity)
            self._update_control(at)
        elif self.mines.pop(entity, None) is not None or self.npcs.pop(entity, None) is not None:
            self._event(at, "gone", entity)

    def _update_control(self, at: int, *, quiet: bool = False) -> None:
        if self._keyframe:
            return
        towers = (
            ((x // CELL_SIZE, z // CELL_SIZE), team) for team, x, z, _ in self.towers.values()
        )
        control = _NO_MAP.controllers(towers)
        if control != self.control:
            self.control = control
            if not quiet:
                self._event(at, "ctrl", _control_rows(control))

    def _event(self, at: int, code: str, *args: Any) -> None:
        if self.collecting and not self._keyframe:
            self.collected.events.append([at, code, *args])

    # --- output ------------------------------------------------------------------------------

    def state(self, at: int) -> dict[str, Any]:
        """The city now, as JSON; times relative to ``at``."""
        return {
            "pilots": [
                [p.id, p.callsign, p.team, int(p.bot)]
                for p in sorted(self.pilots.values(), key=_id)
            ],
            "pods": [
                [i, p.x, p.z, p.h, p.s, p.r, p.f, int(p.alive)]
                for i, p in sorted(self.pods.items())
            ],
            "towers": [[i, *t] for i, t in sorted(self.towers.items())],
            "mines": [[i, *m] for i, m in sorted(self.mines.items())],
            "npcs": [
                [
                    i,
                    n.kind,
                    n.x,
                    n.z,
                    *(n.dest or (None, None)),
                    None if n.dest is None else n.since - at,
                ]
                for i, n in sorted(self.npcs.items())
            ],
            "control": _control_rows(self.control),
            "scores": [[i, *s] for i, s in sorted(self.scores.items())],
            "floor": self.floor,
        }


def _id(pilot: _Pilot) -> int:
    return pilot.id


def _control_rows(control: Mapping[tuple[int, int], int]) -> list[list[int]]:
    return [[bx, bz, team] for (bx, bz), team in sorted(control.items())]


# --- the format ----------------------------------------------------------------------------------

#: The columns of a pod's reports in a window: time (ms since the window's start), x, z (feet),
#: heading (degrees), speed (feet a second), turn rate (degrees a second), state.
REPORT_COLUMNS = ("t", "x", "z", "h", "s", "r", "f")


def _deltas(values: Sequence[int]) -> list[int]:
    out, last = [], 0
    for v in values:
        out.append(v - last)
        last = v
    return out


def encode(
    city: str,
    start: int,
    end: int,
    state: Mapping[str, Any] | None,
    collected: Collected,
    *,
    chat: bool = False,
) -> dict[str, Any]:
    """The JSON a viewer gets for ``[start, end)`` (UTC ms): the city at ``start`` (``state``,
    or None for a piece that follows one the viewer has), each pod's reports as delta-coded
    columns, and the events with times in ms since ``start``. Reports and events outside the
    window are left out, and chat unless ``chat``."""
    pods: dict[int, list[Report]] = {}
    for report in collected.reports:
        if start <= report[0] < end:
            pods.setdefault(report[1], []).append(report)
    columns = {
        str(pod): [
            _deltas([r[0] - start for r in rows]),
            *(_deltas([r[i] for r in rows]) for i in range(2, 8)),
        ]
        for pod, rows in sorted(pods.items())
    }
    events = [
        [e[0] - start, *e[1:]]
        for e in collected.events
        if start <= e[0] < end and (chat or e[1] != "chat")
    ]
    return {
        "v": FORMAT_VERSION,
        "city": city,
        "start": start,
        "end": end,
        "state": state,
        "pods": columns,
        "ev": events,
    }


def dumps(window: Mapping[str, Any]) -> bytes:
    return json.dumps(window, separators=(",", ":")).encode("utf-8")
