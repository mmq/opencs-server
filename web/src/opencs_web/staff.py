"""The admin pages' log-in, roles, audit log and requests to the game server (``staffviews`` has
the pages).

- An admin is a Django user with an :class:`~opencs_web.models.AdminAccount`, made only from the
  command line (``opencs-web createadmin``). Django's own admin site is not installed, and a user
  without an ``AdminAccount`` (one made by ``createsuperuser``, say) cannot log in here.
- A log-in takes the name, the password and a code from the authenticator app (django-otp's
  TOTP device) or a recovery code (its static device; each works once), on one form, so a wrong
  password and a wrong code look the same. ``LOCK_AFTER`` wrong ones in a row lock the account
  for ``LOCK_SECONDS``; tries from one network are limited too (``TRIES_PER_SOURCE``).
- The log-in is kept in the session under ``SESSION_KEY``, beside a player's (the same cookie),
  and lasts ``OPENCS_ADMIN_SESSION_MINUTES`` at most and ``IDLE_SECONDS`` without a page.
  Anything changed about the admin from the command line ends it (``AdminAccount.generation``).
- A destructive action asks for a fresh code unless one was given in the last
  ``REAUTH_SECONDS``.
- Roles: an admin does everything; a moderator sees the status, kicks and announces.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from django_otp.models import Device
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from opencs.net.listener import source_of
from opencs.ratelimit import KeyedBuckets
from opencs_web import live
from opencs_web.models import AdminAccount, AuditEntry

log = logging.getLogger("opencs_web.staff")

SESSION_KEY = "opencs_admin"
#: An admin's log-in ends after this long without a page.
IDLE_SECONDS = 15 * 60
#: A destructive action needs a code given this recently, or a fresh one.
REAUTH_SECONDS = 5 * 60
#: Wrong log-ins in a row before an account is locked, and for how long.
LOCK_AFTER = 5
LOCK_SECONDS = 15 * 60
#: Log-in tries from one network (an IPv4 address or an IPv6 /64): a burst, then one a minute.
TRIES_PER_SOURCE = (10, 1 / 60)

#: The authenticator app's codes are this many digits (django-otp's default).
TOTP_DIGITS = 6

#: What each role may do. The status is every role's.
MODERATOR_ACTIONS = frozenset({"kick", "announce"})

_tries: KeyedBuckets[str] = KeyedBuckets(*TRIES_PER_SOURCE)


def _now() -> float:
    """The time, for the session's limits (tests move it)."""
    return time.time()


def reset() -> None:
    """Forget the log-in tries counted (tests)."""
    global _tries  # noqa: PLW0603
    _tries = KeyedBuckets(*TRIES_PER_SOURCE)


def source(request: HttpRequest) -> str:
    return source_of(request.META.get("REMOTE_ADDR") or "?")


def may(role: str, action: str) -> bool:
    return role == AdminAccount.ADMIN or action in MODERATOR_ACTIONS


# --- logging in ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LoginResult:
    account: AdminAccount | None
    #: Recovery codes left, if one was used to log in.
    codes_left: int | None = None


def _dummy_hash(password: str) -> None:
    """The cost of checking a password, for a name with no admin: the time a log-in takes does
    not say whether the name exists."""
    make_password(password)


def check_login(name: str, password: str, code: str, where: str) -> LoginResult:
    """The admin ``name`` if its password and code are right and it may log in now; a failure is
    counted and audited. Runs on Django's database thread (it hashes)."""
    now = timezone.now()
    if not _tries.take(where, time.monotonic()):
        _audit("login", AuditEntry.DENIED, who=name, where=where, detail="too many tries")
        return LoginResult(None)
    user = get_user_model().objects.filter(username=name, is_active=True).first()
    account = AdminAccount.objects.filter(user=user).first() if user is not None else None
    if user is None or account is None:
        _dummy_hash(password)
        _audit("login", AuditEntry.DENIED, who=name, where=where, detail="no such admin")
        return LoginResult(None)
    if account.locked_until is not None and account.locked_until > now:
        _dummy_hash(password)
        _audit("login", AuditEntry.DENIED, who=name, where=where, detail="locked")
        return LoginResult(None)
    if not user.check_password(password):
        _failed(account, where, "wrong password", now)
        return LoginResult(None)
    device = _match(user, code)
    if device is None:
        _failed(account, where, "wrong code", now)
        return LoginResult(None)
    AdminAccount.objects.filter(pk=account.pk).update(failures=0, locked_until=None)
    user.last_login = now
    user.save(update_fields=["last_login"])
    codes_left = None
    detail = ""
    if isinstance(device, StaticDevice):
        codes_left = device.token_set.count()
        detail = f"with a recovery code ({codes_left} left)"
    _audit("login", AuditEntry.OK, account=account, where=where, detail=detail)
    return LoginResult(account, codes_left)


def _failed(account: AdminAccount, where: str, why: str, now: Any) -> None:
    with transaction.atomic():
        account.refresh_from_db()
        failures = account.failures + 1
        locked = None
        if failures >= LOCK_AFTER:
            locked = now + timedelta(seconds=LOCK_SECONDS)
            log.warning(
                "admin %s locked for %d minutes after %d wrong log-ins",
                account.user.get_username(),
                LOCK_SECONDS // 60,
                failures,
            )
            why += f"; locked for {LOCK_SECONDS // 60} minutes"
            failures = 0
        AdminAccount.objects.filter(pk=account.pk).update(failures=failures, locked_until=locked)
    _audit("login", AuditEntry.DENIED, account=account, where=where, detail=why)


def check_code(account: AdminAccount, code: str) -> bool:
    """A fresh code for a destructive action (database thread)."""
    return _match(account.user, code) is not None


def _match(user: Any, code: str) -> Device | None:
    """The device that takes ``code``: six digits are the authenticator app's, anything else a
    recovery code (used up by this). Only the one kind is tried, so a wrong code slows down
    (django-otp's throttling) only the device it was meant for."""
    code = code.replace(" ", "").replace("-", "")
    if not code:
        return None
    totp = code.isascii() and code.isdigit() and len(code) == TOTP_DIGITS
    model = TOTPDevice if totp else StaticDevice
    with transaction.atomic():
        for device in model.objects.devices_for_user(user, confirmed=True).select_for_update():
            if device.verify_token(code if totp else code.lower()):
                return device
    return None


# --- the session ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Admin:
    """The admin a request is from."""

    account: AdminAccount
    #: When the log-in was, and when a code was last given (Unix times).
    since: float
    verified: float

    @property
    def name(self) -> str:
        return str(self.account.user.get_username())

    @property
    def role(self) -> str:
        return str(self.account.role)

    @property
    def recently_verified(self) -> bool:
        return _now() - self.verified < REAUTH_SECONDS


async def log_in(request: HttpRequest, account: AdminAccount) -> None:
    from django.middleware.csrf import rotate_token  # noqa: PLC0415

    await request.session.acycle_key()  # a session made before the log-in is not this one
    now = _now()
    await request.session.aset(
        SESSION_KEY,
        {
            "id": account.pk,
            "generation": account.generation,
            "since": now,
            "seen": now,
            "verified": now,
        },
    )
    rotate_token(request)


async def log_out(request: HttpRequest) -> None:
    await request.session.apop(SESSION_KEY, None)
    await request.session.acycle_key()


async def current(request: HttpRequest) -> Admin | None:
    """The admin logged in, or None: not logged in, or the log-in has expired (too old, idle, or
    the admin was changed or removed since)."""
    held = await request.session.aget(SESSION_KEY)
    if not isinstance(held, dict):
        return None
    now = _now()
    try:
        since, seen, verified = float(held["since"]), float(held["seen"]), float(held["verified"])
        pk, generation = int(held["id"]), int(held["generation"])
    except (KeyError, TypeError, ValueError):
        since = seen = verified = 0.0
        pk = generation = -1
    expired = now - since > settings.OPENCS_ADMIN_SESSION_MINUTES * 60 or now - seen > IDLE_SECONDS
    account = (
        None
        if expired
        else await AdminAccount.objects.select_related("user")
        .filter(pk=pk, generation=generation, user__is_active=True)
        .afirst()
    )
    if account is None:
        await request.session.apop(SESSION_KEY, None)
        return None
    await request.session.aset(SESSION_KEY, {**held, "seen": now})
    return Admin(account, since, verified)


async def verified_now(request: HttpRequest) -> None:
    """A code was given just now (a destructive action's)."""
    held = await request.session.aget(SESSION_KEY)
    if isinstance(held, dict):
        await request.session.aset(SESSION_KEY, {**held, "verified": _now()})


# --- the audit log -------------------------------------------------------------------------------


def _audit(
    action: str,
    result: str,
    *,
    account: AdminAccount | None = None,
    who: str = "",
    where: str = "",
    target: str = "",
    detail: str = "",
) -> None:
    AuditEntry.objects.create(
        who=account.user.get_username() if account is not None else who[:150],
        role=account.role if account is not None else "",
        source=where[:64],
        action=action,
        target=target[:64],
        result=result,
        detail=detail,
    )


async def audit(
    admin: Admin | None,
    action: str,
    result: str,
    *,
    request: HttpRequest,
    target: str = "",
    detail: str = "",
) -> None:
    await sync_to_async(_audit)(
        action,
        result,
        account=admin.account if admin is not None else None,
        who="(not logged in)" if admin is None else "",
        where=source(request),
        target=target,
        detail=detail,
    )


def audit_from_command_line(action: str, name: str, detail: str = "") -> None:
    """A change made with ``createadmin`` or ``resetadmin``."""
    _audit(action, AuditEntry.OK, who="(command line)", target=name, detail=detail)


# --- the game server -----------------------------------------------------------------------------

#: A first reply line that means the server did not do it (``opencs.control``'s and
#: ``opencs.cities``' words). "no city changed" after a reload is not one.
_REFUSAL = re.compile(
    r"(refused|reload refused|usage:|error:|unknown command|at most|no pilot |"
    r"no shutdown is pending|no config file|no \w+ guppy exists)"
)


def outcome(result: object) -> tuple[str, list[str]]:
    """``ok`` or ``refused``, and the reply lines, from an operator command's result."""
    lines = result.get("lines", []) if isinstance(result, dict) else []
    lines = [line for line in lines if isinstance(line, str)]
    refused = bool(lines) and _REFUSAL.match(lines[0]) is not None
    return (AuditEntry.REFUSED if refused else AuditEntry.OK), lines


async def request(op: str, **args: Any) -> Any:
    """One request over the service channel, shared with the status cache and the account
    pages. Raises ``UnavailableError`` or ``RefusedError`` as they do."""
    return await live.channel().request(op, **args)
