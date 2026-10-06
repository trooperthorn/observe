"""The Storage interface: one contract suite for every backend, plus the SQLite specifics
(single writer thread, read-only pool, pragmas, deadlines).

PostgreSQL cases run only when OBSERVE_TEST_PG_DSN is set; the backend itself arrives in a
later slice, so with a DSN set they skip until it exists."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

import pytest

from observe.storage import (CHANGE_DOMAINS, IntegrityConflict, Storage, StorageBusy,
                             StorageError, StorageTimeout, open_storage)
from observe.storage.schema import SCHEMA_VERSION, SchemaTooNewError
from observe.storage.sqlite import SqliteStorage


@dataclass
class Migration:
    version: int
    statements: tuple[str, ...]


def _make(backend: str, tmp_path) -> Storage:
    if backend == "sqlite":
        return open_storage(str(tmp_path / "s.db"))
    dsn = os.environ.get("OBSERVE_TEST_PG_DSN")
    if not dsn:
        pytest.skip("OBSERVE_TEST_PG_DSN is not set")
    pytest.skip("the PostgreSQL backend is delivered in a later slice")


@pytest.fixture(params=["sqlite", "postgres"])
def storage(request, tmp_path):
    s = _make(request.param, tmp_path)
    yield s
    s.close()


@pytest.fixture
def sqlite_storage(tmp_path):
    s = SqliteStorage(str(tmp_path / "s.db"))
    yield s
    s.close()


# ---- contract: every backend --------------------------------------------------------------

def test_the_backend_satisfies_the_protocol(storage):
    assert isinstance(storage, Storage)
    assert storage.backend in ("sqlite", "postgres")


async def test_write_commits_and_returns_the_unit_result(storage):
    got = await storage.write(lambda db: db.execute(
        "INSERT INTO app_settings (key, value, updated) VALUES ('k','v',1) RETURNING key").fetchone())
    assert got == ("k",)
    assert await storage.fetchall("SELECT value FROM app_settings WHERE key='k'") == [("v",)]


async def test_a_failing_unit_rolls_back_everything_it_did(storage):
    def unit(db):
        db.execute("INSERT INTO app_settings (key, value, updated) VALUES ('a','1',1)")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await storage.write(unit, touches=("admin",))
    assert await storage.fetchall("SELECT COUNT(*) FROM app_settings") == [(0,)]


async def test_a_constraint_failure_is_an_integrity_conflict(storage):
    await storage.execute("INSERT INTO app_settings (key, value, updated) VALUES ('k','v',1)")
    with pytest.raises(IntegrityConflict):
        await storage.execute("INSERT INTO app_settings (key, value, updated) VALUES ('k','w',2)")
    assert await storage.fetchall("SELECT value FROM app_settings") == [("v",)]


async def test_execute_returns_the_rows_a_statement_produces(storage):
    rows = await storage.execute(
        "INSERT INTO users (username, hash, created) VALUES ('u','x',1) RETURNING id")
    assert rows == [(1,)]


async def test_change_sequences_bump_per_touched_domain_after_commit(storage):
    assert storage.change_seqs() == {d: 0 for d in CHANGE_DOMAINS}
    await storage.write(lambda db: None, touches=("hosts", "events"))
    await storage.write(lambda db: None, touches=("hosts",))
    assert storage.change_seq("hosts") == 2
    assert storage.change_seq("events") == 1
    assert storage.change_seq("metrics") == 0
    assert await storage.fetchall("SELECT seq FROM change_seq WHERE domain='hosts'") == [(2,)]


async def test_a_rolled_back_unit_does_not_bump_a_sequence(storage):
    def unit(db):
        raise ValueError("no")

    with pytest.raises(ValueError):
        await storage.write(unit, touches=("metrics",))
    assert storage.change_seq("metrics") == 0
    assert await storage.fetchall("SELECT seq FROM change_seq WHERE domain='metrics'") == [(0,)]


async def test_an_unknown_change_domain_is_refused_before_anything_runs(storage):
    ran = []
    with pytest.raises(ValueError, match="unknown change domain"):
        await storage.write(lambda db: ran.append(1), touches=("nope",))
    assert ran == []


def test_a_unit_that_writes_through_the_sync_form_joins_its_own_transaction(storage):
    def outer(db):
        db.execute("INSERT INTO app_settings (key, value, updated) VALUES ('a','1',1)")
        storage.write_sync(lambda d: d.execute(
            "INSERT INTO app_settings (key, value, updated) VALUES ('b','2',1)"), touches=("admin",))
        raise RuntimeError("undo both")

    with pytest.raises(RuntimeError):
        storage.write_sync(outer)
    assert storage.read_sync(lambda db: db.execute(
        "SELECT COUNT(*) FROM app_settings").fetchone()) == (0,)
    assert storage.change_seq("admin") == 0


async def test_plugin_tables_are_created_once_and_kept(storage):
    plugins = {"demo": [Migration(1, ("CREATE TABLE IF NOT EXISTS demo_t (id INTEGER PRIMARY KEY)",))]}
    storage.apply_plugin_migrations(plugins)
    storage.apply_plugin_migrations(plugins)
    await storage.execute("INSERT INTO demo_t (id) VALUES (1)")
    assert await storage.fetchall("SELECT version FROM plugin_schema WHERE plugin='demo'") == [(1,)]


async def test_retention_drops_old_rows_and_keeps_the_rest(storage):
    now = 1_000_000_000.0
    day = 86400
    await storage.execute("INSERT INTO results VALUES ('m', ?, 'ok', 1, 1, '')", (now - 40 * day,))
    await storage.execute("INSERT INTO results VALUES ('m', ?, 'ok', 1, 1, '')", (now - 1 * day,))
    await storage.execute("INSERT INTO audit (ts, actor, kind) VALUES (?, 'a', 'login')",
                          (now - 400 * day,))
    await storage.execute("INSERT INTO audit (ts, actor, kind) VALUES (?, 'a', 'login')",
                          (now - 10 * day,))
    removed = await storage.apply_retention(now=now, retention_days=30, audit_retention_days=365)
    assert removed == 1
    assert await storage.fetchall("SELECT COUNT(*) FROM results") == [(1,)]
    assert await storage.fetchall("SELECT COUNT(*) FROM audit") == [(1,)]


async def test_rollup_with_nothing_to_fold_writes_no_buckets(storage):
    assert await storage.rollup(1_000_000_000.0) == 0


def test_close_is_safe_to_repeat(storage):
    storage.close()
    storage.close()


# ---- SQLite: the writer ---------------------------------------------------------------------

def test_every_write_unit_runs_on_the_one_writer_thread_in_submission_order(sqlite_storage):
    seen: list[tuple[str, int]] = []

    def unit(n: int):
        def run(db):
            seen.append((threading.current_thread().name, n))
        return run

    futures = [sqlite_storage._writer.submit(sqlite_storage._run_unit, unit(n), ())
               for n in range(20)]
    for f in futures:
        f.result()
    assert [n for _, n in seen] == list(range(20))
    assert {name for name, _ in seen} == {"db-writer_0"}


async def test_a_blocked_write_unit_does_not_delay_name_resolution(sqlite_storage):
    """The writer has its own thread, so the default pool that getaddrinfo uses never waits for
    the database, however long a write unit holds the writer."""
    started, release = threading.Event(), threading.Event()

    def long_unit(db):
        started.set()
        release.wait(30)
        return "done"

    task = asyncio.ensure_future(sqlite_storage.write(long_unit))
    assert await asyncio.to_thread(started.wait, 10)
    try:
        started_at = time.monotonic()
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo("localhost", 80), 10)
        assert not task.done()  # the unit is still holding the writer
        assert time.monotonic() - started_at < 1.5
    finally:
        release.set()
    assert await task == "done"


async def test_a_hundred_concurrent_writes_and_reads_never_hit_a_locked_database(sqlite_storage):
    async def write(i: int):
        await sqlite_storage.execute(
            "INSERT INTO app_settings (key, value, updated) VALUES (?,?,?)", (f"k{i}", "v", i))

    async def read(i: int):
        return await sqlite_storage.fetchall("SELECT COUNT(*) FROM app_settings")

    results = await asyncio.gather(*(f(i) for i in range(100) for f in (write, read)),
                                   return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == []
    assert await sqlite_storage.fetchall("SELECT COUNT(*) FROM app_settings") == [(100,)]


# ---- SQLite: the read pool ------------------------------------------------------------------

def test_a_read_connection_refuses_writes(sqlite_storage):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        sqlite_storage.read_sync(lambda db: db.execute(
            "INSERT INTO app_settings (key, value, updated) VALUES ('x','y',1)"))


async def test_a_read_sees_a_committed_write_in_one_snapshot(sqlite_storage):
    await sqlite_storage.execute("INSERT INTO app_settings (key, value, updated) VALUES ('a','1',1)")

    def two_reads(db):
        first = db.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0]
        # The writer is free while a read runs; the row it commits is not in this snapshot.
        sqlite_storage.write_sync(lambda w: w.execute(
            "INSERT INTO app_settings (key, value, updated) VALUES ('b','2',2)"))
        return first, db.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0]

    assert await sqlite_storage.read(two_reads) == (1, 1)
    assert await sqlite_storage.fetchall("SELECT COUNT(*) FROM app_settings") == [(2,)]


def test_writer_pragmas_are_set(sqlite_storage):
    def pragmas(db):
        return {name: db.execute(f"PRAGMA {name}").fetchone()[0] for name in (
            "journal_mode", "synchronous", "foreign_keys", "busy_timeout", "cache_size",
            "temp_store", "mmap_size", "wal_autocheckpoint", "journal_size_limit")}

    got = sqlite_storage.write_sync(pragmas)
    assert got == {"journal_mode": "wal", "synchronous": 1, "foreign_keys": 1,
                   "busy_timeout": 5000, "cache_size": -16000, "temp_store": 2,
                   "mmap_size": 67108864, "wal_autocheckpoint": 1000,
                   "journal_size_limit": 67108864}


def test_reader_pragmas_are_set_and_there_are_three_of_them(sqlite_storage):
    def pragmas(db):
        return {name: db.execute(f"PRAGMA {name}").fetchone()[0] for name in (
            "query_only", "cache_size", "temp_store", "mmap_size")}

    assert sqlite_storage.read_sync(pragmas) == {
        "query_only": 1, "cache_size": -8000, "temp_store": 2, "mmap_size": 67108864}
    assert len(sqlite_storage._all_readers) == 3


def test_an_empty_pool_raises_busy_at_the_deadline(tmp_path):
    s = SqliteStorage(str(tmp_path / "s.db"), read_deadline_s=0.3)
    held, release = threading.Event(), threading.Event()
    holders = []

    def hold(db):
        held.set()
        release.wait(30)

    try:
        for _ in range(3):
            held.clear()
            t = threading.Thread(target=s.read_sync, args=(hold,))
            t.start()
            holders.append(t)
            assert held.wait(10)
        started = time.monotonic()
        with pytest.raises(StorageBusy):
            s.read_sync(lambda db: db.execute("SELECT 1").fetchone())
        assert 0.25 <= time.monotonic() - started < 3
    finally:
        release.set()
        for t in holders:
            t.join(10)
        s.close()


def test_a_read_past_the_deadline_is_interrupted(tmp_path):
    s = SqliteStorage(str(tmp_path / "s.db"), read_deadline_s=0.2)
    try:
        with pytest.raises(StorageTimeout):
            s.read_sync(lambda db: db.execute(
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) "
                "SELECT COUNT(*) FROM n").fetchall())
        # the connection went back to the pool and still works
        assert s.read_sync(lambda db: db.execute("SELECT 1").fetchone()) == (1,)
    finally:
        s.close()


def test_a_memory_database_serves_reads_from_the_writer_connection():
    s = open_storage(":memory:")
    try:
        s.write_sync(lambda db: db.execute(
            "INSERT INTO app_settings (key, value, updated) VALUES ('a','1',1)"))
        assert s.read_sync(lambda db: db.execute("SELECT key FROM app_settings").fetchall()) == [("a",)]
    finally:
        s.close()


def test_a_database_written_by_a_newer_release_is_refused(tmp_path):
    path = str(tmp_path / "n.db")
    open_storage(path).close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION + 1,))
    db.commit()
    db.close()
    with pytest.raises(SchemaTooNewError, match="newer"):
        open_storage(path)


def test_an_unknown_backend_is_refused():
    with pytest.raises(StorageError):
        open_storage(":memory:", backend="mysql")
