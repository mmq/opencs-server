"""The SQLite store: accounts and progress in one file (DESIGN §2, ADR 7).

One connection, used only from the event loop thread (``check_same_thread`` stays on, so a stray
thread fails loudly). A save is a single small row in its own transaction. The file is in WAL mode
with ``synchronous=NORMAL``: a commit is written to the log without waiting for the disk, and the
log is synced at each checkpoint, so a save costs no fsync on the event loop. The database cannot
be corrupted that way; a power cut or an operating system crash can lose the last few commits (a
process crash loses none), which for this data means a player's last few seconds of progress or a
just-set password. A server that grew to feel the writes themselves would move saves to a thread,
which the protocols allow but nothing needs yet.

The schema is versioned in a ``schema_version`` table and only ever moves forward: ``MIGRATIONS``
is an append-only list, the n-th entry taking a database from version n-1 to n. A database from a
newer server is refused rather than guessed at.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from opencs import policy
from opencs.store.base import PBKDF2_SHA256, Account, Kdf, ProgressRecord, StaleCursor, Stores

log = logging.getLogger("opencs.store")

#: Append-only. Never edit an entry that has shipped: add a new one.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE accounts (
            name TEXT PRIMARY KEY,
            salt BLOB NOT NULL,
            password_hash BLOB NOT NULL,
            iterations INTEGER NOT NULL,
            created REAL NOT NULL,
            last_login REAL  -- NULL until the first login; lets an unplayed account be pruned
        )""",
        """CREATE TABLE progress (
            identity TEXT PRIMARY KEY,
            bays TEXT NOT NULL,
            marker INTEGER NOT NULL,
            bay_points INTEGER NOT NULL,
            score INTEGER NOT NULL,
            career INTEGER NOT NULL,
            updated REAL NOT NULL
        )""",
    ),
    # 2: how each password hash was made. '' is PBKDF2-HMAC-SHA256 with the row's ``iterations``,
    # which is every row made before this column; any other value is ``_kdf_columns``'s.
    ("ALTER TABLE accounts ADD COLUMN kdf TEXT NOT NULL DEFAULT ''",),
    # 3: indexes for the callsign lookup and the prune.
    (
        # A guest login looks up its callsign among the accounts' names cut to 12 characters
        # (policy.MAX_CALLSIGN_LENGTH); without this it scans the table.
        "CREATE INDEX accounts_callsign ON accounts (substr(name, 1, 12))",
        # The prune sweeps the stale accounts in batches, oldest first.
        "CREATE INDEX accounts_seen ON accounts (coalesce(last_login, created), name)",
    ),
)

#: The length ``accounts_callsign`` cuts names to. A query must spell its expression the same way
#: to use it.
_CALLSIGN_INDEX_LENGTH = 12


class SchemaError(Exception):
    """The database cannot be used by this server."""


_PBKDF2_HASH_BYTES = 32  # the length every PBKDF2 row was made with (``hashlib``'s SHA-256)


def _kdf_columns(kdf: Kdf) -> tuple[str, int]:
    """``kdf`` as the ``kdf`` and ``iterations`` columns. PBKDF2 keeps the form rows had before
    the ``kdf`` column; anything else is ``ALGORITHM:m=..,t=..,p=..,l=..`` with no iterations."""
    if kdf.algorithm == PBKDF2_SHA256 and kdf.hash_bytes == _PBKDF2_HASH_BYTES:
        return "", kdf.time_cost
    text = (
        f"{kdf.algorithm}:m={kdf.memory_kib},t={kdf.time_cost},"
        f"p={kdf.parallelism},l={kdf.hash_bytes}"
    )
    return text, 0


def _kdf_from(text: str, iterations: int) -> Kdf:
    """The ``Kdf`` of a row from its ``kdf`` and ``iterations`` columns (``_kdf_columns``)."""
    if not text:
        return Kdf.pbkdf2(iterations)
    algorithm, _, params = text.partition(":")
    try:
        values = dict(item.split("=", 1) for item in params.split(","))
        return Kdf(
            algorithm,
            time_cost=int(values["t"]),
            memory_kib=int(values["m"]),
            parallelism=int(values["p"]),
            hash_bytes=int(values["l"]),
        )
    except (KeyError, ValueError) as exc:
        raise SchemaError(f"an account's kdf {text!r} cannot be read") from exc


class SqliteStore:
    """``AccountStore`` and ``ProgressStore`` over one SQLite file."""

    def __init__(
        self,
        path: Path | str,
        *,
        now: Callable[[], float] = time.time,
        migrations: Sequence[Sequence[str]] = MIGRATIONS,
    ) -> None:
        self._now = now
        # Autocommit mode, so every transaction below is an explicit BEGIN .. COMMIT.
        self._db = sqlite3.connect(path, isolation_level=None, timeout=policy.DATABASE_BUSY_TIMEOUT)
        try:
            if str(path) != ":memory:":
                # Readers (a backup, a future registration front end) never block the server's
                # writes, nor it theirs, beyond the busy timeout.
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=NORMAL")
            self._migrate(migrations)
        except BaseException:
            self._db.close()
            raise

    # --- lifecycle ---------------------------------------------------------------------------

    def close(self) -> None:
        self._db.close()

    @property
    def schema_version(self) -> int:
        row = self._db.execute("SELECT version FROM schema_version").fetchone()
        return int(row[0])

    def _migrate(self, migrations: Sequence[Sequence[str]]) -> None:
        self._db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self._db.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            self._db.execute("INSERT INTO schema_version VALUES (0)")
            current = 0
        else:
            current = int(row[0])
        if current > len(migrations):
            raise SchemaError(
                f"the database is at schema version {current}, newer than this server's "
                f"{len(migrations)}"
            )
        for version in range(current + 1, len(migrations) + 1):
            self._db.execute("BEGIN")
            try:
                for statement in migrations[version - 1]:
                    self._db.execute(statement)
                self._db.execute("UPDATE schema_version SET version = ?", (version,))
                self._db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise

    def _write(self, sql: str, params: tuple[object, ...]) -> sqlite3.Cursor:
        """One statement in its own transaction."""
        self._db.execute("BEGIN")
        try:
            cursor = self._db.execute(sql, params)
            self._db.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return cursor

    def _rollback(self) -> None:
        """Leave no transaction open after a failure (a failed COMMIT leaves one), so the next
        write starts clean instead of failing for as long as the server runs."""
        if self._db.in_transaction:
            self._db.execute("ROLLBACK")

    # --- AccountStore ------------------------------------------------------------------------

    def get(self, name: str) -> Account | None:
        row = self._db.execute(
            "SELECT name, salt, password_hash, iterations, kdf FROM accounts WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return Account(row[0], bytes(row[1]), bytes(row[2]), _kdf_from(row[4], row[3]))

    def create(self, account: Account) -> bool:
        kdf, iterations = _kdf_columns(account.kdf)
        cursor = self._write(
            "INSERT OR IGNORE INTO accounts (name, salt, password_hash, iterations, kdf, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (account.name, account.salt, account.password_hash, iterations, kdf, self._now()),
        )
        return cursor.rowcount == 1

    def callsign_registered(self, callsign: str, length: int) -> bool:
        if length == _CALLSIGN_INDEX_LENGTH:
            sql = "SELECT 1 FROM accounts WHERE substr(name, 1, 12) = ? LIMIT 1"
            row = self._db.execute(sql, (callsign,)).fetchone()
        else:
            sql = "SELECT 1 FROM accounts WHERE substr(name, 1, ?) = ? LIMIT 1"
            row = self._db.execute(sql, (length, callsign)).fetchone()
        return row is not None

    def count(self) -> int:
        return int(self._db.execute("SELECT count(*) FROM accounts").fetchone()[0])

    def touch_login(self, name: str) -> None:
        self._write("UPDATE accounts SET last_login = ? WHERE name = ?", (self._now(), name))

    def stale(
        self, before: float, *, after: StaleCursor | None = None, limit: int | None = None
    ) -> list[tuple[StaleCursor, str]]:
        seen, name = after if after is not None else (float("-inf"), "")
        # Spelled out, not as a row value, so the index range starts at the cursor.
        rows = self._db.execute(
            "SELECT coalesce(last_login, created) AS seen, name FROM accounts "
            "WHERE seen < ? AND seen >= ? AND (seen > ? OR name > ?) ORDER BY seen, name LIMIT ?",
            (before, seen, seen, name, -1 if limit is None else limit),
        ).fetchall()
        return [((row[0], row[1]), row[1]) for row in rows]

    def set_password(self, account: Account) -> bool:
        kdf, iterations = _kdf_columns(account.kdf)
        cursor = self._write(
            "UPDATE accounts SET salt = ?, password_hash = ?, iterations = ?, kdf = ? "
            "WHERE name = ?",
            (account.salt, account.password_hash, iterations, kdf, account.name),
        )
        return cursor.rowcount == 1

    def delete(self, name: str) -> bool:
        return self._write("DELETE FROM accounts WHERE name = ?", (name,)).rowcount == 1

    def delete_many(self, names: Iterable[str]) -> list[str]:
        gone = []
        self._db.execute("BEGIN")
        try:
            for name in names:
                if self._db.execute("DELETE FROM accounts WHERE name = ?", (name,)).rowcount:
                    gone.append(name)
            self._db.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return gone

    # --- ProgressStore -----------------------------------------------------------------------

    def load(self, identity: str) -> ProgressRecord | None:
        row = self._db.execute(
            "SELECT bays, marker, bay_points, score, career, updated FROM progress "
            "WHERE identity = ?",
            (identity,),
        ).fetchone()
        if row is None:
            return None
        bays = tuple(int(n) for n in row[0].split(","))
        return ProgressRecord(bays, row[1], row[2], row[3], row[4], row[5])

    def save(self, identity: str, record: ProgressRecord) -> None:
        self._write(
            "INSERT INTO progress (identity, bays, marker, bay_points, score, career, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(identity) DO UPDATE SET "
            "bays = excluded.bays, marker = excluded.marker, bay_points = excluded.bay_points, "
            "score = excluded.score, career = excluded.career, updated = excluded.updated",
            (
                identity,
                ",".join(str(n) for n in record.bays),
                record.marker,
                record.bay_points,
                record.score,
                record.career,
                self._now(),
            ),
        )

    def erase(self, identity: str) -> bool:
        return self._write("DELETE FROM progress WHERE identity = ?", (identity,)).rowcount == 1


def open_stores(
    data_dir: Path, *, now: Callable[[], float] = time.time
) -> tuple[Stores, SqliteStore]:
    """Create ``data_dir`` if needed and open its database. Returns the stores and the one object
    to ``close`` when the server stops. The directory and the file are made private to the server's
    user (``policy.DATA_DIR_MODE``, ``DATABASE_FILE_MODE``): they hold the password hashes."""
    data_dir.mkdir(mode=policy.DATA_DIR_MODE, parents=True, exist_ok=True)
    path = data_dir / policy.DATABASE_FILE_NAME
    db = SqliteStore(path, now=now)
    try:
        path.chmod(policy.DATABASE_FILE_MODE)
    except OSError as exc:
        log.warning("could not make %s private (%s); check its owner and mode", path, exc)
    return Stores(db, db), db
