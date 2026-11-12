// The map viewer: a city's track (maptrack.js) drawn on a canvas, top down with the RED home in the
// top left corner (north to the right, east down), with a timeline. The page (_viewer.html) gives the endpoints and the delay it plays at. The server never
// sends anything newer than its audience's delay; the viewer never plays nearer than the page's.
//
// Two modes: "live" follows now less the delay, from the stream (/maps/<L>/live), or from the
// minute windows if the stream is busy or missing; "replay" plays any time recorded, from the
// windows (/maps/<L>/track.json), fetching the next one ahead.

import { MARK_MS, Track, WINDOW_MS, npcAt, windowStart } from "./maptrack.js";

const LIVE_LAG = 2000; // ms the live playhead stays behind the newest time it may show
const PREFETCH_AFTER = 30000; // ms into a minute when the next one is fetched
const REFETCH_MS = 4000; // how often a minute cut short at the limit is asked for again
const RETRY_MS = 5000;
const CITIES_EVERY = 60000;
const TRACKS_KEPT = 4;
const STREAM_ERRORS = 3; // stream failures without a track in between before using the windows
const OPEN_SPAN_MS = 120000; // a span ending this near the limit is a game still going on
const SHOT_FEET = 250;
const TEAM_COLOURS = ["#d62728", "#1f5fd6", "#1a9a3a", "#1b1b1b"];
const NEUTRAL = "#777";
const BLOCK_SHADES = ["#e6e3da", "#d8d0bc", "#c9bd9d", "#b8a77c"];
const NPC_STYLE = { s: "#7b3fa0", m: "#e07b00", g: "#139a9a" };
const DEFAULT_MAP = {
  size: 4800, block: 600, blocks: null, values: [1, 2, 5, 10],
  teams: ["RED", "BLUE", "GREEN", "BLACK"], homes: [[0, 0], [7, 0], [0, 7], [7, 7]],
};
const RAD = Math.PI / 180;

function colour(team) {
  return team === null || team === undefined ? NEUTRAL : TEAM_COLOURS[team] || NEUTRAL;
}

function duration(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 120) return `${s} s`;
  if (s < 7200) return `${Math.round(s / 60)} min`;
  return `${Math.round(s / 3600)} h`;
}

function clock(t) {
  return new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
}

class Viewer {
  constructor(root) {
    const $ = (id) => document.getElementById(id);
    this.base = root.dataset.base;
    this.delay = Number(root.dataset.delay) * 1000;
    this.delayWords = root.dataset.delayWords;
    this.chatShown = root.dataset.chat === "1";
    this.ui = {
      city: $("map-city"), note: $("map-note"), canvas: $("map-canvas"), play: $("map-play"),
      back: $("map-back"), forward: $("map-forward"), live: $("map-live"), speed: $("map-speed"),
      time: $("map-time"), seek: $("map-seek"), zoomIn: $("map-zoom-in"),
      zoomOut: $("map-zoom-out"), zoomReset: $("map-zoom-reset"),
      scores: $("map-scores").tBodies[0], chat: $("map-chat"),
    };
    this.ctx = this.ui.canvas.getContext("2d");
    this.offset = 0; // the server's clock less ours
    this.cities = [];
    this.city = null;
    this.spans = [];
    this.map = DEFAULT_MAP;
    this.mode = "live";
    this.playing = false;
    this.speed = 1;
    this.t = 0;
    this.tracks = new Map(); // minute start -> {track, partial, loading, fetched, retryAt}
    this.stream = null; // {source, track, errors}
    this.streamFailed = false;
    this.shown = null; // the track drawn last
    this.view = { zoom: 1, x: 2400, z: 2400 };
    this.seeking = false;
    this.lastFrame = 0;
    this.lastPanel = 0;
    this.panelKey = "";
    this.chatKey = "";
    root.hidden = false;
    this.wire();
    this.resize();
    this.loadCities(true);
    setInterval(() => document.hidden || this.loadCities(false), CITIES_EVERY);
    requestAnimationFrame((ts) => this.frame(ts));
  }

  // --- time ------------------------------------------------------------------------------------

  now() {
    return Date.now() + this.offset;
  }

  limit() {
    return this.now() - this.delay;
  }

  first() {
    if (this.spans.length) return this.spans[0][0];
    if (this.stream && this.stream.track) return this.stream.track.start;
    return this.limit() - WINDOW_MS;
  }

  // The span holding `t`, or the start of the next one (null past the last).
  spanAt(t) {
    for (const [s, e] of this.spans) {
      if (t < s) return { next: s };
      if (t < e) return { in: true };
    }
    return null;
  }

  // --- the cities and the map ------------------------------------------------------------------

  async loadCities(first) {
    let data;
    try {
      const response = await fetch(`${this.base}cities.json`, { cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      data = await response.json();
    } catch {
      this.note("The maps are not available just now; trying again shortly.");
      if (first) setTimeout(() => this.loadCities(true), RETRY_MS * 3);
      return;
    }
    this.offset = data.now - Date.now();
    this.cities = data.cities;
    const select = this.ui.city;
    const chosen = this.city || location.hash.slice(1).toUpperCase();
    select.replaceChildren(
      ...data.cities.map((c) => new Option(c.name ? `${c.city}: ${c.name}` : c.city, c.city)),
    );
    this.setSpans(data);
    if (!data.cities.length) {
      this.note("No city has been recorded yet.");
      return;
    }
    if (first || !this.city) {
      const pick = data.cities.find((c) => c.city === chosen) || this.busiest(data.cities);
      this.choose(pick.city);
    }
    select.value = this.city;
  }

  // The city recorded most lately (a game going on, most likely), or the first one live.
  busiest(cities) {
    const end = (c) => (c.spans.length ? c.spans[c.spans.length - 1][1] : -1);
    const recorded = cities.filter((c) => c.spans.length).sort((a, b) => end(b) - end(a));
    return recorded[0] || cities.find((c) => c.live) || cities[0];
  }

  setSpans(data) {
    const city = data.cities.find((c) => c.city === this.city);
    this.spans = (city ? city.spans : []).map(([s, e]) => [s, e]);
    const last = this.spans[this.spans.length - 1];
    if (last && last[1] >= data.limit - OPEN_SPAN_MS) last[1] = Infinity; // still being played
  }

  async choose(letter) {
    this.closeStream();
    this.tracks.clear();
    this.city = letter;
    this.shown = null;
    history.replaceState(null, "", `#${letter}`);
    const city = this.cities.find((c) => c.city === letter);
    this.setSpans({ cities: this.cities, limit: this.limit() });
    this.map = DEFAULT_MAP;
    try {
      const response = await fetch(`${this.base}${letter}/map.json`);
      if (response.ok) this.map = { ...DEFAULT_MAP, ...(await response.json()) };
    } catch {
      // the default map: all blocks worth 1
    }
    if (this.city !== letter) return;
    this.view = { zoom: 1, x: this.map.size / 2, z: this.map.size / 2 };
    const going = this.delay === 0 ? city && city.live : this.spans.some((s) => s[1] === Infinity);
    if (going) {
      this.goLive();
    } else if (this.spans.length) {
      const [start, end] = this.spans[this.spans.length - 1];
      this.mode = "replay";
      this.playing = false;
      this.t = Math.max(Math.min(end, this.limit() - LIVE_LAG) - WINDOW_MS, start);
      this.note("No game is going on in this city now: here is the end of the last one recorded.");
    } else {
      this.mode = "replay";
      this.playing = false;
      this.note("Nothing has been recorded in this city yet.");
    }
  }

  // --- the stream (live mode) ------------------------------------------------------------------

  goLive() {
    this.mode = "live";
    this.playing = true;
    this.t = this.limit() - LIVE_LAG;
    this.note(this.delay ? `Following the game, ${this.delayWords} behind.` : "Live.");
    if (!this.stream && !this.streamFailed && window.EventSource && !document.hidden) {
      this.openStream();
    }
  }

  openStream() {
    const asked = this.delay ? `?delay=${this.delay / 1000}` : "";
    const source = new EventSource(`${this.base}${this.city}/live${asked}`);
    const stream = { source, track: null, errors: 0 };
    this.stream = stream;
    source.addEventListener("track", (e) => {
      const data = JSON.parse(e.data);
      stream.errors = 0;
      if (data.state) stream.track = new Track(data, { keep: false });
      else if (stream.track) stream.track.append(data);
    });
    source.addEventListener("busy", () => {
      this.closeStream();
      this.streamFailed = true; // the windows will do; the stream is tried again on a new city
      setTimeout(() => { this.streamFailed = false; }, 30000);
    });
    source.addEventListener("error", () => {
      stream.errors++;
      if (stream.errors >= STREAM_ERRORS) {
        this.closeStream();
        this.streamFailed = true;
        setTimeout(() => { this.streamFailed = false; }, 30000);
      }
    });
  }

  closeStream() {
    if (this.stream) this.stream.source.close();
    this.stream = null;
  }

  // --- the windows (replay, and live without the stream) ---------------------------------------

  windowFor(t) {
    const start = windowStart(t);
    const held = this.tracks.get(start);
    const now = performance.now();
    if (!held || (!held.track && !held.loading && now >= held.retryAt)) {
      this.fetchWindow(start);
    } else if (held.partial && t > held.track.end - 1000 && now - held.fetched > REFETCH_MS) {
      this.fetchWindow(start);
    }
    const next = start + WINDOW_MS;
    if (t - start > PREFETCH_AFTER && next < this.limit() && !this.tracks.has(next)) {
      this.fetchWindow(next);
    }
    return held ? held.track : null;
  }

  async fetchWindow(start) {
    const held = this.tracks.get(start) || { track: null, partial: false, fetched: 0, retryAt: 0 };
    if (held.loading || performance.now() < held.retryAt || document.hidden) return;
    held.loading = true;
    this.tracks.set(start, held);
    for (const key of [...this.tracks.keys()]) {
      if (this.tracks.size <= TRACKS_KEPT) break;
      if (Math.abs(key - windowStart(this.t)) > WINDOW_MS) this.tracks.delete(key);
    }
    const city = this.city;
    try {
      const url = `${this.base}${city}/track.json?t=${start}`;
      const response = await fetch(url, { cache: "no-store" });
      const body = await response.json();
      if (this.city !== city) return;
      if (response.ok) {
        held.track = new Track(body);
        held.partial = body.end < start + WINDOW_MS;
        held.fetched = performance.now();
      } else {
        // too-recent (the clock is ahead of the server's), rate, or nothing there: wait
        held.retryAt = performance.now() + (response.status === 403 ? 2000 : RETRY_MS);
        if (response.status >= 500) this.note("The map could not be loaded; trying again.");
      }
    } catch {
      held.retryAt = performance.now() + RETRY_MS;
      this.note("The map could not be loaded; trying again.");
    } finally {
      held.loading = false;
    }
  }

  // --- playing ---------------------------------------------------------------------------------

  seek(t, playing = this.playing) {
    const limit = this.limit();
    if (t >= limit - LIVE_LAG) {
      this.goLive();
      return;
    }
    this.closeStream();
    this.mode = "replay";
    this.playing = playing;
    this.t = Math.max(this.first(), t);
    this.note(this.playing ? "Playing back." : "Paused.");
  }

  toggle() {
    if (this.mode === "live") {
      this.seek(this.t, false);
    } else {
      this.playing = !this.playing;
      this.note(this.playing ? "Playing back." : "Paused.");
    }
  }

  // Move the playhead on and say which track to draw.
  advance(dt) {
    if (!this.city) return null;
    const limit = this.limit();
    if (this.mode === "live") {
      const target = limit - LIVE_LAG;
      const live = this.stream && this.stream.track;
      if (live) {
        let t = this.t + dt;
        if (Math.abs(target - t) > 3000) t = target;
        this.t = Math.max(Math.min(t, target, live.end), live.start);
        return live;
      }
      this.t = target;
    } else if (this.playing) {
      this.t += dt * this.speed;
      if (this.t >= limit - LIVE_LAG) {
        this.goLive();
        return this.advance(0);
      }
      const span = this.spanAt(this.t);
      if (span && span.next) {
        this.t = span.next; // over a gap: nothing was recorded there
      } else if (!span && this.spans.length) {
        this.t = this.spans[this.spans.length - 1][1];
        this.playing = false;
        this.note("The end of what was recorded.");
      }
    }
    this.t = Math.min(this.t, limit);
    const track = this.windowFor(this.t);
    if (track && this.t >= track.end) {
      this.t = track.end; // the rest is not in yet
      if (this.mode === "replay" && this.playing && track.end < track.start + WINDOW_MS) {
        this.goLive(); // played up to the newest time there is: follow it from now on
      }
    }
    return track;
  }

  frame(ts) {
    const dt = this.lastFrame ? Math.min(ts - this.lastFrame, 250) : 0;
    this.lastFrame = ts;
    const track = this.advance(dt);
    let world = null;
    if (track) {
      if (track !== this.shown && this.shown && track.start >= this.shown.start) {
        // on into the next minute: keep what is still drawn of the last one
        const last = this.shown.world;
        track.world.marks = last.marks.concat(track.world.marks);
        track.world.chat = last.chat.concat(track.world.chat);
      }
      this.shown = track;
      world = track.at(this.t);
    }
    this.draw(world, track, !this.city || (!this.spans.length && this.mode === "replay"));
    if (ts - this.lastPanel > 250) {
      this.lastPanel = ts;
      this.panel(world);
    }
    requestAnimationFrame((next) => this.frame(next));
  }

  // --- the page around the map -----------------------------------------------------------------

  note(text) {
    if (this.ui.note.textContent !== text) this.ui.note.textContent = text;
  }

  panel(world) {
    const { play, time, seek } = this.ui;
    const playing = this.mode === "live" || this.playing;
    play.textContent = playing ? "Pause" : "Play";
    play.setAttribute("aria-pressed", String(playing));
    this.ui.live.disabled = this.mode === "live";
    const behind = this.now() - this.t;
    const when = this.t ? clock(this.t) : "";
    time.textContent = this.mode === "live"
      ? `${when} (${this.delay ? `${this.delayWords} behind` : `live, ${duration(behind)} behind`})`
      : when;
    time.dataset.t = String(Math.round(this.t)); // the playhead, UTC ms, for tests and tools
    if (!this.seeking) {
      seek.min = String(this.first());
      seek.max = String(Math.max(this.limit(), this.first() + 1000));
      seek.value = String(this.t);
      seek.setAttribute("aria-valuetext", when);
    }
    this.scores(world);
    this.chat(world);
  }

  scores(world) {
    const rows = world
      ? [...world.pilots.values()].map((p) => ({ ...p, ...(world.scores.get(p.id) || {}) }))
      : [];
    rows.sort((a, b) => a.team - b.team || (b.score || 0) - (a.score || 0) || a.id - b.id);
    const key = JSON.stringify(rows);
    if (key === this.panelKey) return;
    this.panelKey = key;
    const teams = this.map.teams;
    this.ui.scores.replaceChildren(
      ...rows.map((p) => {
        const tr = document.createElement("tr");
        const cells = [
          teams[p.team] || String(p.team),
          p.callsign,
          p.score === undefined ? "–" : String(p.score),
          p.mult === undefined ? "–" : `×${p.mult}`,
        ];
        for (const text of cells) {
          const td = document.createElement("td");
          td.textContent = text; // never markup: callsigns are what players chose
          tr.append(td);
        }
        tr.className = `team-${p.team}`;
        return tr;
      }),
    );
  }

  chat(world) {
    const list = this.ui.chat;
    if (!this.chatShown || !list) return; // the public's page never shows chat
    const lines = world ? world.chat.filter((c) => c.t <= this.t).slice(-20) : [];
    const key = lines.length ? `${lines.length}:${lines[lines.length - 1].t}` : "";
    if (key === this.chatKey) return;
    this.chatKey = key;
    list.replaceChildren(
      ...lines.map((c) => {
        const li = document.createElement("li");
        const pilot = c.id === null ? null : world.pilots.get(c.id);
        const who = c.id === null ? "(server)" : pilot ? pilot.callsign : `#${c.id}`;
        li.textContent = `${new Date(c.t).toLocaleTimeString()} ${who}: ${c.text}`;
        return li;
      }),
    );
  }

  wire() {
    const ui = this.ui;
    ui.city.addEventListener("change", () => this.choose(ui.city.value));
    ui.play.addEventListener("click", () => this.toggle());
    ui.back.addEventListener("click", () => this.seek(this.t - 10000));
    ui.forward.addEventListener("click", () => this.seek(this.t + 10000));
    ui.live.addEventListener("click", () => this.goLive());
    ui.speed.addEventListener("change", () => { this.speed = Number(ui.speed.value); });
    ui.seek.addEventListener("pointerdown", () => { this.seeking = true; });
    ui.seek.addEventListener("pointerup", () => { this.seeking = false; });
    ui.seek.addEventListener("change", () => { this.seeking = false; });
    ui.seek.addEventListener("input", () => this.seek(Number(ui.seek.value)));
    ui.zoomIn.addEventListener("click", () => this.zoom(1.5));
    ui.zoomOut.addEventListener("click", () => this.zoom(1 / 1.5));
    ui.zoomReset.addEventListener("click", () => {
      this.view = { zoom: 1, x: this.map.size / 2, z: this.map.size / 2 };
    });
    document.addEventListener("keydown", (e) => this.key(e));
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        this.closeStream(); // nothing is fetched while the tab is hidden
      } else if (this.mode === "live") {
        this.loadCities(false);
        this.goLive();
      }
    });
    window.addEventListener("resize", () => this.resize());
    const canvas = ui.canvas;
    let drag = null;
    canvas.addEventListener("pointerdown", (e) => {
      drag = { x: e.clientX, y: e.clientY };
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const k = this.scale();
      this.pan(-(e.clientY - drag.y) / k, -(e.clientX - drag.x) / k);
      drag = { x: e.clientX, y: e.clientY };
    });
    canvas.addEventListener("pointerup", () => { drag = null; });
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      this.zoom(Math.exp(-e.deltaY * 0.0015), e.clientX - rect.left, e.clientY - rect.top);
    }, { passive: false });
  }

  key(e) {
    const tag = e.target.tagName;
    const typing = tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
    if (typing || e.altKey || e.ctrlKey || e.metaKey) return;
    const step = e.shiftKey ? 60000 : 10000;
    switch (e.key) {
      case " ":
      case "k":
        if (tag === "BUTTON") return; // the button's own click
        this.toggle();
        break;
      case "ArrowLeft":
      case "j":
        this.seek(this.t - step);
        break;
      case "ArrowRight":
        this.seek(this.t + step);
        break;
      case "l":
      case "L":
        this.goLive();
        break;
      case "+":
      case "=":
        this.zoom(1.5);
        break;
      case "-":
        this.zoom(1 / 1.5);
        break;
      default:
        return;
    }
    e.preventDefault();
  }

  // --- the view --------------------------------------------------------------------------------

  resize() {
    const canvas = this.ui.canvas;
    const ratio = window.devicePixelRatio || 1;
    const side = Math.max(200, Math.round(canvas.clientWidth));
    canvas.width = Math.round(side * ratio);
    canvas.height = Math.round(side * ratio);
    this.side = side;
    this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  scale() {
    return (this.side / this.map.size) * this.view.zoom;
  }

  // Screen point of a city point (x east, z north): z grows to the right and x down, so block
  // (0, 0), RED's home, is in the top left corner.
  at(x, z) {
    const k = this.scale();
    return [(z - this.view.z) * k + this.side / 2, (x - this.view.x) * k + this.side / 2];
  }

  pan(dx, dz) {
    const size = this.map.size;
    this.view.x = Math.min(Math.max(this.view.x + dx, 0), size);
    this.view.z = Math.min(Math.max(this.view.z + dz, 0), size);
  }

  zoom(by, sx = this.side / 2, sy = this.side / 2) {
    const k = this.scale();
    const x = this.view.x + (sy - this.side / 2) / k;
    const z = this.view.z + (sx - this.side / 2) / k;
    this.view.zoom = Math.min(Math.max(this.view.zoom * by, 1), 10);
    const k2 = this.scale();
    this.view.x = x - (sy - this.side / 2) / k2;
    this.view.z = z - (sx - this.side / 2) / k2;
    this.pan(0, 0);
  }

  // --- drawing ---------------------------------------------------------------------------------

  draw(world, track, blank) {
    const c = this.ctx;
    const map = this.map;
    const k = this.scale();
    c.fillStyle = "#f6f4ee";
    c.fillRect(0, 0, this.side, this.side);
    const n = Math.round(map.size / map.block);
    c.font = "11px system-ui, sans-serif";
    for (let bz = 0; bz < n; bz++) {
      for (let bx = 0; bx < n; bx++) {
        const cls = map.blocks ? map.blocks[bz][bx] : 0;
        const [x, y] = this.at(bx * map.block, bz * map.block);
        const side = map.block * k;
        const street = Math.max(1, side * 0.04);
        c.fillStyle = BLOCK_SHADES[cls] || BLOCK_SHADES[0];
        c.fillRect(x + street, y + street, side - 2 * street, side - 2 * street);
        const team = world ? world.control.get(`${bx},${bz}`) : undefined;
        if (team !== undefined) {
          c.globalAlpha = 0.3;
          c.fillStyle = colour(team);
          c.fillRect(x + street, y + street, side - 2 * street, side - 2 * street);
          c.globalAlpha = 1;
        }
        const value = map.values[cls];
        if (value > 1) {
          c.fillStyle = "#5a5040";
          c.fillText(String(value), x + street + 3, y + street + 12);
        }
      }
    }
    map.homes.forEach(([bx, bz], team) => {
      const [x, y] = this.at(bx * map.block, bz * map.block);
      c.strokeStyle = colour(team);
      c.lineWidth = 3;
      c.strokeRect(x + 2, y + 2, map.block * k - 4, map.block * k - 4);
    });
    if (!world) {
      this.caption(blank ? "No map to show" : "Loading…");
      return;
    }
    const t = this.t;
    for (const m of world.mines.values()) this.cross(m.x, m.z, 4, colour(m.team), 2);
    for (const tower of world.towers.values()) {
      const [x, y] = this.at(tower.x, tower.z);
      const r = tower.mega ? 7 : 5;
      c.fillStyle = colour(tower.team);
      c.strokeStyle = "#fff";
      c.lineWidth = 1.5;
      c.fillRect(x - r, y - r, 2 * r, 2 * r);
      c.strokeRect(x - r, y - r, 2 * r, 2 * r);
    }
    for (const npc of world.npcs.values()) this.npc(npc, npcAt(npc, t));
    for (const mark of world.marks) this.mark(mark, t);
    const labels = [];
    for (const [id, pod] of world.pods) {
      if (!pod.alive) continue;
      const p = track.podAt(world, id, t);
      if (!p) continue;
      const pilot = world.pilots.get(id);
      this.pod(p, colour(pilot ? pilot.team : null));
      labels.push([p, pilot ? pilot.callsign : `#${id}`]);
    }
    c.font = "11px system-ui, sans-serif";
    c.lineWidth = 3;
    c.strokeStyle = "rgba(255,255,255,0.85)";
    c.fillStyle = "#111";
    for (const [p, text] of labels) {
      const [x, y] = this.at(p.x, p.z);
      c.strokeText(text, x + 8, y - 7); // canvas text: drawn as letters, never read as markup
      c.fillText(text, x + 8, y - 7);
    }
    if (world.ended) this.caption(`The game ended (${world.ended}).`);
    else if (this.mode === "replay" && this.spanAt(t) === null) this.caption("Nothing recorded");
  }

  caption(text) {
    const c = this.ctx;
    c.font = "bold 15px system-ui, sans-serif";
    const w = c.measureText(text).width;
    c.fillStyle = "rgba(255,255,255,0.85)";
    c.fillRect(this.side / 2 - w / 2 - 10, this.side / 2 - 18, w + 20, 30);
    c.fillStyle = "#222";
    c.fillText(text, this.side / 2 - w / 2, this.side / 2 + 2);
  }

  cross(px, pz, r, style, width) {
    const c = this.ctx;
    const [x, y] = this.at(px, pz);
    c.strokeStyle = style;
    c.lineWidth = width;
    c.beginPath();
    c.moveTo(x - r, y - r);
    c.lineTo(x + r, y + r);
    c.moveTo(x + r, y - r);
    c.lineTo(x - r, y + r);
    c.stroke();
  }

  pod(p, style) {
    const c = this.ctx;
    const [x, y] = this.at(p.x, p.z);
    const dx = Math.cos(p.h * RAD); // heading 0 (+z) to the right, 90 (+x) down
    const dy = Math.sin(p.h * RAD);
    c.beginPath();
    c.moveTo(x + dx * 9, y + dy * 9);
    c.lineTo(x - dx * 6 - dy * 5, y - dy * 6 + dx * 5);
    c.lineTo(x - dx * 6 + dy * 5, y - dy * 6 - dx * 5);
    c.closePath();
    c.fillStyle = style;
    c.fill();
    c.strokeStyle = "#fff";
    c.lineWidth = 1.5;
    c.stroke();
    if (p.f === 4) {
      c.beginPath();
      c.arc(x, y, 12, 0, 2 * Math.PI);
      c.strokeStyle = style;
      c.stroke();
    }
  }

  npc(npc, p) {
    const c = this.ctx;
    const [x, y] = this.at(p.x, p.z);
    c.fillStyle = NPC_STYLE[npc.kind] || NEUTRAL;
    c.strokeStyle = "#fff";
    c.lineWidth = 1.5;
    c.beginPath();
    if (npc.kind === "s") {
      c.moveTo(x, y - 7);
      c.lineTo(x + 7, y);
      c.lineTo(x, y + 7);
      c.lineTo(x - 7, y);
      c.closePath();
    } else {
      c.arc(x, y, npc.kind === "m" ? 6 : 4, 0, 2 * Math.PI);
    }
    c.fill();
    c.stroke();
  }

  mark(m, t) {
    const c = this.ctx;
    const age = (t - m.t) / MARK_MS[m.kind];
    c.globalAlpha = Math.max(0, 1 - age);
    if (m.kind === "shot") {
      const [x, y] = this.at(m.x, m.z);
      const [x2, y2] = this.at(
        m.x + Math.sin(m.h * RAD) * SHOT_FEET,
        m.z + Math.cos(m.h * RAD) * SHOT_FEET,
      );
      c.strokeStyle = colour(m.team);
      c.lineWidth = 2;
      c.beginPath();
      c.moveTo(x, y);
      c.lineTo(x2, y2);
      c.stroke();
    } else if (m.kind === "die") {
      this.cross(m.x, m.z, 8, "#c00000", 3);
    } else {
      const [x, y] = this.at(m.x, m.z);
      c.strokeStyle = m.kind === "gren" ? "#444" : colour(m.team);
      c.lineWidth = 2;
      c.beginPath();
      c.arc(x, y, m.kind === "gren" ? 5 : 6 + 14 * age, 0, 2 * Math.PI);
      c.stroke();
    }
    c.globalAlpha = 1;
  }
}

const root = document.getElementById("viewer");
if (root && document.getElementById("map-canvas").getContext) {
  new Viewer(root);
}
