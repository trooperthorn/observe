"""Saved threshold rules evaluated in production, a monitor loop that survives storage errors and
the Degraded cooldown (docs/DATA-API-DESIGN.md sections 10.3 and 10.4).

The clock is a fake that the test moves and the probes are scripted fakes, so nothing waits or
touches a network."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from observe import rules
from observe.alerts import Alerter
from observe.checks.base import CheckResult
from observe.ingest.keys import create_key
from observe.ingest.schema import Batch
from observe.scheduler import Scheduler
from observe.state import State
from observe.storage import StorageBusy
from observe.store import Store
from observe.web import create_app

from .conftest import make_config
from .otlp_build import post_batch
from .test_recheck import Env, T0, mon, storage  # noqa: F401


async def save(env: Env, *items: dict) -> None:
    parsed = rules.validate(list(items))
    await env.store.storage.write(lambda db: rules.save(db, parsed, now=T0, actor="t", remote=""))
    env.sched.apply_rules(parsed)


CRIT = {"id": "hot", "kind": "consecutive", "metric": "monitor.value", "condition": "above",
        "crit": 90, "x": 1}


# ---- rules change status and alert through the confirmation path ---------------------------

async def test_a_saved_rule_changes_status_and_alerts_after_confirmation(storage):  # noqa: F811
    env = Env(storage, [mon("a")], failures_to_down=2)
    await save(env, CRIT)
    env.probes["a"].result = CheckResult.ok("fine", value=95.0, unit="C")
    await env.poll("a")
    # One breach is not an outage: the ordinary confirmation count still applies.
    assert env.st("a").state is State.PENDING and env.sent == []
    res = await env.poll("a", 60)
    assert "threshold rule hot is critical" in res.message
    assert env.st("a").state is State.DOWN
    assert env.sent == [("a", "down", False)]
    assert env.sched.rollup.group_states()["default"]["state"] == "down"


async def test_a_warning_rule_makes_the_host_warn_and_clears_by_hysteresis(storage):  # noqa: F811
    env = Env(storage, [mon("a")], failures_to_down=1)
    await save(env, {"id": "warm", "kind": "consecutive", "metric": "monitor.value",
                     "condition": "above", "warn": 80, "x": 1, "clear": 2})
    probe = env.probes["a"]
    probe.result = CheckResult.ok("fine", value=85.0)
    await env.poll("a")
    assert env.st("a").state is State.WARN
    probe.result = CheckResult.ok("fine", value=10.0)
    await env.poll("a", 60)
    assert env.st("a").state is State.WARN  # one clean poll is not enough
    await env.poll("a", 60)
    await env.poll("a", 60)
    assert env.st("a").state is State.UP


async def test_removing_the_rules_stops_their_effect(storage):  # noqa: F811
    env = Env(storage, [mon("a")], failures_to_down=1)
    await save(env, CRIT)
    env.probes["a"].result = CheckResult.ok("fine", value=95.0)
    await env.poll("a")
    assert env.st("a").state is State.DOWN
    await save(env)
    await env.poll("a", 60)
    await env.poll("a", 60)
    assert env.st("a").state is State.UP


async def test_pushed_samples_are_evaluated_and_feed_the_host_poll(storage):  # noqa: F811
    env = Env(storage, [{"name": "nas", "type": "pushed_host", "host": "nas",
                         "stale_after": 120}], failures_to_down=1)
    await save(env, {"id": "cpu", "kind": "consecutive", "metric": "hw.temperature",
                     "condition": "above", "crit": 90, "x": 1, "host": "nas"})
    hot = SimpleNamespace(source="hostwatch.collector.hwmon", metric="hw.temperature", value=99.0, labels={}, ts=T0)
    await env.sched.observe_pushed("nas", [hot], T0)
    assert env.sched.rules.worst("nas")[0] == rules.CRITICAL
    await env.poll("nas")
    assert env.st("nas").state is State.DOWN
    assert env.sent == [("nas", "down", False)]


async def test_a_restart_seeds_the_ring_from_stored_samples(storage):  # noqa: F811
    env = Env(storage, [{"name": "nas", "type": "pushed_host", "host": "nas",
                         "stale_after": 120}])
    for i in range(2):
        await env.store.ingest_batch(_hot(T0 + i), {}, now=T0 + i)
    await save(env, {"id": "cpu", "kind": "consecutive", "metric": "hw.temperature",
                     "condition": "above", "crit": 30, "x": 3, "host": "nas"})
    # A fresh process: empty rings. Two stored hot samples plus the new one make three in a row.
    third = _hot(T0 + 2)
    await env.store.ingest_batch(third, {}, now=T0 + 2)
    await env.sched.observe_pushed("nas", third.samples, T0 + 2)
    assert env.sched.rules.worst("nas")[0] == rules.CRITICAL


def _hot(ts: float) -> Batch:
    return Batch.model_validate({
        "schema_version": 1, "agent_version": "t", "host": "nas", "platform": "linux",
        "sent_at": ts, "sources": [{"source": "hwmon", "available": True}],
        "samples": [{"source": "hostwatch.collector.hwmon", "metric": "hw.temperature", "value": 40.0, "unit": "C",
                     "labels": {}, "ts": ts}]})


def test_otlp_ingest_reaches_the_rule_engine(tmp_path):
    path = str(tmp_path / "w.db")
    store = Store(path)
    cfg = make_config([{"name": "nas", "type": "pushed_host", "host": "nas"}],
                      server={"db_path": path})
    sched = Scheduler(cfg, store, Alerter(cfg))
    client = TestClient(create_app(cfg, store, sched, Alerter(cfg)))
    key = asyncio.run(create_key(store, "nas"))[0]
    sched.apply_rules(rules.validate([{
        "id": "cpu", "kind": "consecutive", "metric": "hw.temperature", "condition": "above",
        "crit": 30, "x": 1}]))
    body = {"schema_version": 1, "agent_version": "t", "host": "nas", "platform": "linux",
            "sent_at": T0, "sources": [{"source": "hwmon", "available": True}],
            "samples": [{"source": "hostwatch.collector.hwmon", "metric": "hw.temperature", "value": 40.0, "unit": "C",
                         "labels": {}, "ts": T0}]}
    assert post_batch(client, body, key).status_code == 200
    assert sched.rules.worst("nas")[0] == rules.CRITICAL


def test_window_rule_does_not_judge_when_the_full_ring_is_shorter_than_the_window():
    now = [1000.0]
    engine = rules.RuleEngine(
        [rules.parse_rule({"id": "w", "kind": "window", "metric": "m", "condition": "above",
                           "crit": 10, "window": 3600, "agg": "avg"})],
        clock=lambda: now[0], capacity=3)
    for i in range(3):
        v = engine.observe("k", "h", "m", 50.0, ts=900.0 + i)
    assert v[0].level == rules.OK  # three samples span seconds, not the hour asked for
    big = rules.RuleEngine(
        [rules.parse_rule({"id": "w", "kind": "window", "metric": "m", "condition": "above",
                           "crit": 10, "window": 3600, "agg": "avg"})],
        clock=lambda: now[0], capacity=100)
    assert big.observe("k", "h", "m", 50.0, ts=900.0)[0].level == rules.CRITICAL


# ---- a storage error never ends a monitor loop ----------------------------------------------

async def test_a_storage_error_in_one_cycle_is_followed_by_a_normal_cycle(storage, caplog):  # noqa: F811
    env = Env(storage, [mon("a")])
    real = env.store.record
    calls = {"n": 0}
    done = asyncio.Event()

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] in (2, 3):
            raise StorageBusy("busy")
        return await real(*a, **kw)

    async def wait(_seconds):
        if calls["n"] >= 4:
            done.set()
            await asyncio.sleep(3600)  # cancelled below
        await asyncio.sleep(0)

    env.store.record = flaky
    env.sched._wait = wait
    with caplog.at_level(logging.INFO, logger="observe.scheduler"):
        task = asyncio.create_task(env.sched._loop(env.sched.by_slug["a"]))
        await asyncio.wait_for(done.wait(), 10)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    failed = [r for r in caplog.records if "failed; trying again" in r.getMessage()]
    assert len(failed) == 1  # two failing cycles in a row are one streak, logged once
    assert any("recovered" in r.getMessage() for r in caplog.records)
    assert calls["n"] == 4 and env.st("a").state is State.UP


# ---- the Degraded cooldown ------------------------------------------------------------------

async def test_the_degraded_cooldown_suppresses_rapid_repeat_episodes(storage):  # noqa: F811
    env = Env(storage, [mon("a")], failures_to_down=3, degraded_cooldown=120)
    probe = env.probes["a"]
    await env.poll("a")
    probe.silent()
    await env.poll("a", 60)
    assert env.st("a").degraded  # the first episode starts at once
    probe.answers()
    await env.poll("a", 10)
    await env.poll("a", 10)
    assert env.st("a").state is State.UP and not env.st("a").degraded
    episodes = len([s for s in env.sent if s[2]])
    probe.silent()
    await env.poll("a", 30)  # 30 s after the recovery, inside the cooldown
    assert not env.st("a").degraded and env.st("a").state is State.UP
    assert len([s for s in env.sent if s[2]]) == episodes
    probe.answers()
    await env.poll("a", 30)
    await env.poll("a", 200)  # well past the cooldown
    probe.silent()
    await env.poll("a", 60)
    assert env.st("a").degraded and env.st("a").state is State.WARN


async def test_a_zero_cooldown_keeps_the_old_behaviour(storage):  # noqa: F811
    env = Env(storage, [mon("a")], failures_to_down=3, degraded_cooldown=0)
    probe = env.probes["a"]
    await env.poll("a")
    for _ in range(2):
        probe.silent()
        await env.poll("a", 60)
        assert env.st("a").degraded
        probe.answers()
        await env.poll("a", 10)
        await env.poll("a", 10)
        assert env.st("a").state is State.UP


def test_the_cooldown_defaults_to_two_minutes():
    assert make_config([mon("a")]).defaults.degraded_cooldown == 120.0
    with pytest.raises(Exception):
        make_config([mon("a")], defaults={"degraded_cooldown": -1})
