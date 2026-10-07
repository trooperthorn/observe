"""The SNMP poller stores its readings under the OpenTelemetry names of docs/DATA-API-DESIGN.md
section 3.5: scope `observe.check.snmp`, ratios from 0 to 1, bytes as `By` and rates as `bit/s`."""

from __future__ import annotations

import pathlib
import re

import pytest

from observe import hostview
from observe.checks import snmp as snmpmod
from observe.checks.base import CheckResult
from observe.checks.host import CRITICAL, GOOD, WARNING

from .test_recheck import storage  # noqa: F401

NOW = 1_700_000_000.0
OBSERVE = pathlib.Path(__file__).resolve().parent.parent / "observe"


def points(batch):
    return {(s.metric, tuple(sorted(s.labels.items()))): (s.value, s.unit) for s in batch.samples}


def test_golden_cpu_poll():
    res = CheckResult.ok("cpu", value=40.0, unit="%", detail={"per_core": [10, 70]})
    b = snmpmod.host_batch("h", "cpu", res, NOW)
    assert {s.source for s in b.samples} == {"observe.check.snmp"}
    assert [x.source for x in b.sources] == ["observe.check.snmp"]
    assert points(b) == {
        ("system.cpu.utilization", ()): (0.4, "1"),
        ("system.cpu.utilization", (("cpu.logical_number", "0"),)): (0.1, "1"),
        ("system.cpu.utilization", (("cpu.logical_number", "1"),)): (0.7, "1")}


def test_golden_memory_poll():
    res = CheckResult.ok("mem", value=25.0, unit="%",
                         detail={"total_bytes": 8000, "used_bytes": 2000})
    assert points(snmpmod.host_batch("h", "memory", res, NOW)) == {
        ("system.memory.utilization", ()): (0.25, "1"),
        ("system.memory.limit", ()): (8000.0, "By"),
        ("system.memory.usage", (("system.memory.state", "used"),)): (2000.0, "By")}


def test_golden_storage_poll():
    disk = {"mount": "/data", "total_bytes": 1000, "used_bytes": 900, "used_pct": 90.0}
    res = CheckResult.ok("disk", value=90.0, unit="%", detail={"disks": [disk]})
    mp = ("system.filesystem.mountpoint", "/data")
    assert points(snmpmod.host_batch("h", "storage", res, NOW)) == {
        ("system.filesystem.utilization", (mp,)): (0.9, "1"),
        ("system.filesystem.usage", (mp, ("system.filesystem.state", "used"))): (900.0, "By"),
        ("system.filesystem.usage", (mp, ("system.filesystem.state", "free"))): (100.0, "By")}


def test_golden_interface_poll():
    detail = {"ifIndex": "2", "name": "eth0", "oper_status": "up", "in_bps": 8000,
              "out_bps": 4000, "speed_mbps": 1000}
    res = CheckResult.ok("if", value=0.8, unit="%", detail=detail)
    nic = ("network.interface.name", "eth0")
    got = points(snmpmod.host_batch("h", "interface", res, NOW))
    assert got == {
        ("observe.network.interface.up", (nic, ("observe.network.interface.status", "up"))):
            (1.0, "1"),
        ("observe.network.interface.rate", (nic, ("network.io.direction", "receive"))):
            (8000.0, "bit/s"),
        ("observe.network.interface.rate", (nic, ("network.io.direction", "transmit"))):
            (4000.0, "bit/s"),
        ("observe.network.interface.speed", (nic,)): (1e9, "bit/s"),
        ("observe.network.interface.utilization", (nic,)): (0.008, "1")}


# (old percent limits, section, metric, new ratio limits). The same values must grade the same.
CASES = [("cpu", "system.cpu.utilization", (90, 98)),
         ("memory", "system.memory.utilization", (90, 97)),
         ("disks", "system.filesystem.utilization", (85, 95)),
         ("network", "observe.network.interface.utilization", (70, 90))]


def old_grade(percent, warn, crit):
    return CRITICAL if percent >= crit else WARNING if percent >= warn else GOOD


@pytest.mark.parametrize("section,metric,limits", CASES)
@pytest.mark.parametrize("percent", [0.0, 12.5, 69.9, 70.0, 84.9, 85.0, 89.9, 90.0, 94.9, 95.0,
                                     96.9, 97.0, 97.9, 98.0, 100.0])
def test_a_ratio_grades_like_the_old_percent(section, metric, limits, percent):
    grader = hostview.RULES[section][(hostview.SNMP, metric)]
    ratio = snmpmod._ratio(percent)
    assert grader(ratio, {})[0] == old_grade(percent, *limits)


def test_one_core_is_shown_not_graded():
    grader = hostview.RULES["cpu"][(hostview.SNMP, "system.cpu.utilization")]
    assert grader(1.0, {"cpu.logical_number": "0"})[0] == GOOD
    assert grader(1.0, {})[0] == CRITICAL


def test_no_old_snmp_key_remains():
    old = r"cpu_pct|cpu_core_pct|mem_used_pct|mem_total_bytes|mem_used_bytes|disk_used_pct|" \
          r"disk_total_bytes|disk_used_bytes|if_in_bps|if_out_bps|if_speed_mbps|if_util_pct|if_up"
    for path in (OBSERVE / "checks" / "snmp.py", OBSERVE / "hostview.py"):
        text = path.read_text(encoding="utf-8")
        # hassio keeps disk_used_pct, so only a line that is not a hassio key counts
        lines = [ln for ln in text.splitlines()
                 if not (path.name == "hostview.py" and '("hassio", "disk_used_pct")' in ln)]
        hits = re.findall(old, "\n".join(lines))
        assert not hits, (path.name, hits)
    assert not [k for t in hostview.RULES.values() for k in t if k[0] == "snmp"]


# ---- configuration and data written before the OpenTelemetry names must not read Good ----------

OLD_SNMP = ["cpu_pct", "cpu_core_pct", "mem_used_pct", "mem_total_bytes", "mem_used_bytes",
            "disk_used_pct", "disk_total_bytes", "disk_used_bytes", "if_up", "if_in_bps",
            "if_out_bps", "if_speed_mbps", "if_util_pct"]


def test_a_component_on_the_old_snmp_source_is_refused_with_the_new_scope():
    from pydantic import ValidationError
    from observe.config import ComponentThresholds
    with pytest.raises(ValidationError) as err:
        ComponentThresholds(source="snmp", metric="cpu_pct", warn=90, crit=98)
    assert "observe.check.snmp" in str(err.value)
    assert "hostwatch.collector.snmp" not in str(err.value)
    ok = ComponentThresholds(source="observe.check.snmp", metric="system.cpu.utilization",
                             warn=0.9, crit=0.98)
    assert ok.source == "observe.check.snmp"


@pytest.mark.parametrize("name", OLD_SNMP)
def test_an_old_snmp_rule_metric_is_recognised_and_names_its_replacement(name):
    from observe.otelnames import legacy_rule_advice, legacy_rule_metric
    assert legacy_rule_metric(f"snmp.{name}")
    advice = legacy_rule_advice(f"snmp.{name}")
    assert advice and "section 3.2" not in advice  # a named replacement, not the generic text


def test_the_new_snmp_rule_metric_is_not_legacy():
    from observe.otelnames import legacy_rule_metric, rule_metric
    name = rule_metric("observe.check.snmp", "system.cpu.utilization")
    assert name == "observe.check.snmp.system.cpu.utilization"
    assert not legacy_rule_metric(name)


def test_saving_an_old_snmp_rule_is_refused():
    from observe import rules
    bad = [{"id": "r1", "kind": "consecutive", "metric": "snmp.cpu_pct",
            "condition": "above", "crit": 90, "x": 1}]
    with pytest.raises(rules.RuleError):
        rules.validate(bad, refuse_legacy=True)


async def test_loading_an_old_snmp_rule_warns(storage, caplog):  # noqa: F811
    import logging
    from observe import rules
    from .test_recheck import Env, T0
    env = Env(storage, [])
    parsed = rules.validate([{"id": "r1", "kind": "consecutive", "metric": "snmp.cpu_pct",
                              "condition": "above", "crit": 90, "x": 1}])
    await storage.write(lambda db: rules.save(db, parsed, now=T0, actor="t", remote=""))
    with caplog.at_level(logging.WARNING):
        await env.sched.load_rules()
    assert "snmp.cpu_pct" in caplog.text


def test_the_old_snmp_source_row_is_not_listed():
    sources = {"snmp": {"available": True, "reason": "", "updated": NOW - 9999},
               "observe.check.snmp": {"available": True, "reason": "", "updated": NOW}}
    views = hostview.source_views(sources, NOW, 300)
    assert [v["source"] for v in views] == ["observe.check.snmp"]
    assert views[0]["status"] == GOOD


@pytest.mark.parametrize("percent", [89.99996, 69.99999, 84.999999, 97.00001])
def test_a_percent_next_to_a_limit_does_not_round_onto_it(percent):
    grader = hostview.RULES["cpu"][(hostview.SNMP, "system.cpu.utilization")]
    assert grader(snmpmod._ratio(percent), {})[0] == old_grade(percent, 90, 98)
