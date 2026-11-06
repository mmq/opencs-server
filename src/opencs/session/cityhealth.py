"""What has gone wrong in each city since the server started (DESIGN §5, §6).

An exception inside a ``City`` is a bug. The call is dropped and the city keeps running, but what
the call changed before it raised stays changed, so the city may be inconsistent. It is marked here,
and ``cities.CityManager`` rebuilds it the next time its game is empty. The counts outlast the
rebuild, so the admin channel's ``status`` can show that it happened. Keyed by city letter.
"""

from __future__ import annotations

from collections import Counter


class CityHealth:
    """Failures per city, and which cities await a rebuild."""

    def __init__(self) -> None:
        self._failures: Counter[str] = Counter()
        self._rebuilds: Counter[str] = Counter()
        self._tainted: set[str] = set()

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

    def forget(self, letter: str) -> None:
        """The city is gone."""
        self._tainted.discard(letter)
        del self._failures[letter], self._rebuilds[letter]
