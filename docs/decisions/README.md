# Architecture decision records

| # | decision |
|---|---|
| [0001](0001-stdlib-only-at-runtime.md) | Standard library at runtime, except password hashing (amended by 0013) |
| [0002](0002-world-returns-events.md) | The world returns events and never performs I/O |
| [0003](0003-single-loop-no-locks.md) | One event loop; cities never await; no locks |
| [0004](0004-audiences-as-data.md) | Audiences as data, resolved by a router |
| [0005](0005-tags-at-send-boundary.md) | Tags assigned at the send boundary |
| [0006](0006-policy-centralized.md) | Policy values centralized in `policy.py` and config |
| [0007](0007-sqlite-behind-protocols.md) | SQLite for persistence, behind store protocols |
| [0008](0008-written-from-spec.md) | Written from the spec |
| [0009](0009-limits-per-connection.md) | Rate limits belong to the connection, not the phase |
| [0010](0010-registration-behind-identity.md) | Every way to make an account goes through `Identity` |
| [0011](0011-pace-writes-to-the-client.md) | Writes keep to what the client can read |
| [0012](0012-web-front-end-over-a-service-channel.md) | A web front end runs in its own process and reaches the server over a service channel |
| [0013](0013-argon2id-for-passwords.md) | Argon2id for passwords |
| [0014](0014-games-recorded-as-segments.md) | Games are recorded as append-only segments of wire payloads |
