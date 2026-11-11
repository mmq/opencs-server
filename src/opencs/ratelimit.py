"""Token buckets on a caller's clock, alone or one per source (DESIGN §5).

Shared by the layers that meter input: the listener (connections per address), the sessions
(lines, frames, chat) and the admin channel.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable

from opencs import policy


class TokenBucket:
    """Allows bursts of up to ``capacity`` events and ``refill_per_second`` sustained.

    It starts full. Time comes from the caller, so tests drive it with a ``ManualClock``.
    """

    def __init__(self, capacity: int, refill_per_second: float, now: float) -> None:
        self._capacity = float(capacity)
        self._rate = refill_per_second
        self._tokens = float(capacity)
        self._at = now

    def is_full(self, now: float) -> bool:
        """True if the bucket has refilled completely: it remembers nothing worth keeping."""
        return self._tokens + max(0.0, now - self._at) * self._rate >= self._capacity

    def has_token(self, now: float) -> bool:
        """True if a ``take`` at ``now`` would succeed; spends nothing."""
        return self._tokens + max(0.0, now - self._at) * self._rate >= 1.0

    def take(self, now: float) -> bool:
        """Spend one token at ``now``; False, spending nothing, if there is none."""
        self._tokens = min(self._capacity, self._tokens + max(0.0, now - self._at) * self._rate)
        self._at = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


class KeyedBuckets[K: Hashable]:
    """One ``TokenBucket`` per key (an address, say), created on first use.

    The table is bounded: past ``policy.MAX_TRACKED_SOURCES`` keys the buckets that have refilled
    are dropped (they hold no history), and if that is not enough the oldest keys go, so a flood
    from many addresses cannot grow it without limit (DESIGN §5).
    """

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self._capacity = capacity
        self._rate = refill_per_second
        self._buckets: dict[K, TokenBucket] = {}

    def take(self, key: K, now: float) -> bool:
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= policy.MAX_TRACKED_SOURCES:
                self._prune(now)
            bucket = self._buckets[key] = TokenBucket(self._capacity, self._rate, now)
        return bucket.take(now)

    def has_token(self, key: K, now: float) -> bool:
        """True if a ``take`` for ``key`` at ``now`` would succeed; spends nothing and remembers
        nothing (a key never seen has a full bucket)."""
        bucket = self._buckets.get(key)
        return bucket is None or bucket.has_token(now)

    def forget(self, key: K) -> None:
        """Drop ``key``'s bucket: it starts full again."""
        self._buckets.pop(key, None)

    def forget_if(self, doomed: Callable[[K], bool]) -> None:
        """Drop the bucket of every key ``doomed`` picks."""
        for key in [k for k in self._buckets if doomed(k)]:
            del self._buckets[key]

    def __len__(self) -> int:
        return len(self._buckets)

    def _prune(self, now: float) -> None:
        for key in [k for k, b in self._buckets.items() if b.is_full(now)]:
            del self._buckets[key]
        while len(self._buckets) >= policy.MAX_TRACKED_SOURCES:
            del self._buckets[next(iter(self._buckets))]
