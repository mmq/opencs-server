"""``opencs-web local``: the web site on this machine, with nothing to configure, for trying it
out (``opencs --web`` starts it this way). Its files go in one state directory: the web's
database, the archive and a secret key made on the first run. Plain HTTP, the admin pages on, and
the maps without the public's delay. Any ``OPENCS_WEB_`` setting in the environment still wins.

``opencs-web local [options] COMMAND ...`` runs a management command (``createadmin NAME``) with
the same settings instead of serving the site."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
from collections.abc import MutableMapping, Sequence
from pathlib import Path

import django
import uvicorn
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command, execute_from_command_line
from django.db import DatabaseError

from opencs_web.config import NAMES, PREFIX

logger = logging.getLogger("opencs_web")

DEFAULT_STATE_DIR = "opencs-web-local"
SECRET_KEY_FILE = "secret-key"
#: The archiver's first run comes soon after the start, for the segments already waiting; then
#: one every segment rotation (the server's ``RECORDING_ROTATE_SECONDS``).
ARCHIVE_FIRST_SECONDS = 5.0
ARCHIVE_EVERY_SECONDS = 300.0
#: What the production unit (deploy/opencs-web.service) gives uvicorn, less the forwarded headers:
#: nothing sits in front of it here.
LIMIT_CONCURRENCY = 400
GRACEFUL_SHUTDOWN_SECONDS = 5


class LocalError(Exception):
    """A setting or file the site cannot start with: one line, exit status 2."""


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="opencs-web local",
        description="Serve the web site on this machine over plain HTTP, its files in one "
        "directory. With a COMMAND, run that management command with the same settings instead.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="the address to serve on")
    parser.add_argument("--port", type=int, default=8000, help="the port to serve on")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(DEFAULT_STATE_DIR),
        help=f"the web's database, archive and secret key (default: ./{DEFAULT_STATE_DIR})",
    )
    parser.add_argument("--service-socket", type=Path, help="the game server's service socket")
    parser.add_argument(
        "--recordings", type=Path, help="the server's recordings directory, to archive from"
    )
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="a management command, e.g. createadmin NAME"
    )
    return parser.parse_args(argv)


def secret_key(state: Path) -> str:
    """The key in ``state/secret-key``, made (mode 0600) the first time."""
    path = state / SECRET_KEY_FILE
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_text(encoding="ascii").strip()
        if not key:
            raise LocalError(f"{path} is empty: delete it to make a new key") from None
        return key
    key = secrets.token_urlsafe(48)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key + "\n")
    return key


def prepare(args: argparse.Namespace, env: MutableMapping[str, str]) -> None:
    """Fill in ``env`` for the site: the environment's own settings first, then the local
    defaults. The socket and recordings given on the command line override both."""
    unknown = sorted(
        name for name in env if name.startswith(PREFIX) and name[len(PREFIX) :] not in NAMES
    )
    if unknown:
        raise LocalError(f"{', '.join(unknown)}: not a setting the web knows (misspelt?)")
    state = args.state_dir.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not env.get(PREFIX + "SECRET_KEY", "").strip():
        env[PREFIX + "SECRET_KEY"] = secret_key(state)
    env.setdefault(PREFIX + "HTTPS", "false")
    env.setdefault(PREFIX + "ALLOWED_HOSTS", "127.0.0.1,localhost")
    env.setdefault(PREFIX + "DATABASE", str(state / "opencs-web.db"))
    env.setdefault(PREFIX + "ARCHIVE", str(state / "opencs-archive.db"))
    env.setdefault(PREFIX + "MAP_DELAY_PUBLIC", "0")
    env.setdefault(PREFIX + "ADMIN", "true")
    if args.service_socket is not None:
        env[PREFIX + "SERVICE_SOCKET"] = str(args.service_socket)
    if args.recordings is not None:
        env[PREFIX + "RECORDINGS"] = str(args.recordings)
    env["DJANGO_SETTINGS_MODULE"] = "opencs_web.settings"


def setup() -> None:
    """Django, with the web's database made or brought up to date."""
    try:
        django.setup()
        call_command("migrate", verbosity=0, interactive=False)
    except (ImproperlyConfigured, DatabaseError) as e:
        raise LocalError(str(e)) from e


class Archiver:
    """``opencs-web archive`` now and then, in a process of its own, so compressing with xz never
    holds up the site. A run cut short is finished by the next one."""

    def __init__(self, recordings: Path) -> None:
        self.recordings = recordings
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.thread = threading.Thread(target=self.run, name="archiver", daemon=True)

    def run(self) -> None:
        delay = ARCHIVE_FIRST_SECONDS
        while not self.stopping.wait(delay):
            delay = ARCHIVE_EVERY_SECONDS
            # The server makes the directory when it first records: nothing to do until then.
            if self.recordings.is_dir():
                self.once()

    def once(self) -> None:
        with self.lock:
            if self.stopping.is_set():
                return
            try:
                # Its own session: a Ctrl-C at the terminal is for the site, which stops it.
                process = self.process = subprocess.Popen(
                    [sys.executable, "-m", "opencs_web", "archive"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except OSError as e:
                logger.warning("the archiver did not start: %s", e)
                return
        out, err = process.communicate()
        with self.lock:
            self.process = None
        if self.stopping.is_set():
            return
        for line in out.splitlines():
            logger.info("archiver: %s", line)
        if process.returncode != 0:
            lines = err.strip().splitlines()
            logger.warning(
                "the archiver failed (status %d): %s",
                process.returncode,
                lines[-1] if lines else "no message",
            )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        """Stop a run in progress (it is safe to interrupt) and run no more."""
        with self.lock:
            self.stopping.set()
            process = self.process
        if process is not None:
            process.terminate()
            try:
                process.wait(GRACEFUL_SHUTDOWN_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(args: argparse.Namespace) -> int:
    """Run the site until SIGINT or SIGTERM: 0 then, 1 if it could not start."""
    host = f"[{args.host}]" if ":" in args.host else args.host
    logger.info("web site at http://%s:%d/", host, args.port)
    if not _is_loopback(args.host):
        logger.info(
            "only 127.0.0.1 and localhost are allowed as the site's address: "
            "add others to OPENCS_WEB_ALLOWED_HOSTS"
        )
    if settings.OPENCS_ADMIN:
        logger.info(
            "admin pages at http://%s:%d/%s/: make an admin with "
            "`uv run opencs-web local --state-dir %s createadmin NAME`",
            host,
            args.port,
            settings.OPENCS_ADMIN_PATH,
            args.state_dir.resolve(),
        )
    archiver = None
    if settings.OPENCS_RECORDINGS is not None:
        archiver = Archiver(Path(settings.OPENCS_RECORDINGS))
        archiver.start()
    # uvicorn stops gracefully on SIGINT and SIGTERM, then raises the signal again to end the
    # process the default way; here a stop is a clean exit.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda signum, frame: None)
    try:
        uvicorn.run(
            "opencs_web.asgi:application",
            host=args.host,
            port=args.port,
            workers=1,
            log_level="info",
            access_log=False,
            server_header=False,
            lifespan="off",
            limit_concurrency=LIMIT_CONCURRENCY,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
    except SystemExit as e:  # the port is taken, or not ours to bind
        return 1 if e.code else 0
    finally:
        if archiver is not None:
            archiver.stop()
    return 0


def main(argv: Sequence[str]) -> int:
    args = parse(argv)
    # The database holds email addresses and the archive chat, so the files are the user's
    # alone, as production's UMask=0077 makes them.
    os.umask(0o077)
    try:
        prepare(args, os.environ)
        setup()
    except (LocalError, OSError) as e:
        print(f"opencs-web local: {e}", file=sys.stderr)
        return 2
    if args.command:
        execute_from_command_line(["opencs-web", *args.command])
        return 0
    return serve(args)
