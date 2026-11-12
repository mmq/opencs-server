"""The registration, log-in and account pages (``accounts`` has what they share).

A registration: the form (name, optional callsign, email) -> an email with a link -> the link's
page, where a button makes the game account over the service channel -> the password, shown or
emailed as ``OPENCS_WEB_PASSWORD_DELIVERY`` says. Nothing on the way says whether an address
already has an account: that address is sent an email saying so instead. A log-in is an emailed
link as well. Every page that acts for a web account first asks the server whether its game
account is still the one it registered (:func:`_check`); if the operator, or the daily prune of
unplayed accounts, removed it, the web account is removed too.
"""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from opencs.net.listener import source_of
from opencs_web import accounts
from opencs_web.accounts import Refusal
from opencs_web.channel import RefusedError, UnavailableError
from opencs_web.forms import (
    CallsignForm,
    ConfirmForm,
    DeleteForm,
    LoginForm,
    NewPasswordForm,
    RegisterForm,
)
from opencs_web.models import EmailLink, Player
from opencs_web.siteassets import asset_version

log = logging.getLogger("opencs_web.accounts")

#: The session's key for the web account logged in.
PLAYER = "player"
#: A line for the account page after a redirect.
NOTICE = "notice"

OFFLINE = "The game server is not answering just now. Please try again in a few minutes."
TOO_FAST = "Too many requests from your network just now. Please wait a minute and try again."


def site(request: HttpRequest) -> dict[str, Any]:
    """What every page's header and footer need (a context processor: no database)."""
    return {
        "assets": asset_version(),
        "contact": settings.OPENCS_CONTACT,
        "registration_open": settings.OPENCS_REGISTRATION,
    }


def _source(request: HttpRequest) -> str:
    return source_of(request.META.get("REMOTE_ADDR") or "?")


def _private(response: HttpResponse) -> HttpResponse:
    """A page for one person: no cache keeps it."""
    response["Cache-Control"] = "no-store"
    return response


# --- the session -------------------------------------------------------------------------------


async def _player(request: HttpRequest) -> Player | None:
    pk = await request.session.aget(PLAYER)
    if not isinstance(pk, int):
        return None
    return await Player.objects.filter(pk=pk).afirst()


async def _log_in(request: HttpRequest, player: Player) -> None:
    await request.session.acycle_key()  # a session made before the log-in is not this one
    await request.session.aset(PLAYER, player.pk)
    await Player.objects.filter(pk=player.pk).aupdate(last_login=timezone.now())


async def _check(request: HttpRequest, player: Player) -> tuple[dict[str, Any] | None, bool]:
    """The game account as the server has it now (None while it does not answer), and whether it
    is gone. A gone one's web account is deleted and the session ended: it must not go on
    claiming the name, which someone else may now register. Past ``accounts.may_ask`` the page
    goes on as if the server did not answer."""
    if not accounts.may_ask(_source(request)):
        return None, False
    try:
        found = await accounts.game().account(player.game_name)
    except UnavailableError:
        return None, False
    except RefusedError as e:
        log.warning("the game server would not look up %s: %s", player.game_name, e)
        return None, False
    if accounts.is_ours(found, player.game_created):
        return found, False
    log.info("game account %s is gone from the server: its web account is removed", player)
    await player.adelete()
    await request.session.aflush()
    return None, True


def _gone(request: HttpRequest, name: str) -> HttpResponse:
    return _private(render(request, "opencs_web/gone.html", {"name": name}, status=410))


# --- registration ------------------------------------------------------------------------------


@require_http_methods(["GET", "HEAD", "POST"])
async def register(request: HttpRequest) -> HttpResponse:
    if not settings.OPENCS_REGISTRATION:
        return render(request, "opencs_web/register_closed.html")
    if request.method != "POST":
        return _private(render(request, "opencs_web/register.html", {"form": RegisterForm()}))
    form = RegisterForm(request.POST)
    if form.caught:
        log.info("a registration form with its hidden field filled in was ignored")
        return _sent(request, form.data.get("email", ""))
    if form.is_valid() and not accounts.may_ask(_source(request)):
        form.add_error(None, TOO_FAST)
    elif form.is_valid():
        data = form.cleaned_data
        try:
            found = await accounts.game().account(data["name"])
        except (UnavailableError, RefusedError):
            form.add_error(None, OFFLINE)
        else:
            if found.get("exists"):
                form.add_error("name", "THAT NAME IS TAKEN")
            else:
                return await _send_registration(request, data)
    form.renew()
    return _private(render(request, "opencs_web/register.html", {"form": form}))


async def _send_registration(request: HttpRequest, data: dict[str, Any]) -> HttpResponse:
    email = data["email"]
    if (refusal := await accounts.reserve("register", email, _source(request))) is not None:
        return _not_sent(request, refusal)
    player = await Player.objects.filter(email=email).afirst()
    if player is not None:  # said by email only: the page is the same either way
        sent = await accounts.send(
            "already",
            {"name": player.game_name, "login": accounts.link_url(request, reverse("login"))},
            email,
        )
    else:
        link = await accounts.new_link(EmailLink.REGISTER, email, data["name"], data["callsign"])
        url = accounts.link_url(request, reverse("confirm", args=[accounts.make_token(link)]))
        sent = await accounts.send(
            "verify",
            {"name": data["name"], "url": url, "hours": accounts.REGISTER_LINK_SECONDS // 3600},
            email,
        )
        if not sent:
            await link.adelete()
    if not sent:
        return _not_sent(request, Refusal("failed"))
    return _sent(request, email)


def _sent(request: HttpRequest, email: str) -> HttpResponse:
    return _private(render(request, "opencs_web/sent.html", {"email": email}))


def _not_sent(request: HttpRequest, refusal: Refusal) -> HttpResponse:
    status = 503 if refusal.reason in ("failed", "cap") else 429
    return _private(
        render(request, "opencs_web/not_sent.html", {"reason": refusal.reason}, status=status)
    )


@sensitive_post_parameters("password")
@require_http_methods(["GET", "HEAD", "POST"])
async def confirm(request: HttpRequest, token: str) -> HttpResponse:
    """The emailed link: the address is verified. A button makes the game account, so a mail
    scanner fetching the link makes nothing. A refused name or callsign may be changed here, and
    while the game server does not answer the link keeps working, until it expires."""
    link = await accounts.read_token(EmailLink.REGISTER, token)
    if link is None:
        return _bad_link(request)
    chosen = settings.OPENCS_CHOSEN_PASSWORDS
    if request.method != "POST":
        form = ConfirmForm(
            initial={"name": link.game_name, "callsign": link.callsign}, chosen=chosen
        )
        return _confirm_page(request, form, link)
    if await Player.objects.filter(email=link.email).aexists():
        await accounts.use(link)
        return _private(render(request, "opencs_web/already.html"))
    form = ConfirmForm(request.POST, chosen=chosen)
    if not form.is_valid():
        return _confirm_page(request, form, link)
    if not await accounts.claim(link):
        return _bad_link(request)
    return await _make_account(request, link, form)


async def _make_account(request: HttpRequest, link: EmailLink, form: ConfirmForm) -> HttpResponse:
    """Ask the game server for the account; on a refusal, or no answer, the form again."""
    data = form.cleaned_data
    password = data.get("password") or None
    try:
        made = await accounts.game().register(data["name"], data["callsign"] or None, password)
    except (UnavailableError, RefusedError) as e:
        await accounts.release(link)
        form.add_error(
            None,
            str(e)
            if isinstance(e, RefusedError)
            else "The game server is not answering just now, so the account could not be made. "
            "This link keeps working until it expires: try it again in a few minutes.",
        )
        return _confirm_page(request, form, link)
    name = made.get("name", data["name"])
    try:
        player = await sync_to_async(_registered)(link, name, made.get("created"))
    except IntegrityError:  # this address registered in another tab meanwhile
        log.error(
            "game account %s was made, but its address already has a web account: the "
            "operator may delete it (delete-account %s)",
            name,
            name,
        )
        return _private(render(request, "opencs_web/already.html"))
    log.info("game account %s registered from the web", name)
    await _log_in(request, player)
    return await _deliver(request, player, made["password"], chosen=password is not None)


def _confirm_page(request: HttpRequest, form: ConfirmForm, link: EmailLink) -> HttpResponse:
    return _private(render(request, "opencs_web/confirm.html", {"form": form, "email": link.email}))


def _registered(link: EmailLink, name: str, created: float | None) -> Player:
    """Note the new game account: the link is used, and a web account for an earlier account of
    this name (deleted on the server since) goes."""
    with transaction.atomic():
        EmailLink.objects.filter(pk=link.pk).update(used=timezone.now())
        stale = Player.objects.filter(game_name=name)
        if stale.exists():
            log.info("web account for an earlier game account %s removed: the name is new", name)
            stale.delete()
        return Player.objects.create(email=link.email, game_name=name, game_created=created)


async def _deliver(
    request: HttpRequest, player: Player, password: str, *, chosen: bool
) -> HttpResponse:
    """The page after a new password: a made-up one shown, emailed, or both
    (``OPENCS_WEB_PASSWORD_DELIVERY``). If the email cannot be sent it is shown after all, so it
    is never lost. A chosen password is never emailed, nor shown again."""
    mode = settings.OPENCS_PASSWORD_DELIVERY
    emailed = failed = False
    if not chosen and mode in ("email", "both"):
        await accounts.reserve("password", player.email, _source(request))
        emailed = await accounts.send(
            "password", {"name": player.game_name, "password": password}, player.email
        )
        failed = not emailed
    show = not chosen and (mode in ("show", "both") or failed)
    context = {
        "player": player,
        "password": password if show else None,
        "emailed": emailed,
        "failed": failed,
        "chosen": chosen,
    }
    return _private(render(request, "opencs_web/password.html", context))


def _bad_link(request: HttpRequest) -> HttpResponse:
    return _private(render(request, "opencs_web/bad_link.html", status=410))


# --- logging in and out ------------------------------------------------------------------------


@require_http_methods(["GET", "HEAD", "POST"])
async def login(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return _private(render(request, "opencs_web/login.html", {"form": LoginForm()}))
    form = LoginForm(request.POST)
    if form.caught:
        log.info("a log-in form with its hidden field filled in was ignored")
        return _sent(request, form.data.get("email", ""))
    if not form.is_valid():
        form.renew()
        return _private(render(request, "opencs_web/login.html", {"form": form}))
    email = form.cleaned_data["email"]
    if (refusal := await accounts.reserve("login", email, _source(request))) is not None:
        return _not_sent(request, refusal)
    player = await Player.objects.filter(email=email).afirst()
    if player is None:  # said by email only, as for a registration
        sent = await accounts.send(
            "no_account",
            {
                "register": accounts.link_url(request, reverse("register")),
                "registration_open": settings.OPENCS_REGISTRATION,
            },
            email,
        )
    else:
        link = await accounts.new_link(EmailLink.LOGIN, email)
        url = accounts.link_url(request, reverse("login-link", args=[accounts.make_token(link)]))
        sent = await accounts.send(
            "login",
            {
                "name": player.game_name,
                "url": url,
                "minutes": accounts.LOGIN_LINK_SECONDS // 60,
            },
            email,
        )
        if not sent:
            await link.adelete()
    if not sent:
        return _not_sent(request, Refusal("failed"))
    return _sent(request, email)


@require_http_methods(["GET", "HEAD", "POST"])
async def login_link(request: HttpRequest, token: str) -> HttpResponse:
    link = await accounts.read_token(EmailLink.LOGIN, token)
    if link is None:
        return _bad_link(request)
    if request.method != "POST":
        return _private(render(request, "opencs_web/login_link.html"))
    if not await accounts.use(link):
        return _bad_link(request)
    player = await Player.objects.filter(email=link.email).afirst()
    if player is None:  # deleted since the link was sent
        return _bad_link(request)
    await _log_in(request, player)
    return HttpResponseRedirect(reverse("account"))


@require_POST
async def logout(request: HttpRequest) -> HttpResponse:
    await request.session.aflush()
    return HttpResponseRedirect(reverse("home"))


# --- the account page --------------------------------------------------------------------------


@require_safe
async def account(request: HttpRequest) -> HttpResponse:
    player = await _player(request)
    if player is None:
        return HttpResponseRedirect(reverse("login"))
    found, gone = await _check(request, player)
    if gone:
        return _gone(request, player.game_name)
    notice = await request.session.apop(NOTICE, None)
    return _account_page(request, player, found, notice=notice)


def _account_page(
    request: HttpRequest,
    player: Player,
    found: dict[str, Any] | None,
    *,
    notice: str | None = None,
    callsign_form: CallsignForm | None = None,
    password_form: NewPasswordForm | None = None,
    delete_form: DeleteForm | None = None,
    status: int = 200,
) -> HttpResponse:
    chosen = settings.OPENCS_CHOSEN_PASSWORDS
    context = {
        "player": player,
        "found": found,
        "never_played": found is not None and found.get("last_login") is None,
        "notice": notice,
        "callsign_form": callsign_form or CallsignForm(),
        "password_form": password_form or NewPasswordForm(chosen=chosen),
        "delete_form": delete_form or DeleteForm(game_name=player.game_name),
        "delivery": settings.OPENCS_PASSWORD_DELIVERY,
        "chosen": chosen,
    }
    return _private(render(request, "opencs_web/account.html", context, status=status))


async def _acting_player(
    request: HttpRequest,
) -> tuple[Player, dict[str, Any] | None] | HttpResponse:
    """The web account a change is for, and its game account now: a page instead if there is
    no log-in, or the game account is gone."""
    player = await _player(request)
    if player is None:
        return HttpResponseRedirect(reverse("login"))
    found, gone = await _check(request, player)
    if gone:
        return _gone(request, player.game_name)
    return player, found


@require_POST
async def change_callsign(request: HttpRequest) -> HttpResponse:
    acting = await _acting_player(request)
    if isinstance(acting, HttpResponse):
        return acting
    player, found = acting
    form = CallsignForm(request.POST)
    if form.is_valid():
        try:
            done = await accounts.game().set_callsign(
                player.game_name, form.cleaned_data["callsign"]
            )
        except UnavailableError:
            form.add_error(None, OFFLINE)
        except RefusedError as e:
            form.add_error("callsign", str(e))
        else:
            await request.session.aset(NOTICE, f"Your callsign is now {done.get('callsign')}.")
            return HttpResponseRedirect(reverse("account"))
    return _account_page(request, player, found, callsign_form=form, status=400)


@sensitive_post_parameters("password")
@require_POST
async def new_password(request: HttpRequest) -> HttpResponse:
    acting = await _acting_player(request)
    if isinstance(acting, HttpResponse):
        return acting
    player, found = acting
    form = NewPasswordForm(request.POST, chosen=settings.OPENCS_CHOSEN_PASSWORDS)
    if form.is_valid():
        password = form.cleaned_data.get("password") or None
        try:
            made = await accounts.game().reset_password(player.game_name, password)
        except UnavailableError:
            form.add_error(None, OFFLINE)
        except RefusedError as e:
            form.add_error(None, str(e))
        else:
            log.info("password of %s reset from the web", player.game_name)
            return await _deliver(request, player, made["password"], chosen=password is not None)
    return _account_page(request, player, found, password_form=form, status=400)


@require_POST
async def delete(request: HttpRequest) -> HttpResponse:
    """Forget the address (the game account stays, and can no longer be reached from here), or
    delete the game account as well, through the server's ``delete-account``."""
    player = await _player(request)
    if player is None:
        return HttpResponseRedirect(reverse("login"))
    form = DeleteForm(request.POST, game_name=player.game_name)
    if not form.is_valid():
        found, gone = await _check(request, player)
        if gone:
            return _gone(request, player.game_name)
        return _account_page(request, player, found, delete_form=form, status=400)
    what = form.cleaned_data["what"]
    callsign = None
    if what == DeleteForm.GAME:
        acting = await _acting_player(request)
        if isinstance(acting, HttpResponse):
            return acting
        _, found = acting
        callsign = found.get("callsign") if found else None
        try:
            await accounts.game().delete_account(player.game_name)
        except UnavailableError:
            form.add_error(None, OFFLINE)
        except RefusedError as e:
            form.add_error(None, str(e))
        if form.errors:
            return _account_page(request, player, found, delete_form=form, status=400)
        # Recordings keep the callsign; taking it out is the operator's choice, on request.
        log.info(
            "game account %s deleted from the web; its callsign %s stays in recordings",
            player.game_name,
            callsign or "(none)",
        )
    else:
        log.info("web account for %s deleted; the game account stays", player.game_name)
    await player.adelete()
    await request.session.aflush()
    context = {"name": player.game_name, "game": what == DeleteForm.GAME}
    return _private(render(request, "opencs_web/deleted.html", context))


# --- the privacy note --------------------------------------------------------------------------


@require_safe
async def privacy(request: HttpRequest) -> HttpResponse:
    return render(request, "opencs_web/privacy.html", {"operator_text": _operator_text()})


def _operator_text() -> list[str]:
    """The operator's own words for the privacy page (``OPENCS_WEB_PRIVACY_FILE``), as
    paragraphs of plain text; none if there is no file or it cannot be read."""
    path = settings.OPENCS_PRIVACY_FILE
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("cannot read the privacy file %s: %s", path, e)
        return []
    return [" ".join(p.split()) for p in text.split("\n\n") if p.strip()]
