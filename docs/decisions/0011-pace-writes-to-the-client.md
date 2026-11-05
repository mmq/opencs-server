# 11. Writes are paced to the client's line rate

## Context

The real client reads through an emulated modem. DOSBox-X delivers exactly what the program set on the UART, 960 bytes a second at 9600 baud, the most the game's menu offers. Everything the server sends faster than that waits somewhere. On loopback the kernels' socket buffers took over 500 KB, about ten minutes at that rate, without ever pushing back, and a smaller transport high-water mark or `SO_SNDBUF` barely changed it. So the outbox stayed empty, `backlog()` and `DROPPABLE_WATERMARK` saw nothing, and a player over budget got positions later and later with no way for the server to act.

## Decision

The write pump keeps each connection to `policy.CLIENT_BYTES_PER_SECOND` (`[limits] client_bytes_per_second`), less a small burst, so the backlog stays in the outbox. There, a newer `'b'` for a pod replaces the queued one where it sits. Only `'b'` is replaced: tagged messages are acknowledged, and a shot is an event. A queued `Introduce`, `Delete`, `Destroy` or `ReleaseDrop` for a pod ends replacement for that pod, so a report never jumps ahead of a change in the pod's life.

## Consequences

- Under load a receiver gets fewer, fresher positions, and the outbox limit is reached only by traffic that cannot be replaced.
- The rate is fixed, not measured. A client on a slower setting still builds a hidden backlog, and a faster one is held to the rate. The client echoes every tag in `'d'`, so a measured rate is possible later without any configuration.
- Replacement does not consider distance: near pods are thinned as much as far ones. Thinning by distance is a separate, later choice, made from what the close summary shows in real games.
- `send_first` overtakes the outbox, so the client's latency meter does not show time spent queued there.
- A close is not paced, or a long backlog would miss `CLOSE_FLUSH_TIMEOUT`.
