"""Series storage on every backend (docs/DATA-API-DESIGN.md sections 2 and 10.2): rollups against
raw data, replay and out-of-order delivery, compaction that keeps coverage, and the summary views.

The `storage` fixture runs each case on SQLite and, when OBSERVE_TEST_PG_DSN is set, on PostgreSQL
(plain or TimescaleDB, as OBSERVE_TEST_PG_TIMESCALE says). On TimescaleDB the summary levels are
continuous aggregates, so each case asks `settle` to refresh them before it reads."""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import math
import random
import time

import pytest

from observe.storage import compaction, pg_timescale, series
from observe.storage.postgres import PgStorage
from observe.storage.rollups import RetentionLevels

from .dbq import put, run_summary_policies, settle
from .test_storage import storage  # noqa: F401  (the fixture)

LEVELS = (("rollup_5m", 300_000), ("rollup_1h", 3_600_000), ("rollup_1d", 86_400_000))
BASE = 1_700_000_000  # a fixed time: nothing here depends on the clock
DAY = 86400


def row(ts, metric, value, scope="cpu", host="h", labels="{}", unit="C"):
    return (ts, host, scope, metric, labels, value, unit)


def expected(points, width):
    """{(metric, bucket): (n, sum, min, max)} computed from the raw points the slow way."""
    out: dict[tuple[str, int], list] = {}
    for metric, ts_ms, value in points:
        if value is None:
            continue
        bucket = ts_ms // width * width
        agg = out.setdefault((metric, bucket), [0, 0.0, math.inf, -math.inf])
        agg[0] += 1
        agg[1] += value
        agg[2] = min(agg[2], value)
        agg[3] = max(agg[3], value)
    return {k: tuple(v) for k, v in out.items()}


async def level_rows(storage, table):
    rows = await storage.fetchall(
        f"SELECT s.metric, r.bucket, r.n, r.sum_v, r.min_v, r.max_v FROM {table} r "
        "JOIN series s ON s.id = r.series_id")
    return {(m, int(b)): (int(n), s, lo, hi) for m, b, n, s, lo, hi in rows}


def same_levels(got, want):
    assert got.keys() == want.keys()
    for key, (n, total, lo, hi) in want.items():
        g = got[key]
        assert g[0] == n and g[2] == lo and g[3] == hi, key
        assert math.isclose(g[1], total, rel_tol=1e-9, abs_tol=1e-9), key


async def assert_levels_match(storage, points):
    await settle(storage)
    for table, width in LEVELS:
        same_levels(await level_rows(storage, table), expected(points, width))


async def snapshot(storage):
    await settle(storage)
    parts = []
    for sql in ("SELECT s.metric, a.ts, a.value FROM samples a JOIN series s ON s.id = a.series_id "
                "ORDER BY s.metric, a.ts",
                "SELECT s.metric, l.ts, l.value, l.prev_ts, l.prev_value FROM latest l "
                "JOIN series s ON s.id = l.series_id ORDER BY s.metric"):
        parts.append(await storage.fetchall(sql))
    for table, _ in LEVELS:
        parts.append(sorted((m, b, n, lo, hi) for (m, b), (n, _, lo, hi)
                            in (await level_rows(storage, table)).items()))
    return parts


def random_points(seed, metrics=("m0", "m1", "m2"), per_metric=400, days=3):
    rnd = random.Random(seed)
    out = []
    for metric in metrics:
        stamps = rnd.sample(range(days * DAY * 1000), per_metric)
        for ms in stamps:
            value = None if rnd.random() < 0.1 else round(rnd.uniform(-50, 150), 3)
            out.append((metric, BASE * 1000 + ms, value))
    return out


# ---- rollups against raw data ---------------------------------------------------------------

@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_every_level_matches_the_raw_samples_on_random_data(storage, seed):
    points = random_points(seed)
    shuffled = points[:]
    random.Random(seed).shuffle(shuffled)  # out of order, in several batches
    cut = 0
    for size in (13, 120, 400, 1, 300, 10_000):
        batch = shuffled[cut:cut + size]
        cut += size
        if batch:
            await put(storage, [row(ts / 1000, m, v) for m, ts, v in batch])
    await assert_levels_match(storage, points)
    raw = await storage.fetchall("SELECT COUNT(*), COUNT(value) FROM samples")
    assert raw == [(len(points), sum(1 for p in points if p[2] is not None))]


async def test_a_point_with_no_value_is_stored_but_counts_toward_nothing(storage):
    await put(storage, [row(BASE + 1, "m", None), row(BASE + 2, "m", 4.0)])
    await settle(storage)
    assert await storage.fetchall("SELECT n, avg_v FROM metric_5m") == [(1, 4.0)]
    assert await storage.fetchall("SELECT COUNT(*) FROM samples") == [(2,)]
    assert await storage.fetchall("SELECT ts, value FROM latest") == [((BASE + 2) * 1000, 4.0)]


# ---- replay and out-of-order delivery -------------------------------------------------------

async def test_replaying_a_batch_in_any_order_changes_nothing(storage):
    rows = [row(BASE + i * 37, f"m{i % 3}", float(i)) for i in range(60)]
    await put(storage, rows)
    first = await snapshot(storage)
    await put(storage, rows)
    assert await snapshot(storage) == first
    await put(storage, rows[::-1])
    assert await snapshot(storage) == first
    await put(storage, rows[10:20])
    assert await snapshot(storage) == first


async def test_the_latest_row_does_not_depend_on_the_order_of_arrival(storage):
    stamps = [10, 20, 30, 40]
    for host, order in (("up", stamps), ("down", stamps[::-1]), ("mixed", [30, 10, 40, 20])):
        await put(storage, [row(BASE + t, "m", float(t), host=host) for t in order])
    rows = await storage.fetchall(
        "SELECT r.name, l.ts, l.value, l.prev_ts, l.prev_value FROM latest l "
        "JOIN series s ON s.id = l.series_id JOIN resources r ON r.id = s.resource_id "
        "ORDER BY r.name")
    newest = (BASE + 40) * 1000
    assert rows == [(name, newest, 40.0, (BASE + 30) * 1000, 30.0)
                    for name in ("down", "mixed", "up")]


async def test_recording_reports_new_duplicate_and_replaced_points(storage):
    def record(points):
        return storage.write(lambda db: series.record_points(
            db, kind="host", name="h", now=1.0, rollups=storage.incremental_rollups,
            points=[series.Point("cpu", "m", "C", "{}", ts, v) for ts, v in points]))

    assert dataclasses.astuple(await record([(1000, 1.0), (2000, 2.0)])) == (2, 0, 0, 0, 0)
    assert dataclasses.astuple(await record([(1000, 1.0), (3000, 3.0)])) == (1, 0, 1, 0, 0)
    assert dataclasses.astuple(await record([(2000, 9.0)])) == (0, 1, 0, 0, 0)
    assert await storage.fetchall("SELECT ts, value FROM samples ORDER BY ts") == [
        (1000, 1.0), (2000, 9.0), (3000, 3.0)]


async def test_a_point_sent_again_with_a_new_value_corrects_every_level(storage):
    base = BASE // 86400 * 86400 + 3600
    points = [("m", (base + t) * 1000, v) for t, v in
              ((10, 5.0), (20, 6.0), (400, 7.0), (4000, 8.0), (5000, None))]
    await put(storage, [row(ts / 1000, m, v) for m, ts, v in points])
    await assert_levels_match(storage, points)
    for change in ((20, 100.0), (10, None), (5000, -3.0), (4000, 8.5)):
        offset, value = change
        await put(storage, [row(base + offset, "m", value)])
        points = [(m, ts, value if ts == (base + offset) * 1000 else v) for m, ts, v in points]
        await assert_levels_match(storage, points)
    assert await storage.fetchall(
        "SELECT value FROM samples WHERE ts = ?", ((base + 20) * 1000,)) == [(100.0,)]
    assert await storage.fetchall("SELECT ts, value FROM latest") == [((base + 5000) * 1000, -3.0)]


# ---- identity -------------------------------------------------------------------------------

async def test_a_series_is_a_resource_a_scope_a_metric_and_the_attributes(storage):
    labels_ab = series.canonical({"a": "1", "b": "2"})
    assert labels_ab == series.canonical({"b": "2", "a": "1"})
    await put(storage, [row(BASE, "m", 1.0, labels='{"b": "2", "a": "1"}'),
                        row(BASE + 1, "m", 2.0, labels='{"a": "1", "b": "2"}'),
                        row(BASE, "m", 3.0, scope="other"),
                        row(BASE, "m", 4.0, host="h2")])
    assert await storage.fetchall("SELECT COUNT(*) FROM series") == [(3,)]
    assert await storage.fetchall("SELECT COUNT(*) FROM resources") == [(2,)]
    assert await storage.fetchall("SELECT kind, name FROM resources ORDER BY name") == [
        ("host", "h"), ("host", "h2")]
    assert await storage.fetchall("SELECT name FROM scopes ORDER BY name") == [("cpu",), ("other",)]
    with pytest.raises(ValueError):
        series.canonical({"a": ["x"]})


async def test_the_cardinality_guard_drops_points_of_a_series_over_the_cap(storage):
    def record(points, per, total):
        return storage.write(lambda db: series.record_points(
            db, kind="host", name="h", now=1.0, rollups=storage.incremental_rollups,
            max_per_resource=per, max_total=total,
            points=[series.Point("cpu", m, "", "{}", ts, 1.0) for m, ts in points]))

    got = await record([("a", 1), ("b", 1), ("c", 1), ("d", 1), ("a", 2)], per=3, total=100)
    assert (got.new, got.dropped) == (4, 1)
    assert await storage.fetchall("SELECT COUNT(*) FROM series") == [(3,)]
    await storage.write(lambda db: series.resource_id(db, "host", "other", 1.0))
    # An existing series is never refused; a new one counts toward the total.
    got = await record([("a", 3), ("z", 1)], per=10, total=3)
    assert (got.new, got.dropped) == (1, 1)


async def test_removing_a_resource_removes_its_series_samples_latest_and_summaries(storage):
    await put(storage, [row(BASE + i, "m", float(i)) for i in range(5)]
              + [row(BASE, "m", 1.0, host="keep")])
    removed = await storage.write(lambda db: series.remove_resource(
        db, "host", "h", rollups=storage.incremental_rollups))
    assert removed == 5
    await settle(storage)
    assert await storage.fetchall("SELECT COUNT(*) FROM samples") == [(1,)]
    assert await storage.fetchall("SELECT COUNT(*) FROM latest") == [(1,)]
    assert await storage.fetchall("SELECT resource FROM metric_5m") == [("keep",)]
    assert await storage.fetchall("SELECT name FROM resources") == [("keep",)]
    assert await storage.write(lambda db: series.remove_resource(
        db, "host", "gone", rollups=storage.incremental_rollups)) == 0


async def test_new_points_take_consecutive_insertion_numbers_and_replays_take_none(storage):
    await put(storage, [row(BASE + 50, "m", 1.0), row(BASE + 10, "m", 2.0)])
    await put(storage, [row(BASE + 50, "m", 1.0), row(BASE + 70, "m", 3.0)])  # one replay
    got = await storage.fetchall("SELECT ts, seq FROM samples ORDER BY seq")
    assert [seq for _, seq in got] == [1, 2, 3]
    assert [ts for ts, _ in got] == [(BASE + 50) * 1000, (BASE + 10) * 1000, (BASE + 70) * 1000]
    assert await storage.fetchall("SELECT seq FROM ingest_seq") == [(3,)]


async def test_churned_series_are_collected_after_retention_and_the_cap_frees(storage):
    now = time.time()
    old = now - 5000 * DAY  # far past every level's retention
    recent = now - 3600

    def record(host, metrics, ts, per=3):
        return storage.write(lambda db: series.record_points(
            db, kind="host", name=host, now=ts, rollups=storage.incremental_rollups,
            max_per_resource=per, max_total=100,
            points=[series.Point("cpu", m, "", "{}", int(ts * 1000), 1.0) for m in metrics]))

    await record("churn", ["a", "b", "c"], old)
    await record("keep", ["k"], recent)
    full = await record("churn", ["d"], recent)
    assert full.dropped == 1  # the dead series still count against the cap
    await settle(storage)
    await storage.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    await run_summary_policies(storage)
    await storage.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    assert await storage.fetchall("SELECT metric FROM series ORDER BY metric") == [("k",)]
    assert await storage.fetchall("SELECT name FROM resources") == [("keep",)]
    assert await storage.fetchall("SELECT COUNT(*) FROM latest") == [(1,)]
    assert (await record("churn", ["d", "e"], recent)).dropped == 0  # the cap has freed
    # A series that still holds data is never collected.
    await storage.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    assert await storage.fetchall("SELECT metric FROM series ORDER BY metric") == [
        ("d",), ("e",), ("k",)]


# ---- the summary views ----------------------------------------------------------------------

async def test_the_views_name_the_series_and_compute_the_average_from_sum_and_count(storage):
    base = BASE // 86400 * 86400
    await put(storage, [row(base + 10, "temp", 10.0, labels='{"core": "0"}'),
                        row(base + 20, "temp", 30.0, labels='{"core": "0"}'),
                        row(base + 4000, "temp", 5.0, labels='{"core": "0"}')])
    await settle(storage)
    cols = "resource, scope, metric, unit, attrs, bucket, n, sum_v, min_v, max_v, avg_v"
    attrs = '{"core":"0"}'
    assert await storage.fetchall(f"SELECT {cols} FROM metric_5m ORDER BY bucket") == [
        ("h", "cpu", "temp", "C", attrs, base, 2, 40.0, 10.0, 30.0, 20.0),
        ("h", "cpu", "temp", "C", attrs, base + 3900, 1, 5.0, 5.0, 5.0, 5.0)]
    assert await storage.fetchall(f"SELECT {cols} FROM metric_hourly ORDER BY bucket") == [
        ("h", "cpu", "temp", "C", attrs, base, 2, 40.0, 10.0, 30.0, 20.0),
        ("h", "cpu", "temp", "C", attrs, base + 3600, 1, 5.0, 5.0, 5.0, 5.0)]
    assert await storage.fetchall(f"SELECT {cols} FROM metric_daily") == [
        ("h", "cpu", "temp", "C", attrs, base, 3, 45.0, 5.0, 30.0, 15.0)]


async def test_there_is_no_view_over_raw_samples(storage):
    for name in ("metric_raw", "metric_samples", "host_samples"):
        with pytest.raises(Exception):
            await storage.fetchall(f"SELECT * FROM {name}")


# ---- compaction on every backend ------------------------------------------------------------

async def test_compaction_keeps_what_the_levels_above_still_cover(storage):
    now = time.time()
    base = int(now) // 86400 * 86400
    points = [("m", (base - age * DAY + 600 * k) * 1000, float(age * 10 + k))
              for age in range(1, 31) for k in range(3)]
    await put(storage, [row(ts / 1000, m, v) for m, ts, v in points])
    await settle(storage)
    # No settle after the trim: a refresh over the whole range would rebuild the aggregates from
    # the raw chunks already dropped and lose the buckets they exist to keep.
    await storage.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    cuts = compaction.cuts_for(RetentionLevels(), "m", now)
    ts_list = [ts for _, ts, _ in points]
    chunked = bool(getattr(storage, "timescale", False))  # whole chunks, so up to a day late
    slack = DAY * 1000 if chunked else 0
    oldest, newest = (await storage.fetchall("SELECT MIN(ts), MAX(ts) FROM samples"))[0]
    assert cuts.raw - slack <= oldest and newest == max(ts_list)
    assert (await storage.fetchall("SELECT COUNT(*) FROM samples"))[0][0] >= sum(
        1 for ts in ts_list if ts >= cuts.raw)
    # The summaries never lose a point that is inside their own retention.
    for table, width, cut in (("rollup_5m", 300_000, cuts.m5), ("rollup_1h", 3_600_000, cuts.h1),
                              ("rollup_1d", 86_400_000, cuts.d1)):
        have = await level_rows(storage, table)
        want = expected([p for p in points if p[1] >= cut + (DAY * 1000 if chunked else 0)],
                        width)
        assert all(have.get(k, (0,))[0] >= v[0] for k, v in want.items()), table


async def test_compaction_on_empty_storage_records_a_run_and_removes_nothing(storage):
    now = time.time()
    assert await storage.apply_retention(now=now, retention_days=7,
                                         audit_retention_days=365) == 0
    assert await storage.fetchall("SELECT COUNT(*) FROM samples") == [(0,)]


# ---- TimescaleDB raw chunk drop, on a fake connection ---------------------------------------

class FakeAdmin:
    def __init__(self):
        self.calls = []
        self.raw = self

    def execute(self, sql, args=None):
        self.calls.append(sql)


class InlineWriter:
    def submit(self, fn, *args):
        future = concurrent.futures.Future()
        future.set_result(fn(*args))
        return future


class RecordingDb:
    def __init__(self):
        self.calls = []

    def execute(self, sql, args=()):
        self.calls.append((sql, args))


def fake_timescale(covered, rows=42):
    pg = object.__new__(PgStorage)
    pg.timescale = True
    pg._admin = FakeAdmin()
    pg._writer = InlineWriter()
    pg.db = RecordingDb()
    compacted = []

    async def read(unit):
        return covered, rows

    async def write(unit, *, touches=(), critical=False):
        return unit(pg.db)

    async def compact_level(storage, level, now, levels):
        compacted.append(level)
        return 0

    class Raw:
        def rollback(self):
            pass

    pg._wraw = Raw()
    pg._read_on_writer = lambda unit: (covered, rows)
    pg.read, pg.write, pg.compacted = read, write, compacted
    return pg, compact_level


def test_raw_chunks_are_dropped_only_after_the_aggregates_cover_them(monkeypatch):
    pg, compact_level = fake_timescale(covered=True)
    monkeypatch.setattr(compaction, "compact_level", compact_level)
    levels = RetentionLevels(raw_days=7)
    now = 1_800_000_000.0
    removed = asyncio.run(pg.drop_raw(now, levels))
    bound = compaction.whole_table_cuts(levels, now).raw
    calls = pg._admin.calls
    refreshed = [i for i, c in enumerate(calls) if "refresh_continuous_aggregate" in c]
    dropped = [i for i, c in enumerate(calls) if c == pg_timescale.drop_raw_statement(bound)]
    assert removed == 42 and refreshed and dropped and max(refreshed) < dropped[0]
    assert pg.compacted == []  # no overrides: whole chunks only
    level, upto, _, rows, error = pg.db.calls[-1][1][0], pg.db.calls[-1][1][1], None, \
        pg.db.calls[-1][1][3], pg.db.calls[-1][1][4]
    assert (level, upto, rows, error) == ("raw", bound // 1000, 42, "")


def test_raw_chunks_are_kept_when_coverage_fails(monkeypatch):
    pg, compact_level = fake_timescale(covered=False)
    monkeypatch.setattr(compaction, "compact_level", compact_level)
    assert asyncio.run(pg.drop_raw(1_800_000_000.0, RetentionLevels())) == 0
    assert not any("drop_chunks" in c for c in pg._admin.calls)
    assert "do not cover" in pg.db.calls[-1][1][4]


def test_an_override_trims_that_metric_by_row_before_whole_chunks_go(monkeypatch):
    pg, compact_level = fake_timescale(covered=True)
    monkeypatch.setattr(compaction, "compact_level", compact_level)
    levels = RetentionLevels(raw_days=7, overrides={"x": {"raw_days": 30}})
    asyncio.run(pg.drop_raw(1_800_000_000.0, levels))
    assert pg.compacted == ["raw"]
    # The chunk bound follows the longest level any metric keeps.
    bound = compaction.whole_table_cuts(levels, 1_800_000_000.0).raw
    assert bound == (1_800_000_000 * 1000 - 30 * DAY * 1000) // 300_000 * 300_000


@pytest.mark.parametrize("seed", range(1, 13))
async def test_levels_stay_exact_when_values_replace_nulls_and_each_other(storage, seed):
    """A brute-force oracle: random batches over few timestamps, so that nulls become values,
    values become nulls and values change, in buckets that share an hour and a day. After the
    batches every level holds what the raw points say, with no row missing."""
    rnd = random.Random(seed)
    day = BASE // DAY * DAY
    # Slots cluster inside a few 5 minute buckets, hours and days.
    slots = sorted({(day + d * DAY + h * 3600 + m * 100 + s) * 1000
                    for d in range(3) for h in (1, 2, 9) for m in (0, 1, 4) for s in (0, 7)})
    model: dict[tuple[str, int], float | None] = {}
    for _batch in range(300):
        # One value per point in a batch, so the oracle needs no rule for repeats inside one.
        chosen: dict[tuple[str, int], float | None] = {}
        for _ in range(rnd.randint(1, 4)):
            key = (rnd.choice(("m0", "m1")), rnd.choice(slots))
            chosen[key] = None if rnd.random() < 0.45 else rnd.randint(-40, 40) / 4
        model.update(chosen)
        await put(storage, [row(ts / 1000, metric, value)
                             for (metric, ts), value in chosen.items()])
    await assert_levels_match(storage, [(m, ts, v) for (m, ts), v in model.items()])


async def test_a_replacement_never_writes_a_row_into_a_level_that_trimmed_the_bucket(storage):
    """The daily row is gone while the raw samples are still there (a short daily retention).
    Turning a value into null must not store a negative count, and turning a null into a value
    must not bring back a partial row."""
    if isinstance(storage, PgStorage) and storage.timescale:
        pytest.skip("the levels are continuous aggregates there, and no correction runs")
    ts = BASE + 10
    await put(storage, [row(ts, "m0", 5.0), row(ts + 1, "m0", 7.0), row(ts + 2, "m0", None)])
    await settle(storage)
    await storage.execute("DELETE FROM rollup_1d")
    await put(storage, [row(ts, "m0", None)])  # a value becomes null
    await settle(storage)
    assert await level_rows(storage, "rollup_1d") == {}
    await put(storage, [row(ts + 2, "m0", 9.0)])  # a null becomes a value
    await settle(storage)
    assert await level_rows(storage, "rollup_1d") == {}
    points = [("m0", ts * 1000, None), ("m0", (ts + 1) * 1000, 7.0), ("m0", (ts + 2) * 1000, 9.0)]
    for table, width in LEVELS[:2]:
        same_levels(await level_rows(storage, table), expected(points, width))
