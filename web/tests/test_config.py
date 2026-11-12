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


def test_the_site_settings_default_to_https_and_no_service_socket() -> None:
    config = load(KEY)
    assert config.https is True
    assert config.service_socket is None
    assert (config.max_streams, config.max_streams_per_address) == (200, 4)


def test_the_site_settings_are_read() -> None:
    config = load(
        {
            **KEY,
            "OPENCS_WEB_HTTPS": "false",
            "OPENCS_WEB_SERVICE_SOCKET": "/run/opencs/service.sock",
            "OPENCS_WEB_MAX_STREAMS": "50",
            "OPENCS_WEB_MAX_STREAMS_PER_ADDRESS": "2",
        }
    )
    assert config.https is False
    assert config.service_socket == Path("/run/opencs/service.sock")
    assert (config.max_streams, config.max_streams_per_address) == (50, 2)
    with pytest.raises(ConfigError, match="MAX_STREAMS"):
        load({**KEY, "OPENCS_WEB_MAX_STREAMS": "0"})


def test_the_account_settings_default_to_showing_a_made_up_password_and_the_console() -> None:
    config = load(KEY)
    assert config.registration is True
    assert config.password_delivery == "show"
    assert config.chosen_passwords is False
    assert config.email is None  # the console: mail is printed to the log
    assert config.email_daily_cap == 90
    assert config.site_url is None and config.contact is None and config.privacy_file is None


def test_resends_smtp_settings_are_read() -> None:
    config = load(
        {
            **KEY,
            "OPENCS_WEB_EMAIL_HOST": "smtp.resend.com",
            "OPENCS_WEB_EMAIL_USER": "resend",
            "OPENCS_WEB_EMAIL_PASSWORD": "re_secret",
            "OPENCS_WEB_EMAIL_FROM": "OpenCS <noreply@opencs.example.org>",
            "OPENCS_WEB_EMAIL_DAILY_CAP": "80",
            "OPENCS_WEB_PASSWORD_DELIVERY": "Both",
            "OPENCS_WEB_CHOSEN_PASSWORDS": "true",
            "OPENCS_WEB_SITE_URL": "https://opencs.example.org/",
        }
    )
    assert config.email is not None
    assert (config.email.host, config.email.port, config.email.security) == (
        "smtp.resend.com",
        587,
        "starttls",
    )
    assert (config.email.user, config.email.password) == ("resend", "re_secret")
    assert "re_secret" not in repr(config)
    assert config.email_from == "OpenCS <noreply@opencs.example.org>"
    assert config.email_daily_cap == 80
    assert config.password_delivery == "both"
    assert config.chosen_passwords is True
    assert config.site_url == "https://opencs.example.org"
    ssl = load(
        {
            **KEY,
            "OPENCS_WEB_EMAIL_HOST": "h",
            "OPENCS_WEB_EMAIL_FROM": "a@b",
            "OPENCS_WEB_EMAIL_SECURITY": "ssl",
        }
    )
    assert ssl.email is not None and ssl.email.port == 465


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ({"OPENCS_WEB_PASSWORD_DELIVERY": "post"}, "PASSWORD_DELIVERY"),
        ({"OPENCS_WEB_EMAIL_HOST": "smtp.resend.com"}, "EMAIL_FROM"),
        (
            {
                "OPENCS_WEB_EMAIL_HOST": "h",
                "OPENCS_WEB_EMAIL_FROM": "a@b",
                "OPENCS_WEB_EMAIL_SECURITY": "tls13",
            },
            "EMAIL_SECURITY",
        ),
        (
            {
                "OPENCS_WEB_EMAIL_HOST": "h",
                "OPENCS_WEB_EMAIL_FROM": "a@b",
                "OPENCS_WEB_EMAIL_PORT": "70000",
            },
            "EMAIL_PORT",
        ),
        ({"OPENCS_WEB_EMAIL_DAILY_CAP": "0"}, "EMAIL_DAILY_CAP"),
        ({"OPENCS_WEB_SITE_URL": "opencs.example.org"}, "SITE_URL"),
        ({"OPENCS_WEB_CHOSEN_PASSWORDS": "sometimes"}, "CHOSEN_PASSWORDS"),
    ],
)
def test_account_settings_it_cannot_use_are_refused(env: dict[str, str], name: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load({**KEY, **env})
