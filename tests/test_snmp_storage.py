"""The SNMP storage mode, and SNMP readings on a host page. No agent is contacted: the net-snmp
walk and get calls are replaced by rows from tests/fixtures/snmp/ha-probe-walk.json, which is
shaped from ha_Int_soc docs/SNMPV3.md (descriptions and unit sizes are unverified)."""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any

import pytest

from observe.checks import build_check
from observe.checks import snmp as snmpmod
from observe.checks.base import Result
from observe.config import Config
from observe.store import Store

from .test_host_views import Env, detail, reading

FIX = json.loads((pathlib.Path(__file__).parent / "fixtures" / "snmp"
                  / "ha-probe-walk.json").read_text())
COLUMNS = {
    snmpmod.HR_STORAGE_TYPE: "hrStorageType", snmpmod.HR_STORAGE_DESCR: "hrStorageDescr",
    snmpmod.HR_STORAGE_UNITS: "hrStorageAllocationUnits", snmpmod.HR_STORAGE_SIZE: "hrStorageSize",
    snmpmod.HR_STORAGE_USED: "hrStorageUsed", snmpmod.HR_PROCESSOR_LOAD: "hrProcessorLoad",
    snmpmod.IF_OPER_STATUS: "ifOperStatus", snmpmod.IF_HC_IN_OCTETS: "ifHCInOctets",
    snmpmod.IF_HC_OUT_OCTETS: "ifHCOutOctets", snmpmod.IF_HIGH_SPEED: "ifHighSpeed",
    snmpmod.IF_NAME: "ifName",
}
GIB = 2 ** 30
CREDS = {"v2": {"type": "snmpv2c", "community": "c"}}


class FakeAgent:
    """Stands in for the snmpget and snmpwalk calls of one check."""

    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fx = fixture

    async def walk(self, oid: str) -> dict[str, str]:
        return dict(self.fx.get(COLUMNS[oid], {}))

    async def get(self, *oids: str) -> dict[str, str]:
        out = {}
        for oid in oids:
            for col, key in COLUMNS.items():
                if oid.startswith(col + "."):
                    v = self.fx.get(key, {}).get(oid[len(col) + 1:])
                    if v is not None:
                        out[oid] = v
        return out


def wire(cfg: Config, index: int, store: Store | None = None, fixture: dict | None = None):
    chk = build_check(cfg.monitors[index], cfg, store)
    agent = FakeAgent(FIX if fixture is None else fixture)
    chk.walk, chk.get = agent.walk, agent.get  # type: ignore[method-assign]
    return chk


def make(mode: str, fixture: dict | None = None, **mon: Any):
    cfg = Config.model_validate({
        "credentials": CREDS,
        "monitors": [{"name": f"ha {mode}", "type": "snmp", "host": "127.0.0.1",
                      "credential": "v2", "mode": mode, **mon}]})
    return wire(cfg, 0, None, fixture)


def test_storage_bytes_is_units_times_count():
    assert snmpmod.storage_bytes("4096", "7864320", "1572864") == (4096 * 7864320, 4096 * 1572864)
    # A raised allocation unit keeps a large disk representable: 65536 * 3815470 is about 233 GiB.
    total, used = snmpmod.storage_bytes("65536", "3815470", "3434923")
    assert total == 65536 * 3815470 and total / GIB == pytest.approx(232.9, abs=0.1)
    assert used / total == pytest.approx(0.9, abs=0.001)
    for bad in (("0", "1", "1"), ("4096", "-1", "0"), ("4096", None, "0"), ("x", "1", "1")):
        with pytest.raises(ValueError):
            snmpmod.storage_bytes(*bad)


async def test_storage_mode_reads_fixed_disks_only():
    res = await make("storage").run()
    assert res.result is Result.OK, res.message
    disks = {d["mount"]: d for d in res.detail["disks"]}
    assert set(disks) == {"/", "/data"}  # RAM, swap and the zero-size /backup are not disks
    assert disks["/"]["total_bytes"] == 4096 * 7864320
    assert disks["/"]["used_pct"] == 20.0
    assert disks["/data"]["total_bytes"] == 65536 * 3815470
    assert disks["/data"]["used_pct"] == 90.0
    assert res.value == 90.0 and "/data" in res.message


async def test_storage_mount_filter_and_missing_mount():
    res = await make("storage", mount="/").run()
    assert res.result is Result.OK and res.value == 20.0 and len(res.detail["disks"]) == 1
    res = await make("storage", mount="/nope").run()
    assert res.result is Result.FAIL and "/nope" in res.message


async def test_storage_threshold_applies_to_the_fullest_disk():
    res = await make("storage", thresholds={"direction": "above", "warn": 80, "crit": 95}).run()
    assert res.result is Result.WARN


async def test_storage_without_fixed_disks_fails_plainly():
    fx = {**FIX, "hrStorageType": {"1": snmpmod.HR_STORAGE_RAM}}
    res = await make("storage", fixture=fx).run()
    assert res.result is Result.FAIL and "no fixed disk" in res.message


def test_host_name_needs_a_reading_mode():
    with pytest.raises(ValueError, match="host_name"):
        make("uptime", host_name="ha")


# ------------------------------------------------------------ the host page


def snmp_monitors(host_name: str = "homeassistant") -> list[dict[str, Any]]:
    base = {"type": "snmp", "host": "127.0.0.1", "credential": "v2", "host_name": host_name}
    return [{**base, "name": "ha cpu", "mode": "cpu"},
            {**base, "name": "ha mem", "mode": "memory"},
            {**base, "name": "ha disks", "mode": "storage"},
            {**base, "name": "ha eth0", "mode": "interface", "interface": "2"}]


def poll_cfg(host_name: str) -> Config:
    return Config.model_validate({"credentials": CREDS, "monitors": snmp_monitors(host_name)})


def test_host_page_shows_cpu_memory_disks_and_interface(tmp_path):
    env = Env(tmp_path, snmp_monitors())
    try:
        cfg = poll_cfg("homeassistant")
        for i, m in enumerate(cfg.monitors):
            chk = wire(cfg, i, env.store)
            chk.clock = lambda: env.clock.now - 5
            res = asyncio.run(chk.run())
            assert res.result is Result.OK, (m.mode, res.message)
        env.login()
        d = detail(env, "homeassistant")
        assert d["heard"] and d["platform"] == "snmp" and not d["stale"]
        cpu = [i for i in d["cpu"]["items"] if i["metric"] == "system.cpu.utilization"]
        whole = next(i for i in cpu if "cpu.logical_number" not in i["labels"])
        assert whole["source"] == snmpmod.SCOPE and whole["unit"] == "1"
        assert whole["value"] == 0.4 and whole["status"] == "good"
        assert reading(d, "cpu", "system.cpu.utilization",
                       {"cpu.logical_number": "3"})["value"] == 0.7
        assert reading(d, "memory", "system.memory.utilization")["value"] == 0.25
        assert reading(d, "memory", "system.memory.limit")["value"] == 8000000 * 1024
        mp = "system.filesystem.mountpoint"
        root = reading(d, "disks", "system.filesystem.utilization", {mp: "/"})
        data = reading(d, "disks", "system.filesystem.utilization", {mp: "/data"})
        assert root["value"] == 0.2 and root["status"] == "good"
        assert data["value"] == 0.9 and data["status"] == "warning"
        used = reading(d, "disks", "system.filesystem.usage",
                       {mp: "/data", "system.filesystem.state": "used"})
        free = reading(d, "disks", "system.filesystem.usage",
                       {mp: "/data", "system.filesystem.state": "free"})
        assert used["value"] + free["value"] == 65536 * 3815470 and used["unit"] == "By"
        nic = {"network.interface.name": "2"}
        assert reading(d, "network", "observe.network.interface.up", nic)["value"] == 1.0
        assert reading(d, "network", "observe.network.interface.speed", nic)["value"] == 1e9
        assert d["disks"]["status"] == "warning" and d["status"] == "warning"
        listed = {h["host"]: h for h in env.client.get("/api/v2/hosts").json()["items"]}
        assert listed["homeassistant"]["sections"]["disks"] == "warning"
    finally:
        env.close()


def test_snmp_host_is_listed_before_any_poll_and_a_failed_poll_stores_nothing(tmp_path):
    env = Env(tmp_path, snmp_monitors("ha-probe"))
    try:
        env.login()
        listed = {h["host"]: h for h in env.client.get("/api/v2/hosts").json()["items"]}
        assert listed["ha-probe"]["heard"] is False
        chk = wire(poll_cfg("ha-probe"), 0, env.store, fixture={})
        assert asyncio.run(chk.run()).result is Result.FAIL
        assert asyncio.run(env.store.latest_host("ha-probe")) is None
    finally:
        env.close()


def test_a_down_interface_is_a_warning_not_zero_traffic(tmp_path):
    env = Env(tmp_path, snmp_monitors())
    try:
        chk = wire(poll_cfg("homeassistant"), 3, env.store, fixture={**FIX, "ifOperStatus": {"2": "2"}})
        chk.clock = lambda: env.clock.now - 5
        assert asyncio.run(chk.run()).result is Result.FAIL
        env.login()
        d = detail(env, "homeassistant")
        item = reading(d, "network", "observe.network.interface.up", {"network.interface.name": "2"})
        assert item["value"] == 0.0 and item["status"] == "warning"
        assert not any(i["metric"] == "observe.network.interface.rate"
                       for i in d["network"]["items"])
    finally:
        env.close()
