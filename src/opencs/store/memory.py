"""In-memory stores, for tests and for a server run without a data directory (DESIGN §3)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import replace

from opencs import policy
from opencs.store.base import Account, Callsign, ProgressRecord, StaleCursor, Stores


class MemoryAccountStore:
    """``AccountStore`` backed by a dict; forgets everything at exit. ``now`` stamps logins."""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._accounts: dict[str, Account] = {}
        self._seen: dict[str, float] = {}  # created or last logged in to
        self._callsigns: dict[str, Callsign] = {}
        self._owners: dict[str, str] = {}  # upper-cased callsign -> account name
        self._now = now

    def get(self, name: str) -> Account | None:
        return self._accounts.get(name)

    def create(self, account: Account, callsign: str | None = None) -> bool:
        if account.name in self._accounts:
            return False
        self._accounts[account.name] = account
        self._seen[account.name] = self._now()
        if callsign is None:
            callsign = account.name[: policy.MAX_CALLSIGN_LENGTH]
        self._callsigns[account.name] = Callsign(callsign, None)
        if callsign:
            self._owners.setdefault(callsign.upper(), account.name)
        return True

    def callsign(self, name: str) -> Callsign | None:
        return self._callsigns.get(name)

    def callsign_owner(self, callsign: str) -> str | None:
        return self._owners.get(callsign.upper()) if callsign else None

    def set_callsign(self, name: str, callsign: str, changed: float | None) -> bool:
        if name not in self._accounts:
            return False
        self._disown(name)
        self._callsigns[name] = Callsign(callsign, changed)
        self._owners.setdefault(callsign.upper(), name)
        return True

    def _disown(self, name: str) -> None:
        old = self._callsigns.get(name)
        if old is not None and self._owners.get(old.callsign.upper()) == name:
            del self._owners[old.callsign.upper()]

    def count(self) -> int:
        return len(self._accounts)

    def touch_login(self, name: str) -> None:
        if name in self._accounts:
            self._seen[name] = self._now()

    def stale(
        self, before: float, *, after: StaleCursor | None = None, limit: int | None = None
    ) -> list[tuple[StaleCursor, str]]:
        found = sorted(
            ((seen, name), name)
            for name, seen in self._seen.items()
            if seen < before and (after is None or (seen, name) > after)
        )
        return found if limit is None else found[:limit]

    def set_password(self, account: Account) -> bool:
        if account.name not in self._accounts:
            return False
        self._accounts[account.name] = account
        return True  # the callsign is kept apart, so a new password leaves it as it was

    def delete(self, name: str) -> bool:
        self._seen.pop(name, None)
        self._disown(name)
        self._callsigns.pop(name, None)
        return self._accounts.pop(name, None) is not None

    def delete_many(self, names: Iterable[str]) -> list[str]:
        return [name for name in names if self.delete(name)]


class MemoryProgressStore:
    """``ProgressStore`` backed by a dict. ``now`` stamps ``updated`` (the wall clock by default;
    a test passes its own)."""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._records: dict[str, ProgressRecord] = {}
        self._now = now

    def load(self, identity: str) -> ProgressRecord | None:
        return self._records.get(identity)

    def save(self, identity: str, record: ProgressRecord) -> None:
        self._records[identity] = replace(record, updated=self._now())

    def erase(self, identity: str) -> bool:
        return self._records.pop(identity, None) is not None


def memory_stores() -> Stores:
    """A fresh set of in-memory stores."""
    return Stores(MemoryAccountStore(), MemoryProgressStore())
