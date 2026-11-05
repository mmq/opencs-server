"""What one connection sent: totals, and the busiest stretches, for the log at close (DESIGN §6).

The real client reads through an emulated modem, about 960 bytes a second at 9600 baud, and handles
about 50 frames a second (I §3). These figures show how close real games come to either.
"""

from __future__ import annotations

from opencs import policy


class _Window:
    """The most bytes (and frames) sent in any one fixed stretch of ``seconds``."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._index = 0
        self._bytes = 0
        self._frames = 0
        self.peak_bytes = 0
        self.peak_frames = 0

    def add(self, elapsed: float, n_bytes: int, frames: int) -> None:
        index = int(elapsed // self.seconds)
        if index != self._index:
            self._index, self._bytes, self._frames = index, 0, 0
        self._bytes += n_bytes
        self._frames += frames
        self.peak_bytes = max(self.peak_bytes, self._bytes)
        self.peak_frames = max(self.peak_frames, self._frames)


class Traffic:
    """Counts what a connection writes. ``frame`` is a game message, not raw Chamber text."""

    def __init__(
        self, start: float, windows: tuple[float, ...] = policy.TRAFFIC_PEAK_WINDOWS
    ) -> None:
        self._start = start
        self._windows = [_Window(s) for s in windows]
        self.bytes = 0
        self.frames = 0
        #: Queued messages a newer one replaced before they were written (``Connection._enqueue``).
        self.replaced = 0
        #: The most items ever waiting in the outbox.
        self.peak_outbox = 0
        #: Times the write pump waited for the client's acks, and gave up waiting.
        self.ack_waits = 0
        self.ack_stalls = 0
        self.peak_unacked = 0
        self.worst_ack_delay = 0.0

    def sent(self, n_bytes: int, now: float, *, frame: bool) -> None:
        self.bytes += n_bytes
        self.frames += frame
        for window in self._windows:
            window.add(now - self._start, n_bytes, int(frame))

    def note_window(self, peak_unacked: int, worst_ack_delay: float) -> None:
        self.peak_unacked = peak_unacked
        self.worst_ack_delay = worst_ack_delay

    def summary(self, now: float) -> str:
        """``sent 4800 bytes, 210 frames in 60 s: avg 80 B/s; peak 410 B/s, 18.0 frames/s (1 s);
        ...; peak outbox 12, 30 replaced; peak unacked 900 bytes, worst ack 1.2 s, 0 waits, 0
        stalls``. Each rate's peak is the busiest fixed stretch of that length, averaged over it. A
        high outbox or many replaced means the client was over its rate: it got fewer positions, not
        later ones. Waits mean the game was reading slower than the rate; stalls that its acks
        stopped altogether for a while."""
        elapsed = now - self._start
        peaks = "; ".join(
            f"peak {w.peak_bytes / w.seconds:.0f} B/s, {w.peak_frames / w.seconds:.1f} frames/s"
            f" ({w.seconds:g} s)"
            for w in self._windows
        )
        return (
            f"sent {self.bytes} bytes, {self.frames} frames in {elapsed:.0f} s:"
            f" avg {self.bytes / max(elapsed, 1.0):.0f} B/s; {peaks};"
            f" peak outbox {self.peak_outbox}, {self.replaced} replaced;"
            f" peak unacked {self.peak_unacked} bytes, worst ack {self.worst_ack_delay:.1f} s,"
            f" {self.ack_waits} waits, {self.ack_stalls} stalls"
        )
