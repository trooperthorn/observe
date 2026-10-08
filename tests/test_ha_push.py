"""The HA SOC push contract: the golden OTLP files ha_Int_soc sends (tests/fixtures/observe_otlp,
see the README there for the source commit) go through the real /v1/metrics and /v1/logs routes
and are graded on the host page under the OpenTelemetry names of docs/DATA-API-DESIGN.md
section 3.4. The key binds the push to its host, an observe.ha.crash log is a boot classification,
and a host that sends observe.ha.* without os.type is platform homeassistant."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from observe import auth, hostview
from observe.alerts import Alerter
from observe.checks.ha_host import build_batch
from observe.checks.host import PushedHostCheck
from observe.ingest.boot import classify_events
from observe.ingest.keys import create_key
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

FIXTURES = Path(__file__).parent / "fixtures" / "observe_otlp"
HOST = "haos-lab"
PASSWORD = "correct horse battery"
T_SENT = 1_790_000_005.0  # five seconds after the points of the fixture
SILENT_STOP = 1_789_787_700.0  # the observe.ha.crash silent_stop record of the fixture


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Env:
    def __init__(self, tmp_path, now: float = T_SENT) -> None:
        self.path = str(tmp_path / "w.db")
        self.store = Store(self.path)
        self.now = now
        srv = {"db_path": self.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
               "argon2_parallelism": 1, "session_idle_s": 100_000_000,
               "session_absolute_s": 200_000_000}
        self.cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}], server=srv)
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        self.client = TestClient(create_app(self.cfg, self.store, sched, alerter,
                                            ingest_clock=lambda: self.now,
                                            auth_clock=lambda: self.now),
                                 base_url="https://testserver")

    def key(self, host: str) -> str:
        return asyncio.run(create_key(self.store, host))[0]

    def post(self, path: str, body: dict[str, Any], key: str):
        return self.client.post(path, json=body, headers={"Authorization": f"Bearer {key}"})

    def push(self, key: str, metrics: dict[str, Any] | None = None,
             logs: dict[str, Any] | None = None) -> None:
        for path, body in (("/v1/logs", logs if logs is not None else load("logs")),
                           ("/v1/metrics", metrics if metrics is not None else load("metrics"))):
            r = self.post(path, body, key)
            assert r.status_code == 200, r.text
            assert r.json() == {}, r.text  # nothing refused

    def login(self) -> None:
        asyncio.run(auth.create_user(self.store, self.cfg, "alice", PASSWORD, False, now=self.now))
        r = self.client.post("/api/login", json={"username": "alice", "password": PASSWORD})
        assert r.status_code == 200

    def view(self, host: str = HOST) -> dict[str, Any]:
        r = self.client.get(f"/api/v2/hosts/{host}")
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


def test_the_golden_push_is_accepted_and_graded_in_every_ha_section(env):
    env.push(env.key(HOST))
    env.login()
    d = env.view()
    assert d["platform"] == "homeassistant"  # no os.type, observe.ha.* points
    assert d["heard"] and not d["stale"]
    # ha: an unhealthy Supervisor is Critical, an unsupported one a Warning
    assert item(d, "ha", "observe.ha.supervisor.healthy")["status"] == "critical"
    assert item(d, "ha", "observe.ha.supervisor.supported")["status"] == "warning"
    assert item(d, "ha", "observe.ha.supervisor.unhealthy_reasons",
                **{"observe.ha.supervisor.reason": "privileged"})["status"] == "good"
    assert d["ha"]["status"] == "critical" and d["ha"]["state"] == "ok"
    # containers: ratios graded at 0.85 and 0.95; a stopped one and a breach are Warnings
    cpu = {i["labels"]["container.name"]: i["status"] for i in d["containers"]["items"]
           if i["metric"] == "container.cpu.utilization"}
    assert cpu == {"core": "good", "supervisor": "good", "core_mosquitto": "good",
                   "a0d7b954_probe": "warning"}
    assert item(d, "containers", "container.memory.utilization",
                **{"container.name": "a0d7b954_probe"})["status"] == "warning"
    assert item(d, "containers", "container.memory.usage",
                **{"container.name": "core"})["value"] == 734003200.0
    assert item(d, "containers", "observe.ha.container.running",
                **{"container.name": "core_samba"})["status"] == "warning"
    assert item(d, "containers", "observe.ha.container.running",
                **{"container.name": "core"})["status"] == "good"
    assert item(d, "containers", "observe.ha.watchdog.breaches")["status"] == "warning"
    assert d["containers"]["status"] == "warning"
    # integrations and repairs
    assert item(d, "integrations", "observe.ha.integration.count",
                **{"observe.ha.integration.category": "errors"})["status"] == "warning"
    assert item(d, "integrations", "observe.ha.integration.count",
                **{"observe.ha.integration.category": "collection"})["status"] == "good"
    assert item(d, "integrations", "observe.ha.integration.errors",
                **{"observe.ha.integration": "unifi"})["value"] == 41.0
    assert item(d, "repairs", "observe.ha.repair.issues",
                **{"observe.ha.repair.domain": "hassio"})["status"] == "warning"
    # backups: no password on the backups is a Warning
    assert item(d, "backups", "observe.ha.backup.unprotected")["status"] == "warning"
    assert d["backups"]["status"] == "warning"
    assert d["status"] == "critical"
    # every HA SOC point is shown by some section: nothing is dropped
    shown = sum(len(d[n]["items"]) for n in ("ha", "containers", "integrations", "repairs",
                                             "backups"))
    pushed = sum(len(m.get("gauge", m.get("sum"))["dataPoints"])
                 for rm in load("metrics")["resourceMetrics"] for sm in rm["scopeMetrics"]
                 for m in sm["metrics"])
    assert shown == pushed


def test_no_old_home_assistant_key_remains():
    old = {"running", "safe_mode", "recovery_mode", "update_pending", "updates_pending",
           "unavailable_entities", "entities_total", "entities", "version", "healthy",
           "supported", "unhealthy_reasons", "posture_score", "open_detections",
           "users_at_risk", "suspicious_activity", "cpu_percent", "memory_percent",
           "memory_usage_bytes", "memory_limit_bytes", "breach_count", "issue",
           "issues_total", "loaded_total", "open", "open_total", "backups_total",
           "last_backup_ok", "last_success_age_hours", "disk_used_pct", "disk_free_gb",
           "disk_used_gb", "disk_total_gb"}
    old_sources = {"homeassistant", "hassio", "ha_soc", "ha_container", "ha_watchdog",
                   "ha_integrations", "ha_repairs", "ha_backup", "ha_supervisor"}
    keys = {k for table in hostview.RULES.values() for k in table}
    assert not any(scope in old_sources or metric in old for scope, metric in keys)
    states = [{"entity_id": "sensor.home_assistant_core_cpu_percent", "state": "3",
               "attributes": {}},
              {"entity_id": "sensor.home_assistant_host_disk_used", "state": "1",
               "attributes": {}},
              {"entity_id": "sensor.home_assistant_host_disk_total", "state": "4",
               "attributes": {}},
              {"entity_id": "sensor.ha_soc_posture_score", "state": "9", "attributes": {}}]
    batch = build_batch("homeassistant", {"version": "1", "state": "RUNNING"}, states, 1.0)
    assert not any(s.source in old_sources or s.metric in old for s in batch.samples)
    assert not any(s.source in old_sources for s in batch.sources)


def test_graded_levels_follow_the_values(env):
    metrics = copy.deepcopy(load("metrics"))
    unifi = {"key": "observe.ha.integration", "value": {"stringValue": "unifi"}}
    for rm in metrics["resourceMetrics"]:
        for sm in rm["scopeMetrics"]:
            for m in sm["metrics"]:
                for dp in m["gauge"]["dataPoints"]:
                    if m["name"] == "observe.ha.supervisor.healthy":
                        dp["asDouble"] = 1.0
                    if m["name"] == "container.cpu.utilization":
                        dp["asDouble"] = 0.97
                    if m["name"] == "observe.ha.integration.errors" and unifi in dp["attributes"]:
                        dp["attributes"] = [a for a in dp["attributes"]
                                            if a["key"] != "observe.ha.integration.category"]
                        dp["attributes"].append({"key": "observe.ha.integration.category",
                                                 "value": {"stringValue": "failing"}})
    env.push(env.key(HOST), metrics=metrics)
    env.login()
    d = env.view()
    assert item(d, "ha", "observe.ha.supervisor.healthy")["status"] == "good"
    assert item(d, "containers", "container.cpu.utilization",
                **{"container.name": "core"})["status"] == "critical"
    failing = item(d, "integrations", "observe.ha.integration.errors",
                   **{"observe.ha.integration": "unifi"})
    assert failing["status"] == "critical"


def _add_points(metrics: dict[str, Any], scope: str, name: str, unit: str,
                points: list[tuple[float, dict[str, str]]]) -> None:
    """Add a gauge with these (value, string attributes) points to a scope of the fixture."""
    dps = [{"asDouble": v, "timeUnixNano": "1790000000000000000",
            "attributes": [{"key": k, "value": {"stringValue": t}} for k, t in attrs.items()]}
           for v, attrs in points]
    for rm in metrics["resourceMetrics"]:
        for sm in rm["scopeMetrics"]:
            if sm["scope"]["name"] == scope:
                sm["metrics"].append({"name": name, "unit": unit, "gauge": {"dataPoints": dps}})
                return
    raise AssertionError(scope)


@pytest.mark.parametrize("hours,status", [(10, "good"), (40, "warning"), (100, "critical")])
def test_the_backup_age_is_graded_in_seconds(env, hours, status):
    metrics = copy.deepcopy(load("metrics"))
    _add_points(metrics, "ha_soc.collector.backup", "observe.ha.backup.last_success_age", "s",
                [(hours * 3600.0, {})])
    env.push(env.key(HOST), metrics=metrics)
    env.login()
    d = env.view()
    assert item(d, "backups", "observe.ha.backup.last_success_age")["status"] == status


def test_a_failed_last_backup_is_a_warning_and_a_good_one_is_good(env):
    metrics = copy.deepcopy(load("metrics"))
    _add_points(metrics, "ha_soc.collector.backup", "observe.ha.backup.last_ok", "1",
                [(0.0, {})])
    env.push(env.key(HOST), metrics=metrics)
    env.login()
    assert item(env.view(), "backups", "observe.ha.backup.last_ok")["status"] == "warning"


@pytest.mark.parametrize("severity,status", [("critical", "critical"), ("error", "warning"),
                                             ("warning", "warning")])
def test_the_repair_severity_attribute_sets_the_level(env, severity, status):
    metrics = copy.deepcopy(load("metrics"))
    _add_points(metrics, "ha_soc.collector.repairs", "observe.ha.repair.issues", "{issue}",
                [(2.0, {"observe.ha.repair.state": "open", "observe.ha.repair.domain": "zz",
                        "observe.ha.repair.severity": severity})])
    env.push(env.key(HOST), metrics=metrics)
    env.login()
    got = item(env.view(), "repairs", "observe.ha.repair.issues",
               **{"observe.ha.repair.domain": "zz"})
    assert got["status"] == status


def test_the_host_events_list_the_pushed_breach_and_the_silent_stop(env):
    env.push(env.key(HOST))
    env.login()
    d = env.view()
    kinds = {e["kind"] for e in d["events"]}
    assert {"boot.silent_stop", "observe.ha.watchdog.breach"} <= kinds


def test_the_fixtures_stay_inside_the_size_bounds_and_an_oversized_push_is_refused(env):
    from observe.ingest.schema import MAX_BODY_BYTES
    for name in ("metrics", "logs"):
        assert len(json.dumps(load(name))) < MAX_BODY_BYTES
    key = env.key(HOST)
    big = copy.deepcopy(load("metrics"))
    pad = "x" * 1000
    big["padding"] = [pad] * (MAX_BODY_BYTES // 1000 + 10)
    assert len(json.dumps(big)) > MAX_BODY_BYTES
    assert env.post("/v1/metrics", big, key).status_code == 413
    assert asyncio.run(env.store.host_rows()) == []


def test_a_host_with_os_type_keeps_its_platform(env):
    metrics = copy.deepcopy(load("metrics"))
    metrics["resourceMetrics"][0]["resource"]["attributes"].append(
        {"key": "os.type", "value": {"stringValue": "linux"}})
    env.push(env.key(HOST), metrics=metrics)
    env.login()
    assert env.view()["platform"] == "linux"


def test_key_bound_to_one_host_cannot_push_as_another(env):
    key = env.key(HOST)
    assert env.post("/v1/metrics", load("metrics"), env.key("nas01")).status_code == 403
    assert env.post("/v1/logs", load("logs"), env.key("nas01")).status_code == 403
    assert asyncio.run(env.store.host_rows()) == []
    other = copy.deepcopy(load("metrics"))
    for a in other["resourceMetrics"][0]["resource"]["attributes"]:
        if a["key"] == "host.name":
            a["value"]["stringValue"] = "nas01"
    assert env.post("/v1/metrics", other, key).status_code == 403
    assert asyncio.run(env.store.host_rows()) == []


def test_resend_of_the_push_stores_nothing_more(env):
    key = env.key(HOST)
    env.push(key)
    count = "SELECT (SELECT COUNT(*) FROM host_events), (SELECT COUNT(*) FROM host_sources)"
    before = env.store.storage.read_sync(lambda db: db.execute(count).fetchone())
    env.push(key)
    assert env.store.storage.read_sync(lambda db: db.execute(count).fetchone()) == before


# ---- crash forensics ------------------------------------------------------------------------

def crash_logs(*classes: str) -> dict[str, Any]:
    """The logs of the fixture reduced to the observe.ha.crash records of these classifications."""
    logs = copy.deepcopy(load("logs"))
    scopes = logs["resourceLogs"][0]["scopeLogs"]
    for scope in scopes:
        scope["logRecords"] = [
            r for r in scope["logRecords"]
            if any(a["key"] == "observe.ha.crash.classification"
                   and a["value"]["stringValue"] in classes for a in r["attributes"])]
    logs["resourceLogs"][0]["scopeLogs"] = [s for s in scopes if s["logRecords"]]
    return logs


def boot_row(env: Env) -> dict[str, Any]:
    return next(r for r in asyncio.run(env.store.host_rows()) if r["host"] == HOST)


@pytest.mark.parametrize("cls,flag,severity", [
    ("silent_stop", 0, "critical"), ("kernel_fault", 0, "critical"),
    ("core_restart", 1, "warning"), ("clean_reboot", 1, "info")])
def test_observe_ha_crash_is_classified_by_the_boot_classifier(tmp_path, cls, flag, severity):
    rec = copy.deepcopy(crash_logs("silent_stop")["resourceLogs"][0]["scopeLogs"][0]
                        ["logRecords"][0])
    for a in rec["attributes"]:
        if a["key"] == "observe.ha.crash.classification":
            a["value"]["stringValue"] = cls
    rec["severityText"] = {"critical": "ERROR", "warning": "WARN", "info": "INFO"}[severity]
    logs = crash_logs("silent_stop")
    logs["resourceLogs"][0]["scopeLogs"][0]["logRecords"] = [rec]
    e = Env(tmp_path)
    try:
        e.push(e.key(HOST), logs=logs)
        row = boot_row(e)
        assert (row["boot_id"], row["clean_shutdown"]) == ("crash-2026-09-19T031500Z", flag)
        assert row["platform"] == "homeassistant"
        ev = asyncio.run(e.store.host_events(HOST))[0]
        assert ev["kind"] == "boot." + cls and ev["severity"] == severity
        assert ev["detail"]["classification"] == ("clean" if flag else "crash")
        assert ev["detail"]["observe.ha.crash.classification"] == cls
    finally:
        e.close()


async def _check(store: Store, now: float, **extra: Any) -> Any:
    cfg = make_config([{"name": HOST, "type": "pushed_host", "host": HOST, "stale_after": 10**9,
                        "components": [], **extra}], defaults={"failures_to_down": 1, "timeout": 1})
    check = Scheduler(cfg, store, Alerter(cfg)).checks[HOST]
    assert isinstance(check, PushedHostCheck)
    check.clock = lambda: now
    return await check.probe()


def test_a_silent_stop_fires_the_crash_check_and_a_clean_boot_does_not(tmp_path):
    e = Env(tmp_path, now=SILENT_STOP + 60)
    try:
        key = e.key(HOST)
        e.push(key, logs=crash_logs("silent_stop"))
        row = boot_row(e)
        assert row["clean_shutdown"] == 0 and row["boot_id"] == "crash-2026-09-19T031500Z"
        assert row["boot_ts"] == pytest.approx(SILENT_STOP)
        res = asyncio.run(_check(e.store, SILENT_STOP + 60, crash_hold_s=3600,
                                 crash_result="fail"))
        assert res.detail["components"]["boot"] == "critical" and res.result.value == "fail"
        # a later clean reboot replaces the boot fields, and the check goes quiet
        e.now = SILENT_STOP + 400_000
        e.push(key, logs=crash_logs("clean_reboot"))
        row = boot_row(e)
        assert row["clean_shutdown"] == 1 and row["boot_id"] == "crash-2026-09-21T000000Z"
        res = asyncio.run(_check(e.store, row["boot_ts"] + 60, crash_hold_s=3600))
        assert "boot" not in res.detail["components"]
    finally:
        e.close()


def test_the_pulled_batch_uses_the_opentelemetry_names_beside_the_push(env):
    pulled = build_batch(
        HOST, {"version": "2026.9.1", "state": "RUNNING", "safe_mode": False,
               "recovery_mode": False},
        [{"entity_id": "light.hall", "state": "on", "attributes": {}},
         {"entity_id": "sensor.home_assistant_core_cpu_percent", "state": "3.2",
          "attributes": {}}],
        env.now - 5)
    asyncio.run(env.store.ingest_batch(pulled, classify_events(pulled.events), now=env.now - 5))
    env.push(env.key(HOST))
    env.login()
    d = env.view()
    assert len(asyncio.run(env.store.host_rows())) == 1
    sources = {s["source"] for s in d["sources"]}
    assert {"observe.check.homeassistant", "observe.check.hassio"} <= sources
    total = next(i for i in d["ha"]["items"]
                 if i["metric"] == "observe.ha.entity.count" and not i["labels"])
    assert total["value"] == 2.0
    scopes = {i["source"] for i in d["containers"]["items"]}
    assert {"observe.check.hassio", "ha_soc.collector.containers"} <= scopes
    assert d["integrations"]["state"] == "ok" and d["repairs"]["state"] == "ok"
