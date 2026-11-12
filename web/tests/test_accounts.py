"""Registration and web accounts, through the pages, with Django's in-memory email and a stand-in
for the game server's account operations (:class:`FakeGame`) that keeps the server's words."""

from __future__ import annotations

import asyncio
import re
import smtplib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.core import mail
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import EmailMessage
from django.test import Client
from pytest_django.fixtures import Settings as SettingsWrapper

from fakeservice import FakeService, socket_dir
from opencs_web import accounts, live
from opencs_web.accounts import Game
from opencs_web.channel import RefusedError, UnavailableError
from opencs_web.models import EmailLink, EmailSent, Player

pytestmark = pytest.mark.django_db

RESERVED = {"ADMIN", "SERVER", "SYSOP"}


class FakeGame(Game):
    """The game server's accounts, as the service channel shows them."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}
        self.offline = False
        self.calls: list[tuple[str, ...]] = []
        self.made = 0
        self._clock = 1_800_000_000.0

    def _up(self) -> None:
        if self.offline:
            raise UnavailableError("no service socket")

    def _now(self) -> float:
        self._clock += 1.25
        return self._clock

    def add(self, name: str, callsign: str = "") -> dict[str, Any]:
        self.accounts[name] = {
            "callsign": callsign,
            "created": self._now(),
            "last_login": None,
            "online": False,
            "password": "OLD",
        }
        return self.accounts[name]

    async def account(self, name: str) -> dict[str, Any]:
        self._up()
        self.calls.append(("account", name))
        found = self.accounts.get(name)
        if found is None:
            return {
                "name": name,
                "exists": False,
                "online": False,
                "callsign": None,
                "created": None,
                "last_login": None,
            }
        return {
            "name": name,
            "exists": True,
            "online": found["online"],
            "callsign": found["callsign"],
            "created": found["created"],
            "last_login": found["last_login"],
        }

    async def register(
        self, name: str, callsign: str | None, password: str | None
    ) -> dict[str, Any]:
        self._up()
        self.calls.append(("register", name, callsign or "", password or ""))
        if name in RESERVED:
            raise RefusedError("refused", "THAT NAME IS RESERVED")
        if name in self.accounts:
            raise RefusedError("refused", "THAT NAME IS TAKEN")
        if callsign and any(
            a["callsign"].upper() == callsign.upper() for a in self.accounts.values()
        ):
            raise RefusedError("refused", "THAT CALLSIGN IS TAKEN")
        if password is not None and password != password.upper():
            raise RefusedError(
                "refused", "A PASSWORD MAY HAVE NO LOWER CASE: THE GAME TYPES IT IN CAPITALS"
            )
        self.made += 1
        made = self.add(name, callsign or "")
        made["password"] = password or f"MADEUP{self.made:04d}"
        return {
            "name": name,
            "password": made["password"],
            "callsign": callsign,
            "created": made["created"],
        }

    async def reset_password(self, name: str, password: str | None) -> dict[str, Any]:
        self._up()
        self.calls.append(("reset-password", name, password or ""))
        if name not in self.accounts:
            raise RefusedError("refused", "NO SUCH ACCOUNT")
        self.made += 1
        self.accounts[name]["password"] = password or f"NEWONE{self.made:04d}"
        return {"name": name, "password": self.accounts[name]["password"]}

    async def set_callsign(self, name: str, callsign: str) -> dict[str, Any]:
        self._up()
        self.calls.append(("set-callsign", name, callsign))
        found = self.accounts[name]
        if found.get("changed"):
            raise RefusedError("refused", "YOU MAY CHOOSE A NEW CALLSIGN IN 7 DAYS")
        found["callsign"] = callsign
        found["changed"] = True
        return {"name": name, "callsign": callsign}

    async def delete_account(self, name: str) -> None:
        self._up()
        self.calls.append(("delete-account", name))
        if self.accounts[name]["online"]:
            raise RefusedError("refused", "That account is in the game now: log off first.")
        del self.accounts[name]


@pytest.fixture
def game(settings: SettingsWrapper) -> Iterator[FakeGame]:
    settings.OPENCS_FORM_MIN_SECONDS = 0
    live.reset()
    accounts.reset()
    fake = FakeGame()
    accounts._game = fake
    yield fake
    accounts.reset()
    live.reset()


def register(
    client: Client,
    name: str,
    email: str,
    callsign: str = "",
    address: str = "192.0.2.1",
    **extra: str,
) -> Any:
    data = {
        "name": name,
        "callsign": callsign,
        "email": email,
        "website": "",
        "stamp": accounts.form_stamp(),
        **extra,
    }
    return client.post("/register", data, REMOTE_ADDR=address)


def log_in(client: Client, email: str, address: str = "192.0.2.1") -> Any:
    data = {"email": email, "website": "", "stamp": accounts.form_stamp()}
    return client.post("/login", data, REMOTE_ADDR=address)


def link_in(message: EmailMessage) -> str:
    found = re.search(r"http://testserver(/\S+)", str(message.body))
    assert found, message.body
    return found.group(1)


def last_link() -> str:
    return link_in(mail.outbox[-1])


def page(response: Any) -> str:
    return str(response.content.decode())


def registered(
    client: Client,
    game: FakeGame,
    name: str = "PILOT",
    email: str = "p@example.org",
    **confirm: str,
) -> Any:
    """A whole registration: the form, the email, the link, the button."""
    assert register(client, name, email, confirm.pop("callsign", "")).status_code == 200
    url = last_link()
    assert client.get(url).status_code == 200
    return client.post(url, {"name": name, "callsign": "", **confirm})


# --- the whole flow ----------------------------------------------------------------------------


def test_a_registration_sends_a_link_that_makes_the_account_and_shows_the_password_once(
    game: FakeGame,
) -> None:
    client = Client()
    response = register(client, "pilot", "Pilot@Example.org", callsign="Ace-1")
    assert response.status_code == 200
    assert "Check your email" in page(response)
    assert "pilot@example.org" in page(response)
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["pilot@example.org"]
    assert message.subject == "Confirm your OpenCS account"
    assert "PILOT" in str(message.body)
    assert "MADEUP" not in str(message.body)
    assert not game.accounts  # nothing is made before the link is followed

    url = link_in(message)
    shown = client.get(url)  # a mail scanner fetching the link makes nothing
    assert shown.status_code == 200 and not game.accounts
    assert 'value="PILOT"' in page(shown) and 'value="Ace-1"' in page(shown)
    assert "password" not in page(shown).lower().split("<form")[1]  # chosen ones are off

    made = client.post(url, {"name": "PILOT", "callsign": "Ace-1"})
    assert made.status_code == 200
    assert made["Cache-Control"] == "no-store"
    assert "MADEUP0001" in page(made)
    assert "only time it is shown" in page(made)
    assert game.accounts["PILOT"]["callsign"] == "Ace-1"
    assert len(mail.outbox) == 1  # shown, not emailed (the default)
    player = Player.objects.get()
    assert (player.email, player.game_name) == ("pilot@example.org", "PILOT")
    assert player.game_created == game.accounts["PILOT"]["created"]

    account = client.get("/account")  # logged in by the link
    assert account.status_code == 200
    assert "PILOT" in page(account) and "Ace-1" in page(account)
    assert "MADEUP0001" not in page(account)

    again = client.post(url, {"name": "PILOT", "callsign": "Ace-1"})  # once only
    assert again.status_code == 410
    assert client.get(url).status_code == 410


def test_an_expired_or_forged_link_does_nothing(
    game: FakeGame, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client()
    register(client, "PILOT", "p@example.org")
    url = last_link()
    assert client.get(url[:-2] + "xx").status_code == 410
    assert client.get("/register/1.abc:def").status_code == 410
    login_token = url.removeprefix("/register/")
    assert client.get(f"/login/{login_token}").status_code == 410  # a link of the other kind
    monkeypatch.setattr(accounts, "REGISTER_LINK_SECONDS", -1)
    assert client.get(url).status_code == 410
    assert client.post(url, {"name": "PILOT"}).status_code == 410
    assert not game.accounts


def test_a_reserved_name_is_refused_in_the_servers_words_and_another_may_be_chosen(
    game: FakeGame,
) -> None:
    client = Client()
    register(client, "SYSOP", "p@example.org")
    url = last_link()
    refused = client.post(url, {"name": "SYSOP", "callsign": ""})
    assert refused.status_code == 200
    assert "THAT NAME IS RESERVED" in page(refused)
    assert not Player.objects.exists()
    made = client.post(url, {"name": "OTHER", "callsign": ""})  # the link still works
    assert "MADEUP0001" in page(made)
    assert Player.objects.get().game_name == "OTHER"


def test_the_shape_of_a_name_is_checked_before_any_email(game: FakeGame) -> None:
    client = Client()
    for name, words in (("ab", "3 TO 12"), ("NO SPACES", "ONLY LETTERS"), ("X" * 13, "")):
        response = register(client, name, "p@example.org")
        assert response.status_code == 200
        assert "Check your email" not in page(response)
        assert words in page(response)
    assert "CALLSIGN MAY HOLD" in page(register(client, "GOOD", "p@example.org", callsign="a b c"))
    assert not mail.outbox and not game.calls


def test_a_taken_name_is_said_before_any_email(game: FakeGame) -> None:
    game.add("TAKEN")
    response = register(Client(), "taken", "p@example.org")
    assert "THAT NAME IS TAKEN" in page(response)
    assert not mail.outbox


def test_an_address_with_an_account_is_sent_a_reminder_and_the_page_says_the_same(
    game: FakeGame,
) -> None:
    registered(Client(), game)
    mail.outbox.clear()
    response = register(Client(), "SECOND", "P@example.org")
    first = register(Client(), "THIRD", "new@example.org")
    assert page(response).replace("p@example.org", "X") == page(first).replace(
        "new@example.org", "X"
    ).replace("p@example.org", "X")
    reminder, link = mail.outbox
    assert reminder.subject == "You already have an OpenCS account"
    assert "PILOT" in str(reminder.body) and "/register/" not in str(reminder.body)
    assert "/login" in str(reminder.body)
    assert link.subject == "Confirm your OpenCS account"
    assert EmailLink.objects.filter(email="p@example.org", kind="register", used=None).count() == 0


def test_two_links_for_one_address_make_one_account(game: FakeGame) -> None:
    client = Client()
    register(client, "FIRST", "p@example.org")
    one = last_link()
    register(client, "SECOND", "p@example.org")
    two = last_link()
    assert "MADEUP" in page(client.post(one, {"name": "FIRST"}))
    other = client.post(two, {"name": "SECOND"})
    assert "already has a game account" in page(other)
    assert list(game.accounts) == ["FIRST"]


# --- the challenge and the limits --------------------------------------------------------------


def test_a_form_with_its_hidden_field_filled_looks_sent_but_sends_nothing(game: FakeGame) -> None:
    response = register(Client(), "PILOT", "p@example.org", website="http://spam.example")
    assert "Check your email" in page(response)
    assert not mail.outbox and not EmailSent.objects.exists()


def test_a_form_sent_back_too_quickly_or_without_its_time_is_shown_again(
    game: FakeGame, settings: SettingsWrapper
) -> None:
    settings.OPENCS_FORM_MIN_SECONDS = 60
    response = register(Client(), "PILOT", "p@example.org")
    assert "quicker than a person types" in page(response)
    response = register(Client(), "PILOT", "p@example.org", stamp="forged")
    assert "not understood" in page(response)
    assert not mail.outbox


def test_emails_to_one_address_are_limited(game: FakeGame) -> None:
    for i in range(3):
        assert (
            register(Client(), f"NAME{i}", "p@example.org", address=f"192.0.2.{i}").status_code
            == 200
        )
    refused = register(Client(), "NAME9", "P@EXAMPLE.ORG", address="192.0.2.9")
    assert refused.status_code == 429
    assert "sent to that address" in page(refused)
    assert len(mail.outbox) == 3


def test_emails_from_one_network_are_limited_and_an_ipv6_64_is_one_network(
    game: FakeGame,
) -> None:
    for i in range(5):
        response = register(
            Client(), f"NAME{i}", f"p{i}@example.org", address=f"2001:db8:1:2::{i + 1}"
        )
        assert response.status_code == 200
    refused = register(Client(), "NAME9", "p9@example.org", address="2001:db8:1:2:ffff::9")
    assert refused.status_code == 429
    assert "from your network" in page(refused)
    assert (
        register(Client(), "NAME9", "p9@example.org", address="2001:db8:1:3::1").status_code == 200
    )
    assert len(mail.outbox) == 6


def test_questions_to_the_game_server_are_limited_per_network(game: FakeGame) -> None:
    for i in range(accounts.ASKS_PER_SOURCE[0]):
        register(Client(), f"NAME{i}", f"p{i}@example.org", address="192.0.2.7")
    asked = len(game.calls)
    refused = register(Client(), "LAST", "last@example.org", address="192.0.2.7")
    assert "Too many requests from your network" in page(refused)
    assert len(game.calls) == asked
    assert "Check your email" in page(register(Client(), "LAST", "last@example.org"))


def test_the_daily_cap_stops_links_but_not_a_password(
    game: FakeGame, settings: SettingsWrapper
) -> None:
    settings.OPENCS_EMAIL_DAILY_CAP = 2
    settings.OPENCS_PASSWORD_DELIVERY = "email"
    client = Client()
    register(client, "FIRST", "a@example.org", address="192.0.2.1")
    url = last_link()
    register(Client(), "SECOND", "b@example.org", address="192.0.2.2")
    capped = register(Client(), "THIRD", "c@example.org", address="192.0.2.3")
    assert capped.status_code == 503
    assert "as many emails today" in page(capped)
    assert len(mail.outbox) == 2
    made = client.post(url, {"name": "FIRST"})
    assert "emailed the password" in page(made)
    assert mail.outbox[-1].subject == "Your OpenCS password"


class FailingBackend(BaseEmailBackend):
    """A mail provider that is down, or refuses: every send raises."""

    def send_messages(self, email_messages: Any) -> int:
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")


class FailingForPasswords(BaseEmailBackend):
    def send_messages(self, email_messages: Any) -> int:
        for message in email_messages:
            if "password" in message.subject:
                raise smtplib.SMTPDataError(451, b"try later")
            mail.outbox.append(message)
        return len(email_messages)


def _mailer(settings: SettingsWrapper, backend: type) -> None:
    settings.MAILERS = {"default": {"BACKEND": f"{__name__}.{backend.__name__}"}}


def test_an_email_that_cannot_be_sent_is_said_plainly_and_leaves_nothing_half_done(
    game: FakeGame, settings: SettingsWrapper, caplog: pytest.LogCaptureFixture
) -> None:
    _mailer(settings, FailingBackend)
    response = register(Client(), "PILOT", "p@example.org")
    assert response.status_code == 503
    assert "could not be sent" in page(response)
    assert "try again later" in page(response)
    assert not EmailLink.objects.exists() and not game.accounts
    assert "could not send an email" in caplog.text
    settings.OPENCS_CONTACT = "ops@example.org"
    assert "ops@example.org" in page(log_in(Client(), "p@example.org"))


# --- delivering the password -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delivery", "shown", "emailed"),
    [("show", True, False), ("email", False, True), ("both", True, True)],
)
def test_a_made_up_password_is_delivered_as_the_setting_says(
    game: FakeGame, settings: SettingsWrapper, delivery: str, shown: bool, emailed: bool
) -> None:
    settings.OPENCS_PASSWORD_DELIVERY = delivery
    made = registered(Client(), game)
    assert ("MADEUP0001" in page(made)) is shown
    sent = [m for m in mail.outbox if m.subject == "Your OpenCS password"]
    assert bool(sent) is emailed
    if sent:
        assert "MADEUP0001" in str(sent[0].body)
        assert sent[0].to == ["p@example.org"]


def test_a_password_email_that_fails_shows_the_password_instead(
    game: FakeGame, settings: SettingsWrapper
) -> None:
    settings.OPENCS_PASSWORD_DELIVERY = "email"
    _mailer(settings, FailingForPasswords)
    made = registered(Client(), game)
    assert "MADEUP0001" in page(made)
    assert "could not email the password" in page(made)


def test_a_chosen_password_goes_to_the_server_and_is_never_shown_or_emailed(
    game: FakeGame, settings: SettingsWrapper
) -> None:
    settings.OPENCS_CHOSEN_PASSWORDS = True
    settings.OPENCS_PASSWORD_DELIVERY = "both"
    client = Client()
    register(client, "PILOT", "p@example.org")
    url = last_link()
    assert 'type="password"' in page(client.get(url))
    refused = client.post(url, {"name": "PILOT", "password": "lower!1"})
    assert "A PASSWORD MAY HAVE NO LOWER CASE" in page(refused)
    assert "lower!1" not in page(refused)
    made = client.post(url, {"name": "PILOT", "password": "MY-OWN-9"})
    assert game.accounts["PILOT"]["password"] == "MY-OWN-9"
    assert "MY-OWN-9" not in page(made)
    assert "the one you chose" in page(made)
    assert all("MY-OWN-9" not in str(m.body) for m in mail.outbox)
    assert all(m.subject != "Your OpenCS password" for m in mail.outbox)


def test_a_password_is_not_taken_when_players_may_not_choose(game: FakeGame) -> None:
    registered(Client(), game, password="MY-OWN-9")
    assert game.accounts["PILOT"]["password"] == "MADEUP0001"


# --- the game server offline -------------------------------------------------------------------


def test_with_the_game_server_offline_no_email_is_sent_and_a_link_waits_for_it(
    game: FakeGame,
) -> None:
    game.offline = True
    client = Client()
    response = register(client, "PILOT", "p@example.org")
    assert "not answering" in page(response)
    assert not mail.outbox
    game.offline = False
    register(client, "PILOT", "p@example.org")
    url = last_link()
    game.offline = True
    waiting = client.post(url, {"name": "PILOT"})
    assert "keeps working until it expires" in page(waiting)
    assert not Player.objects.exists()
    game.offline = False
    assert "MADEUP0001" in page(client.post(url, {"name": "PILOT"}))


@pytest.mark.parametrize("socket", [None, Path("/nonexistent/service.sock")])
def test_without_a_game_server_at_all_registration_says_so(
    settings: SettingsWrapper, socket: Path | None
) -> None:
    settings.OPENCS_FORM_MIN_SECONDS = 0
    settings.OPENCS_SERVICE_SOCKET = socket
    live.reset()
    accounts.reset()
    response = register(Client(), "PILOT", "p@example.org")
    assert response.status_code == 200
    assert "not answering" in page(response)
    assert not mail.outbox


# --- logging in --------------------------------------------------------------------------------


def test_an_emailed_link_logs_in_once_and_logout_ends_the_session(game: FakeGame) -> None:
    registered(Client(), game)
    client = Client()
    assert client.get("/account").status_code == 302
    assert "Check your email" in page(log_in(client, "P@Example.org"))
    message = mail.outbox[-1]
    assert message.subject == "Log in to OpenCS"
    url = link_in(message)
    assert client.get(url).status_code == 200  # a page with a button, nothing used
    response = client.post(url)
    assert response.status_code == 302 and response["Location"] == "/account"
    assert "PILOT" in page(client.get("/account"))
    assert client.post(url).status_code == 410
    client.post("/logout")
    assert client.get("/account").status_code == 302


def test_a_log_in_for_an_unknown_address_looks_the_same_and_sends_no_link(game: FakeGame) -> None:
    response = log_in(Client(), "nobody@example.org")
    assert "Check your email" in page(response)
    (message,) = mail.outbox
    assert "no account here uses it" in str(message.body)
    assert "/login/" not in str(message.body)
    assert not EmailLink.objects.exists()


def test_a_log_in_link_expires(game: FakeGame, monkeypatch: pytest.MonkeyPatch) -> None:
    registered(Client(), game)
    client = Client()
    log_in(client, "p@example.org")
    monkeypatch.setattr(accounts, "LOGIN_LINK_SECONDS", -1)
    assert client.post(last_link()).status_code == 410


# --- the account page --------------------------------------------------------------------------


def test_a_game_account_removed_on_the_server_takes_the_web_account_with_it(
    game: FakeGame,
) -> None:
    client = Client()
    registered(client, game)
    del game.accounts["PILOT"]  # the operator's delete-account, or the prune
    gone = client.get("/account")
    assert gone.status_code == 410
    assert "no longer on the game server" in page(gone)
    assert not Player.objects.exists()
    assert client.get("/account").status_code == 302  # logged out


def test_a_later_account_of_the_same_name_is_not_taken_for_ours(game: FakeGame) -> None:
    client = Client()
    registered(client, game)
    del game.accounts["PILOT"]
    game.add("PILOT")  # someone else's, made since
    assert client.post("/account/password").status_code == 410
    assert game.accounts["PILOT"]["password"] == "OLD"
    assert not Player.objects.exists()


def test_registering_a_name_the_server_freed_replaces_the_stale_web_account(
    game: FakeGame,
) -> None:
    registered(Client(), game)
    del game.accounts["PILOT"]
    registered(Client(), game, email="q@example.org")
    assert list(Player.objects.values_list("email", flat=True)) == ["q@example.org"]


def test_an_unplayed_account_is_warned_of_the_prune(game: FakeGame) -> None:
    client = Client()
    registered(client, game)
    assert "never played" in page(client.get("/account"))
    game.accounts["PILOT"]["last_login"] = 1.0
    assert "never played" not in page(client.get("/account"))


def test_the_account_page_while_the_game_server_is_offline(game: FakeGame) -> None:
    client = Client()
    registered(client, game)
    game.offline = True
    response = client.get("/account")
    assert response.status_code == 200
    assert "cannot be reached" in page(response)
    assert Player.objects.exists()


def test_the_callsign_is_changed_through_the_server_which_says_when_not(game: FakeGame) -> None:
    client = Client()
    registered(client, game)
    response = client.post("/account/callsign", {"callsign": "Viper"})
    assert response.status_code == 302
    assert "Your callsign is now Viper." in page(client.get("/account"))
    assert game.accounts["PILOT"]["callsign"] == "Viper"
    refused = client.post("/account/callsign", {"callsign": "Cobra"})
    assert "YOU MAY CHOOSE A NEW CALLSIGN IN 7 DAYS" in page(refused)


def test_a_new_password_is_made_up_and_shown_once(game: FakeGame) -> None:
    client = Client()
    registered(client, game)
    response = client.post("/account/password")
    assert "NEWONE0002" in page(response)
    assert response["Cache-Control"] == "no-store"
    assert "does not end" in page(response)
    assert ("reset-password", "PILOT", "") in game.calls


def test_forgetting_the_address_keeps_the_game_account(game: FakeGame) -> None:
    client = Client()
    registered(client, game)
    wrong = client.post("/account/delete", {"what": "forget", "confirm": "OTHER"})
    assert wrong.status_code == 400 and Player.objects.exists()
    response = client.post("/account/delete", {"what": "forget", "confirm": "pilot"})
    assert "stays on the" in page(response)
    assert not Player.objects.exists()
    assert "PILOT" in game.accounts
    assert client.get("/account").status_code == 302


def test_deleting_the_game_account_goes_through_the_server(game: FakeGame) -> None:
    client = Client()
    registered(client, game)
    game.accounts["PILOT"]["online"] = True
    refused = client.post("/account/delete", {"what": "game", "confirm": "PILOT"})
    assert "log off first" in page(refused)
    assert Player.objects.exists()
    game.accounts["PILOT"]["online"] = False
    done = client.post("/account/delete", {"what": "game", "confirm": "PILOT"})
    assert "progress and your email address are deleted" in page(done)
    assert "PILOT" not in game.accounts and not Player.objects.exists()


# --- escaping and CSRF -------------------------------------------------------------------------


def test_everything_a_player_types_is_escaped(game: FakeGame) -> None:
    client = Client()
    hostile = "<b>x</b>"
    shown = register(client, "PILOT", "p@example.org", callsign=hostile)
    url = last_link()
    form = page(client.get(url))
    assert hostile not in form and "&lt;b&gt;x&lt;/b&gt;" in form
    client.post(url, {"name": "PILOT", "callsign": hostile})
    account = page(client.get("/account"))
    assert hostile not in account and "&lt;b&gt;x&lt;/b&gt;" in account
    assert shown.status_code == 200
    refused = register(Client(), "PILOT2", '"<script>"@example.org')
    assert "<script>" not in page(refused)


def test_every_form_needs_its_csrf_token(game: FakeGame) -> None:
    registered(Client(), game)
    client = Client(enforce_csrf_checks=True)
    assert register(client, "OTHER", "o@example.org").status_code == 403
    assert log_in(client, "p@example.org").status_code == 403
    for path in ("/logout", "/account/password", "/account/callsign", "/account/delete"):
        assert client.post(path).status_code == 403
    page_with_form = client.get("/register")
    assert 'name="csrfmiddlewaretoken"' in page(page_with_form)


# --- the other pages ---------------------------------------------------------------------------


def test_the_privacy_note_is_linked_from_every_page_and_takes_the_operators_words(
    game: FakeGame, settings: SettingsWrapper, tmp_path: Path
) -> None:
    for path in ("/", "/register", "/login"):
        assert 'href="/privacy"' in page(Client().get(path))
    note = page(Client().get("/privacy"))
    assert "Your email address" in note and "may be recorded" in note
    words = tmp_path / "privacy.txt"
    words.write_text("Games here are recorded\nfor a year.\n\n<i>Ask</i> us.\n")
    settings.OPENCS_PRIVACY_FILE = words
    note = page(Client().get("/privacy"))
    assert "<p>Games here are recorded for a year.</p>" in note
    assert "&lt;i&gt;Ask&lt;/i&gt; us." in note


def test_with_registration_off_the_site_takes_none(
    game: FakeGame, settings: SettingsWrapper
) -> None:
    settings.OPENCS_REGISTRATION = False
    settings.OPENCS_CONTACT = "ops@example.org"
    response = register(Client(), "PILOT", "p@example.org")
    assert "Registration is closed" in page(response) and "ops@example.org" in page(response)
    assert not mail.outbox
    assert 'href="/register"' not in page(Client().get("/"))


# --- the operations on the wire ----------------------------------------------------------------


def test_the_game_operations_speak_the_service_channels_requests() -> None:
    with socket_dir() as d:
        sock = d / "service.sock"

        async def scenario() -> list[dict[str, Any]]:
            fake = await FakeService(sock).start()
            fake.results = {
                "register": {"name": "PILOT", "password": "P", "callsign": None, "created": 1.5},
                "delete-account": {"lines": ["refused: PILOT is online; kick it first"]},
            }
            from django.test import override_settings  # noqa: PLC0415

            with override_settings(OPENCS_SERVICE_SOCKET=sock):
                live.reset()
                made = await Game().register("PILOT", None, None)
                assert made["created"] == 1.5
                await Game().register("PILOT", "Ace", "SECRET1")
                with pytest.raises(RefusedError, match="log off first"):
                    await Game().delete_account("PILOT")
                await live.channel().close()
            await fake.stop()
            return fake.requests

        requests = asyncio.run(scenario())
        live.reset()
    assert [{k: v for k, v in r.items() if k != "id"} for r in requests] == [
        {"op": "register", "name": "PILOT"},
        {"op": "register", "name": "PILOT", "callsign": "Ace", "password": "SECRET1"},
        {"op": "delete-account", "name": "PILOT"},
    ]
