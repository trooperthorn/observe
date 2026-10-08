"""Batched ingest writes (slice o10-perf-ingest): the same stored rows with a fraction of the
statements.

`series.record_points` used to run about 6.5 statements per point: a series lookup, a sample
insert, a latest upsert and three summary upserts for each. It now looks the series up once per
batch, reads the stored samples of the batch once, and writes the new samples, the latest rows
and each summary level with one batched statement each. This file keeps the old per point
implementation as `old_record_points` (copied unchanged from before the change) and proves the two
agree:

* `test_the_batched_path_stores_the_same_rows` runs one workload through both paths (batches of
  new points, a replay, out of order and null points, a point sent again with a new value, and a
  series over the cap) and compares a hash of every stored sample, latest row, summary row and
  series, on SQLite, on the PostgreSQL stand-in and, when a server is configured, on PostgreSQL.
* `test_the_ids_and_numbering_are_the_same_on_sqlite` compares the raw tables as well, including
  the series ids and the insertion numbers the OTLP exporter reads.

Measured on SQLite, one 45 point batch of 45 series (statements sent through the connection; an
executemany counts as one statement), `record_points` alone:

    first batch, 45 new series    old 373 statements   new 23   (16 times fewer)
    next batch, existing series   old 274 statements   new 12   (23 times fewer)

Throughput of `record_points` on SQLite on disk, 45 points per batch, two runs each (this machine;
the numbers move by a third from run to run):

    300 batches in one transaction   old 51,000 to 69,000 points/s   new 78,000 points/s
    one commit per batch             old 37,000 to 40,000 points/s   new 42,000 to 43,000 points/s

SQLite runs in the process, so a statement costs little and the gain there is 1.1 to 1.5 times.
On PostgreSQL every statement is a round trip to the server and the batched statements are sent
together, so the saved statements are saved round trips (test_throughput_is_recorded prints the
points per second of the run).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import time
from typing import Any

import pytest

from observe.storage import open_storage, series

from .dbq import settle
from .fakes.pg_fake import PgFakeStorage
from .test_storage import live_pg

BASE = 1_700_000_000_000  # milliseconds; nothing here depends on the clock
LEVELS = ("rollup_5m", "rollup_1h", "rollup_1d")


@pytest.fixture(params=["sqlite", "postgres-fake", "postgres"])
def storage(request, tmp_path):
    if request.param == "postgres":
        with live_pg() as s:  # skipped without OBSERVE_TEST_PG_DSN
            yield s
        return
    s = PgFakeStorage() if request.param == "postgres-fake" else open_storage(str(tmp_path / "s.db"))
    yield s
    s.close()


# ---- the old path, copied unchanged -----------------------------------------------------------

class _Counts:
    """The series counts for the cardinality guard, read once per batch and kept up to date."""

    def __init__(self, db: Conn, rid: int) -> None:
        self.resource = int(db.execute("SELECT COUNT(*) FROM series WHERE resource_id = ?",
                                       (rid,)).fetchone()[0])
        self.total = int(db.execute("SELECT COUNT(*) FROM series").fetchone()[0])


def old_series_ids(db: Conn, rid: int, points: Sequence[series.Point], now: float,
                max_per_resource: int, max_total: int) -> dict[tuple[str, str, str], int | None]:
    """The series id of each distinct (scope, metric, attributes) in the points, creating the
    ones that are new. None marks a series the cardinality guard refused."""
    ids: dict[tuple[str, str, str], int | None] = {}
    first: dict[tuple[str, str, str], series.Point] = {}
    for p in points:
        first.setdefault((p.scope, p.metric, p.attrs), p)
    counts: _Counts | None = None
    scopes: dict[str, int] = {}
    for key, p in first.items():
        scope, metric, attrs = key
        digest = series._digest(str(rid), scope, metric, attrs)
        row = db.execute("SELECT id FROM series WHERE key_hash = ?", (digest,)).fetchone()
        if row is not None:
            ids[key] = int(row[0])
            continue
        counts = counts or _Counts(db, rid)
        if counts.resource >= max_per_resource or counts.total >= max_total:
            ids[key] = None
            continue
        sid = scopes.get(scope)
        if sid is None:
            sid = scopes[scope] = series.scope_id(db, scope)
        seen = p.ts_ms / 1000.0
        db.execute(
            "INSERT INTO series (key_hash, resource_id, scope_id, metric, unit, instrument, "
            "attrs, first_seen, last_seen) VALUES (?,?,?,?,?,'gauge',?,?,?) "
            "ON CONFLICT (key_hash) DO NOTHING", (digest, rid, sid, metric, p.unit, attrs, seen, seen))
        ids[key] = int(db.execute("SELECT id FROM series WHERE key_hash = ?",
                                  (digest,)).fetchone()[0])
        counts.resource += 1
        counts.total += 1
    return ids


def old_record_points(db: Conn, *, kind: str, name: str, points: Iterable[series.Point], now: float,
                  rollups: bool, attrs: Mapping[str, Any] | None = None,
                  max_per_resource: int = series.MAX_SERIES_PER_RESOURCE,
                  max_total: int = series.MAX_SERIES_TOTAL,
                  raw_cut: Callable[[str], int] | None = None) -> series.Recorded:
    """The point by point ingest before the batched rewrite, frozen as the reference."""
    pts = list(points)
    late = 0
    if raw_cut is not None:
        cuts: dict[str, int] = {}
        kept = []
        for p in pts:
            cut = cuts.get(p.metric)
            if cut is None:
                cut = cuts[p.metric] = raw_cut(p.metric)
            if p.ts_ms < cut:
                late += 1
            else:
                kept.append(p)
        pts = kept
    rid = series.resource_id(db, kind, name, now, attrs)
    ids = old_series_ids(db, rid, pts, now, max_per_resource, max_total)
    new = replaced = duplicate = dropped = 0
    seq = int(db.execute("SELECT seq FROM ingest_seq WHERE name = 'samples'").fetchone()[0])
    first_seq = seq
    for p in pts:
        sid = ids[(p.scope, p.metric, p.attrs)]
        if sid is None:
            dropped += 1
            continue
        # The next insertion number is used only if the point is new, so numbers have no holes.
        cur = db.execute("INSERT INTO samples (series_id, ts, value, seq) VALUES (?,?,?,?) "
                         "ON CONFLICT (series_id, ts) DO NOTHING", (sid, p.ts_ms, p.value, seq + 1))
        if cur.rowcount == 1:
            new += 1
            seq += 1
            series._latest(db, sid, p.ts_ms, p.value)
            if rollups and p.value is not None:
                series._bump(db, sid, p.ts_ms, p.value)
            continue
        stored = db.execute("SELECT value FROM samples WHERE series_id = ? AND ts = ?",
                            (sid, p.ts_ms)).fetchone()
        old = None if stored is None else stored[0]
        if old == p.value:
            duplicate += 1  # a replay changes nothing
        else:
            replaced += 1
            series._replace(db, sid, p.ts_ms, old, p.value, rollups)
    if seq != first_seq:
        db.execute("UPDATE ingest_seq SET seq = ? WHERE name = 'samples'", (seq,))
    return series.Recorded(new, replaced, duplicate, dropped, late)


# ---- helpers ----------------------------------------------------------------------------------

class Counting:
    """A connection that counts the statements a unit sends."""

    def __init__(self, db: Any) -> None:
        self._conn = db
        self.statements = 0

    def execute(self, sql: str, args: Any = ()) -> Any:
        self.statements += 1
        return self._conn.execute(sql, args)

    def executemany(self, sql: str, rows: Any) -> Any:
        self.statements += 1
        return self._conn.executemany(sql, rows)


def pt(metric: str, ts: int, value: float | None, scope: str = "cpu", attrs: str = "{}") -> Any:
    return series.Point(scope, metric, "1", attrs, ts, value)


def batch(rng: random.Random, at: int, series_count: int = 45, nulls: float = 0.0) -> list[Any]:
    """One push: a reading of `series_count` distinct series (metrics times label sets)."""
    out = []
    for i in range(series_count):
        value = None if rng.random() < nulls else round(rng.uniform(0, 100), 3)
        out.append(pt(f"m{i % 15}", at, value, scope=f"s{i % 2}",
                      attrs=series.canonical({"core": str(i // 15)})))
    return out


def workload() -> list[tuple[str, list[Any], dict[str, Any]]]:
    """Named batches for one resource; the last argument is extra record_points keywords."""
    rng = random.Random(10)
    steps: list[tuple[str, list[Any], dict[str, Any]]] = []
    first = batch(rng, BASE)
    steps.append(("first batch", first, {}))
    steps.append(("next batch", batch(rng, BASE + 30_000), {}))
    steps.append(("replay", first, {}))  # every point stored already with the same value
    steps.append(("with nulls", batch(rng, BASE + 60_000, nulls=0.2), {}))
    late = batch(rng, BASE - 700_000)  # older than everything stored, a new 5 minute bucket
    steps.append(("out of order", late + batch(rng, BASE + 90_000), {}))
    steps.append(("older than the previous point", batch(rng, BASE + 15_000), {}))
    twice = batch(rng, BASE + 120_000)
    steps.append(("repeated inside one batch", twice + twice[:10], {}))
    steps.append(("empty", [], {}))
    again = batch(rng, BASE + 150_000)
    changed = [dataclasses.replace(again[3], value=-1.5), *again[4:6]]
    steps.append(("new points", again, {}))
    steps.append(("a value sent again", changed + batch(rng, BASE + 180_000), {}))
    steps.append(("one point twice with two values", [pt("dup", BASE + 5, 1.0),
                                                      pt("dup", BASE + 5, 2.0)], {}))
    steps.append(("hours later", batch(rng, BASE + 7 * 3_600_000), {}))
    steps.append(("over the series cap", batch(rng, BASE + 200_000, 60)
                  + [pt("extra", BASE + 200_000, 1.0)], {"max_per_resource": 50}))
    steps.append(("late points are cut", batch(rng, BASE + 210_000),
                  {"raw_cut": lambda metric: BASE + 205_000}))
    return steps


def run(storage: Any, record: Any, name: str, steps: Any, counts: list[int] | None = None
        ) -> list[Any]:
    out = []
    for _label, points, extra in steps:
        def unit(db: Any, points: Any = points, extra: Any = extra) -> Any:
            conn = Counting(db)
            rec = record(conn, kind="host", name=name, points=list(points), now=1.0,
                         rollups=storage.incremental_rollups, **extra)
            if counts is not None:
                counts.append(conn.statements)
            return rec
        out.append(storage.write_sync(unit))
    return out


def digest(rows: list[Any]) -> str:
    h = hashlib.sha256()
    for row in rows:
        h.update(json.dumps(row, default=str).encode())
    return h.hexdigest()


async def content(storage: Any, name: str) -> dict[str, Any]:
    """A hash of everything stored for one resource, by name and not by id, so two resources of
    one database can be compared. The summed values of each summary view are kept apart as lists
    under "<view>.sum_v": on TimescaleDB the views are computed from the samples and the order in
    which a float sum adds its terms is not defined, so those compare with a tolerance there
    (see same_content). The counts, minimums and maximums stay in the exact hash."""
    await settle(storage)
    ident = ("FROM {t} x JOIN series s ON s.id = x.series_id JOIN resources r ON r.id = s.resource_id "
             "JOIN scopes sc ON sc.id = s.scope_id WHERE r.name = ? ")
    head = "SELECT sc.name, s.metric, s.unit, s.attrs, "
    out = {
        "series": digest(await storage.fetchall(
            "SELECT sc.name, s.metric, s.unit, s.attrs, s.instrument, s.first_seen, s.last_seen "
            "FROM series s JOIN resources r ON r.id = s.resource_id "
            "JOIN scopes sc ON sc.id = s.scope_id WHERE r.name = ? "
            "ORDER BY sc.name, s.metric, s.attrs", (name,))),
        "samples": digest(await storage.fetchall(
            head + "x.ts, x.value " + ident.format(t="samples")
            + "ORDER BY sc.name, s.metric, s.attrs, x.ts", (name,))),
        "latest": digest(await storage.fetchall(
            head + "x.ts, x.value, x.prev_ts, x.prev_value " + ident.format(t="latest")
            + "ORDER BY sc.name, s.metric, s.attrs", (name,))),
    }
    for view in ("metric_5m", "metric_hourly", "metric_daily"):
        out[view] = digest(await storage.fetchall(
            "SELECT scope, metric, unit, attrs, bucket, n, min_v, max_v "
            f"FROM {view} WHERE resource = ? ORDER BY scope, metric, attrs, bucket", (name,)))
        out[view + ".sum_v"] = [r[0] for r in await storage.fetchall(
            f"SELECT sum_v FROM {view} WHERE resource = ? "
            "ORDER BY scope, metric, attrs, bucket", (name,))]
    return out


async def same_content(storage: Any) -> None:
    """The new and the old path stored the same rows. Summed values are exact where the summary
    rows are written by the code under test, and equal within a relative 1e-9 where the database
    computes them from the samples (TimescaleDB)."""
    new, old = await content(storage, "new"), await content(storage, "old")
    for key in [k for k in new if k.endswith(".sum_v")]:
        a, b = new.pop(key), old.pop(key)
        if storage.incremental_rollups:
            assert a == b, key
        else:
            assert a == pytest.approx(b, rel=1e-9, abs=1e-12), key
    assert new == old


# ---- the two paths agree ----------------------------------------------------------------------

async def test_the_batched_path_stores_the_same_rows(storage):
    steps = workload()
    old = run(storage, old_record_points, "old", steps)
    new = run(storage, series.record_points, "new", steps)
    labels = [s[0] for s in steps]
    assert [(lab, dataclasses.astuple(r)) for lab, r in zip(labels, new)] == [
        (lab, dataclasses.astuple(r)) for lab, r in zip(labels, old)]
    await same_content(storage)
    got = await storage.fetchall("SELECT COUNT(*) FROM samples")
    assert got[0][0] > 400  # the workload stored something worth comparing
    # The insertion numbers have no holes, and are one per stored sample, on either path.
    seqs = [r[0] for r in await storage.fetchall("SELECT seq FROM samples ORDER BY seq")]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_the_ids_and_numbering_are_the_same_on_sqlite(tmp_path):
    dumps = []
    for label, record in (("old", old_record_points), ("new", series.record_points)):
        s = open_storage(str(tmp_path / f"{label}.db"))
        run(s, record, "h", workload())
        tables = {}
        for table in ("resources", "scopes", "series", "samples", "latest", *LEVELS,
                      "ingest_seq"):
            tables[table] = digest(await s.fetchall(f"SELECT * FROM {table} ORDER BY 1, 2"))
        dumps.append(tables)
        s.close()
    assert dumps[0] == dumps[1]


async def test_a_batch_that_replaces_a_value_is_stored_like_the_old_path(storage):
    """The rare batch that sends a stored point again with a new value takes the point by point
    path inside record_points, with its summary corrections."""
    rng = random.Random(3)
    base = batch(rng, BASE)
    steps = [("first", base, {}),
             ("replaced", [dataclasses.replace(base[0], value=None),
                           dataclasses.replace(base[1], value=7.0)] + batch(rng, BASE + 1000), {}),
             ("replaced back", [dataclasses.replace(base[0], value=3.0)], {})]
    old = run(storage, old_record_points, "old", steps)
    new = run(storage, series.record_points, "new", steps)
    assert [dataclasses.astuple(r) for r in new] == [dataclasses.astuple(r) for r in old]
    assert new[1].replaced == 2
    await same_content(storage)


# ---- statements per batch ---------------------------------------------------------------------

def test_a_45_point_batch_takes_at_most_half_the_statements(tmp_path):
    rows = {}
    for label, record in (("old", old_record_points), ("new", series.record_points)):
        s = open_storage(str(tmp_path / f"{label}.db"))
        counts: list[int] = []
        rng = random.Random(1)
        steps = [("new series", batch(rng, BASE), {}),
                 ("existing series", batch(rng, BASE + 30_000), {})]
        run(s, record, "h", steps, counts)
        rows[label] = counts
        s.close()
    for n, (old, new) in enumerate(zip(rows["old"], rows["new"])):
        assert new * 2 <= old, (n, old, new)
    # Recorded for docs/ARCHITECTURE.md: these are the numbers in the module docstring.
    assert rows["new"][1] <= 14
    assert rows["old"][1] >= 250


def test_the_statement_count_does_not_grow_with_the_points(tmp_path):
    s = open_storage(str(tmp_path / "s.db"))
    rng = random.Random(2)
    counts: list[int] = []
    run(s, series.record_points, "h", [("a", batch(rng, BASE, 45), {})], counts)
    run(s, series.record_points, "h", [("b", batch(rng, BASE + 30_000, 45), {})], counts)
    run(s, series.record_points, "g", [("c", batch(rng, BASE, 450), {})], counts)
    run(s, series.record_points, "g", [("d", batch(rng, BASE + 30_000, 450), {})], counts)
    s.close()
    assert counts[3] <= counts[1] + 6  # ten times the points, a few more chunked lookups


# ---- throughput -------------------------------------------------------------------------------

def test_throughput_is_recorded(tmp_path):
    """Times 300 batches of 45 points through each path on a database on disk, in one transaction
    and with a commit per batch, and prints the points per second. Nothing is asserted about
    speed, since timings depend on the machine; the two databases must still hold the same rows."""
    results = {}
    hashes = []
    for label, record in (("old", old_record_points), ("new", series.record_points)):
        s = open_storage(str(tmp_path / f"{label}.db"))
        rng = random.Random(4)
        batches = [batch(rng, BASE + 30_000 * n) for n in range(300)]

        def one_transaction(db: Any, record: Any = record, batches: Any = batches) -> None:
            for points in batches:
                record(db, kind="host", name="one", points=points, now=1.0, rollups=True)
        started = time.perf_counter()
        s.write_sync(one_transaction)
        results[label] = 300 * 45 / (time.perf_counter() - started)
        started = time.perf_counter()
        for points in batches[:100]:
            s.write_sync(lambda db, points=points: record(
                db, kind="host", name="each", points=points, now=1.0, rollups=True))
        results[label + " with a commit per batch"] = 100 * 45 / (time.perf_counter() - started)
        hashes.append(s.read_sync(lambda db: digest(db.execute(
            "SELECT s.metric, x.ts, x.value FROM samples x JOIN series s ON s.id = x.series_id "
            "JOIN resources r ON r.id = s.resource_id WHERE r.name = 'one' ORDER BY 1, 2"
        ).fetchall())))
        s.close()
    print("throughput points/s: " + ", ".join(f"{k} {v:.0f}" for k, v in results.items()))
    assert hashes[0] == hashes[1]
    assert all(v > 0 for v in results.values())
