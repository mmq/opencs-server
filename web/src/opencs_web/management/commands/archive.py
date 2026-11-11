"""``opencs-web archive``: store the recorder's finished segments in the archive, delete them, and
apply the archive's retention. Run by ``deploy/opencs-web-archive.path`` when a segment finishes."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from opencs_web.management.commands._archive import now_ms, open_archive, recordings
from opencs_web.recordings.archive import archive_directory


class Command(BaseCommand):
    help = (
        "Archive the finished game recordings (*.ocsrec) in the recordings directory, oldest "
        "first, deleting each once stored; then remove the oldest games past the archive's age "
        "and size limits. Never touches a segment still being written (.part)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--recordings", help="the recordings directory (default: OPENCS_WEB_RECORDINGS)"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        directory = recordings(options["recordings"])
        if directory is None:
            raise CommandError("no recordings directory: set OPENCS_WEB_RECORDINGS")
        if not directory.is_dir():
            raise CommandError(f"{directory} is not a directory")
        with open_archive() as archive:
            result = archive_directory(
                archive,
                directory,
                now_ms=now_ms(),
                max_days=settings.OPENCS_ARCHIVE_MAX_DAYS,
                max_bytes=settings.OPENCS_ARCHIVE_MAX_BYTES,
            )
        for name in result.archived:
            self.stdout.write(f"archived {name}")
        for name in result.removed:
            self.stdout.write(f"deleted {name}, archived by an earlier run")
        if result.pruned_games:
            self.stdout.write(f"removed {result.pruned_games} old games from the archive")
        if result.failed:
            raise CommandError(
                f"{len(result.failed)} segments left where they are: {', '.join(result.failed)}"
            )
