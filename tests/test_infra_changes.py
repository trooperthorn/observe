"""Field change findings: what each kind needs, that they reach the dashboard, port page and map,
and that nothing here ever calls an alert target."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.checks.base import CheckResult
from observe.infra import InfraService
from observe.infra_changes import port_changes
from observe.infra_match import Matcher
from observe.portkey import switch_id
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

SID = switch_id("aa:bb:cc:dd:ee:03")
PASSWORD = "correct horse battery"
STATIC = Path(__file__).parent.parent / "observe" / "static"
JACK = "HQ/Main/R1/PP/01"
MONITORS = [
    {"name": "edge sw", "type": "ping", "host": "10.0.0.3"},
    {"name": "edge gi5", "type": "snmp", "host": "10.0.0.3", "credential": "v2",
     "mode": "interface", "interface": "GigabitEthernet1/0/5"},
]

# (kind, severity, property, earlier value, newest value): every kind of finding.
FIRES = [
    ("speed_drop", "warning", "link_speed_mbps", 1000, 100),
    ("cable_fault", "warning", "pair_fault", "none", "pair 3-6 open"),
    ("cable_fault", "warning", "pair_fault", "pair 1-2 short", "pair 4-5 open"),
    ("length_change", "warning", "pair_1_2_length_m", 42.5, 60.0),
    ("length_change", "warning", "pair_7_8_length_m", 90.0, 41.0),
    ("poe_drop", "warning", "poe_class", "4", "2"),
    ("poe_drop", "warning", "poe_load_w", 12.0, 3.0),
    ("vlan_change", "info", "vlan", 20, 30),
    ("vlan_change", "info", "voice_vlan", 30, 31),
    ("dhcp_fail", "warning", "dhcp_ok", True, False),
    ("verdict_worse", "warning", "cable_verdict", "pass", "warn"),
    ("verdict_worse", "warning", "cable_verdict", "warn", "fail"),
]
# The neighbouring cases that must stay quiet.
QUIET = [
    ("link_speed_mbps", 100, 1000),  # faster
    ("link_speed_mbps", 1000, 1000),  # unchanged
    ("pair_fault", "pair 3-6 open", "none"),  # fixed
    ("pair_fault", "pair 3-6 open", "pair 3-6 open"),
    ("pair_1_2_length_m", 42.5, 44.0),  # inside the tolerance
    ("poe_class", "2", "4"),
    ("poe_load_w", 12.0, 8.0),  # above half
    ("poe_load_w", 0.0, 0.0),
    ("vlan", 20, 20),
    ("dhcp_ok", False, True),
    ("dhcp_ok", False, False),
    ("cable_verdict", "fail", "pass"),
    ("cable_verdict", "pass", "unknown"),  # unknown has no rank
]


@pytest.mark.parametrize("kind,severity,name,old,new", FIRES)
def test_each_kind_fires_from_the_last_two_values(kind, severity, name, old, new):
    got = port_changes({name: old}, {name: new})
    assert [(k, s) for k, s, _ in got] == [(kind, severity)]
    assert got[0][2].endswith(".")


@pytest.mark.parametrize("name,old,new", QUIET)
def test_improvements_noise_and_repeats_produce_nothing(name, old, new):
    assert port_changes({name: old}, {name: new}) == []


def test_a_property_with_one_value_is_a_baseline_not_a_change():
    assert port_changes({}, {"link_speed_mbps": 10}) == []
    assert port_changes({"link_speed_mbps": 1000}, {}) == []


def test_one_finding_per_kind_joins_several_facts():
    got = port_changes({"pair_1_2_length_m": 10.0, "pair_3_6_length_m": 10.0,
                        "poe_class": "4", "poe_load_w": 20.0},
                       {"pair_1_2_length_m": 40.0, "pair_3_6_length_m": 50.0,
                        "poe_class": "2", "poe_load_w": 1.0})
    assert [k for k, _, _ in got] == ["length_change", "poe_drop"]
    assert "1-2" in got[0][2] and "3-6" in got[0][2]
    assert "class" in got[1][2] and "load" in got[1][2]


class Web:
    def __init__(self, tmp_path: Any) -> None:
        path = str(tmp_path / "w.db")
        self.store = Store(path)
        self.cfg = make_config(MONITORS, credentials={
            "v2": {"type": "snmpv2c", "community": "c"}},
            server={"db_path": path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
                    "argon2_parallelism": 1})
        self.alerter = Alerter(self.cfg)
        self.sched = Scheduler(self.cfg, self.store, self.alerter)
        self.client = TestClient(create_app(self.cfg, self.store, self.sched, self.alerter),
                                 base_url="https://testserver")
        self.infra = InfraService(self.store)
        self.clock = 100.0

    async def login(self, name: str = "bob", admin: bool = False) -> dict[str, str]:
        await auth.create_user(self.store, self.cfg, name, PASSWORD, admin)
        r = self.client.post("/api/login", json={"username": name, "password": PASSWORD})
        return {"X-CSRF-Token": r.json()["csrf"]}

    async def seed(self, rows: list[tuple[str, Any]]) -> None:
        await self.infra.upsert_switch(SID, name="edge", mgmt_addresses=["10.0.0.3"], now=1.0)
        await self.infra.upsert_port(SID, "Gi1/0/5", if_index=3, now=1.0)
        await self.infra.upsert_jack(JACK, room="R1", switch=SID, port="Gi1/0/5", now=1.0)
        await self.infra.upsert_link(self.infra.jack_ref(JACK),
                                     self.infra.port_ref(SID, "Gi1/0/5"), source="lldp",
                                     confidence=0.9, now=self.clock)
        for name, value in rows:
            self.clock += 10
            await self.infra.append_property(
                SID, "Gi1/0/5", name, value, source="field", report_id=f"r{self.clock}",
                observed_at=self.clock, now=self.clock)
        self.sched.states["edge-gi5"].observe(CheckResult.ok("up", detail={"speed_mbps": 1000}))

    def port(self) -> dict[str, Any]:
        r = self.client.get("/api/infra/port", params={"switch_id": SID, "port": "Gi1/0/5"})
        assert r.status_code == 200, r.text
        return r.json()


async def map_state(web: Web) -> str:
    """The port's state on the map. The live columns are refreshed by the map hook, which the
    test runs here instead of waiting for the scheduler; the GET itself never rebuilds."""
    await web.client.app.state.mapper.rebuild()
    nodes = {n["id"]: n for n in web.client.get("/api/infra/map").json()["nodes"]}
    return nodes[f"port:{SID}|gi1/0/5"]["state"]


@pytest.fixture
def web(tmp_path):
    w = Web(tmp_path)
    yield w
    w.client.close()
    w.store.close()


async def test_findings_reach_dashboard_port_page_and_map_without_any_alert(web, monkeypatch):
    sent: list[Any] = []

    async def no_alerts(*a: Any, **k: Any) -> None:
        sent.append((a, k))
        raise AssertionError("a field finding must never call an alert target")
    monkeypatch.setattr(Alerter, "notify", no_alerts)
    await web.seed([("link_speed_mbps", 1000), ("link_speed_mbps", 100),
                    ("vlan", 20), ("vlan", 30)])
    assert web.client.get("/api/infra/findings").status_code == 401
    await web.login()

    listed = web.client.get("/api/infra/findings").json()["findings"]
    assert {(f["kind"], f["severity"]) for f in listed} == {("speed_drop", "warning"),
                                                            ("vlan_change", "info")}
    port = web.port()
    assert {f["kind"] for f in port["findings"]} == {"speed_drop", "vlan_change"}
    assert port["state"] == "warn"  # the live check passes, the field finding turns it to warning
    await web.client.app.state.mapper.rebuild()
    nodes = {n["id"]: n for n in web.client.get("/api/infra/map").json()["nodes"]}
    node = nodes[f"port:{SID}|gi1/0/5"]
    assert node["state"] == "warn"
    assert set(node["findings"]) == {"speed_drop", "vlan_change"}
    assert sent == [] and web.alerter.status == {}


async def test_an_info_finding_alone_does_not_turn_a_passing_port_to_warning(web):
    await web.seed([("vlan", 20), ("vlan", 30)])
    await web.login()
    port = web.port()
    assert [f["kind"] for f in port["findings"]] == ["vlan_change"]
    assert port["state"] == "up" and await map_state(web) == "up"


async def test_a_later_report_that_restores_the_value_clears_the_finding(web):
    await web.seed([("dhcp_ok", True), ("dhcp_ok", False)])
    matcher = Matcher(web.cfg, web.infra)
    assert [f.kind for f in await matcher.findings(lambda m: None)] == ["dhcp_fail"]
    await web.seed([("dhcp_ok", True)])
    assert await matcher.findings(lambda m: None) == []


async def test_a_change_finding_can_be_acknowledged_and_returns_when_facts_change(web):
    await web.seed([("link_speed_mbps", 1000), ("link_speed_mbps", 100)])
    h = await web.login("root", admin=True)
    body = {"switch_id": SID, "port_key": "gi1/0/5", "kind": "speed_drop"}
    assert web.client.post("/api/admin/infra/findings/ack", json=body,
                           headers=h).status_code == 200
    port = web.port()
    assert port["findings"][0]["acknowledged"] and port["state"] == "up"
    assert await map_state(web) == "up"  # the map agrees with the port page
    await web.seed([("link_speed_mbps", 10)])
    port = web.port()
    assert not port["findings"][0]["acknowledged"] and port["state"] == "warn"
    assert await map_state(web) == "warn"


def test_dashboard_script_lists_findings_and_the_plugin_nav_with_text_only():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    shell = (STATIC / "js" / "shell.js").read_text(encoding="utf-8")
    assert "/api/infra/findings" in js and "/api/plugins" in shell
    assert 'id="findings"' in html and 'id="shell-nav"' in html
    assert not re.search(r"\.innerHTML\s*=|insertAdjacentHTML|document\.write|eval\(", js)
