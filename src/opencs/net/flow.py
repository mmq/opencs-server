"""How much a client has been sent and not yet shown it has read (DESIGN §3.3, ADR 11).

CS.COM reads into a 4096-byte ring with no flow control: when the game falls behind, the ring fills
and further bytes are lost, and what is left can splice into frames that pass the 7-bit checksum.
How fast the game reads depends on what it is drawing, so no fixed rate is safe. But it echoes the
tag of every uppercase frame once it has handled it (``client.Acks``), so a tag acknowledged means
everything written up to that frame has left the ring. This counts what has not.
"""

from __future__ import annotations

from collections import deque


class AckWindow:
    """Bytes written since the last frame the client acknowledged. Pure: the connection decides what
    to do when it is full."""

    #: Outstanding tags kept: below the 240 tags in use, so a wrapped tag cannot match a stale one.
    MAX_OUTSTANDING = 200

    def __init__(self, limit: int, probe_every: int) -> None:
        self.limit = limit
        self._probe_every = probe_every
        self._written = 0
        self._confirmed = 0
        self._since_tag = 0
        self._outstanding: deque[tuple[int, int, float]] = deque(maxlen=self.MAX_OUTSTANDING)
        self.peak = 0
        self.worst_delay = 0.0

    @property
    def unacked(self) -> int:
        return self._written - self._confirmed

    @property
    def full(self) -> bool:
        return self.unacked >= self.limit

    @property
    def wants_probe(self) -> bool:
        """True after so many bytes with no tag that nothing would be acknowledged for a while."""
        return self._since_tag >= self._probe_every

    def wrote(self, n_bytes: int, tag: int | None, now: float) -> None:
        self._written += n_bytes
        self._since_tag += n_bytes
        self.peak = max(self.peak, self.unacked)
        if tag is not None:
            self._outstanding.append((tag, self._written, now))
            self._since_tag = 0

    def acked(self, tags: bytes, now: float) -> bool:
        """The client has handled these tags. A later tag vouches for the earlier ones, whose frames
        may have been lost: they will not be acknowledged. True if anything was confirmed."""
        progress = False
        for tag in tags:
            found = next((i for i, entry in enumerate(self._outstanding) if entry[0] == tag), None)
            if found is None:
                continue  # never sent, or vouched for already
            for _ in range(found):
                self._outstanding.popleft()
            _, written, sent_at = self._outstanding.popleft()
            self.worst_delay = max(self.worst_delay, now - sent_at)
            self._confirmed = max(self._confirmed, written)
            progress = True
        return progress

    def release(self) -> None:
        """Nothing has come back for too long: assume what was sent has been read, since the frame
        whose ack it waits for may be the one that was lost, and waiting for it would never end."""
        self._confirmed = self._written

    def reset(self) -> None:
        """The stream is not game frames, which are all that are acknowledged."""
        self._confirmed = self._written
        self._since_tag = 0
        self._outstanding.clear()
