"""The ha_Int_soc push contract: a recorded batch replayed through POST /internal/v1/ingest, graded
on the host page, bound to its host by the key, and merged with the non-admin token reading.
The fixture is shaped from the ha_Int_soc code and docs; the metric names and the label values
are unverified against a live push (see docs/ARCHITECTURE.md)."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.checks.ha_host import build_batch
from observe.ingest.boot import classify_events
from observe.ingest.keys import create_key
from observe.ingest.schema import MAX_BODY_BYTES, Batch
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

FIXTURE = Path(__file__).parent / "fixtures" / "ha_soc" / "push_batch.json"
PASSWORD = "correct horse battery"
PATH = "/internal/v1/ingest"


def load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class Env:
    def __init__(self, tmp_path) -> None:
        self.path = str(tmp_path / "w.db")
        self.store = Store(self.path)
        self.now = load()["sent_at"] + 5
        srv = {"db_path": self.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
               "argon2_parallelism": 1, "session_idle_s": 100_000, "session_absolute_s": 200_000}
        self.cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}], server=srv)
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        self.client = TestClient(create_app(self.cfg, self.store, sched, alerter,
                                            ingest_clock=lambda: self.now,
                                            auth_clock=lambda: self.now),
                                 base_url="https://testserver")

    def key(self, host: str) -> str:
        return asyncio.run(create_key(self.store, host))[0]

    def push(self, body: dict, key: str):
        return self.client.post(PATH, json=body, headers={"Authorization": f"Bearer {key}"})

    def login(self) -> None:
        asyncio.run(auth.create_user(self.store, self.cfg, "alice", PASSWORD, False, now=self.now))
        r = self.client.post("/api/login", json={"username": "alice", "password": PASSWORD})
        assert r.status_code == 200

    def view(self, host: str = "homeassistant") -> dict:
        r = self.client.get(f"/api/hosts/{host}")
        assert r.status_code == 200, r.text
        return r.json()

    def close(self) -> None:
        self.client.close()
        self.store.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def item(d: dict, section: str, metric: str, **labels: str) -> dict:
    return next(i for i in d[section]["items"] if i["metric"] == metric
                and all(i["labels"].get(k) == v for k, v in labels.items()))


def test_push_fixture_is_accepted_and_graded(env):
    key = env.key("homeassistant")
    r = env.push(load(), key)
    assert r.status_code == 200, r.text
    assert r.json() == {"stored": len(load()["samples"]), "events_stored": 2}
    env.login()
    d = env.view()
    assert d["heard"] and not d["stale"]
    # containers: a runaway add-on is Critical, a stopped one Warning, a quiet one Good
    assert item(d, "containers", "cpu_percent", slug="core_mosquitto")["status"] == "critical"
    assert item(d, "containers", "cpu_percent", slug="core")["status"] == "good"
    assert item(d, "containers", "running", slug="a0d7b954_ssh")["status"] == "warning"
    assert item(d, "containers", "breach_count", slug="core_mosquitto")["status"] == "warning"
    # integrations: a credential problem is a Warning, debug logging only is Good
    assert item(d, "integrations", "issue", domain="unifi")["status"] == "warning"
    assert item(d, "integrations", "issue", domain="hue")["status"] == "good"
    # repairs: an open warning is a Warning, zero open critical is Good
    assert item(d, "repairs", "open", severity="warning")["status"] == "warning"
    assert item(d, "repairs", "open", severity="critical")["status"] == "good"
    # backups and supervisor
    assert item(d, "backups", "last_success_age_hours")["status"] == "good"
    assert item(d, "ha", "healthy")["status"] == "good"
    assert d["containers"]["status"] == "critical"
    assert d["status"] == "critical"
    # crash events: the silent stop is listed, the host boot state is a crash
    kinds = {e["kind"] for e in d["events"]}
    assert {"boot.silent_stop", "ha_watchdog.breach"} <= kinds
    assert d["boot"]["clean_shutdown"] is False
    assert {s["source"] for s in d["sources"]} >= {
        "ha_container", "ha_watchdog", "ha_integrations", "ha_repairs", "ha_backup",
        "ha_supervisor"}


def test_graded_levels_follow_the_values(env):
    body = load()
    for smp in body["samples"]:
        if smp["metric"] == "last_success_age_hours":
            smp["value"] = 100.0
        if smp["metric"] == "healthy":
            smp["value"] = 0.0
        if smp["metric"] == "open" and smp["labels"]["severity"] == "critical":
            smp["value"] = 2.0
        if smp["metric"] == "issue" and smp["labels"]["domain"] == "unifi":
            smp["labels"]["category"] = "failing"
    assert env.push(body, env.key("homeassistant")).status_code == 200
    env.login()
    d = env.view()
    assert item(d, "backups", "last_success_age_hours")["status"] == "critical"
    assert item(d, "ha", "healthy")["status"] == "critical"
    assert item(d, "repairs", "open", severity="critical")["status"] == "critical"
    assert item(d, "integrations", "issue", domain="unifi")["status"] == "critical"


def test_key_bound_to_homeassistant_cannot_push_as_another_host(env):
    key = env.key("homeassistant")
    other = load()
    other["host"] = "nas01"
    assert env.push(other, key).status_code == 403
    assert asyncio.run(env.store.host_rows()) == []
    # and a key for another host cannot push as homeassistant
    assert env.push(load(), env.key("nas01")).status_code == 403
    assert asyncio.run(env.store.host_rows()) == []


def test_resend_of_the_push_is_acknowledged_once(env):
    key = env.key("homeassistant")
    assert env.push(load(), key).status_code == 200
    again = env.push(load(), key)
    assert again.json() == {"stored": 0, "events_stored": 0, "duplicate": True}


def test_push_stays_inside_the_size_bounds():
    body = load()
    assert len(json.dumps(body)) < MAX_BODY_BYTES
    assert len(body["samples"]) <= 5000 and len(body["events"]) <= 500
    big = copy.deepcopy(body)
    big["samples"] = big["samples"] * 300
    with pytest.raises(ValueError):
        Batch.model_validate(big)


def test_pull_and_push_merge_under_one_host(env):
    pulled = build_batch(
        "homeassistant",
        {"version": "2026.9.1", "state": "RUNNING", "safe_mode": False, "recovery_mode": False},
        [{"entity_id": "light.hall", "state": "on", "attributes": {}},
         {"entity_id": "sensor.home_assistant_core_cpu_percent", "state": "3.2",
          "attributes": {}}],
        env.now - 5)
    asyncio.run(env.store.ingest_batch(pulled, classify_events(pulled.events), now=env.now - 5))
    assert env.push(load(), env.key("homeassistant")).status_code == 200
    env.login()
    d = env.view()
    assert len(asyncio.run(env.store.host_rows())) == 1
    sources = {s["source"] for s in d["sources"]}
    assert {"homeassistant", "hassio", "ha_container"} <= sources
    assert item(d, "ha", "entities_total")["value"] == 2.0
    assert {i["source"] for i in d["containers"]["items"]} >= {"hassio", "ha_container"}
    assert d["integrations"]["state"] == "ok" and d["repairs"]["state"] == "ok"
