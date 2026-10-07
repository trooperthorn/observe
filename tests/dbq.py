"""Test helper: run one SQL statement through the storage writer and return its rows."""

from __future__ import annotations

from typing import Any

from observe.store import Store


def run_sql(store: Store, query: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    return store.storage.write_sync(lambda db: db.execute(query, args).fetchall())


# The stored samples with the names a test reads them by: host, source, metric, labels, unit.
SAMPLE_ROWS = ("(SELECT r.name AS host, sc.name AS source, s.metric AS metric, a.value AS value, "
               "a.ts AS ts, s.unit AS unit, s.attrs AS labels FROM samples a "
               "JOIN series s ON s.id = a.series_id JOIN scopes sc ON sc.id = s.scope_id "
               "JOIN resources r ON r.id = s.resource_id) AS stored")


def put_samples(db: Any, rows: list[tuple[Any, ...]], *, rollups: bool = True) -> None:
    """Store samples inside a write unit. Each row is (ts seconds, host, source, metric, labels
    JSON text, value, unit), the order the old host_samples table took."""
    import json

    from observe.storage import series

    by_host: dict[str, list[series.Point]] = {}
    for ts, host, source, metric, labels, value, unit in rows:
        by_host.setdefault(host, []).append(series.Point(
            source, metric, unit, series.canonical(json.loads(labels)), series.to_ms(ts), value))
    for host, points in by_host.items():
        series.record_points(db, kind="host", name=host, points=points, now=0.0,
                             rollups=rollups)


async def put(storage: Any, rows: list[tuple[Any, ...]]) -> None:
    """Store samples through the writer of any backend (see put_samples for the row layout)."""
    await storage.write(lambda db: put_samples(db, rows, rollups=storage.incremental_rollups),
                        touches=("metrics",))


async def settle(storage: Any) -> None:
    """Make the summary levels current. On SQLite and plain PostgreSQL they always are; on
    TimescaleDB the continuous aggregates are refreshed over their whole range."""
    if getattr(storage, "timescale", False):
        storage._admin_run([f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)"
                            for view in ("rollup_5m", "rollup_1h", "rollup_1d")])


async def run_summary_policies(storage: Any) -> None:
    """Run the retention policies of the summary levels now. On TimescaleDB they trim the
    continuous aggregates on a schedule, so a test that needs the trim to have happened runs the
    jobs itself; on the other backends compaction already trims every level."""
    if getattr(storage, "timescale", False):
        jobs = await storage.fetchall(
            "SELECT j.job_id FROM timescaledb_information.jobs j "
            "JOIN timescaledb_information.continuous_aggregates c "
            "ON c.materialization_hypertable_name = j.hypertable_name "
            "AND c.materialization_hypertable_schema = j.hypertable_schema "
            "WHERE j.proc_name = 'policy_retention' AND c.view_schema = current_schema() "
            "ORDER BY j.job_id")
        storage._admin_run([f"CALL run_job({int(job)})" for (job,) in jobs])
