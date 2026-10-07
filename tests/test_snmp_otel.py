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
        hits = [m for m in re.findall(old, text)
                if not (path.name == "hostview.py" and m == "disk_used_pct")]  # hassio keeps it
        assert not hits, (path.name, hits)
    assert not [k for t in hostview.RULES.values() for k in t if k[0] == "snmp"]
