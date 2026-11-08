# 12. A web front end runs in its own process and reaches the server over a service channel

## Context

A web site is wanted for registration, administration and showing what is happening in the game: how
many accounts there are, who is in which city, and later live and delayed maps. Live state lives only in
the server's memory, and accounts must pass `Identity`'s rules (ADR 10).

Serving HTTP from the game server would need a web framework (against ADR 1) or a hand-written HTTP stack
with sessions, forms, CSRF protection and email, all on the game's one loop (ADR 3), where a slow page
holds up every game. A deploy of the site would restart the server and empty the cities. A second process
that opens `opencs.db` itself would see the password hashes, and its writes would bypass the account
count and the online checks that `Identity` keeps in memory.

## Decision

The web front end is a separate process, as its own user, with its own database for what only it knows
(email addresses, web sessions, its audit log). It reaches the server through the service channel
(`opencs.service`), JSON lines over a Unix socket that `[service] socket` turns on. Who may connect is
decided by the socket file: mode 0660, so the web process's user must be in the server's group.

Each request goes through the same `Identity` and `Control` methods as a first login or the admin
channel. The web cannot do anything an operator cannot, and it never opens `opencs.db`. A connection opens
with a hello naming a protocol version, so the web process and the server can be upgraded apart.

The channel is on the game's loop, so everything it does there is bounded:

- connections, line length and the request rate per connection are limited;
- `register` and `reset-password` hash on the hasher threads, never the loop, under a server-wide allowance
  below the game logins' (`SERVICE_HASH_LIMIT`), so signups cannot crowd out players logging in;
- a request whose work on the loop takes over `SLOW_SERVICE_REQUEST_SECONDS` is logged;
- a snapshot carries callsigns, teams and counts, never addresses, account details or chat, and the web
  caches it, so public traffic does not reach the loop.

## Consequences

- The server's runtime dependencies stay as ADR 1 and ADR 13 have them. The web process may use any
  framework and is deployed, restarted and sandboxed on its own.
- A bug or a flood in the web process can reach the game only through bounded requests. A compromised web
  process can do what the operator can, but cannot read the password hashes.
- The Unix socket needs a platform that has one; on others the channel cannot be turned on.
- The protocol is a second interface to keep stable. A change a client could misread gets a new version
  number, and the server may speak several.
- The web's own records can fall out of step with the server's (an account deleted or pruned on the
  server). The `account` request lets the web check.
