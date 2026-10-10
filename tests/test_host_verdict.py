"""One host, one verdict (bug plan WP2): the pushed_host monitor behind the dashboard card, the
Hosts list, the host page and its header chip all read the same state and reason, so no page can
call a host "all components good" while another calls it critical."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from observe.alerts import Alerter
from observe.checks.base import Result
from observe.hostview import HA_POLL
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from . import test_host_views as hv
from .conftest import make_config


class Env(hv.Env):
    """The host views environment, keeping the scheduler so the monitor can be polled."""

    def __init__(self, tmp_path, monitors) -> None:
        self.path = str(tmp_path / "w.db")
        self.store = Store(self.path)
        srv = {"db_path": self.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
               "argon2_parallelism": 1, "session_idle_s": 100_000,
               "session_absolute_s": 200_000}
        creds = {"ha": {"type": "homeassistant", "token": "t"}}
        self.cfg = make_config(monitors, server=srv, credentials=creds)
        self.clock = hv.Clock()
        alerter = Alerter(self.cfg)
        self.sched = Scheduler(self.cfg, self.store, alerter, clock=self.clock)
        for chk in self.sched.checks.values():
            if hasattr(chk, "clock"):
                chk.clock = self.clock
        self.client = TestClient(create_app(self.cfg, self.store, self.sched, alerter,
                                            auth_clock=self.clock), base_url="https://testserver")

    def poll(self, slug: str):
        return asyncio.run(self.sched.poll_once(self.sched.by_slug[slug]))


def test_one_critical_reading_reads_the_same_on_every_surface(tmp_path):
    # A fan at 0 RPM is critical under the built-in rules, though the monitor lists no fan
    # component: before, the check said "all components good" while the Hosts list said critical.
    env = Env(tmp_path, [{"name": "MediaIn-SVR", "type": "pushed_host", "host": "nas01",
                          "components": [{"source": "hostwatch.collector.hwmon",
                                          "metric": "hw.temperature", "warn": 80, "crit": 90}]}])
    try:
        env.push(hv.batch(samples=[
            hv.s("hwmon", "hw.temperature", 41.0, "Cel", labels={"hw.id": "coretemp:0"}),
            hv.s("hwmon", "hw.fan.speed", 0.0, "{rpm}", labels={"hw.id": "nct:fan2"})],
            sources=[{"source": "hwmon", "available": True}]))
        env.login()
        res = env.poll("mediain-svr")
        row = env.client.get("/api/v2/hosts").json()["items"][0]
        page = hv.detail(env)
        mon = env.client.get("/api/v2/monitors/mediain-svr").json()
        assert res.result is Result.FAIL
        assert row["status"] == page["status"] == "critical"
        assert row["status_reason"] == page["status_reason"] == res.message == mon["message"]
        assert "fan reads 0 RPM" in res.message and "all components good" not in res.message
        assert mon["state"] == "down"
        assert page["monitor"]["effective_state"] == "down"
    finally:
        env.close()


def test_a_good_host_is_good_everywhere(tmp_path):
    env = Env(tmp_path, [{"name": "NAS", "type": "pushed_host", "host": "nas01"}])
    try:
        env.push(hv.batch(samples=[hv.s("hwmon", "hw.temperature", 41.0, "Cel",
                                        labels={"hw.id": "coretemp:0"})],
                          sources=[{"source": "hwmon", "available": True}]))
        env.login()
        res = env.poll("nas")
        row = env.client.get("/api/v2/hosts").json()["items"][0]
        assert res.result is Result.OK and row["status"] == "good"
        assert hv.detail(env)["monitor"]["effective_state"] == "up"
    finally:
        env.close()


def test_ha_unavailable_entities_use_the_monitor_threshold(tmp_path):
    """The same crossing of the critical limit is Down on the dashboard and critical on the host
    page, not Down on one and Warning on the other."""
    env = Env(tmp_path, [
        {"name": "HA host", "type": "homeassistant", "host": "ha.lan", "credential": "ha",
         "mode": "host", "host_name": "homeassistant"},
        {"name": "HA unavailable", "type": "homeassistant", "host": "ha.lan", "credential": "ha",
         "mode": "unavailable", "thresholds": {"warn": 10, "crit": 25}}])
    try:
        env.push(hv.batch("homeassistant", samples=[
            {"source": HA_POLL, "metric": "observe.ha.entity.unavailable", "value": 600.0,
             "unit": "{entity}", "labels": {}, "ts": hv.NOW - 5}]))
        env.login()
        d = hv.detail(env, "homeassistant")
        assert d["ha"]["status"] == "critical" and d["status"] == "critical"
    finally:
        env.close()
