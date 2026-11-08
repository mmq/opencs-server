# OpenCS — design

> The contract for this repository's structure.

## 1. What this is

**OpenCS** is a server compatible with the 1993 online game *CyberStrike*: one that the unmodified client can log into and play on, built from
the CyberStrike Server Specification v1.0 (the `cyberstrike-spec` repository). This is the product players use.

**Goals for 1.0**, in priority order:

1. **Correct to the spec.** Every behavior is based on the spec, and our own choices are documented as such.
2. **Readable by a newcomer, human or AI.** A reader finds any behavior by following the layers in §3,
   and each module does one thing its name says.
3. **Changeable.** Rules, policy values and wire formats live in separate places, so changing one does
   not mean touching the others.
4. **Robust on the open internet.** A malformed packet, a modified client or a dropped line never takes
   the server down or corrupts another player's game.
5. **A history that tells the story.** Built milestone by milestone, each commit a small change that
   passes the tests, ruff and mypy.

**Non-goals for 1.0:** a plugin system; more than one game server process; any change to the client;
distributing the game files (they are copyrighted and never enter the repo). A web front end is not part
of the server: it runs as its own process and reaches the server over the service channel (ADR 12).

## 2. Constraints and choices

| | choice | why |
|---|---|---|
| Language | Python ≥ 3.12, `asyncio` (3.12 is what Ubuntu 24.04 LTS ships) | the spec's examples and our tools are Python; 16 players a city is a light load |
| Runtime dependencies | `argon2-cffi` only, for password hashing (ADR 13); the rest is the standard library | trivial to deploy anywhere; SQLite and TOML are in the standard library, a memory-hard password hash is not |
| Development tools | `pytest`, `ruff` (lint + format), `mypy --strict` on `src/`, managed with `uv` in `.venv` | type hints and a fixed style are what make the code legible to both humans and AI tools |
| Packaging | `pyproject.toml`, `src/` layout, `python -m opencs` | the standard modern layout; tests run against the installed package |
| Configuration | TOML files read with `tomllib`, validated into typed dataclasses at startup | readable, comments allowed, fails fast on a typo |
| Persistence | SQLite via `sqlite3` | one file, transactional, no server to run |
| License | MIT | owner's choice |

## 3. Architecture: layers

Each layer imports only the layers above it in this table, and the small root modules every layer may
use (`policy`, `config`, `clock`, `connlog`, `ratelimit`, `tasks`, `logsetup`). **The arrows never point
upward:** the wire layer knows nothing of players, and the world knows nothing of sockets or bytes. The
layers from Net to Lobby do not import one another. `tests/unit/test_layering.py` fails if a module
imports what its layer may not.

| layer | package | does | does not know about |
|---|---|---|---|
| **Wire** | `opencs.wire` | the three number codecs, strings, grid bytes; frame build/parse; an incremental frame reader; the Chamber's line reader and command-mode arming | sockets, messages' meaning |
| **Messages** | `opencs.messages` | one typed, immutable class per message (spec I §8), each with `encode()`/`decode()` that mirror each other; a registry from type letter (and sub-letter) to class | what a message causes |
| **Net** | `opencs.net` | the asyncio listener; one `Connection` per socket: a read pump, a write pump with a bounded outbox, the tag counter applied at the send boundary | rules, phases |
| **Store** | `opencs.store` | accounts, progress and `/RESUME` records behind small interfaces; a SQLite implementation and an in-memory one for tests | the game |
| **World** | `opencs.world` | one `City` per joinable game ("room"): pods, towers, guppies, modules, scoring, territory, weather, NPCs. **Pure rules: calls in, events out** | sockets, bytes, the clock's source |
| **Lobby** | `opencs.lobby` | who is connected and where; `/PLAY`, `/CITY`, `/WHO`…; team choice; identity and callsigns | in-game rules |
| **Routing** | `opencs.routing` | turns an event's audience (spec I App. B) into the sessions that receive it | rules |
| **Sessions** | `opencs.session` | the connection's phases as explicit state machines (spec I §7.8): `Login` → `Chamber` → `InGame` → `Chamber`. Turns decoded client messages into calls on the lobby or a city | game rules |
| **Root modules** | `opencs.cities`, `opencs.control`, `opencs.admin`, `opencs.service` | the city tickers and reloading city files; the operator's commands as text in, text out; the loopback admin channel that carries them; the web front end's service channel | sockets of players, game rules |
| **App** | `opencs.app` | reads config, builds every service, wires them, starts the listener, handles shutdown | — |

### 3.1 Package layout

Every module under `src/opencs` is named here; `tests/unit/test_design_doc.py` fails if one is missing
or listed but gone. (`__init__.py` files and `py.typed` are left out.)

```
src/opencs/
  __main__.py            python -m opencs [--config PATH] [--port N] [--host H]
  app.py                 wiring, signals, graceful shutdown
  cities.py              the city tickers and reloading city config
  control.py             the operator's commands as text in, text out
  admin.py               the loopback admin channel: token, bounds, carries Control
  service.py             the web front end's channel: JSON lines on a Unix socket, carries Control
  tasks.py               long-lived background tasks: a failure is logged, not silent
  logsetup.py            [logging] applied to the opencs logger tree
  connlog.py             the per-connection log context: connection id and callsign on every record
  config.py              typed config: ServerConfig, CityConfig (validated from TOML)
  policy.py              every value the server chooses (spec I §10), named, each citing its spec section
  clock.py               Clock protocol; MonotonicClock; ManualClock for tests
  ratelimit.py           TokenBucket, KeyedBuckets (bounded per-source buckets)
  wire/
    codec.py             bias10, bias88, enc_num, str, grid byte            (spec I §4.3, I §6.1)
    frame.py             build_frame, FrameReader (sans-IO, incremental)    (spec I §4.1)
    text.py              LineReader, arm()/command lines                    (spec I §5.2)
    errors.py            the one exception the wire layer raises
  messages/
    base.py              Message base class, registry, decode_client()
    fields.py            field groups and checks shared by the message classes
    limits.py            limits that are facts about the client or the frame (I §4.1, I §8)
    catalog.py           the client's own tables of module indices (I §9.3.1)
    server.py            S→C: B, A, G, H, J, I, M, N, F, P, Q, K, L, O, R, S, T, D, E-*, a, b, c, C
    client.py            C→S: S, A, b, c, I, J, U, C, D-*, E-*, M, K, Q, d
  net/
    listener.py          start_server, connection accounting and limits
    connection.py        Connection: read pump, outbox, write pump, tags
    flow.py              how much a client has been sent and not yet acknowledged (ADR 11)
    traffic.py           what one connection sent: totals and busiest stretches, for the log at close
    capture.py           optional wire capture in the fixture format
  store/
    base.py              AccountStore, ProgressStore protocols (resume records are in memory)
    sqlite.py            SQLite implementation, versioned forward-only migrations
    memory.py            in-memory implementation (tests)
  world/
    city.py              City: owns one game's state; join/leave; tick(now)
    seating.py           who may join a city and what a seat is made of (I §6.2, I §7.2)
    lifecycle.py         when a city's game begins and ends (II §17)
    ids.py               id ranges and guarded reuse (I §6.2, II §8)
    entities.py          telling a client an entity exists, exactly once (I §7.2, I §7.3)
    view.py              ClientView: what one client has been told (late-joiner sync, I §7.3)
    events.py            Event = (message, Audience)
    ledger.py            the records a city says have changed, for the session to save (II §11.3)
    pods.py              a player's pod life: drop, death handshake, respawn (I §7.1, I §7.5, II §7, I §7.8)
    podreport.py         a client's 'b' report: the pod's handshake and the player going live
    combat.py            shot log, attribution, hit set, damage byte (I §9.1, II §9.3)
    hits.py              the shot and hit rules: what a shot or an 'I' does (I §8.25, I §9.1, II §9)
    relay.py             client messages passed on to the other players (I §8.24, I §8.25)
    chat.py              chat and HUD text (I §8.23)
    death.py             a death: payout, 'E'-'K' notices, 'J', bounty zero (I §9.2.9, II §11.2)
    player.py            Player: identity, slot and every per-player record (I §6.3)
    grace.py             the spawn-grace mirror of the client's rule (I §9.1.5)
    scoring.py           bounty, multiplier floor, the client's score mirror (I §9.2, II §11.1)
    progress.py          a player's persistent record and the rules for saving it (II §11.3)
    attacks.py           when a pod was last under attack: the /RESUME fight test (II §13 item 3)
    resume.py            a line drop: death in a fight, the ResumeRecord, the slots held (II §13)
    territory.py         block classes (I §8.22)
    weather.py           a city's weather condition, program and transitions (I §8.20)
    logistics.py         guppies and orders (I §9.4, I §9.4.3)
    orders.py            tower orders, guppy actions to events, towers placed and destroyed (I §9.4)
    modules.py           what a city stocks, drops, pickup, rest, eject (I §9.3.1, I §9.3.3)
    loadout.py           the installed-module ledger of one pod (I §9.3.4, II §13)
    dispatch.py          the message-type -> handler entries for orders, drops, effects, score
    host.py              WorldHost: what the rule modules ask of the city
    effects.py           effect-module requests (I §9.3.5, II §16.1)
    structures.py        towers and their durability
    npcs/sentinels.py    resident Sentinels
    npcs/munchers.py     Munchers (I §9.5.2)
  lobby/
    lobby.py             Lobby: members, locations, city choice, teams
    commands.py          parsing /PLAY, /CITY, … (pure)
    identity.py          accounts vs JOINGENIE, callsigns: an account's is chosen apart from its name
    replies.py           the text the Chamber shows in answer to lobby commands
  routing.py             Audience → sessions
  session/
    session.py           one connection's session: the phase machine's driver
    phases.py            the Phase interface, the text-phase base and shared services (I §7.8)
    login.py             GEnie logon (I §5.1)
    chamber.py           lobby text session (I §5.2-§5.5, II §4)
    ingame.py            binary session: decode → city calls; quit handshake (I §5.5)
    io.py                SessionIO: what a session may do to its connection
    persistence.py       loads a record before a join, writes what a city reports changed (II §11.3)
    held.py              the games held for dropped players: windows, reserved slots (II §13)
    registry.py          every live session, for the operator
    cityhealth.py        which cities have failed or been slow, and so need rebuilding when empty
    budgets.py           a connection's rate limits, which outlive its phases (ADR 9)
    badinput.py          counting bad input over a sliding window
    loginlimits.py       server-wide limits on logins and guests
config/
  server.toml            port, data directory, limits, logging
  cities/A.toml B.toml C.toml
tests/
  unit/                  one test module per source module
  replay/                real captures as fixtures, replayed through the world
  fuzz/                  random and hostile input against the net and session layers
  longrun/               a simulated 48-hour city
  e2e/                   the black-box judge against a running server
tools/
  fake_client.py         the scripted client (built on `opencs.wire`)
  judge.py               the black-box conformance check, 19 checks
  smoke.sh               start a throwaway server and run the judge
  make_fixture.py        turn a capture log into a replay fixture
.github/workflows/
  ci.yml                 lint, types and tests on 3.12 and 3.13, and the smoke test
deploy/
  opencs.service         the systemd unit the operator guide installs
```

### 3.2 How a message flows

```
socket bytes ─► Connection.read_pump ─► FrameReader ─► Frame ─► messages.decode_client() ─► ClientMessage
     ─► InGameSession.handle(msg) ─► City.method(player, …)  ─► list[Event]
     ─► Router.deliver(events) ─► each target Connection.outbox ─► write_pump: assign tag, encode, write
```

- **The world never sends and never awaits.** A `City` method takes a typed request and returns
  `list[Event]`; each `Event` is a server message plus an `Audience`. That keeps every rule testable with
  no sockets, and lets the replay tests drive real captured traffic through it.
- **Audiences are data** (`ToPlayer(id)`, `ToAll`, `ToAllExcept(id)`, `ToTeam(team)`,
  `ToPlayers(ids)`), resolved by the router — the spec's A.4 table becomes one small module.
- **Tags are assigned at the send boundary** (spec I §4.2), never by the code that builds a message.

### 3.3 Concurrency

- One asyncio event loop. **Each `City` is changed only from the event loop, and its methods never
  `await`**, so no city state is ever seen half-updated and no locks are needed. This is the "room" model
  common to multiplayer servers.
- Each city has one ticker task calling `City.tick(now)` at a fixed rate (`policy.TICK_HZ`) for timed
  rules: bounty accrual, the drop release, guppy queues, NPC timers, weather.
- Each connection has a read task and a write task. A slow client fills only its own bounded outbox; when
  that overflows, that client is disconnected, never the others slowed.
- The write task keeps to the client's line rate (`policy.CLIENT_BYTES_PER_SECOND`, ADR 11), so a backlog
  waits in the outbox and not in the kernels' socket buffers. There, a newer `'b'` for a pod replaces the
  queued one where it sits. In a game it also stops while too much is unacknowledged (`net/flow.py`), which
  keeps the client's receive ring from overflowing, and the router sheds shots to a client that is behind.

### 3.4 Time

All rules read time from a `Clock` passed in, never from `time` directly. Tests use `ManualClock` and
advance it; nothing in the test suite sleeps.

### 3.5 State per client inside the world

Some rules depend on what one client has been told: which entities it holds (late joiners, I §7.3), which
pods it has been introduced to (I §7.2), a guppy's or Muncher's leg count as that client sees it. That lives
in `world/view.py` as one `ClientView` per player, owned by the city. Sessions do not keep game state.

## 4. Policy, configuration and the spec

- **Every value the server chooses** (spec I §10) is a named constant in
  `policy.py` or a field in the config, with a comment naming its spec section. No unexplained number
  appears anywhere else.
- **Cities are configuration** (`config/cities/*.toml`): letter, name, map, multiplier cap, tower output,
  module level, banned modules, block-class map, weather program, NPC population, team policy.
- **The spec lives in its own repository** (`cyberstrike-spec`, spec v1.0). Code cites it as `I §n`, `II §n`,
  `III §n` or `IV §n` (part and section). **Where the spec does not cover a behavior, the choice is the server's own** and
  is documented where it is made (see the first bullet).

## 5. Robustness and security

- **Input is hostile.** A frame that fails its checksum or length is dropped and counted; a message that
  fails to decode is dropped but not counted (I §4.1); a connection that passes `policy.MAX_BAD_FRAMES` is closed.
  **No input can raise out of a session**: the session's handler catches, logs with context and carries on
  or closes that one connection.
- **Limits:** connections in total and per address, line length in the Chamber (the client's armed buffer
  is 512 bytes, spec I §5.2), outbox size, login attempts.
- **Never send what crashes the client** (spec II §8): message constructors validate their fields
  (`'c'` KIND, `'R'` ranges, `'T'` classes, chat bytes) and raise in our code rather than on a player's
  screen.
- **Passwords:** salted Argon2id from `argon2-cffi` (ADR 13; an account made before it keeps its PBKDF2 hash until its next login rehashes it), never stored or logged in clear, hashed on a small thread pool (`app.ThreadHasher`) so a login never stalls the loop; the session hands the work over through `Services.hasher` and stays sans-IO. A password the operator or the web makes is hashed there too (`Control.run`, the service channel).
- **Bugs vs bad input:** an exception inside a `City` is a bug — logged with the triggering request, the
  request dropped, the city kept running. What the call changed before it raised stays changed, so the city
  is counted as failed (`session.cityhealth`, shown by `status`) and rebuilt from its configuration the next
  time its game is empty. Losing a game in play would be worse than a slightly wrong one, and snapshots would
  cost a copy per message.
- **Slow calls:** everything shares one loop (ADR 3), so a city call or tick, with the save that follows it,
  that takes over `policy.SLOW_CITY_CALL_SECONDS` holds up every player. `session.cityhealth` counts them, and
  warns at most once a minute per city.

## 6. Observability and operations

- `logging` with one logger per layer and a per-connection adapter (connection id, callsign).
- **Optional wire capture** per connection, in the replay format the tests read, so a bug a player hits
  becomes a test fixture.
- An admin channel: status, announce, kick, graceful shutdown that saves every player's progress.
- A service channel (ADR 12) for the web front end: a snapshot of who plays where, registration, and the
  operator's commands, over a Unix socket only the server's group can reach.
- Graceful shutdown on SIGINT/SIGTERM: stop accepting, save progress, close connections.

## 7. Testing

| level | what | where |
|---|---|---|
| unit | codecs and frames against the spec's hex (A.3, A.3b); every message round-trips; every rule module with a `ManualClock` | `tests/unit/` |
| replay | real captured client traffic (deaths, guppy legs, Muncher trips, rest reports) driven through the world, checked against what the test server did | `tests/replay/` |
| end-to-end | the judge: two scripted clients against a running server, 19 checks | `tests/e2e/`, `tools/judge.py` |
| live | two real clients, only for what must be seen | a round at the end of each milestone |

Rules: **every commit passes `pytest`, `ruff` and `mypy`**; a bug fix comes with a test shown to fail
without it; a new rule comes with its test in the same commit.

## 8. Conventions

- Type hints everywhere; `mypy --strict` clean. Dataclasses (`frozen=True, slots=True`) for messages,
  events and config.
- One concept per module. Past the two deliberate catalogues, `policy.py` (one named constant per
  server choice) and `messages/server.py` (one class per server message), which run long, no module
  passes 600 lines; one that nears it is a prompt to split.
- Docstrings say *what and why* and cite the spec as `I §n`, `II §n` or `III §n`; they do not narrate history (git does that).
- No binary addresses in the code (the spec carries provenance).
- Names follow the spec's vocabulary: pod, tower, guppy, Muncher, Sentinel, bay, bounty, multiplier,
  territory, city, map.

## 9. Decision records (`docs/decisions/`)

One file each; [the index](decisions/README.md) has the same list.

1. [Standard library at runtime, except password hashing](decisions/0001-stdlib-only-at-runtime.md) (amended by 13).
2. [The world returns events and never performs I/O](decisions/0002-world-returns-events.md).
3. [One event loop; cities never await; no locks](decisions/0003-single-loop-no-locks.md).
4. [Audiences as data, resolved by a router](decisions/0004-audiences-as-data.md).
5. [Tags assigned at the send boundary](decisions/0005-tags-at-send-boundary.md).
6. [Policy values centralized in `policy.py` and config, each citing the spec](decisions/0006-policy-centralized.md).
7. [SQLite for persistence, behind store protocols](decisions/0007-sqlite-behind-protocols.md).
8. [Written from the spec](decisions/0008-written-from-spec.md).
9. [Rate limits belong to the connection, not the phase](decisions/0009-limits-per-connection.md).
10. [Every way to make an account goes through `Identity`](decisions/0010-registration-behind-identity.md).
11. [Writes keep to what the client can read](decisions/0011-pace-writes-to-the-client.md).
12. [A web front end runs in its own process and reaches the server over a service channel](decisions/0012-web-front-end-over-a-service-channel.md).
13. [Argon2id for passwords](decisions/0013-argon2id-for-passwords.md).

## 10. Naming and trademarks

The project is **OpenCS**, package `opencs`. *CyberStrike* is a trademark of Simutronics Corp.; the name
appears only as a factual statement of compatibility ("a server compatible with CyberStrike"), never as the
project's name, in logos or in branding. The repository never contains the game's files: its
program, maps or manual.
