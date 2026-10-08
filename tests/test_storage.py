"""The Storage interface: one contract suite for every backend, plus the SQLite specifics
(single writer thread, read-only pool, pragmas, deadlines).

PostgreSQL cases run only when OBSERVE_TEST_PG_DSN is set and skip otherwise; CI sets it for a
TimescaleDB service container (OBSERVE_TEST_PG_TIMESCALE=on) and for plain PostgreSQL (off). Each
PostgreSQL case gets its own schema, dropped afterwards, so cases never see each other's rows."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

import pytest

from observe.storage import (CHANGE_DOMAINS, IntegrityConflict, Storage, StorageBusy,
                             StorageError, StorageTimeout, open_storage)
from observe.storage import rollups
from observe.storage.schema import SCHEMA_VERSION, SchemaTooNewError
from observe.storage.sqlite import SqliteStorage

from .dbq import put, settle


@dataclass
class Migration:
    version: int
    statements: tuple[str, ...]


def _pg_dsn() -> str:
    dsn = os.environ.get("OBSERVE_TEST_PG_DSN")
    if not dsn:
        pytest.skip("OBSERVE_TEST_PG_DSN is not set")
    return dsn


@contextlib.contextmanager
def live_pg(timescale: str | None = None, plugins=None):
    """A storage on a throwaway schema of the server in OBSERVE_TEST_PG_DSN; the case is skipped
    when there is none."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    dsn = _pg_dsn()
    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    scoped = make_conninfo(dsn, options=f"-c search_path={schema},public")
    s = open_storage("", plugins, backend="postgres", dsn=scoped,
                     timescale=timescale or os.environ.get("OBSERVE_TEST_PG_TIMESCALE", "auto"))
    try:
        if getattr(s, "timescale", False):
            _hold_background_jobs(s, psycopg, dsn, schema)
        yield s
    finally:
        s.close()
        _drop_schema(psycopg, dsn, schema)


def _hold_background_jobs(s, psycopg, dsn: str, schema: str) -> None:
    """Unschedule the TimescaleDB background jobs of the schema, so a test decides when a policy
    runs. The policies are registered against dates years in the past, so their compression and
    retention jobs would otherwise start within seconds and take a lock that a running write unit
    needs. A job still runs on request (CALL run_job). Registering the policies again creates new
    jobs, so that is held as well."""
    def hold() -> None:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs "
                "WHERE hypertable_schema = %s OR config::text LIKE %s",
                (schema, f'%"{schema}"%'))

    original = s._apply_policies

    def apply_and_hold(levels) -> None:
        original(levels)
        hold()

    s._apply_policies = apply_and_hold
    hold()


def _drop_schema(psycopg, dsn: str, schema: str) -> None:
    """Drop a throwaway schema. On TimescaleDB its background jobs are removed first, and the drop
    is retried on a deadlock: with parallel workers, another worker's catalogue updates or a
    scheduled continuous aggregate refresh can take the same locks in the opposite order."""
    for attempt in range(5):
        try:
            with psycopg.connect(dsn, autocommit=True) as admin:
                has_ts = admin.execute(
                    "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'").fetchone()
                if has_ts:
                    admin.execute(
                        "SELECT delete_job(job_id) FROM timescaledb_information.jobs "
                        "WHERE hypertable_schema = %s OR config::text LIKE %s",
                        (schema, f'%"{schema}"%'))
                admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            return
        except psycopg.errors.DeadlockDetected:
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError(f"could not drop test schema {schema} after retries")


@pytest.fixture(params=["sqlite", "postgres"])
def storage(request, tmp_path):
    if request.param == "sqlite":
        s = open_storage(str(tmp_path / "s.db"))
        yield s
        s.close()
        return
    with live_pg() as s:
        yield s


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
    await storage.execute("INSERT INTO ingest_batches (host, batch_id, ts) VALUES ('h', 'old', ?)",
                          (now - 40 * day,))
    await storage.execute("INSERT INTO ingest_batches (host, batch_id, ts) VALUES ('h', 'new', ?)",
                          (now - 1 * day,))
    await storage.execute("INSERT INTO audit (ts, actor, kind) VALUES (?, 'a', 'login')",
                          (now - 400 * day,))
    await storage.execute("INSERT INTO audit (ts, actor, kind) VALUES (?, 'a', 'login')",
                          (now - 10 * day,))
    removed = await storage.apply_retention(now=now, retention_days=30, audit_retention_days=365)
    assert removed == 1
    assert await storage.fetchall("SELECT COUNT(*) FROM ingest_batches") == [(1,)]
    assert await storage.fetchall("SELECT COUNT(*) FROM audit") == [(1,)]


def _view_columns(storage, view):
    return storage.read_sync(lambda db: [c[0] for c in db.execute(
        f"SELECT * FROM {view} WHERE 1 = 0").description])


def test_the_summary_views_have_the_same_columns_on_every_backend(storage):
    summary = ["series_id", "resource", "scope", "metric", "unit", "attrs", "bucket", "n", "sum_v",
               "min_v", "max_v", "avg_v"]
    for view in ("metric_5m", "metric_hourly", "metric_daily"):
        assert _view_columns(storage, view) == summary
    assert _view_columns(storage, "availability_history") == [
        "monitor", "ts", "previous_state", "state", "message"]


async def test_availability_history_lists_every_state_change(storage):
    await storage.execute("INSERT INTO events (monitor, ts, previous, current, message) "
                          "VALUES ('m', 5, 'up', 'warn', 'Degraded')")
    assert await storage.fetchall(
        "SELECT monitor, ts, previous_state, state, message FROM availability_history") == [
        ("m", 5.0, "up", "warn", "Degraded")]


async def test_retention_drops_raw_samples_past_the_raw_level(storage):
    now = time.time()
    old = now - 40 * 86400
    await put(storage, [(old, "h", "cpu", "temp", "{}", 1.0, "C")])
    await storage.execute(
        "INSERT INTO app_settings (key, value, updated) VALUES ('retention.raw_days', '7', 1)")
    await settle(storage)
    await storage.apply_retention(now=now, retention_days=30, audit_retention_days=365)
    assert await storage.fetchall("SELECT COUNT(*) FROM samples") == [(0,)]


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


# ---- contract: the application queries return the same Python types on every backend --------

def _store_on(storage):
    from observe.store import Store
    st = Store.__new__(Store)
    st.storage = storage
    return st


async def test_hourly_series_returns_plain_floats_on_every_backend(storage):
    from observe.checks.base import CheckResult, Result
    st = _store_on(storage)
    base = (time.time() // 3600 - 2) * 3600
    for i, (res, val) in enumerate([(Result.OK, 10.0), (Result.OK, 20.0),
                                    (Result.FAIL, 999.0), (Result.OK, None)]):
        await st.record("m", base + i * 60 + 0.5, CheckResult(res, "", value=val))
    await st.record("m", base + 3600 + 5, CheckResult(Result.WARN, "", value=40.0))
    await settle(storage)
    got = await st.hourly_series("m", 1)
    assert got == [(base + 1800, 15.0), (base + 5400, 40.0)]
    assert all(type(t) is float and type(v) is float for t, v in got)


async def test_availability_is_a_plain_float_percentage_on_every_backend(storage):
    from observe.checks.base import CheckResult, Result
    st = _store_on(storage)
    now = time.time()
    for i, res in enumerate([Result.OK, Result.OK, Result.WARN, Result.FAIL]):
        await st.record("m", now - 10 - i, CheckResult(res, ""))
    await settle(storage)
    got = await st.availability("m", 1)
    assert got == 75.0 and type(got) is float
    assert await st.availability("none", 1) is None


async def test_the_summary_views_return_plain_ints_and_floats_on_every_backend(storage):
    base = int(time.time()) // 3600 * 3600 - 7200
    await put(storage, [(base + off, "h", "cpu", "temp", "{}", v, "C")
                        for off, v in [(10, 10.0), (20, 30.0), (310, 5.0)]])
    await settle(storage)
    for view in ("metric_5m", "metric_hourly"):
        rows = await storage.fetchall(f"SELECT n, sum_v, min_v, max_v, avg_v, bucket FROM {view}")
        assert rows
        for n, sum_v, min_v, max_v, avg_v, bucket in rows:
            assert type(n) is int and type(bucket) is int
            assert all(type(x) is float for x in (sum_v, min_v, max_v, avg_v))
    total = await storage.fetchall("SELECT SUM(n) FROM metric_5m")
    assert int(total[0][0]) == 3


async def test_latest_host_picks_the_newest_row_and_the_last_value_sent_wins_a_tie(storage):
    st = _store_on(storage)
    now = time.time()
    await storage.execute("INSERT INTO hosts (host, first_seen, last_seen, clean_shutdown) "
                          "VALUES ('h', 1, ?, 1)", (now,))
    for value in (1.0, 2.0, 3.0):  # one timestamp: the point sent last wins
        await put(storage, [(now - 5, "h", "cpu", "temp", "{}", value, "C")])
    await put(storage, [(now - 5000, "h", "disk", "used", "{}", 7.5, "%")])
    got = await st.latest_host("h", window=60, now=now, series=(("disk", "used"),))
    by_metric = {(s["source"], s["metric"]): s for s in got["samples"]}
    assert by_metric[("cpu", "temp")]["value"] == 3.0
    assert by_metric[("disk", "used")]["value"] == 7.5  # silent series still read
    assert type(got["last_seen"]) is float and got["clean_shutdown"] in (0, 1)
    assert await st.latest_host("nobody") is None


class _RowsOnly:
    def __init__(self, rows):
        self.rows = rows

    async def fetchall(self, _sql, _args=()):
        return self.rows


@pytest.mark.parametrize("total", [None, float("inf"), float("nan")])
async def test_availability_is_none_for_a_null_or_non_finite_total(total):
    st = _store_on(_RowsOnly([(4, total)]))
    assert await st.availability("m", 1) is None
    assert await _store_on(_RowsOnly([(4, 3.0)])).availability("m", 1) == 75.0


async def test_hourly_series_drops_a_non_finite_mean():
    st = _store_on(_RowsOnly([(3600, float("inf")), (7200, float("nan")), (10800, 2.5)]))
    assert await st.hourly_series("m", 1) == [(10800 + 1800.0, 2.5)]


async def test_a_monitor_reading_above_the_ingest_bound_is_not_stored(storage):
    from observe.checks.base import CheckResult, Result
    st = _store_on(storage)
    now = time.time()
    for i in range(2):  # two such readings in one bucket would overflow a float8 sum
        await st.record("m", now - 10 - i, CheckResult(Result.OK, "", value=1.7e308))
    await st.record("m", now - 20, CheckResult(Result.OK, "", value=4.0))
    await settle(storage)
    rows = await st.fetch(
        "SELECT COALESCE(SUM(n), 0), MAX(max_v) FROM metric_5m WHERE metric = ?",
        ("monitor.value",))
    assert rows[0][0] == 1 and rows[0][1] == 4.0
    assert await st.last_result("m") is not None


def test_timescale_jobs_of_a_test_schema_are_held_until_a_test_runs_them():
    """The fixture unschedules the background jobs, also after the policies are registered again,
    and a held job still runs on request."""
    import psycopg

    with live_pg() as s:
        if not s.timescale:
            pytest.skip("TimescaleDB is not in use")
        schema = s._wconn.execute("SELECT current_schema()").fetchone()[0]
        query = ("SELECT job_id, scheduled FROM timescaledb_information.jobs "
                 "WHERE hypertable_schema = %s OR config::text LIKE %s")
        args = (schema, f'%"{schema}"%')

        def jobs():
            with psycopg.connect(os.environ["OBSERVE_TEST_PG_DSN"], autocommit=True) as admin:
                return admin.execute(query, args).fetchall()

        before = jobs()
        assert before and not any(scheduled for _, scheduled in before)
        s._apply_policies(rollups.load_levels(s._wconn))
        after = jobs()
        assert after and not any(scheduled for _, scheduled in after)
        s._admin_run([f"CALL run_job({int(job)})" for job, _ in after])
