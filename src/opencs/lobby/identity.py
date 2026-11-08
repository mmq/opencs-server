"""Who a login line is: real accounts and ``JOINGENIE`` guests, and their callsigns.

II §3, I §5.1. ``Identity`` knows nothing of sockets or sessions: it
takes the account line and answers with a ``Pilot`` or a reason to refuse. It tracks who is online
so a second login is refused and callsigns stay unique server-wide.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import logging
import math
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from argon2.low_level import Type, hash_secret_raw

from opencs import policy
from opencs.lobby import commands
from opencs.store.base import ARGON2ID, PBKDF2_SHA256, Account, AccountStore, Kdf, StaleCursor

log = logging.getLogger("opencs.lobby")

#: What ``commands.parse`` makes of a line that is not a Chamber command: a callsign may be one of
#: these (``/ACE`` is an unknown command, so chat), never a command the Chamber would act on.
_NOT_COMMANDS = (commands.Say, commands.BareLetter, commands.Unknown)


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
    def needs_callsign(self) -> bool:
        """An account whose player has not chosen a callsign yet: ``callsign`` is ``""`` until
        the Chamber's prompt is answered (``Identity.choose_callsign``)."""
        return not self.callsign

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


@dataclass(frozen=True, slots=True)
class PruneBatch:
    """One batch of ``Identity.prune_unplayed``: the accounts it deleted, and where the next batch
    starts, None when the sweep is done."""

    gone: list[str]
    resume: StaleCursor | None


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


@dataclass(frozen=True, slots=True)
class NewPassword:
    """An account being made, or given a new password, outside a login (the operator's commands,
    the web registration), waiting for its hash as ``PasswordCheck`` does: ``hash`` may run on
    another thread, and its result goes to ``Identity.complete_password`` on the event loop.
    ``password`` is the one to show once; it is never logged."""

    name: str
    password: str = field(repr=False)
    salt: bytes = field(repr=False)
    kdf: Kdf
    new_account: bool
    #: A new account's callsign, if the web chose one; ``""`` leaves it to the Chamber's prompt.
    callsign: str = ""

    def hash(self) -> bytes:
        return hash_password(self.password, self.salt, self.kdf)


def _untypeable(password: str) -> str | None:
    """Why a password set outside a login could not be typed into the client, whose entry turns
    letters into capitals (``policy.NEW_PASSWORD_CHARACTERS``)."""
    if password != password.upper():
        return "A PASSWORD MAY HAVE NO LOWER CASE: THE GAME TYPES IT IN CAPITALS"
    return None


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
        wall: Callable[[], float] = lambda: 0.0,
    ) -> None:
        self._store = store
        self._guest_names = frozenset(name.upper() for name in guest_names)
        self._kdf = kdf
        self._registration_open = registration == "open"
        self._min_password_length = min_password_length
        self._max_accounts = max_accounts
        #: The wall clock (the server passes ``time.time``): when a callsign was chosen must
        #: outlive a restart. This layer does no I/O, so it is given one.
        self._wall = wall
        #: The callsigns online, upper-cased: two may not differ only in case.
        self._callsigns: set[str] = set()
        self._accounts: set[str] = set()
        #: How many accounts there are, kept here so a registration does not count the table. Every
        #: account is made and removed through this class (ADR 10).
        self._count = store.count()

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
        self._callsigns.discard(pilot.callsign.upper())
        if pilot.account is not None:
            self._accounts.discard(pilot.account)

    def callsign_online(self, callsign: str) -> bool:
        """True if ``callsign``, in any case, is in use."""
        return callsign.upper() in self._callsigns

    def _guest(self, name: str, gate: LoginGate | None) -> Pilot | Refused:
        # Compare callsigns, not names: ALPHAXYZW would otherwise be played as ALPHAXYZ's callsign.
        base = name[: policy.MAX_CALLSIGN_LENGTH]
        if name != policy.GUEST_ACCOUNT_NAME and (
            self._store.get(name) is not None or self._store.callsign_owner(base) is not None
        ):
            return Refused("THAT NAME IS A REGISTERED ACCOUNT: LOG IN WITH ITS PASSWORD")
        if gate is not None and not gate.may_admit_guest():
            return Refused("TOO MANY GUESTS ONLINE: TRY AGAIN LATER")
        return self._admit(base, None)

    def complete(
        self, check: PasswordCheck, digest: bytes, gate: LoginGate | None = None
    ) -> Pilot | Refused:
        """Finish an account login with ``check.hash()``. The world may have moved on while it
        ran: the account may have logged in elsewhere, or been registered by another login."""
        if check.stored is None:
            account = Account(check.name, check.salt, digest, check.kdf)
            if not self._create(account, ""):  # its callsign is asked for in the Chamber
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
        callsign = self.callsign_of(check.name)  # "" until chosen at the Chamber's prompt
        if callsign is None:  # deleted while the hash ran: it plays this once under its name
            callsign = check.name[: policy.MAX_CALLSIGN_LENGTH]
        return self._admit(callsign, check.name)

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

    def new_account_refusal(self, name: str, password: str | None) -> Refused | None:
        """Why ``name`` (upper case) with ``password`` cannot be a new account, None if it can:
        every rule a registration must pass, wherever it comes from (a first login, the
        operator). It does not look at the limits on how often. A password the server made up
        (None here) is not held to the rules for a chosen one: they guard against a player's poor
        choice, and ``min_password_length`` may be above its length."""
        if (
            reason := self._new_name_problem(name)
            or (None if password is None else self._new_password_problem(name, password))
        ) is not None:
            return Refused(reason)
        if self._count >= self._max_accounts:
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
        """Whether ``_allocate_callsign`` could give ``callsign`` (upper case) to a guest: a guest
        name cut to 12 characters, or with its tail replaced by a number (``JOINGENIE2``)."""
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
        if len(password) > policy.MAX_NEW_PASSWORD_LENGTH:
            return f"A PASSWORD MAY BE AT MOST {policy.MAX_NEW_PASSWORD_LENGTH} CHARACTERS"
        if any(ch not in policy.NEW_PASSWORD_CHARACTERS for ch in password):
            return "A PASSWORD MAY HOLD ONLY LETTERS, DIGITS AND PUNCTUATION, NO SPACES"
        shouted = password.upper()  # the client capitalises what it sends
        if shouted == name:
            return "A PASSWORD MUST NOT BE THE ACCOUNT NAME"
        if shouted in policy.COMMON_PASSWORDS:
            return "THAT PASSWORD IS TOO COMMON: CHOOSE ANOTHER"
        return None

    def register(
        self, name: str, password: str | None = None, callsign: str | None = None
    ) -> tuple[Account, str] | Refused:
        """``begin_register`` and ``complete_password`` with the hash made here and now, for
        callers that may block. Returns the account and its password."""
        begun = self.begin_register(name, password, callsign)
        if isinstance(begun, Refused):
            return begun
        return self.complete_password(begun, begun.hash())

    def reset_password(self, name: str, password: str | None = None) -> str | Refused:
        """``begin_reset_password`` and ``complete_password`` here and now, for callers that may
        block. Returns the new password."""
        begun = self.begin_reset_password(name, password)
        if isinstance(begun, Refused):
            return begun
        done = self.complete_password(begun, begun.hash())
        return done if isinstance(done, Refused) else done[1]

    def begin_register(
        self, name: str, password: str | None = None, callsign: str | None = None
    ) -> NewPassword | Refused:
        """Start making an account outside a login. Without ``password`` one is made up; without
        ``callsign`` the player is asked for one at the first login. The answer is the hash to
        run and hand to ``complete_password``, or why there is no account."""
        name = name.strip().upper()
        if callsign is not None and (reason := self._free_callsign_problem(callsign)):
            return Refused(reason)
        if password is not None and (reason := _untypeable(password)) is not None:
            return Refused(reason)
        if self._store.get(name) is not None:
            return Refused("THAT NAME IS TAKEN")
        if (refusal := self.new_account_refusal(name, password)) is not None:
            return refusal
        if password is None:
            password = make_password()
        salt = os.urandom(policy.PASSWORD_SALT_BYTES)
        return NewPassword(
            name, password, salt, self._kdf, new_account=True, callsign=callsign or ""
        )

    def begin_reset_password(self, name: str, password: str | None = None) -> NewPassword | Refused:
        """Start giving account ``name`` a new password: ``password``, which must pass the rules
        for a new account's, or a made-up one. A player logged in stays logged in."""
        name = name.strip().upper()
        if self._store.get(name) is None:
            return Refused("NO SUCH ACCOUNT")
        if password is None:
            password = make_password()
        elif (
            reason := _untypeable(password) or self._new_password_problem(name, password)
        ) is not None:
            return Refused(reason)
        salt = os.urandom(policy.PASSWORD_SALT_BYTES)
        return NewPassword(name, password, salt, self._kdf, new_account=False)

    def complete_password(self, new: NewPassword, digest: bytes) -> tuple[Account, str] | Refused:
        """Store ``new`` with its hash, ``new.hash()``: the account and its password, to be shown
        once. The world may have moved on while the hash ran: the name may have been taken, the
        server filled, or the account deleted."""
        account = Account(new.name, new.salt, digest, new.kdf)
        if new.new_account:
            if self._count >= self._max_accounts:
                return Refused("THIS SERVER HAS NO ROOM FOR NEW ACCOUNTS")
            if new.callsign and self._free_callsign_problem(new.callsign) is not None:
                return Refused("THAT CALLSIGN WAS JUST TAKEN")
            if not self._create(account, new.callsign):
                return Refused("THAT NAME IS TAKEN")
        elif not self._store.set_password(account):
            return Refused("NO SUCH ACCOUNT")
        return account, new.password

    # --- callsigns ---------------------------------------------------------------------------

    def callsign_of(self, name: str) -> str | None:
        """The callsign account ``name`` (upper case) plays under, None if there is no account."""
        found = self._store.callsign(name)
        return None if found is None else found.callsign

    def change_callsign(self, name: str, callsign: str, *, force: bool = False) -> str | Refused:
        """Give account ``name``, which must be offline, the callsign ``callsign``, in the case
        given: the web's or the operator's change. ``force`` (the operator) skips the wait between
        changes. The answer is the callsign, or why not."""
        name = name.strip().upper()
        if name in self._accounts:
            return Refused("THAT ACCOUNT IS ONLINE: IT CHOOSES ITS CALLSIGN WITH /CALLSIGN")
        if (reason := self._callsign_problem(name, callsign, force=force)) is not None:
            return Refused(reason)
        self._store.set_callsign(name, callsign, self._wall())
        return callsign

    def default_callsign(self, pilot: Pilot) -> str | None:
        """What an account that has not chosen a callsign may take by pressing Return: its name,
        if that is free and passes the rules, else None."""
        assert pilot.account is not None
        name = pilot.account[: policy.MAX_CALLSIGN_LENGTH]
        return None if self._free_callsign_problem(name) is not None else name

    def choose_callsign(self, pilot: Pilot, callsign: str | None) -> Pilot | Refused:
        """The Chamber's first-login prompt answered: ``callsign``, or with None the name
        (``default_callsign``). The answer is the pilot under it, or why not. A first choice does
        not start the wait between changes."""
        assert pilot.account is not None and pilot.needs_callsign
        if callsign is None:
            callsign = self.default_callsign(pilot)
            if callsign is None:
                return Refused("YOUR NAME IS TAKEN AS A CALLSIGN: TYPE ANOTHER")
        elif (reason := self._free_callsign_problem(callsign)) is not None:
            return Refused(reason)
        if not self._store.set_callsign(pilot.account, callsign, None):
            return Refused("NO SUCH ACCOUNT")
        self._callsigns.add(callsign.upper())
        return dataclasses.replace(pilot, callsign=callsign)

    def _free_callsign_problem(self, callsign: str) -> str | None:
        """Why ``callsign`` cannot be taken by an account that has none: the rules, or another's."""
        if (problem := self._new_callsign_problem(callsign)) is not None:
            return problem
        if self._store.callsign_owner(callsign) is not None or callsign.upper() in self._callsigns:
            return "THAT CALLSIGN IS TAKEN"
        return None

    def rename(self, pilot: Pilot, callsign: str) -> Pilot | Refused:
        """A logged-in account's ``/CALLSIGN``: the pilot under its new callsign, or why not.
        Whether it may change now (not in a game, nor with one held) is the caller's to know."""
        if pilot.account is None:
            return Refused("A GUEST CANNOT CHOOSE A CALLSIGN: LOG IN WITH AN ACCOUNT")
        if (reason := self._callsign_problem(pilot.account, callsign, force=False)) is not None:
            return Refused(reason)
        self._store.set_callsign(pilot.account, callsign, self._wall())
        self._callsigns.discard(pilot.callsign.upper())
        self._callsigns.add(callsign.upper())
        return dataclasses.replace(pilot, callsign=callsign)

    def _callsign_problem(self, name: str, callsign: str, *, force: bool) -> str | None:
        current = self._store.callsign(name)
        if current is None:
            return "NO SUCH ACCOUNT"
        if callsign == current.callsign:
            return f"YOUR CALLSIGN IS ALREADY {callsign}"
        if (problem := self._new_callsign_problem(callsign)) is not None:
            return problem
        own = callsign.upper() == current.callsign.upper()  # only its case changes
        if not own and (
            self._store.callsign_owner(callsign) is not None or callsign.upper() in self._callsigns
        ):
            return "THAT CALLSIGN IS TAKEN"
        if not force and current.changed is not None:
            wait = current.changed + policy.CALLSIGN_CHANGE_INTERVAL - self._wall()
            if wait > 0:
                days = math.ceil(wait / 86400)
                return f"YOU MAY CHOOSE A NEW CALLSIGN IN {days} DAY{'S' if days > 1 else ''}"
        return None

    def _new_callsign_problem(self, callsign: str) -> str | None:
        """Why ``callsign`` cannot be chosen whoever asks (``policy.CALLSIGN_CHARACTERS``)."""
        if not policy.MIN_CALLSIGN_LENGTH <= len(callsign) <= policy.MAX_CALLSIGN_LENGTH:
            return (
                f"A CALLSIGN MUST BE {policy.MIN_CALLSIGN_LENGTH} TO "
                f"{policy.MAX_CALLSIGN_LENGTH} CHARACTERS"
            )
        if any(ch not in policy.CALLSIGN_CHARACTERS for ch in callsign):
            return "A CALLSIGN MAY HOLD LETTERS, DIGITS AND PUNCTUATION, NO SPACES"
        if not any(ch.isalnum() for ch in callsign):
            return "A CALLSIGN NEEDS A LETTER OR A DIGIT"
        if not isinstance(commands.parse(callsign), _NOT_COMMANDS):
            return "THAT CALLSIGN READS AS A CHAMBER COMMAND"  # /PLAY, or a button's ACE/CITY
        folded = callsign.upper()
        if folded in policy.RESERVED_ACCOUNT_NAMES or self._a_guest_could_be(folded):
            return "THAT CALLSIGN IS RESERVED"
        return None

    def account_online(self, name: str) -> bool:
        """True if account ``name`` (upper case) is logged in."""
        return name in self._accounts

    def account_count(self) -> int:
        return self._count

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
        if not self._store.delete(name):
            return False
        self._count -= 1
        return True

    def prune_unplayed(
        self,
        before: float,
        has_progress: Callable[[str], bool],
        *,
        after: StaleCursor | None = None,
        limit: int = policy.ACCOUNT_PRUNE_BATCH,
    ) -> PruneBatch:
        """Delete the accounts nobody has created or logged in to since ``before`` (wall-clock
        seconds) that hold no progress (``has_progress`` of their record key) and are not online,
        so names claimed and never played come free (``policy.UNPLAYED_ACCOUNT_DAYS``). One batch:
        at most ``limit`` stale accounts past ``after`` are looked at, and the deletions are one
        write. Pass the batch's ``resume`` as ``after`` for the next."""
        stale = self._store.stale(before, after=after, limit=limit)
        doomed = [
            name
            for _, name in stale
            if name not in self._accounts and not has_progress(identity_key(name, name))
        ]
        gone = self._store.delete_many(doomed) if doomed else []
        self._count -= len(gone)
        return PruneBatch(gone, stale[-1][0] if len(stale) == limit else None)

    def _create(self, account: Account, callsign: str) -> bool:
        if not self._store.create(account, callsign):
            return False
        self._count += 1
        return True

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

    def _admit(self, base: str, account: str | None) -> Pilot | Refused:
        if account is not None and not base:
            self._accounts.add(account)
            return Pilot("", account)  # takes a callsign at the Chamber's prompt
        callsign = self._allocate_callsign(base, guest=account is None)
        if callsign is None:
            return Refused("TOO MANY PILOTS WITH THAT NAME ARE ONLINE")
        self._callsigns.add(callsign.upper())
        if account is not None:
            self._accounts.add(account)
        return Pilot(callsign, account)

    def _allocate_callsign(self, base: str, *, guest: bool) -> str | None:
        """``base`` (at most 12 characters: a guest name cut, or an account's callsign), or with its
        tail replaced by a number until free (II §3: ``JOINGENIE``, ``JOINGENIE2`` ...
        ``JOINGENIE999``), ignoring case. A guest also passes over every account's callsign, so an
        account always plays under its own; an account needs a number only if two made before
        names were cut to 12 share one."""

        def free(callsign: str) -> bool:
            if callsign.upper() in self._callsigns:
                return False
            return not (guest and self._store.callsign_owner(callsign) is not None)

        if free(base):
            return base
        for n in range(policy.CALLSIGN_FIRST_SUFFIX, policy.CALLSIGN_LAST_SUFFIX + 1):
            suffix = str(n)
            candidate = base[: policy.MAX_CALLSIGN_LENGTH - len(suffix)] + suffix
            if free(candidate):
                return candidate
        return None
