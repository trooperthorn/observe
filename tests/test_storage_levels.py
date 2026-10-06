"""The shared incremental rollups and the retention levels, on SQLite with a fake clock
(docs/DATA-API-DESIGN.md section 10.2). The same behaviour is checked on PostgreSQL by the
contract cases in test_storage.py, which run in CI."""

from __future__ import annotations

import pytest

from observe.storage import open_storage
from observe.storage.rollups import (LATE_GRACE_S, RetentionLevels, cutoffs, load_levels)

DAY = 86400


@pytest.fixture
def db(tmp_path):
    s = open_storage(str(tmp_path / "l.db"))
    yield s
    s.close()


async def put(s, ts, value, metric="temp"):
    await s.execute("INSERT INTO host_samples (ts, host, source, metric, labels, value, unit) "
                    "VALUES (?, 'h', 'cpu', ?, '{}', ?, 'C')", (ts, metric, value))


async def test_the_daily_level_is_built_from_the_hourly_level(db):
    day = 20 * DAY
    for offset, value in ((100, 1.0), (4000, 3.0), (50000, 8.0)):
        await put(db, day + offset, value)
    await db.rollup(day + 2 * DAY)
    assert await db.fetchall("SELECT bucket, n, min_v, max_v, avg_v FROM metric_daily") == [
        (day, 3, 1.0, 8.0, 4.0)]
    assert [r[0] for r in await db.fetchall("SELECT bucket FROM metric_hourly ORDER BY bucket")
            ] == [day, day + 3600, day + 13 * 3600]


async def test_only_complete_buckets_are_folded(db):
    base = 1_000_000 // 300 * 300
    await put(db, base + 10, 1.0)
    await db.rollup(base + 300 + LATE_GRACE_S - 1)  # the bucket ended less than the grace ago
    assert await db.fetchall("SELECT COUNT(*) FROM metric_5m") == [(0,)]
    await db.rollup(base + 300 + LATE_GRACE_S)
    assert await db.fetchall("SELECT n FROM metric_5m") == [(1,)]


async def test_a_sample_that_arrives_after_its_bucket_was_folded_stays_raw_only(db):
    base = 1_000_000 // 300 * 300
    await put(db, base + 10, 1.0)
    await db.rollup(base + 1000)
    await put(db, base + 20, 2.0)  # late
    await db.rollup(base + 2000)
    assert await db.fetchall("SELECT n, sum_v FROM metric_5m") == [(1, 1.0)]
    assert await db.fetchall("SELECT COUNT(*) FROM host_samples") == [(2,)]


async def test_a_missing_value_counts_toward_nothing(db):
    base = 1_000_000 // 300 * 300
    await put(db, base + 10, None)
    await put(db, base + 20, 4.0)
    await db.rollup(base + 1000)
    assert await db.fetchall("SELECT n, avg_v FROM metric_5m") == [(1, 4.0)]


async def test_the_rollup_state_records_the_last_run(db):
    base = 1_000_000 // 300 * 300
    await put(db, base + 10, 1.0)
    await db.rollup(base + 1000)
    rows = await db.fetchall("SELECT upto, last_rows FROM rollup_state WHERE level = '5m'")
    assert rows == [(base + 900, 1)]


async def test_retention_trims_each_level_at_its_own_age_and_keeps_history(db):
    now = 800 * DAY
    for age, value in ((1, 1.0), (20, 2.0), (100, 3.0), (800 - 1, 4.0)):
        await put(db, now - age * DAY, value)
    await db.rollup(now)
    await db.execute("INSERT INTO events (monitor, ts, previous, current) VALUES ('m', ?, 'up', 'down')",
                     (now - 700 * DAY,))
    await db.execute("INSERT INTO events (monitor, ts, previous, current) VALUES ('m', ?, 'up', 'down')",
                     (now - 760 * DAY,))
    await db.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    assert await db.fetchall("SELECT COUNT(*) FROM host_samples") == [(1,)]        # 7 days raw
    assert await db.fetchall("SELECT COUNT(*) FROM metric_5m") == [(1,)]            # 14 days
    assert await db.fetchall("SELECT COUNT(*) FROM metric_hourly") == [(2,)]        # 90 days
    assert await db.fetchall("SELECT COUNT(*) FROM metric_daily") == [(2,)]         # 730 days; today is not complete
    assert await db.fetchall("SELECT COUNT(*) FROM availability_history") == [(1,)]  # 2 years


async def test_levels_come_from_the_admin_settings_and_bad_values_fall_back(db):
    for key, value in (("retention.raw_days", "3"), ("retention.hourly_days", "120"),
                       ("retention.daily_days", "9"), ("retention.5m_days", "999"),
                       ("retention.history_days", "x"), ("retention.other", "1")):
        await db.execute("INSERT INTO app_settings (key, value, updated) VALUES (?, ?, 1)",
                         (key, value))
    levels = await db.read(lambda c: load_levels(c, 30))
    assert levels == RetentionLevels(raw_days=3, rollup_5m_days=14, hourly_days=120,
                                     daily_days=9, history_days=730)


async def test_the_raw_level_falls_back_to_the_server_setting(db):
    assert (await db.read(lambda c: load_levels(c, 30))).raw_days == 30
    assert (await db.read(lambda c: load_levels(c))).raw_days == 7


async def test_a_level_is_never_trimmed_past_what_the_next_level_has_folded(db):
    levels = RetentionLevels()
    cut = await db.read(lambda c: cutoffs(c, 100 * DAY, levels))
    assert cut["raw_samples"] == 0 and cut["rollup_5m"] == 0 and cut["rollup_1h"] == 0
    assert cut["rollup_1d"] == 100 * DAY - 730 * DAY
