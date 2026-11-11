"""What the archive commands share: the archive and recordings directory from the settings."""

from __future__ import annotations

import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError

from opencs_web.recordings.archive import Archive, ArchiveError


def now_ms() -> int:
    return int(time.time() * 1000)


def open_archive() -> Archive:
    try:
        return Archive(Path(settings.OPENCS_ARCHIVE))
    except ArchiveError as exc:
        raise CommandError(str(exc)) from exc


def recordings(given: str | None) -> Path | None:
    """The recordings directory: ``given``, else ``OPENCS_WEB_RECORDINGS``, else None."""
    raw = given or settings.OPENCS_RECORDINGS
    return None if raw is None else Path(raw)
