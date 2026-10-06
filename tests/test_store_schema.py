"""Versioned store schema: fresh create, migration, refusal, rerun, retention."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from observe.storage.schema import MIGRATIONS, SCHEMA_VERSION, SchemaTooNewError, migrate
from observe.store import Store
from .dbq import put_samples, run_sql

NEW_TABLES = {"schema_version", "hosts", "resources", "scopes", "series", "samples", "latest",
              "host_sources", "host_events",
              "ingest_keys", "users", "sessions", "audit"}
DAY = 86400


def tables(path: str) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()


def version(path: str) -> int:
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        db.close()


def test_fresh_database_has_all_tables(tmp_path):
    path = str(tmp_path / "w.db")
    Store(path).close()
    assert NEW_TABLES | {"results", "events"} <= tables(path)
    assert version(path) == SCHEMA_VERSION


def make_main_schema_db(path: str) -> None:
    """A database exactly as the current main schema creates it, with rows."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE results (monitor TEXT NOT NULL, ts REAL NOT NULL, result TEXT NOT NULL,
            value REAL, latency_ms REAL, message TEXT);
        CREATE INDEX results_monitor_ts ON results(monitor, ts);
        CREATE TABLE events (monitor TEXT NOT NULL, ts REAL NOT NULL, previous TEXT NOT NULL,
            current TEXT NOT NULL, message TEXT);
        CREATE INDEX events_ts ON events(ts);
        """)
    now = time.time()
    db.execute("INSERT INTO results VALUES ('a', ?, 'ok', 1.5, 2.0, 'fine')", (now,))
    db.execute("INSERT INTO events VALUES ('a', ?, 'up', 'down', 'boom')", (now,))
    db.commit()
    db.close()


def test_migrates_main_schema_database_keeping_rows(tmp_path):
    path = str(tmp_path / "w.db")
    make_main_schema_db(path)
    store = Store(path)
    hist = asyncio.run(store.history("a", 1))
    evs = asyncio.run(store.events())
    store.close()
    assert [h["message"] for h in hist] == ["fine"]
    assert [e["message"] for e in evs] == ["boom"]
    assert NEW_TABLES <= tables(path)
    assert version(path) == SCHEMA_VERSION


def test_newer_schema_is_refused_and_untouched(tmp_path):
    path = str(tmp_path / "w.db")
    Store(path).close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION + 1,))
    db.commit()
    db.close()
    with pytest.raises(SchemaTooNewError):
        Store(path)
    assert version(path) == SCHEMA_VERSION + 1


def test_rerunning_migrations_is_safe(tmp_path):
    path = str(tmp_path / "w.db")
    make_main_schema_db(path)
    Store(path).close()
    Store(path).close()  # reopening runs nothing new
    db = sqlite3.connect(path)
    # Force every step to run again against the finished schema.
    db.execute("DELETE FROM schema_version")
    db.commit()
    migrate(db)
    versions = [r[0] for r in db.execute("SELECT version FROM schema_version ORDER BY version")]
    rows = db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    db.close()
    assert versions == sorted(MIGRATIONS)
    assert rows == 1


def test_failed_step_rolls_back(tmp_path, monkeypatch):
    db = sqlite3.connect(str(tmp_path / "w.db"))
    migrate(db)
    monkeypatch.setitem(MIGRATIONS, SCHEMA_VERSION + 1, (
        "CREATE TABLE half_done (x INTEGER)", "THIS IS NOT SQL"))
    with pytest.raises(sqlite3.OperationalError):
        migrate(db)
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    top = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    db.close()
    assert "half_done" not in names
    assert top == SCHEMA_VERSION


def test_prune_applies_retention(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    now = time.time()
    old, recent, very_old, audit_old = now - 40 * DAY, now - DAY, now - 800 * DAY, now - 100 * DAY
    store.storage.write_sync(lambda db: put_samples(
        db, [(ts, "h", "cpu", "temp", "{}", 1.0, "") for ts in (old, recent)]))
    for ts in (very_old, old):
        run_sql(store, "INSERT INTO host_events (host, ts, kind, severity, source, title, dedup_key) "
                    "VALUES ('h', ?, 'boot', 'info', 'agent', 't', ?)", (ts, str(ts)))
    for ts in (audit_old, recent):
        run_sql(store, "INSERT INTO audit (ts, actor, kind) VALUES (?, 'admin', 'login')", (ts,))
    run_sql(store, "INSERT INTO users (username, hash, created) VALUES ('u', 'x', ?)", (now,))
    for sid, exp in (("dead", now - 10), ("live", now + 3600)):
        run_sql(store, "INSERT INTO sessions VALUES (?, 1, 'c', ?, ?, ?, 0)", (sid, now, exp, now))
    run_sql(store, "INSERT INTO results VALUES ('m', ?, 'ok', 1, 1, '')", (old,))

    removed = asyncio.run(store.prune(30, audit_retention_days=60))

    def count(table: str) -> int:
        return run_sql(store, f"SELECT COUNT(*) FROM {table}")[0][0]

    assert removed == 1
    assert count("samples") == 1  # the 40 day old sample is gone at 30 days
    assert [r[0] for r in run_sql(store, "SELECT ts FROM host_events")] == [old]  # kept for the two year history level
    assert count("audit") == 1  # audit uses its own, shorter window here
    assert [r[0] for r in run_sql(store, "SELECT id_hash FROM sessions")] == ["live"]
    store.close()


def test_audit_retention_is_independent_of_sample_retention(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    now = time.time()
    run_sql(store, "INSERT INTO audit (ts, actor, kind) VALUES (?, 'a', 'login')", (now - 100 * DAY,))
    asyncio.run(store.prune(7))  # default audit retention is a year
    assert run_sql(store, "SELECT COUNT(*) FROM audit")[0][0] == 1
    store.close()


def test_history_over_the_row_limit_returns_the_newest_rows_in_time_order(tmp_path):
    store = Store(str(tmp_path / "h.db"))
    now = time.time()
    for i in range(10):
        db_ts = now - (9 - i) * 60
        run_sql(store, "INSERT INTO results VALUES ('a', ?, 'ok', ?, 1.0, '')", (db_ts, float(i)))
    hist = asyncio.run(store.history("a", 24 * 30, limit=4))
    store.close()
    assert [h["value"] for h in hist] == [6.0, 7.0, 8.0, 9.0]
    assert [h["ts"] for h in hist] == sorted(h["ts"] for h in hist)
