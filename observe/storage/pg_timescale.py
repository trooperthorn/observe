"""TimescaleDB objects for the PostgreSQL backend (docs/DATA-API-DESIGN.md section 12).

`samples` becomes a hypertable chunked by day on `ts`, whole milliseconds. The 5 minute, hourly and
daily levels are continuous aggregates named `rollup_5m`, `rollup_1h` and `rollup_1d`, with the
columns of the shared summary tables, so the `metric_*` views are the same on every backend. The
refresh and compression policies, and the retention policies of the aggregates, are set from the
admin's retention levels, and are set again whenever retention runs, so a changed setting takes
effect at the next compaction. Raw chunks have no retention policy: compaction drops them with
`drop_chunks` once the aggregates are refreshed and cover them (PgStorage.drop_raw).

This module returns statements; the backend runs them on its administration connection, which is
in autocommit mode because continuous aggregates cannot be created inside a transaction.
"""

from __future__ import annotations

from .rollups import RetentionLevels, longest_days, shortest_days
from .series import MS_1D, MS_1H, MS_5M

HYPERTABLE_CHUNK_MS = MS_1D
INTEGER_NOW_FUNC = "observe_now_ms"
# A bucket is refreshed once it ended this long ago, so a point that is a little late still lands
# in a bucket that has not been materialised yet.
COMPLETE_GRACE_MS = 60_000


def integer_now_statement(relation: str, *, tolerant: bool = False) -> str:
    """Register the integer_now function, which every policy on an integer time column needs.
    A continuous aggregate normally inherits it from its source, so for those the call may be
    refused as already set; the refusal is then reported as a warning, never hidden."""
    call = (f"set_integer_now_func('{relation}', '{INTEGER_NOW_FUNC}', replace_if_exists => TRUE)")
    if not tolerant:
        return f"SELECT {call}"
    return (f"DO $$ BEGIN PERFORM {call}; EXCEPTION WHEN others THEN "
            f"RAISE WARNING 'set_integer_now_func on {relation} failed: %', SQLERRM; END $$")


def _aggregate(name: str, width: int, source: str, level_below: bool) -> str:
    bucket = f"time_bucket({width}::bigint, {'bucket' if level_below else 'ts'})"
    if level_below:
        cols = "sum(n) AS n, sum(sum_v) AS sum_v, min(min_v) AS min_v, max(max_v) AS max_v"
        where = ""
    else:
        cols = "count(value) AS n, sum(value) AS sum_v, min(value) AS min_v, max(value) AS max_v"
        where = " WHERE value IS NOT NULL"
    return (f"CREATE MATERIALIZED VIEW IF NOT EXISTS {name} WITH (timescaledb.continuous) AS "
            f"SELECT series_id, {bucket} AS bucket, {cols} FROM {source}{where} "
            f"GROUP BY series_id, {bucket} WITH NO DATA")


SETUP = (
    "CREATE OR REPLACE FUNCTION observe_now_ms() RETURNS bigint LANGUAGE sql STABLE AS "
    "$$ SELECT floor(extract(epoch FROM now()) * 1000)::bigint $$",
    f"SELECT create_hypertable('samples', 'ts', chunk_time_interval => {HYPERTABLE_CHUNK_MS}, "
    "if_not_exists => TRUE, migrate_data => TRUE)",
    integer_now_statement("samples"),
    "ALTER TABLE samples SET (timescaledb.compress, timescaledb.compress_segmentby = 'series_id', "
    "timescaledb.compress_orderby = 'ts DESC')",
    _aggregate("rollup_5m", MS_5M, "samples", False),
    _aggregate("rollup_1h", MS_1H, "rollup_5m", True),
    _aggregate("rollup_1d", MS_1D, "rollup_1h", True),
)


def apply_setup(conn) -> None:
    """Run SETUP on a raw driver connection. Continuous aggregates cannot be created inside a
    transaction block, so any open transaction is committed and autocommit is switched on first,
    and the connection is left in autocommit."""
    if not getattr(conn, "autocommit", False):
        conn.commit()
        conn.autocommit = True
    for stmt in SETUP:
        conn.execute(stmt)


IS_HYPERTABLE = ("SELECT 1 FROM timescaledb_information.hypertables "
                 "WHERE hypertable_name = 'samples'")


def refresh_start_offsets(levels: RetentionLevels) -> dict[str, int]:
    """How far back each continuous aggregate is refreshed, in milliseconds. A refresh window must
    stay inside the retention of its source, or the aggregate would be rebuilt from nothing and
    lose the buckets whose source rows are gone, so each is at most half the source's
    retention."""
    # The shortest any metric keeps a level, so a window never reaches rows an override
    # dropped.
    raw_ms = shortest_days(levels, "raw_days") * MS_1D

    def aligned(ms: int, width: int) -> int:
        # A multiple of the bucket width, and at least three buckets so the window always holds
        # whole buckets after the end offset of one bucket is taken off.
        return max(3 * width, ms // width * width)

    return {
        "rollup_5m": aligned(min(MS_1D, raw_ms // 2), MS_5M),
        "rollup_1h": aligned(min(7 * MS_1D, shortest_days(levels, "rollup_5m_days") * MS_1D // 2),
                             MS_1H),
        "rollup_1d": aligned(min(30 * MS_1D, shortest_days(levels, "hourly_days") * MS_1D // 2),
                             MS_1D),
    }


def _aggregate_retention(levels: RetentionLevels) -> list[tuple[str, int]]:
    """Days each continuous aggregate keeps whole chunks. A continuous aggregate cannot be deleted
    from by metric, so a per-metric override maps to the longest of the global level and the
    overrides."""
    return [("rollup_5m", longest_days(levels, "rollup_5m_days")),
            ("rollup_1h", longest_days(levels, "hourly_days")),
            ("rollup_1d", longest_days(levels, "daily_days"))]


def policy_statements(levels: RetentionLevels) -> list[str]:
    """Remove and add every policy from the retention levels, so the call is repeatable. Raw
    samples get no retention policy: compaction drops verified chunks."""
    starts = refresh_start_offsets(levels)
    raw_ms = shortest_days(levels, "raw_days") * MS_1D
    out: list[str] = [integer_now_statement("samples")]
    for view in ("rollup_5m", "rollup_1h", "rollup_1d"):
        out.append(integer_now_statement(view, tolerant=True))
    for view, width, every in (("rollup_5m", MS_5M, "5 minutes"), ("rollup_1h", MS_1H, "1 hour"),
                               ("rollup_1d", MS_1D, "1 day")):
        out.append(f"SELECT remove_continuous_aggregate_policy('{view}', if_exists => TRUE)")
        out.append(f"SELECT add_continuous_aggregate_policy('{view}', start_offset => "
                   f"{starts[view]}, end_offset => {width}, schedule_interval => INTERVAL '{every}')")
    out.append("SELECT remove_retention_policy('samples', if_exists => TRUE)")
    for view, days in _aggregate_retention(levels):
        out.append(f"SELECT remove_retention_policy('{view}', if_exists => TRUE)")
        out.append(f"SELECT add_retention_policy('{view}', drop_after => {days * MS_1D})")
    out.append("SELECT remove_compression_policy('samples', if_exists => TRUE)")
    out.append("SELECT add_compression_policy('samples', compress_after => "
               f"{max(HYPERTABLE_CHUNK_MS, min(levels.compress_after_days * MS_1D, raw_ms // 2))})")
    return out


def refresh_statements(now: float, levels: RetentionLevels) -> list[str]:
    """Refresh each aggregate over its recent window now, in level order, instead of waiting for
    the policy. Complete buckets only."""
    starts = refresh_start_offsets(levels)
    complete = int(now * 1000) - COMPLETE_GRACE_MS
    out = []
    for view, width in (("rollup_5m", MS_5M), ("rollup_1h", MS_1H), ("rollup_1d", MS_1D)):
        end = complete // width * width
        start = min((int(now * 1000) - starts[view]) // width * width, end - width)
        out.append(f"CALL refresh_continuous_aggregate('{view}', {start}, {end})")
    return out


def drop_raw_statement(older_than_ms: int) -> str:
    """Drop whole raw chunks that end before the bound."""
    return f"SELECT drop_chunks('samples', older_than => {older_than_ms})"
