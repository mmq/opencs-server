# 16. Maps are tracks decoded in the web process, from recordings and a live feed of the same records

## Context

The web front end (ADR 12) is to show a city's map: delayed by an hour for the public, as it happens
for administrators, and past games as replays. The recorder (ADR 14) and the archive (ADR 15) hold what
happened, but as wire payloads, mostly the clients' pod reports, which a browser cannot use and should
not have (they hold chat, which is for administrators), and a viewer needs a few numbers per pod, not
the protocol. A real-time map needs the city as it is played, which only the game server knows, on its
one loop (ADR 3), where a viewer that reads slowly must cost nothing.

## Decision

**One decoder, one source format.** The web process decodes records into a *track* in
`opencs_web.maps.track` (`CityTrack`): the pilots, each pod's place, heading, speed, turn rate and
state, the towers, mines and NPCs (Sentinels, guppies, Munchers), who controls which block (from the
towers, as the client works it out), and the scores; and, while it collects, each pod report and
events (shots, grenades, deaths with their killer, pods standing again, joins and leaves, towers and
mines placed, things destroyed, NPCs created, sent and arrived, control changes, scores, keyframes, the
end of a game; chat). Recorded games and the live feed are decoded by the same code, because the live
feed carries the same records.

It follows the server where the records leave a choice: a death's fly-over reports describe a camera
and are not the pod's place until its bare `'A'` or `DROP_REQUEST_TIMEOUT` (as `Pod.placed`); creating
packets sent to one late joiner (`go_live`) are copies of what is known, not moves; a keyframe rebuilds
the state.

**The track format** (version 1, JSON) is a *window*:

```
{"v": 1, "city": "B", "start": <UTC ms>, "end": <UTC ms>,
 "state": {...} or null,
 "pods": {"<id>": [t[], x[], z[], h[], s[], r[], f[]]},
 "ev": [[t, code, ...], ...]}
```

- `state` is the city at `start`: `pilots` `[id, callsign, team, bot]`, `pods` `[id, x, z, heading,
  speed, turn, state, alive]`, `towers` `[id, team, x, z, mega]`, `mines` `[id, team, x, z]`, `npcs`
  `[id, kind ("s", "g", "m"), x, z, dest x, dest z, ms since it set off (negative)]`, `control`
  `[block x, block z, team]`, `scores` `[id, score, multiplier]`, `floor`. A piece that follows one
  the viewer already has carries `null`.
- `pods` holds each pod's reports in `[start, end)` as columns, each delta-coded (a value is the one
  before plus the entry, starting from 0): time in ms since `start`, x and z in feet, heading in whole
  degrees (0 north, 90 east), speed in feet a second, turn rate in degrees a second (0 while airborne),
  state (0 ground, 1 airborne, 2 touchdown, 4 dropping).
- `ev` events, times in ms since `start`: `join id callsign team bot`, `leave id`, `live id`, `die id
  killer earned`, `shot shooter x z heading kind`, `gren id x z heading`, `tower id team x z mega`,
  `mine id team x z`, `npc id kind x z`, `go id x z`, `arr id x z`, `gone id`, `ctrl [[bx, bz, team]]`,
  `score id score multiplier`, `key state`, `end reason`, and `chat id style text` (administrators
  only, with `OPENCS_WEB_MAP_ADMIN_CHAT`).

Windows are whole minutes, decoded once into a bounded cache however many viewers ask, and cut to
each audience's limit when encoded. A busy 16-pod city is about 37 kB a minute of JSON, 8 kB
compressed (Caddy compresses it), against 39 kB of raw records.

**Delays are enforced by the web process.** Each audience has a delay (`OPENCS_WEB_MAP_DELAY_PUBLIC`,
3600 s by default; `..._ADMIN`, 0); a request is never answered with anything at or after now less its
delay: a window that starts after that is refused, one that ends after it is cut, a stream plays behind
it. A public delay of 0 is a configuration change, and makes the map a radar for the game being played.
Delayed tracks come from the archive and the segments not yet archived (a `.part` included, which is
readable to its last whole record).

**The live feed.** The service channel's `subscribe` (`opencs.session.livefeed`) streams one city's
records, as the recorder is given them, on the connection that asked: lines `{"feed", "epoch",
"data"}` whose base64 data, joined over an epoch, read as a segment file, the first line (`"start"`) a
header and a keyframe built from the world as it is. It works whether or not the city is being
recorded, since it taps the recorder's hooks, not its files: waiting for segments would add the
writer's delay and nothing could be watched with recording off. `[recording] chat` still decides the
chat it carries. The loop only appends to a queue of `SERVICE_FEED_ITEMS`; the connection's sender
encodes at most `SERVICE_FEED_BATCH` records a pass and waits on the reader. A reader that falls behind
loses the queue, nothing more is queued, and once it has caught up the next tick starts a new epoch
(`"start": "gap"` with the count dropped). The web subscribes once per city while anyone watches it,
on a connection of its own, decodes the feed once and sends every viewer the same pieces each second.

## Consequences

- Nothing a map shows needs the server: past windows need only the recordings, and with the public
  delayed an hour the public never reaches the service channel at all.
- A live feed costs the loop an append per record per subscriber and the encoding it would have cost a
  late joiner; a slow reader costs nothing but its own gaps. At most `SERVICE_MAX_FEEDS` at once.
- The router hears every event when the service channel is on (not only with a recorder), to feed
  subscribers; with neither on it does nothing extra (amending ADR 14's "with it off").
- The track is lossy by design (headings to the degree, nothing of power or armor) and versioned; the
  archive keeps everything, so a later format can be decoded from the same recordings.
- An hour-old map still shows towers that may still stand: accepted (the plan's risk list).
