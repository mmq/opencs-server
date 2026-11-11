"""``opencs-web`` (or ``python -m opencs_web``): Django's management commands, as ``manage.py``
would give them (``opencs-web migrate``, ``opencs-web check --deploy``)."""

import os
import sys

from django.core.management import execute_from_command_line


def main(argv: list[str] | None = None) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "opencs_web.settings")
    execute_from_command_line(["opencs-web", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    main()
