"""The web's own tables, in its own SQLite file (``OPENCS_WEB_DATABASE``), never the game server's:
web accounts, the links it has emailed, a note of each email sent, for the rate limits, and the
admin pages' accounts and audit log."""

from __future__ import annotations

from typing import Any, NoReturn

from django.conf import settings
from django.db import models


class Player(models.Model):
    """A web account: a verified email address and the one game account it registered. The game
    account itself, its password and callsign, are the game server's (ADR 10); ``game_created``
    tells it from a later account of the same name, made after this one was deleted."""

    email = models.EmailField(unique=True)
    game_name = models.CharField(max_length=12, unique=True)
    #: When the game server made the account (Unix time), as its ``register`` said.
    game_created = models.FloatField(null=True)
    created = models.DateTimeField(auto_now_add=True)
    last_login = models.DateTimeField(null=True)

    def __str__(self) -> str:
        return self.game_name


class EmailLink(models.Model):
    """A link the site has emailed: signed and expiring (``accounts.make_token``), and used once. A
    registration's link carries the account name and callsign asked for; a log-in's only the
    address."""

    REGISTER = "register"
    LOGIN = "login"
    KINDS = ((REGISTER, "register"), (LOGIN, "log in"))

    kind = models.CharField(max_length=10, choices=KINDS)
    email = models.EmailField()
    #: Random, and in the link: a link is good only with the row's own secret.
    secret = models.CharField(max_length=32)
    game_name = models.CharField(max_length=12, blank=True)
    callsign = models.CharField(max_length=12, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    #: Set while the game server is asked to make the account, so two clicks make one.
    claimed = models.DateTimeField(null=True)
    used = models.DateTimeField(null=True)

    class Meta:
        indexes = (models.Index(fields=["created"]),)


class EmailSent(models.Model):
    """One email sent, or tried: when, to which address (a keyed digest, not the address) and
    from which source (an IPv4 address or IPv6 /64). Kept a day or two, for the rate limits."""

    at = models.DateTimeField(db_index=True)
    recipient = models.CharField(max_length=64, db_index=True)
    source = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=16)


class AdminAccount(models.Model):
    """Who may use the admin pages: a Django user (its name and password, hashed by Django's
    Argon2 hasher) with a role, made only from the command line (``opencs-web createadmin``). Its
    second factor is a django-otp TOTP device, its recovery codes a static device. Players' web
    accounts (:class:`Player`) are a different thing and never reach these pages."""

    ADMIN = "admin"
    MODERATOR = "moderator"
    ROLES = ((ADMIN, "admin"), (MODERATOR, "moderator"))

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="opencs_admin"
    )
    role = models.CharField(max_length=16, choices=ROLES)
    #: Wrong log-ins in a row; at ``staff.LOCK_AFTER`` the account is locked until
    #: ``locked_until``.
    failures = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True)
    #: Raised by every change made from the command line (password, second factor, role): a
    #: session logged in before it ends.
    generation = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.user.get_username()} ({self.role})"


class AppendOnlyQuerySet(models.QuerySet["AuditEntry"]):
    def update(self, **kwargs: Any) -> NoReturn:
        raise TypeError("the audit log is append-only")

    def delete(self) -> NoReturn:
        raise TypeError("the audit log is append-only")


class AuditEntry(models.Model):
    """One thing done on the admin pages, or tried: who, when, from where, what to whom, and how
    it went. The app only ever adds entries; nothing it runs changes or deletes one. Never a
    password."""

    OK = "ok"
    REFUSED = "refused"  # the game server said no
    OFFLINE = "offline"  # the game server did not answer
    DENIED = "denied"  # not allowed: the role, a wrong code, a wrong password
    RESULTS = ((OK, OK), (REFUSED, REFUSED), (OFFLINE, OFFLINE), (DENIED, DENIED))

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    who = models.CharField(max_length=150)
    role = models.CharField(max_length=16, blank=True)
    source = models.CharField(max_length=64, blank=True)
    action = models.CharField(max_length=32)
    target = models.CharField(max_length=64, blank=True)
    result = models.CharField(max_length=16, choices=RESULTS)
    detail = models.TextField(blank=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ("-at", "-id")

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise TypeError("the audit log is append-only")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError("the audit log is append-only")

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M:%S} {self.who} {self.action} {self.target} {self.result}"
