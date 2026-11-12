# opencs-web

The web site for an OpenCS server, as a separate process: Django under one uvicorn worker, with its
own SQLite database. It talks to the game server only over the service channel (ADR 12) and reads the
game recorder's segments (ADR 14); it never opens the server's `opencs.db`. Its dependencies are its
own: the server still needs only `argon2-cffi`.

So far: settings, the status page, a `/healthz` page, the `opencs-web` command, and the recording
archiver (`opencs_web.recordings`): `opencs-web archive` stores the recorder's finished segments in an archive
of compressed one-minute chunks (ADR 15) with tables of games, pilots and battle events, deletes them,
and applies the archive's retention; `opencs-web forget_callsign` takes a deleted account's callsign
out of it. `docs/OPERATING.md` ("The archiver") has the details and `deploy/` its systemd units.

The status page (`/`) shows the account count, the uptime, each city's pilots by team and the held
games, from the service channel's `snapshot`, and stays live by server-sent events
(`/status/events`); without JavaScript it shows the status as loaded. `channel.py` is the channel's
client (one connection, a short timeout, a backoff after a failure), `status.py` the cache: one
snapshot a second while a live page is open, else only when a page needs one and the last is over a
second old, so with no one looking the game server is asked nothing. Under ASGI Django has no startup
hook, so the update task starts with the first live page and stops with the last. While the game
server cannot be reached the page says "offline". `deploy/opencs-web.service` runs it behind Caddy
(`deploy/Caddyfile`); OPERATING, "The web front end", installs both.

Registration and web accounts (`accounts.py`, `accountviews.py`, `forms.py`, `models.py`): a player
registers with an account name, an optional callsign and an email address; an emailed link (signed,
expiring, used once) opens a page whose button makes the game account over the channel's `register`;
the made-up password is shown once, emailed, or both. Logging in is by emailed link too, and the
account page changes the callsign (`set-callsign`), gives a new password (`reset-password`) and
deletes the web account, or the game account as well (`delete-account`). The game server's rules
decide every name, callsign and password, and its refusals are shown as they come; the site keeps
only the email address, the account name and when the server made it. Emails are limited per network,
per address and per day, and go through any SMTP server, or are printed to the log when none is set.
OPERATING, "Registration and accounts" and "Email", has the details.

Administration (`staff.py`, `staffviews.py`, `staffforms.py`, `staffurls.py`), off unless
`OPENCS_WEB_ADMIN=true`: admins are Django users with an `AdminAccount` (a role, `admin` or
`moderator`), made only by `opencs-web createadmin` and changed by `opencs-web resetadmin`. A log-in
takes the password (Django's Argon2 hasher) and a code from an authenticator app (django-otp's TOTP
device) or a recovery code (its static device), with a lock after five wrong ones and a short session.
The pages show the status and each city's health counters from the channel's `status`, and run the
operator's commands over the channel; destructive ones are confirmed on a second page, with a fresh
code if the last was over five minutes ago, and everything goes in an append-only audit log
(`AuditEntry`). OPERATING, "Administration", has the details.

Maps (`maps/`, `mapviews.py`, ADR 16): `maps/track.py` decodes records into a viewer track (the city
as a map shows it, and what happens), `maps/source.py` reads a city's records for a time from the archive
and the unarchived segments, seeking to the keyframe before it, `maps/windows.py` decodes one-minute
windows once into a bounded cache, and `maps/live.py` subscribes to the game server's live feed of a
city while anyone watches it, decoding it once for all of them. The endpoints are `/maps/cities.json`,
`/maps/<letter>/map.json`, `/maps/<letter>/track.json?t=<UTC ms>` and `/maps/<letter>/live`
(server-sent `track` events); every one is cut at the audience's delay on the server. ADR 16 has the
track format; OPERATING, "Maps", the settings.

## Settings

Read from the environment (in production, a systemd `EnvironmentFile`):

| variable | meaning | default |
|---|---|---|
| `OPENCS_WEB_SECRET_KEY` | Django's secret key | none: required |
| `OPENCS_WEB_ALLOWED_HOSTS` | host names, comma-separated | none, so every request is refused |
| `OPENCS_WEB_DEBUG` | `true` for development only | `false` |
| `OPENCS_WEB_HTTPS` | `false` to serve plain HTTP (development, or trying it on loopback); `true` redirects to HTTPS and sends HSTS | `true` |
| `OPENCS_WEB_SERVICE_SOCKET` | the game server's service socket | none: the status page says offline |
| `OPENCS_WEB_MAX_STREAMS` | live status streams at once | 200 |
| `OPENCS_WEB_MAX_STREAMS_PER_ADDRESS` | live status streams from one address (an IPv6 /64) | 4 |
| `OPENCS_WEB_DATABASE` | the web's SQLite file (web accounts, emailed links, sessions, admins, the audit log) | `opencs-web.db` in `$STATE_DIRECTORY`, else the working directory |
| `OPENCS_WEB_SITE_URL` | the site's address, for links in emails | the address each request came to |
| `OPENCS_WEB_REGISTRATION` | `false` to take no new registrations | `true` |
| `OPENCS_WEB_PASSWORD_DELIVERY` | a made-up password is `show`n once, `email`ed, or `both` | `show` |
| `OPENCS_WEB_CHOSEN_PASSWORDS` | `true` lets players choose their password (never emailed) | `false` |
| `OPENCS_WEB_CONTACT` | how to reach the operator, shown on the pages | none |
| `OPENCS_WEB_PRIVACY_FILE` | plain text of the operator's own for the privacy page | none |
| `OPENCS_WEB_EMAIL_HOST` | the SMTP server (Resend: `smtp.resend.com`) | none: emails are printed to the log |
| `OPENCS_WEB_EMAIL_SECURITY` | `starttls`, `ssl` or `none` | `starttls` |
| `OPENCS_WEB_EMAIL_PORT` | the SMTP port | 587, 465 or 25 by security |
| `OPENCS_WEB_EMAIL_USER`, `OPENCS_WEB_EMAIL_PASSWORD` | the SMTP login (Resend: `resend` and an API key) | none |
| `OPENCS_WEB_EMAIL_FROM` | the sender, on a domain the provider has verified | `OpenCS <opencs@localhost>`; required with a host |
| `OPENCS_WEB_EMAIL_DAILY_CAP` | emails in 24 hours, below the provider's limit (Resend's free plan: 100) | 90 |
| `OPENCS_WEB_ADMIN` | `true` for the admin pages; off, they are 404s | `false` |
| `OPENCS_WEB_ADMIN_PATH` | where the admin pages are | `admin` |
| `OPENCS_WEB_ADMIN_SESSION_MINUTES` | the longest an admin's log-in lasts (15 minutes idle ends it sooner) | 60 |
| `OPENCS_WEB_MAP_DELAY_PUBLIC` | seconds the public's maps are behind real time; 0 makes them a radar | 3600 |
| `OPENCS_WEB_MAP_DELAY_ADMIN` | the same for a logged-in administrator | 0 |
| `OPENCS_WEB_MAP_ADMIN_CHAT` | `true` puts recorded chat in administrators' tracks | `false` |
| `OPENCS_WEB_MAP_MAX_STREAMS`, `..._PER_ADDRESS` | map streams at once, in all and from one address | 100, 2 |
| `OPENCS_WEB_RECORDINGS` | the game recorder's directory, for `archive` and the maps | none |
| `OPENCS_WEB_ARCHIVE` | the recording archive's SQLite file | `opencs-archive.db`, placed as the database |
| `OPENCS_WEB_ARCHIVE_MAX_DAYS` | the archive keeps games this many days | 365 |
| `OPENCS_WEB_ARCHIVE_MAX_BYTES` | and removes the oldest past this size | 10000000000 |

## Running

```sh
uv sync                                     # at the repository root: the server, the web and the tools
export OPENCS_WEB_SECRET_KEY=... OPENCS_WEB_ALLOWED_HOSTS=localhost OPENCS_WEB_DEBUG=true \
    OPENCS_WEB_HTTPS=false OPENCS_WEB_SERVICE_SOCKET=/path/to/service.sock   # [service] socket
uv run opencs-web migrate                   # Django's management commands, like manage.py
uv run uvicorn opencs_web.asgi:application  # serve it
```

Without `OPENCS_WEB_EMAIL_HOST` the site's emails are printed to uvicorn's output: follow the
registration link from there.

## Checks

From the repository root:

```sh
uv run pytest web
uv run mypy --config-file web/pyproject.toml
uv run ruff check . && uv run ruff format --check .   # covers web/ too
```

## Compression benchmark

`benchmarks/compression.py` compares compressors for the archive per one-minute chunk, on a directory
of recorded segments (ADR 15 has the results). Its compressors are in the `benchmark` group:

```sh
uv run --package opencs-web --group benchmark python web/benchmarks/compression.py DIR --memory
```
