"""The hostwatch agent's golden OpenTelemetry fixtures drive Observe's consumers end to end.

The fixtures in tests/fixtures/otel are the agent's output (see its README for the source commit).
Each test sends them the way the agent does: the points become an OTLP metrics request and the
logs an OTLP logs request, both encoded as protobuf, then decode (observe/otlp/wire.py), normalize
(observe/otlp/normalize.py), ingest (Store.ingest_batch with the boot classification), and are read
back through the host view (observe/hostview.py), the pushed host check and the threshold rules.
A point that no section of the host page claims, or that is graded differently from the table
below, fails here, so a rename on either side of the contract is caught.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from observe import hostview, rules
from observe.alerts import Alerter
from observe.checks.host import PushedHostCheck
from observe.ingest.boot import classify_events
from observe.otelnames import COLLECTOR_PREFIX, collector_scope, rule_metric, short_source
from observe.otlp import normalize, wire
from observe.scheduler import Scheduler
from observe.store import Store

from .conftest import make_config
from .otlp_build import (gauge, log_record, logs_request, metrics_request, number, proto, total)

FIXTURES = Path(__file__).parent / "fixtures" / "otel"
COLLECTORS = sorted(p.stem for p in FIXTURES.glob("*.json") if p.stem != "events")
T0 = 1_700_000_000.0
NOW = T0 + 30.0
HOST = "nas01"

# Scopes that Observe's own checks and ha_Int_soc write, which are not hostwatch collector scopes
# (the SNMP check has its own section 3.5 test in test_snmp_otel.py, the Home Assistant scopes
# are tested in test_ha_push.py and test_ha_host.py).
OTHER_PRODUCERS = {"observe.check.snmp", "observe.check.homeassistant", "observe.check.hassio",
                   "observe.check.ha_soc"}
HA_SOC_PREFIX = "ha_soc.collector."

# Every point of every fixture: (collector, metric, a subset of its attributes) -> (section, grade).
# The Windows storage rows (physical disk, pool, virtual disk) are `hw.status` and are told apart by
# `hw.type`; a logical disk is shown under raid.
GOOD, WARN, CRIT = "good", "warning", "critical"
EXPECTED: dict[str, list[tuple[str, dict[str, Any], str, str]]] = {
    "cpu": [
        ("system.cpu.utilization", {}, "cpu", GOOD),
        ("system.cpu.load_average.1m", {}, "cpu", GOOD),
        ("system.cpu.load_average.5m", {}, "cpu", GOOD),
        ("system.cpu.load_average.15m", {}, "cpu", GOOD),
        ("system.cpu.frequency", {"cpu.logical_number": "3"}, "cpu", GOOD),
        ("observe.cpu.idle_residency", {"observe.cpu.idle_state": "C6"}, "cpu", GOOD)],
    "win_cpu": [("system.cpu.utilization", {}, "cpu", GOOD)],
    "memory": [
        ("system.memory.limit", {}, "memory", GOOD),
        ("system.memory.usage", {"system.memory.state": "used"}, "memory", GOOD),
        ("system.memory.usage", {"system.memory.state": "free"}, "memory", GOOD),
        ("system.paging.usage", {"system.paging.state": "used"}, "memory", GOOD),
        ("system.paging.usage", {"system.paging.state": "free"}, "memory", GOOD)],
    "win_memory": [
        ("system.memory.limit", {}, "memory", GOOD),
        ("observe.legacy.memory.commit_used", {}, "memory", GOOD),
        ("system.memory.usage", {"system.memory.state": "used"}, "memory", GOOD),
        ("system.memory.usage", {"system.memory.state": "free"}, "memory", GOOD)],
    "hwmon": [
        ("hw.temperature", {"hw.id": "coretemp:Package id 0"}, "temperatures", GOOD),
        ("hw.fan.speed", {"hw.id": "nct6775:fan1"}, "fans", GOOD),
        ("hw.voltage", {"hw.id": "nct6775:in0"}, "power", GOOD),
        ("hw.power", {"hw.id": "amdgpu:power1"}, "power", GOOD)],
    "rapl": [("hw.power", {"hw.id": "rapl:intel-rapl:0"}, "power", GOOD)],
    "rpi": [
        ("hw.temperature", {"hw.id": "soc"}, "temperatures", GOOD),
        ("observe.rpi.throttled", {"observe.rpi.flag": "under_voltage_now"}, "fans", GOOD),
        ("observe.rpi.throttled_raw", {}, "fans", GOOD)],
    "mdraid": [
        ("hw.status", {"hw.state": "clean"}, "raid", GOOD),
        ("observe.mdraid.sync_action", {"observe.mdraid.action": "idle"}, "raid", GOOD),
        ("observe.mdraid.sync_progress", {"hw.id": "md:md0"}, "raid", GOOD),
        ("observe.legacy.mdraid.degraded", {"array": "md0"}, "raid", GOOD)],
    "zfs": [("hw.status", {"hw.state": "ONLINE"}, "zfs", GOOD)],
    "truenas": [
        ("hw.status", {"hw.state": "ONLINE"}, "zfs", GOOD),
        ("observe.zfs.pool.health", {}, "zfs", WARN),
        ("hw.status", {"hw.state": "healthy"}, "zfs", GOOD),
        ("hw.status", {"hw.state": "warning"}, "zfs", GOOD),
        ("observe.zfs.pool.scan.errors", {}, "zfs", GOOD),
        ("observe.zfs.pool.scan.state", {}, "zfs", GOOD),
        ("hw.errors", {"error.type": "read", "hw.id": "zpool:tank/sda"}, "zfs", WARN),
        ("hw.errors", {"error.type": "write", "hw.id": "zpool:tank/sda"}, "zfs", GOOD),
        ("hw.errors", {"error.type": "checksum", "hw.id": "zpool:tank/sda"}, "zfs", WARN),
        ("hw.errors", {"error.type": "read", "hw.id": "zpool:tank"}, "zfs", WARN),
        ("observe.zfs.vdev.self_healed", {}, "zfs", GOOD),
        ("hw.temperature", {"hw.id": "disk:sda"}, "disks", GOOD)],
    "scrutiny": [
        ("observe.scrutiny.up", {}, "disks", GOOD),
        ("observe.scrutiny.up", {"error.type": "URLError"}, "disks", WARN),
        ("hw.status", {"hw.id": "disk:0x5000"}, "disks", GOOD),
        ("observe.legacy.scrutiny.temp", {"wwn": "0x5000"}, "disks", GOOD)],
    "nut": [
        ("hw.battery.charge", {}, "ups", GOOD),
        ("hw.battery.time_left", {}, "ups", GOOD),
        ("hw.voltage", {}, "ups", GOOD),
        ("observe.ups.load", {}, "ups", GOOD),
        ("observe.ups.status", {"observe.ups.flag": "OL"}, "ups", GOOD)],
    "thermalctl": [
        ("hw.temperature", {"observe.thermal.zone": "cpu"}, "temperatures", GOOD),
        ("observe.thermal.zone.load", {}, "fans", GOOD),
        ("observe.thermal.fan.duty", {}, "fans", GOOD),
        ("hw.fan.speed", {}, "fans", GOOD),
        ("observe.thermal.mode", {"observe.thermal.mode": "auto"}, "fans", GOOD)],
    "win_thermalsuite": [
        ("observe.thermal.failsafe", {}, "fans", WARN),
        ("hw.temperature", {"observe.thermal.zone": "cpu"}, "temperatures", GOOD),
        ("observe.thermal.zone.load", {}, "fans", GOOD),
        ("observe.thermal.zone.duty", {}, "fans", GOOD),
        ("observe.thermal.fan.duty", {}, "fans", GOOD),
        ("hw.fan.speed", {}, "fans", GOOD),
        ("observe.thermal.fan.target_duty", {}, "fans", GOOD),
        ("observe.thermal.mode", {"observe.thermal.mode": "auto"}, "fans", GOOD)],
    "win_smartctl": [
        ("hw.status", {"hw.state": "smart_ok"}, "disks", GOOD),
        ("hw.errors", {"error.type": "media"}, "disks", WARN),
        ("hw.physical_disk.endurance_utilization", {}, "disks", GOOD)],
    "win_storage": [
        ("hw.status", {"hw.id": "0"}, "disks", GOOD),
        ("hw.status", {"hw.id": "Pool1"}, "raid", GOOD),
        ("hw.status", {"hw.id": "VD1"}, "raid", WARN),
        ("observe.legacy.win_storage.temp", {"id": "0"}, "disks", GOOD)],
}


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def metrics_from_points(points: list[dict[str, Any]], host: str = HOST) -> dict[str, Any]:
    """The OTLP metrics request an agent builds from golden points, one metric per name and scope."""
    scopes: dict[str, dict[tuple[str, str, str], list[dict[str, Any]]]] = {}
    for p in points:
        series = scopes.setdefault(p["scope"], {}).setdefault((p["name"], p["unit"], p["kind"]), [])
        series.append(number(p["value"], p["ts"], p["attributes"]))
    built = {scope: [(total if kind == "sum" else gauge)(name, pts, unit)
                     for (name, unit, kind), pts in metrics.items()]
             for scope, metrics in scopes.items()}
    return metrics_request(host, built, os__type="linux", service__version="0.9.0",
                           observe__agent__sent_at=T0 + 5)


def logs_from_fixture(doc: dict[str, Any], host: str = HOST) -> dict[str, Any]:
    records = [log_record(rec["event_name"], rec["ts"], rec["body"], rec["severity_text"],
                          rec["severity_number"], **rec["attributes"]) for rec in doc["logs"]]
    return logs_request(host, records, scope="hostwatch.agent", os__type="linux",
                        service__version="0.9.0")


async def push(store: Store, metrics: dict[str, Any] | None = None,
               logs: dict[str, Any] | None = None, host: str = HOST) -> None:
    """Protobuf on the wire, then decode, normalize and ingest, as POST /v1/metrics and /v1/logs."""
    for request, message, fn in ((metrics, "MetricsRequest", normalize.normalize_metrics),
                                 (logs, "LogsRequest", normalize.normalize_logs)):
        if request is None:
            continue
        decoded = wire.decode(proto(request), message)
        result = fn(decoded, host, T0 + 10)
        assert result.rejects.count == 0, result.rejects.message()
        assert result.batch is not None
        await store.ingest_batch(result.batch, classify_events(result.batch.events), now=T0 + 10)


async def view(store: Store, host: str = HOST, monitor: Any = None,
               overrides: dict | None = None) -> dict[str, Any]:
    row = next(r for r in await store.host_rows() if r["host"] == host)
    return hostview.build_host_view(
        row, await store.latest_host(host), await store.host_sources(host),
        await store.host_events(host), NOW, 3600.0, monitor, overrides or {}, None)


def located(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(name, item) for name in hostview.SECTIONS for item in doc[name]["items"]]


def find(doc: dict[str, Any], scope: str, metric: str,
         attrs: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The readings of one collector and metric whose attributes include `attrs`; a row that
    names no attribute is the one with the fewest."""
    want = {k: str(v) for k, v in attrs.items()}
    hits = [(name, item) for name, item in located(doc)
            if item["source"] == collector_scope(scope) and item["metric"] == metric
            and all(item["labels"].get(k) == v for k, v in want.items())]
    fewest = min((len(item["labels"]) for _n, item in hits), default=0)
    return [(n, i) for n, i in hits if len(i["labels"]) == fewest]


def test_every_collector_fixture_has_an_expected_row():
    assert set(COLLECTORS) == set(EXPECTED)
    for name in COLLECTORS:
        assert len(EXPECTED[name]) == len(_fixture(name)["points"]), name


@pytest.mark.parametrize("name", COLLECTORS)
async def test_every_golden_point_lands_in_its_group_with_its_grade(name, tmp_path):
    doc = _fixture(name)
    store = Store(str(tmp_path / "w.db"))
    try:
        await push(store, metrics_from_points(doc["points"]))
        page = await view(store)
        for metric, attrs, section, grade in EXPECTED[name]:
            scope = doc["points"][0]["scope"].removeprefix(COLLECTOR_PREFIX)
            hits = find(page, scope, metric, attrs)
            assert len(hits) == 1, (name, metric, attrs, hits)
            got_section, item = hits[0]
            assert (got_section, item["status"]) == (section, grade), (name, metric, attrs, item)
        shown = len(located(page)) - sum(1 for _s, i in located(page)
                                         if i["metric"] == hostview.UTILIZATION)
        assert shown == len(doc["points"]), name
        assert all(not i["stale"] for _s, i in located(page))
    finally:
        store.close()


async def test_all_golden_points_of_one_host_make_a_complete_page(tmp_path):
    """The shared scopes (memory) overwrite each other, so one page needs a fixture per host; this
    checks that the page of the Linux fixtures together shows every section and the computed
    memory utilization, and grades the whole host from its worst reading."""
    store = Store(str(tmp_path / "w.db"))
    try:
        points: list[dict[str, Any]] = []
        for name in ("cpu", "memory", "hwmon", "rapl", "mdraid", "zfs", "nut", "thermalctl"):
            points += _fixture(name)["points"]
        await push(store, metrics_from_points(points))
        page = await view(store)
        for section in ("cpu", "memory", "power", "temperatures", "fans", "raid", "zfs", "ups"):
            assert page[section]["items"], section
            assert page[section]["status"] == GOOD, section
        used = next(i for i in page["memory"]["items"] if i["metric"] == hostview.UTILIZATION)
        assert used["value"] == pytest.approx(5000 / 8000) and used["status"] == GOOD
        assert page["status"] == GOOD
    finally:
        store.close()


async def test_a_ratio_is_graded_as_a_ratio(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    try:
        points = [dict(_fixture("cpu")["points"][0], value=v) for v in (0.95,)]
        await push(store, metrics_from_points(points))
        page = await view(store)
        item = page["cpu"]["items"][0]
        assert item["status"] == WARN and page["cpu"]["status"] == WARN
        points = [dict(_fixture("cpu")["points"][0], value=0.99)]
        await push(store, metrics_from_points([dict(points[0], ts=T0 + 1)]))
        assert (await view(store))["cpu"]["items"][0]["status"] == CRIT
    finally:
        store.close()


async def test_the_state_attribute_picks_the_grade(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    try:
        base = _fixture("zfs")["points"][0]
        degraded = dict(base, attributes=dict(base["attributes"], **{"hw.state": "DEGRADED"}))
        absent = dict(base, value=0.0, attributes=dict(base["attributes"], **{"hw.state": "FAULTED"}))
        other = dict(base, attributes=dict(base["attributes"], **{"hw.id": "zpool:other"}))
        await push(store, metrics_from_points([degraded, absent, other]))
        page = await view(store)
        by_state = {i["labels"]["hw.state"]: i["status"] for i in page["zfs"]["items"]}
        # A state point of 0 says the pool is not in that state, which claims nothing.
        assert by_state == {"DEGRADED": CRIT, "FAULTED": GOOD, "ONLINE": GOOD}
    finally:
        store.close()


def test_the_rules_are_keyed_by_opentelemetry_names_and_no_old_key_remains():
    old = {(s["source"], s["metric"]) for name in COLLECTORS for s in _fixture(name)["samples"]}
    seen_metrics = {p["name"] for name in COLLECTORS for p in _fixture(name)["points"]}
    keys = {key for table in hostview.RULES.values() for key in table}
    assert not (keys & old)
    hostwatch_keys = {k for k in keys if short_source(k[0]) not in OTHER_PRODUCERS
                      and not k[0].startswith(HA_SOC_PREFIX)}
    assert hostwatch_keys
    for scope, metric in hostwatch_keys:
        assert scope.startswith(COLLECTOR_PREFIX), (scope, metric)
        assert metric in seen_metrics, (scope, metric)


def test_the_old_hostwatch_names_are_gone_from_the_memory_extra_and_the_overrides():
    assert hostview.UTILIZATION == "system.memory.utilization"
    assert ("memory", "used_pct") not in {k for t in hostview.RULES.values() for k in t}


async def test_a_yaml_limit_on_an_opentelemetry_name_overrides_the_default(tmp_path):
    from observe.config import ComponentThresholds
    store = Store(str(tmp_path / "w.db"))
    try:
        await push(store, metrics_from_points(_fixture("hwmon")["points"]))
        scope = collector_scope("hwmon")
        limit = ComponentThresholds(source=scope, metric="hw.temperature", warn=40, crit=50)
        page = await view(store, overrides={(scope, "hw.temperature"): limit})
        assert page["temperatures"]["items"][0]["status"] == WARN
        with pytest.raises(ValueError):  # an old key would match nothing, so it is refused
            ComponentThresholds(source="hwmon", metric="temp", warn=40, crit=50)
    finally:
        store.close()


class Clock:
    def __call__(self) -> float:
        return NOW


def _monitor(*components: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"name": HOST, "type": "pushed_host", "host": HOST, "stale_after": 3600,
            "components": list(components), **extra}


async def _check(store: Store, monitor: dict[str, Any]) -> Any:
    cfg = make_config([monitor], defaults={"failures_to_down": 1, "timeout": 1})
    sched = Scheduler(cfg, store, Alerter(cfg))
    check = sched.checks[HOST]
    assert isinstance(check, PushedHostCheck)
    check.clock = Clock()
    return await check.probe()


async def test_pushed_host_components_use_opentelemetry_names(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    try:
        await push(store, metrics_from_points(
            _fixture("hwmon")["points"] + _fixture("cpu")["points"]))
        hot = {"source": collector_scope("hwmon"), "metric": "hw.temperature",
               "warn": 40, "crit": 50}
        busy = {"source": collector_scope("cpu"), "metric": "system.cpu.utilization",
                "warn": 0.1, "crit": 0.5}
        res = await _check(store, _monitor(hot, busy))
        assert res.detail["components"] == {"hwmon.hw.temperature": "warning",
                                            "cpu.system.cpu.utilization": "warning"}
        assert res.result.value == "warn"
        crit = await _check(store, _monitor(dict(hot, crit=45)))
        assert crit.detail["components"] == {"hwmon.hw.temperature": "critical"}
        assert crit.result.value == "fail"
        # The agent's source status names the collector id, which require_sources names too.
        sources = metrics_request(HOST, {"hostwatch.agent": [
            gauge("observe.source.available", [number(0.0, T0, {"observe.source": "nut",
                  "observe.source.reason": "no access"})]),
            gauge("observe.source.present", [number(1.0, T0, {"observe.source": "nut"})])]})
        await push(store, sources)
        needed = await _check(store, _monitor(hot, require_sources=["nut"]))
        assert needed.detail["components"]["nut"] == "warning"
    finally:
        store.close()


async def test_boot_events_are_classified_and_a_power_loss_stays_critical(tmp_path):
    doc = _fixture("events")
    store = Store(str(tmp_path / "w.db"))
    try:
        first = dict(doc, logs=[doc["logs"][0]])
        second = dict(doc, logs=[dict(doc["logs"][1], ts=T0 + 1)])
        await push(store, logs=logs_from_fixture(first))
        row = next(r for r in await store.host_rows() if r["host"] == HOST)
        assert (row["boot_id"], row["clean_shutdown"]) == ("b1", 1)
        res = await _check(store, _monitor(crash_hold_s=3600))
        assert res.detail["components"] == {}  # a clean boot raises nothing
        await push(store, logs=logs_from_fixture(second))
        row = next(r for r in await store.host_rows() if r["host"] == HOST)
        assert (row["boot_id"], row["clean_shutdown"]) == ("b2", 0)
        events = {e["boot_id"]: e for e in await store.host_events(HOST) if e["boot_id"]}
        assert events["b1"]["kind"] == "boot.clean_shutdown"
        assert events["b1"]["detail"]["classification"] == "clean"
        assert events["b2"]["kind"] == "boot.power_loss"
        assert events["b2"]["detail"]["classification"] == "crash"
        assert events["b2"]["severity"] == "critical"  # sent as WARN, kept as critical
        crash = await _check(store, _monitor(crash_hold_s=10_000_000, crash_result="fail"))
        assert crash.detail["components"]["boot"] == "critical"
        assert crash.result.value == "fail"
    finally:
        store.close()


async def test_golden_events_reach_the_host_page_with_their_kind_and_severity(tmp_path):
    doc = _fixture("events")
    store = Store(str(tmp_path / "w.db"))
    try:
        await push(store, logs=logs_from_fixture(doc))
        page = await view(store)
        by_title = {e["title"]: (e["kind"], e["severity"]) for e in page["events"]}
        assert len(page["events"]) == len(doc["events"])
        for want in doc["events"]:
            assert by_title[want["title"]] == (want["kind"], want["severity"]), want["title"]
        md = next(e for e in page["events"] if e["kind"] == "md.degraded")
        assert md["detail"]["message"] == "md/raid1:md0: Disk failure"
        assert md["detail"]["cursor"] == "s=1"
        assert all(not k.startswith("observe.") for e in page["events"] for k in e["detail"]
                   if k != "classification")
        # A critical event is an alert on the page even though it was sent as ERROR, WARN or INFO.
        assert page["alerts"]["status"] == CRIT
    finally:
        store.close()


async def test_threshold_rules_name_the_opentelemetry_metric(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    cfg = make_config([_monitor()], defaults={"failures_to_down": 1, "timeout": 1})
    sched = Scheduler(cfg, store, Alerter(cfg))
    try:
        sched.apply_rules(rules.validate([{
            "id": "hot", "kind": "consecutive", "metric": "hw.temperature",
            "condition": "above", "crit": 40, "x": 1, "host": HOST}]))
        points = _fixture("hwmon")["points"] + _fixture("thermalctl")["points"]
        await push(store, metrics_from_points(points))
        result = normalize.normalize_metrics(
            wire.decode(proto(metrics_from_points(points)), "MetricsRequest"), HOST, T0 + 10)
        await sched.observe_pushed(HOST, result.batch.samples, T0 + 10)
        assert sched.rules.worst(HOST)[0] == rules.CRITICAL
        # Two collectors send hw.temperature: each keeps its own series state.
        keys = {k for k in sched.rules._meta}
        assert len(keys) == 2
        assert rule_metric(collector_scope("hwmon"), "hw.temperature") == "hw.temperature"
        assert rule_metric("hassio", "disk_used_pct") == "hassio.disk_used_pct"
    finally:
        store.close()


def _source_change(source: str, available: bool, reason: str, ts: float) -> dict[str, Any]:
    """The log record the agent builds in hostwatch/otel_map.py map_source_change."""
    body = f"{source} available" if available else \
        f"{source} unavailable" + (f": {reason}" if reason else "")
    attrs: dict[str, Any] = {"observe__source": source}
    if reason:
        attrs["observe__source__reason"] = reason
    return log_record("observe.source.change", ts, body, "WARN", 13, **attrs)


async def test_a_source_change_log_sets_the_reason_shown_on_the_host_page(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    try:
        await push(store, metrics_from_points(_fixture("zfs")["points"]))
        down = logs_request(HOST, [_source_change("zfs", False, "permission denied", T0 + 20)],
                            scope="hostwatch.agent", os__type="linux")
        await push(store, logs=down)
        page = await view(store)
        zfs = next(s for s in page["sources"] if s["source"] == "zfs")
        assert (zfs["available"], zfs["reason"], zfs["status"]) == (False, "permission denied", WARN)
        assert page["zfs"]["state"] == "unavailable"
        assert "zfs: permission denied" in page["zfs"]["note"]
        assert [e["kind"] for e in page["events"]] == ["observe.source.change"]
        assert "observe.source.reason" not in page["events"][0]["detail"]
        up = logs_request(HOST, [_source_change("zfs", True, "", T0 + 40)],
                          scope="hostwatch.agent", os__type="linux")
        await push(store, logs=up)
        zfs = next(s for s in (await view(store))["sources"] if s["source"] == "zfs")
        assert (zfs["available"], zfs["reason"]) == (True, "")
    finally:
        store.close()


async def test_every_boot_kind_is_classified_from_the_attribute_and_keeps_its_severity(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    try:
        expected = {"boot.clean_shutdown": ("clean", 1), "boot.kernel_panic": ("crash", 0),
                    "boot.watchdog_reset": ("crash", 0), "boot.power_loss": ("crash", 0),
                    "boot.agent_stopped": ("unknown", None), "boot.novel_kind": ("unknown", None)}
        for i, (kind, (cls, flag)) in enumerate(expected.items()):
            rec = log_record("observe.host.boot", T0 + i, "booted", "WARN", 13,
                             observe__event__kind=kind, observe__boot_id=f"b{i}",
                             observe__dedup_key=f"boot:b{i}", observe__severity="critical"
                             if cls == "crash" else "info",
                             observe__host__clean_shutdown=cls == "clean")
            await push(store, logs=logs_request(HOST, [rec], scope="hostwatch.agent"))
            row = next(r for r in await store.host_rows() if r["host"] == HOST)
            assert (row["boot_id"], row["clean_shutdown"]) == (f"b{i}", flag), kind
            ev = next(e for e in await store.host_events(HOST) if e["boot_id"] == f"b{i}")
            assert (ev["kind"], ev["detail"]["classification"]) == (kind, cls)
            assert ev["severity"] == ("critical" if cls == "crash" else "info"), kind
    finally:
        store.close()
