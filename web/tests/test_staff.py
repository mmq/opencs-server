"""The admin pages: their log-in (password and a second factor), roles, confirmations, re-entry
of a code, the audit log, and each command's request on the wire, to a stand-in for the service
channel on a real socket (:class:`FakeService`, run on a thread of its own)."""

from __future__ import annotations

import asyncio
import io
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.utils import timezone
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice
from pytest_django.fixtures import Settings as SettingsWrapper

from fakeservice import FakeService, socket_dir
from opencs.ratelimit import KeyedBuckets
from opencs_web import live, staff
from opencs_web.models import AdminAccount, AuditEntry, Player

pytestmark = pytest.mark.django_db

PASSWORD = "correct horse battery staple"
STATUS = {
    "lines": ["uptime: 3725 s", "city B BOSTON: 2 players [odd hit reports: 3 without a shot]"],
    "cities": [
        {
            "letter": "B",
            "name": "BOSTON",
            "players": 2,
            "failures": 1,
            "rebuilds": 1,
            "slow_calls": 4,
            "worst_call_ms": 87,
            "hits_without_a_shot": 3,
            "distant_hits": 2,
        }
    ],
}


class ThreadedService:
    """A :class:`FakeService` on an event loop of its own thread, so the pages (each request on
    a loop of its own under the test client) reach it over the socket as they would the server."""

    def __init__(self, fake: FakeService, loop: asyncio.AbstractEventLoop) -> None:
        self.fake = fake
        self._loop = loop

    def ops(self, *skip: str) -> list[dict[str, Any]]:
        """The requests so far, without their ids and without the lookups ``skip`` names."""
        return [
            {k: v for k, v in r.items() if k != "id"}
            for r in list(self.fake.requests)
            if r["op"] not in {"snapshot", "status", "account", *skip}
        ]

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self.fake.stop(), self._loop).result(5)


@pytest.fixture
def service(settings: SettingsWrapper) -> Iterator[ThreadedService]:
    settings.OPENCS_ADMIN = True
    # django-otp slows down a device after a wrong code (a second, then doubling); the tests
    # give right codes straight after wrong ones. Its own tests cover it.
    settings.OTP_TOTP_THROTTLE_FACTOR = 0
    settings.OTP_STATIC_THROTTLE_FACTOR = 0
    with socket_dir() as d:
        sock = d / "service.sock"
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        fake = asyncio.run_coroutine_threadsafe(FakeService(sock).start(), loop).result(5)
        fake.results = {"status": STATUS, "announce": {"lines": ["announced to 2"]}}
        settings.OPENCS_SERVICE_SOCKET = sock
        live.reset()
        staff.reset()
        threaded = ThreadedService(fake, loop)
        try:
            yield threaded
        finally:
            if fake._server is not None and fake._server.is_serving():
                threaded.stop()
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()
            live.reset()
            staff.reset()


@dataclass
class Made:
    account: AdminAccount
    totp: TOTPDevice
    codes: list[str]
    step: int = 0

    def code(self, ahead: int = 0) -> str:
        """The app's code now, or ``ahead`` steps on (a code is good once: the next log-in or
        confirmation in a test takes the next step, within django-otp's tolerance of one)."""
        totp = TOTP(self.totp.bin_key, self.totp.step, self.totp.t0, self.totp.digits)
        totp.time = time.time() + ahead * self.totp.step
        return f"{totp.token():06d}"

    def fresh(self) -> str:
        code = self.code(self.step)
        self.step += 1
        return code


def make_admin(name: str = "boss", role: str = AdminAccount.ADMIN) -> Made:
    user = get_user_model().objects.create(username=name)
    user.set_password(PASSWORD)
    user.save()
    account = AdminAccount.objects.create(user=user, role=role)
    # A tolerance of ten steps, so a test can log in several times with codes not used before.
    totp = TOTPDevice.objects.create(user=user, name="authenticator", confirmed=True, tolerance=10)
    static = StaticDevice.objects.create(user=user, name="recovery", confirmed=True)
    codes = [f"code{i:04d}" for i in range(3)]
    for code in codes:
        static.token_set.create(token=code)
    return Made(account, totp, codes)


def log_in(client: Client, made: Made, code: str | None = None, password: str = PASSWORD) -> Any:
    return client.post(
        "/admin/login",
        {
            "name": made.account.user.username,
            "password": password,
            "code": made.fresh() if code is None else code,
        },
    )


def page(response: Any) -> str:
    return str(response.content.decode())


def entries(**match: str) -> list[AuditEntry]:
    return list(AuditEntry.objects.filter(**match).order_by("id"))


# --- off, and not logged in ------------------------------------------------------------------


def test_the_admin_pages_are_not_there_when_turned_off(settings: SettingsWrapper) -> None:
    settings.OPENCS_ADMIN = False
    make_admin()
    client = Client()
    for path in ("/admin/", "/admin/login", "/admin/audit"):
        assert client.get(path).status_code == 404
    assert client.post("/admin/do/kick", {"callsign": "ACE"}).status_code == 404
    assert client.post("/admin/login", {"name": "boss", "password": PASSWORD}).status_code == 404


def test_without_a_log_in_every_page_sends_you_to_log_in(service: ThreadedService) -> None:
    client = Client()
    for response in (
        client.get("/admin/"),
        client.get("/admin/audit"),
        client.post("/admin/do/shutdown", {"seconds": "0", "confirmed": "yes"}),
        client.post("/admin/do/announce", {"text": "hello"}),
    ):
        assert response.status_code == 302
        assert response["Location"] == "/admin/login"
    assert service.ops() == []
    login = client.get("/admin/login")
    assert login.status_code == 200
    assert login["Cache-Control"] == "no-store"
    assert "noindex" in login["X-Robots-Tag"]


def test_a_player_logged_in_is_not_an_admin(service: ThreadedService) -> None:
    player = Player.objects.create(email="p@example.org", game_name="PILOT")
    client = Client()
    session = client.session
    session["player"] = player.pk
    session.save()
    assert client.get("/admin/").status_code == 302


# --- logging in --------------------------------------------------------------------------------


def test_the_password_alone_is_not_enough(service: ThreadedService) -> None:
    made = make_admin()
    client = Client()
    for code in ("", "000000", "nonsense"):
        response = log_in(client, made, code=code)
        assert response.status_code == 403
        assert "Not logged in" in page(response)
        assert PASSWORD not in page(response)
    assert client.get("/admin/").status_code == 302
    assert [e.detail for e in entries(action="login")] == ["wrong code"] * 3


def test_a_wrong_password_with_a_right_code_is_refused_alike(service: ThreadedService) -> None:
    made = make_admin()
    client = Client()
    response = log_in(client, made, password="wrong password!")
    assert response.status_code == 403
    assert client.get("/admin/").status_code == 302
    assert entries(action="login")[-1].detail == "wrong password"


def test_an_unknown_name_or_a_user_without_an_admin_account_cannot_log_in(
    service: ThreadedService,
) -> None:
    made = make_admin()
    plain = get_user_model().objects.create_superuser("root", password=PASSWORD)
    TOTPDevice.objects.create(user=plain, name="x", confirmed=True)
    client = Client()
    for name in ("nobody", "root"):
        response = client.post(
            "/admin/login", {"name": name, "password": PASSWORD, "code": made.code()}
        )
        assert response.status_code == 403
    assert [e.detail for e in entries(action="login")] == ["no such admin"] * 2


def test_a_log_in_with_the_code_shows_the_status_and_the_health_counters(
    service: ThreadedService,
) -> None:
    made = make_admin()
    client = Client()
    response = log_in(client, made)
    assert response.status_code == 302 and response["Location"] == "/admin/"
    home = client.get("/admin/")
    text = page(home)
    assert home.status_code == 200
    assert "Game server: online" in text and "ALPHA" in text  # the public status
    assert re.search(r"<td>3</td><td>2</td>\s*<td>4 \(87\)</td>\s*<td>1 \(1\)</td>", text)
    assert "odd hit reports: 3 without a shot" in text
    for name in ("kick", "announce", "reset-password", "delete-account", "shutdown", "reload"):
        assert f'action="/admin/do/{name}"' in text
    assert entries(action="login", result="ok")[0].who == "boss"
    assert TOTPDevice.objects.get(pk=made.totp.pk).last_t > 0


def test_a_code_works_once(service: ThreadedService) -> None:
    made = make_admin()
    code = made.code()
    assert log_in(Client(), made, code=code).status_code == 302
    assert log_in(Client(), made, code=code).status_code == 403


def test_a_recovery_code_works_once(service: ThreadedService) -> None:
    made = make_admin()
    client = Client()
    response = log_in(client, made, code=made.codes[0].upper())
    assert response.status_code == 302
    assert "recovery code" in page(client.get("/admin/")) and "2 left" in page(
        client.get("/admin/audit")
    )
    client.post("/admin/logout")
    assert client.get("/admin/").status_code == 302
    assert log_in(Client(), made, code=made.codes[0]).status_code == 403
    assert log_in(Client(), made, code=made.codes[1]).status_code == 302


def test_wrong_log_ins_lock_the_account(service: ThreadedService) -> None:
    made = make_admin()
    client = Client()
    for _ in range(staff.LOCK_AFTER):
        assert log_in(client, made, password="not the password").status_code == 403
    assert "locked" in entries(action="login")[-1].detail
    assert log_in(client, made).status_code == 403  # the right ones, locked all the same
    assert entries(action="login")[-1].detail == "locked"
    AdminAccount.objects.filter(pk=made.account.pk).update(
        locked_until=timezone.now() - timedelta(seconds=1)
    )
    assert log_in(client, made).status_code == 302


def test_tries_from_one_network_are_limited(service: ThreadedService) -> None:
    made = make_admin()
    staff._tries = KeyedBuckets(2, 1 / 60)
    client = Client()
    for _ in range(2):
        assert log_in(client, made, code="000000").status_code == 403
    AdminAccount.objects.filter(pk=made.account.pk).update(failures=0)
    assert log_in(client, made).status_code == 403
    assert entries(action="login")[-1].detail == "too many tries"


def test_the_log_in_ends_when_idle_too_old_or_changed(
    service: ThreadedService, monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    made = make_admin()
    start = time.time()
    clock = [start]
    monkeypatch.setattr(staff, "_now", lambda: clock[0])
    client = Client()
    log_in(client, made)
    clock[0] += staff.IDLE_SECONDS - 10
    assert client.get("/admin/").status_code == 200
    clock[0] += staff.IDLE_SECONDS + 1
    assert client.get("/admin/").status_code == 302

    log_in(client, made)
    for _ in range(settings.OPENCS_ADMIN_SESSION_MINUTES // 10):
        clock[0] += 600
        assert client.get("/admin/").status_code == 200
    clock[0] += 601
    assert client.get("/admin/").status_code == 302

    log_in(client, made)
    assert client.get("/admin/").status_code == 200
    call_command("resetadmin", "boss", "--unlock", stdout=io.StringIO())
    assert client.get("/admin/").status_code == 302


def test_logging_out_ends_the_admin_log_in(service: ThreadedService) -> None:
    made = make_admin()
    client = Client()
    log_in(client, made)
    assert client.post("/admin/logout").status_code == 302
    assert client.get("/admin/").status_code == 302
    assert entries(action="logout")[0].who == "boss"


# --- CSRF ------------------------------------------------------------------------------------


def test_csrf_is_enforced_on_the_log_in_and_the_commands(service: ThreadedService) -> None:
    made = make_admin()
    client = Client(enforce_csrf_checks=True)
    response = client.post(
        "/admin/login", {"name": "boss", "password": PASSWORD, "code": made.code()}
    )
    assert response.status_code == 403 and "CSRF" in page(response)
    client.get("/admin/login")
    token = client.cookies["csrftoken"].value
    response = client.post(
        "/admin/login",
        {"name": "boss", "password": PASSWORD, "code": made.fresh(), "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 302
    assert client.cookies["csrftoken"].value != token  # a new one at log-in
    response = client.post("/admin/do/announce", {"text": "hello"})
    assert response.status_code == 403 and "CSRF" in page(response)
    response = client.post(
        "/admin/do/shutdown",
        {"seconds": "0", "confirmed": "yes", "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 403
    assert service.ops() == []
    token = client.cookies["csrftoken"].value
    response = client.post("/admin/do/announce", {"text": "hello", "csrfmiddlewaretoken": token})
    assert response.status_code == 200
    assert service.ops() == [{"op": "announce", "text": "hello"}]


# --- the commands ------------------------------------------------------------------------------

#: Each command: its form, the request it makes, and the reply it gets.
COMMANDS = [
    (
        "kick",
        {"callsign": "Ace", "reason": "spamming"},
        {"op": "kick", "callsign": "Ace", "reason": "spamming"},
        {"lines": ["kicked Ace (1 connection(s))"]},
    ),
    ("announce", {"text": "back soon"}, {"op": "announce", "text": "back soon"}, None),
    ("guppy-home", {"team": "GREEN"}, {"op": "guppy-home", "team": "GREEN"}, None),
    (
        "reset-progress",
        {"name": "pilot"},
        {"op": "reset-progress", "name": "PILOT"},
        {"lines": ["progress of PILOT erased"]},
    ),
    (
        "delete-account",
        {"name": "pilot"},
        {"op": "delete-account", "name": "PILOT"},
        {"lines": ["deleted PILOT"]},
    ),
    (
        "shutdown",
        {"seconds": "120"},
        {"op": "shutdown", "seconds": 120},
        {"lines": ["shutting down in 120 s"]},
    ),
    ("cancel-shutdown", {}, {"op": "shutdown", "cancel": True}, {"lines": ["shutdown canceled"]}),
    ("reload", {}, {"op": "reload"}, {"lines": ["city B BOSTON: unchanged"]}),
]
DESTRUCTIVE = {"kick", "reset-progress", "delete-account", "shutdown", "reload", "reset-password"}


@pytest.mark.parametrize(("action", "form", "request_", "reply"), COMMANDS)
def test_each_command_asks_the_game_server_and_is_audited(
    service: ThreadedService,
    action: str,
    form: dict[str, str],
    request_: dict[str, Any],
    reply: dict[str, Any] | None,
) -> None:
    reply = reply or {"lines": ["done it"]}
    service.fake.results[request_["op"]] = reply
    service.fake.results["account"] = {"name": "PILOT", "exists": True, "online": False}
    made = make_admin()
    client = Client()
    log_in(client, made)
    response = client.post(f"/admin/do/{action}", form)
    if action in DESTRUCTIVE:
        assert response.status_code == 200
        assert 'name="confirmed" value="yes"' in page(response)
        assert service.ops() == []  # nothing done yet
        assert not entries(action=action)
        response = client.post(f"/admin/do/{action}", {**form, "confirmed": "yes"})
    assert response.status_code == 200, page(response)
    assert reply["lines"][0] in page(response)
    assert service.ops() == [request_]
    [entry] = entries(action=action)
    assert (entry.who, entry.role, entry.result) == ("boss", "admin", "ok")
    assert entry.source == "127.0.0.1"
    assert reply["lines"][0] in entry.detail


def test_the_confirmation_page_shows_the_account_as_the_server_has_it(
    service: ThreadedService,
) -> None:
    service.fake.results["account"] = {
        "name": "PILOT",
        "exists": True,
        "online": True,
        "callsign": "<Ace>",
    }
    made = make_admin()
    client = Client()
    log_in(client, made)
    text = page(client.post("/admin/do/delete-account", {"name": "pilot"}))
    assert "&lt;Ace&gt;" in text and "<Ace>" not in text
    assert re.search(r"<dt>In the game</dt><dd>yes</dd>", text)


def test_a_refusal_from_the_server_is_shown_and_audited(service: ThreadedService) -> None:
    service.fake.results["delete-account"] = {"lines": ["refused: PILOT is online; kick it first"]}
    Player.objects.create(email="p@example.org", game_name="PILOT")
    made = make_admin()
    client = Client()
    log_in(client, made)
    response = client.post("/admin/do/delete-account", {"name": "PILOT", "confirmed": "yes"})
    assert response.status_code == 409
    assert "refused" in page(response) and "kick it first" in page(response)
    assert entries(action="delete-account")[0].result == "refused"
    assert Player.objects.filter(game_name="PILOT").exists()


@pytest.mark.parametrize(
    ("line", "result"),
    [
        ("no city changed", "ok"),
        ("city D NEWCITY: opened", "ok"),
        ("reload refused, nothing changed: bad file", "refused"),
        ("no config file is known: nothing to reload", "refused"),
        ("no pilot ACE is connected", "refused"),
        ("no RED guppy exists in any city", "refused"),
        ("refused: no account PILOT", "refused"),
        ("at most 3600 seconds", "refused"),
    ],
)
def test_the_servers_reply_lines_tell_done_from_refused(line: str, result: str) -> None:
    assert staff.outcome({"lines": [line, "more"]}) == (result, [line, "more"])


def test_deleting_an_account_removes_its_web_account(service: ThreadedService) -> None:
    service.fake.results["delete-account"] = {"lines": ["deleted PILOT"]}
    Player.objects.create(email="p@example.org", game_name="PILOT")
    made = make_admin()
    client = Client()
    log_in(client, made)
    response = client.post("/admin/do/delete-account", {"name": "pilot", "confirmed": "yes"})
    assert "web account here was removed" in page(response)
    assert not Player.objects.exists()


def test_a_password_reset_shows_it_once_and_may_kick_the_session(
    service: ThreadedService,
) -> None:
    service.fake.results.update(
        {
            "reset-password": {"name": "PILOT", "password": "K7QW2MZP"},
            "account": {"name": "PILOT", "exists": True, "online": True, "callsign": "Ace"},
            "kick": {"lines": ["kicked Ace (1 connection(s))"]},
        }
    )
    made = make_admin()
    client = Client()
    log_in(client, made)
    confirm = page(client.post("/admin/do/reset-password", {"name": "pilot", "kick": "on"}))
    assert 'name="kick" value="on"' in confirm
    response = client.post(
        "/admin/do/reset-password", {"name": "PILOT", "kick": "on", "confirmed": "yes"}
    )
    text = page(response)
    assert "K7QW2MZP" in text and "kicked Ace" in text
    assert response["Cache-Control"] == "no-store"
    assert service.ops() == [
        {"op": "reset-password", "name": "PILOT"},
        {"op": "kick", "callsign": "Ace", "reason": "password reset"},
    ]
    assert [(e.action, e.target, e.result) for e in entries()[-2:]] == [
        ("kick", "Ace", "ok"),
        ("reset-password", "PILOT", "ok"),
    ]
    assert not AuditEntry.objects.filter(detail__contains="K7QW2MZP").exists()


def test_a_destructive_command_wants_a_fresh_code_after_a_while(
    service: ThreadedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.fake.results["shutdown"] = {"lines": ["shutting down in 60 s"]}
    made = make_admin()
    clock = [time.time()]
    monkeypatch.setattr(staff, "_now", lambda: clock[0])
    client = Client()
    log_in(client, made)
    clock[0] += staff.REAUTH_SECONDS + 1
    confirm = page(client.post("/admin/do/shutdown", {"seconds": "60"}))
    assert 'name="code"' in confirm
    for code in ("", "123456"):
        response = client.post(
            "/admin/do/shutdown", {"seconds": "60", "confirmed": "yes", "code": code}
        )
        assert response.status_code == 403
        assert "not right" in page(response)
    assert service.ops() == []
    assert [e.result for e in entries(action="shutdown")] == ["denied", "denied"]
    response = client.post(
        "/admin/do/shutdown", {"seconds": "60", "confirmed": "yes", "code": made.fresh()}
    )
    assert response.status_code == 200
    assert service.ops() == [{"op": "shutdown", "seconds": 60}]
    # Verified just now: the next one goes without.
    assert 'name="code"' not in page(client.post("/admin/do/shutdown", {"seconds": "60"}))


def test_a_form_that_does_not_hold_is_shown_again(service: ThreadedService) -> None:
    made = make_admin()
    client = Client()
    log_in(client, made)
    for action, form in (
        ("shutdown", {"seconds": "-1"}),
        ("shutdown", {"seconds": "99999"}),
        ("guppy-home", {"team": "PURPLE"}),
        ("kick", {"callsign": "two words"}),
        ("announce", {"text": "café"}),
    ):
        assert client.post(f"/admin/do/{action}", form).status_code == 400
    assert client.post("/admin/do/format-disk", {}).status_code == 404
    assert client.get("/admin/do/announce").status_code == 405
    assert service.ops() == []


def test_with_the_game_server_offline_commands_fail_cleanly(service: ThreadedService) -> None:
    made = make_admin()
    client = Client()
    log_in(client, made)
    service.stop()
    live.reset()
    home = client.get("/admin/")
    assert home.status_code == 200
    assert "Game server: offline" in page(home) and "not answering" in page(home)
    response = client.post("/admin/do/announce", {"text": "anyone?"})
    assert response.status_code == 503
    assert "not answering" in page(response)
    response = client.post("/admin/do/delete-account", {"name": "PILOT", "confirmed": "yes"})
    assert response.status_code == 503
    assert [e.result for e in entries()[-2:]] == ["offline", "offline"]


# --- roles -----------------------------------------------------------------------------------


def test_a_moderator_kicks_and_announces_and_nothing_else(service: ThreadedService) -> None:
    service.fake.results["kick"] = {"lines": ["kicked Ace (1 connection(s))"]}
    made = make_admin("mod", AdminAccount.MODERATOR)
    client = Client()
    log_in(client, made)
    home = page(client.get("/admin/"))
    assert 'action="/admin/do/kick"' in home and 'action="/admin/do/announce"' in home
    assert "/admin/do/delete-account" not in home and "Audit log" not in home
    for action, form in (
        ("delete-account", {"name": "PILOT", "confirmed": "yes"}),
        ("reset-password", {"name": "PILOT", "confirmed": "yes"}),
        ("reset-progress", {"name": "PILOT", "confirmed": "yes"}),
        ("shutdown", {"seconds": "0", "confirmed": "yes"}),
        ("cancel-shutdown", {}),
        ("reload", {"confirmed": "yes"}),
        ("guppy-home", {"team": "RED"}),
    ):
        assert client.post(f"/admin/do/{action}", form).status_code == 403
    assert client.get("/admin/audit").status_code == 403
    assert service.ops() == []
    assert all(e.result == "denied" and e.role == "moderator" for e in entries(who="mod")[1:])
    assert client.post("/admin/do/announce", {"text": "hi"}).status_code == 200
    response = client.post("/admin/do/kick", {"callsign": "Ace", "confirmed": "yes"})
    assert response.status_code == 200
    assert [r["op"] for r in service.ops()] == ["announce", "kick"]


# --- the audit log -----------------------------------------------------------------------------


def test_the_audit_log_is_shown_to_admins_and_only_added_to(service: ThreadedService) -> None:
    made = make_admin()
    client = Client()
    log_in(client, made, code="999999")
    log_in(client, made)
    client.post("/admin/do/announce", {"text": "<b>hello</b>"})
    text = page(client.get("/admin/audit"))
    assert "wrong code" in text and "announce" in text
    assert "&lt;b&gt;hello&lt;/b&gt;" in text and "<b>hello" not in text
    entry = AuditEntry.objects.first()
    assert entry is not None
    entry.detail = "changed"
    with pytest.raises(TypeError):
        entry.save()
    with pytest.raises(TypeError):
        entry.delete()
    with pytest.raises(TypeError):
        AuditEntry.objects.all().delete()
    with pytest.raises(TypeError):
        AuditEntry.objects.filter(pk=entry.pk).update(detail="changed")


# --- the commands that make admins ---------------------------------------------------------------


def _create(name: str, password: str, *args: str) -> str:
    out = io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(password + "\n")):
        call_command("createadmin", name, "--password-stdin", *args, stdout=out)
    return out.getvalue()


def test_createadmin_shows_the_second_factor_once_and_it_logs_in(
    service: ThreadedService,
) -> None:
    out = _create("ops", PASSWORD, "--role", "moderator")
    uri = re.search(r"otpauth://totp/\S+", out)
    assert uri and "issuer=OpenCS" in uri.group()
    codes = re.findall(r"^  ([a-z2-7]{8})$", out, re.MULTILINE)
    assert len(codes) == 10
    account = AdminAccount.objects.get(user__username="ops")
    assert account.role == "moderator"
    assert account.user.password.startswith("argon2$argon2id$")
    made = Made(account, TOTPDevice.objects.get(user=account.user), codes)
    client = Client()
    assert log_in(client, made).status_code == 302
    assert client.get("/admin/").status_code == 200
    assert entries(action="createadmin")[0].target == "ops"
    with pytest.raises(CommandError, match="exists"):
        _create("ops", PASSWORD)


@pytest.mark.parametrize("password", ["short", "123456789012345", "password1234"])
def test_createadmin_wants_a_strong_password(service: ThreadedService, password: str) -> None:
    with pytest.raises(CommandError):
        _create("ops", password)
    assert not AdminAccount.objects.exists()


def test_resetadmin_changes_the_second_factor_and_ends_log_ins(service: ThreadedService) -> None:
    made = make_admin()
    client = Client()
    log_in(client, made)
    out = io.StringIO()
    call_command("resetadmin", "boss", "--totp", "--codes", stdout=out)
    assert "otpauth://" in out.getvalue()
    assert client.get("/admin/").status_code == 302  # the log-in has ended
    assert log_in(Client(), made).status_code == 403  # the old secret no longer works
    assert log_in(Client(), made, code=made.codes[2]).status_code == 403
    new = Made(made.account, TOTPDevice.objects.get(user=made.account.user), [])
    assert log_in(Client(), new).status_code == 302
    call_command("resetadmin", "boss", "--remove", stdout=io.StringIO())
    assert not AdminAccount.objects.exists()
    assert [e.action for e in entries(who="(command line)")] == ["resetadmin", "removeadmin"]
    with pytest.raises(CommandError, match="no admin"):
        call_command("resetadmin", "boss", "--unlock")
