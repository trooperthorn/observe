"""Home Assistant host mode: /api/config and /api/states become a hostwatch batch for the Hosts
page. The fixtures follow the REST shapes; the hassio and HA SOC entity ids are unverified
against a live install (see observe/checks/ha_host.py)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from observe import hostview
from observe.checks import build_check
from observe.checks.base import Result
from observe.checks.ha_host import build_batch
from observe.config import Config
from observe.ingest.schema import Batch
from observe.store import ABSENT_REASON, Store

from .fakes.servers import HA_TOKEN, json_server

NOW = 2_000_000.0
CONFIG = {"version": "2026.9.1", "state": "RUNNING", "safe_mode": False, "recovery_mode": False,
          "location_name": "Home"}


def st(eid: str, state: str, **attrs: Any) -> dict[str, Any]:
    return {"entity_id": eid, "state": state, "attributes": attrs,
            "last_changed": "2026-10-01T00:00:00+00:00"}


STATES = [
    st("light.hall", "on"), st("light.porch", "unavailable"),
    st("sensor.nas_temp", "41.5"),
    st("update.home_assistant_core_update", "on", installed_version="2026.9.1",
       latest_version="2026.10.0"),
    st("update.home_assistant_supervisor_update", "off", installed_version="2026.09.0",
       latest_version="2026.09.0"),
    st("update.esphome", "off", installed_version="1", latest_version="1"),
    st("sensor.home_assistant_core_cpu_percent", "3.2"),
    st("sensor.home_assistant_core_memory_percent", "12.5"),
    st("sensor.home_assistant_supervisor_cpu_percent", "unavailable"),
    st("sensor.home_assistant_host_disk_used", "20"),
    st("sensor.home_assistant_host_disk_total", "100"),
    st("sensor.home_assistant_host_disk_free", "80"),
    st("sensor.ha_soc_posture_score", "92"),
    st("binary_sensor.ha_soc_suspicious_activity", "off"),
]


def view(batch, now: float = NOW, stale_after: float = 900.0, last_seen: float = NOW):
    sources = {x.source: {"available": x.available and x.present,
                          "reason": x.reason if x.present else ABSENT_REASON,
                          "updated": batch.sent_at} for x in batch.sources}
    data = {"samples": [{"source": s.source, "metric": s.metric, "labels": s.labels,
                         "value": s.value, "unit": s.unit, "ts": s.ts} for s in batch.samples],
            "sources": sources}
    row = {"host": batch.host, "last_seen": last_seen, "platform": "homeassistant"}
    return hostview.build_host_view(row, data, sources, [], now, stale_after, None, {}, None)


def item(v: dict[str, Any], section: str, metric: str, **labels: str) -> dict[str, Any]:
    return next(i for i in v[section]["items"] if i["metric"] == metric
                and all(i["labels"].get(k) == x for k, x in labels.items()))


def test_states_map_to_samples_and_grades():
    v = view(build_batch("homeassistant", CONFIG, STATES, NOW))
    assert item(v, "ha", "running")["status"] == "good"
    assert item(v, "ha", "entities_total")["value"] == len(STATES)
    assert item(v, "ha", "unavailable_entities")["value"] == 2
    assert item(v, "ha", "entities", domain="update")["value"] == 3
    assert item(v, "ha", "version", component="core")["labels"]["version"] == "2026.9.1"
    assert item(v, "ha", "version", component="supervisor")["labels"]["version"] == "2026.09.0"
    assert item(v, "ha", "posture_score")["value"] == 92.0
    assert item(v, "containers", "cpu_percent", name="home_assistant_core")["value"] == 3.2
    assert item(v, "containers", "memory_percent", name="home_assistant_core")["status"] == "good"
    unknown = item(v, "containers", "cpu_percent", name="home_assistant_supervisor")
    assert unknown["value"] is None and unknown["status"] == "warning"  # never zero
    assert item(v, "disks", "disk_used_pct")["value"] == 20.0
    assert v["containers"]["state"] == "ok" and v["ha"]["state"] == "ok"


def test_update_pending_is_warning():
    v = view(build_batch("homeassistant", CONFIG, STATES, NOW))
    pend = item(v, "ha", "update_pending", entity_id="update.home_assistant_core_update")
    assert pend["labels"]["entity_id"] == "update.home_assistant_core_update"
    assert pend["status"] == "warning" and v["ha"]["status"] == "warning"
    clean = [s for s in STATES if s["entity_id"] != "update.home_assistant_core_update"]
    v = view(build_batch("homeassistant", CONFIG, clean, NOW))
    assert all(i["value"] == 0.0 for i in v["ha"]["items"] if i["metric"] == "update_pending")
    assert v["ha"]["status"] == "good"


def test_not_running_is_critical_and_safe_mode_warns():
    v = view(build_batch("homeassistant", {**CONFIG, "state": "NOT_RUNNING"}, STATES, NOW))
    assert item(v, "ha", "running")["status"] == "critical" and v["status"] == "critical"
    v = view(build_batch("homeassistant", {**CONFIG, "safe_mode": True}, [], NOW))
    assert item(v, "ha", "safe_mode")["status"] == "warning"


def test_many_unavailable_entities_warn():
    many = [st(f"sensor.s{i}", "unavailable") for i in range(hostview.HA_UNAVAILABLE_WARN)]
    v = view(build_batch("homeassistant", CONFIG, many, NOW))
    assert item(v, "ha", "unavailable_entities")["status"] == "warning"
    few = many[: hostview.HA_UNAVAILABLE_WARN - 1]
    v = view(build_batch("homeassistant", CONFIG, few, NOW))
    assert item(v, "ha", "unavailable_entities")["status"] == "good"


def test_container_percent_grades_and_absent_sources():
    states = [st("sensor.some_addon_cpu_percent", "90"),
              st("sensor.some_addon_memory_percent", "96"),
              st("binary_sensor.some_addon_running", "on")]
    v = view(build_batch("homeassistant", CONFIG, states, NOW))
    assert item(v, "containers", "cpu_percent")["status"] == "warning"
    assert item(v, "containers", "memory_percent")["status"] == "critical"
    v = view(build_batch("homeassistant", CONFIG, [], NOW))
    assert v["containers"]["state"] == "absent" and v["containers"]["status"] == "good"


def test_an_installed_update_clears_the_pending_state():
    pending = st("update.home_assistant_core_update", "on", installed_version="2026.9.1",
                 latest_version="2026.10.0")
    # the entity state still says on, but the installed version is now the latest one
    lagging = st("update.home_assistant_core_update", "on", installed_version="2026.10.0",
                 latest_version="2026.10.0")
    installed = st("update.home_assistant_core_update", "off", installed_version="2026.10.0",
                   latest_version="2026.10.0")
    first = build_batch("homeassistant", CONFIG, [pending], NOW)
    assert next(s for s in first.samples if s.metric == "updates_pending").value == 1.0
    for done in (lagging, installed):
        b = build_batch("homeassistant", CONFIG, [done], NOW + 300)
        assert next(s for s in b.samples if s.metric == "updates_pending").value == 0.0
        zero = [s for s in b.samples if s.metric == "update_pending"]
        assert len(zero) == 1 and zero[0].value == 0.0
        assert view(b, now=NOW + 300)["ha"]["status"] == "good"


async def test_an_installed_update_supersedes_the_stored_pending_reading(store):
    pending = st("update.home_assistant_core_update", "on", installed_version="1",
                 latest_version="2")
    done = st("update.home_assistant_core_update", "off", installed_version="2",
              latest_version="2")
    await store.ingest_batch(build_batch("homeassistant", CONFIG, [pending], NOW), {}, now=NOW)
    await store.ingest_batch(build_batch("homeassistant", CONFIG, [done], NOW + 300), {},
                             now=NOW + 300)
    data = await store.latest_host("homeassistant")
    cur = [s for s in data["samples"] if s["metric"] == "update_pending"]
    assert len(cur) == 1 and cur[0]["value"] == 0.0


def test_the_updates_mode_and_the_host_mode_agree_on_pending():
    from observe.checks.ha_host import update_is_pending
    assert update_is_pending(st("update.a", "on", installed_version="1", latest_version="2"))
    assert update_is_pending(st("update.a", "on"))
    assert not update_is_pending(st("update.a", "on", installed_version="2", latest_version="2"))
    assert not update_is_pending(st("update.a", "off", installed_version="1", latest_version="2"))


def test_a_sensor_from_another_integration_is_not_a_container():
    states = [st("sensor.nas_cpu_percent", "99"), st("sensor.living_room_pc_memory_percent", "97"),
              st("sensor.some_addon_cpu_percent", "50"), st("update.some_addon_update", "off")]
    b = build_batch("homeassistant", CONFIG, states, NOW)
    names = {(s.metric, s.labels["name"]) for s in b.samples if s.source == "hassio"}
    assert names == {("cpu_percent", "some_addon")}
    foreign = build_batch("homeassistant", CONFIG, states[:2], NOW)
    assert not any(s.source == "hassio" for s in foreign.samples)
    v = view(foreign)
    assert v["containers"]["state"] == "absent" and v["containers"]["status"] == "good"


def test_a_healthy_default_home_assistant_grades_good():
    default = [st("light.hall", "on"), st("sensor.nas_temp", "41.5"),
               st("update.home_assistant_core_update", "off", installed_version="2026.9.1",
                  latest_version="2026.9.1"),
               st("update.esphome", "off", installed_version="1", latest_version="1")]
    v = view(build_batch("homeassistant", CONFIG, default, NOW))
    assert v["status"] == "good"
    for section in ("ha", "containers", "disks"):
        assert v[section]["status"] == "good", section
    assert v["containers"]["state"] == "absent" and v["disks"]["state"] == "absent"


async def test_a_default_install_stores_absent_sources_and_grades_good(store):
    await store.ingest_batch(build_batch("homeassistant", CONFIG, [st("light.hall", "on")], NOW),
                             {}, now=NOW)
    sources = await store.host_sources("homeassistant")
    for name in ("hassio", "ha_soc"):
        assert sources[name]["reason"] == ABSENT_REASON and not sources[name]["available"]
    data = await store.latest_host("homeassistant")
    row = {"host": "homeassistant", "last_seen": NOW, "platform": data["platform"]}
    v = hostview.build_host_view(row, data, sources, [], NOW, 900.0, None, {}, None)
    assert v["status"] == "good"


def _producer_batch(platform: str, agent: str, ts: float) -> Batch:
    return Batch.model_validate({
        "schema_version": 1, "agent_version": agent, "host": "homeassistant",
        "platform": platform, "sent_at": ts,
        "sources": [{"source": "x", "available": True}], "samples": []})


def test_declared_pull_producers_match_the_checks():
    from observe.checks import snmp
    from observe.checks.ha_host import AGENT_VERSION
    from observe.store import PULL_PRODUCER_RANK
    assert set(PULL_PRODUCER_RANK) == {snmp.AGENT_VERSION, AGENT_VERSION}


async def test_platform_and_version_are_stable_with_every_producer_writing(store):
    order = [("homeassistant", "ha_soc 1.0.0"), ("homeassistant", "observe-ha-host"),
             ("snmp", "observe-snmp-host")]
    for i in range(9):
        platform, agent = order[i % 3]
        await store.ingest_batch(_producer_batch(platform, agent, NOW + 60 * i), {},
                                 now=NOW + 60 * i)
        row = await store.latest_host("homeassistant")
        assert (row["platform"], row["agent_version"]) == ("homeassistant", "ha_soc 1.0.0")


async def test_pull_producers_rank_home_assistant_over_snmp_in_any_order(store):
    for i, (platform, agent) in enumerate([("snmp", "observe-snmp-host"),
                                           ("homeassistant", "observe-ha-host"),
                                           ("snmp", "observe-snmp-host")]):
        await store.ingest_batch(_producer_batch(platform, agent, NOW + i), {}, now=NOW + i)
    row = await store.latest_host("homeassistant")
    assert (row["platform"], row["agent_version"]) == ("homeassistant", "observe-ha-host")


def test_stale_host_goes_stale():
    b = build_batch("homeassistant", CONFIG, STATES, NOW)
    v = view(b, now=NOW + 5000, stale_after=900.0)
    assert v["stale"] is True and v["status"] == "critical"
    assert v["ha"]["state"] == "stale" and all(i["stale"] for i in v["ha"]["items"])


# ------------------------------------------------------------ the check, end to end


def make(srv, store, **mon):
    cfg = Config.model_validate({
        "defaults": {"timeout": 20},
        "credentials": {"ha": {"type": "homeassistant", "token": HA_TOKEN}},
        "monitors": [{"name": "HA host", "type": "homeassistant", "host": "127.0.0.1",
                      "port": srv.server_address[1], "https": False, "credential": "ha",
                      "mode": "host", **mon}]})
    return cfg, build_check(cfg.monitors[0], cfg, store)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "w.db"))
    yield s
    s.close()


def server(states, config=None):
    return json_server({"/api/config": config or CONFIG, "/api/states": states},
                       ("Authorization", f"Bearer {HA_TOKEN}"))


def test_host_mode_defaults_to_300_seconds_and_a_host_name(store):
    srv = server(STATES)
    try:
        cfg, _ = make(srv, store)
        m = cfg.monitors[0]
        assert m.interval == 300 and m.host_name == "homeassistant"
        cfg, _ = make(srv, store, interval=120, host_name="ha-prod")
        assert cfg.monitors[0].interval == 120 and cfg.monitors[0].host_name == "ha-prod"
    finally:
        srv.shutdown()


async def test_check_ingests_a_batch_for_the_host(store):
    srv = server(STATES)
    try:
        _, chk = make(srv, store)
        res = await chk.run()
        assert res.result is Result.OK, res.message
        data = await store.latest_host("homeassistant")
        assert data is not None and data["platform"] == "homeassistant"
        metrics = {(s["source"], s["metric"]) for s in data["samples"]}
        assert ("homeassistant", "running") in metrics and ("hassio", "cpu_percent") in metrics
        assert {p for p, _h in srv.seen} == {"/api/config", "/api/states"}
    finally:
        srv.shutdown()


async def test_a_failed_read_stores_nothing_and_fails(store):
    srv = json_server({"/api/config": CONFIG}, ("Authorization", f"Bearer {HA_TOKEN}"))
    try:
        _, chk = make(srv, store)
        res = await chk.run()
        assert res.result is Result.FAIL and "404" in res.message
        assert await store.latest_host("homeassistant") is None
    finally:
        srv.shutdown()


async def test_four_megabyte_states_parse_within_budget(store):
    big = [st(f"sensor.device_{i}_reading", str(i % 97), unit_of_measurement="W",
              friendly_name=f"Device {i} reading", icon="mdi:flash") for i in range(20500)]
    big += STATES
    assert len(json.dumps(big)) > 4 * 1024 * 1024
    srv = server(big)
    try:
        _, chk = make(srv, store)
        start = time.perf_counter()
        res = await chk.run()
        elapsed = time.perf_counter() - start
        assert res.result is Result.OK, res.message
        assert elapsed < 5.0
        data = await store.latest_host("homeassistant")
        total = next(s for s in data["samples"] if s["metric"] == "entities_total")
        assert total["value"] == len(big)
    finally:
        srv.shutdown()


async def test_an_oversize_reply_is_refused(store, monkeypatch):
    from observe.checks import apps
    monkeypatch.setattr(apps, "MAX_HA_BODY", 1000)
    srv = server(STATES)
    try:
        _, chk = make(srv, store)
        res = await chk.run()
        assert res.result is Result.FAIL and "larger than" in res.message
    finally:
        srv.shutdown()
