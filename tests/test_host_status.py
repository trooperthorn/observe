"""Pushed hosts as confirmed monitors: thresholds, confirmation, staleness, rollup, metrics."""

from __future__ import annotations

import asyncio

import pytest

from fastapi.testclient import TestClient

from observe.alerts import Alerter
from observe.checks import build_check
from observe.checks.base import CheckResult
from observe.checks.host import PushedHostCheck
from observe.ingest.schema import Batch
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

T0 = 10_000.0
COMPONENTS = [{"source": "hwmon", "metric": "cpu_temp_c", "direction": "above",
               "warn": 70, "crit": 90}]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class UpCheck:
    def thresholds(self):
        return None

    async def run(self):
        return CheckResult.ok("up")


def host_mon(name="nas01", **kw):
    return {"name": name, "type": "pushed_host", "host": name, "group": "storage",
            "components": COMPONENTS, "stale_after": 120, **kw}


def batch(host="nas01", temp: float | None = 50.0, ts=T0, sources=None):
    return Batch.model_validate({
        "schema_version": 1, "agent_version": "t", "host": host, "platform": "linux",
        "sent_at": ts, "sources": sources or [{"source": "hwmon", "available": True}],
        "samples": [{"source": "hwmon", "metric": "cpu_temp_c", "value": temp, "unit": "C",
                     "labels": {}, "ts": ts}],
    })


class Env:
    def __init__(self, monitors, f2d=3):
        self.cfg = make_config(monitors, defaults={"failures_to_down": f2d, "timeout": 1})
        self.store = Store(":memory:")
        self.sched = Scheduler(self.cfg, self.store, Alerter(self.cfg))
        self.clock = Clock()
        for chk in self.sched.checks.values():
            if isinstance(chk, PushedHostCheck):
                chk.clock = self.clock
        self.sent: list[tuple[str, str]] = []

        async def record(monitor, tr):
            self.sent.append((monitor.slug, tr.current.value))

        self.sched.alerter.notify = record

    async def push(self, **kw):
        await self.store.ingest_batch(batch(ts=self.clock.now, **kw), {}, now=self.clock.now)

    async def poll(self, slug="nas01"):
        res = await self.sched.poll_once(self.sched.by_slug[slug])
        await asyncio.sleep(0)
        await asyncio.gather(*self.sched._pending_alerts)
        return res

    def state(self, slug="nas01"):
        return self.sched.states[slug].state.value


def test_build_check_gives_store_to_pushed_host():
    env = Env([host_mon()])
    chk = build_check(env.sched.by_slug["nas01"], env.cfg, env.store)
    assert isinstance(chk, PushedHostCheck)


async def test_threshold_mapping_good_warning_critical():
    env = Env([host_mon()], f2d=1)
    for temp, result, component in ((50, "ok", "good"), (70, "warn", "warning"),
                                    (89.9, "warn", "warning"), (90, "fail", "critical")):
        await env.push(temp=temp)
        res = await env.poll()
        assert res.result.value == result, temp
        assert res.detail["components"]["hwmon.cpu_temp_c"] == component
    assert env.state() == "down"


async def test_null_reading_is_not_zero_or_alarming():
    env = Env([host_mon()], f2d=1)
    await env.push(temp=None)
    res = await env.poll()
    assert res.result.value == "ok"


async def test_required_source_unavailable_is_warning():
    env = Env([host_mon(require_sources=["mdraid"])], f2d=1)
    await env.push(sources=[{"source": "hwmon", "available": True},
                            {"source": "mdraid", "available": False, "reason": "no access"}])
    res = await env.poll()
    assert res.result.value == "warn"
    assert "no access" in res.message
    assert env.state() == "warn"


async def test_confirmation_before_paging():
    env = Env([host_mon()], f2d=3)
    await env.push(temp=95)
    await env.poll()
    await env.poll()
    assert env.state() == "pending" and env.sent == []
    await env.poll()
    assert env.state() == "down" and env.sent == [("nas01", "down")]


async def test_one_good_batch_resets_the_count():
    env = Env([host_mon()], f2d=3)
    await env.push(temp=95)
    await env.poll()
    await env.poll()
    await env.push(temp=40)
    await env.poll()
    await env.push(temp=95)
    await env.poll()
    await env.poll()
    assert env.sent == [] and env.state() != "down"


async def test_stale_host_goes_down_and_recovers():
    env = Env([host_mon()], f2d=2)
    await env.push(temp=40)
    assert (await env.poll()).result.value == "ok"
    env.clock.now += 121
    res = await env.poll()
    assert res.result.value == "fail" and "no batch" in res.message
    await env.poll()
    assert env.state() == "down" and env.sent == [("nas01", "down")]
    await env.push(temp=40)  # fresh batch at the new time
    await env.poll()
    assert env.state() == "up"
    assert env.sent == [("nas01", "down"), ("nas01", "up")]


async def test_never_seen_host_fails():
    env = Env([host_mon()], f2d=1)
    res = await env.poll()
    assert res.result.value == "fail" and "ever received" in res.message


async def test_other_hosts_batches_do_not_count():
    env = Env([host_mon()], f2d=1)
    await env.push(host="other", temp=40)
    assert (await env.poll()).result.value == "fail"


async def test_group_rollup_and_alert_for_pushed_host():
    env = Env([
        {"name": "sw", "type": "tcp", "host": "192.0.2.1", "port": 1, "group": "net"},
        host_mon("nas01", depends_on=["sw"]),
        host_mon("nas02"),
    ], f2d=1)
    env.sched.checks["sw"] = UpCheck()
    await env.poll("sw")
    await env.push(host="nas01", temp=40)
    await env.push(host="nas02", temp=40)
    await env.poll("nas01")
    await env.poll("nas02")
    assert env.sched.rollup.group_states()["storage"]["state"] == "up"
    await env.push(host="nas02", temp=95)
    await env.poll("nas02")
    groups = env.sched.rollup.group_states()
    assert groups["storage"]["state"] == "down" and groups["storage"]["worst"] == ["nas02"]
    assert env.sent[-1] == ("nas02", "down")


async def test_dependency_down_makes_pushed_host_unreachable_and_silent():
    class Down:
        def thresholds(self):
            return None

        async def run(self):
            return CheckResult.fail("refused")

    env = Env([
        {"name": "sw", "type": "tcp", "host": "192.0.2.1", "port": 1, "group": "net"},
        host_mon("nas01", depends_on=["sw"]),
    ], f2d=1)
    env.sched.checks["sw"] = Down()
    await env.poll("sw")
    await env.poll("nas01")  # no batch ever: FAIL, but its parent is down
    assert env.sent == [("sw", "down")]
    assert env.sched.rollup.effective("nas01") == ("unreachable", "sw")


async def test_non_critical_host_only_degrades_its_group():
    env = Env([host_mon("nas01", critical=False)], f2d=1)
    await env.push(temp=95)
    await env.poll()
    assert env.sched.rollup.group_states()["storage"]["state"] == "warn"


async def test_metrics_lines():
    env = Env([host_mon()], f2d=1)
    await env.push(temp=75)
    await env.poll()
    client = TestClient(create_app(env.cfg, env.store, env.sched, env.sched.alerter))
    text = client.get("/metrics").text
    assert 'watchpost_state{monitor="nas01",group="storage",type="pushed_host"} 1' in text
    assert 'watchpost_group_state{group="storage"} 1' in text
    assert ('watchpost_host_component_state{monitor="nas01",group="storage",host="nas01",'
            'component="hwmon.cpu_temp_c"} 1') in text
    assert 'watchpost_host_age_seconds{monitor="nas01",group="storage",host="nas01"} 0' in text


async def test_future_dated_sample_does_not_mask_later_reading():
    env = Env([host_mon()], f2d=1)
    far = env.clock.now + 10_000_000
    await env.store.ingest_batch(batch(temp=10.0, ts=far), {}, now=env.clock.now)
    env.clock.now += 60
    await env.push(temp=95)
    res = await env.poll()
    assert res.result.value == "fail"
    assert res.detail["components"]["hwmon.cpu_temp_c"] == "critical"


async def test_samples_older_than_stale_window_grade_stale_and_fail():
    env = Env([host_mon()], f2d=1)
    old = env.clock.now - 1200
    await env.store.ingest_batch(batch(temp=50.0, ts=old), {}, now=env.clock.now)
    res = await env.poll()
    assert res.result.value == "fail"
    assert res.detail["components"]["hwmon.cpu_temp_c"] == "stale"
    assert env.state() == "down"


async def test_older_replayed_batch_leaves_newer_host_row_and_sources():
    env = Env([host_mon()], f2d=1)
    new_src = [{"source": "hwmon", "available": False, "reason": "newer"}]
    old_src = [{"source": "hwmon", "available": True}]
    await env.store.ingest_batch(batch(ts=T0, sources=new_src), {}, now=T0)
    older = batch(ts=T0 - 500, sources=old_src)
    older.agent_version = "old"
    await env.store.ingest_batch(older, {}, now=T0 + 1)
    srcs = await env.store.host_sources("nas01")
    assert srcs["hwmon"]["available"] is False and srcs["hwmon"]["reason"] == "newer"
    data = await env.store.latest_host("nas01")
    assert data["agent_version"] == "t"


def _boot_batch(host, kind, severity, ts, boot_id="b1"):
    return Batch.model_validate({
        "schema_version": 1, "agent_version": "t", "host": host, "platform": "linux",
        "sent_at": ts, "sources": [{"source": "hwmon", "available": True}], "samples": [],
        "events": [{"kind": kind, "severity": severity, "source": "boot", "ts": ts,
                    "title": "boot", "dedup_key": f"boot:{boot_id}", "boot_id": boot_id}]})


async def _push_boot(env, kind, boot_id, severity="critical"):
    from observe.ingest.boot import classify_events
    b = _boot_batch("nas01", kind, severity, env.clock.now, boot_id)
    await env.store.ingest_batch(b, classify_events(b.events), now=env.clock.now)
    await env.push(temp=50)


async def test_crash_boot_raises_alert_and_clears_after_hold():
    env = Env([host_mon(crash_hold_s=600)], f2d=2)
    await _push_boot(env, "boot.kernel_panic", "b1")
    res = await env.poll()
    assert res.result.value == "warn" and "crash" in res.message
    await env.poll()
    assert env.state() == "warn" and env.sent[-1] == ("nas01", "warn")
    env.clock.now += 601
    await env.push(temp=50)
    assert (await env.poll()).result.value == "ok"


async def test_crash_result_fail_goes_down():
    env = Env([host_mon(crash_result="fail")], f2d=1)
    await _push_boot(env, "boot.power_loss", "b1")
    assert (await env.poll()).result.value == "fail"
    assert env.state() == "down"


async def test_clean_reboot_and_unknown_boot_do_not_alert():
    env = Env([host_mon()], f2d=1)
    await _push_boot(env, "boot.clean_shutdown", "b1", severity="info")
    assert (await env.poll()).result.value == "ok"
    await _push_boot(env, "boot.agent_stopped", "b2", severity="info")
    assert (await env.poll()).result.value == "ok"
    assert env.sent == []


async def test_later_clean_boot_clears_crash():
    env = Env([host_mon()], f2d=1)
    await _push_boot(env, "boot.unknown_unclean", "b1")
    assert (await env.poll()).result.value == "warn"
    env.clock.now += 10
    await _push_boot(env, "boot.clean_shutdown", "b2", severity="info")
    assert (await env.poll()).result.value == "ok"


@pytest.mark.parametrize("raw,expected", [
    ("Critical", "critical"), ("CRITICAL", "critical"), ("fatal", "critical"),
    ("Error", "critical"), ("emerg", "critical"), ("Warning", "warning"),
    ("INFO", "info"), (" info ", "info"), ("weird", "warning")])
def test_normalize_severity(raw, expected):
    from observe.ingest.schema import normalize_severity
    assert normalize_severity(raw) == expected


async def test_stored_severity_normalized_and_raw_kept():
    from observe.hostview import _alerts
    store = Store(":memory:")
    b = Batch.model_validate({
        "schema_version": 1, "agent_version": "t", "host": "h", "platform": "linux",
        "sent_at": T0, "sources": [], "samples": [],
        "events": [{"kind": "x.y", "severity": sev, "source": "s", "ts": T0, "title": "t",
                    "dedup_key": sev} for sev in ("Critical", "fatal", "info", "odd")]})
    await store.ingest_batch(b, {}, now=T0)
    events = await store.host_events("h")
    by_key = {e["detail"].get("severity_raw", e["severity"]): e for e in events}
    assert by_key["Critical"]["severity"] == "critical"
    assert by_key["fatal"]["severity"] == "critical"
    assert by_key["info"]["severity"] == "info" and "severity_raw" not in by_key["info"]["detail"]
    assert by_key["odd"]["severity"] == "warning"
    assert _alerts(events, T0)["status"] == "critical"
