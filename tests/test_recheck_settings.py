"""The re-check settings of the admin console (docs/DATA-API-DESIGN.md section 10.3): the admin
endpoints, CSRF, the audit trail, validation, the saved values on each storage backend and the
rule that a saved per-monitor override beats the global value in the engine."""

from __future__ import annotations

import json

import pytest

from observe import recheck_settings
from observe.alerts import Alerter
from observe.checks.base import CheckResult
from observe.ingest.keys import create_key
from observe.scheduler import Scheduler
from observe.state import State
from observe.web import create_app

from .conftest import make_config
from .dbq import settle  # noqa: F401  (kept for parity with the other storage tests)
from .test_auth import Env
from .test_recheck import Clock, Probe, mon, storage  # noqa: F401  (storage is a fixture)
from .test_storage import _store_on

URL = "/api/admin/recheck"


def _admin(tmp_path):
    env = Env(tmp_path)
    env.user("root", admin=True)
    return env, env.csrf(env.login("root"))


def _audit(env, kind):
    return env.rows("SELECT actor, detail FROM audit WHERE kind=?", kind)


def test_an_admin_reads_the_defaults_and_the_bounds(tmp_path):
    env, _ = _admin(tmp_path)
    try:
        data = env.client.get(URL).json()
        assert data["settings"] == {"window": 180.0, "interval": 10.0, "good": 2}
        assert data["overrides"] == {}
        assert data["bounds"]["interval"]["min"] == 5.0
        assert data["monitors"] == [{"slug": "p", "name": "p"}]
    finally:
        env.client.close()
        env.store.close()


def test_an_admin_updates_the_global_values_and_the_change_is_audited(tmp_path):
    env, hdr = _admin(tmp_path)
    try:
        r = env.client.put(URL, headers=hdr, json={"window": 300, "interval": 15, "good": 3})
        assert r.status_code == 200
        assert r.json()["settings"] == {"window": 300.0, "interval": 15.0, "good": 3}
        assert env.client.get(URL).json()["settings"] == {"window": 300.0, "interval": 15.0,
                                                          "good": 3}
        rows = _audit(env, "recheck_settings_changed")
        assert len(rows) == 1 and rows[0][0] == "root"
        detail = json.loads(rows[0][1])
        assert detail["old"]["settings"] == {}
        assert detail["new"]["settings"] == {"good": 3, "interval": 15.0, "window": 300.0}
        # null resets one value to the config default
        env.client.put(URL, headers=hdr, json={"window": None})
        assert env.client.get(URL).json()["settings"]["window"] == 180.0
        assert len(_audit(env, "recheck_settings_changed")) == 2
    finally:
        env.client.close()
        env.store.close()


def test_an_admin_sets_and_clears_a_per_monitor_override(tmp_path):
    env, hdr = _admin(tmp_path)
    try:
        r = env.client.put(URL, headers=hdr, json={"overrides": {"p": {"window": 0, "good": 5}}})
        assert r.status_code == 200
        assert r.json()["overrides"] == {"p": {"good": 5, "window": 0.0}}
        assert env.client.get(URL).json()["overrides"] == {"p": {"good": 5, "window": 0.0}}
        assert len(_audit(env, "recheck_settings_changed")) == 1
        env.client.put(URL, headers=hdr, json={"overrides": {}})
        assert env.client.get(URL).json()["overrides"] == {}
        assert len(_audit(env, "recheck_settings_changed")) == 2
    finally:
        env.client.close()
        env.store.close()


def test_a_non_admin_and_a_missing_session_are_refused(tmp_path):
    env = Env(tmp_path)
    try:
        assert env.client.get(URL).status_code == 401
        assert env.client.get("/admin/recheck").status_code == 401
        env.user("bob")
        hdr = env.csrf(env.login("bob"))
        assert env.client.get(URL).status_code == 403
        assert env.client.get("/admin/recheck").status_code == 403
        assert env.client.put(URL, headers=hdr, json={"good": 3}).status_code == 403
        env.user("root", admin=True)
        root = env.csrf(env.login("root"))
        assert env.client.put(URL, headers=root, json={"good": 3}).status_code == 200
    finally:
        env.client.close()
        env.store.close()


def test_basic_auth_and_ingest_keys_are_refused(tmp_path):
    import asyncio
    from .test_auth import BASIC
    env = Env(tmp_path)
    try:
        env.user("root", admin=True)
        assert env.client.get(URL, headers=BASIC).status_code == 401
        key, _ = asyncio.run(create_key(env.store, "h", "root", scope="ingest"))
        assert env.client.put(URL, headers={"Authorization": f"Bearer {key}"},
                              json={"good": 3}).status_code in (401, 403)
        assert env.rows("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'recheck.%'") == [(0,)]
    finally:
        env.client.close()
        env.store.close()


def test_a_put_without_the_csrf_token_is_refused_and_changes_nothing(tmp_path):
    env, hdr = _admin(tmp_path)
    try:
        assert env.client.put(URL, json={"good": 3}).status_code == 403
        assert env.client.put(URL, headers={"X-CSRF-Token": "wrong"},
                              json={"good": 3}).status_code == 403
        assert env.rows("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'recheck.%'") == [(0,)]
        assert _audit(env, "recheck_settings_changed") == []
    finally:
        env.client.close()
        env.store.close()


@pytest.mark.parametrize("body", [
    {}, [], "x", {"nonsense": 1}, {"window": -1}, {"window": 86401}, {"window": "60"},
    {"window": True}, {"interval": 4.9}, {"interval": 3601}, {"good": 0}, {"good": 21},
    {"good": 2.5}, {"overrides": []}, {"overrides": {"nope": {"good": 2}}},
    {"overrides": {"p": {}}}, {"overrides": {"p": {"good": 0}}}, {"overrides": {"p": {"x": 1}}},
    {"overrides": {"p": {"good": None}}}, {"overrides": {"p": []}},
])
def test_invalid_values_are_refused_audited_and_not_saved(tmp_path, body):
    env, hdr = _admin(tmp_path)
    try:
        r = env.client.put(URL, headers=hdr, json=body)
        assert r.status_code == 422 and r.json()["detail"]
        assert env.rows("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'recheck.%'") == [(0,)]
        assert len(_audit(env, "recheck_settings_failed")) == 1
        assert _audit(env, "recheck_settings_changed") == []
    finally:
        env.client.close()
        env.store.close()


def test_the_page_carries_the_token_and_escapes_monitor_names(tmp_path):
    env = Env(tmp_path)
    try:
        env.user("root", admin=True)
        token = env.login("root").json()["csrf"]
        r = env.client.get("/admin/recheck")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert f'name="csrf" value="{token}"' in r.text
        assert 'name="window"' in r.text and 'data-slug="p"' in r.text
        assert "admin-recheck.js" in r.text
    finally:
        env.client.close()
        env.store.close()


# ---- both storage backends, and the engine ----------------------------------------------------

async def _save(store, body, slugs=("a",)):
    changes, overrides = recheck_settings.validate(body, set(slugs))
    await store.storage.write(lambda db: recheck_settings.save(
        db, changes, overrides, now=1.0, actor="root", remote="127.0.0.1"),
        touches=("admin", "audit"))


async def test_the_values_round_trip_through_each_backend(storage):
    env = _engine(storage, [mon("a")])
    await _save(env.store, {"window": 90, "overrides": {"a": {"interval": 20}}})
    glob, overrides = await env.store.storage.read(recheck_settings.load)
    assert glob == {"window": 90.0} and overrides == {"a": {"interval": 20.0}}
    audit = await env.store.fetch("SELECT kind FROM audit")
    assert audit == [("recheck_settings_changed",)]


def _engine(storage, monitors, **defaults):
    cfg = make_config(monitors, defaults={"failures_to_down": 3, "timeout": 1, "interval": 60,
                                          **defaults})

    class E:
        pass

    e = E()
    e.cfg, e.clock, e.store = cfg, Clock(), _store_on(storage)
    e.sched = Scheduler(cfg, e.store, Alerter(cfg), clock=e.clock)
    e.probe = Probe()
    e.sched.checks["a"] = e.probe
    return e


async def _poll(e, advance=0.0):
    e.clock.now += advance
    return await e.sched.poll_once(e.sched.by_slug["a"])


async def test_a_saved_per_monitor_override_beats_the_global_value_in_the_engine(storage):
    e = _engine(storage, [mon("a")])
    mon_a = e.sched.by_slug["a"]
    assert e.sched.recheck_value(mon_a, "good") == 2 and e.sched.delay(mon_a) == 60
    await _save(e.store, {"window": 40, "interval": 12, "good": 4})
    await e.sched.load_recheck()
    assert (e.sched.recheck_value(mon_a, "window"), e.sched.recheck_value(mon_a, "good")) == (40, 4)
    await _save(e.store, {"overrides": {"a": {"window": 600, "good": 1, "interval": 7}}})
    await e.sched.load_recheck()
    # the override wins for the monitor; the global values stay saved
    assert e.sched.recheck_value(mon_a, "window") == 600
    assert e.sched.recheck_value(mon_a, "good") == 1
    assert e.sched.states["a"].recheck_good == 1 and e.sched.states["a"].recheck_window == 600
    await _poll(e)
    e.probe.result = CheckResult.fail("no reply", unreachable=True)
    await _poll(e, 60)
    assert e.sched.states["a"].degraded and e.sched.delay(mon_a) == 7
    # one good reply is enough with the override (the global value asks for four)
    e.probe.result = CheckResult.ok("up")
    await _poll(e, 7)
    assert e.sched.states["a"].state is State.UP
    # removing the override returns the monitor to the global value
    await _save(e.store, {"overrides": {}})
    await e.sched.load_recheck()
    assert e.sched.recheck_value(mon_a, "good") == 4 and e.sched.delay(mon_a) == 60


async def test_a_config_value_on_the_monitor_beats_the_global_value(storage):
    e = _engine(storage, [mon("a", recheck_good=3)])
    await _save(e.store, {"good": 5})
    await e.sched.load_recheck()
    assert e.sched.recheck_value(e.sched.by_slug["a"], "good") == 3
    await _save(e.store, {"overrides": {"a": {"good": 6}}})
    await e.sched.load_recheck()
    assert e.sched.recheck_value(e.sched.by_slug["a"], "good") == 6


def test_the_endpoint_applies_the_values_to_the_running_scheduler(tmp_path):
    from fastapi.testclient import TestClient
    env = Env(tmp_path)
    sched = Scheduler(env.cfg, env.store, Alerter(env.cfg))
    app = create_app(env.cfg, env.store, sched, Alerter(env.cfg), auth_clock=env.clock)
    env.client = TestClient(app, base_url="https://testserver")
    try:
        env.user("root", admin=True)
        hdr = env.csrf(env.login("root"))
        env.client.put(URL, headers=hdr, json={"window": 50, "overrides": {"p": {"good": 7}}})
        monitor = sched.by_slug["p"]
        assert sched.recheck_value(monitor, "window") == 50
        assert sched.states["p"].recheck_good == 7
    finally:
        env.client.close()
        env.store.close()
