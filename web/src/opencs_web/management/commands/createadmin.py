"""``opencs-web createadmin NAME``: an account for the admin pages, the only way to make one."""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from opencs_web import staff
from opencs_web.management.commands._admins import (
    new_recovery_codes,
    new_totp,
    password_argument,
    read_password,
)
from opencs_web.models import AdminAccount


class Command(BaseCommand):
    help = (
        "Make an account for the admin pages: a name, a password (asked for), a second factor "
        "for an authenticator app and recovery codes, both shown once. An admin does "
        "everything; a moderator sees the status, kicks and announces."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("name")
        parser.add_argument(
            "--role",
            choices=[AdminAccount.ADMIN, AdminAccount.MODERATOR],
            default=AdminAccount.ADMIN,
        )
        password_argument(parser)

    def handle(self, *args: Any, **options: Any) -> None:
        name = options["name"].strip()
        if not name or not name.isascii() or not name.isprintable() or " " in name:
            raise CommandError("a name is one word of printable ASCII")
        users = get_user_model()
        if users.objects.filter(username=name).exists():
            raise CommandError(f"{name} exists already: see opencs-web resetadmin")
        user = users(username=name)
        password = read_password(user, options)
        with transaction.atomic():
            user.set_password(password)
            user.save()
            AdminAccount.objects.create(user=user, role=options["role"])
            new_totp(user, self.stdout)
            new_recovery_codes(user, self.stdout)
            staff.audit_from_command_line("createadmin", name, options["role"])
        self.stdout.write(
            f"\n{options['role']} {name} made. Log in at the admin pages' log-in page "
            f"(OPENCS_WEB_ADMIN_PATH), with OPENCS_WEB_ADMIN=true.\n"
        )
