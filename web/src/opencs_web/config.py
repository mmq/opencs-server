"""The web process's settings, read from the environment (a systemd ``EnvironmentFile`` in
production). Every default is the safe one: debug off, no host allowed, no secret key made up."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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
#: Live status streams (server-sent events) open at once, in all and from one address (an IPv6
#: /64). Each costs a connection and a few kilobytes; none of them costs the game server anything,
#: since they share one snapshot a second.
DEFAULT_MAX_STREAMS = 200
DEFAULT_MAX_STREAMS_PER_ADDRESS = 4
#: How a password the server makes up reaches the player: on the page, once; by email; or both.
PASSWORD_DELIVERIES = ("show", "email", "both")
DEFAULT_PASSWORD_DELIVERY = "show"
#: How the mail server is reached: STARTTLS (port 587), TLS from the start (465), or in the clear
#: (a relay on the same machine only).
EMAIL_SECURITIES = {"starttls": 587, "ssl": 465, "none": 25}
DEFAULT_EMAIL_SECURITY = "starttls"
#: Emails the site sends in a day (24 hours), in all: below the provider's own limit (Resend's
#: free plan sends 100 a day), so sign-ups and log-ins stop with a clear page before the provider
#: refuses, and a few are left for passwords (which are sent past it).
DEFAULT_EMAIL_DAILY_CAP = 90
DEFAULT_EMAIL_FROM = "OpenCS <opencs@localhost>"
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
    service_socket: Path | None = None
    https: bool = True
    max_streams: int = DEFAULT_MAX_STREAMS
    max_streams_per_address: int = DEFAULT_MAX_STREAMS_PER_ADDRESS
    #: The site's address as players reach it, for the links in its emails (no trailing slash);
    #: None: taken from each request.
    site_url: str | None = None
    #: Whether the site takes new registrations.
    registration: bool = True
    password_delivery: str = DEFAULT_PASSWORD_DELIVERY
    chosen_passwords: bool = False
    #: How to reach the operator, for when the site cannot help (shown on its pages).
    contact: str | None = None
    #: A plain-text file of the operator's own words for the privacy page.
    privacy_file: Path | None = None
    email: EmailConfig | None = None
    email_from: str = DEFAULT_EMAIL_FROM
    email_daily_cap: int = DEFAULT_EMAIL_DAILY_CAP


@dataclass(frozen=True)
class EmailConfig:
    """An SMTP server to send through. Without one, mail is printed to the log (development)."""

    host: str
    port: int
    user: str
    password: str = field(repr=False)
    security: str


def _flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(PREFIX + name, "").strip().lower()
    if not raw:
        return default
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


def _choice(env: Mapping[str, str], name: str, choices: Iterable[str], default: str) -> str:
    raw = env.get(PREFIX + name, "").strip().lower() or default
    if raw not in choices:
        raise ConfigError(f"{PREFIX}{name} must be one of {', '.join(choices)}, not {raw!r}")
    return raw


def _email(env: Mapping[str, str]) -> EmailConfig | None:
    """``OPENCS_WEB_EMAIL_HOST`` and the rest, or None (the console) if no host is set."""
    host = env.get(PREFIX + "EMAIL_HOST", "").strip()
    if not host:
        return None
    security = _choice(env, "EMAIL_SECURITY", EMAIL_SECURITIES, DEFAULT_EMAIL_SECURITY)
    port = _count(env, "EMAIL_PORT", EMAIL_SECURITIES[security])
    if port > 65535:
        raise ConfigError(f"{PREFIX}EMAIL_PORT must be a port number, not {port}")
    if not env.get(PREFIX + "EMAIL_FROM", "").strip():
        raise ConfigError(f"{PREFIX}EMAIL_FROM must be set to send email through {host}")
    return EmailConfig(
        host=host,
        port=port,
        user=env.get(PREFIX + "EMAIL_USER", "").strip(),
        password=env.get(PREFIX + "EMAIL_PASSWORD", "").strip(),
        security=security,
    )


def _site_url(env: Mapping[str, str]) -> str | None:
    raw = env.get(PREFIX + "SITE_URL", "").strip().rstrip("/")
    if not raw:
        return None
    if not raw.startswith(("https://", "http://")) or " " in raw:
        raise ConfigError(f"{PREFIX}SITE_URL must be an address like https://opencs.example.org")
    return raw


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
    socket = env.get(PREFIX + "SERVICE_SOCKET", "").strip()
    return WebConfig(
        secret_key=secret_key,
        debug=_flag(env, "DEBUG"),
        database=database,
        allowed_hosts=hosts,
        recordings=Path(recordings) if recordings else None,
        archive=archive,
        archive_max_days=_count(env, "ARCHIVE_MAX_DAYS", DEFAULT_ARCHIVE_MAX_DAYS),
        archive_max_bytes=_count(env, "ARCHIVE_MAX_BYTES", DEFAULT_ARCHIVE_MAX_BYTES),
        service_socket=Path(socket) if socket else None,
        https=_flag(env, "HTTPS", default=True),
        max_streams=_count(env, "MAX_STREAMS", DEFAULT_MAX_STREAMS),
        max_streams_per_address=_count(
            env, "MAX_STREAMS_PER_ADDRESS", DEFAULT_MAX_STREAMS_PER_ADDRESS
        ),
        site_url=_site_url(env),
        registration=_flag(env, "REGISTRATION", default=True),
        password_delivery=_choice(
            env, "PASSWORD_DELIVERY", PASSWORD_DELIVERIES, DEFAULT_PASSWORD_DELIVERY
        ),
        chosen_passwords=_flag(env, "CHOSEN_PASSWORDS"),
        contact=env.get(PREFIX + "CONTACT", "").strip() or None,
        privacy_file=Path(privacy)
        if (privacy := env.get(PREFIX + "PRIVACY_FILE", "").strip())
        else None,
        email=_email(env),
        email_from=env.get(PREFIX + "EMAIL_FROM", "").strip() or DEFAULT_EMAIL_FROM,
        email_daily_cap=_count(env, "EMAIL_DAILY_CAP", DEFAULT_EMAIL_DAILY_CAP),
    )
