"""Server-wide limits on logins (DESIGN §5 "login attempts").

A password check costs a PBKDF2 run on one of a few hashing threads, so a flood of logins, from one
address or many, would hold every other login up. Each costly step spends a token from the
source's bucket and from a global one; and guests, who cost no check, are counted online. A source
is an address, or an IPv6 /64 (``ConnectionContext.source``), since a host may use all of one.

Wrong passwords for an account are limited per source, so one source's guesses never spend
another's allowance, and across every source the owner has not logged in from since the server
started, so many addresses cannot take turns guessing one account. Those sources are remembered
in memory only; nothing about addresses is stored. The numbers are in ``policy``. The gate handed
to ``Identity`` is made per attempt.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from opencs import policy
from opencs.clock import Clock
from opencs.ratelimit import KeyedBuckets, TokenBucket


@dataclass(frozen=True, slots=True)
class Guess:
    """A password check charged to account ``name`` from ``source``; ``shared`` if it was charged
    to the allowance of the sources the account has not logged in from too."""

    name: str
    source: str
    shared: bool


class LoginLimits:
    """The buckets, shared by every session."""

    def __init__(
        self,
        clock: Clock,
        *,
        max_guests: int = policy.MAX_GUESTS,
        max_guests_per_address: int = policy.MAX_GUESTS_PER_ADDRESS,
    ) -> None:
        self._clock = clock
        self._max_guests = max_guests
        self._max_guests_per_address = max_guests_per_address
        now = clock.now()
        self._attempts: KeyedBuckets[str] = KeyedBuckets(*policy.LOGIN_ATTEMPTS_PER_ADDRESS)
        self._attempts_global = TokenBucket(*policy.LOGIN_ATTEMPTS_GLOBAL, now)
        self._accounts: KeyedBuckets[str] = KeyedBuckets(*policy.NEW_ACCOUNTS_PER_ADDRESS)
        self._accounts_global = TokenBucket(*policy.NEW_ACCOUNTS_GLOBAL, now)
        self._guests: Counter[str] = Counter()  # guests online, by address
        self._failures: KeyedBuckets[tuple[str, str]] = KeyedBuckets(
            *policy.FAILED_LOGINS_PER_ACCOUNT_AND_SOURCE
        )  # by (account, source)
        self._failures_unknown: KeyedBuckets[str] = KeyedBuckets(
            *policy.FAILED_LOGINS_PER_ACCOUNT
        )  # by account, for the sources not in _known
        self._known: dict[
            str, list[str]
        ] = {}  # account: the sources it logged in from, oldest first

    def try_account(self, name: str, source: str) -> Guess | None:
        """Spend a password check for account ``name`` from ``source``, before it is made, so
        checks running at once cannot all slip past the limit: one from the source's own allowance
        (``FAILED_LOGINS_PER_ACCOUNT_AND_SOURCE``) and, unless the account has logged in from it,
        one from that of every such source (``FAILED_LOGINS_PER_ACCOUNT``). None, spending
        nothing, if either is empty. A wrong password keeps it spent; ``give_back`` returns it."""
        now = self._clock.now()
        own = (name, source)
        shared = not self._is_known(name, source)
        if not self._failures.has_token(own, now) or (
            shared and not self._failures_unknown.has_token(name, now)
        ):
            return None
        self._failures.take(own, now)
        if shared:
            self._failures_unknown.take(name, now)
        return Guess(name, source, shared)

    def give_back(self, guess: Guess) -> None:
        """Return what ``try_account`` spent: no password was checked, or it was right."""
        now = self._clock.now()
        self._failures.give_back((guess.name, guess.source), now)
        if guess.shared:
            self._failures_unknown.give_back(guess.name, now)

    def account_succeeded(self, name: str, source: str) -> None:
        """The right password for ``name`` came from ``source``: its failures there are forgotten,
        and from now on it is a source the owner uses. Give the check back first."""
        self._failures.forget((name, source))
        self._learn(name, source)

    def forget_account(self, name: str) -> None:
        """Forget every failure and known source of account ``name`` (its password was reset)."""
        self._failures.forget_if(lambda key: key[0] == name)
        self._failures_unknown.forget(name)
        self._known.pop(name, None)

    def _is_known(self, name: str, source: str) -> bool:
        return source in self._known.get(name, ())

    def _learn(self, name: str, source: str) -> None:
        """Remember ``source`` for ``name``: the most recent ``KNOWN_SOURCES_PER_ACCOUNT``, for the
        most recent ``MAX_TRACKED_SOURCES`` accounts."""
        sources = self._known.pop(name, [])
        if source in sources:
            sources.remove(source)
        sources.append(source)
        del sources[: -policy.KNOWN_SOURCES_PER_ACCOUNT]
        while len(self._known) >= policy.MAX_TRACKED_SOURCES:
            del self._known[next(iter(self._known))]
        self._known[name] = sources

    def may_admit_guest(self, address: str) -> bool:
        """Whether a guest from ``address`` may log in now (``max_guests*``)."""
        return (
            self._guests[address] < self._max_guests_per_address
            and self._guests.total() < self._max_guests
        )

    def guest_in(self, address: str) -> None:
        """A guest from ``address`` has logged in."""
        self._guests[address] += 1

    def guest_out(self, address: str) -> None:
        """A guest from ``address`` has gone."""
        self._guests[address] -= 1
        if self._guests[address] <= 0:
            del self._guests[address]

    def gate(self, address: str) -> AddressGate:
        return AddressGate(self, address)

    def attempt(self, address: str) -> bool:
        """Spend one password check for ``address``. The address is charged first, so a flooder
        cannot use up the global allowance for everyone else once it is itself refused."""
        now = self._clock.now()
        return self._attempts.take(address, now) and self._attempts_global.take(now)

    def register(self, address: str) -> bool:
        """Spend one new account for ``address``."""
        now = self._clock.now()
        return self._accounts.take(address, now) and self._accounts_global.take(now)


class AddressGate:
    """A ``LoginGate`` for one source address."""

    def __init__(self, limits: LoginLimits, address: str) -> None:
        self._limits = limits
        self._address = address
        self._guess: Guess | None = None  # what may_try_account spent

    def may_check_password(self) -> bool:
        return self._limits.attempt(self._address)

    def may_register(self) -> bool:
        return self._limits.register(self._address)

    def may_admit_guest(self) -> bool:
        return self._limits.may_admit_guest(self._address)

    def may_try_account(self, name: str) -> bool:
        self._guess = self._limits.try_account(name, self._address)
        return self._guess is not None

    def account_not_tried(self, name: str) -> None:
        if (guess := self._take_guess(name)) is not None:
            self._limits.give_back(guess)

    def account_succeeded(self, name: str) -> None:
        if (guess := self._take_guess(name)) is not None:
            self._limits.give_back(guess)
        self._limits.account_succeeded(name, self._address)

    def _take_guess(self, name: str) -> Guess | None:
        guess, self._guess = self._guess, None
        return guess if guess is not None and guess.name == name else None
