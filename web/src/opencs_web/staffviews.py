"""The admin pages (``staff`` has the log-in, roles and audit log): the status with what only
admins see, the operator's commands over the service channel, and the audit log.

Every action is a POST with Django's CSRF token. A destructive one (``Action.destructive``) is
first shown on a page that says what it will do, and done only when that page's button is
pressed; that page asks for a fresh code when the last one was given over
``staff.REAUTH_SECONDS`` ago. Every action, refusal and failed log-in goes in the audit log. With
``OPENCS_WEB_ADMIN`` off, every page here is a 404.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from asgiref.sync import sync_to_async
from django import forms
from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from opencs_web import live, staff
from opencs_web.channel import RefusedError, UnavailableError
from opencs_web.models import AdminAccount, AuditEntry, Player
from opencs_web.staffforms import (
    AccountNameForm,
    AdminLoginForm,
    AnnounceForm,
    CodeForm,
    GuppyForm,
    KickForm,
    NoArguments,
    ResetPasswordForm,
    ShutdownForm,
)

log = logging.getLogger("opencs_web.staff")

#: A line for the next page after a redirect.
NOTICE = "opencs_admin_notice"
#: Entries the audit page shows, newest first.
AUDIT_PAGE = 200

OFFLINE = "The game server is not answering, so nothing was done. Try again when it is back."


@dataclass(frozen=True)
class Action:
    label: str
    form: type[forms.Form]
    #: Shown on a page of its own, and done only when that page's button is pressed.
    destructive: bool
    #: What it does, on that page.
    warning: str = ""


ACTIONS: dict[str, Action] = {
    "kick": Action(
        "Kick a pilot",
        KickForm,
        destructive=True,
        warning="The pilot is taken out of the game (saved, back to the Chamber), or off the "
        "server from the Chamber, and told the reason.",
    ),
    "announce": Action("Announce", AnnounceForm, destructive=False),
    "guppy-home": Action("Send a team's guppy home", GuppyForm, destructive=False),
    "reset-password": Action(
        "Reset a game password",
        ResetPasswordForm,
        destructive=True,
        warning="The account's password is replaced by a made-up one, shown once on the next "
        "page. The old one stops working, but a session already logged on goes on unless it is "
        "kicked too.",
    ),
    "reset-progress": Action(
        "Reset progress",
        AccountNameForm,
        destructive=True,
        warning="The account's saved progress (rank, scores, loadout) is erased for good. It must "
        "be offline.",
    ),
    "delete-account": Action(
        "Delete a game account",
        AccountNameForm,
        destructive=True,
        warning="The account, its callsign, password and progress are removed from the game "
        "server for good, and its web account here. It must be offline. Recordings keep the "
        "callsign (opencs-web forget_callsign takes it out).",
    ),
    "shutdown": Action(
        "Shut down the game server",
        ShutdownForm,
        destructive=True,
        warning="Every player is told now; when the time is up the server saves everyone and "
        "stops. Unless something restarts it (systemd does), the game is down until then.",
    ),
    "cancel-shutdown": Action("Cancel a shutdown", NoArguments, destructive=False),
    "reload": Action(
        "Reload the city files",
        NoArguments,
        destructive=True,
        warning="The server reads its city files again: new and changed cities open, removed ones "
        "close when empty. A running game is never changed.",
    ),
}


def _private(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


type _View = Callable[..., Coroutine[Any, Any, HttpResponse]]


def _enabled(view: _View) -> _View:
    """A page that exists only while ``OPENCS_WEB_ADMIN`` is on."""

    @functools.wraps(view)
    async def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not settings.OPENCS_ADMIN:
            raise Http404
        return _private(await view(request, *args, **kwargs))

    return wrapped


def _page(
    request: HttpRequest,
    template: str,
    context: dict[str, Any],
    admin: staff.Admin | None = None,
    status: int = 200,
) -> HttpResponse:
    context = {
        **context,
        "admin": admin,
        "is_admin": admin is not None and admin.role == AdminAccount.ADMIN,
    }
    return render(request, f"opencs_web/staff/{template}.html", context, status=status)


# --- logging in and out --------------------------------------------------------------------------


@_enabled
@sensitive_post_parameters("password", "code")
@require_http_methods(["GET", "HEAD", "POST"])
async def login(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        if await staff.current(request) is not None:
            return HttpResponseRedirect(reverse("staff:home"))
        return _page(request, "login", {"form": AdminLoginForm()})
    form = AdminLoginForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        result = await sync_to_async(staff.check_login)(
            data["name"], data["password"], data["code"], staff.source(request)
        )
        if result.account is not None:
            await staff.log_in(request, result.account)
            log.info("admin %s logged in", data["name"])
            if result.codes_left is not None:
                await request.session.aset(
                    NOTICE,
                    f"You logged in with a recovery code, which is now used up: "
                    f"{result.codes_left} left. Make new ones with "
                    f"opencs-web resetadmin --codes.",
                )
            return HttpResponseRedirect(reverse("staff:home"))
        form.add_error(
            None,
            "Not logged in: the name, password or code is wrong, or there have been too many "
            "tries. Wait a while before trying again.",
        )
    form.data = {**dict(form.data.items()), "password": "", "code": ""}
    return _page(request, "login", {"form": form}, status=403)


@_enabled
@require_POST
async def logout(request: HttpRequest) -> HttpResponse:
    admin = await staff.current(request)
    if admin is not None:
        await staff.audit(admin, "logout", AuditEntry.OK, request=request)
    await staff.log_out(request)
    return HttpResponseRedirect(reverse("staff:login"))


async def _admin(request: HttpRequest) -> staff.Admin | HttpResponse:
    admin = await staff.current(request)
    if admin is None:
        return HttpResponseRedirect(reverse("staff:login"))
    return admin


# --- the status ----------------------------------------------------------------------------------


@_enabled
@require_safe
async def home(request: HttpRequest) -> HttpResponse:
    """The public status, kept live by the public page's stream, and what the ``status`` command
    adds, as loaded: each city's failures, slow calls and odd hit reports, and the held games."""
    admin = await _admin(request)
    if isinstance(admin, HttpResponse):
        return admin
    status = await live.status_cache().get()
    try:
        details = await staff.request("status")
    except (UnavailableError, RefusedError):
        details = None
    actions = [
        (name, action, action.form())
        for name, action in ACTIONS.items()
        if staff.may(admin.role, name)
    ]
    context = {
        "status": status,
        "details": details if isinstance(details, dict) else None,
        "actions": actions,
        "notice": await request.session.apop(NOTICE, None),
    }
    return _page(request, "home", context, admin)


@_enabled
@require_safe
async def audit_log(request: HttpRequest) -> HttpResponse:
    admin = await _admin(request)
    if isinstance(admin, HttpResponse):
        return admin
    if admin.role != AdminAccount.ADMIN:
        await staff.audit(admin, "view-audit", AuditEntry.DENIED, request=request)
        return _page(request, "denied", {}, admin, status=403)
    entries = [e async for e in AuditEntry.objects.all()[:AUDIT_PAGE]]
    return _page(request, "audit", {"entries": entries}, admin)


# --- actions -------------------------------------------------------------------------------------


@_enabled
@require_POST
async def act(request: HttpRequest, action: str) -> HttpResponse:
    admin = await _admin(request)
    if isinstance(admin, HttpResponse):
        return admin
    spec = ACTIONS.get(action)
    if spec is None:
        raise Http404
    if not staff.may(admin.role, action):
        await staff.audit(admin, action, AuditEntry.DENIED, request=request, detail="role")
        return _page(request, "denied", {}, admin, status=403)
    form = spec.form(request.POST)
    if not form.is_valid():
        return _page(request, "invalid", {"action": spec, "form": form}, admin, status=400)
    data = form.cleaned_data
    target = _target(data)
    if spec.destructive:
        code_form = CodeForm(request.POST)
        if request.POST.get("confirmed") != "yes":
            return await _confirm(request, admin, action, spec, form)
        if not admin.recently_verified:
            code = code_form.cleaned_data["code"] if code_form.is_valid() else ""
            if not await sync_to_async(staff.check_code)(admin.account, code):
                await staff.audit(
                    admin,
                    action,
                    AuditEntry.DENIED,
                    request=request,
                    target=target,
                    detail="wrong or no code",
                )
                code_form.add_error("code", "That code is not right: enter a current one.")
                return await _confirm(request, admin, action, spec, form, code_form, status=403)
            await staff.verified_now(request)
    return await _do(request, admin, action, spec, data, target)


def _target(data: dict[str, Any]) -> str:
    for key in ("name", "callsign", "team", "seconds", "text"):
        if key in data:
            return str(data[key])
    return ""


async def _confirm(
    request: HttpRequest,
    admin: staff.Admin,
    action: str,
    spec: Action,
    form: forms.Form,
    code_form: CodeForm | None = None,
    status: int = 200,
) -> HttpResponse:
    """The page that says what a destructive action will do, with the button that does it; for
    an account, what the server knows of it now."""
    found = None
    if "name" in form.cleaned_data:
        try:
            found = await staff.request("account", name=form.cleaned_data["name"])
        except (UnavailableError, RefusedError):
            found = None
    context = {
        "name": action,
        "action": spec,
        "form": form,
        "values": [(form[f].label, _shown(form.cleaned_data[f])) for f in form.cleaned_data],
        "hidden": [(f, _hidden_value(form.cleaned_data[f])) for f in form.cleaned_data],
        "found": found,
        "code_form": None if admin.recently_verified else (code_form or CodeForm()),
    }
    return _page(request, "confirm", context, admin, status=status)


def _shown(value: object) -> object:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


def _hidden_value(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else ""
    return str(value)


async def _do(
    request: HttpRequest,
    admin: staff.Admin,
    action: str,
    spec: Action,
    data: dict[str, Any],
    target: str,
) -> HttpResponse:
    """Ask the game server, note it in the audit log, and show what it said."""
    password = None
    extra: list[str] = []
    try:
        result = await _request(action, data)
        if action == "reset-password":
            password = result.get("password") if isinstance(result, dict) else None
            outcome, lines = AuditEntry.OK, [f"new password for {data['name']}"]
            if data.get("kick"):
                extra = await _kick_after_reset(request, admin, data["name"])
        else:
            outcome, lines = staff.outcome(result)
            if action == "delete-account" and outcome == AuditEntry.OK:
                removed, _ = await Player.objects.filter(game_name=data["name"]).adelete()
                if removed:
                    extra.append("its web account here was removed too")
    except UnavailableError:
        outcome, lines = AuditEntry.OFFLINE, [OFFLINE]
    except RefusedError as e:
        outcome, lines = AuditEntry.REFUSED, [str(e)]
    await staff.audit(
        admin, action, outcome, request=request, target=target, detail="\n".join(lines + extra)
    )
    log.info("admin %s: %s %s: %s", admin.name, action, target, outcome)
    context = {
        "action": spec,
        "outcome": outcome,
        "lines": lines + extra,
        "password": password,
        "account_name": data.get("name"),
    }
    status = {AuditEntry.OK: 200, AuditEntry.OFFLINE: 503}.get(outcome, 409)
    return _page(request, "result", context, admin, status=status)


#: Each action's request to the service channel, from its form's data.
_REQUESTS: dict[str, Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]] = {
    "kick": lambda d: ("kick", {"callsign": d["callsign"], "reason": d["reason"]}),
    "announce": lambda d: ("announce", {"text": d["text"]}),
    "guppy-home": lambda d: ("guppy-home", {"team": d["team"]}),
    "reset-password": lambda d: ("reset-password", {"name": d["name"]}),
    "reset-progress": lambda d: ("reset-progress", {"name": d["name"]}),
    "delete-account": lambda d: ("delete-account", {"name": d["name"]}),
    "shutdown": lambda d: ("shutdown", {"seconds": d["seconds"]}),
    "cancel-shutdown": lambda d: ("shutdown", {"cancel": True}),
    "reload": lambda d: ("reload", {}),
}


async def _request(action: str, data: dict[str, Any]) -> Any:
    op, args = _REQUESTS[action](data)
    return await staff.request(op, **args)


async def _kick_after_reset(request: HttpRequest, admin: staff.Admin, name: str) -> list[str]:
    """Kick the account whose password was just reset, if it is in the game."""
    try:
        found = await staff.request("account", name=name)
        callsign = found.get("callsign") if isinstance(found, dict) else None
        if not (isinstance(found, dict) and found.get("online") and callsign):
            return [f"{name} was not in the game: nothing to kick"]
        result = await staff.request("kick", callsign=callsign, reason="password reset")
    except UnavailableError:
        outcome, lines = AuditEntry.OFFLINE, [OFFLINE]
        callsign = name
    except RefusedError as e:
        outcome, lines = AuditEntry.REFUSED, [str(e)]
        callsign = name
    else:
        outcome, lines = staff.outcome(result)
    await staff.audit(
        admin, "kick", outcome, request=request, target=str(callsign), detail="\n".join(lines)
    )
    return lines
