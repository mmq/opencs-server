from pathlib import Path

import pytest

from opencs_web.config import ConfigError, load

KEY = {"OPENCS_WEB_SECRET_KEY": "k"}


def test_a_secret_key_is_required() -> None:
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        load({})
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        load({"OPENCS_WEB_SECRET_KEY": "  "})


def test_the_defaults_are_the_safe_ones() -> None:
    config = load(KEY)
    assert config.debug is False
    assert config.allowed_hosts == ()
    assert config.database == Path("opencs-web.db")


def test_settings_are_read() -> None:
    config = load(
        {
            **KEY,
            "OPENCS_WEB_DEBUG": "yes",
            "OPENCS_WEB_ALLOWED_HOSTS": "example.org, www.example.org,",
            "OPENCS_WEB_DATABASE": "/var/lib/opencs-web/web.db",
        }
    )
    assert config.debug is True
    assert config.allowed_hosts == ("example.org", "www.example.org")
    assert config.database == Path("/var/lib/opencs-web/web.db")


def test_the_database_goes_in_systemds_state_directory() -> None:
    config = load({**KEY, "STATE_DIRECTORY": "/var/lib/opencs-web"})
    assert config.database == Path("/var/lib/opencs-web/opencs-web.db")


def test_a_debug_flag_it_does_not_understand_is_refused() -> None:
    with pytest.raises(ConfigError, match="DEBUG"):
        load({**KEY, "OPENCS_WEB_DEBUG": "maybe"})


def test_the_game_servers_database_is_refused() -> None:
    with pytest.raises(ConfigError, match=r"opencs\.db"):
        load({**KEY, "OPENCS_WEB_DATABASE": "/var/lib/opencs/opencs.db"})


def test_the_archive_settings() -> None:
    config = load(KEY)
    assert config.recordings is None
    assert config.archive == Path("opencs-archive.db")
    assert (config.archive_max_days, config.archive_max_bytes) == (365, 10_000_000_000)
    config = load(
        {
            **KEY,
            "STATE_DIRECTORY": "/var/lib/opencs-web",
            "OPENCS_WEB_RECORDINGS": "/var/lib/opencs-recordings",
            "OPENCS_WEB_ARCHIVE_MAX_DAYS": "30",
            "OPENCS_WEB_ARCHIVE_MAX_BYTES": "1000000",
        }
    )
    assert config.recordings == Path("/var/lib/opencs-recordings")
    assert config.archive == Path("/var/lib/opencs-web/opencs-archive.db")
    assert (config.archive_max_days, config.archive_max_bytes) == (30, 1_000_000)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ARCHIVE_MAX_DAYS", "0"),
        ("ARCHIVE_MAX_BYTES", "ten"),
        ("ARCHIVE", "/var/lib/opencs/opencs.db"),
        ("ARCHIVE", "opencs-web.db"),
    ],
)
def test_archive_settings_it_cannot_use_are_refused(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load({**KEY, f"OPENCS_WEB_{name}": value})
