"""``opencs-web`` (or ``python -m opencs_web``): Django's management commands, as ``manage.py``
would give them (``opencs-web migrate``, ``opencs-web check --deploy``), and ``opencs-web local``
(:mod:`opencs_web.local`), which sets its defaults before Django reads the settings."""

import os
import sys

from django.core.management import execute_from_command_line

from opencs_web import local


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["local"]:
        sys.exit(local.main(args[1:]))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "opencs_web.settings")
    execute_from_command_line(["opencs-web", *args])


if __name__ == "__main__":
    main()
