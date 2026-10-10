"""Staleness follows the polling tiers (bug plan WP1): a reading is stale only after the window
of its own tier, from the host's effective rate (per-host override, then global, then default),
and a pushed host's silence limit follows its availability tier. A rate change applies on the
next check without a restart."""

from __future__ import annotations

import asyncio

from observe import tiers
from observe.checks.host import PushedHostCheck
from observe.otelnames import collector_scope

from . import test_host_status as hs
from . import test_host_views as hv


def save(store, changes=None, overrides=None) -> None:
    asyncio.run(store.storage.write(lambda db: tiers.save(
        db, changes or {}, overrides, now=1.0, actor="t", remote=""), touches=("admin", "audit")))


async def asave(store, changes=None, overrides=None) -> None:
    await store.storage.write(lambda db: tiers.save(
        db, changes or {}, overrides, now=1.0, actor="t", remote=""), touches=("admin", "audit"))


# ---------------------------------------------------------------- the windows themselves


def test_window_uses_default_global_and_host_override():
    base = 180.0
    d = tiers.staleness("h", {}, {}, base=base)
    assert d.for_scope(collector_scope("cpu")) == max(base, 2.5 * 60)
    assert d.for_scope(collector_scope("mdraid")) == 2.5 * 900
    assert d.for_scope(collector_scope("scrutiny")) == 2.5 * 3600
    g = tiers.staleness("h", {"device_metrics": 300.0}, {}, base=base)
    assert g.for_scope(collector_scope("hwmon")) == 750
    o = tiers.staleness("h", {"device_metrics": 300.0}, {"h": {"device_metrics": 60.0}}, base=base)
    assert o.for_scope(collector_scope("hwmon")) == base
    other = tiers.staleness("x", {"device_metrics": 300.0}, {"h": {"device_metrics": 60.0}},
                            base=base)
    assert other.for_scope(collector_scope("hwmon")) == 750


def test_silence_limit_follows_availability_unless_set():
    assert tiers.staleness("h", {}, {}, base=180).batch == 180
    assert tiers.staleness("h", {"availability": 120.0}, {}, base=180).batch == 300
    assert tiers.staleness("h", {"availability": 120.0}, {}, base=180, explicit=200).batch == 200


def test_scopes_outside_the_agent_keep_the_host_window():
    d = tiers.staleness("h", {"device_metrics": 3000.0}, {}, base=180)
    assert d.for_scope("observe.check.snmp") == 180
    assert d.for_scope("ha_soc.collector.backup") == 180


# ---------------------------------------------------------------- the host view (API)


def _push_old_reading(env, age: float) -> None:
    env.push(hv.batch(samples=[hv.s("hwmon", "hw.temperature", 41.0, "Cel", ts=hv.NOW - age,
                                    labels={"hw.id": "coretemp:Package id 0"}),
                               hv.s("mdraid", "hw.status", 1.0, "1", ts=hv.NOW - 646,
                                    labels={"hw.id": "md:md0", "hw.state": "clean"})],
                      sources=[{"source": "hwmon", "available": True},
                               {"source": "mdraid", "available": True}]))


def test_reading_286s_old_is_current_at_300s_and_stale_at_60s(tmp_path):
    env = hv.Env(tmp_path)
    try:
        _push_old_reading(env, 286)
        env.login()
        save(env.store, {"device_metrics": 300.0, "storage_health": 1500.0})
        d = hv.detail(env)
        temp = hv.reading(d, "temperatures", "hw.temperature")
        assert temp["stale"] is False and temp["status"] == "good"
        assert hv.reading(d, "raid", "hw.status")["stale"] is False
        assert d["temperatures"]["state"] == "ok" and d["raid"]["state"] == "ok"
        assert not any(i["stale"] for sec in hv.SECTIONS for i in d[sec]["items"])
        # The same reading against a 60 s tier is stale, with no restart in between.
        save(env.store, {"device_metrics": 60.0})
        d = hv.detail(env)
        temp = hv.reading(d, "temperatures", "hw.temperature")
        assert temp["stale"] is True and "last reading 286s ago" in temp["reason"]
    finally:
        env.close()


def test_host_override_wins_over_global(tmp_path):
    env = hv.Env(tmp_path)
    try:
        _push_old_reading(env, 286)
        env.login()
        save(env.store, {"device_metrics": 60.0}, {"nas01": {"device_metrics": 300.0}})
        assert hv.reading(hv.detail(env), "temperatures", "hw.temperature")["stale"] is False
        save(env.store, None, {"nas01": {"device_metrics": 60.0}})
        assert hv.reading(hv.detail(env), "temperatures", "hw.temperature")["stale"] is True
    finally:
        env.close()


def test_hosts_list_has_no_stale_warning_inside_the_tier_window(tmp_path):
    env = hv.Env(tmp_path)
    try:
        _push_old_reading(env, 256)
        env.login()
        rows = env.client.get("/api/v2/hosts").json()["items"]
        assert rows[0]["sections"]["temperatures"] == "warning"  # default 60 s tier
        save(env.store, {"device_metrics": 300.0, "storage_health": 1500.0})
        rows = env.client.get("/api/v2/hosts").json()["items"]
        assert rows[0]["sections"]["temperatures"] == "good"
        assert rows[0]["status"] == "good", rows[0]["status_reason"]
    finally:
        env.close()


# ---------------------------------------------------------------- the pushed_host check


async def test_check_silence_limit_follows_availability_tier():
    env = hs.Env([{**hs.host_mon(), "stale_after": None}], f2d=1)
    await env.push()
    env.clock.now += 204
    res = await env.poll()
    assert res.result.value == "fail" and "limit 180s" in res.message
    await asave(env.store, {"availability": 120.0})
    res = await env.poll()
    assert res.result.value == "ok", res.message


async def test_check_component_window_follows_device_tier():
    env = hs.Env([{**hs.host_mon(), "stale_after": None}], f2d=1)
    await asave(env.store, {"device_metrics": 300.0, "availability": 120.0})
    await env.store.ingest_batch(hs.batch(ts=env.clock.now - 286), {}, now=env.clock.now - 5)
    res = await env.poll()
    assert res.result.value == "ok", res.message
    await asave(env.store, {"device_metrics": 60.0, "availability": None})
    res = await env.poll()
    assert res.result.value == "fail" and "last reading 286s old" in res.message
    chk = env.sched.checks["nas01"]
    assert isinstance(chk, PushedHostCheck)
