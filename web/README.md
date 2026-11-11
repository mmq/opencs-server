# opencs-web

The web site for an OpenCS server, as a separate process: Django under one uvicorn worker, with its
own SQLite database. It talks to the game server only over the service channel (ADR 12) and reads the
game recorder's segments (ADR 14); it never opens the server's `opencs.db`. Its dependencies are its
own: the server still needs only `argon2-cffi`.

So far this is a skeleton: settings, a `/healthz` page and the `opencs-web` command. The recording
archiver, the public pages, registration and administration come next.

## Settings

Read from the environment (in production, a systemd `EnvironmentFile`):

| variable | meaning | default |
|---|---|---|
| `OPENCS_WEB_SECRET_KEY` | Django's secret key | none: required |
| `OPENCS_WEB_ALLOWED_HOSTS` | host names, comma-separated | none, so every request is refused |
| `OPENCS_WEB_DEBUG` | `true` for development only | `false` |
| `OPENCS_WEB_DATABASE` | the web's SQLite file | `opencs-web.db` in `$STATE_DIRECTORY`, else the working directory |

## Running

```sh
uv sync                                     # at the repository root: the server, the web and the tools
export OPENCS_WEB_SECRET_KEY=... OPENCS_WEB_ALLOWED_HOSTS=localhost OPENCS_WEB_DEBUG=true
uv run opencs-web migrate                   # Django's management commands, like manage.py
uv run uvicorn opencs_web.asgi:application  # serve it
```

## Checks

From the repository root:

```sh
uv run pytest web
uv run mypy --config-file web/pyproject.toml
uv run ruff check . && uv run ruff format --check .   # covers web/ too
```
