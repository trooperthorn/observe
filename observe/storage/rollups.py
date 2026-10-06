"""The rollup levels, the summary views and the retention levels, shared by both backends.

Raw samples (`host_samples`) are folded into three summary tables: `rollup_5m` from raw samples,
`rollup_1h` from the 5 minute rows and `rollup_1d` from the hourly rows. Each level keeps a
watermark in `rollup_state`, folds each source range exactly once and only ever folds complete
buckets, so a summary is never recomputed and a replay cannot count a point twice. A sample that
arrives after its 5 minute bucket was folded (later than LATE_GRACE_S past the bucket end) is
kept as a raw row until retention and is left out of the summaries.

SQLite and plain PostgreSQL run `fold` as written. On PostgreSQL with TimescaleDB the three
levels are continuous aggregates with the same names and columns, so the views over them are
the same text on every backend (docs/DATA-API-DESIGN.md sections 10.2 and 12).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .base import Conn

LATE_GRACE_S = 60
WIDTH_5M, WIDTH_1H, WIDTH_1D = 300, 3600, 86400

# level name -> (table, bucket width in seconds)
LEVELS = {"5m": ("rollup_5m", WIDTH_5M), "1h": ("rollup_1h", WIDTH_1H),
          "1d": ("rollup_1d", WIDTH_1D)}

_SERIES = "host TEXT NOT NULL, source TEXT NOT NULL, metric TEXT NOT NULL, labels TEXT NOT NULL"
_KEY = "PRIMARY KEY (host, source, metric, labels, bucket)"


def _table(name: str) -> str:
    return (f"CREATE TABLE IF NOT EXISTS {name} ({_SERIES}, bucket INTEGER NOT NULL, "
            f"n INTEGER NOT NULL, sum_v REAL, min_v REAL, max_v REAL, {_KEY})")


ROLLUP_TABLES = (
    _table("rollup_5m"), _table("rollup_1h"), _table("rollup_1d"),
    # One row per level: how far it has folded, and when and how much the last fold wrote.
    "CREATE TABLE IF NOT EXISTS rollup_state (level TEXT PRIMARY KEY, upto INTEGER NOT NULL, "
    "last_run REAL NOT NULL DEFAULT 0, last_rows INTEGER NOT NULL DEFAULT 0)",
)


def _view(name: str, source: str) -> str:
    return (f"CREATE VIEW IF NOT EXISTS {name} AS SELECT host, source, metric, labels, bucket, "
            f"CAST(n AS BIGINT) AS n, "
            f"sum_v, min_v, max_v, sum_v / NULLIF(n, 0) AS avg_v FROM {source}")


# The same text on every backend. Only the small summary tables are exposed; there is no view
# over raw samples.
METRIC_VIEWS = (
    _view("metric_5m", "rollup_5m"), _view("metric_hourly", "rollup_1h"),
    _view("metric_daily", "rollup_1d"),
    "CREATE VIEW IF NOT EXISTS availability_history AS SELECT monitor, ts, previous AS "
    "previous_state, current AS state, message FROM events",
)
VIEW_NAMES = ("metric_5m", "metric_hourly", "metric_daily", "availability_history")
VIEW_COLUMNS = {
    "metric_5m": ("host", "source", "metric", "labels", "bucket", "n", "sum_v", "min_v", "max_v",
                  "avg_v"),
    "availability_history": ("monitor", "ts", "previous_state", "state", "message"),
}
VIEW_COLUMNS["metric_hourly"] = VIEW_COLUMNS["metric_daily"] = VIEW_COLUMNS["metric_5m"]


# ---- retention levels (docs/DATA-API-DESIGN.md section 10.2) ---------------------------------

@dataclass(frozen=True)
class RetentionLevels:
    raw_days: int = 7
    rollup_5m_days: int = 14
    hourly_days: int = 90
    daily_days: int = 730
    history_days: int = 730


# setting key -> (field, lowest, highest). The keys live in app_settings, where the admin page
# writes them with an audit record.
RETENTION_SETTINGS = {
    "retention.raw_days": ("raw_days", 1, 30),
    "retention.5m_days": ("rollup_5m_days", 1, 365),
    "retention.hourly_days": ("hourly_days", 90, 180),
    "retention.daily_days": ("daily_days", 1, 3650),
    "retention.history_days": ("history_days", 1, 3650),
}


def load_levels(db: Conn, fallback_raw_days: int | None = None) -> RetentionLevels:
    """The admin's retention settings, defaults for a missing or out-of-range value. When no raw
    setting exists, `fallback_raw_days` (server.retention_days) is the raw level."""
    values: dict[str, int] = {}
    if fallback_raw_days is not None:
        values["raw_days"] = int(fallback_raw_days)
    for key, value in db.execute("SELECT key, value FROM app_settings WHERE key LIKE 'retention.%'"):
        spec = RETENTION_SETTINGS.get(key)
        if spec is None:
            continue
        name, low, high = spec
        try:
            days = int(value)
        except (TypeError, ValueError):
            continue
        if low <= days <= high:
            values[name] = days
    return RetentionLevels(**values)


# ---- folding ---------------------------------------------------------------------------------

def _state(db: Conn, level: str) -> int | None:
    row = db.execute("SELECT upto FROM rollup_state WHERE level = ?", (level,)).fetchone()
    return None if row is None else int(row[0])


def _save_state(db: Conn, level: str, upto: int, now: float, rows: int) -> None:
    db.execute(
        "INSERT INTO rollup_state (level, upto, last_run, last_rows) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (level) DO UPDATE SET upto = excluded.upto, last_run = excluded.last_run, "
        "last_rows = excluded.last_rows", (level, upto, now, rows))


def _fold_level(db: Conn, level: str, source_select: str, first_sql: str, end: int,
                now: float) -> int:
    table, _ = LEVELS[level]
    start = _state(db, level)
    if start is None:
        row = db.execute(first_sql).fetchone()
        if row is None or row[0] is None:
            return 0
        start = int(row[0])
    if start >= end:
        return 0
    cur = db.execute(
        f"INSERT INTO {table} (host, source, metric, labels, bucket, n, sum_v, min_v, max_v) "
        f"{source_select} "
        "ON CONFLICT (host, source, metric, labels, bucket) DO UPDATE SET "
        f"n = {table}.n + excluded.n, sum_v = {table}.sum_v + excluded.sum_v, "
        f"min_v = MIN({table}.min_v, excluded.min_v), max_v = MAX({table}.max_v, excluded.max_v)",
        (start, end))
    rows = cur.rowcount
    _save_state(db, level, end, now, rows)
    return max(rows, 0)


def fold(db: Conn, now: float, bucket_of_ts: Callable[[str, int], str]) -> int:
    """Fold every complete bucket that is new since the last call. Runs inside one write unit.
    `bucket_of_ts(column, width)` is the backend's expression for the start of a bucket of a
    REAL second timestamp. Returns the number of summary rows written."""
    complete = int(now - LATE_GRACE_S)
    end_5m = complete // WIDTH_5M * WIDTH_5M
    b5 = bucket_of_ts("ts", WIDTH_5M)
    total = _fold_level(
        db, "5m",
        "SELECT host, source, metric, labels, " + b5 + " AS bk, COUNT(value), SUM(value), "
        "MIN(value), MAX(value) FROM host_samples WHERE ts >= ? AND ts < ? AND value IS NOT NULL "
        "GROUP BY host, source, metric, labels, " + b5,
        f"SELECT {bucket_of_ts('MIN(ts)', WIDTH_5M)} FROM host_samples",
        end_5m, now)
    done_5m = _state(db, "5m")
    if done_5m is None:
        return total
    for level, source, width, below in (("1h", "rollup_5m", WIDTH_1H, "5m"),
                                        ("1d", "rollup_1h", WIDTH_1D, "1h")):
        # Only buckets the level below has finished folding, and only whole buckets.
        end = min(complete, _state(db, below) or 0) // width * width
        bk = f"(bucket / {width}) * {width}"
        total += _fold_level(
            db, level,
            f"SELECT host, source, metric, labels, {bk} AS bk, SUM(n), SUM(sum_v), MIN(min_v), "
            f"MAX(max_v) FROM {source} WHERE bucket >= ? AND bucket < ? "
            f"GROUP BY host, source, metric, labels, {bk}",
            f"SELECT (MIN(bucket) / {width}) * {width} FROM {source}", end, now)
    return total


def cutoffs(db: Conn, now: float, levels: RetentionLevels) -> dict[str, float]:
    """The delete bound of each table. A level is never trimmed past the point the next level
    has folded, so retention cannot remove data that is still needed to build a summary."""
    raw = now - levels.raw_days * 86400
    folded_5m = _state(db, "5m")
    folded_1h = _state(db, "1h")
    folded_1d = _state(db, "1d")
    return {
        "raw_samples": min(raw, float(folded_5m if folded_5m is not None else 0)),
        "raw": raw,
        "rollup_5m": min(now - levels.rollup_5m_days * 86400,
                         float(folded_1h if folded_1h is not None else 0)),
        "rollup_1h": min(now - levels.hourly_days * 86400,
                         float(folded_1d if folded_1d is not None else 0)),
        "rollup_1d": now - levels.daily_days * 86400,
        "history": now - levels.history_days * 86400,
    }


def retention_statements(cut: dict[str, float], *, now: float,
                         audit_retention_days: int) -> list[tuple[str, float]]:
    """Every delete as (sql, bound) in the order to run them, one write unit each so ingest is
    never held back behind one long delete. The first is the poll rows, whose count is returned
    to the caller."""
    return [
        ("DELETE FROM results WHERE ts < ?", cut["raw"]),
        ("DELETE FROM host_samples WHERE ts < ?", cut["raw_samples"]),
        ("DELETE FROM ingest_batches WHERE ts < ?", cut["raw"]),
        ("DELETE FROM rollup_5m WHERE bucket < ?", cut["rollup_5m"]),
        ("DELETE FROM rollup_1h WHERE bucket < ?", cut["rollup_1h"]),
        ("DELETE FROM rollup_1d WHERE bucket < ?", cut["rollup_1d"]),
        ("DELETE FROM events WHERE ts < ?", cut["history"]),
        ("DELETE FROM host_events WHERE ts < ?", cut["history"]),
        ("DELETE FROM audit WHERE ts < ?", now - audit_retention_days * 86400),
        ("DELETE FROM sessions WHERE expires < ?", now),
    ]
