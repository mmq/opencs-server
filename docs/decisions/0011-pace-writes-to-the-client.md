# 11. Writes keep to what the client can read

## Context

The real client reads through an emulated modem. DOSBox-X delivers exactly what the program set on the UART, 960 bytes a second at 9600 baud, the most the game's menu offers. Everything the server sends faster than that waits somewhere. On loopback the kernels' socket buffers took over 500 KB, about ten minutes at that rate, without ever pushing back, and a smaller transport high-water mark or `SO_SNDBUF` barely changed it. So the outbox stayed empty, `backlog()` and `DROPPABLE_WATERMARK` saw nothing, and a player over budget got positions later and later with no way for the server to act.

A live test then showed the rate is not enough. CS.COM reads into a 4096-byte ring with no flow control, and how fast the game empties it depends on what it is drawing. At 40 frames/s and 836 B/s, inside the rate, a client watching from a distance held a steady delay of about a second for minutes. Moved down among 15 pods it fell 7 s behind, its overflow flag and peak (`0FFF`) were set, tagged frames were lost, and it crashed with `-3869`. The server sent only valid shots, and its outbox stayed short, because the backlog was inside the client.

## Decision

The write pump keeps each connection to `policy.CLIENT_BYTES_PER_SECOND` (`[limits] client_bytes_per_second`), less a small burst, so the backlog stays in the outbox. There, a newer `'b'` for a pod replaces the queued one where it sits. Only `'b'` is replaced: tagged messages are acknowledged, and a shot is an event. A queued `Introduce`, `Delete`, `Destroy` or `ReleaseDrop` for a pod ends replacement for that pod, so a report never jumps ahead of a change in the pod's life.

In a game the pump also stops when `policy.CLIENT_UNACKED_BYTES` (`[limits] client_unacked_bytes`) have been written and not acknowledged (`net.flow.AckWindow`). The client echoes each uppercase frame's tag once it has handled it, so a tag acknowledged means all before it has left the ring. Tags are sparse, so after `ACK_PROBE_BYTES` with none the pump adds an echo probe. A later tag vouches for an earlier one, whose frame may have been lost, and acks that stop for `ACK_STALL_SECONDS` end the wait, because the ack awaited may be for that lost frame.

While it waits, reports replace each other but shots would pile up, so the router sheds another player's relayed shot once a client is behind: past `SHOTS_SHED_FAR_BACKLOG` items if it began beyond `SHOT_KEEP_FEET`, past `SHOTS_SHED_ALL_BACKLOG` items whatever its distance. The client that fired a shot is the one that reports its hit, so a shed shot costs its sight, not its damage. Server-made shots are not shed.

## Consequences

- Under load a receiver gets fewer, fresher positions, and the outbox limit is reached only by traffic that cannot be replaced.
- The window adapts to what the game can read, including a slower baud setting; the rate alone would not. A client faster than 9600 is still held to the rate.
- A client that never acknowledges gets a window every `ACK_STALL_SECONDS`. Only a client that is not the real one behaves so.
- A player far behind sees fewer shots. In a fight near them that costs some of what they can see; the alternative is a client that crashes.
- Replacing reports does not consider distance: near pods are thinned as much as far ones. Thinning them by distance is a separate, later choice, made from what the close summary shows in real games.
- `send_first` overtakes the outbox but not the window, so the client's latency meter shows a game that is behind but not all of the time spent queued.
- A close is not paced, or a long backlog would miss `CLOSE_FLUSH_TIMEOUT`.
