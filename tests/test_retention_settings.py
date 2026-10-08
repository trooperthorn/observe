"""Admin retention settings: validation, the audit trail, per-metric overrides on SQLite, the
TimescaleDB policy refresh on a fake connection and the admin-only endpoint."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json

import pytest

from observe import retention
from observe.ingest.keys import create_key
from observe.storage import open_storage, pg_timescale, rollups
from observe.storage.postgres import PgStorage
from observe.storage.rollups import RetentionLevels

from .dbq import put as dbq_put
from .test_auth import Env

DAY = 86400


@pytest.fixture
def db(tmp_path):
    s = open_storage(str(tmp_path / "r.db"))
    yield s
    s.close()


class StoreOf:
    """The service needs only the storage of a Store."""

    def __init__(self, storage):
        self.storage = storage


# ---- validation ------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {}, [], "x", {"raw_days": 0}, {"raw_days": 31}, {"raw_days": 5.5}, {"raw_days": "7"},
    {"raw_days": True}, {"hourly_days": 89}, {"hourly_days": 181}, {"history_days": 3651},
    {"compress_after_days": 31}, {"late_grace_s": 60},
    {"nonsense": 1}, {"overrides": []}, {"overrides": {"cpu": {}}},
    {"overrides": {"cpu": {"history_days": 5}}}, {"overrides": {"cpu": {"raw_days": 99}}},
    {"overrides": {"bad name": {"raw_days": 5}}}, {"overrides": {"": {"raw_days": 5}}},
    {"overrides": {f"m{i}": {"raw_days": 5} for i in range(101)}},
])
def test_bad_values_are_refused(body):
    with pytest.raises(retention.RetentionError):
        retention.validate(body)


def test_good_values_become_settings_keys_and_null_resets():
    changes = retention.validate({"raw_days": 30, "hourly_days": 180, "compress_after_days": 2,
                                  "daily_days": None,
                                  "overrides": {"temp": {"raw_days": 2, "daily_days": 5}}})
    assert changes["retention.raw_days"] == "30"
    assert changes["retention.hourly_days"] == "180"
    assert changes["retention.compress_after_days"] == "2"
    assert changes["retention.daily_days"] is None
    assert json.loads(changes[rollups.OVERRIDES_KEY]) == {"temp": {"daily_days": 5, "raw_days": 2}}
    assert retention.validate({"overrides": {}}) == {rollups.OVERRIDES_KEY: None}


# ---- the audit trail -------------------------------------------------------------------------

async def test_each_write_makes_one_audit_entry_with_old_and_new_values(db):
    store = StoreOf(db)
    for body in ({"raw_days": 3}, {"raw_days": 3, "overrides": {"temp": {"raw_days": 1}}}):
        await retention.update_settings(store, body, actor="root", remote="10.0.0.1",
                                        now=1000.0, fallback_raw_days=7)
    rows = await db.fetchall("SELECT actor, kind, method, path, status, remote, detail FROM audit "
                             "ORDER BY id")
    assert len(rows) == 2
    first, second = rows
    assert first[:6] == ("root", "retention_settings_changed", "PUT", retention.PATH, 200,
                         "10.0.0.1")
    d1, d2 = json.loads(first[6]), json.loads(second[6])
    assert d1["old"]["raw_days"] == 7 and d1["new"]["raw_days"] == 3
    assert d1["old"]["overrides"] == {} and d1["new"]["overrides"] == {}
    assert d2["old"]["overrides"] == {} and d2["new"]["overrides"] == {"temp": {"raw_days": 1}}


async def test_a_refused_change_writes_nothing(db):
    with pytest.raises(retention.RetentionError):
        await retention.update_settings(StoreOf(db), {"raw_days": 99}, actor="a", remote="",
                                        now=1.0, fallback_raw_days=7)
    assert await db.fetchall("SELECT COUNT(*) FROM audit") == [(0,)]
    assert await db.fetchall("SELECT COUNT(*) FROM app_settings") == [(0,)]


# ---- per-metric overrides on SQLite ----------------------------------------------------------

async def put(s, ts, value, metric):
    await dbq_put(s, [(ts, "h", "cpu", metric, "{}", value, "C")])


async def counts(s):
    return dict(await s.fetchall(
        "SELECT s.metric, COUNT(*) FROM samples a JOIN series s ON s.id = a.series_id "
        "GROUP BY s.metric"))


async def save(db, changes, now):
    await db.save_retention_settings(changes, now=now, actor="root", remote="",
                                     path=retention.PATH)


async def test_an_override_keeps_and_drops_rows_differently_from_the_global_level(db):
    now = 100 * DAY
    for metric in ("plain", "short", "long"):
        for age in (1, 5, 20):  # days
            await put(db, now - age * DAY, 1.0, metric)
    await save(db, {"retention.raw_days": "7", rollups.OVERRIDES_KEY: json.dumps(
        {"short": {"raw_days": 2}, "long": {"raw_days": 30}})}, now)
    await db.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    # plain keeps the global 7 days, short keeps 2 days, long keeps 30 days.
    assert await counts(db) == {"plain": 2, "short": 1, "long": 3}


async def test_overrides_apply_to_the_rollup_levels_too(db):
    now = 800 * DAY
    for metric in ("plain", "kept"):
        await put(db, now - 20 * DAY, 1.0, metric)
    await save(db, {rollups.OVERRIDES_KEY: json.dumps({"kept": {"rollup_5m_days": 40}})}, now)
    await db.apply_retention(now=now, retention_days=7, audit_retention_days=365)
    assert await db.fetchall("SELECT metric FROM metric_5m") == [("kept",)]
    assert await db.fetchall("SELECT DISTINCT metric FROM metric_hourly ORDER BY 1") == [
        ("kept",), ("plain",)]


async def test_resetting_the_overrides_returns_to_the_global_level(db):
    now = 100 * DAY
    await put(db, now - 5 * DAY, 1.0, "m")
    await save(db, {rollups.OVERRIDES_KEY: json.dumps({"m": {"raw_days": 30}})}, now)
    await save(db, {rollups.OVERRIDES_KEY: None}, now)
    await db.apply_retention(now=now, retention_days=2, audit_retention_days=365)
    assert await counts(db) == {}


def test_timescale_policies_follow_the_overrides_and_compaction_setting():
    levels = RetentionLevels(raw_days=7, compress_after_days=3, overrides={
        "a": {"raw_days": 30}, "b": {"raw_days": 2}})
    text = "\n".join(pg_timescale.policy_statements(levels))
    # Raw chunks are dropped by compaction after the coverage check; the aggregates keep the
    # longest level any metric needs.
    assert "add_retention_policy('samples'" not in text
    assert f"add_retention_policy('rollup_5m', drop_after => {14 * DAY * 1000})" in text
    # compress_after is capped at half the shortest raw level (2 days), never below one chunk.
    assert f"add_compression_policy('samples', compress_after => {DAY * 1000})" in text
    longer = "\n".join(pg_timescale.policy_statements(RetentionLevels(raw_days=30,
                                                                      compress_after_days=3)))
    assert f"compress_after => {3 * DAY * 1000})" in longer


# ---- a settings change re-registers the Timescale policies -----------------------------------

class FakeAdmin:
    def __init__(self):
        self.calls = []
        self.raw = self

    def execute(self, sql, args=None):
        self.calls.append(sql)


class InlineWriter:
    def submit(self, fn, *args):
        future = concurrent.futures.Future()
        future.set_result(fn(*args))
        return future


def fake_pg(timescale, saved):
    pg = object.__new__(PgStorage)
    pg.timescale = timescale
    pg._admin = FakeAdmin()
    pg._writer = InlineWriter()
    levels = RetentionLevels(raw_days=5, overrides={"x": {"raw_days": 20}})

    async def write(unit, *, touches=(), critical=False):
        if touches:
            saved.append(touches)
            return {"old": {}, "new": {}}
        return levels
    pg.write = write
    return pg


def test_a_settings_change_registers_the_timescale_policies_again():
    saved = []
    pg = fake_pg(True, saved)
    asyncio.run(pg.save_retention_settings({"retention.raw_days": "5"}, now=1.0, actor="a",
                                           remote="", path="/p"))
    assert saved == [("admin", "audit")]
    calls = pg._admin.calls
    assert any("remove_retention_policy('samples'" in c for c in calls)
    assert not any("add_retention_policy('samples'" in c for c in calls)
    assert any(f"add_retention_policy('rollup_5m', drop_after => {14 * DAY * 1000})" in c
               for c in calls)
    assert any("add_compression_policy('samples'" in c for c in calls)
    assert any("add_continuous_aggregate_policy('rollup_5m'" in c for c in calls)


def test_plain_postgres_does_not_touch_timescale_policies():
    saved = []
    pg = fake_pg(False, saved)
    asyncio.run(pg.save_retention_settings({"retention.raw_days": "5"}, now=1.0, actor="a",
                                           remote="", path="/p"))
    assert saved and pg._admin.calls == []


# ---- the endpoint ----------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def admin(env):
    env.user("root", admin=True)
    return env.csrf(env.login("root"))


def test_the_endpoint_needs_an_admin_session(env):
    assert env.client.get("/api/v2/admin/settings/retention").status_code == 401
    env.user("bob")
    hdr = env.csrf(env.login("bob"))
    assert env.client.get("/api/v2/admin/settings/retention").status_code == 403
    r = env.client.put("/api/admin/retention", json={"raw_days": 3}, headers=hdr)
    assert r.status_code == 403
    assert env.rows("SELECT COUNT(*) FROM audit WHERE kind LIKE 'retention%'") == [(0,)]


def test_the_endpoint_needs_the_csrf_token(env):
    admin(env)
    assert env.client.put("/api/admin/retention", json={"raw_days": 3}).status_code == 403
    bad = {"X-CSRF-Token": "wrong"}
    assert env.client.put("/api/admin/retention", json={"raw_days": 3}, headers=bad
                          ).status_code == 403
    assert env.rows("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'retention.%'") == [(0,)]


def test_an_ingest_key_of_any_scope_is_not_an_admin_credential(env):
    keys = []
    for scope in ("wpi", "wpc"):
        key, _ = asyncio.run(create_key(env.store, "h", "root", scope=scope))
        keys.append(key)
    for key in keys:
        hdr = {"Authorization": f"Bearer {key}", "X-API-Key": key}
        assert env.client.get("/api/v2/admin/settings/retention", headers=hdr).status_code == 401
        assert env.client.put("/api/admin/retention", json={"raw_days": 3}, headers=hdr
                              ).status_code == 401


def test_an_admin_reads_and_updates_the_settings(env):
    hdr = admin(env)
    got = env.client.get("/api/v2/admin/settings/retention").json()
    assert got["settings"]["raw_days"] == 30  # server.retention_days
    assert got["bounds"]["hourly_days"] == {"min": 90, "max": 180, "default": 90}
    r = env.client.put("/api/admin/retention", headers=hdr, json={
        "raw_days": 4, "overrides": {"cpu_temp": {"raw_days": 20}}})
    assert r.status_code == 200
    assert r.json()["settings"]["raw_days"] == 4
    assert r.json()["settings"]["overrides"] == {"cpu_temp": {"raw_days": 20}}
    rows = env.rows("SELECT actor, detail FROM audit WHERE kind='retention_settings_changed'")
    assert len(rows) == 1 and rows[0][0] == "root"
    detail = json.loads(rows[0][1])
    assert detail["old"]["raw_days"] == 30 and detail["new"]["raw_days"] == 4


def test_a_bad_value_is_refused_and_audited(env):
    hdr = admin(env)
    r = env.client.put("/api/admin/retention", headers=hdr, json={"raw_days": 500})
    assert r.status_code == 422 and "raw_days" in r.json()["detail"]
    assert env.rows("SELECT kind FROM audit WHERE kind LIKE 'retention%'") == [
        ("retention_settings_failed",)]
    assert env.rows("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'retention.%'") == [(0,)]
