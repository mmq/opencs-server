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
