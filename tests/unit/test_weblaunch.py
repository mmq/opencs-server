"""``opencs --web`` (``opencs.weblaunch``): what the server turns on for the site, and the site's
command line and environment."""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from opencs import app
from opencs.config import ConfigError, RecordingConfig, ServerConfig, ServiceConfig, load_config
from opencs.weblaunch import web_command, with_web_defaults

CONFIG = Path(__file__).parents[2] / "config" / "server.toml"
#: Nothing is made there: these functions only build paths.
DATA = Path("/srv/opencs")


def shipped(data_dir: Path) -> ServerConfig:
    server = load_config(CONFIG).server
    return replace(server, data_dir=data_dir)


def test_web_turns_on_the_service_channel_and_recording_in_the_data_directory() -> None:
    server = with_web_defaults(shipped(DATA))
    assert server.service.socket == DATA / "service.sock"
    assert server.recording.directory == DATA / "recordings"
    assert server.recording.enabled


def test_web_keeps_what_the_config_file_sets() -> None:
    server = replace(
        shipped(DATA),
        service=ServiceConfig(Path("/run/opencs/s.sock")),
        recording=RecordingConfig(directory=Path("/srv/rec"), when="off"),
    )
    server = with_web_defaults(server)
    assert server.service.socket == Path("/run/opencs/s.sock")
    assert server.recording.directory == Path("/srv/rec")
    argv, _ = web_command(server, 8000, {})
    assert "--recordings" not in argv  # recording is off: the site has nothing to archive


def test_the_site_is_this_python_with_the_servers_paths() -> None:
    argv, _ = web_command(with_web_defaults(shipped(DATA)), 8123, {})
    assert argv[:4] == [sys.executable, "-m", "opencs_web", "local"]
    options = dict(zip(argv[4::2], argv[5::2], strict=True))
    assert options == {
        "--host": "127.0.0.1",
        "--port": "8123",
        "--state-dir": str(DATA / "web"),
        "--service-socket": str(DATA / "service.sock"),
        "--recordings": str(DATA / "recordings"),
    }


def test_the_web_table_becomes_variables_and_the_environment_wins() -> None:
    server = replace(
        with_web_defaults(shipped(DATA)),
        web=(("contact", "file"), ("map_delay_public", "60")),
    )
    _, env = web_command(server, 8000, {"PATH": "/bin", "OPENCS_WEB_CONTACT": "environment"})
    assert env["PATH"] == "/bin"
    assert env["OPENCS_WEB_CONTACT"] == "environment"
    assert env["OPENCS_WEB_MAP_DELAY_PUBLIC"] == "60"


def test_web_without_the_web_package_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(app, "web_installed", lambda: False)
    code = app.main(["--config", str(CONFIG), "--data-dir", str(tmp_path), "--web"])
    assert code == 2
    assert "--web needs the web package" in capsys.readouterr().err


def test_web_refuses_a_data_directory_too_deep_for_the_socket() -> None:
    deep = Path("/" + "d" * 120)
    with pytest.raises(ConfigError, match="too long for a Unix socket"):
        with_web_defaults(shipped(deep))
