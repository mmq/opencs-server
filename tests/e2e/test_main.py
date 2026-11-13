import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from opencs import policy

ROOT = Path(__file__).parents[2]
CONFIG = ROOT / "config" / "server.toml"
TIMEOUT = 15.0
LISTENING = re.compile(r"listening on (\S+):(\d+)")
#: The site imports Django and makes its database before it answers.
WEB_TIMEOUT = 60.0


@pytest.mark.e2e
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_main_listens_on_the_given_address_then_exits_zero(
    sig: signal.Signals, tmp_path: Path
) -> None:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "opencs",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--config",
            str(CONFIG),
            "--data-dir",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    # A watchdog turns a hung child into EOF on the pipe instead of a hung test.
    watchdog = threading.Timer(TIMEOUT, proc.kill)
    watchdog.start()
    try:
        lines: list[str] = []
        for line in proc.stdout:
            lines.append(line)
            found = LISTENING.search(line)
            if found:
                break
        else:
            pytest.fail("no 'listening on' line:\n" + "".join(lines))
        # The overrides took effect: the configured port is 2399, the flag asked for any free one.
        assert found.group(1) == "127.0.0.1"
        port = int(found.group(2))
        assert port not in (0, policy.DEFAULT_PORT)
        socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT).close()
        proc.send_signal(sig)
        lines.extend(proc.stdout)
        assert proc.wait(TIMEOUT) == 0
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
    assert "Traceback" not in "".join(lines)
    assert (tmp_path / policy.DATABASE_FILE_NAME).exists()  # the flag took effect, not ../data


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _get(url: str) -> str | None:
    """The page's text, or None while nothing answers."""
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return str(response.read().decode())
    except OSError:
        return None


@pytest.mark.e2e
def test_web_starts_the_site_on_the_service_channel_and_stops_it_with_the_server() -> None:
    pytest.importorskip("opencs_web")
    # A short data directory: the service socket goes in it, and macOS's temporary directory is
    # too deep for a Unix socket's path.
    data = Path(tempfile.mkdtemp(prefix="ocs", dir="/tmp"))
    web_port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "opencs",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--config",
            str(CONFIG),
            "--data-dir",
            str(data),
            "--web",
            "--web-port",
            str(web_port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    output: list[str] = []
    reader = threading.Thread(target=lambda: output.extend(proc.stdout or ()), daemon=True)
    reader.start()
    site = f"http://127.0.0.1:{web_port}"
    try:
        deadline = time.monotonic() + WEB_TIMEOUT
        while _get(f"{site}/healthz") is None:
            assert proc.poll() is None, "".join(output)
            assert time.monotonic() < deadline, "the site never answered:\n" + "".join(output)
            time.sleep(0.2)
        page = _get(f"{site}/")
        assert page is not None
        assert "Game server: offline" not in page  # it reached the server over the socket
        # SIGINT to the server alone, as systemd or kill would send it: the server stops the site.
        proc.send_signal(signal.SIGINT)
        assert proc.wait(TIMEOUT) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    reader.join(TIMEOUT)
    text = "".join(output)
    assert "Traceback" not in text
    assert "web site stopped" in text
    assert _get(f"{site}/healthz") is None
    assert (data / "web" / "opencs-web.db").exists()
    shutil.rmtree(data)
