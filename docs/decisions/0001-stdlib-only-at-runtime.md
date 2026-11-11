# 1. Standard library at runtime, except password hashing

Amended by [ADR 13](0013-argon2id-for-passwords.md), which adds `argon2-cffi` for password hashes.

## Context

The server must be trivial to deploy anywhere. Python ships asyncio, sqlite3 and tomllib, which cover transport, persistence and configuration (DESIGN §2). Its password hashes cost a guesser only CPU time, so password hashing is the one exception (ADR 13).

## Decision

`pyproject.toml` declares one runtime dependency, `argon2-cffi`, for password hashing. Everything else the server does at runtime uses the standard library. Development tools (pytest, ruff, mypy) live in the uv dev dependency group only.

## Consequences

- Deployment is a Python 3.12 interpreter, the source tree and `uv sync` (which fetches `argon2-cffi`).
- We write small utilities ourselves instead of importing them.
- Adding another runtime dependency needs a new ADR amending this one.
