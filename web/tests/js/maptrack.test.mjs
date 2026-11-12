// The map viewer's track logic (opencs_web/assets/maptrack.js), run by `node --test`. The windows
// come from the recorded fixtures: test_map_viewer.py writes them to $OPENCS_MAP_WINDOWS (a
// directory of JSON files, each a minute of the track as /maps/<L>/track.json serves it).

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import {
  AHEAD_MS, EMPTY, NPC_SPEED, Track, decode, estimate, npcAt, reckon,
} from "../../src/opencs_web/assets/maptrack.js";

const DIR = process.env.OPENCS_MAP_WINDOWS;
const windows = DIR
  ? readdirSync(DIR).filter((n) => n.endsWith(".json")).sort()
    .map((n) => JSON.parse(readFileSync(join(DIR, n), "utf8")))
  : [];

function near(p, q, feet = 1e-6) {
  return Math.hypot(p.x - q.x, p.z - q.z) <= feet;
}

function deltas(values) {
  let last = 0;
  return values.map((v) => {
    const d = v - last;
    last = v;
    return d;
  });
}

// A window [start, end) of `window`'s reports and events, as the server encodes one.
function cut(window, start, end, state) {
  const { reports, events } = decode(window);
  const pods = {};
  for (const [id, rows] of reports) {
    const kept = rows.filter((r) => r.t >= start && r.t < end);
    if (kept.length) {
      pods[id] = [
        deltas(kept.map((r) => r.t - start)),
        ...["x", "z", "h", "s", "r", "f"].map((k) => deltas(kept.map((r) => r[k]))),
      ];
    }
  }
  const ev = events.filter((e) => e.t >= start && e.t < end).map((e) => [e.t - start, e.c, ...e.a]);
  return { v: 1, city: window.city, start, end, state, pods, ev };
}

test("the fixtures' windows are there", () => {
  assert.ok(DIR, "OPENCS_MAP_WINDOWS is not set");
  assert.ok(windows.length >= 2);
});

test("a window's columns are its reports", () => {
  for (const w of windows) {
    const { reports, events } = decode(w);
    for (const [id, rows] of reports) {
      assert.equal(rows.length, w.pods[String(id)][0].length);
      for (let i = 1; i < rows.length; i++) assert.ok(rows[i].t >= rows[i - 1].t);
      assert.ok(rows.every((r) => r.t >= w.start && r.t < w.end));
    }
    assert.equal(events.length, w.ev.length);
  }
});

test("a pod's drawn path passes through every report, and close to the line between them", () => {
  let checked = 0;
  for (const w of windows) {
    const track = new Track(w);
    for (const [id, rows] of decode(w).reports) {
      for (let i = 0; i < rows.length; i++) {
        const r = rows[i];
        const world = track.at(r.t);
        const pod = world.pods.get(id);
        if (!pod || !pod.alive) continue;
        assert.ok(near(track.podAt(world, id, r.t), r), `pod ${id} at ${r.t}`);
        checked++;
        const b = rows[i + 1];
        if (b && b.t - r.t <= 1000 && b.f === 0 && r.f === 0) {
          const mid = track.podAt(track.at((r.t + b.t) / 2), id, (r.t + b.t) / 2);
          const step = Math.hypot(b.x - r.x, b.z - r.z);
          assert.ok(near(mid, { x: (r.x + b.x) / 2, z: (r.z + b.z) / 2 }, step / 2 + 5));
        }
      }
    }
  }
  assert.ok(checked > 1000, `${checked} reports`);
});

test("going back gives the city as it was", () => {
  const w = windows[0];
  const track = new Track(w);
  const t1 = w.start + 30000;
  track.at(w.end - 1);
  const again = track.at(t1);
  const fresh = new Track(w).at(t1);
  assert.deepEqual([...again.pilots.keys()], [...fresh.pilots.keys()]);
  assert.deepEqual([...again.pods.keys()], [...fresh.pods.keys()]);
  assert.deepEqual([...again.scores], [...fresh.scores]);
});

test("a key event replaces the whole city", () => {
  const w = windows.find((x) => x.ev.some((e) => e[1] === "key"));
  const key = w.ev.find((e) => e[1] === "key");
  const track = new Track(w);
  const before = track.at(w.start + key[0] - 1);
  assert.equal(before.npcs.size, w.state.npcs.length);
  const after = track.at(w.start + key[0]);
  assert.deepEqual([...after.pilots.keys()], key[2].pilots.map((p) => p[0]));
  assert.deepEqual([...after.npcs.keys()], key[2].npcs.map((n) => n[0]));
});

// The pieces of a live stream: each second, what arrived in it; every seventh record arrives
// 300 ms late, so a piece may begin before the last one ended.
function pieces(window) {
  const { reports, events } = decode(window);
  const arrived = (t, i) => t + (i % 7 === 0 ? 300 : 0);
  const out = [];
  const piece = (t) => {
    const k = Math.floor((t - window.start) / 1000);
    while (out.length <= k) out.push({ rows: new Map(), ev: [] });
    return out[k];
  };
  let i = 0;
  for (const [id, rows] of reports) {
    for (const r of rows) {
      const p = piece(arrived(r.t, i++));
      if (!p.rows.has(id)) p.rows.set(id, []);
      p.rows.get(id).push(r);
    }
  }
  for (const e of events) piece(arrived(e.t, i++)).ev.push(e);
  return out.map((p, k) => {
    const times = [...[...p.rows.values()].flat().map((r) => r.t), ...p.ev.map((e) => e.t)];
    const start = k === 0 ? window.start : Math.min(window.start + k * 1000, ...times);
    const pods = {};
    for (const [id, rows] of p.rows) {
      rows.sort((a, b) => a.t - b.t);
      pods[id] = [
        deltas(rows.map((r) => r.t - start)),
        ...["x", "z", "h", "s", "r", "f"].map((c) => deltas(rows.map((r) => r[c]))),
      ];
    }
    p.ev.sort((a, b) => a.t - b.t);
    const ev = p.ev.map((e) => [e.t - start, e.c, ...e.a]);
    const end = window.start + (k + 1) * 1000;
    return { v: 1, city: window.city, start, end, state: k === 0 ? window.state : null, pods, ev };
  });
}

test("a stream of one-second pieces, some records late, draws as the whole minute does", () => {
  let checked = 0;
  for (const w of windows) {
    const whole = new Track(w);
    const [first, ...rest] = pieces(w);
    const live = new Track(first, { keep: false });
    for (const piece of rest) {
      live.append(piece);
      live.append(cut(w, piece.start, piece.start, null)); // an empty piece changes nothing
      // drawn a little behind what arrived, so a pod's next report is in hand, as the viewer does
      const t = piece.end - 1 - 1300;
      if (t < w.start) continue;
      const a = whole.at(t);
      const b = live.at(t);
      assert.deepEqual([...b.pilots.keys()], [...a.pilots.keys()], `pilots at ${t}`);
      assert.deepEqual([...b.pods.keys()], [...a.pods.keys()], `pods at ${t}`);
      for (const id of a.pods.keys()) {
        const p = whole.podAt(a, id, t);
        const q = live.podAt(b, id, t);
        assert.equal(p === null, q === null);
        if (p) {
          assert.ok(near(p, q, 1e-6), `pod ${id} at ${t}`);
          checked++;
        }
      }
    }
    // a report sent twice is kept once
    const again = cut(w, w.end - 1000, w.end, null);
    again.ev = [];
    const before = [...live.reports.values()].reduce((n, rows) => n + rows.length, 0);
    live.append(again);
    assert.equal([...live.reports.values()].reduce((n, rows) => n + rows.length, 0), before);
  }
  assert.ok(checked > 1000, `${checked} positions`);
});

test("a pod is never moved on more than AHEAD_MS past its last report", () => {
  const a = { t: 1000, x: 100, z: 100, h: 90, s: 30, r: 0, f: 0 };
  assert.ok(near(estimate(a, null, 1000 + AHEAD_MS, true), { x: 130, z: 100 }));
  assert.ok(near(estimate(a, null, 60000, true), { x: 130, z: 100 }));
  assert.ok(near(estimate(a, null, 500, true), a)); // before it: where it was
  assert.ok(near(estimate(a, null, 60000, false), a)); // dead: never moves
  // a report far from where the last led is jumped to, not slid to
  const far = { t: 1500, x: 2000, z: 2000, h: 0, s: 0, r: 0, f: 0 };
  assert.ok(near(estimate(a, far, 1250, true), { x: 107.5, z: 100 }));
});

test("dead reckoning turns at the turn rate", () => {
  const p = { t: 0, x: 1000, z: 1000, h: 0, s: 30, r: 36, f: 0 };
  assert.ok(near(reckon(p, 10), p, 1e-6)); // a whole circle in 10 s
  const half = reckon(p, 5);
  assert.ok(Math.abs(half.h - 180) < 1e-9);
  assert.ok(near(half, { x: 1000 + (2 * 30 * 10) / (2 * Math.PI), z: 1000 }, 1e-6));
  assert.ok(near(reckon({ ...p, r: 0 }, 2), { x: 1000, z: 1060 }));
});

test("an NPC goes from where it was to where it was sent, arriving when the track says", () => {
  const state = { ...EMPTY, npcs: [[300, "m", 0, 0, null, null, null]] };
  const ev = [[100, "go", 300, 1000, 0]];
  const w = { v: 1, city: "B", start: 0, end: 1000, state, pods: {}, ev };
  const track = new Track(w, { keep: false });
  let world = track.at(600);
  // no arrival yet: at the assumed speed
  assert.ok(near(npcAt(world.npcs.get(300), 600), { x: (NPC_SPEED * 500) / 1000, z: 0 }));
  track.append({ start: 1000, end: 2000, state: null, pods: {}, ev: [[100, "arr", 300, 1000, 0]] });
  world = track.at(1100 - 1);
  assert.ok(near(npcAt(world.npcs.get(300), 600), { x: 500, z: 0 }));
  world = track.at(1500);
  assert.ok(near(npcAt(world.npcs.get(300), 1500), { x: 1000, z: 0 }));
  // one already on its way in a state: its departure is relative to the state's time
  const moving = { ...EMPTY, npcs: [[301, "s", 0, 0, 0, 800, -500]] };
  const arrives = [[500, "arr", 301, 0, 800]];
  const m = new Track({ start: 10000, end: 11000, state: moving, pods: {}, ev: arrives });
  assert.ok(near(npcAt(m.at(10250).npcs.get(301), 10250), { x: 0, z: 600 }));
});

test("deaths, shots and destructions are marked for a while", () => {
  const state = {
    ...EMPTY,
    pilots: [[1, "ACE", 0, 0]],
    pods: [[1, 500, 500, 0, 0, 0, 0, 1]],
    towers: [[9000, 1, 1200, 1200, 0]],
  };
  const ev = [[100, "shot", 1, 500, 500, 90, 0], [200, "die", 1, null, 0], [300, "gone", 9000]];
  const track = new Track({ start: 0, end: 10000, state, pods: {}, ev });
  let world = track.at(400);
  assert.deepEqual(world.marks.map((m) => m.kind), ["shot", "die", "gone"]);
  assert.equal(world.pods.get(1).alive, false);
  assert.equal(world.towers.size, 0);
  world = track.at(3000);
  assert.deepEqual(world.marks.map((m) => m.kind), ["die"]);
  assert.equal(track.at(9000).marks.length, 0);
});
