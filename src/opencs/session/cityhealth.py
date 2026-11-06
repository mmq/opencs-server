"""What has gone wrong in each city since the server started: failures and slow calls (DESIGN §5,
§6).

An exception inside a ``City`` is a bug. The call is dropped and the city keeps running, but what
the call changed before it raised stays changed, so the city may be inconsistent. It is marked here,
and ``cities.CityManager`` rebuilds it the next time its game is empty. The counts outlast the
rebuild, so the admin channel's ``status`` can show that it happened. Keyed by city letter.
"""

from __future__ import annotations

import logging
from collections import Counter

from opencs import policy

log = logging.getLogger("opencs.session")


class CityHealth:
    """Failures per city, which cities await a rebuild, and which calls were slow."""

    def __init__(self) -> None:
        self._failures: Counter[str] = Counter()
        self._rebuilds: Counter[str] = Counter()
        self._tainted: set[str] = set()
        self._slow: Counter[str] = Counter()
        self._worst: dict[str, float] = {}
        self._unlogged: Counter[str] = Counter()
        self._next_warning: dict[str, float] = {}

    def failed(self, letter: str) -> None:
        """A call into the city raised."""
        self._failures[letter] += 1
        self._tainted.add(letter)

    def failures(self, letter: str) -> int:
        return self._failures[letter]

    def rebuilds(self, letter: str) -> int:
        return self._rebuilds[letter]

    def tainted(self, letter: str) -> bool:
        """True if the city has failed since it was last built."""
        return letter in self._tainted

    def replaced(self, letter: str) -> None:
        """The city was built anew, for whatever reason; any failure died with the old one."""
        if letter in self._tainted:
            self._tainted.discard(letter)
            self._rebuilds[letter] += 1

    def called(self, letter: str, what: str, started: float, ended: float) -> None:
        """A call into the city ran from ``started`` to ``ended``; note it if that was slow."""
        took = ended - started
        if took < policy.SLOW_CITY_CALL_SECONDS:
            return
        self._slow[letter] += 1
        self._worst[letter] = max(took, self._worst.get(letter, 0.0))
        self._unlogged[letter] += 1
        if ended < self._next_warning.get(letter, ended):
            return
        log.warning(
            "city %s: %s took %.0f ms; %d slow since the last warning",
            letter,
            what,
            took * 1000,
            self._unlogged[letter],
        )
        self._unlogged[letter] = 0
        self._next_warning[letter] = ended + policy.SLOW_CITY_CALL_LOG_SECONDS

    def slow_calls(self, letter: str) -> int:
        return self._slow[letter]

    def worst_call(self, letter: str) -> float:
        """The slowest call noted, in seconds."""
        return self._worst.get(letter, 0.0)

    def forget(self, letter: str) -> None:
        """The city is gone."""
        self._tainted.discard(letter)
        self._worst.pop(letter, None)
        self._next_warning.pop(letter, None)
        for counts in (self._failures, self._rebuilds, self._slow, self._unlogged):
            del counts[letter]
