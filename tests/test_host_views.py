"""GET /api/v2/hosts and /api/v2/hosts/{host}: shape per component, status labels, missing
and stale sources, and the login requirement. The readings carry the OpenTelemetry scope and
metric names of docs/DATA-API-DESIGN.md section 3."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.api.models import rfc3339
from observe.alerts import Alerter
from observe.ingest.boot import classify_events
from observe.ingest.schema import Batch
from observe.otelnames import collector_scope
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

PASSWORD = "correct horse battery"
NOW = 1_000_000.0
SECTIONS = ("cpu", "memory", "power", "temperatures", "fans", "raid", "zfs", "disks", "ups")
ITEM_KEYS = {"source", "metric", "labels", "value", "unit", "ts", "age_seconds", "stale",
             "status", "reason", "ignored"}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def s(source: str, metric: str, value: float | None, unit: str = "", ts: float = NOW - 5,
      labels: dict[str, Any] | None = None) -> dict[str, Any]:
    """A point as the agent sends it: the scope is hostwatch.collector.<source>."""
    return {"source": collector_scope(source), "metric": metric, "value": value, "unit": unit,
            "labels": labels or {}, "ts": ts}


def batch(host: str = "nas01", samples: list | None = None, sources: list | None = None,
          events: list | None = None, sent: float = NOW - 5) -> Batch:
    return Batch.model_validate({
        "schema_version": 1, "agent_version": "0.9.0", "host": host, "platform": "linux",
        "sent_at": sent, "sources": sources or [], "samples": samples or [],
        "events": events or []})


FULL = [
    s("cpu", "system.cpu.utilization", 0.12, "1"),
    s("cpu", "system.cpu.load_average.1m", 0.5, "{thread}"),
    s("memory", "system.memory.limit", 16e9, "By"),
    s("memory", "system.memory.usage", 8e9, "By", labels={"system.memory.state": "used"}),
    s("memory", "system.memory.usage", 8e9, "By", labels={"system.memory.state": "free"}),
    s("rapl", "hw.power", 14.5, "W", labels={"hw.id": "rapl:intel-rapl:0", "hw.type": "cpu"}),
    s("hwmon", "hw.temperature", 41.0, "Cel", labels={"hw.id": "coretemp:Package id 0"}),
    s("hwmon", "hw.temperature", 93.0, "Cel", labels={"hw.id": "nvme:Composite"}),
    s("hwmon", "hw.fan.speed", 1200.0, "{rpm}", labels={"hw.id": "nct:fan1"}),
    s("thermalctl", "observe.thermal.fan.duty", 0.4, "1", labels={"hw.id": "fan:a"}),
    s("win_thermalsuite", "observe.thermal.failsafe", 1.0, "{reason}"),
    s("mdraid", "observe.legacy.mdraid.degraded", 1.0, "{count}", labels={"array": "md0"}),
    s("mdraid", "hw.status", 1.0, "1", labels={"hw.id": "md:md0", "hw.state": "clean"}),
    s("zfs", "hw.status", 1.0, "1", labels={"hw.id": "zpool:tank", "hw.state": "ONLINE"}),
    s("zfs", "hw.status", 1.0, "1", labels={"hw.id": "zpool:old", "hw.state": "DEGRADED"}),
    s("scrutiny", "hw.status", 0.0, "1", labels={"hw.id": "disk:w1"}),
    s("scrutiny", "observe.legacy.scrutiny.temp", 35.0, "Cel", labels={"wwn": "w1"}),
    s("nut", "hw.battery.charge", 1.0, "1", labels={"hw.id": "ups:ups"}),
    s("nut", "observe.ups.status", 1.0, "1",
      labels={"hw.id": "ups:ups", "observe.ups.flag": "OB"}),
]
FULL_SOURCES = [{"source": n, "available": True} for n in
                ("cpu", "memory", "rapl", "hwmon", "thermalctl", "mdraid", "zfs", "scrutiny", "nut")]
EVENTS = [
    {"kind": "boot.unclean_shutdown", "severity": "critical", "source": "journal",
     "ts": NOW - 100, "title": "Previous boot ended without a clean shutdown",
     "dedup_key": "boot:b", "boot_id": "b"},
    {"kind": "md.degraded", "severity": "warning", "source": "journal", "ts": NOW - 200000,
     "title": "old warning", "dedup_key": "md:1"},
]


class Env:
    def __init__(self, tmp_path, monitors: list | None = None) -> None:
        self.path = str(tmp_path / "w.db")
        self.store = Store(self.path)
        srv = {"db_path": self.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
               "argon2_parallelism": 1, "session_idle_s": 100_000,
               "session_absolute_s": 200_000}
        self.cfg = make_config(monitors or [{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                               server=srv)
        self.clock = Clock()
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        self.client = TestClient(create_app(self.cfg, self.store, sched, alerter,
                                            auth_clock=self.clock), base_url="https://testserver")

    def push(self, b: Batch) -> None:
        asyncio.run(self.store.ingest_batch(b, classify_events(b.events), now=self.clock.now - 5))

    def login(self) -> None:
        asyncio.run(auth.create_user(self.store, self.cfg, "alice", PASSWORD, False,
                                     now=self.clock()))
        r = self.client.post("/api/login", json={"username": "alice", "password": PASSWORD})
        assert r.status_code == 200

    def close(self) -> None:
        self.client.close()
        self.store.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def detail(env: Env, host: str = "nas01") -> dict[str, Any]:
    r = env.client.get(f"/api/v2/hosts/{host}")
    assert r.status_code == 200, r.text
    return r.json()


def reading(d: dict[str, Any], section: str, metric: str,
            labels: dict[str, str] | None = None, **more: str) -> dict[str, Any]:
    want = {**(labels or {}), **more}
    return next(i for i in d[section]["items"] if i["metric"] == metric
                and all(i["labels"].get(k) == v for k, v in want.items()))


def test_login_is_required_for_every_host_route(env):
    env.push(batch(samples=FULL, sources=FULL_SOURCES))
    for path in ("/api/v2/hosts", "/api/v2/hosts/nas01"):
        r = env.client.get(path)
        assert r.status_code == 401
        assert "basic" not in r.headers.get("www-authenticate", "").lower()
    env.login()
    assert env.client.get("/api/v2/hosts").status_code == 200
    assert env.client.get("/api/v2/hosts/nas01").status_code == 200


def test_basic_auth_does_not_open_host_routes(tmp_path):
    e = Env(tmp_path)
    try:
        e.cfg.server.basic_auth_user = "ui"
        e.cfg.server.basic_auth_password = "uipass"
        assert e.client.get("/api/v2/hosts", auth=("ui", "uipass")).status_code == 401
        assert e.client.get("/api/v2/hosts/nas01", auth=("ui", "uipass")).status_code == 401
    finally:
        e.close()


def test_json_shape_per_component(env):
    env.push(batch(samples=FULL, sources=FULL_SOURCES, events=EVENTS))
    env.login()
    d = detail(env)
    assert {"host", "platform", "agent_version", "heard", "last_seen", "age_seconds", "stale",
            "stale_after", "confirmed", "monitored", "monitor", "boot", "status",
            "status_reason", "alerts", "events", "sources", *SECTIONS} <= d.keys()
    for name in SECTIONS:
        sec = d[name]
        assert set(sec) == {"status", "state", "note", "items"}
        assert sec["status"] in {"good", "warning", "critical"}
        for item in sec["items"]:
            assert set(item) == ITEM_KEYS
            assert item["status"] in {"good", "warning", "critical"}
    assert {i["metric"] for i in d["cpu"]["items"]} == {
        "system.cpu.utilization", "system.cpu.load_average.1m"}
    assert {i["metric"] for i in d["memory"]["items"]} == {
        "system.memory.limit", "system.memory.usage", "system.memory.utilization"}
    used = reading(d, "memory", "system.memory.utilization")
    assert used["value"] == 0.5 and used["status"] == "good"
    assert [i["metric"] for i in d["power"]["items"]] == ["hw.power"]
    assert len(d["temperatures"]["items"]) == 2
    assert {i["metric"] for i in d["fans"]["items"]} == {
        "hw.fan.speed", "observe.thermal.fan.duty", "observe.thermal.failsafe"}
    assert {i["metric"] for i in d["raid"]["items"]} == {
        "observe.legacy.mdraid.degraded", "hw.status"}
    assert len(d["zfs"]["items"]) == 2
    assert {i["metric"] for i in d["disks"]["items"]} == {
        "hw.status", "observe.legacy.scrutiny.temp"}
    assert {i["metric"] for i in d["ups"]["items"]} == {
        "hw.battery.charge", "observe.ups.status"}
    assert d["boot"] == {"boot_id": "b", "boot_ts": rfc3339(NOW - 100), "clean_shutdown": False}
    assert [e["kind"] for e in d["events"]] == ["boot.unclean_shutdown", "md.degraded"]
    assert d["events"][0]["detail"]["classification"] == "crash"
    assert [e["kind"] for e in d["alerts"]["items"]] == ["boot.unclean_shutdown"]
    assert d["alerts"]["status"] == "critical"
    assert {x["source"] for x in d["sources"]} == {x["source"] for x in FULL_SOURCES}


def test_status_labels(env):
    env.push(batch(samples=FULL, sources=FULL_SOURCES))
    env.login()
    d = detail(env)
    assert d["cpu"]["status"] == "good"
    temp = "hw.temperature"
    assert reading(d, "temperatures", temp, {"hw.id": "coretemp:Package id 0"})["status"] == "good"
    assert reading(d, "temperatures", temp, {"hw.id": "nvme:Composite"})["status"] == "critical"
    assert d["temperatures"]["status"] == "critical"
    assert reading(d, "fans", "observe.thermal.failsafe")["status"] == "warning"
    assert reading(d, "raid", "observe.legacy.mdraid.degraded")["status"] == "critical"
    assert reading(d, "zfs", "hw.status", {"hw.id": "zpool:tank"})["status"] == "good"
    assert reading(d, "zfs", "hw.status", {"hw.id": "zpool:old"})["status"] == "critical"
    assert reading(d, "ups", "observe.ups.status")["status"] == "warning"
    assert d["power"]["status"] == "good"
    assert d["status"] == "critical" and not d["stale"]


def test_null_value_is_no_data_not_zero(env):
    env.push(batch(samples=[s("cpu", "system.cpu.utilization", None, "1")],
                   sources=[{"source": "cpu", "available": True}]))
    env.login()
    d = detail(env)
    item = d["cpu"]["items"][0]
    assert item["value"] is None and item["status"] == "no_data"
    assert "no value" in item["reason"]
    assert d["status"] == "good"  # a missing value claims nothing, the same as the check


def test_missing_source_is_reported_honestly(env):
    env.push(batch(
        samples=[s("cpu", "system.cpu.utilization", 0.05, "1")],
        sources=[{"source": "cpu", "available": True},
                 {"source": "nut", "available": False, "present": False},
                 {"source": "mdraid", "available": False, "reason": "permission denied"}]))
    env.login()
    d = detail(env)
    assert d["ups"]["state"] == "absent" and d["ups"]["status"] == "no_data"
    assert d["raid"]["state"] == "unavailable" and d["raid"]["status"] == "warning"
    assert "permission denied" in d["raid"]["note"]
    assert d["zfs"]["state"] == "not_reported" and d["zfs"]["items"] == []
    srcs = {x["source"]: x for x in d["sources"]}
    assert srcs["nut"]["present"] is False and srcs["nut"]["status"] == "no_data"
    assert srcs["mdraid"]["present"] is True and srcs["mdraid"]["available"] is False
    assert srcs["mdraid"]["status"] == "warning"


def test_stale_reading_and_stale_host_are_marked(env):
    env.push(batch(samples=[s("cpu", "system.cpu.utilization", 0.05, "1", ts=NOW - 600),
                            s("memory", "system.memory.limit", 8e9, "By")],
                   sources=[{"source": "cpu", "available": True}]))
    env.login()
    d = detail(env)
    assert d["stale_after"] == 3 * env.cfg.defaults.interval and not d["stale"]
    assert d["cpu"]["state"] == "stale" and d["cpu"]["status"] == "warning"
    assert d["cpu"]["items"][0]["stale"] is True
    assert d["memory"]["items"][0]["stale"] is False
    env.clock.now = NOW + 10_000  # nothing has arrived since
    d = detail(env)
    assert d["stale"] is True and d["status"] == "critical"
    assert d["cpu"]["state"] == "stale" and d["memory"]["state"] == "stale"
    assert all(i["stale"] for sec in SECTIONS for i in d[sec]["items"])
    assert "no batch for" in d["status_reason"]


def test_listing_unknown_host_and_monitor_state(tmp_path):
    e = Env(tmp_path, [{"name": "NAS", "type": "pushed_host", "host": "nas01",
                        "components": [{"source": "hostwatch.collector.hwmon",
                                        "metric": "hw.temperature", "warn": 30, "crit": 40}]},
                       {"name": "Silent", "type": "pushed_host", "host": "ghost"}])
    try:
        e.push(batch(samples=[s("hwmon", "hw.temperature", 35.0, "Cel", labels={"hw.id": "x"})],
                     sources=[{"source": "hwmon", "available": True}]))
        e.push(batch("other", samples=[s("cpu", "system.cpu.load_average.1m", 1.0)]))
        e.login()
        assert e.client.get("/api/v2/hosts/nope").status_code == 404
        rows = {r["host"]: r for r in e.client.get("/api/v2/hosts").json()["items"]}
        assert set(rows) == {"nas01", "other", "ghost"}
        assert rows["nas01"]["monitored"] and rows["nas01"]["monitor"]["name"] == "NAS"
        assert rows["other"]["monitored"] is False and rows["other"]["monitor"] is None
        assert rows["ghost"]["heard"] is False and rows["ghost"]["status"] == "critical"
        assert rows["nas01"]["sections"]["temperatures"] == "warning"  # YAML limit beats default
        assert rows["nas01"]["states"]["ups"] == "not_reported"
        ghost = e.client.get("/api/v2/hosts/ghost").json()
        assert ghost["heard"] is False and ghost["last_seen"] is None
        assert "no batch received yet" in ghost["status_reason"]
    finally:
        e.close()


def test_host_page_is_static_and_dashboard_links_to_it(env):
    r = env.client.get("/host")
    assert r.status_code == 200 and "host.js" in r.text
    assert "/api/v2/hosts/" in env.client.get("/static/host.js").text
    assert "hostlink" in env.client.get("/static/app.js").text


def test_host_name_with_slash_opens_in_api_and_page_link(env):
    env.push(batch(host="rack/nas 01?#%", samples=FULL, sources=FULL_SOURCES))
    env.login()
    assert [h["host"] for h in env.client.get("/api/v2/hosts").json()["items"]] == ["rack/nas 01?#%"]
    from urllib.parse import quote
    r = env.client.get(f"/api/v2/hosts/{quote('rack/nas 01?#%', safe='')}")
    assert r.status_code == 200 and r.json()["host"] == "rack/nas 01?#%"
    assert env.client.get("/api/v2/hosts/rack/nas 01?#%".replace("?#%", "%3F%23%25")).status_code == 200
    assert env.client.get("/host?name=rack%2Fnas%2001").status_code == 200


def test_configured_component_silent_for_long_stays_stale_on_the_page(tmp_path):
    e = Env(tmp_path, [{"name": "NAS", "type": "pushed_host", "host": "nas01",
                        "components": [{"source": "hostwatch.collector.cpu",
                                        "metric": "system.cpu.utilization",
                                        "warn": 0.8, "crit": 0.95}]}])
    try:
        e.push(batch(samples=[s("cpu", "system.cpu.utilization", 0.05, "1", ts=NOW - 5000),
                              s("memory", "system.memory.limit", 8e9, "By")]))
        e.login()
        d = detail(e)
        assert d["cpu"]["items"][0]["stale"] is True
        assert d["cpu"]["state"] == "stale"
    finally:
        e.close()
