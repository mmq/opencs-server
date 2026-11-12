"""The web's own tables, in its own SQLite file (``OPENCS_WEB_DATABASE``), never the game server's:
web accounts, the links it has emailed, and a note of each email sent, for the rate limits."""

from __future__ import annotations

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
