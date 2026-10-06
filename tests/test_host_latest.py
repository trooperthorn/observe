"""Latest-value reads for pushed hosts stay bounded and the page timers never overlap."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from observe.checks.host import LATEST_WINDOW_S
from observe.store import Store

NOW = 1_000_000.0
STATIC = Path(__file__).resolve().parent.parent / "observe" / "static"


def seed(store: Store, old_rows: int, host: str = "nas01") -> None:
    db = store._db
    db.execute("INSERT INTO hosts (host, first_seen, last_seen, platform, agent_version) VALUES (?,?,?,?,?)",
               (host, NOW - 99, NOW, "linux", "1"))
    rows = []
    for i in range(old_rows):
        rows.append((NOW - 10 * 86400 + i, host, "hwmon", "cpu_temp_c", "{}", 40.0 + i % 5, "C"))
    for i in range(5):
        rows.append((NOW - 100 + i * 10, host, "hwmon", "cpu_temp_c", "{}", 60.0 + i, "C"))
        rows.append((NOW - 100 + i * 10, host, "zfs", "pool_used", json.dumps({"p": "a"}), 5.0 + i, "%"))
    db.executemany("INSERT INTO host_samples (ts, host, source, metric, labels, value, unit) "
                   "VALUES (?,?,?,?,?,?,?)", rows)
    db.commit()


async def test_windowed_latest_matches_unbounded(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    seed(store, 200)
    full = await store.latest_host("nas01")
    bounded = await store.latest_host("nas01", window=LATEST_WINDOW_S, now=NOW)
    assert bounded == full
    assert len(full["samples"]) == 2
    store.close()


async def test_series_silent_beyond_the_window_is_left_out(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    seed(store, 3)
    bounded = await store.latest_host("nas01", window=70, now=NOW)
    assert {s["source"] for s in bounded["samples"]} == {"hwmon", "zfs"}
    bounded = await store.latest_host("nas01", window=1, now=NOW + 1000)
    assert len(bounded["samples"]) == 1  # nothing in the window: the newest row only
    assert bounded["samples"][0]["ts"] == NOW - 60
    store.close()


async def test_named_silent_series_is_returned_with_its_old_reading(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    seed(store, 3)
    store._db.execute("INSERT INTO host_samples (ts, host, source, metric, labels, value, unit) "
                      "VALUES (?,?,?,?,?,?,?)", (NOW - 5000, "nas01", "fan", "rpm", "{}", 900.0, "rpm"))
    store._db.commit()
    plain = await store.latest_host("nas01", window=LATEST_WINDOW_S, now=NOW)
    assert "fan" not in {s["source"] for s in plain["samples"]}
    named = await store.latest_host("nas01", window=LATEST_WINDOW_S, now=NOW,
                                    series=(("fan", "rpm"), ("none", "never")))
    unbounded = await store.latest_host("nas01")
    key = lambda s: (s["source"], s["metric"])
    assert sorted(named["samples"], key=key) == sorted(unbounded["samples"], key=key)  # the silent series is kept, a never-seen one adds nothing
    store.close()


def test_a_version_14_database_with_samples_gains_both_indexes(tmp_path):
    from observe.store import MIGRATIONS, migrate
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    newer = {v: m for v, m in MIGRATIONS.items() if v > 14}
    for v in newer:
        del MIGRATIONS[v]
    try:
        migrate(db)
    finally:
        MIGRATIONS.update(newer)
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 14
    db.execute("INSERT INTO host_samples (ts, host, source, metric, labels, value, unit) "
               "VALUES (1.0,'nas01','hwmon','t','{}',1.0,'C')")
    db.commit()
    db.close()
    Store(path).close()
    db = sqlite3.connect(path)
    try:
        names = {r[1] for r in db.execute("PRAGMA index_list(host_samples)")}
        assert {"host_samples_host_ts", "host_samples_series"} <= names
        assert db.execute("SELECT COUNT(*) FROM host_samples").fetchone() == (1,)
    finally:
        db.close()


def test_host_ts_index_exists_after_migration(tmp_path):
    path = str(tmp_path / "w.db")
    Store(path).close()
    db = sqlite3.connect(path)
    cols = [r[2] for r in db.execute("PRAGMA index_info(host_samples_host_ts)")]
    db.close()
    assert cols == ["host", "ts"]


def _steps(store: Store) -> int:
    count = 0

    def tick() -> int:
        nonlocal count
        count += 1
        return 0

    store._db.set_progress_handler(tick, 100)
    try:
        store._latest_host_sync("nas01", NOW - LATEST_WINDOW_S)
    finally:
        store._db.set_progress_handler(None, 0)
    return count


def test_bounded_read_does_not_scale_with_retention(tmp_path):
    small, large = Store(str(tmp_path / "a.db")), Store(str(tmp_path / "b.db"))
    seed(small, 500)
    seed(large, 20_000)
    plan = " ".join(r[3] for r in large._db.execute(
        "EXPLAIN QUERY PLAN SELECT source, metric, labels, value, unit, ts FROM host_samples "
        "WHERE host=? AND ts>=?", ("nas01", NOW - 900)))
    assert "host_samples_host_ts" in plan
    a, b = _steps(small), _steps(large)
    assert b <= a + 20, (a, b)
    small.close()
    large.close()


def test_page_refresh_never_overlaps():
    for name in ("app.js", "host.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        body = re.search(r"async function refresh\(\) \{(.*?)\n\}\n", text, re.S).group(1)
        assert "if (refreshing) return;" in body
        assert body.index("refreshing = true") < body.index("fetch(")
        assert "finally" in body and "refreshing = false" in body
