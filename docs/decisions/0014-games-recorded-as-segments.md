# 14. Games are recorded as append-only segments of wire payloads

## Context

The web front end (ADR 12) is to show maps and replays of games, live and past, and administrators want
to look back at what happened in a city. Live state lives only in the server's memory, so something on the
server has to write games down as they are played.

The recording must never cost the players anything. Everything shares one loop (ADR 3): a write that
waits on a slow or full disk would hold up every game. A crash or a power cut must leave what was written
readable. The game's traffic is mostly copies: each pod report and shot is relayed to every other client,
and the `'B'` load repeats are 91% of the server's bytes in the captures. Recordings hold callsigns and
chat, so who may see them, and how long they stay, matters.

## Decision

A city is recorded in segment files, about five minutes each (`RECORDING_ROTATE_SECONDS`), written by
`opencs.recording`. The format, version 1:

```
segment: magic "\x89OCSREC\n", varint format version (1), varint header length, header (JSON)
record:  varint ms since the previous record, kind byte, varint source, varint payload length, payload
```

Varints are unsigned LEB128. The header holds the server version, the city letter, the start time (UTC,
the only wall time in the file), the city's configuration and the player table (id, callsign, team, bot)
at the start. A source is 0 for the server or everyone, else a player id plus one. The kinds:

| kind | payload |
|---|---|
| 1 CLIENT | a client message as it reached the city (type letter and body); source is the sender |
| 2 SERVER | a server message, once, with the tag `BYTE_MIN`; source is its one recipient, or 0 |
| 3 PLAYER | a join (JSON: callsign, team, bot) or a leave (empty); source is the player |
| 4 KEYFRAME | JSON state: the reason, each player's score, multiplier, bay points, bays and marker, the multiplier floor |
| 5 SNAPSHOT | a server message that recreates part of the city, after a KEYFRAME |
| 6 KEYFRAME_END | empty |
| 7 END | JSON reason: `empty`, `rotate`, `full` or `shutdown` |

A payload is a wire payload without its frame header or terminator, so `opencs.messages` decodes it; the
client fixes the protocol, so it stays decodable. A keyframe is what a late joiner is sent (territory,
weather, every live entity's creating messages), built without marking anything as told to a client. Each
segment starts with one, and a `/RESUME` adds one, so playback can seek.

What is left out, because it can be worked out again or says nothing about the game:

- relayed copies of a client's message (a pod's `'b'`, a shot, a grenade, player chat): the client's own
  message is the record. The world marks them (`Event.relayed`);
- the per-recipient copies of one event, folded into one record for everyone;
- acks (`'d'`), pings, flags and echo replies, which never reach the city;
- the `'B'` load and its repeats, and everything from the logon: a join is a PLAYER record.

The server does not compress. Compression is the archiver's job, off the game's machine time, and its
method was chosen by measuring real recordings (ADR 15); raw records keep the writer simple and a cut
file readable.

**Crash safety.** Records are only appended and each is length-prefixed. A reader takes every whole record
and stops at the first incomplete one or at a zero byte where a kind should be (a zero-filled tail after a
power cut). A segment is complete if it has an END.

**Files.** A segment is `<letter>-<UTC time>.ocsrec.part` while open and `.ocsrec` once finished; a
`.part` left by a crash is renamed at the next start. A segment is made mode 0640 whatever the umask,
so the archiver, which runs as another user in the server's group, can read it; the directory's mode
(2770 for that) decides whether the group reaches it. The archiver deletes a segment once it has stored
it (it may instead rename it `.ocsrec.done`). The directory has a size cap and the filesystem a free-space floor; to make room
the server removes only the oldest `.done` files, so a recording not yet archived is never deleted. With
no room a city's next segment is skipped, and recording carries on when there is room again.

**Failure.** The loop only appends to a bounded queue; one writer thread writes and flushes about twice a
second. A full queue, a write error or a bug in the recorder's hooks is logged once and stops recording
for the rest of the run. Nothing the recorder does raises into the game.

**What is recorded.** `[recording] when`, globally or per city: `humans` (the default) records a city
while a pilot who is not a bot is in it, `any` while any pilot is, `off` never. Bots are guests whose
callsign is in `[recording] bots`, until accounts carry a bot flag. An empty city writes nothing.
`[recording] chat`: `all` (the default, team chat included), `public` or `none`; system lines are always
kept. Recorded chat is for administrators only; a public replay shows none.

## Consequences

- Off unless `[recording] dir` is set. With it off, the router and the sessions do nothing extra.
- The recorder is a second reader of the world's events. A new server message needs no recorder change;
  a new relayed copy must be marked `relayed`, or it is recorded twice.
- A format change gets a new version number, and readers refuse a version they do not know.
- The archiver (`opencs-web archive`, ADR 15) deletes each segment once its archive transaction has
  committed, rather than marking it `.done`: the archive holds it byte for byte, and a raw copy would
  keep callsigns out of reach of an account's deletion. Without the archiver, the operator moves or
  deletes segments by hand, since the server never deletes an unmarked one.
- Callsigns are in the header, in PLAYER records and in payloads (`'G'` and others). Deleting an account
  leaves them: a recording is a record of the game, and the callsign stays in it. Taking a callsign
  out is an operator's tool, used on request (`opencs-web forget_callsign`); it works on the archive,
  since raw segments are short-lived and rewriting them on the server would mean decoding every payload.
- Recordings hold chat, team chat included by default. Whether and how to tell players is the
  operator's decision.
