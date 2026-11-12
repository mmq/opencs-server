"""What ``createadmin`` and ``resetadmin`` share: the password, asked for or read from standard
input, and the second factor, made and shown once."""

from __future__ import annotations

import getpass
import io
import sys
from base64 import b32encode
from typing import Any

import segno
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser, OutputWrapper
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

#: Recovery codes made at a time; each works once.
RECOVERY_CODES = 10


def password_argument(parser: CommandParser) -> None:
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from the first line of standard input instead of asking",
    )


def read_password(user: Any, options: dict[str, Any]) -> str:
    """A new password, held to the site's password rules (12 characters or more, not common, not
    all digits, not like the name)."""
    if options["password_stdin"]:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Password again: ") != password:
            raise CommandError("the passwords differ")
    try:
        validate_password(password, user)
    except ValidationError as e:
        raise CommandError(" ".join(e.messages)) from None
    return password


def new_totp(user: Any, out: OutputWrapper) -> None:
    """Replace the admin's authenticator device and show its secret, this once."""
    TOTPDevice.objects.filter(user=user).delete()
    device = TOTPDevice.objects.create(user=user, name="authenticator", confirmed=True)
    secret = b32encode(device.bin_key).decode("ascii").rstrip("=")
    # Drawn in text, light squares as blocks, so it scans from a terminal with a dark background;
    # if a phone will not read it, the key and the URI below say the same.
    qr = io.StringIO()
    segno.make(device.config_url, error="m").terminal(out=qr, compact=True)
    out.write(
        "\nAdd this to an authenticator app (shown only now; anyone who has it has the "
        "second factor). Scan the code:\n\n"
        f"{qr.getvalue()}\n"
        f"  or type the key:  {' '.join(secret[i : i + 4] for i in range(0, len(secret), 4))}\n"
        f"  or paste the URI: {device.config_url}\n"
        "  (TOTP, SHA-1, 6 digits, 30 seconds)\n"
    )


def new_recovery_codes(user: Any, out: OutputWrapper) -> None:
    """Replace the admin's recovery codes and show them, this once."""
    StaticDevice.objects.filter(user=user).delete()
    device = StaticDevice.objects.create(user=user, name="recovery", confirmed=True)
    codes = [StaticToken.random_token() for _ in range(RECOVERY_CODES)]
    for code in codes:
        device.token_set.create(token=code)
    out.write(
        "\nRecovery codes, each good for one log-in in place of the app's code (shown only "
        "now: keep them somewhere apart from the app):\n" + "".join(f"  {code}\n" for code in codes)
    )
