"""The TimescaleDB statements and the connection adapter, checked as text and against a fake
driver connection. The statements run for real in CI on a TimescaleDB service container."""

from __future__ import annotations

import pytest

from observe.storage import pg_timescale
from observe.storage.postgres import PgConn
from observe.storage.rollups import RetentionLevels


class FakeCursor:
    def __init__(self, description=None, rows=()):
        self.description, self.rows, self.rowcount = description, list(rows), len(rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeRaw:
    def __init__(self):
        self.calls = []

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        return FakeCursor(description=[("x",)] if sql.lstrip().upper().startswith("SELECT") else None,
                          rows=[(1,)])


def test_the_adapter_rewrites_sql_and_converts_flags():
    raw = FakeRaw()
    db = PgConn(raw)
    db.execute("INSERT OR IGNORE INTO t (a, b) VALUES (?, ?)", (True, 5))
    assert raw.calls[-1] == ("INSERT INTO t (a, b) VALUES (%s, %s) ON CONFLICT DO NOTHING", (1, 5))
    db.execute("SELECT key FROM s WHERE key LIKE 'retention.%'")
    assert raw.calls[-1] == ("SELECT key FROM s WHERE key LIKE 'retention.%'", None) or \
        raw.calls[-1][1] is None


def test_a_statement_that_returns_no_rows_fetches_an_empty_list():
    db = PgConn(FakeRaw())
    assert db.execute("UPDATE t SET a=?", (1,)).fetchall() == []
    assert db.execute("UPDATE t SET a=?", (1,)).fetchone() is None
    assert db.execute("SELECT 1").fetchall() == [(1,)]


def test_pragma_table_info_runs_the_catalogue_query():
    raw = FakeRaw()
    PgConn(raw).execute("PRAGMA table_info(ingest_keys)")
    sql, args = raw.calls[-1]
    assert "information_schema.columns" in sql and args == ("ingest_keys",)


def test_the_hypertable_is_chunked_by_day_on_whole_seconds():
    text = " ".join(pg_timescale.SETUP)
    assert "create_hypertable('host_samples', 'ts_s'" in text
    assert "chunk_time_interval => 86400" in text
    assert "timescaledb.compress_segmentby" in text
    for name in ("rollup_5m", "rollup_1h", "rollup_1d"):
        assert f"CREATE MATERIALIZED VIEW IF NOT EXISTS {name} WITH (timescaledb.continuous)" in text


def test_the_policies_follow_the_retention_levels():
    levels = RetentionLevels(raw_days=3, rollup_5m_days=10, hourly_days=100, daily_days=400)
    text = "\n".join(pg_timescale.policy_statements(levels))
    assert "add_retention_policy('host_samples', drop_after => 259200)" in text
    assert "add_retention_policy('rollup_5m', drop_after => 864000)" in text
    assert "add_retention_policy('rollup_1h', drop_after => 8640000)" in text
    assert "add_retention_policy('rollup_1d', drop_after => 34560000)" in text
    assert "remove_retention_policy('host_samples', if_exists => TRUE)" in text  # repeatable
    assert "add_compression_policy('host_samples'" in text
    assert text.count("add_continuous_aggregate_policy") == 3


@pytest.mark.parametrize("raw_days", [1, 7, 30])
def test_a_refresh_window_stays_inside_the_retention_of_its_source(raw_days):
    levels = RetentionLevels(raw_days=raw_days)
    starts = pg_timescale.refresh_start_offsets(levels)
    assert starts["rollup_5m"] <= raw_days * 86400 // 2 or starts["rollup_5m"] == 900
    assert starts["rollup_1h"] <= levels.rollup_5m_days * 86400
    assert starts["rollup_1d"] <= levels.hourly_days * 86400
    assert starts["rollup_5m"] >= 900  # at least two buckets plus the end offset


def test_a_refresh_covers_complete_buckets_in_level_order():
    calls = pg_timescale.refresh_statements(1_000_000_000.0, RetentionLevels())
    assert [c.split("'")[1] for c in calls] == ["rollup_5m", "rollup_1h", "rollup_1d"]
    for call in calls:
        start, end = (int(x) for x in call.rstrip(")").rsplit(",", 2)[1:])
        assert start < end <= 1_000_000_000 - 60


class FakeAdmin(FakeRaw):
    """A raw connection that records whether autocommit was on for every statement."""

    def __init__(self, autocommit=False):
        super().__init__()
        self.autocommit = autocommit
        self.commits = 0
        self.modes = []

    def commit(self):
        self.commits += 1

    def execute(self, sql, args=None):
        self.modes.append(self.autocommit)
        return super().execute(sql, args)


def test_the_setup_runs_in_autocommit_after_closing_any_open_transaction():
    raw = FakeAdmin(autocommit=False)
    pg_timescale.apply_setup(raw)
    assert raw.commits == 1 and raw.autocommit is True
    assert len(raw.calls) == len(pg_timescale.SETUP)
    assert all(raw.modes)
    cagg = [sql for sql, _ in raw.calls if "timescaledb.continuous" in sql]
    assert len(cagg) == 3 and all(m for sql, m in zip(raw.calls, raw.modes) if "continuous" in sql[0])


def test_integer_now_is_registered_before_any_policy_is_added():
    stmts = pg_timescale.policy_statements(RetentionLevels())
    first_add = next(i for i, s in enumerate(stmts) if "add_" in s and "_policy" in s)
    registered = [i for i, s in enumerate(stmts) if "set_integer_now_func('host_samples'" in s]
    assert registered and registered[0] < first_add
    setup = pg_timescale.SETUP
    assert (next(i for i, s in enumerate(setup) if "set_integer_now_func" in s)
            < next(i for i, s in enumerate(setup) if "timescaledb.compress" in s))


@pytest.mark.parametrize("raw_days", [1, 3, 7, 45])
def test_refresh_windows_and_policy_offsets_are_multiples_of_the_bucket_width(raw_days):
    levels = RetentionLevels(raw_days=raw_days, rollup_5m_days=11, hourly_days=13)
    widths = {"rollup_5m": 300, "rollup_1h": 3600, "rollup_1d": 86400}
    for view, start in pg_timescale.refresh_start_offsets(levels).items():
        assert start % widths[view] == 0
    for call in pg_timescale.refresh_statements(1_000_000_123.0, levels):
        view = call.split("'")[1]
        start, end = (int(x) for x in call.rstrip(")").rsplit(",", 2)[1:])
        assert start % widths[view] == 0 and end % widths[view] == 0 and start < end
