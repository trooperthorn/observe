"""The summary levels, the summary views and the retention levels, shared by both backends.

Raw samples (`samples`, keyed by series id and millisecond timestamp) are summarised in three
tables: `rollup_5m`, `rollup_1h` and `rollup_1d`, each holding the count, sum, minimum and maximum
of the non-null values per series and bucket. A bucket is the millisecond timestamp of its start.
On SQLite and plain PostgreSQL the writer folds every new point into all three levels in the same
transaction as the point (observe/storage/series.py), so the levels are always current and are
never recomputed from raw data at read time. On PostgreSQL with TimescaleDB the same three names
are continuous aggregates over `samples` (observe/storage/pg_timescale.py), so the views over them
are the same text on every backend (docs/DATA-API-DESIGN.md sections 10.2 and 12). Retention and
compaction of the levels are in observe/storage/compaction.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .base import Conn

WIDTH_5M, WIDTH_1H, WIDTH_1D = 300, 3600, 86400
MS = 1000

# level name -> (table, bucket width in seconds)
LEVELS = {"5m": ("rollup_5m", WIDTH_5M), "1h": ("rollup_1h", WIDTH_1H),
          "1d": ("rollup_1d", WIDTH_1D)}


def _table(name: str) -> str:
    return (f"CREATE TABLE IF NOT EXISTS {name} (series_id INTEGER NOT NULL, bucket INTEGER NOT NULL, "
            "n INTEGER NOT NULL, sum_v REAL, min_v REAL, max_v REAL, PRIMARY KEY (series_id, bucket)) "
            "WITHOUT ROWID")


ROLLUP_TABLES = (
    _table("rollup_5m"), _table("rollup_1h"), _table("rollup_1d"),
    # One row per compacted level (raw, 5m, 1h, 1d): the time before which it was last trimmed
    # and verified, when it last ran, the rows it removed and the first coverage problem it met.
    # The row named MAINTENANCE_LEVEL is the whole compaction pass: when it last ran, the ingest batch
    # records it removed and its error. This table is always last, because TimescaleDB replaces the
    # summary tables above it with continuous aggregates.
    "CREATE TABLE IF NOT EXISTS rollup_state (level TEXT PRIMARY KEY, upto INTEGER NOT NULL, "
    "last_run REAL NOT NULL DEFAULT 0, last_rows INTEGER NOT NULL DEFAULT 0, "
    "last_error TEXT NOT NULL DEFAULT '')",
)


MAINTENANCE_LEVEL = "compaction"
MAX_ERROR_CHARS = 200


def note_maintenance(db: Conn, now: float, rows: int, error: str) -> None:
    """Record one compaction pass: when it ran, the batch records it removed and its error, if any."""
    db.execute(
        "INSERT INTO rollup_state (level, upto, last_run, last_rows, last_error) "
        "VALUES (?, 0, ?, ?, ?) ON CONFLICT (level) DO UPDATE SET last_run = excluded.last_run, "
        "last_rows = excluded.last_rows, last_error = excluded.last_error",
        (MAINTENANCE_LEVEL, now, rows, error[:MAX_ERROR_CHARS]))


STATE_SQL = "SELECT level, last_run, last_rows, last_error FROM rollup_state ORDER BY level"

# The series a summary row belongs to, named: the resource, the scope (the producer's source) and
# the metric come from the small identity tables. Bucket is shown in whole seconds.
_VIEW_SELECT = (
    "SELECT m.series_id AS series_id, r.name AS resource, sc.name AS scope, s.metric AS metric, "
    "s.unit AS unit, s.attrs AS attrs, m.bucket / 1000 AS bucket, CAST(m.n AS BIGINT) AS n, "
    "m.sum_v AS sum_v, m.min_v AS min_v, m.max_v AS max_v, m.sum_v / NULLIF(m.n, 0) AS avg_v "
    "FROM {source} m JOIN series s ON s.id = m.series_id "
    "JOIN resources r ON r.id = s.resource_id JOIN scopes sc ON sc.id = s.scope_id")


def _view(name: str, source: str) -> str:
    return f"CREATE VIEW IF NOT EXISTS {name} AS " + _VIEW_SELECT.format(source=source)


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
    "metric_5m": ("series_id", "resource", "scope", "metric", "unit", "attrs", "bucket", "n",
                  "sum_v", "min_v", "max_v", "avg_v"),
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
