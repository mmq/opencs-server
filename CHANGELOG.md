# Changelog

What changed for players and operators, newest first. How the code is built is in
[`docs/DESIGN.md`](docs/DESIGN.md); the reasoning behind its structure is in
[`docs/decisions/`](docs/decisions/README.md).

## Unreleased

A database opened by this version moves to schema version 3, which `1.0.0rc2` refuses: back it up before
upgrading.

### Operators
- An optional service channel for a web front end (`[service] socket`): JSON lines over a Unix socket
  that only the server's group can reach, with a snapshot of who plays where, registration and the
  operator's commands (ADR 12).
- `create-account` and `reset-password` hash their password off the game's loop.
- The daily removal of unplayed accounts goes in small batches, and guest callsign checks use an index,
  so neither slows the games as the account table grows.

### Players
- A new password is 6 to 12 printable characters without spaces, the most the client's password entry
  takes; the web may let a player choose it. Accounts made before keep their passwords.

## 1.0.0rc2 (release candidate)

Writes paced to the client's modem, fixes for four ways one client could harm others, and harder
password storage. A database opened by this release moves to schema version 2, which `1.0.0rc1`
refuses: back it up before upgrading.

### Security
- A client could send deaths and drop requests as fast as it liked, and each went to every player in
  the city. Both now have a budget; an honest death and respawn is unaffected.
- One wrong password a minute from anywhere locked an account's owner out, and guesses sent at the same
  moment all got past the limit. Wrong passwords now count per account and address, plus a shared
  allowance that binds only addresses the owner has not logged in from since the server started; a
  guess is charged before its password is hashed and given back if it is right. `reset-password`
  clears an account's limits.
- An acknowledgement frame full of made-up tags made the server search what was awaiting
  acknowledgement once per tag. At most 16 tags of a frame are read, and each is found without a
  search.
- Per-address limits counted each IPv6 address apart, so a host could pick fresh addresses from its
  block for fresh allowances. An IPv6 client is now limited by its /64; logs keep the full address.

### Players
- Passwords are hashed with Argon2id. An existing account logs in as before, and its old PBKDF2 hash is
  replaced at that login.
- New passwords must not be the account name or one of about 110 common ones (`PASSWORD`, `QWERTY`,
  `123456` and the like, in any case). Accounts made before keep working.
- Reserved names are also refused in look-alike spellings such as `ADM1N` and `ADM-IN`.
- Passwords made up by `create-account` and `reset-password` are capitals and digits, which the game's
  settings screen can type; it turns what is typed into capitals, so a made-up password with lower case
  could not be entered.
- If the server cannot seat a resuming player, the game stays held and `/RESUME` can be typed again
  within 3 minutes.
- A player whose game falls behind gets fewer, fresher positions of other pods, sees fewer shots and
  misses some chat lines, rather than seeing the game later and later, losing data or crashing. Damage
  still lands, and no other game message is skipped.

### Operators
- What is written to each connection is paced to a 9600-baud modem's 960 bytes a second
  (`[limits] client_bytes_per_second`), and held back in a game while over 2 KB is unacknowledged
  (`[limits] client_unacked_bytes`). The line each connection logs at close shows how far behind it fell.
- A city that hits a bug keeps running, is shown as failed by `status`, and is rebuilt from its
  configuration the next time its game is empty. A bug in the work done after a tick is handled the
  same way rather than stopping the city's clock, and a background task that ends with an error says
  so in the log. A player whose line drops as the city hits a bug still vanishes from the other
  players' screens.
- A city call or tick over 20 ms, which holds up every player on the server, is logged at most once a
  minute per city and counted in `status`.
- A hit report with no shot on that connection before it, or from a pod more than 2,400 ft from the
  victim's, is counted per city and warned about once per player; it is still applied. `status` shows
  the counts.
- `argon2-cffi` is the server's one runtime dependency (ADR 13). Wheels exist for Linux x86-64 and
  arm64 and for Apple-silicon macOS; elsewhere it builds from source and needs a C compiler.
- The database is written with `synchronous=NORMAL`, so a save does not wait for the disk; a power cut
  or operating system crash can lose the last few saves, a server crash cannot.
- A warning at start when `server.toml` holds the admin token and is readable by its group or by others.
- A log line no longer loses its text when a player's callsign contains `%`.
- `create-account` no longer refuses its own made-up password when `[limits] min_password_length` is
  above 10. The rules for new passwords apply to those players choose.
- `tools/smoke.sh <port>`, run against a server that is already running, logs in as two accounts with
  random names and a random password and prints the `delete-account` commands that remove them.
  `tools/fake_client.py` takes `--password`.
- The systemd unit ships as `deploy/opencs.service`, and the operator guide installs it from there.
- The README has a status and limitations section; `CONTRIBUTING.md` and a CI workflow are added.

## 1.0.0rc1 (release candidate)

The first release: a server the unmodified DOS client can log into and play on. `1.0.0` follows real
play testing.

### Players
- Log in as a guest (`JOINGENIE`) or with an account and password, chat in the Chamber and use its
  commands (`/PLAY`, `/CITY`, `/WHO`, `/SCORE`, `/RESUME` and the rest), then join a city on a team.
- A full game: pods, teams, open and team chat, shots and hits, the Grenade, bounty and scoring, deaths and
  respawns, late joiners, and the latency meter.
- Towers flown in by the team's guppy, territory with per-city block maps, and every module the client
  offers: purchase orders, drops, pickup, eject, and the effects Repair, Harden, Immunize, Liberator,
  Weather, Mine, Mine Sweep and Air Support.
- Weather programs, Sentinels that are worn down and replaced, and called and free Munchers.
- Accounts and progress are saved and restored, and `/SCORE` and `/WHO <callsign>` show the record.
  Guests are never saved.
- New accounts take a name of 3 to 12 letters, digits, `_` or `-` (a few, such as `ADMIN`, are reserved)
  and a password of at least 6 characters. Five wrong passwords lock an account for a few minutes,
  wherever they come from. Accounts never played are removed after 30 days.
- A dropped line from a real account holds the game for 10 minutes, and `/RESUME` brings it back (3 minutes
  to type it once you are in the Chamber again). A drop within 20 seconds of an attack counts as a death.
- Callsigns are up to 12 characters. A duplicate gets a number from 2 to 999 (`JOINGENIE`, `JOINGENIE2`).
- A player whose modem falls behind misses some chat lines rather than being disconnected; game
  messages are never skipped.
- At most 2 guests per address and 24 in all; an idle guest leaves the Chamber after 15 minutes.
- Three cities (A, a training city with no NPCs; B; C) ship in `config/cities/`.

### Operators
- Configuration in TOML (`config/server.toml` and one file per city), validated at start with errors that
  name the file and key.
- Logging per layer with an optional rotating file; passwords are never logged.
- Optional per-connection wire capture, in the format the tests replay.
- Each connection logs at close what it sent: bytes and frames, the average rate and the busiest
  1 s and 10 s.
- A token-guarded admin channel on 127.0.0.1: `status`, `announce`, `kick`, `shutdown`, `reload`, and a
  failsafe for a stranded guppy.
- Graceful shutdown on SIGINT and SIGTERM that saves every player; `SIGHUP` or `reload` re-reads the city
  files without changing a running game.
- Limits on connections, logons, account creation, idle time in the Chamber, in-game frame rate and bad
  input, so one misbehaving client cannot harm another player's game (`docs/robustness.md`, with a
  threat model). They last for the whole connection, not one phase of it:
  - connections per address (4), per /24 (12) and in all (128), a cap on connections not yet logged in,
    and a limit on how often one address reconnects;
  - refusals logged at most once a minute per address, in a line fail2ban can match;
  - password checks on worker threads, so a burst of logins never delays a game.
- `registration = "open" | "closed"`, a cap on the number of accounts, and an optional `[limits]` table
  in `server.toml` to tune the security limits (for example behind a shared NAT).
- Admin commands `accounts`, `create-account`, `reset-password`, `reset-progress` and
  `delete-account`; generated passwords are shown once and never logged.
- The database uses WAL mode and is created private (directory 0700, file 0600).
- A sandboxed systemd unit and a checklist for running a public server (firewall rate limits, fail2ban,
  backups).
- `python -m opencs --version` and `tools/smoke.sh`, which starts a throwaway server, runs the judge's 19
  checks and stops it.
- One operator guide, [`docs/OPERATING.md`](docs/OPERATING.md), covers installing, running as a service,
  configuring, backing up and upgrading.
