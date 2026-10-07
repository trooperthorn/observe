"""Retention and compaction of the series tables, shared by both backends
(docs/DATA-API-DESIGN.md section 10.2, step 2).

A compaction pass removes, level by level, the rows past each level's retention: raw samples, then
5 minute, hourly and daily summaries. Three rules keep it safe.

* Coverage is verified before anything is deleted. For a series, the points about to go must be
  summarised by the levels that still hold their time range: the coarser level must count at least
  as many points, reach as low and as high, and (at equal counts) have the same sum. A series that fails the check is left alone, the
  first problem is recorded for the admin page, and the pass carries on with the others.
* Deletes are chunked. A write unit removes at most CHUNK_ROWS rows and looks at no more than
  MAX_SERIES_PER_UNIT series, so ingest never waits behind one long delete.
* Cuts are aligned. A level is trimmed only at a multiple of the width of the level that covers
  it, so a bucket is never split between a trimmed and a kept half.

* Dead series are collected. After the levels are trimmed, a series that has no raw sample and no
  summary row at any level is removed with its latest row, and a resource with no series left is
  removed too. Without this, series from churned labels would count against the cardinality caps
  for ever. The delete is chunked like the others, and a series that still holds any row is never
  touched.

A per-metric override gives that metric's series their own cuts. On TimescaleDB the summary levels
are continuous aggregates, trimmed by their retention policies, and raw chunks are dropped only
after the same coverage check (see PgStorage).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .base import Conn, Storage
from .rollups import RetentionLevels, days_for, load_levels, longest_days, shortest_days
from .series import MS_1D, MS_1H, MS_5M

log = logging.getLogger("observe.storage.compaction")

CHUNK_ROWS = 5000
MAX_SERIES_PER_UNIT = 500
DAY_MS = MS_1D

# level -> (table, time column)
TABLES = {"raw": ("samples", "ts"), "5m": ("rollup_5m", "bucket"), "1h": ("rollup_1h", "bucket"),
          "1d": ("rollup_1d", "bucket")}
# Levels that cover a level, finest first.
COVERED_BY = {"raw": ("5m", "1h", "1d"), "5m": ("1h", "1d"), "1h": ("1d",), "1d": ()}
ORDER = ("raw", "5m", "1h", "1d")


@dataclass(frozen=True, slots=True)
class Cuts:
    """Millisecond bounds before which a level's rows go, aligned to the covering level's
    width. A level is guaranteed to hold everything at or after its cut."""

    raw: int
    m5: int
    h1: int
    d1: int

    def of(self, level: str) -> int:
        return {"raw": self.raw, "5m": self.m5, "1h": self.h1, "1d": self.d1}[level]


def cuts_for(levels: RetentionLevels, metric: str, now: float) -> Cuts:
    now_ms = int(now * 1000)

    def cut(name: str, width: int) -> int:
        return (now_ms - days_for(levels, metric, name) * DAY_MS) // width * width

    return Cuts(raw=cut("raw_days", MS_5M), m5=cut("rollup_5m_days", MS_1H),
                h1=cut("hourly_days", MS_1D), d1=cut("daily_days", MS_1D))


def whole_table_cuts(levels: RetentionLevels, now: float) -> Cuts:
    """Cuts that hold for every series at once, for the TimescaleDB drop of whole raw chunks: the
    raw bound is the longest raw level any metric keeps (a chunk goes only when all of it is past
    retention), and each summary level's cut is the latest edge any metric trims it to, which is
    what every series certainly holds."""
    now_ms = int(now * 1000)

    def cut(name: str, width: int, days: int) -> int:
        return (now_ms - days * DAY_MS) // width * width

    return Cuts(raw=cut("raw", MS_5M, longest_days(levels, "raw_days")),
                m5=cut("m5", MS_1H, shortest_days(levels, "rollup_5m_days")),
                h1=cut("h1", MS_1D, shortest_days(levels, "hourly_days")),
                d1=cut("d1", MS_1D, shortest_days(levels, "daily_days")))


def _stats(db: Conn, level: str, sid: int | None, lo: int, hi: int
           ) -> tuple[int, float, float | None, float | None]:
    """Count, sum, min and max of the points a level holds in [lo, hi)."""
    table, col = TABLES[level]
    where = f"{col} >= ? AND {col} < ?"
    args: tuple[Any, ...] = (lo, hi)
    if sid is not None:
        where = "series_id = ? AND " + where
        args = (sid, *args)
    if level == "raw":
        what = "COUNT(value), COALESCE(SUM(value), 0), MIN(value), MAX(value)"
    else:
        what = "COALESCE(SUM(n), 0), COALESCE(SUM(sum_v), 0), MIN(min_v), MAX(max_v)"
    n, total, lo_v, hi_v = db.execute(f"SELECT {what} FROM {table} WHERE {where}", args).fetchone()
    return (int(n or 0), float(total or 0.0), None if lo_v is None else float(lo_v),
            None if hi_v is None else float(hi_v))


def _covers(fine: tuple[int, float, float | None, float | None],
            coarse: tuple[int, float, float | None, float | None]) -> bool:
    """The coarse level counts at least the points of the fine one, reaches at least as low and
    as high, and, when the counts match, has the same sum (within rounding)."""
    if coarse[0] < fine[0]:
        return False
    if fine[2] is not None and (coarse[2] is None or coarse[2] > fine[2]):
        return False
    if fine[3] is not None and (coarse[3] is None or coarse[3] < fine[3]):
        return False
    if coarse[0] == fine[0] and abs(coarse[1] - fine[1]) > 1e-9 * max(1.0, abs(fine[1])):
        return False
    return True


def coverage_ok(db: Conn, level: str, hi: int, cuts: Cuts, sid: int | None = None) -> bool:
    """Whether the rows of `level` older than `hi` are summarised in the levels above it. Each
    covering level is checked over the range it still holds, on count, sum, minimum and maximum;
    what is older than every covering level's cut has been trimmed everywhere by retention and
    is not checked. `sid` limits the check to one series."""
    upper = hi
    for coarse in COVERED_BY[level]:
        hold = cuts.of(coarse)
        if upper > hold:
            if not _covers(_stats(db, level, sid, hold, upper),
                           _stats(db, coarse, sid, hold, upper)):
                return False
            upper = hold
    return True


def _compact_unit(db: Conn, level: str, items: list[tuple[int, Cuts]], pos: int) -> tuple[
        int, int, list[int]]:
    """Process series from `pos` until the row budget or the series budget is used. Returns the
    next position, the rows deleted and the series that failed the coverage check."""
    table, col = TABLES[level]
    budget = CHUNK_ROWS
    deleted = 0
    failed: list[int] = []
    looked = 0
    while pos < len(items) and budget > 0 and looked < MAX_SERIES_PER_UNIT:
        sid, cuts = items[pos]
        hi = cuts.of(level)
        looked += 1
        due = db.execute(f"SELECT 1 FROM {table} WHERE series_id = ? AND {col} < ? LIMIT 1",
                         (sid, hi)).fetchone()
        if due is None:
            pos += 1
            continue
        if not coverage_ok(db, level, hi, cuts, sid):
            failed.append(sid)
            pos += 1
            continue
        count = db.execute(
            f"DELETE FROM {table} WHERE series_id = ? AND {col} IN "
            f"(SELECT {col} FROM {table} WHERE series_id = ? AND {col} < ? ORDER BY {col} LIMIT ?)",
            (sid, sid, hi, budget)).rowcount
        deleted += max(count, 0)
        budget -= max(count, 0)
        if budget > 0:
            pos += 1  # the series is finished; with no budget left it is visited again
    return pos, deleted, failed


def _collect_unit(db: Conn) -> tuple[int, int]:
    """Remove up to CHUNK_ROWS series that hold no samples and no summary rows, with their latest
    rows, and then the resources left with no series. Returns (series, resources) removed."""
    absent = " AND ".join(f"NOT EXISTS (SELECT 1 FROM {table} t WHERE t.series_id = s.id)"
                          for table, _ in (TABLES[level] for level in ORDER))
    dead = [int(r[0]) for r in db.execute(
        f"SELECT s.id FROM series s WHERE {absent} ORDER BY s.id LIMIT ?", (CHUNK_ROWS,))
        .fetchall()]
    for i in range(0, len(dead), MAX_SERIES_PER_UNIT):
        chunk = dead[i:i + MAX_SERIES_PER_UNIT]
        marks = ",".join("?" * len(chunk))
        db.execute(f"DELETE FROM latest WHERE series_id IN ({marks})", tuple(chunk))
        db.execute(f"DELETE FROM series WHERE id IN ({marks})", tuple(chunk))
    gone = db.execute(
        "DELETE FROM resources WHERE NOT EXISTS (SELECT 1 FROM series s WHERE s.resource_id = "
        "resources.id)").rowcount
    return len(dead), max(gone, 0)


async def collect_dead_series(storage: Storage) -> tuple[int, int]:
    """Collect dead series and empty resources in chunks. Returns the totals removed."""
    series = resources = 0
    while True:
        n, r = await storage.write(_collect_unit)
        series += n
        resources += r
        if n < CHUNK_ROWS:
            break
    if series or resources:
        log.info("compaction removed %d series and %d resources that held no data", series,
                 resources)
    return series, resources


def note_level(db: Conn, level: str, upto_s: int, now: float, rows: int, error: str) -> None:
    db.execute(
        "INSERT INTO rollup_state (level, upto, last_run, last_rows, last_error) "
        "VALUES (?,?,?,?,?) ON CONFLICT (level) DO UPDATE SET upto = excluded.upto, "
        "last_run = excluded.last_run, last_rows = excluded.last_rows, "
        "last_error = excluded.last_error", (level, upto_s, now, rows, error[:200]))


async def compact_level(storage: Storage, level: str, now: float, levels: RetentionLevels) -> int:
    """Trim one level, series by series, in chunks. Returns the rows deleted."""
    series = await storage.read(lambda db: db.execute("SELECT id, metric FROM series").fetchall())
    by_metric: dict[str, Cuts] = {}
    items: list[tuple[int, Cuts]] = []
    for sid, metric in series:
        cuts = by_metric.get(metric)
        if cuts is None:
            cuts = by_metric[metric] = cuts_for(levels, metric, now)
        items.append((int(sid), cuts))
    pos = deleted = 0
    failed: list[int] = []
    while pos < len(items):
        pos, n, bad = await storage.write(
            lambda db, p=pos: _compact_unit(db, level, items, p))
        deleted += n
        failed += bad
    error = ""
    if failed:
        error = (f"{len(failed)} series kept: summaries do not cover their old {level} rows "
                 f"(first series {failed[0]})")
        log.warning("compaction of %s: %s", level, error)
    upto = cuts_for(levels, "", now).of(level) // 1000
    await storage.write(lambda db: note_level(db, level, upto, now, deleted, error))
    return deleted


def housekeeping(levels: RetentionLevels, now: float, audit_retention_days: int
                 ) -> list[tuple[str, tuple[Any, ...]]]:
    """The deletes that are not series data, as (sql, args); the first is the poll rows."""
    raw = now - levels.raw_days * 86400
    history = now - levels.history_days * 86400
    return [
        ("DELETE FROM results WHERE ts < ?", (raw,)),
        ("DELETE FROM ingest_batches WHERE ts < ?", (raw,)),
        ("DELETE FROM events WHERE ts < ?", (history,)),
        ("DELETE FROM host_events WHERE ts < ?", (history,)),
        ("DELETE FROM audit WHERE ts < ?", (now - audit_retention_days * 86400,)),
        ("DELETE FROM sessions WHERE expires < ?", (now,)),
    ]


async def run(storage: Storage, now: float, retention_days: int, audit_retention_days: int, *,
              summaries_by_policy: bool = False,
              raw: Callable[[float, RetentionLevels], Awaitable[int]] | None = None) -> int:
    """One compaction pass. `summaries_by_policy` leaves the summary levels to the backend's own
    policies (TimescaleDB); `raw` replaces the per-series raw trim (TimescaleDB drops verified
    chunks). Returns the poll rows removed."""
    levels = await storage.write(lambda db: load_levels(db, retention_days))
    statements = housekeeping(levels, now, audit_retention_days)
    removed = int(await storage.write(
        lambda db, s=statements[0]: db.execute(s[0], s[1]).rowcount))
    if raw is not None:
        await raw(now, levels)
    else:
        await compact_level(storage, "raw", now, levels)
    for sql, args in statements[1:2]:
        await storage.write(lambda db, sql=sql, args=args: db.execute(sql, args))
    if not summaries_by_policy:
        for level in ("5m", "1h", "1d"):
            await compact_level(storage, level, now, levels)
    await collect_dead_series(storage)
    for sql, args in statements[2:]:
        await storage.write(lambda db, sql=sql, args=args: db.execute(sql, args))
    return max(removed, 0)


__all__ = ["CHUNK_ROWS", "Cuts", "collect_dead_series", "compact_level", "coverage_ok", "cuts_for", "run"]
