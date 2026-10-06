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

import json
from collections.abc import Callable
from dataclasses import dataclass, field

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
    # One row per level: how far it has folded, and when and how much the last fold wrote. The
    # row named MAINTENANCE_LEVEL is the whole compaction pass: when it last ran and its error.
    "CREATE TABLE IF NOT EXISTS rollup_state (level TEXT PRIMARY KEY, upto INTEGER NOT NULL, "
    "last_run REAL NOT NULL DEFAULT 0, last_rows INTEGER NOT NULL DEFAULT 0, "
    "last_error TEXT NOT NULL DEFAULT '')",
)


MAINTENANCE_LEVEL = "compaction"
MAX_ERROR_CHARS = 200


def note_maintenance(db: Conn, now: float, rows: int, error: str) -> None:
    """Record one compaction pass: when it ran, the poll rows it removed and its error, if any."""
    db.execute(
        "INSERT INTO rollup_state (level, upto, last_run, last_rows, last_error) "
        "VALUES (?, 0, ?, ?, ?) ON CONFLICT (level) DO UPDATE SET last_run = excluded.last_run, "
        "last_rows = excluded.last_rows, last_error = excluded.last_error",
        (MAINTENANCE_LEVEL, now, rows, error[:MAX_ERROR_CHARS]))


STATE_SQL = "SELECT level, last_run, last_rows, last_error FROM rollup_state ORDER BY level"


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
    """The retention, compaction and rollup settings. `overrides` maps a metric name to the
    levels that metric keeps for a different time than the global level."""

    raw_days: int = 7
    rollup_5m_days: int = 14
    hourly_days: int = 90
    daily_days: int = 730
    history_days: int = 730
    compress_after_days: int = 1
    late_grace_s: int = LATE_GRACE_S
    overrides: dict[str, dict[str, int]] = field(default_factory=dict)


# setting key -> (field, lowest, highest). The keys live in app_settings, where the admin
# endpoint writes them with an audit record.
RETENTION_SETTINGS = {
    "retention.raw_days": ("raw_days", 1, 30),
    "retention.5m_days": ("rollup_5m_days", 1, 365),
    "retention.hourly_days": ("hourly_days", 90, 180),
    "retention.daily_days": ("daily_days", 1, 3650),
    "retention.history_days": ("history_days", 1, 3650),
    "retention.compress_after_days": ("compress_after_days", 1, 30),
    "retention.late_grace_s": ("late_grace_s", 0, 3600),
}
FIELD_KEYS = {spec[0]: key for key, spec in RETENTION_SETTINGS.items()}
FIELD_BOUNDS = {spec[0]: (spec[1], spec[2]) for spec in RETENTION_SETTINGS.values()}

# The levels a single metric may keep for its own time. The history level is not keyed by metric.
OVERRIDE_FIELDS = ("raw_days", "rollup_5m_days", "hourly_days", "daily_days")
OVERRIDES_KEY = "retention.overrides"
MAX_OVERRIDES = 100


def parse_overrides(text: object) -> dict[str, dict[str, int]]:
    """The stored per-metric overrides; anything out of range or malformed is left out."""
    try:
        data = json.loads(text) if isinstance(text, str) else {}
    except ValueError:
        return {}
    out: dict[str, dict[str, int]] = {}
    if not isinstance(data, dict):
        return out
    for metric, levels in data.items():
        if not isinstance(metric, str) or not isinstance(levels, dict):
            continue
        kept = {}
        for name, days in levels.items():
            if name in OVERRIDE_FIELDS and type(days) is int:
                low, high = FIELD_BOUNDS[name]
                if low <= days <= high:
                    kept[name] = days
        if kept:
            out[metric] = kept
    return out


def load_levels(db: Conn, fallback_raw_days: int | None = None) -> RetentionLevels:
    """The admin's retention settings, defaults for a missing or out-of-range value. When no raw
    setting exists, `fallback_raw_days` (server.retention_days) is the raw level."""
    values: dict[str, int] = {}
    overrides: dict[str, dict[str, int]] = {}
    if fallback_raw_days is not None:
        values["raw_days"] = int(fallback_raw_days)
    for key, value in db.execute("SELECT key, value FROM app_settings WHERE key LIKE 'retention.%'"):
        if key == OVERRIDES_KEY:
            overrides = parse_overrides(value)
            continue
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
    return RetentionLevels(**values, overrides=overrides)


def days_for(levels: RetentionLevels, metric: str, name: str) -> int:
    """The days a metric keeps a level: its own override, else the global level."""
    return levels.overrides.get(metric, {}).get(name, getattr(levels, name))


def longest_days(levels: RetentionLevels, name: str) -> int:
    """The longest any metric keeps a level. A chunk of a hypertable can only be dropped when
    every metric in it is past retention, so the policy uses this."""
    return max([getattr(levels, name), *(o[name] for o in levels.overrides.values() if name in o)])


def shortest_days(levels: RetentionLevels, name: str) -> int:
    return min([getattr(levels, name), *(o[name] for o in levels.overrides.values() if name in o)])


def settings_view(levels: RetentionLevels) -> dict:
    """The effective settings as the admin sees them, and as an audit record keeps them."""
    out: dict = {name: getattr(levels, name) for name in FIELD_BOUNDS}
    out["overrides"] = {m: dict(sorted(o.items())) for m, o in sorted(levels.overrides.items())}
    return out


def save_settings(db: Conn, changes: dict[str, str | None], *, now: float, actor: str,
                  remote: str, path: str, fallback_raw_days: int | None = None) -> dict:
    """Inside one write unit: write the changed keys (None resets one to its default), read the
    settings before and after, and append the one audit row that records both. Returns
    {"old": ..., "new": ...}."""
    old = settings_view(load_levels(db, fallback_raw_days))
    for key, value in changes.items():
        if value is None:
            db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            db.execute(
                "INSERT INTO app_settings (key, value, updated) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (key, value, now))
    new = settings_view(load_levels(db, fallback_raw_days))
    db.execute(
        "INSERT INTO audit (ts, actor, kind, method, path, status, remote, detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (now, actor, "retention_settings_changed", "PUT", path, 200, remote,
         json.dumps({"old": old, "new": new}, sort_keys=True)))
    return {"old": old, "new": new}


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


def fold(db: Conn, now: float, bucket_of_ts: Callable[[str, int], str],
         late_grace_s: int = LATE_GRACE_S) -> int:
    """Fold every complete bucket that is new since the last call. Runs inside one write unit.
    `bucket_of_ts(column, width)` is the backend's expression for the start of a bucket of a
    REAL second timestamp. Returns the number of summary rows written."""
    complete = int(now - late_grace_s)
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


def override_cutoffs(db: Conn, now: float, levels: RetentionLevels) -> dict[str, dict[str, float]]:
    """The delete bound of each table for each overridden metric, with the same fold limits as
    the global bounds."""
    folded = {lv: _state(db, lv) for lv in ("5m", "1h", "1d")}

    def done(level: str) -> float:
        return float(folded[level] if folded[level] is not None else 0)

    out = {}
    for metric in levels.overrides:
        def age(name: str, metric: str = metric) -> float:
            return now - days_for(levels, metric, name) * 86400
        out[metric] = {"host_samples": min(age("raw_days"), done("5m")),
                       "rollup_5m": min(age("rollup_5m_days"), done("1h")),
                       "rollup_1h": min(age("hourly_days"), done("1d")),
                       "rollup_1d": age("daily_days")}
    return out


_METRIC_TABLES = (("host_samples", "ts"), ("rollup_5m", "bucket"), ("rollup_1h", "bucket"),
                  ("rollup_1d", "bucket"))


def retention_statements(cut: dict[str, float], *, now: float, audit_retention_days: int,
                         override_cut: dict[str, dict[str, float]] | None = None
                         ) -> list[tuple[str, tuple]]:
    """Every delete as (sql, args) in the order to run them, one write unit each so ingest is
    never held back behind one long delete. The first is the poll rows, whose count is returned
    to the caller. A metric with an override is left out of the global delete of each metric
    table and has a delete of its own with its own bound."""
    override_cut = override_cut or {}
    metrics = tuple(sorted(override_cut))
    skip = f" AND metric NOT IN ({', '.join('?' * len(metrics))})" if metrics else ""

    def glob(table: str, column: str, key: str) -> tuple[str, tuple]:
        return f"DELETE FROM {table} WHERE {column} < ?{skip}", (cut[key], *metrics)

    out = [
        ("DELETE FROM results WHERE ts < ?", (cut["raw"],)),
        glob("host_samples", "ts", "raw_samples"),
        ("DELETE FROM ingest_batches WHERE ts < ?", (cut["raw"],)),
        glob("rollup_5m", "bucket", "rollup_5m"),
        glob("rollup_1h", "bucket", "rollup_1h"),
        glob("rollup_1d", "bucket", "rollup_1d"),
        ("DELETE FROM events WHERE ts < ?", (cut["history"],)),
        ("DELETE FROM host_events WHERE ts < ?", (cut["history"],)),
        ("DELETE FROM audit WHERE ts < ?", (now - audit_retention_days * 86400,)),
        ("DELETE FROM sessions WHERE expires < ?", (now,)),
    ]
    for metric in metrics:
        for table, column in _METRIC_TABLES:
            out.append((f"DELETE FROM {table} WHERE metric = ? AND {column} < ?",
                        (metric, override_cut[metric][table])))
    return out
