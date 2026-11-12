// The map viewer's track logic, with no page in it (maps.js draws): windows of the track format
// (ADR 16) decoded, the city worked out at any time, and where a pod or an NPC is between the
// times the track gives. Tested with node (web/tests/js/).

export const WINDOW_MS = 60000;
// The most a pod is moved on past its last report, in ms: a pod that stops reporting stops.
export const AHEAD_MS = 1000;
// Reports further apart than this (ms), or further from where the first one leads (feet), are
// not blended into each other: the pod jumps.
export const BLEND_MS = 2500;
export const BLEND_FEET = 300;
// An NPC's speed (feet a second) while the track has not yet said when it arrives.
export const NPC_SPEED = 40;
// How long a shot, grenade, death or destruction stays drawn, in ms.
export const MARK_MS = { shot: 600, gren: 1500, die: 4000, gone: 2000 };
const CHAT_KEPT = 50;
const RAD = Math.PI / 180;

export function windowStart(t) {
  return Math.floor(t / WINDOW_MS) * WINDOW_MS;
}

function undelta(column) {
  const out = new Array(column.length);
  let v = 0;
  for (let i = 0; i < column.length; i++) {
    v += column[i];
    out[i] = v;
  }
  return out;
}

// A window's (or piece's) reports by pod, as rows {t, x, z, h, s, r, f} with t in UTC ms, and its
// events as {t, c, a} (time, code, the rest), in order.
export function decode(window) {
  const reports = new Map();
  for (const [id, columns] of Object.entries(window.pods || {})) {
    const [t, x, z, h, s, r, f] = columns.map(undelta);
    const rows = t.map((ti, i) => ({
      t: window.start + ti, x: x[i], z: z[i], h: h[i], s: s[i], r: r[i], f: f[i],
    }));
    reports.set(Number(id), rows);
  }
  const events = (window.ev || []).map((e) => ({ t: window.start + e[0], c: e[1], a: e.slice(2) }));
  return { reports, events };
}

export function norm(degrees) {
  return ((degrees % 360) + 360) % 360;
}

function turn(from, to) {
  return ((to - from + 540) % 360) - 180;
}

// Where a pod at report `p` is `sec` seconds on, at its speed and turn rate (heading 0 north, +z;
// 90 east, +x; a positive turn rate turns towards east).
export function reckon(p, sec) {
  const h = p.h * RAD;
  const w = p.r * RAD;
  if (Math.abs(w) < 1e-9) {
    return { x: p.x + p.s * Math.sin(h) * sec, z: p.z + p.s * Math.cos(h) * sec, h: p.h };
  }
  const h2 = h + w * sec;
  return {
    x: p.x + (p.s * (Math.cos(h) - Math.cos(h2))) / w,
    z: p.z + (p.s * (Math.sin(h2) - Math.sin(h))) / w,
    h: norm(p.h + p.r * sec),
  };
}

// Where a pod is at `t`, its last report `a` and the next `b` (or null). Dead reckoning from `a`,
// at most AHEAD_MS on; with `b` close enough, the gap between where `a` leads and `b` is closed
// as `t` nears it, so the drawn path passes through every report.
export function estimate(a, b, t, alive) {
  if (!alive) {
    return { x: a.x, z: a.z, h: a.h };
  }
  const p = reckon(a, Math.min(Math.max(t - a.t, 0), AHEAD_MS) / 1000);
  if (b && b.t > a.t && b.t - a.t <= BLEND_MS) {
    const q = reckon(a, Math.min(b.t - a.t, AHEAD_MS) / 1000);
    const ex = b.x - q.x;
    const ez = b.z - q.z;
    if (ex * ex + ez * ez <= BLEND_FEET * BLEND_FEET) {
      const f = Math.min(Math.max((t - a.t) / (b.t - a.t), 0), 1);
      p.x += ex * f;
      p.z += ez * f;
      p.h = norm(p.h + turn(q.h, b.h) * f);
    }
  }
  return p;
}

// The last index in `rows` (sorted by t) with rows[i].t <= t, or -1.
export function lastAtOrBefore(rows, t) {
  let lo = 0;
  let hi = rows.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].t <= t) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}

// Where an NPC is at `t`: still, or on its way from where it set off to `dest`, arriving when the
// track says (its `arr`) or, until it says, at NPC_SPEED.
export function npcAt(npc, t) {
  if (npc.dx === null) {
    return { x: npc.x, z: npc.z };
  }
  const dist = Math.hypot(npc.dx - npc.x, npc.dz - npc.z);
  const until = npc.go && npc.go.until !== null ? npc.go.until : null;
  const ms = until !== null ? until - npc.since : (dist / NPC_SPEED) * 1000;
  const f = ms <= 0 ? 1 : Math.min(Math.max((t - npc.since) / ms, 0), 1);
  return { x: npc.x + (npc.dx - npc.x) * f, z: npc.z + (npc.dz - npc.z) * f };
}

// The city at one time: what a state gives, and the events applied after it. Pod places are kept
// as a fallback (`t`, the time they are from); the reports in a Track override them.
export class World {
  constructor(state, at, goes) {
    this.t = at;
    this.marks = [];
    this.chat = [];
    this.ended = null;
    this.load(state, at, goes);
  }

  load(state, at, goes) {
    this.pilots = new Map(
      state.pilots.map(([id, callsign, team, bot]) => [id, { id, callsign, team, bot: !!bot }]),
    );
    this.pods = new Map(
      state.pods.map(([id, x, z, h, s, r, f, alive]) => [
        id, { t: at, x, z, h, s, r, f, alive: !!alive, diedAt: alive ? null : at },
      ]),
    );
    this.towers = new Map(
      state.towers.map(([id, team, x, z, mega]) => [id, { team, x, z, mega: !!mega }]),
    );
    this.mines = new Map(state.mines.map(([id, team, x, z]) => [id, { team, x, z }]));
    this.npcs = new Map(
      state.npcs.map(([id, kind, x, z, dx, dz, since]) => [
        id,
        {
          kind, x, z, dx, dz,
          since: since === null ? at : at + since,
          go: (goes && goes.get(id)) || null,
        },
      ]),
    );
    this.control = controlMap(state.control);
    this.scores = new Map(state.scores.map(([id, score, mult]) => [id, { score, mult }]));
    this.floor = state.floor;
  }

  mark(kind, t, x, z, h, team) {
    this.marks.push({ kind, t, x, z, h, team });
  }

  teamOf(id) {
    const pilot = this.pilots.get(id);
    return pilot ? pilot.team : null;
  }

  // Apply one event; `track` says where pods are (for the marks of their deaths).
  apply(ev, track) {
    const a = ev.a;
    const t = ev.t;
    switch (ev.c) {
      case "join":
        this.pilots.set(a[0], { id: a[0], callsign: a[1], team: a[2], bot: !!a[3] });
        this.pods.delete(a[0]);
        break;
      case "leave":
        this.pilots.delete(a[0]);
        this.pods.delete(a[0]);
        this.scores.delete(a[0]);
        break;
      case "live": {
        const pod = this.pods.get(a[0]);
        if (pod) {
          pod.alive = true;
          pod.diedAt = null;
        } else {
          const fresh = { t, x: null, z: null, h: 0, s: 0, r: 0, f: 0, alive: true, diedAt: null };
          this.pods.set(a[0], fresh); // placed by its first report, at this time
        }
        break;
      }
      case "die": {
        const pod = this.pods.get(a[0]);
        if (pod) {
          const p = track ? track.podAt(this, a[0], t) : pod;
          if (p && p.x !== null) {
            Object.assign(pod, { t, x: p.x, z: p.z, h: p.h, s: 0, r: 0 });
            this.mark("die", t, p.x, p.z, p.h, this.teamOf(a[0]));
          }
          pod.alive = false;
          pod.diedAt = t;
        }
        break;
      }
      case "shot":
        this.mark("shot", t, a[1], a[2], a[3], this.teamOf(a[0]));
        break;
      case "gren":
        this.mark("gren", t, a[1], a[2], a[3], this.teamOf(a[0]));
        break;
      case "tower":
        this.towers.set(a[0], { team: a[1], x: a[2], z: a[3], mega: !!a[4] });
        break;
      case "mine":
        this.mines.set(a[0], { team: a[1], x: a[2], z: a[3] });
        break;
      case "npc":
        this.npcs.set(a[0], {
          kind: a[1], x: a[2], z: a[3], dx: null, dz: null, since: t, go: null,
        });
        break;
      case "go": {
        const npc = this.npcs.get(a[0]);
        if (npc) {
          const p = npcAt(npc, t);
          Object.assign(npc, { x: p.x, z: p.z, dx: a[1], dz: a[2], since: t, go: ev });
        }
        break;
      }
      case "arr": {
        const npc = this.npcs.get(a[0]);
        if (npc) Object.assign(npc, { x: a[1], z: a[2], dx: null, dz: null, go: null });
        break;
      }
      case "gone": {
        const thing = this.towers.get(a[0]) || this.mines.get(a[0]);
        const npc = this.npcs.get(a[0]);
        const p = thing || (npc && npcAt(npc, t));
        if (p) this.mark("gone", t, p.x, p.z, 0, thing ? thing.team : null);
        this.towers.delete(a[0]);
        this.mines.delete(a[0]);
        this.npcs.delete(a[0]);
        break;
      }
      case "ctrl":
        this.control = controlMap(a[0]);
        break;
      case "score":
        this.scores.set(a[0], { score: a[1], mult: a[2] });
        break;
      case "key":
        this.load(a[0], t, ev.goes);
        this.ended = null;
        break;
      case "end":
        this.load(EMPTY, t, null);
        this.ended = a[0];
        break;
      case "chat":
        this.chat.push({ t, id: a[0], style: a[1], text: a[2] });
        if (this.chat.length > CHAT_KEPT) this.chat.shift();
        break;
      default:
        break; // a code this viewer does not know: a later format's
    }
  }
}

export const EMPTY = {
  pilots: [], pods: [], towers: [], mines: [], npcs: [], control: [], scores: [], floor: 0,
};

function controlMap(rows) {
  return new Map(rows.map(([bx, bz, team]) => [bx + "," + bz, team]));
}

// A window (with a state) and the pieces that follow it: the city at any time from its start on.
// `keep: false` (a live stream) forgets what has been played, and never goes back.
export class Track {
  constructor(window, { keep = true } = {}) {
    if (!window.state) throw new Error("a track starts from a window with a state");
    this.start = window.start;
    this.end = window.end;
    this.state = window.state;
    this.keep = keep;
    this.reports = new Map();
    this.events = [];
    this.goes = new Map(); // NPC id -> the `go` (or a state's) still waiting for its arrival
    this.stateGoes = this.pending(window.state, window.start);
    this.world = new World(this.state, this.start, this.stateGoes);
    this.next = 0;
    this.add(window);
  }

  // A piece that follows (its state null). Its records may begin a little before the end of the
  // last one (a record that arrived late): they are put in their place, and an event before the
  // time already shown is applied at once.
  append(piece) {
    this.add(piece);
    this.end = Math.max(this.end, piece.end);
  }

  add(window) {
    const { reports, events } = decode(window);
    for (const [id, rows] of reports) {
      const held = this.reports.get(id);
      if (!held) {
        this.reports.set(id, rows);
        continue;
      }
      for (const row of rows) {
        const i = lastAtOrBefore(held, row.t);
        if (i >= 0 && held[i].t === row.t) continue; // had it already
        if (i === held.length - 1) held.push(row);
        else held.splice(i + 1, 0, row);
      }
    }
    for (const ev of events) {
      this.index(ev);
      const last = this.events.length - 1;
      if (last < 0 || this.events[last].t <= ev.t) {
        this.events.push(ev);
        continue;
      }
      let i = last;
      while (i >= 0 && this.events[i].t > ev.t) i--;
      this.events.splice(i + 1, 0, ev);
      if (i + 1 < this.next) {
        this.world.apply(ev, this);
        this.next++;
      }
    }
  }

  // Note each NPC's departure, to be given its arrival time when the track has it.
  index(ev) {
    const id = ev.a[0];
    switch (ev.c) {
      case "key":
        ev.goes = this.pending(ev.a[0], ev.t);
        break;
      case "go":
        ev.until = null;
        this.goes.set(id, ev);
        break;
      case "arr": {
        const go = this.goes.get(id);
        if (go) go.until = ev.t;
        this.goes.delete(id);
        break;
      }
      case "npc":
      case "gone":
        this.goes.delete(id);
        break;
      default:
        break;
    }
  }

  pending(state, at) {
    const goes = new Map();
    this.goes.clear();
    for (const n of state.npcs) {
      if (n[4] !== null) {
        const go = { t: at + n[6], until: null };
        goes.set(n[0], go);
        this.goes.set(n[0], go);
      }
    }
    return goes;
  }

  // The city at `t`.
  at(t) {
    if (t < this.world.t) {
      if (!this.keep) return this.world;
      this.world = new World(this.state, this.start, this.stateGoes);
      this.next = 0;
    }
    const world = this.world;
    while (this.next < this.events.length && this.events[this.next].t <= t) {
      world.apply(this.events[this.next++], this);
    }
    world.t = t;
    world.marks = world.marks.filter((m) => m.t <= t && t - m.t <= MARK_MS[m.kind]);
    if (!this.keep) this.forget(t);
    return world;
  }

  forget(t) {
    if (this.next > 200) {
      this.events.splice(0, this.next);
      this.next = 0;
    }
    for (const rows of this.reports.values()) {
      if (rows.length > 100) {
        const i = lastAtOrBefore(rows, t - BLEND_MS);
        if (i > 0) rows.splice(0, i);
      }
    }
  }

  // Where pod `id` of `world` is at `t` ({x, z, h, f}), or null if nowhere yet.
  podAt(world, id, t) {
    const pod = world.pods.get(id);
    if (!pod) return null;
    const rows = this.reports.get(id) || [];
    const i = lastAtOrBefore(rows, t);
    let a = i >= 0 ? rows[i] : null;
    if (!a || pod.t > a.t) a = pod;
    if (a.x === null) return null;
    const p = estimate(a, rows[i + 1] || null, t, pod.alive);
    p.f = a.f;
    return p;
  }
}
