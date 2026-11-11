# 13. Argon2id for passwords

Amends [ADR 1](0001-stdlib-only-at-runtime.md).

## Context

The game sends passwords in plain text and players reuse them, so a stolen database must be costly to guess from. The standard library offers PBKDF2, which costs a guesser only CPU time: a graphics card tries a 100 000-round PBKDF2-SHA256 hash orders of magnitude faster than the server makes one. A memory-hard hash makes every guess also hold memory, which is what graphics cards and custom hardware lack. The standard library has none; `hashlib.scrypt` depends on how Python's OpenSSL was built. Argon2id is the current recommendation (RFC 9106, OWASP).

## Decision

New password hashes are Argon2id, made with `argon2.low_level.hash_secret_raw` from `argon2-cffi`, the server's one runtime dependency. The cost is OWASP's minimum Argon2id setting, in `policy.ARGON2_*`: 19 MiB, 2 passes, 1 lane, a 16-byte salt and a 32-byte hash, about 15 ms on an Apple M3 Pro. The hash releases the GIL, so the hashing threads (`app.ThreadHasher`) keep working side by side and the session stays as it was: `PasswordCheck.hash()` returns raw bytes and `Identity.complete` compares them with `hmac.compare_digest`.

Each account keeps the `Kdf` its hash was made with (algorithm and parameters; schema version 2 adds the `kdf` column, `''` meaning PBKDF2 with the row's `iterations`). A right password on an account whose `Kdf` is not the current one is hashed again the current way on a hashing thread and stored. That rehash is not a guess and spends nothing from the login limits; if it fails it is logged and the login stands.

## Consequences

- Deployment needs `uv sync` (or pip) to fetch `argon2-cffi` and its `cffi` bindings. Wheels exist for Linux on x86-64 and arm64 and for macOS on Apple silicon; elsewhere, Intel Macs included, the bindings build from source with a C compiler.
- Two hashes at once hold about 38 MiB. The login limits bound how often they run.
- Old PBKDF2 accounts keep logging in and move to Argon2id at their next login. An account that never logs in again keeps its PBKDF2 hash unless `reset-password` replaces it.
- Raising the cost later is a change to `policy.py`: accounts move to it as they log in.
- A database opened by this server is at schema version 2, which an older server refuses.
- Everything else stays in the standard library (ADR 1). Another runtime dependency needs its own record.
