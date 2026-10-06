"""The summary-view and latest-table reads in the PostgreSQL dialect, and the TimescaleDB
variant of the same views, checked as text and on the PostgreSQL dialect fake (no server).

The live cases that need a real server are in tests/test_storage.py and skip without
OBSERVE_TEST_PG_DSN."""

from __future__ import annotations

import re
import time

import pytest

from observe.checks.base import CheckResult, Result
from observe.storage import pg_timescale, rollups
from observe.storage.pg_dialect import translate_sql
from observe.storage.postgres import PgConn
from observe.store import MONITOR_SCOPE

from .fakes.pg_fake import SQLITE_ONLY, PgFakeStorage, _outside_quotes
from .test_pg_timescale import FakeRaw
from .test_storage import _store_on


@pytest.fixture
def pg():
    s = PgFakeStorage()
    yield s
    s.close()


def _is_postgres_text(sql: str) -> None:
    bare = _outside_quotes(sql)
    assert not SQLITE_ONLY.search(bare), sql
    assert "?" not in bare, sql


# ---- the view definitions -----------------------------------------------------------------

@pytest.mark.parametrize("view", rollups.METRIC_VIEWS)
def test_every_view_definition_rewrites_to_postgresql(view):
    out = translate_sql(view, has_args=False)
    _is_postgres_text(out)
    assert out.startswith("CREATE OR REPLACE VIEW ")


def test_the_summary_views_read_only_the_summary_tables_with_portable_casts():
    for name, table in (("metric_5m", "rollup_5m"), ("metric_hourly", "rollup_1h"),
                        ("metric_daily", "rollup_1d")):
        out = translate_sql(next(v for v in rollups.METRIC_VIEWS if f" {name} " in v),
                            has_args=False)
        assert f"FROM {table} m JOIN series s ON s.id = m.series_id" in out
        assert "CAST(m.n AS BIGINT) AS n" in out  # sum() of bigint is numeric on PostgreSQL
        assert "m.bucket / 1000 AS bucket" in out and "NULLIF(m.n, 0)" in out
        assert "samples" not in out  # there is no view over raw samples


def test_the_availability_and_hourly_reads_are_postgresql_text():
    sqls = (
        f"SELECT CAST(COALESCE(SUM(n), 0) AS BIGINT), "
        f"CAST(COALESCE(SUM(sum_v), 0) AS DOUBLE PRECISION) FROM metric_5m "
        "WHERE resource = ? AND scope = ? AND metric = ? AND bucket >= ?",
        "SELECT bucket, avg_v FROM metric_hourly WHERE resource = ? AND scope = ? "
        "AND metric = ? AND bucket >= ? AND avg_v IS NOT NULL ORDER BY bucket",
    )
    for sql in sqls:
        out = translate_sql(sql)
        _is_postgres_text(out)
        assert out.count("%s") == 4


# ---- what the reads actually send ---------------------------------------------------------

async def test_the_monitor_reads_send_postgresql_text_and_return_plain_values(pg):
    st = _store_on(pg)
    now = time.time()
    for i, res in enumerate([Result.OK, Result.OK, Result.WARN, Result.FAIL]):
        await st.record("m", now - 10 - i, CheckResult(res, "", value=5.0, latency_ms=2.0))
    got = await st.availability("m", 1)
    assert got == 75.0 and type(got) is float
    last = await st.last_result("m")
    assert last is not None and last[1] is Result.OK and type(last[0]) is float
    hourly = await st.hourly_series("m", 1, now=now + 7200)
    assert hourly and all(type(t) is float and type(v) is float for t, v in hourly)
    texts = [s for s in pg.statements if "metric_5m" in s or "FROM latest" in s]
    assert any("FROM latest l" in s for s in texts)
    assert any("FROM metric_5m WHERE resource = %s AND scope = %s AND metric = %s "
               "AND bucket >= %s" in s for s in texts)
    assert all("?" not in _outside_quotes(s) for s in pg.statements)


async def test_the_latest_table_read_names_the_monitor_scope_and_metric(pg):
    st = _store_on(pg)
    await st.record("m", time.time(), CheckResult(Result.FAIL, "x"))
    pg.statements.clear()
    assert (await st.last_result("m"))[1] is Result.FAIL
    (sql,) = pg.statements
    assert sql.count("%s") == 3
    assert "r.kind = 'monitor'" in sql and "l.ts, l.value FROM latest l" in sql
    assert MONITOR_SCOPE == "observe-monitor"


# ---- the TimescaleDB variant --------------------------------------------------------------

def test_the_timescale_step_statements_compile_through_the_adapter():
    raw = FakeRaw()
    db = PgConn(raw)
    for stmt in (rollups.ROLLUP_TABLES[-1], *rollups.METRIC_VIEWS):
        db.execute(stmt)
    assert len(raw.calls) == 1 + len(rollups.METRIC_VIEWS)
    for sql, _args in raw.calls:
        _is_postgres_text(sql)
    views = [sql for sql, _ in raw.calls if "VIEW" in sql]
    assert all(v.startswith("CREATE OR REPLACE VIEW ") for v in views)
    state = raw.calls[0][0]
    assert "last_run DOUBLE PRECISION" in state and "WITHOUT" not in state


def test_the_views_read_columns_that_the_continuous_aggregates_provide():
    setup = " ".join(pg_timescale.SETUP)
    for table in ("rollup_5m", "rollup_1h", "rollup_1d"):
        assert f"CREATE MATERIALIZED VIEW IF NOT EXISTS {table} " in setup
    aggregate = re.findall(r"SELECT series_id, time_bucket\(\d+::bigint, \w+\) AS bucket, (.*?) "
                           r"FROM", setup)
    assert len(aggregate) == 3
    for cols in aggregate:
        assert [c.split(" AS ")[1] for c in cols.split(", ")] == ["n", "sum_v", "min_v", "max_v"]
    # Every column a view selects from the summary table is one of those.
    selected = set(re.findall(r"m\.(\w+)", rollups._VIEW_SELECT))
    assert selected <= {"series_id", "bucket", "n", "sum_v", "min_v", "max_v"}
    # The view casts the count because sum() of a bigint is numeric on PostgreSQL.
    assert "CAST(m.n AS BIGINT)" in rollups._VIEW_SELECT
