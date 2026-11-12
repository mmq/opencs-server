"""Registration and web accounts: what the pages in ``accountviews`` share.

- The game server keeps the accounts (ADR 10): names, callsigns, passwords and their rules. The
  web asks it over the service channel (:class:`Game`) and shows its refusals as they come; it
  checks only the shape of a name or callsign first, to save a round trip.
- Links in emails are signed and expire (:func:`make_token`, :func:`read_token`), and are good
  once (``EmailLink.used``). Following one shows a page with a button: a mail scanner that
  fetches the link uses nothing up.
- The forms carry a challenge that needs no third party: a field hidden from people, which a form
  filler fills, and a signed time, so a form sent back within ``OPENCS_FORM_MIN_SECONDS`` of being
  shown, or after ``FORM_MAX_SECONDS``, is refused. That turns away the scripts that fill every
  form they find. A determined sender gets no further than the rate limits (:func:`reserve`):
  what it could cost is emails, and those are limited per source, per address and per day. A
  proof of work would cost a phone more than a script, and a CAPTCHA service would see every
  visitor.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core import signing
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from opencs import policy
from opencs.ratelimit import KeyedBuckets, TokenBucket
from opencs_web import live
from opencs_web.channel import RefusedError
from opencs_web.models import EmailLink, EmailSent

log = logging.getLogger("opencs_web.accounts")

#: How long the links in emails work.
REGISTER_LINK_SECONDS = 24 * 3600
LOGIN_LINK_SECONDS = 30 * 60
#: A form older than this is shown again rather than taken (its time was signed when shown).
FORM_MAX_SECONDS = 2 * 3600
#: A registration claimed this long ago without an answer was cut short (a restart): it may be
#: tried again.
CLAIM_SECONDS = 60
#: Emails asked for, (how many, in how many seconds): from one source (an IPv4 address or an
#: IPv6 /64), and to one address, whatever kind.
PER_SOURCE = ((5, 3600), (20, 86400))
PER_RECIPIENT = ((3, 3600), (6, 86400))
#: The daily cap is counted over this long.
DAY = 86400
#: Sent-email notes are kept this long, then deleted.
KEEP_SENT = 2 * DAY


# --- the game server ---------------------------------------------------------------------------


class Game:
    """The account operations of the service channel. Each raises ``UnavailableError`` when the
    game server does not answer, and ``RefusedError`` with the server's own words when it says
    no."""

    async def account(self, name: str) -> dict[str, Any]:
        result: dict[str, Any] = await live.channel().request("account", name=name)
        return result

    async def register(
        self, name: str, callsign: str | None, password: str | None
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"name": name}
        if callsign:
            args["callsign"] = callsign
        if password:
            args["password"] = password
        result: dict[str, Any] = await live.channel().request("register", **args)
        return result

    async def reset_password(self, name: str, password: str | None) -> dict[str, Any]:
        args: dict[str, Any] = {"name": name}
        if password:
            args["password"] = password
        result: dict[str, Any] = await live.channel().request("reset-password", **args)
        return result

    async def set_callsign(self, name: str, callsign: str) -> dict[str, Any]:
        result: dict[str, Any] = await live.channel().request(
            "set-callsign", name=name, callsign=callsign
        )
        return result

    async def delete_account(self, name: str) -> None:
        """Delete the account and its progress. The operator's command answers in lines: a
        refusal is put in a player's words."""
        result = await live.channel().request("delete-account", name=name)
        lines = result.get("lines", []) if isinstance(result, dict) else []
        first = lines[0] if lines and isinstance(lines[0], str) else "?"
        if first.startswith("deleted "):
            return
        if "is online" in first:
            message = "That account is in the game now: log off first."
        elif "held for /RESUME" in first:
            message = "That account has a game held for /RESUME: wait until it ends."
        elif "no account" in first:
            message = "There is no such account on the game server."
        else:
            message = first
        raise RefusedError("refused", message)


#: Requests the pages make of the game server for a visitor (is a name taken, is this web
#: account's game account still there): per source, a burst and then one every few seconds, and
#: in all, well below the channel's own 20 a second, so a flood of forms cannot crowd out the
#: status page.
ASKS_PER_SOURCE = (10, 1 / 6)
ASKS_IN_ALL = (20, 2.0)

_game: Game | None = None
_asks_per_source: KeyedBuckets[str] = KeyedBuckets(*ASKS_PER_SOURCE)
_asks_in_all = TokenBucket(*ASKS_IN_ALL, time.monotonic())


def game() -> Game:
    global _game  # noqa: PLW0603
    if _game is None:
        _game = Game()
    return _game


def may_ask(source: str) -> bool:
    """Whether a page for ``source`` may ask the game server something now."""
    now = time.monotonic()
    if not _asks_per_source.take(source, now):
        return False
    if not _asks_in_all.take(now):
        _asks_per_source.give_back(source, now)
        return False
    return True


def reset() -> None:
    """Forget the game and the requests counted (tests)."""
    global _game, _asks_per_source, _asks_in_all  # noqa: PLW0603
    _game = None
    _asks_per_source = KeyedBuckets(*ASKS_PER_SOURCE)
    _asks_in_all = TokenBucket(*ASKS_IN_ALL, time.monotonic())


def is_ours(found: dict[str, Any], created: float | None) -> bool:
    """Whether the server's ``account`` answer is the account this web account registered: it
    exists, and was made when ours was (not a later one of the same name)."""
    if not found.get("exists"):
        return False
    return created is None or found.get("created") is None or found.get("created") == created


# --- the shape of names, before asking the server ----------------------------------------------


def name_problem(name: str) -> str | None:
    """The cheap part of the server's rules for a new account name (``Identity``), in its words;
    the rest (reserved names, taken ones) is the server's to say."""
    low, high = policy.MIN_ACCOUNT_NAME_LENGTH, policy.MAX_CALLSIGN_LENGTH
    if not low <= len(name) <= high:
        return f"A NEW NAME MUST BE {low} TO {high} CHARACTERS"
    if any(ch not in policy.ACCOUNT_NAME_CHARACTERS for ch in name):
        return "A NEW NAME MAY HOLD ONLY LETTERS, DIGITS, _ AND -"
    return None


def callsign_problem(callsign: str) -> str | None:
    low, high = policy.MIN_CALLSIGN_LENGTH, policy.MAX_CALLSIGN_LENGTH
    if not low <= len(callsign) <= high:
        return f"A CALLSIGN MUST BE {low} TO {high} CHARACTERS"
    if any(ch not in policy.CALLSIGN_CHARACTERS for ch in callsign):
        return "A CALLSIGN MAY HOLD LETTERS, DIGITS AND PUNCTUATION, NO SPACES"
    return None


def password_problem(password: str) -> str | None:
    if len(password) > policy.MAX_NEW_PASSWORD_LENGTH:
        return f"A PASSWORD MAY BE AT MOST {policy.MAX_NEW_PASSWORD_LENGTH} CHARACTERS"
    if any(ch not in policy.NEW_PASSWORD_CHARACTERS for ch in password):
        return "A PASSWORD MAY HOLD ONLY LETTERS, DIGITS AND PUNCTUATION, NO SPACES"
    return None


def normal_email(email: str) -> str:
    """One spelling per address: the case of an address is not worth a second account."""
    return email.strip().lower()


# --- the challenge -----------------------------------------------------------------------------

_FORM_SALT = "opencs_web.form"


def form_stamp() -> str:
    """The signed time a form was shown, for its hidden field."""
    return signing.dumps(time.time(), salt=_FORM_SALT)


def stamp_problem(stamp: str) -> str | None:
    """Why a form's time is not right: sent back too soon to have been filled in by a person,
    too late, or not ours."""
    try:
        shown = signing.loads(stamp, salt=_FORM_SALT, max_age=FORM_MAX_SECONDS)
    except signing.SignatureExpired:
        return "The form was open too long. Please fill it in again."
    except signing.BadSignature:
        return "The form was not understood. Please fill it in again."
    if not isinstance(shown, float) or time.time() - shown < settings.OPENCS_FORM_MIN_SECONDS:
        return "That was quicker than a person types. Please wait a moment and send it again."
    return None


# --- links -------------------------------------------------------------------------------------


async def new_link(kind: str, email: str, game_name: str = "", callsign: str = "") -> EmailLink:
    """A link to email. Links long expired go first: they hold addresses never verified."""
    old = timezone.now() - timedelta(seconds=REGISTER_LINK_SECONDS + KEEP_SENT)
    await EmailLink.objects.filter(created__lt=old).adelete()
    return await EmailLink.objects.acreate(
        kind=kind,
        email=email,
        secret=secrets.token_hex(16),
        game_name=game_name,
        callsign=callsign,
    )


def make_token(link: EmailLink) -> str:
    return signing.TimestampSigner(salt=f"opencs_web.link.{link.kind}").sign(
        f"{link.pk}.{link.secret}"
    )


async def read_token(kind: str, token: str) -> EmailLink | None:
    """The unused link ``token`` names, or None: not ours, expired, or used."""
    max_age = REGISTER_LINK_SECONDS if kind == EmailLink.REGISTER else LOGIN_LINK_SECONDS
    try:
        value = signing.TimestampSigner(salt=f"opencs_web.link.{kind}").unsign(
            token, max_age=max_age
        )
    except signing.BadSignature:  # expired too
        return None
    pk, _, secret = value.partition(".")
    if not pk.isdigit():
        return None
    link = await EmailLink.objects.filter(pk=int(pk), kind=kind, used__isnull=True).afirst()
    if link is None or not constant_time_compare(link.secret, secret):
        return None
    return link


async def claim(link: EmailLink) -> bool:
    """Take ``link`` for one request: False if it is used, or another request holds it."""
    now = timezone.now()
    taken = await EmailLink.objects.filter(
        Q(claimed__isnull=True) | Q(claimed__lt=now - timedelta(seconds=CLAIM_SECONDS)),
        pk=link.pk,
        used__isnull=True,
    ).aupdate(claimed=now)
    return taken == 1


async def release(link: EmailLink) -> None:
    await EmailLink.objects.filter(pk=link.pk).aupdate(claimed=None)


async def use(link: EmailLink) -> bool:
    """Mark ``link`` used: False if it already was."""
    done = await EmailLink.objects.filter(pk=link.pk, used__isnull=True).aupdate(
        used=timezone.now()
    )
    return done == 1


def link_url(request: HttpRequest, path: str) -> str:
    """The full address of ``path`` for an email: under ``OPENCS_WEB_SITE_URL``, else under the
    address the request came to (a host in ``ALLOWED_HOSTS``)."""
    base = settings.OPENCS_SITE_URL
    return f"{base}{path}" if base else request.build_absolute_uri(path)


# --- sending -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    """Why no email was sent, for the page."""

    reason: str  # "cap", "source", "recipient", "failed"


def _recipient_key(email: str) -> str:
    """A keyed digest of the address: the limits need to know two sends went to one address,
    not which address it was."""
    return salted_hmac("opencs_web.recipient", email, algorithm="sha256").hexdigest()


def _reserve(kind: str, email: str, source: str, now: datetime) -> str | None:
    with transaction.atomic():
        EmailSent.objects.filter(at__lt=now - timedelta(seconds=KEEP_SENT)).delete()
        if kind != "password":  # a password follows something already sent: never stopped
            if (
                EmailSent.objects.filter(at__gte=now - timedelta(seconds=DAY)).count()
                >= settings.OPENCS_EMAIL_DAILY_CAP
            ):
                return "cap"
            recipient = _recipient_key(email)
            for field, value, limits in (
                ("source", source, PER_SOURCE),
                ("recipient", recipient, PER_RECIPIENT),
            ):
                for count, seconds in limits:
                    recent = EmailSent.objects.filter(
                        at__gte=now - timedelta(seconds=seconds), **{field: value}
                    )
                    if recent.count() >= count:
                        return field
        EmailSent.objects.create(at=now, recipient=_recipient_key(email), source=source, kind=kind)
    return None


async def reserve(kind: str, email: str, source: str) -> Refusal | None:
    """Count an email about to be sent, or say why it may not be. Checked and counted in one
    transaction, on Django's one thread for the database, so two requests cannot both take the
    last place."""
    reason = await sync_to_async(_reserve)(kind, email, source, timezone.now())
    if reason is not None:
        log.info("%s email refused: %s limit", kind, reason)
        return Refusal(reason)
    return None


def _send(subject: str, body: str, to: str) -> bool:
    try:
        EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, [to]).send()
    except Exception as e:  # the provider down, refusing, a timeout: all mean "not sent"
        log.warning("could not send an email (%s): %s", subject, e)
        return False
    return True


async def send(template: str, context: dict[str, Any], to: str) -> bool:
    """Render ``opencs_web/email/<template>.txt`` (its first line the subject) and send it;
    False if it could not be. Sent on a thread of its own: an SMTP server may be slow."""
    text = render_to_string(f"opencs_web/email/{template}.txt", context)
    subject, _, body = text.partition("\n")
    return await sync_to_async(_send, thread_sensitive=False)(subject.strip(), body.lstrip(), to)
