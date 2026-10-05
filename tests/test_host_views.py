"""GET /api/hosts and /api/hosts/{host}: shape per component, status labels, missing
and stale sources, and the login requirement."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.ingest.boot import classify_events
from observe.ingest.schema import Batch
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

PASSWORD = "correct horse battery"
NOW = 1_000_000.0
SECTIONS = ("cpu", "memory", "power", "temperatures", "fans", "raid", "zfs", "disks", "ups")
ITEM_KEYS = {"source", "metric", "labels", "value", "unit", "ts", "age_seconds", "stale",
             "status", "reason"}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def s(source: str, metric: str, value: float | None, unit: str = "", ts: float = NOW - 5,
      **labels: str) -> dict[str, Any]:
    return {"source": source, "metric": metric, "value": value, "unit": unit,
            "labels": labels, "ts": ts}


def batch(host: str = "nas01", samples: list | None = None, sources: list | None = None,
          events: list | None = None, sent: float = NOW - 5) -> Batch:
    return Batch.model_validate({
        "schema_version": 1, "agent_version": "0.9.0", "host": host, "platform": "linux",
        "sent_at": sent, "sources": sources or [], "samples": samples or [],
        "events": events or []})


FULL = [
    s("cpu", "utilization_pct", 12.0, "%"), s("cpu", "load", 0.5, "", span="1m"),
    s("memory", "mem_total", 16e9, "B"), s("memory", "mem_available", 8e9, "B"),
    s("rapl", "watts", 14.5, "W", zone="package-0", domain="package"),
    s("hwmon", "temp", 41.0, "C", chip="coretemp", sensor="Package id 0"),
    s("hwmon", "temp", 93.0, "C", chip="nvme", sensor="Composite"),
    s("hwmon", "fan", 1200.0, "RPM", chip="nct", sensor="fan1"),
    s("thermalctl", "fan_duty", 40.0, "%", fan="a"), s("thermalctl", "failsafe", 1.0, ""),
    s("mdraid", "degraded", 1.0, "count", array="md0", level="raid1"),
    s("mdraid", "array_state", 1.0, "", array="md0", state="clean"),
    s("zfs", "pool_state", 1.0, "", pool="tank", state="ONLINE"),
    s("zfs", "pool_state", 1.0, "", pool="old", state="DEGRADED"),
    s("scrutiny", "device_status", 0.0, "", wwn="w1", device="sda"),
    s("scrutiny", "temp", 35.0, "C", wwn="w1", device="sda"),
    s("nut", "battery_charge_pct", 100.0, "%"),
    s("nut", "ups_status_flag", 1.0, "", flag="OB", status="OB"),
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
    r = env.client.get(f"/api/hosts/{host}")
    assert r.status_code == 200, r.text
    return r.json()


def reading(d: dict[str, Any], section: str, metric: str, **labels: str) -> dict[str, Any]:
    return next(i for i in d[section]["items"] if i["metric"] == metric
                and all(i["labels"].get(k) == v for k, v in labels.items()))


def test_login_is_required_for_every_host_route(env):
    env.push(batch(samples=FULL, sources=FULL_SOURCES))
    for path in ("/api/hosts", "/api/hosts/nas01"):
        r = env.client.get(path)
        assert r.status_code == 401
        assert "www-authenticate" not in r.headers
    env.login()
    assert env.client.get("/api/hosts").status_code == 200
    assert env.client.get("/api/hosts/nas01").status_code == 200


def test_basic_auth_does_not_open_host_routes(tmp_path):
    e = Env(tmp_path)
    try:
        e.cfg.server.basic_auth_user = "ui"
        e.cfg.server.basic_auth_password = "uipass"
        assert e.client.get("/api/hosts", auth=("ui", "uipass")).status_code == 401
        assert e.client.get("/api/hosts/nas01", auth=("ui", "uipass")).status_code == 401
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
    assert {i["metric"] for i in d["cpu"]["items"]} == {"utilization_pct", "load"}
    assert {i["metric"] for i in d["memory"]["items"]} == {
        "mem_total", "mem_available", "used_pct"}
    used = reading(d, "memory", "used_pct")
    assert used["value"] == 50.0 and used["status"] == "good"
    assert [i["metric"] for i in d["power"]["items"]] == ["watts"]
    assert len(d["temperatures"]["items"]) == 2
    assert {i["metric"] for i in d["fans"]["items"]} == {"fan", "fan_duty", "failsafe"}
    assert {i["metric"] for i in d["raid"]["items"]} == {"degraded", "array_state"}
    assert len(d["zfs"]["items"]) == 2
    assert {i["metric"] for i in d["disks"]["items"]} == {"device_status", "temp"}
    assert {i["metric"] for i in d["ups"]["items"]} == {"battery_charge_pct", "ups_status_flag"}
    assert d["boot"] == {"boot_id": "b", "boot_ts": NOW - 100, "clean_shutdown": False}
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
    assert reading(d, "temperatures", "temp", sensor="Package id 0")["status"] == "good"
    assert reading(d, "temperatures", "temp", sensor="Composite")["status"] == "critical"
    assert d["temperatures"]["status"] == "critical"
    assert reading(d, "fans", "failsafe")["status"] == "warning"
    assert reading(d, "raid", "degraded")["status"] == "critical"
    assert reading(d, "zfs", "pool_state", pool="tank")["status"] == "good"
    assert reading(d, "zfs", "pool_state", pool="old")["status"] == "critical"
    assert reading(d, "ups", "ups_status_flag")["status"] == "warning"
    assert d["power"]["status"] == "good"
    assert d["status"] == "critical" and not d["stale"]


def test_null_value_is_warning_not_zero(env):
    env.push(batch(samples=[s("cpu", "utilization_pct", None, "%")],
                   sources=[{"source": "cpu", "available": True}]))
    env.login()
    item = detail(env)["cpu"]["items"][0]
    assert item["value"] is None and item["status"] == "warning"
    assert "no value" in item["reason"]


def test_missing_source_is_reported_honestly(env):
    env.push(batch(
        samples=[s("cpu", "utilization_pct", 5.0, "%")],
        sources=[{"source": "cpu", "available": True},
                 {"source": "nut", "available": False, "present": False},
                 {"source": "mdraid", "available": False, "reason": "permission denied"}]))
    env.login()
    d = detail(env)
    assert d["ups"]["state"] == "absent" and d["ups"]["status"] == "good"
    assert d["raid"]["state"] == "unavailable" and d["raid"]["status"] == "warning"
    assert "permission denied" in d["raid"]["note"]
    assert d["zfs"]["state"] == "not_reported" and d["zfs"]["items"] == []
    srcs = {x["source"]: x for x in d["sources"]}
    assert srcs["nut"]["present"] is False and srcs["nut"]["status"] == "good"
    assert srcs["mdraid"]["present"] is True and srcs["mdraid"]["available"] is False
    assert srcs["mdraid"]["status"] == "warning"


def test_stale_reading_and_stale_host_are_marked(env):
    env.push(batch(samples=[s("cpu", "utilization_pct", 5.0, "%", ts=NOW - 5000),
                            s("memory", "mem_total", 8e9, "B")],
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
                        "components": [{"source": "hwmon", "metric": "temp", "warn": 30,
                                        "crit": 40}]},
                       {"name": "Silent", "type": "pushed_host", "host": "ghost"}])
    try:
        e.push(batch(samples=[s("hwmon", "temp", 35.0, "C", sensor="x")],
                     sources=[{"source": "hwmon", "available": True}]))
        e.push(batch("other", samples=[s("cpu", "load", 1.0)]))
        e.login()
        assert e.client.get("/api/hosts/nope").status_code == 404
        rows = {r["host"]: r for r in e.client.get("/api/hosts").json()["hosts"]}
        assert set(rows) == {"nas01", "other", "ghost"}
        assert rows["nas01"]["monitored"] and rows["nas01"]["monitor"]["name"] == "NAS"
        assert rows["other"]["monitored"] is False and rows["other"]["monitor"] is None
        assert rows["ghost"]["heard"] is False and rows["ghost"]["status"] == "critical"
        assert rows["nas01"]["sections"]["temperatures"] == "warning"  # YAML limit beats default
        assert rows["nas01"]["states"]["ups"] == "not_reported"
        ghost = e.client.get("/api/hosts/ghost").json()
        assert ghost["heard"] is False and ghost["last_seen"] is None
        assert "no batch received yet" in ghost["status_reason"]
    finally:
        e.close()


def test_host_page_is_static_and_dashboard_links_to_it(env):
    r = env.client.get("/host")
    assert r.status_code == 200 and "host.js" in r.text
    assert "/api/hosts" in env.client.get("/static/host.js").text
    assert "hostlink" in env.client.get("/static/app.js").text


def test_host_name_with_slash_opens_in_api_and_page_link(env):
    env.push(batch(host="rack/nas 01?#%", samples=FULL, sources=FULL_SOURCES))
    env.login()
    assert [h["host"] for h in env.client.get("/api/hosts").json()["hosts"]] == ["rack/nas 01?#%"]
    from urllib.parse import quote
    r = env.client.get(f"/api/hosts/{quote('rack/nas 01?#%', safe='')}")
    assert r.status_code == 200 and r.json()["host"] == "rack/nas 01?#%"
    assert env.client.get("/api/hosts/rack/nas 01?#%".replace("?#%", "%3F%23%25")).status_code == 200
    assert env.client.get("/host?name=rack%2Fnas%2001").status_code == 200
