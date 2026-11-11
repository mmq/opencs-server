"""Who a login line is: real accounts and ``JOINGENIE`` guests, and their callsigns.

II §3, I §5.1. ``Identity`` knows nothing of sockets or sessions: it
takes the account line and answers with a ``Pilot`` or a reason to refuse. It tracks who is online
so a second login is refused and callsigns stay unique server-wide.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from argon2.low_level import Type, hash_secret_raw

from opencs import policy
from opencs.store.base import ARGON2ID, PBKDF2_SHA256, Account, AccountStore, Kdf

log = logging.getLogger("opencs.lobby")


def identity_key(callsign: str, account: str | None) -> str:
    """The key a player's record is stored under (II §3): ``acct:NAME`` for a
    real account, else the callsign the lobby assigned (``JOINGENI``, ``JOINGEN2``, ...)."""
    return callsign if account is None else policy.ACCOUNT_KEY_PREFIX + account


class LoginGate(Protocol):
    """Meters the costly steps of a login across connections (DESIGN §5). The session layer
    supplies one per attempt, knowing the source address; ``Identity`` knows only the two steps."""

    def may_check_password(self) -> bool:
        """Whether a password check (a hash) may be made now."""
        ...

    def may_register(self) -> bool:
        """Whether a new account may be created now."""
        ...

    def may_admit_guest(self) -> bool:
        """Whether another guest may log in now."""
        ...

    def may_try_account(self, name: str) -> bool:
        """Whether a password for account ``name`` may be checked now: not while the recent
        failures from this source, or from the sources its owner has not used, are over their
        limit. A yes spends one, before the check, so checks running at once cannot all pass;
        a wrong password keeps it spent."""
        ...

    def account_not_tried(self, name: str) -> None:
        """No password for ``name`` was checked after all: what ``may_try_account`` spent comes
        back."""
        ...

    def account_succeeded(self, name: str) -> None:
        """The right password was given for ``name``: what ``may_try_account`` spent comes back,
        and this source's failures are forgotten."""
        ...


@dataclass(frozen=True, slots=True)
class Pilot:
    """A logged-in player. ``account`` is the upper-cased account name, None for a guest, whose
    persistence key is the callsign (II §3)."""

    callsign: str
    account: str | None

    @property
    def is_guest(self) -> bool:
        return self.account is None

    @property
    def key(self) -> str:
        """Where this pilot's progress is stored."""
        return identity_key(self.callsign, self.account)


@dataclass(frozen=True, slots=True)
class Refused:
    """A login that is not accepted, with a line to show the player."""

    reason: str


@dataclass(frozen=True, slots=True)
class PasswordCheck:
    """An account login waiting for its password hash (DESIGN §5). ``hash`` is pure CPU and touches
    nothing shared, so it may run on another thread; its result goes back to ``Identity.complete``
    on the event loop. ``stored`` is the account to compare with, None if the login registers it."""

    name: str
    password: str = field(repr=False)
    salt: bytes = field(repr=False)
    kdf: Kdf
    stored: Account | None

    def hash(self) -> bytes:
        return hash_password(self.password, self.salt, self.kdf)


#: How new password hashes are made (DESIGN §5, ADR 13).
CURRENT_KDF = Kdf.argon2id(
    memory_kib=policy.ARGON2_MEMORY_KIB,
    time_cost=policy.ARGON2_TIME_COST,
    parallelism=policy.ARGON2_PARALLELISM,
    hash_bytes=policy.ARGON2_HASH_BYTES,
)


def hash_password(password: str, salt: bytes, kdf: Kdf) -> bytes:
    """The salted hash of ``password`` made the way ``kdf`` says (DESIGN §5): Argon2id, or
    PBKDF2-HMAC-SHA256 for an account made before Argon2id. Both release the GIL while they run,
    so the hashing threads work side by side."""
    secret = password.encode("utf-8")
    if kdf.algorithm == ARGON2ID:
        return hash_secret_raw(
            secret,
            salt,
            time_cost=kdf.time_cost,
            memory_cost=kdf.memory_kib,
            parallelism=kdf.parallelism,
            hash_len=kdf.hash_bytes,
            type=Type.ID,
        )
    if kdf.algorithm == PBKDF2_SHA256:
        return hashlib.pbkdf2_hmac("sha256", secret, salt, kdf.time_cost, kdf.hash_bytes)
    raise ValueError(f"unknown password hash algorithm {kdf.algorithm!r}")


def make_password() -> str:
    """A random password with no look-alike characters (``policy.GENERATED_PASSWORD_*``)."""
    return "".join(
        secrets.choice(policy.GENERATED_PASSWORD_ALPHABET)
        for _ in range(policy.GENERATED_PASSWORD_LENGTH)
    )


def look_alikes(name: str) -> set[str]:
    """``name`` as it is, and as it reads with look-alike digits taken for their letters and the
    separators dropped (``policy.LOOK_ALIKE_*``): the forms a reserved name is tested against, so
    ``ADM1N`` and ``AD_MIN`` are as reserved as ``ADMIN``. A 1 is read as I in one form and L in
    the other."""
    bare = name.translate(str.maketrans("", "", policy.LOOK_ALIKE_DROPPED))
    as_i = str.maketrans(policy.LOOK_ALIKE_LETTERS)
    as_l = str.maketrans(policy.LOOK_ALIKE_LETTERS | policy.LOOK_ALIKE_ONE_AS_L)
    return {name, bare.translate(as_i), bare.translate(as_l)}


class Identity:
    """Logs players in and out and hands out callsigns."""

    def __init__(
        self,
        store: AccountStore,
        *,
        guest_names: tuple[str, ...] = policy.DEFAULT_GUEST_NAMES,
        kdf: Kdf = CURRENT_KDF,
        registration: str = policy.DEFAULT_REGISTRATION,
        min_password_length: int = policy.MIN_PASSWORD_LENGTH,
        max_accounts: int = policy.MAX_ACCOUNTS,
    ) -> None:
        self._store = store
        self._guest_names = frozenset(name.upper() for name in guest_names)
        self._kdf = kdf
        self._registration_open = registration == "open"
        self._min_password_length = min_password_length
        self._max_accounts = max_accounts
        self._callsigns: set[str] = set()
        self._accounts: set[str] = set()

    def login(self, line: str, gate: LoginGate | None = None) -> Pilot | Refused:
        """``begin`` and, for an account, ``complete`` with the hash made here and now. The
        session runs the hash off the event loop instead; this is for callers that may block."""
        result = self.begin(line, gate)
        if not isinstance(result, PasswordCheck):
            return result
        pilot = self.complete(result, result.hash(), gate)
        if isinstance(pilot, Pilot) and (rehash := self.rehash(result)) is not None:
            try:
                self.store_rehash(rehash, rehash.hash())
            except Exception:
                log.exception("could not rehash the password of %s", result.name)
        return pilot

    def begin(self, line: str, gate: LoginGate | None = None) -> Pilot | Refused | PasswordCheck:
        """Resolve an account line (I §5.1): ``NAME,PASSWORD`` or ``NAME``.

        - A configured guest name, with or without anything after it, is a guest: it gets a
          callsign from its name (II §3). ``JOINGENIE`` is refused when it is not one.
        - ``NAME,PASSWORD`` is a real account: the first login registers the password, later
          ones must match, a second simultaneous one is refused (II §3). ``NAME`` alone is
          refused for want of a password.

        ``gate`` meters the password checks and registrations; over its limit the login is refused.
        A guest is answered at once; an account login needs its password hashed first, so the
        answer is a ``PasswordCheck`` to run and hand to ``complete``.
        """
        name, _, password = line.strip().partition(",")
        name = name.strip().upper()
        if not name:
            return Refused("EMPTY LOGIN NAME")
        if len(name) > policy.MAX_ACCOUNT_NAME_LENGTH:
            return Refused("LOGIN NAME TOO LONG")
        if name in self._guest_names:
            return self._guest(name, gate)
        if name == policy.GUEST_ACCOUNT_NAME:
            return Refused("GUEST LOGINS ARE TURNED OFF ON THIS SERVER")
        return self._account(name, password, gate)

    def release(self, pilot: Pilot) -> None:
        """The pilot has gone: free its callsign and account for the next login."""
        self._callsigns.discard(pilot.callsign)
        if pilot.account is not None:
            self._accounts.discard(pilot.account)

    def callsign_online(self, callsign: str) -> bool:
        """True if ``callsign`` (upper case) is in use."""
        return callsign in self._callsigns

    def _guest(self, name: str, gate: LoginGate | None) -> Pilot | Refused:
        # Compare callsigns, not names: ALPHAXYZW would otherwise be played as ALPHAXYZ's callsign.
        base = name[: policy.MAX_CALLSIGN_LENGTH]
        if name != policy.GUEST_ACCOUNT_NAME and self._store.callsign_registered(
            base, policy.MAX_CALLSIGN_LENGTH
        ):
            return Refused("THAT NAME IS A REGISTERED ACCOUNT: LOG IN WITH ITS PASSWORD")
        if gate is not None and not gate.may_admit_guest():
            return Refused("TOO MANY GUESTS ONLINE: TRY AGAIN LATER")
        return self._admit(name, None)

    def complete(
        self, check: PasswordCheck, digest: bytes, gate: LoginGate | None = None
    ) -> Pilot | Refused:
        """Finish an account login with ``check.hash()``. The world may have moved on while it
        ran: the account may have logged in elsewhere, or been registered by another login."""
        if check.stored is None:
            account = Account(check.name, check.salt, digest, check.kdf)
            if not self._store.create(account):
                if gate is not None:
                    gate.account_not_tried(check.name)  # a lost race to register, not a guess
                return Refused("THAT NAME WAS JUST TAKEN: TRY AGAIN")
        elif not hmac.compare_digest(check.stored.password_hash, digest):
            return Refused("WRONG PASSWORD")  # the guess stays spent
        if gate is not None:
            gate.account_succeeded(check.name)
        if check.name in self._accounts:
            return Refused("THAT ACCOUNT IS ALREADY LOGGED IN")
        if check.stored is not None:
            self._store.touch_login(check.name)
        return self._admit(check.name, check.name)

    def rehash(self, check: PasswordCheck) -> PasswordCheck | None:
        """After ``check`` let a player in: a hash of the same password made the current way, to
        run off the event loop and hand to ``store_rehash``, if the account's hash was made some
        other way (PBKDF2, or an older Argon2id cost; ADR 13). None if it is current. It costs a
        hash but no guess: the password was right."""
        if check.stored is None or check.stored.kdf == self._kdf:
            return None
        salt = os.urandom(policy.PASSWORD_SALT_BYTES)
        return PasswordCheck(check.name, check.password, salt, self._kdf, check.stored)

    def store_rehash(self, rehash: PasswordCheck, digest: bytes) -> bool:
        """Replace the account's hash with ``rehash.hash()``, unless the account changed while it
        ran (a reset, a delete): then the newer state stands. True if it was stored."""
        if rehash.stored is None or self._store.get(rehash.name) != rehash.stored:
            return False
        return self._store.set_password(Account(rehash.name, rehash.salt, digest, rehash.kdf))

    # --- accounts, outside a login -----------------------------------------------------------

    def new_account_refusal(self, name: str, password: str) -> Refused | None:
        """Why ``name`` (upper case) with ``password`` cannot be a new account, None if it can:
        every rule a registration must pass, wherever it comes from (a first login now; a
        registration command or page later). It does not look at the limits on how often."""
        if (
            reason := self._new_name_problem(name) or self._new_password_problem(name, password)
        ) is not None:
            return Refused(reason)
        if self._store.count() >= self._max_accounts:
            return Refused("THIS SERVER HAS NO ROOM FOR NEW ACCOUNTS")
        return None

    def _new_name_problem(self, name: str) -> str | None:
        if not policy.MIN_ACCOUNT_NAME_LENGTH <= len(name) <= policy.MAX_CALLSIGN_LENGTH:
            return (
                f"A NEW NAME MUST BE {policy.MIN_ACCOUNT_NAME_LENGTH} TO "
                f"{policy.MAX_CALLSIGN_LENGTH} CHARACTERS"
            )
        if any(ch not in policy.ACCOUNT_NAME_CHARACTERS for ch in name):
            return "A NEW NAME MAY HOLD ONLY LETTERS, DIGITS, _ AND -"
        if any(
            seen in policy.RESERVED_ACCOUNT_NAMES or self._a_guest_could_be(seen)
            for seen in look_alikes(name)
        ):
            return "THAT NAME IS RESERVED"
        return None

    def _a_guest_could_be(self, callsign: str) -> bool:
        """Whether ``_allocate_callsign`` could give ``callsign`` to a guest: a guest name cut to
        12 characters, or with its tail replaced by a number (``JOINGENIE2``)."""
        for guest in self._guest_names:
            base = guest[: policy.MAX_CALLSIGN_LENGTH]
            if callsign == base:
                return True
            for n in range(policy.CALLSIGN_FIRST_SUFFIX, policy.CALLSIGN_LAST_SUFFIX + 1):
                suffix = str(n)
                if callsign == base[: policy.MAX_CALLSIGN_LENGTH - len(suffix)] + suffix:
                    return True
        return False

    def _new_password_problem(self, name: str, password: str) -> str | None:
        if len(password) < self._min_password_length:
            return (
                f"A NEW ACCOUNT NEEDS A PASSWORD OF {self._min_password_length} CHARACTERS OR MORE"
            )
        if len(password) > policy.MAX_PASSWORD_LENGTH:
            return "PASSWORD TOO LONG"
        shouted = password.upper()  # the client capitalises what it sends
        if shouted == name:
            return "A PASSWORD MUST NOT BE THE ACCOUNT NAME"
        if shouted in policy.COMMON_PASSWORDS:
            return "THAT PASSWORD IS TOO COMMON: CHOOSE ANOTHER"
        return None

    def register(self, name: str, password: str | None = None) -> tuple[Account, str] | Refused:
        """Make an account outside a login (the operator's ``create-account``; a future
        registration command or page), hashing here and now. Without ``password`` one is made up.
        Returns the account and its password, to be shown once and never logged."""
        name = name.strip().upper()
        password = password if password is not None else make_password()
        if self._store.get(name) is not None:
            return Refused("THAT NAME IS TAKEN")
        if (refusal := self.new_account_refusal(name, password)) is not None:
            return refusal
        account = self._new_hash(name, password)
        if not self._store.create(account):
            return Refused("THAT NAME IS TAKEN")
        return account, password

    def reset_password(self, name: str) -> str | Refused:
        """Give account ``name`` a new, made-up password and return it (shown once, never
        logged). A player logged in stays logged in."""
        name = name.strip().upper()
        password = make_password()
        if not self._store.set_password(self._new_hash(name, password)):
            return Refused("NO SUCH ACCOUNT")
        return password

    def _new_hash(self, name: str, password: str) -> Account:
        """Account ``name`` with ``password`` hashed here and now, the current way."""
        salt = os.urandom(policy.PASSWORD_SALT_BYTES)
        return Account(name, salt, hash_password(password, salt, self._kdf), self._kdf)

    def account_online(self, name: str) -> bool:
        """True if account ``name`` (upper case) is logged in."""
        return name in self._accounts

    def account_count(self) -> int:
        return self._store.count()

    def account_exists(self, name: str) -> bool:
        return self._store.get(name) is not None

    @property
    def registration_open(self) -> bool:
        return self._registration_open

    @property
    def max_accounts(self) -> int:
        return self._max_accounts

    def delete_account(self, name: str) -> bool:
        """Remove account ``name`` (upper case); its progress is the caller's to erase."""
        return self._store.delete(name)

    def prune_unplayed(self, before: float, has_progress: Callable[[str], bool]) -> list[str]:
        """Delete the accounts nobody has created or logged in to since ``before`` (wall-clock
        seconds) that hold no progress (``has_progress`` of their record key) and are not online,
        so names claimed and never played come free (``policy.UNPLAYED_ACCOUNT_DAYS``). Returns
        their names."""
        gone = []
        for name in self._store.stale(before):
            if name in self._accounts or has_progress(identity_key(name, name)):
                continue
            if self._store.delete(name):
                gone.append(name)
        return gone

    def _account(self, name: str, password: str, gate: LoginGate | None) -> Refused | PasswordCheck:
        if (refusal := self._refuse_account(name, password, gate)) is not None:
            return refusal
        result = self._check_or_register(name, password, gate)
        if isinstance(result, Refused) and gate is not None:
            gate.account_not_tried(name)  # refused before any hashing
        return result

    def _check_or_register(
        self, name: str, password: str, gate: LoginGate | None
    ) -> Refused | PasswordCheck:
        account = self._store.get(name)
        if account is not None:
            return PasswordCheck(name, password, account.salt, account.kdf, account)
        if not self._registration_open:
            return Refused("NO SUCH ACCOUNT: ASK THE OPERATOR FOR ONE")
        if (refusal := self.new_account_refusal(name, password)) is not None:
            return refusal
        if gate is not None and not gate.may_register():
            return Refused("TOO MANY NEW ACCOUNTS: TRY AGAIN LATER")
        salt = os.urandom(policy.PASSWORD_SALT_BYTES)
        return PasswordCheck(name, password, salt, self._kdf, None)

    def _refuse_account(self, name: str, password: str, gate: LoginGate | None) -> Refused | None:
        """The cheap reasons to turn an account login away, before any hashing; the gate last,
        because it spends an allowance."""
        if not password:
            return Refused("PASSWORD REQUIRED")
        if len(password) > policy.MAX_PASSWORD_LENGTH:
            return Refused("PASSWORD TOO LONG")
        if name in self._accounts:
            return Refused("THAT ACCOUNT IS ALREADY LOGGED IN")
        if gate is not None and not gate.may_try_account(name):
            return Refused("TOO MANY WRONG PASSWORDS FOR THAT ACCOUNT: WAIT A FEW MINUTES")
        if gate is not None and not gate.may_check_password():
            gate.account_not_tried(name)
            return Refused("TOO MANY LOGIN ATTEMPTS: WAIT A MOMENT")
        return None

    def _admit(self, name: str, account: str | None) -> Pilot | Refused:
        callsign = self._allocate_callsign(name, guest=account is None)
        if callsign is None:
            return Refused("TOO MANY PILOTS WITH THAT NAME ARE ONLINE")
        self._callsigns.add(callsign)
        if account is not None:
            self._accounts.add(account)
        return Pilot(callsign, account)

    def _allocate_callsign(self, name: str, *, guest: bool) -> str | None:
        """The name cut to 12 characters, or with its tail replaced by a number until free
        (II §3: ``JOINGENIE``, ``JOINGENIE2`` ... ``JOINGENIE999``). A guest also passes over
        every registered account's callsign, so an account always plays under its own name."""

        def free(callsign: str) -> bool:
            if callsign in self._callsigns:
                return False
            return not (
                guest and self._store.callsign_registered(callsign, policy.MAX_CALLSIGN_LENGTH)
            )

        base = name[: policy.MAX_CALLSIGN_LENGTH]
        if free(base):
            return base
        for n in range(policy.CALLSIGN_FIRST_SUFFIX, policy.CALLSIGN_LAST_SUFFIX + 1):
            suffix = str(n)
            candidate = base[: policy.MAX_CALLSIGN_LENGTH - len(suffix)] + suffix
            if free(candidate):
                return candidate
        return None
