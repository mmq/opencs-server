"""``opencs-web local``: its settings, its secret key, and the site it serves."""

from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from opencs_web.config import NAMES, PREFIX, load
from opencs_web.local import LocalError, parse, prepare


def prepared(tmp_path: Path, env: dict[str, str], *args: str) -> dict[str, str]:
    prepare(parse(["--state-dir", str(tmp_path / "state"), *args]), env)
    return env


def test_the_local_defaults(tmp_path: Path) -> None:
    env = prepared(tmp_path, {})
    state = (tmp_path / "state").resolve()
    assert env[PREFIX + "HTTPS"] == "false"
    assert env[PREFIX + "ALLOWED_HOSTS"] == "127.0.0.1,localhost"
    assert env[PREFIX + "DATABASE"] == str(state / "opencs-web.db")
    assert env[PREFIX + "ARCHIVE"] == str(state / "opencs-archive.db")
    assert env[PREFIX + "MAP_DELAY_PUBLIC"] == "0"
    assert env[PREFIX + "ADMIN"] == "true"
    assert PREFIX + "SERVICE_SOCKET" not in env
    assert PREFIX + "RECORDINGS" not in env
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    config = load(env)
    assert config.https is False and config.admin is True


def test_the_environment_beats_the_defaults(tmp_path: Path) -> None:
    env = prepared(
        tmp_path,
        {PREFIX + "ADMIN": "false", PREFIX + "MAP_DELAY_PUBLIC": "60", PREFIX + "SECRET_KEY": "k"},
    )
    assert env[PREFIX + "ADMIN"] == "false"
    assert env[PREFIX + "MAP_DELAY_PUBLIC"] == "60"
    assert env[PREFIX + "SECRET_KEY"] == "k"
    assert not (tmp_path / "state" / "secret-key").exists()


def test_the_socket_and_recordings_given_beat_the_environment(tmp_path: Path) -> None:
    env = prepared(
        tmp_path,
        {PREFIX + "SERVICE_SOCKET": "/elsewhere.sock", PREFIX + "RECORDINGS": "/elsewhere"},
        "--service-socket",
        "/run/opencs/service.sock",
        "--recordings",
        "/var/lib/opencs-recordings",
    )
    assert env[PREFIX + "SERVICE_SOCKET"] == "/run/opencs/service.sock"
    assert env[PREFIX + "RECORDINGS"] == "/var/lib/opencs-recordings"


def test_the_secret_key_is_made_once(tmp_path: Path) -> None:
    key = prepared(tmp_path, {})[PREFIX + "SECRET_KEY"]
    path = tmp_path / "state" / "secret-key"
    assert len(key) >= 64
    assert path.read_text().strip() == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert prepared(tmp_path, {})[PREFIX + "SECRET_KEY"] == key


def test_an_unknown_setting_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LocalError, match="OPENCS_WEB_MAP_DELAY_PUBIC"):
        prepared(tmp_path, {PREFIX + "MAP_DELAY_PUBIC": "0"})
    prepared(tmp_path, {"OPENCS_OTHER": "x"})  # not the web's


class Recording(Mapping[str, str]):
    """An environment that notes every name looked up in it."""

    def __init__(self, values: dict[str, str]) -> None:
        self.env = values
        self.read: set[str] = set()

    def __getitem__(self, name: str) -> str:
        self.read.add(name)
        return self.env[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self.env)

    def __len__(self) -> int:
        return len(self.env)


def test_the_known_names_are_the_ones_load_reads() -> None:
    # An email host, so the email settings are read too.
    env = Recording(
        {
            PREFIX + "SECRET_KEY": "k",
            PREFIX + "EMAIL_HOST": "smtp.example.org",
            PREFIX + "EMAIL_FROM": "OpenCS <opencs@example.org>",
        }
    )
    load(env)
    assert {name.removeprefix(PREFIX) for name in env.read if name.startswith(PREFIX)} == NAMES


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read().decode()


def test_the_site_serves_and_stops_cleanly(tmp_path: Path) -> None:
    port = free_port()
    state = tmp_path / "state"
    # Not the tests' settings: only what a user's shell would have.
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(PREFIX) and name != "DJANGO_SETTINGS_MODULE"
    }
    log = tmp_path / "log"
    with log.open("w") as out:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "opencs_web",
                "local",
                "--port",
                str(port),
                "--state-dir",
                str(state),
            ],
            env=env,
            cwd=tmp_path,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                assert get(f"http://127.0.0.1:{port}/healthz") == (200, "ok\n")
                break
            except OSError:
                assert process.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.2)
        status, page = get(f"http://127.0.0.1:{port}/")
        assert status == 200
        assert "Game server: offline" in page
        process.send_signal(signal.SIGINT)
        assert process.wait(15) == 0, log.read_text()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert (state / "opencs-web.db").is_file()
    assert (state / "secret-key").is_file()
    assert f"web site at http://127.0.0.1:{port}/" in log.read_text()
