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


def _flag(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(PREFIX + name, "").strip().lower()
    if raw in TRUE:
        return True
    if raw in FALSE:
        return False
    raise ConfigError(f"{PREFIX}{name} must be true or false, not {raw!r}")


def _database(env: Mapping[str, str]) -> Path:
    """The web's own SQLite file: ``OPENCS_WEB_DATABASE``, else in systemd's ``STATE_DIRECTORY``,
    else in the working directory. Never the game server's database."""
    raw = env.get(PREFIX + "DATABASE", "").strip()
    if raw:
        path = Path(raw)
    else:
        state = env.get("STATE_DIRECTORY", "").split(":")[0].strip()
        path = Path(state or ".") / DEFAULT_DATABASE_NAME
    if path.name == GAME_DATABASE_FILE_NAME:
        raise ConfigError(f"{PREFIX}DATABASE must not be the game server's {path.name}")
    return path


def load(env: Mapping[str, str] | None = None) -> WebConfig:
    """Read the settings; raise ConfigError if one is missing or wrong."""
    if env is None:
        env = os.environ
    secret_key = env.get(PREFIX + "SECRET_KEY", "").strip()
    if not secret_key:
        raise ConfigError(f"{PREFIX}SECRET_KEY must be set")
    hosts = tuple(h.strip() for h in env.get(PREFIX + "ALLOWED_HOSTS", "").split(",") if h.strip())
    return WebConfig(
        secret_key=secret_key,
        debug=_flag(env, "DEBUG"),
        database=_database(env),
        allowed_hosts=hosts,
    )
