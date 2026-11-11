"""The web process's settings, read from the environment (a systemd ``EnvironmentFile`` in
production). Every default is the safe one: debug off, no host allowed, no secret key made up."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from opencs.policy import DATABASE_FILE_NAME as GAME_DATABASE_FILE_NAME

PREFIX = "OPENCS_WEB_"
DEFAULT_DATABASE_NAME = "opencs-web.db"
DEFAULT_ARCHIVE_NAME = "opencs-archive.db"
#: The recording archive's retention: games older than this many days, and the oldest games past
#: this many bytes, are removed. A busy city archives about 0.5 MB an hour (ADR 15), so a city busy
#: all day fills 10 GB in about two years.
DEFAULT_ARCHIVE_MAX_DAYS = 365
DEFAULT_ARCHIVE_MAX_BYTES = 10_000_000_000
TRUE = {"1", "true", "yes", "on"}
FALSE = {"", "0", "false", "no", "off"}


class ConfigError(ValueError):
    """A setting is missing or not understood."""


@dataclass(frozen=True)
class WebConfig:
    secret_key: str
    debug: bool
    database: Path
    allowed_hosts: tuple[str, ...]
    recordings: Path | None = None
    archive: Path = Path(DEFAULT_ARCHIVE_NAME)
    archive_max_days: int = DEFAULT_ARCHIVE_MAX_DAYS
    archive_max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES


def _flag(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(PREFIX + name, "").strip().lower()
    if raw in TRUE:
        return True
    if raw in FALSE:
        return False
    raise ConfigError(f"{PREFIX}{name} must be true or false, not {raw!r}")


def _state_file(env: Mapping[str, str], name: str, default: str) -> Path:
    """One of the web's own SQLite files: ``OPENCS_WEB_<name>``, else ``default`` in systemd's
    ``STATE_DIRECTORY``, else in the working directory. Never the game server's database."""
    raw = env.get(PREFIX + name, "").strip()
    if raw:
        path = Path(raw)
    else:
        state = env.get("STATE_DIRECTORY", "").split(":")[0].strip()
        path = Path(state or ".") / default
    if path.name == GAME_DATABASE_FILE_NAME:
        raise ConfigError(f"{PREFIX}{name} must not be the game server's {path.name}")
    return path


def _count(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(PREFIX + name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ConfigError(f"{PREFIX}{name} must be a whole number above 0, not {raw!r}")
    return value


def load(env: Mapping[str, str] | None = None) -> WebConfig:
    """Read the settings; raise ConfigError if one is missing or wrong."""
    if env is None:
        env = os.environ
    secret_key = env.get(PREFIX + "SECRET_KEY", "").strip()
    if not secret_key:
        raise ConfigError(f"{PREFIX}SECRET_KEY must be set")
    hosts = tuple(h.strip() for h in env.get(PREFIX + "ALLOWED_HOSTS", "").split(",") if h.strip())
    database = _state_file(env, "DATABASE", DEFAULT_DATABASE_NAME)
    archive = _state_file(env, "ARCHIVE", DEFAULT_ARCHIVE_NAME)
    if archive == database and str(database) != ":memory:":
        raise ConfigError(f"{PREFIX}ARCHIVE must not be the web's database")
    recordings = env.get(PREFIX + "RECORDINGS", "").strip()
    return WebConfig(
        secret_key=secret_key,
        debug=_flag(env, "DEBUG"),
        database=database,
        allowed_hosts=hosts,
        recordings=Path(recordings) if recordings else None,
        archive=archive,
        archive_max_days=_count(env, "ARCHIVE_MAX_DAYS", DEFAULT_ARCHIVE_MAX_DAYS),
        archive_max_bytes=_count(env, "ARCHIVE_MAX_BYTES", DEFAULT_ARCHIVE_MAX_BYTES),
    )
