"""``opencs-web resetadmin NAME ...``: change an account for the admin pages. Any change ends
the admin's sessions."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.db.models import F

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
        "Change an admin: a new password, a new authenticator secret, new recovery codes, "
        "another role, unlock it after wrong log-ins, or remove it. Each ends its log-ins."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("name")
        parser.add_argument("--password", action="store_true", help="set a new password")
        password_argument(parser)
        parser.add_argument(
            "--totp", action="store_true", help="a new authenticator secret (a lost phone)"
        )
        parser.add_argument("--codes", action="store_true", help="new recovery codes")
        parser.add_argument("--role", choices=[AdminAccount.ADMIN, AdminAccount.MODERATOR])
        parser.add_argument("--unlock", action="store_true", help="forget wrong log-ins")
        parser.add_argument(
            "--remove", action="store_true", help="remove the admin (the audit log keeps its name)"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        name = options["name"].strip()
        account = AdminAccount.objects.select_related("user").filter(user__username=name).first()
        if account is None:
            raise CommandError(f"no admin {name}")
        user = account.user
        done = []
        if options["remove"]:
            with transaction.atomic():
                user.delete()  # the account and the devices with it
                staff.audit_from_command_line("removeadmin", name)
            self.stdout.write(f"{name} removed.\n")
            return
        password = read_password(user, options) if options["password"] else None
        with transaction.atomic():
            if password is not None:
                user.set_password(password)
                user.save()
                done.append("password")
            if options["totp"]:
                new_totp(user, self.stdout)
                done.append("authenticator")
            if options["codes"]:
                new_recovery_codes(user, self.stdout)
                done.append("recovery codes")
            if options["role"]:
                AdminAccount.objects.filter(pk=account.pk).update(role=options["role"])
                done.append(f"role {options['role']}")
            if options["unlock"]:
                AdminAccount.objects.filter(pk=account.pk).update(failures=0, locked_until=None)
                done.append("unlocked")
            if not done:
                raise CommandError("nothing to change: see --help")
            AdminAccount.objects.filter(pk=account.pk).update(generation=F("generation") + 1)
            staff.audit_from_command_line("resetadmin", name, ", ".join(done))
        self.stdout.write(f"\n{name}: {', '.join(done)}. Its log-ins have ended.\n")
