"""The store protocols, checked against every implementation (DESIGN §3, ADR 7)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from opencs import policy
from opencs.lobby.identity import Identity, Pilot, hash_password
from opencs.store.base import Account, Kdf, ProgressRecord, Stores
from opencs.store.memory import MemoryAccountStore, MemoryProgressStore
from opencs.store.sqlite import MIGRATIONS, SchemaError, SqliteStore, open_stores

STAMP = 1_700_000_000.5

type Factory = Callable[[Path], Stores]


def _memory(tmp_path: Path) -> Stores:
    return Stores(MemoryAccountStore(), MemoryProgressStore(lambda: STAMP))


def _sqlite(tmp_path: Path) -> Stores:
    db = SqliteStore(tmp_path / "t.db", now=lambda: STAMP)
    return Stores(db, db)


FACTORIES: dict[str, Factory] = {"memory": _memory, "sqlite": _sqlite}


@pytest.fixture(params=sorted(FACTORIES))
def stores(request: pytest.FixtureRequest, tmp_path: Path) -> Stores:
    return FACTORIES[request.param](tmp_path)


RECORD = ProgressRecord(
    bays=(3, 2, 1, 1, 5, 4, 0, 1, 2), marker=-7, bay_points=957, score=1670, career=90_000
)


def test_an_account_is_created_once_and_read_back(stores: Stores) -> None:
    account = Account("ALICE", b"\x00salt\xff", b"\x01hash\xfe", Kdf.pbkdf2(1234))
    assert stores.accounts.get("ALICE") is None
    assert stores.accounts.create(account)
    assert stores.accounts.get("ALICE") == account
    assert not stores.accounts.create(Account("ALICE", b"other", b"other", Kdf.pbkdf2(1)))
    assert stores.accounts.get("ALICE") == account


def test_callsign_registered_compares_the_cut_names(stores: Stores) -> None:
    stores.accounts.create(Account("ALPHAXYZ", b"s", b"h", Kdf.pbkdf2(1)))
    assert stores.accounts.callsign_registered("ALPHAXYZ", 8)
    assert not stores.accounts.callsign_registered("ALPHAXY", 8)
    assert stores.accounts.callsign_registered("ALPHA", 5)
    assert not stores.accounts.callsign_registered("BRAVO", 5)


def test_progress_round_trips_every_field_and_is_stamped(stores: Stores) -> None:
    assert stores.progress.load("acct:ALICE") is None
    stores.progress.save("acct:ALICE", RECORD)
    loaded = stores.progress.load("acct:ALICE")
    assert loaded is not None
    assert loaded.updated == STAMP
    assert loaded == replace(RECORD, updated=STAMP)


def test_a_save_replaces_the_whole_record_and_other_identities_are_untouched(
    stores: Stores,
) -> None:
    stores.progress.save("acct:ALICE", RECORD)
    stores.progress.save("JOINGENI", ProgressRecord((1,) * 9, 0, 5, 6, 7))
    newer = ProgressRecord((5,) * 9, 119, 0, 3, 4)
    stores.progress.save("acct:ALICE", newer)
    alice, guest = stores.progress.load("acct:ALICE"), stores.progress.load("JOINGENI")
    assert alice is not None and guest is not None
    assert (alice.bays, alice.marker, alice.score, alice.career) == ((5,) * 9, 119, 3, 4)
    assert (guest.bays, guest.bay_points, guest.score, guest.career) == ((1,) * 9, 5, 6, 7)


def test_identity_keys_are_case_sensitive_and_exact(stores: Stores) -> None:
    stores.progress.save("acct:ALICE", RECORD)
    assert stores.progress.load("acct:alice") is None
    assert stores.progress.load("ALICE") is None


def test_negative_markers_and_large_scores_survive(stores: Stores) -> None:
    big = ProgressRecord((0,) * 9, -136, 9_999_999, 10**13, 10**15)
    stores.progress.save("k", big)
    loaded = stores.progress.load("k")
    assert loaded is not None
    assert (loaded.marker, loaded.bay_points, loaded.score, loaded.career) == (
        -136,
        9_999_999,
        10**13,
        10**15,
    )


# --- SQLite only: the file, the schema and the migrations -------------------------------------


def test_a_reopened_file_still_holds_accounts_and_progress(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    first = SqliteStore(path)
    first.create(Account("ALICE", b"salt", b"hash", Kdf.pbkdf2(7)))
    first.save("acct:ALICE", RECORD)
    first.close()
    second = SqliteStore(path)
    assert second.get("ALICE") == Account("ALICE", b"salt", b"hash", Kdf.pbkdf2(7))
    loaded = second.load("acct:ALICE")
    assert loaded is not None and loaded.bays == RECORD.bays and loaded.career == RECORD.career
    second.close()


def test_a_new_file_is_migrated_to_the_latest_version(tmp_path: Path) -> None:
    db = SqliteStore(tmp_path / "t.db")
    assert db.schema_version == len(MIGRATIONS)
    db.close()


def test_a_new_migration_runs_once_on_an_old_file_and_keeps_its_data(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    old = SqliteStore(path)
    old.save("k", RECORD)
    old.close()
    extra = (*MIGRATIONS, ("ALTER TABLE progress ADD COLUMN note TEXT",))
    upgraded = SqliteStore(path, migrations=extra)
    assert upgraded.schema_version == len(extra)
    loaded = upgraded.load("k")
    assert loaded is not None and loaded.score == RECORD.score
    upgraded.close()
    again = SqliteStore(path, migrations=extra)  # nothing to run: the ALTER would fail twice
    assert again.schema_version == len(extra)
    again.close()


def test_a_version_1_file_gains_the_kdf_column_and_its_accounts_read_as_pbkdf2(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1.db"
    SqliteStore(path, migrations=MIGRATIONS[:1]).close()
    salt = b"0123456789abcdef"
    digest = hash_password("hunter2", salt, Kdf.pbkdf2(1000))
    v1 = sqlite3.connect(path)
    with v1:  # a row as a version-1 server wrote it
        v1.execute(
            "INSERT INTO accounts (name, salt, password_hash, iterations, created) "
            "VALUES ('ALICE', ?, ?, 1000, 1.0)",
            (salt, digest),
        )
    v1.close()
    db = SqliteStore(path)
    assert db.schema_version == len(MIGRATIONS)
    assert db.get("ALICE") == Account("ALICE", salt, digest, Kdf.pbkdf2(1000))
    assert isinstance(Identity(db, kdf=Kdf.pbkdf2(1000)).login("ALICE,hunter2"), Pilot)
    db.close()


def test_a_pbkdf2_row_is_written_as_a_version_1_server_wrote_it(tmp_path: Path) -> None:
    db = SqliteStore(tmp_path / "t.db")
    db.create(Account("ALICE", b"s", b"h", Kdf.pbkdf2(1000)))
    db.create(Account("BOB", b"s", b"h", Kdf.argon2id(8, 1, 1, 32)))
    rows = db._db.execute("SELECT name, iterations, kdf FROM accounts ORDER BY name").fetchall()
    assert rows == [("ALICE", 1000, ""), ("BOB", 0, "argon2id:m=8,t=1,p=1,l=32")]
    db.close()


def test_an_unreadable_kdf_is_a_schema_error(tmp_path: Path) -> None:
    db = SqliteStore(tmp_path / "t.db")
    db.create(Account("ALICE", b"s", b"h", Kdf.pbkdf2(1000)))
    db._db.execute("UPDATE accounts SET kdf = 'argon2id:m=8' WHERE name = 'ALICE'")
    with pytest.raises(SchemaError):
        db.get("ALICE")
    db.close()


def test_a_failing_migration_leaves_the_old_version_intact(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    SqliteStore(path).close()
    bad = (*MIGRATIONS, ("ALTER TABLE progress ADD COLUMN ok TEXT", "THIS IS NOT SQL"))
    with pytest.raises(sqlite3.Error):
        SqliteStore(path, migrations=bad)
    db = SqliteStore(path)  # still version 1, and the half-applied ALTER was rolled back
    assert db.schema_version == len(MIGRATIONS)
    with pytest.raises(sqlite3.Error):
        db._db.execute("SELECT ok FROM progress")
    db.close()


def test_a_database_from_a_newer_server_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    SqliteStore(path, migrations=(*MIGRATIONS, ("CREATE TABLE later (x)",))).close()
    with pytest.raises(SchemaError):
        SqliteStore(path)


def test_passwords_are_never_in_the_file(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    db = SqliteStore(path)
    password = "Hunter2-Unmistakable"
    identity = Identity(db, kdf=Kdf.pbkdf2(1000))
    assert isinstance(identity.login(f"ALICE,{password}"), Pilot)
    db.close()
    raw = path.read_bytes()
    assert password.encode() not in raw
    assert b"ALICE" in raw  # the name is stored, and the check above could otherwise be vacuous
    stored = SqliteStore(path).get("ALICE")
    assert stored is not None and len(stored.password_hash) == 32 and len(stored.salt) > 0


def test_a_locked_database_fails_the_save_quickly_and_recovers(tmp_path: Path) -> None:
    path = tmp_path / "locked.db"
    store = SqliteStore(path)
    record = ProgressRecord((1,) * 9, 0, 5, 6, 7)
    # A second connection holding the write lock (in WAL mode readers never block a writer).
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    began = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        store.save("A", record)
    # DESIGN §3.3: the event loop is never held longer than the busy timeout (plus slack).
    assert time.monotonic() - began < policy.DATABASE_BUSY_TIMEOUT + 1.0
    assert not store._db.in_transaction  # the failed COMMIT was rolled back
    other.execute("ROLLBACK")
    other.close()
    store.save("A", record)  # the connection recovered
    assert store.load("A") is not None
    store.close()


# --- account upkeep (counts, logins, staleness, passwords, deletion) ----------------------------


def test_accounts_are_counted_touched_found_stale_and_deleted(tmp_path: Path) -> None:
    now = [1000.0]
    for accounts in (
        MemoryAccountStore(lambda: now[0]),
        SqliteStore(tmp_path / "upkeep.db", now=lambda: now[0]),
    ):
        assert accounts.create(Account("OLD", b"s", b"h", Kdf.pbkdf2(1)))
        assert accounts.create(Account("BACK", b"s", b"h", Kdf.pbkdf2(1)))
        now[0] = 2000.0
        assert accounts.create(Account("NEW", b"s", b"h", Kdf.pbkdf2(1)))
        accounts.touch_login("BACK")
        assert accounts.count() == 3
        assert accounts.stale(1500.0) == ["OLD"]
        assert accounts.delete("OLD")
        assert not accounts.delete("OLD")
        assert accounts.get("OLD") is None
        assert accounts.count() == 2
        now[0] = 1000.0


def test_a_password_can_be_replaced_only_on_an_existing_account(stores: Stores) -> None:
    stores.accounts.create(Account("ALICE", b"s1", b"h1", Kdf.pbkdf2(1)))
    assert stores.accounts.set_password(Account("ALICE", b"s2", b"h2", Kdf.pbkdf2(2)))
    assert stores.accounts.get("ALICE") == Account("ALICE", b"s2", b"h2", Kdf.pbkdf2(2))
    assert not stores.accounts.set_password(Account("NOBODY", b"s", b"h", Kdf.pbkdf2(1)))


def test_an_account_keeps_how_its_hash_was_made(stores: Stores) -> None:
    argon = Kdf.argon2id(memory_kib=19456, time_cost=2, parallelism=1, hash_bytes=32)
    stores.accounts.create(Account("ALICE", b"s1", b"h1", Kdf.pbkdf2(100_000)))
    stores.accounts.create(Account("BOB", b"s2", b"h2", argon))
    assert stores.accounts.get("ALICE") == Account("ALICE", b"s1", b"h1", Kdf.pbkdf2(100_000))
    assert stores.accounts.get("BOB") == Account("BOB", b"s2", b"h2", argon)
    assert stores.accounts.set_password(Account("ALICE", b"s3", b"h3", argon))
    assert stores.accounts.get("ALICE") == Account("ALICE", b"s3", b"h3", argon)


def test_a_progress_record_can_be_erased(stores: Stores) -> None:
    stores.progress.save("acct:ALICE", RECORD)
    assert stores.progress.erase("acct:ALICE")
    assert stores.progress.load("acct:ALICE") is None
    assert not stores.progress.erase("acct:ALICE")


def test_the_database_file_uses_write_ahead_logging(tmp_path: Path) -> None:
    db = SqliteStore(tmp_path / "wal.db")
    assert db._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db.close()


def test_the_data_directory_and_database_are_private_to_the_server(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _, db = open_stores(data)
    assert data.stat().st_mode & 0o777 == policy.DATA_DIR_MODE
    assert (data / policy.DATABASE_FILE_NAME).stat().st_mode & 0o777 == policy.DATABASE_FILE_MODE
    db.close()
