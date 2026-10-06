"""The retention levels and compaction, on SQLite with a fake clock (docs/DATA-API-DESIGN.md
section 10.2). The same behaviour is checked on PostgreSQL by the contract cases in
test_series.py, which run in CI."""

from __future__ import annotations

import pytest

from observe.storage import compaction, open_storage
from observe.storage.rollups import RetentionLevels, load_levels

from .dbq import put

DAY = 86400


@pytest.fixture
def db(tmp_path):
    s = open_storage(str(tmp_path / "l.db"))
    yield s
    s.close()


async def one(s, ts, value, metric="temp", host="h"):
    await put(s, [(ts, host, "cpu", metric, "{}", value, "C")])


async def count(s, table):
    return (await s.fetchall(f"SELECT COUNT(*) FROM {table}"))[0][0]


async def test_the_levels_are_kept_current_as_samples_arrive(db):
    day = 20 * DAY
    for offset, value in ((100, 1.0), (4000, 3.0), (50000, 8.0)):
        await one(db, day + offset, value)
    assert await db.fetchall("SELECT bucket, n, min_v, max_v, avg_v FROM metric_daily") == [
        (day, 3, 1.0, 8.0, 4.0)]
    assert [r[0] for r in await db.fetchall("SELECT bucket FROM metric_hourly ORDER BY bucket")
            ] == [day, day + 3600, day + 13 * 3600]


async def test_a_missing_value_counts_toward_nothing(db):
    base = 1_000_000 // 300 * 300
    await one(db, base + 10, None)
    await one(db, base + 20, 4.0)
    assert await db.fetchall("SELECT n, avg_v FROM metric_5m") == [(1, 4.0)]
    assert await count(db, "samples") == 2  # the gap itself stays visible in the raw rows


async def test_retention_trims_each_level_at_its_own_age_and_keeps_history(db):
    now = 800 * DAY
    for age, value in ((1, 1.0), (20, 2.0), (100, 3.0), (800 - 1, 4.0)):
        await one(db, now - age * DAY, value)
    await db.execute("INSERT INTO events (monitor, ts, previous, current) VALUES ('m', ?, 'up', 'down')",
                     (now - 700 * DAY,))
    await db.execute("INSERT INTO events (monitor, ts, previous, current) VALUES ('m', ?, 'up', 'down')",
                     (now - 760 * DAY,))
    await db.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    assert await count(db, "samples") == 1          # 7 days raw
    assert await count(db, "metric_5m") == 1        # 14 days
    assert await count(db, "metric_hourly") == 2    # 90 days
    assert await count(db, "metric_daily") == 3     # 730 days
    assert await count(db, "availability_history") == 1  # 2 years


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


def test_cuts_are_aligned_to_the_width_of_the_level_that_covers_them():
    cuts = compaction.cuts_for(RetentionLevels(), "m", 800 * DAY + 12345)
    assert cuts.raw % 300_000 == 0 and cuts.m5 % 3_600_000 == 0
    assert cuts.h1 % 86_400_000 == 0 and cuts.d1 % 86_400_000 == 0
    # Never past the retention, always at most one covering bucket early.
    assert 0 <= (800 * DAY + 12345 - 7 * DAY) * 1000 - cuts.raw < 300_000


# ---- compaction verifies coverage and works in chunks ---------------------------------------

async def test_compaction_keeps_the_coverage_of_what_it_removes(db):
    now = 800 * DAY
    for age in range(1, 40):
        for hour in (0, 1, 13):
            await one(db, now - age * DAY + hour * 3600 + 5, float(age + hour))
    total = 39 * 3
    await db.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    cuts = compaction.cuts_for(RetentionLevels(), "temp", now)
    # Each level still holds, from its cut on, exactly the points of that range.
    kept_raw = (await db.fetchall("SELECT COUNT(*) FROM samples"))[0][0]
    assert kept_raw == sum(1 for age in range(1, 40) for hour in (0, 1, 13)
                           if (now - age * DAY + hour * 3600 + 5) * 1000 >= cuts.raw)
    from_cut = (await db.fetchall("SELECT SUM(n) FROM rollup_5m"))[0][0]
    assert from_cut == sum(1 for age in range(1, 40) for hour in (0, 1, 13)
                           if (now - age * DAY + hour * 3600 + 5) * 1000 >= cuts.m5)
    # The hourly and daily levels lose nothing at these ages.
    assert (await db.fetchall("SELECT SUM(n) FROM rollup_1h"))[0][0] == total
    assert (await db.fetchall("SELECT SUM(n) FROM rollup_1d"))[0][0] == total
    # The finest level still holding a range covers it in full.
    covered = await db.fetchall(
        "SELECT COUNT(*) FROM samples WHERE ts < ?", (cuts.m5,))
    assert covered == [(0,)]
    state = dict((r[0], r) for r in await db.fetchall(
        "SELECT level, upto, last_rows, last_error FROM rollup_state"))
    assert state["raw"][2] > 0 and state["raw"][3] == ""


async def test_a_series_whose_summaries_do_not_cover_it_is_left_alone(db):
    now = 800 * DAY
    await one(db, now - 10 * DAY, 1.0, metric="broken")
    await one(db, now - 10 * DAY, 2.0, metric="fine")
    await db.execute("DELETE FROM rollup_5m WHERE series_id = "
                     "(SELECT id FROM series WHERE metric = 'broken')")
    await db.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    left = await db.fetchall("SELECT s.metric FROM samples a JOIN series s ON s.id = a.series_id")
    assert left == [("broken",)]
    (error,) = await db.fetchall("SELECT last_error FROM rollup_state WHERE level = 'raw'")
    assert "1 series kept" in error[0]


async def test_a_summary_with_a_wrong_sum_or_extreme_is_not_trusted(db):
    now = 800 * DAY
    await one(db, now - 10 * DAY, 5.0, metric="badsum")
    await one(db, now - 10 * DAY, 5.0, metric="badmin")
    await db.execute("UPDATE rollup_5m SET sum_v = 99 WHERE series_id = "
                     "(SELECT id FROM series WHERE metric = 'badsum')")
    await db.execute("UPDATE rollup_5m SET min_v = 6 WHERE series_id = "
                     "(SELECT id FROM series WHERE metric = 'badmin')")
    await db.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    left = await db.fetchall("SELECT s.metric FROM samples a JOIN series s ON s.id = a.series_id "
                             "ORDER BY 1")
    assert left == [("badmin",), ("badsum",)]


async def test_a_five_minute_row_is_not_trimmed_without_the_hourly_row_that_covers_it(db):
    now = 800 * DAY
    await one(db, now - 20 * DAY, 1.0)
    await db.execute("DELETE FROM rollup_1h")
    await db.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    assert await count(db, "rollup_5m") == 1
    (error,) = await db.fetchall("SELECT last_error FROM rollup_state WHERE level = '5m'")
    assert "series kept" in error[0]
    # With the cover back, the next pass trims it.
    await db.execute("INSERT INTO rollup_1h (series_id, bucket, n, sum_v, min_v, max_v) "
                     "SELECT series_id, bucket / 3600000 * 3600000, SUM(n), SUM(sum_v), MIN(min_v), "
                     "MAX(max_v) FROM rollup_5m GROUP BY series_id, bucket / 3600000 * 3600000")
    await db.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    assert await count(db, "rollup_5m") == 0


async def test_deletes_are_chunked_so_no_unit_runs_long(db, monkeypatch):
    monkeypatch.setattr(compaction, "CHUNK_ROWS", 40)
    units = []
    original = compaction._compact_unit

    def spy(conn, level, items, pos):
        out = original(conn, level, items, pos)
        units.append((level, out[1]))
        return out

    monkeypatch.setattr(compaction, "_compact_unit", spy)
    now = 800 * DAY
    await put(db, [(now - 30 * DAY + i, "h", "cpu", "temp", "{}", 1.0, "C") for i in range(130)])
    await db.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    assert await count(db, "samples") == 0
    raw = [rows for level, rows in units if level == "raw"]
    assert max(raw) <= 40 and sum(raw) == 130 and len(raw) >= 4


async def test_an_override_trims_only_its_own_metric(db):
    now = 800 * DAY
    for metric in ("plain", "kept"):
        await one(db, now - 20 * DAY, 1.0, metric=metric)
    await db.execute("INSERT INTO app_settings (key, value, updated) VALUES "
                     "('retention.overrides', '{\"kept\": {\"rollup_5m_days\": 40, \"raw_days\": 30}}', 1)")
    await db.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    assert await db.fetchall("SELECT scope, metric FROM metric_5m") == [("cpu", "kept")]
    assert [r[0] for r in await db.fetchall(
        "SELECT s.metric FROM samples a JOIN series s ON s.id = a.series_id")] == ["kept"]
    assert await db.fetchall("SELECT DISTINCT metric FROM metric_hourly ORDER BY 1") == [
        ("kept",), ("plain",)]
