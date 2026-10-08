"""A write that loses a deadlock or a serialization conflict is rolled back and run again, and
the caller sees StorageBusy only when every run lost. The policy is checked against a fake
connection; the TimescaleDB cases run a compression or retention job against live ingest and
need OBSERVE_TEST_PG_DSN with OBSERVE_TEST_PG_TIMESCALE=on, and skip otherwise."""

from __future__ import annotations

import asyncio
import threading

import psycopg
import pytest
from psycopg import errors as pg_errors

from observe.storage import StorageBusy, not_retryable, postgres, series
from observe.storage.base import IntegrityConflict, WriteGate
from observe.storage.postgres import PgConn, PgStorage, Scrubber

from .test_storage import live_pg


class FakeRaw:
    """The part of a psycopg connection that a write unit's transaction uses."""

    closed = False
    broken = False

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeConn:
    def execute(self, sql, args=()):
        return self

    def fetchone(self):
        return (7,)


def fake_storage(monkeypatch) -> tuple[PgStorage, FakeRaw, list[float]]:
    """A PgStorage with no database behind it: only the transaction loop is real."""
    s = PgStorage.__new__(PgStorage)
    s._scrub = Scrubber("secret")
    s._wraw = raw = FakeRaw()
    s._wconn = FakeConn()
    s._touched = set()
    s._seqs = {}
    s._gate = WriteGate()
    pauses: list[float] = []
    monkeypatch.setattr(postgres.time, "sleep", pauses.append)
    return s, raw, pauses


def losing(times: int, error: type[psycopg.Error] = pg_errors.DeadlockDetected):
    """A unit that raises `error` on its first `times` runs and then returns the run count."""
    runs: list[int] = []

    def unit(db):
        runs.append(1)
        if len(runs) <= times:
            raise error("deadlock detected")
        return len(runs)
    return unit, runs


def test_a_deadlock_rolls_the_unit_back_and_runs_it_again(monkeypatch):
    s, raw, pauses = fake_storage(monkeypatch)
    unit, runs = losing(2)
    assert s._run_unit(unit, ()) == 3
    assert len(runs) == 3 and raw.rollbacks == 2 and raw.commits == 1
    assert len(pauses) == 2


def test_a_serialization_failure_is_retried_too(monkeypatch):
    s, raw, _ = fake_storage(monkeypatch)
    unit, _runs = losing(1, pg_errors.SerializationFailure)
    assert s._run_unit(unit, ()) == 2
    assert raw.rollbacks == 1 and raw.commits == 1


def test_a_write_that_keeps_losing_ends_as_storage_busy(monkeypatch):
    s, raw, pauses = fake_storage(monkeypatch)
    unit, runs = losing(100)
    with pytest.raises(StorageBusy):
        s._run_unit(unit, ())
    assert len(runs) == postgres.WRITE_ATTEMPTS
    assert raw.commits == 0 and raw.rollbacks == postgres.WRITE_ATTEMPTS
    assert len(pauses) == postgres.WRITE_ATTEMPTS - 1


def test_the_pause_between_runs_is_short_and_jittered(monkeypatch):
    s, _raw, pauses = fake_storage(monkeypatch)
    unit, _runs = losing(postgres.WRITE_ATTEMPTS - 1)
    s._run_unit(unit, ())
    assert len(pauses) == postgres.WRITE_ATTEMPTS - 1
    for attempt, pause in enumerate(pauses, 1):
        assert 0 <= pause <= postgres.RETRY_PAUSE_S * 2 ** attempt
    # Even the longest possible pauses add up to well under a second.
    assert sum(postgres.RETRY_PAUSE_S * 2 ** a for a in range(1, postgres.WRITE_ATTEMPTS)) < 1.0


def test_a_unit_marked_not_retryable_runs_once(monkeypatch):
    s, raw, pauses = fake_storage(monkeypatch)
    unit, runs = losing(1)
    with pytest.raises(StorageBusy):
        s._run_unit(not_retryable(unit), ())
    assert len(runs) == 1 and raw.rollbacks == 1 and not pauses


def test_any_other_error_is_not_retried(monkeypatch):
    s, _raw, pauses = fake_storage(monkeypatch)
    unit, runs = losing(5, pg_errors.UniqueViolation)
    with pytest.raises(IntegrityConflict):
        s._run_unit(unit, ())
    assert len(runs) == 1 and not pauses


def test_a_retried_unit_bumps_each_change_counter_once(monkeypatch):
    s, raw, _pauses = fake_storage(monkeypatch)
    unit, _runs = losing(1)

    def touching(db):
        s._touched.add("metrics")
        return unit(db)
    s._run_unit(touching, ())
    assert s._seqs == {"metrics": 7} and raw.commits == 1 and s._touched == set()


def test_a_deadlock_met_outside_a_unit_is_storage_busy():
    s = PgStorage.__new__(PgStorage)
    s._scrub = Scrubber()
    assert isinstance(s._translate(pg_errors.DeadlockDetected("x")), StorageBusy)
    assert isinstance(s._translate(pg_errors.SerializationFailure("x")), StorageBusy)


class Recorder:
    """A driver connection that records the statements and returns no rows."""

    description = None

    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, sql, args=None):
        self.sql.append(sql)
        return self


def test_the_chunks_are_locked_for_writing_only_on_timescale():
    on, off = Recorder(), Recorder()
    PgConn(on, timescale=True).lock_samples_for_write(1000, 2000)
    PgConn(off).lock_samples_for_write(1000, 2000)
    assert len(on.sql) == 1 and "ROW EXCLUSIVE MODE" in on.sql[0]
    assert "range_end_integer > 1000" in on.sql[0] and "range_start_integer <= 2000" in on.sql[0]
    assert off.sql == []


def test_a_batch_takes_the_lock_before_it_reads_the_stored_samples():
    raw = Recorder()
    series._stored_values(PgConn(raw, timescale=True), [(1, 1000), (2, 5000)])
    assert "ROW EXCLUSIVE MODE" in raw.sql[0] and "range_end_integer > 1000" in raw.sql[0]
    assert "range_start_integer <= 5000" in raw.sql[0]
    assert raw.sql[1].startswith("SELECT series_id, ts, value FROM samples")


# ---- TimescaleDB: a policy job against live ingest ----------------------------------------

DAY_MS = 86_400_000
BASE_MS = 1_677_628_800_000  # 2023-03-01 00:00 UTC, older than every compression and retention bound
DAYS = 6
METRICS = 12
ROUNDS = 25
JOB_EVERY_S = 0.02  # a policy job runs now and then, not back to back


def points_for(round_no: int) -> list[series.Point]:
    """One round: a new reading of every metric on each of DAYS days, so each unit writes to
    DAYS chunks and none of its points exists yet."""
    return [series.Point("job", f"m{m}", "1", "{}", BASE_MS + d * DAY_MS + (round_no + 1) * 1000, 1.0)
            for d in range(DAYS) for m in range(METRICS)]


def run_job(conninfo: str, step, stop: threading.Event, log: dict) -> None:
    """What a TimescaleDB policy job does to the chunks: take locks that conflict with ingest,
    pass after pass until ingest has finished. `step(conn)` is one pass."""
    with psycopg.connect(conninfo, autocommit=True) as conn:
        while not stop.is_set():
            try:
                step(conn)
                log["runs"] += 1
            except pg_errors.DeadlockDetected:
                log["job_deadlocks"] += 1  # the job lost; a real job is scheduled again
            except psycopg.Error as err:
                log["errors"].append(f"{type(err).__name__}: {err}")
            stop.wait(JOB_EVERY_S)


def chunk_names(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT format('%I.%I', chunk_schema, chunk_name) FROM timescaledb_information.chunks "
        "WHERE hypertable_schema = current_schema() AND hypertable_name = 'samples' "
        "ORDER BY range_start").fetchall()]


def compress_pass(conn) -> None:
    """The compression policy: one transaction per chunk, then back again so the next pass has
    work (a policy job only ever compresses a chunk once)."""
    for name in chunk_names(conn):
        conn.execute("SELECT compress_chunk(%s::regclass, if_not_compressed => true)", (name,))
    for name in chunk_names(conn):
        conn.execute("SELECT decompress_chunk(%s::regclass, if_compressed => true)", (name,))


async def ingest_during(s, step) -> dict:
    """Write ROUNDS units while a job runs `step` in a loop on its own connection."""
    stop = threading.Event()
    log: dict = {"runs": 0, "job_deadlocks": 0, "errors": []}
    await s.write(lambda db: series.record_points(
        db, kind="host", name="h", points=points_for(-1), now=0.0, rollups=False),
        touches=("metrics",))
    job = threading.Thread(target=run_job, args=(s._conninfo, step, stop, log))
    job.start()
    try:
        for n in range(ROUNDS):
            pts = points_for(n)
            await s.write(lambda db, pts=pts: series.record_points(
                db, kind="host", name="h", points=pts, now=0.0, rollups=False),
                touches=("metrics",))
            await asyncio.sleep(0)
    finally:
        stop.set()
        job.join(timeout=120)
    assert not job.is_alive()
    return log


@pytest.fixture
def timescale():
    with live_pg() as s:
        if not s.timescale:
            pytest.skip("TimescaleDB is not in use")
        yield s


def test_a_batch_holds_the_insert_lock_on_the_chunks_it_may_touch(timescale):
    s = timescale
    pts = [series.Point("job", "m", "1", "{}", BASE_MS + d * DAY_MS, 1.0) for d in range(3)]
    s.write_sync(lambda db: series.record_points(
        db, kind="host", name="h", points=pts, now=0.0, rollups=False))

    def held(db):
        db.lock_samples_for_write(BASE_MS, BASE_MS + DAY_MS)  # the first two days
        rows = db.execute(
            "SELECT c.relname, l.mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
            "WHERE l.pid = pg_backend_pid() AND c.relkind = 'r'").fetchall()
        return {name for name, mode in rows
                if mode == "RowExclusiveLock" and name.startswith("_hyper_")}
    assert len(s.write_sync(held)) == 2


async def test_ingest_loses_nothing_while_chunks_are_compressed_and_decompressed(timescale):
    s = timescale
    log = await ingest_during(s, compress_pass)
    assert not log["errors"], log["errors"]
    assert log["runs"] > 0
    total = (await s.fetchall("SELECT COUNT(*) FROM samples"))[0][0]
    assert total == (ROUNDS + 1) * DAYS * METRICS


async def test_ingest_loses_nothing_while_old_chunks_are_dropped(timescale):
    s = timescale
    # The job puts a row into each of the days before the writer's days and drops those chunks
    # again, so the chunks the writer scans keep appearing and vanishing under it.
    older = [BASE_MS - n * DAY_MS for n in range(1, 5)]

    def drop_pass(conn) -> None:
        for ts in older:
            conn.execute("INSERT INTO samples (series_id, ts, value) SELECT MIN(id), %s, 1.0 "
                         "FROM series ON CONFLICT DO NOTHING", (ts,))
        conn.execute("SELECT drop_chunks('samples', older_than => %s)", (BASE_MS,))

    log = await ingest_during(s, drop_pass)
    assert not log["errors"], log["errors"]
    assert log["runs"] > 0
    total = (await s.fetchall("SELECT COUNT(*) FROM samples WHERE ts >= ?", (BASE_MS,)))[0][0]
    assert total == (ROUNDS + 1) * DAYS * METRICS


def wait_for(condition, what: str, timeout: float = 20.0) -> None:
    """Wait for a state of the database that another connection is about to reach."""
    gate = threading.Event()
    waited = 0.0
    while not condition():
        if waited >= timeout:
            raise AssertionError(f"timed out waiting for {what}")
        gate.wait(0.02)
        waited += 0.02


def test_a_real_deadlock_with_a_compression_job_is_retried_and_loses_nothing(timescale):
    """The cycle that failed in CI, made on purpose. The writer reads chunk B (a weak lock on
    it) and the compression job locks chunk A and then waits for chunk B. When the writer goes
    on to insert into chunk A, each waits for the other, and PostgreSQL ends the writer's
    transaction (its deadlock timeout is short, so it is the one that notices). The write must
    run again, after the job has finished, and store every point."""
    s = timescale
    day_a, day_b = BASE_MS, BASE_MS + DAY_MS
    seed = [series.Point("job", "m", "1", "{}", ts, 1.0) for ts in (day_a, day_b)]
    s.write_sync(lambda db: series.record_points(
        db, kind="host", name="h", points=seed, now=0.0, rollups=False), touches=("metrics",))
    with psycopg.connect(s._conninfo, autocommit=True) as conn:
        assert len(chunk_names(conn)) == 2
    with psycopg.connect(s._conninfo, autocommit=True) as probe:
        try:
            with probe.transaction():
                probe.execute("SET LOCAL deadlock_timeout = '5ms'")
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("the test role may not set deadlock_timeout")

    job_pid: list[int] = []
    job_done = threading.Event()
    job_error: list[BaseException] = []
    go = threading.Event()

    def job() -> None:
        try:
            with psycopg.connect(s._conninfo, autocommit=True) as conn:
                job_pid.append(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
                go.wait(30)
                # One statement, so the lock on the first chunk is held while it waits for the next.
                conn.execute("SELECT compress_chunk(format('%I.%I', chunk_schema, chunk_name)::"
                             "regclass) FROM timescaledb_information.chunks WHERE "
                             "hypertable_schema = current_schema() AND hypertable_name = 'samples' "
                             "ORDER BY range_start")
        except BaseException as err:  # noqa: BLE001 - reported by the test thread
            job_error.append(err)
        finally:
            job_done.set()

    thread = threading.Thread(target=job)
    thread.start()
    wait_for(lambda: bool(job_pid), "the job connection")

    def job_is_waiting() -> bool:
        row = probe_conn.execute("SELECT COUNT(*) FROM pg_locks WHERE pid = %s AND NOT granted",
                                 (job_pid[0],)).fetchone()
        return row[0] > 0

    runs: list[int] = []
    fresh = [series.Point("job", "m", "1", "{}", ts + 1000, 2.0) for ts in (day_a, day_b)]

    def unit(db):
        runs.append(1)
        if len(runs) == 1:
            db.execute("SET LOCAL deadlock_timeout = '5ms'")
            db.execute("SELECT COUNT(*) FROM samples WHERE ts >= ? AND ts < ?",
                       (day_b, day_b + DAY_MS)).fetchall()
            go.set()
            wait_for(job_is_waiting, "the job to wait for the writer's chunk")
        return series.record_points(db, kind="host", name="h", points=fresh, now=0.0,
                                    rollups=False)

    with psycopg.connect(s._conninfo, autocommit=True) as probe_conn:
        try:
            got = s.write_sync(unit, touches=("metrics",))
        finally:
            go.set()
            thread.join(timeout=60)
    assert not thread.is_alive()
    assert not job_error, job_error
    assert len(runs) == 2  # the first run lost the deadlock, the second stored the points
    assert got.new == 2
    total = s.read_sync(lambda db: db.execute("SELECT COUNT(*) FROM samples").fetchone()[0])
    assert total == 4
