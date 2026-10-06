"""Latest-value reads for pushed hosts stay bounded and the page timers never overlap."""

from __future__ import annotations

import json
import re
from pathlib import Path

from observe.checks.host import LATEST_WINDOW_S
from observe.store import Store

from .dbq import put_samples

NOW = 1_000_000.0
STATIC = Path(__file__).resolve().parent.parent / "observe" / "static"


def seed(store: Store, old_rows: int, host: str = "nas01") -> None:
    store.storage.write_sync(lambda db: _seed(db, old_rows, host))


def _seed(db, old_rows: int, host: str) -> None:
    db.execute("INSERT INTO hosts (host, first_seen, last_seen, platform, agent_version) VALUES (?,?,?,?,?)",
               (host, NOW - 99, NOW, "linux", "1"))
    rows = []
    for i in range(old_rows):
        rows.append((NOW - 10 * 86400 + i, host, "hwmon", "cpu_temp_c", "{}", 40.0 + i % 5, "C"))
    for i in range(5):
        rows.append((NOW - 100 + i * 10, host, "hwmon", "cpu_temp_c", "{}", 60.0 + i, "C"))
        rows.append((NOW - 100 + i * 10, host, "zfs", "pool_used", json.dumps({"p": "a"}), 5.0 + i, "%"))
    put_samples(db, rows)


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
    store.storage.write_sync(lambda db: put_samples(
        db, [(NOW - 5000, "nas01", "fan", "rpm", "{}", 900.0, "rpm")]))
    plain = await store.latest_host("nas01", window=LATEST_WINDOW_S, now=NOW)
    assert "fan" not in {s["source"] for s in plain["samples"]}
    named = await store.latest_host("nas01", window=LATEST_WINDOW_S, now=NOW,
                                    series=(("fan", "rpm"), ("none", "never")))
    unbounded = await store.latest_host("nas01")
    key = lambda s: (s["source"], s["metric"])
    assert sorted(named["samples"], key=key) == sorted(unbounded["samples"], key=key)  # the silent series is kept, a never-seen one adds nothing
    store.close()


def test_a_new_database_has_no_host_samples_table(tmp_path):
    import sqlite3
    path = str(tmp_path / "w.db")
    Store(path).close()
    db = sqlite3.connect(path)
    try:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    finally:
        db.close()
    assert "host_samples" not in names
    assert {"resources", "scopes", "series", "samples", "latest"} <= names


def _steps(store: Store) -> int:
    count = 0

    def tick() -> int:
        nonlocal count
        count += 1
        return 0

    def counted(db) -> None:
        db.set_progress_handler(tick, 100)
        try:
            Store._latest_host_unit(db, "nas01", NOW - LATEST_WINDOW_S, False, ())
        finally:
            db.set_progress_handler(None, 0)

    store.storage.write_sync(counted)
    return count


def test_bounded_read_does_not_scale_with_retention(tmp_path):
    small, large = Store(str(tmp_path / "a.db")), Store(str(tmp_path / "b.db"))
    seed(small, 500)
    seed(large, 20_000)
    plan = " ".join(r[3] for r in large.storage.read_sync(lambda db: db.execute(
        "EXPLAIN QUERY PLAN SELECT sc.name, s.metric, s.attrs, l.value, s.unit, l.ts FROM latest l "
        "JOIN series s ON s.id = l.series_id JOIN scopes sc ON sc.id = s.scope_id "
        "WHERE s.resource_id=? AND l.ts>=?", (1, int((NOW - 900) * 1000))).fetchall()))
    assert "series_resource_metric" in plan and "SCAN samples" not in plan
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
