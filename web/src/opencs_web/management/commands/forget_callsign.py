"""``opencs-web forget_callsign CALLSIGN``: take a callsign out of the recording archive, for a
deleted account. What it does and does not reach is in OPERATING ("Recording games")."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from opencs_web.management.commands._archive import now_ms, open_archive, recordings
from opencs_web.recordings.archive import archive_directory, oldest_unarchived_ms


class Command(BaseCommand):
    help = (
        "Take a callsign out of the recording archive: the pilot and event tables, the segment "
        "headers, the recorded player rows and introductions, the pilot's own chat (emptied) "
        "and the callsign in chat lines, in every game it flew in. Archives the finished "
        "segments first; segments still being written are dealt with when they are archived."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("callsign")
        parser.add_argument(
            "--everywhere",
            action="store_true",
            help="also blank it in the chat of games it did not fly in (reads the whole archive)",
        )
        parser.add_argument(
            "--recordings", help="the recordings directory (default: OPENCS_WEB_RECORDINGS)"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        callsign = options["callsign"].strip()
        if not callsign or not callsign.isascii() or not callsign.isprintable():
            raise CommandError("a callsign is printable ASCII")
        directory = recordings(options["recordings"])
        now = now_ms()
        with open_archive() as archive:
            pending = False
            if directory is not None and directory.is_dir():
                result = archive_directory(
                    archive,
                    directory,
                    now_ms=now,
                    max_days=settings.OPENCS_ARCHIVE_MAX_DAYS,
                    max_bytes=settings.OPENCS_ARCHIVE_MAX_BYTES,
                )
                if result.archived:
                    self.stdout.write(f"archived {len(result.archived)} segments first")
                oldest = oldest_unarchived_ms(directory)
                pending = oldest is not None and oldest < now
            else:
                self.stderr.write("no recordings directory: only the archive is changed")
            done = archive.forget(
                callsign, now_ms=now, everywhere=options["everywhere"], pending=pending
            )
        self.stdout.write(
            f"{callsign}: {done.games} games, {done.segments} segments, {done.chunks} chunks "
            f"rewritten; {done.pilots} pilot rows and {done.events} event rows blanked"
        )
        if done.pending:
            self.stdout.write(
                "segments not archived yet may hold it: they will be cleaned as they are archived"
            )
