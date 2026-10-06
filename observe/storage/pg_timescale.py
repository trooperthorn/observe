"""TimescaleDB objects for the PostgreSQL backend (docs/DATA-API-DESIGN.md section 12).

`host_samples` becomes a hypertable chunked by day on `ts_s`, whole seconds kept by a trigger
(a hypertable needs an integer or timestamp time column and `ts` is a REAL second count). The
5 minute, hourly and daily levels are continuous aggregates named `rollup_5m`, `rollup_1h` and
`rollup_1d`, with the columns of the shared summary tables, so the `metric_*` views are the same
on every backend. The refresh, retention and compression policies are set from the admin's
retention levels, and are set again whenever retention runs, so a changed setting takes effect
at the next compaction.

This module returns statements; the backend runs them on its administration connection, which is
in autocommit mode because continuous aggregates cannot be created inside a transaction.
"""

from __future__ import annotations

from .rollups import (WIDTH_1D, WIDTH_1H, WIDTH_5M, RetentionLevels, longest_days,
                      shortest_days)

HYPERTABLE_CHUNK_S = 86400
INTEGER_NOW_FUNC = "observe_now_s"


def integer_now_statement(relation: str, *, tolerant: bool = False) -> str:
    """Register the integer_now function, which every policy on an integer time column needs.
    A continuous aggregate normally inherits it from its source, so for those the call may be
    refused as already set; the refusal is then reported as a warning, never hidden."""
    call = (f"set_integer_now_func('{relation}', '{INTEGER_NOW_FUNC}', replace_if_exists => TRUE)")
    if not tolerant:
        return f"SELECT {call}"
    return (f"DO $$ BEGIN PERFORM {call}; EXCEPTION WHEN others THEN "
            f"RAISE WARNING 'set_integer_now_func on {relation} failed: %', SQLERRM; END $$")


SETUP = (
    "ALTER TABLE host_samples ADD COLUMN IF NOT EXISTS ts_s BIGINT",
    "UPDATE host_samples SET ts_s = floor(ts)::bigint WHERE ts_s IS NULL",
    "ALTER TABLE host_samples ALTER COLUMN ts_s SET NOT NULL",
    "CREATE OR REPLACE FUNCTION observe_set_ts_s() RETURNS trigger LANGUAGE plpgsql AS "
    "$$ BEGIN NEW.ts_s := floor(NEW.ts)::bigint; RETURN NEW; END $$",
    "DROP TRIGGER IF EXISTS observe_host_samples_ts_s ON host_samples",
    "CREATE TRIGGER observe_host_samples_ts_s BEFORE INSERT OR UPDATE OF ts ON host_samples "
    "FOR EACH ROW EXECUTE FUNCTION observe_set_ts_s()",
    f"SELECT create_hypertable('host_samples', 'ts_s', chunk_time_interval => {HYPERTABLE_CHUNK_S}, "
    "if_not_exists => TRUE, migrate_data => TRUE)",
    "CREATE OR REPLACE FUNCTION observe_now_s() RETURNS bigint LANGUAGE sql STABLE AS "
    "$$ SELECT floor(extract(epoch FROM now()))::bigint $$",
    integer_now_statement("host_samples"),
    "ALTER TABLE host_samples SET (timescaledb.compress, "
    "timescaledb.compress_segmentby = 'host, source, metric', "
    "timescaledb.compress_orderby = 'ts_s DESC')",
    "CREATE MATERIALIZED VIEW IF NOT EXISTS rollup_5m WITH (timescaledb.continuous) AS "
    f"SELECT host, source, metric, labels, time_bucket({WIDTH_5M}::bigint, ts_s) AS bucket, "
    "count(value) AS n, sum(value) AS sum_v, min(value) AS min_v, max(value) AS max_v "
    "FROM host_samples WHERE value IS NOT NULL "
    f"GROUP BY host, source, metric, labels, time_bucket({WIDTH_5M}::bigint, ts_s) WITH NO DATA",
    "CREATE MATERIALIZED VIEW IF NOT EXISTS rollup_1h WITH (timescaledb.continuous) AS "
    f"SELECT host, source, metric, labels, time_bucket({WIDTH_1H}::bigint, bucket) AS bucket, "
    "sum(n) AS n, sum(sum_v) AS sum_v, min(min_v) AS min_v, max(max_v) AS max_v "
    "FROM rollup_5m "
    f"GROUP BY host, source, metric, labels, time_bucket({WIDTH_1H}::bigint, bucket) WITH NO DATA",
    "CREATE MATERIALIZED VIEW IF NOT EXISTS rollup_1d WITH (timescaledb.continuous) AS "
    f"SELECT host, source, metric, labels, time_bucket({WIDTH_1D}::bigint, bucket) AS bucket, "
    "sum(n) AS n, sum(sum_v) AS sum_v, min(min_v) AS min_v, max(max_v) AS max_v "
    "FROM rollup_1h "
    f"GROUP BY host, source, metric, labels, time_bucket({WIDTH_1D}::bigint, bucket) WITH NO DATA",
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
                 "WHERE hypertable_name = 'host_samples'")


def refresh_start_offsets(levels: RetentionLevels) -> dict[str, int]:
    """How far back each continuous aggregate is refreshed. A refresh window must stay inside
    the retention of its source, or the aggregate would be rebuilt from nothing and lose the
    buckets whose source rows are gone, so each is at most half the source's retention."""
    # The shortest any metric keeps a level, so a window never reaches rows an override
    # dropped.
    raw_s = shortest_days(levels, "raw_days") * 86400

    def aligned(seconds: int, width: int) -> int:
        # A multiple of the bucket width, and at least three buckets so the window always holds
        # whole buckets after the end offset of one bucket is taken off.
        return max(3 * width, seconds // width * width)

    return {
        "rollup_5m": aligned(min(86400, raw_s // 2), WIDTH_5M),
        "rollup_1h": aligned(min(7 * 86400, shortest_days(levels, "rollup_5m_days") * 86400 // 2), WIDTH_1H),
        "rollup_1d": aligned(min(30 * 86400, shortest_days(levels, "hourly_days") * 86400 // 2), WIDTH_1D),
    }


def _retention_days(levels: RetentionLevels) -> list[tuple[str, int]]:
    """Days each relation keeps whole chunks. A chunk can only be dropped once every metric in
    it is past retention, so a per-metric override maps to the longest of the global level and
    the overrides. Raw samples of a metric kept for less are deleted by row (see
    PgStorage.apply_retention); a continuous aggregate cannot be deleted from, so its metrics
    keep the longest level."""
    return [("host_samples", longest_days(levels, "raw_days")),
            ("rollup_5m", longest_days(levels, "rollup_5m_days")),
            ("rollup_1h", longest_days(levels, "hourly_days")),
            ("rollup_1d", longest_days(levels, "daily_days"))]


def policy_statements(levels: RetentionLevels) -> list[str]:
    """Remove and add every policy from the retention levels, so the call is repeatable."""
    starts = refresh_start_offsets(levels)
    raw_s = shortest_days(levels, "raw_days") * 86400
    out: list[str] = [integer_now_statement("host_samples")]
    for view in ("rollup_5m", "rollup_1h", "rollup_1d"):
        out.append(integer_now_statement(view, tolerant=True))
    for view, width, every in (("rollup_5m", WIDTH_5M, "5 minutes"), ("rollup_1h", WIDTH_1H,
                                                                      "1 hour"),
                               ("rollup_1d", WIDTH_1D, "1 day")):
        out.append(f"SELECT remove_continuous_aggregate_policy('{view}', if_exists => TRUE)")
        out.append(f"SELECT add_continuous_aggregate_policy('{view}', start_offset => "
                   f"{starts[view]}, end_offset => {width}, schedule_interval => INTERVAL '{every}')")
    for table, days in _retention_days(levels):
        out.append(f"SELECT remove_retention_policy('{table}', if_exists => TRUE)")
        out.append(f"SELECT add_retention_policy('{table}', drop_after => {days * 86400})")
    out.append("SELECT remove_compression_policy('host_samples', if_exists => TRUE)")
    out.append("SELECT add_compression_policy('host_samples', compress_after => "
               f"{max(HYPERTABLE_CHUNK_S, min(levels.compress_after_days * 86400, raw_s // 2))})")
    return out


def refresh_statements(now: float, levels: RetentionLevels) -> list[str]:
    """Refresh each aggregate over its recent window now, in level order, instead of waiting for
    the policy. Complete buckets only."""
    starts = refresh_start_offsets(levels)
    complete = int(now) - 60
    out = []
    for view, width in (("rollup_5m", WIDTH_5M), ("rollup_1h", WIDTH_1H), ("rollup_1d", WIDTH_1D)):
        end = complete // width * width
        start = min((int(now) - starts[view]) // width * width, end - width)
        out.append(f"CALL refresh_continuous_aggregate('{view}', {start}, {end})")
    return out


def drop_statements(now: float, levels: RetentionLevels) -> list[str]:
    """Drop chunks past retention now, instead of waiting for the policy."""
    out = []
    for table, days in _retention_days(levels):
        out.append(f"SELECT drop_chunks('{table}', older_than => {int(now) - days * 86400})")
    return out
