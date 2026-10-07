"""Monitor matching, the unlinked queue, and field-versus-live findings."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.checks.base import CheckResult
from observe.infra import InfraError, InfraService
from observe.infra_match import LivePort, Matcher, PortMatch
from observe.portkey import switch_id
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

PASSWORD = "correct horse battery"
CHASSIS = "aa:bb:cc:dd:ee:01"
MONITORS = [
    {"name": "uni core", "type": "unifi_network", "host": "unifi.lab", "credential": "u",
     "mode": "device", "device": "AA:BB:CC:DD:EE:01"},
    {"name": "snmp edge", "type": "snmp", "host": "10.0.0.2", "credential": "v2",
     "mode": "uptime"},
    {"name": "ping dist", "type": "ping", "host": "dist1.lab"},
    {"name": "twin a", "type": "ping", "host": "twin.lab"},
    {"name": "twin b", "type": "ping", "host": "twin.lab"},
    {"name": "edge gi5", "type": "snmp", "host": "10.0.0.2", "credential": "v2",
     "mode": "interface", "interface": "GigabitEthernet1/0/5"},
    {"name": "edge idx7", "type": "snmp", "host": "10.0.0.2", "credential": "v2",
     "mode": "interface", "interface": "7"},
]


def cfg(**extra: Any):
    return make_config(MONITORS, credentials={
        "u": {"type": "unifi", "api_key": "k"},
        "v2": {"type": "snmpv2c", "community": "c"}}, **extra)


@pytest.fixture
def world(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    infra = InfraService(store)
    yield store, infra, Matcher(cfg(), infra)
    store.close()


async def add(infra: InfraService, sid: str, **kw: Any) -> str:
    await infra.upsert_switch(sid, now=1.0, **kw)
    return sid


async def test_chassis_matches_automatically_but_address_and_name_only_propose(world):
    _, infra, m = world
    by_mac = await add(infra, switch_id(CHASSIS), name="whatever")
    by_addr = await add(infra, switch_id("aa:bb:cc:dd:ee:02"), mgmt_addresses=["10.0.0.2"])
    by_name = await add(infra, switch_id(sys_name="DIST1.lab"))
    got = await m.effective_matches()
    assert got[by_mac] == "uni-core"
    assert got[by_addr] is None and got[by_name] is None
    assert await m.proposals() == {by_addr: "snmp-edge", by_name: "ping-dist"}
    queue = {s["switch_id"]: s["proposed_monitor"] for s in await m.unlinked()}
    assert queue == {by_addr: "snmp-edge", by_name: "ping-dist"}


async def test_address_claim_is_a_proposal_until_an_admin_confirms_it(world):
    _, infra, m = world
    sid = await add(infra, switch_id("de:ad:be:ef:00:01"), name="evil",
                    mgmt_addresses=["10.0.0.2"])
    assert (await m.effective_matches())[sid] is None
    await m.link_switch(sid, "snmp-edge", "alice", "10.1.1.1")
    assert (await m.effective_matches())[sid] == "snmp-edge"
    assert await m.proposals() == {}
    (row,) = await infra.read(lambda d: d.execute(
        "SELECT kind, detail FROM audit ORDER BY id").fetchall())
    assert row[0] == "infra_switch_linked" and '"basis": "proposal"' in row[1]


async def test_a_manual_link_that_differs_from_the_proposal_is_audited_as_manual(world):
    _, infra, m = world
    sid = await add(infra, switch_id("de:ad:be:ef:00:02"), mgmt_addresses=["10.0.0.2"])
    await m.link_switch(sid, "ping-dist", "alice")
    (row,) = await infra.read(lambda d: d.execute("SELECT detail FROM audit").fetchall())
    assert '"basis": "manual"' in row[0]


async def test_chassis_beats_address_and_ties_are_not_guessed(world):
    _, infra, m = world
    both = await add(infra, switch_id(CHASSIS), mgmt_addresses=["10.0.0.2"])
    tie = await add(infra, switch_id("aa:bb:cc:dd:ee:03"), mgmt_addresses=["twin.lab"])
    rows = await m.effective_matches()
    assert rows[both] == "uni-core"
    assert rows[tie] is None
    assert await m.proposals() == {}
    assert [s["switch_id"] for s in await m.unlinked()] == [tie]


async def test_port_matches_snmp_by_name_alias_and_index_and_unifi_by_mac_and_index(world):
    _, infra, m = world
    sid = await add(infra, switch_id(CHASSIS), mgmt_addresses=["10.0.0.2"])
    await infra.upsert_port(sid, "Gi1/0/5", if_index=3, unifi_index=5, now=1.0)
    await infra.upsert_port(sid, "Gi1/0/9", if_index=7, now=1.0)
    await infra.upsert_port(sid, "Gi1/0/6", now=1.0)
    five = await m.port_matches(sid, "GigabitEthernet1/0/5")
    assert PortMatch("snmp", "edge-gi5", "GigabitEthernet1/0/5") in five
    assert PortMatch("unifi", "uni-core", "5") in five
    assert [x.monitor for x in await m.port_matches(sid, "gi1/0/9")] == ["edge-idx7"]
    assert await m.port_matches(sid, "gi1/0/6") == []
    assert await m.port_matches(sid, "gi1/0/44") == []


async def test_matching_never_creates_monitors(world):
    _, infra, m = world
    await add(infra, switch_id(sys_name="nowhere"))
    await m.effective_matches()
    assert len(m._config.monitors) == len(MONITORS)


async def test_unmatched_switch_is_queued_and_admin_link_is_audited(world):
    _, infra, m = world
    sid = await add(infra, switch_id(sys_name="mystery"), name="mystery")
    assert [s["switch_id"] for s in await m.unlinked()] == [sid]
    await m.link_switch(sid, "ping-dist", "alice", "10.1.1.1")
    assert await m.unlinked() == []
    # A later read keeps the admin's choice.
    assert (await m.effective_matches())[sid] == "ping-dist"
    (mon,) = [r[0] for r in await infra.read(lambda db: db.execute(
        "SELECT matched_monitor FROM infra_switches WHERE switch_id=?", (sid,)).fetchall())]
    assert mon == "ping-dist"
    rows = await infra.read(lambda d: d.execute(
        "SELECT kind, actor, remote, detail FROM audit ORDER BY id").fetchall())
    assert [r[0] for r in rows] == ["infra_switch_linked"]
    assert rows[0][1] == "alice" and rows[0][2] == "10.1.1.1"
    assert "ping-dist" in rows[0][3] and sid in rows[0][3]


async def test_admin_link_survives_disabled_monitor_and_reads_never_write(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    infra = InfraService(store)
    try:
        sid = await add(infra, switch_id(CHASSIS), mgmt_addresses=["10.0.0.2"])
        enabled = Matcher(cfg(), infra)
        await enabled.link_switch(sid, "ping-dist", "alice")
        mons = [dict(m, **({"enabled": False} if m["name"] == "ping dist" else {}))
                for m in MONITORS]
        off = Matcher(make_config(mons, credentials={
            "u": {"type": "unifi", "api_key": "k"},
            "v2": {"type": "snmpv2c", "community": "c"}}), infra)

        def column(db: Any) -> Any:
            return db.execute("SELECT matched_monitor FROM infra_switches").fetchall()
        before = await infra.read(column)
        assert (await off.effective_matches())[sid] == "ping-dist"
        await off.unlinked()
        await off.findings(lambda _m: None)
        assert await infra.read(column) == before == [("ping-dist",)]
        assert (await enabled.effective_matches())[sid] == "ping-dist"
        kinds = [r[0] for r in await infra.read(lambda d: d.execute(
            "SELECT kind FROM audit").fetchall())]
        assert kinds == ["infra_switch_linked"]
    finally:
        store.close()


async def test_interface_check_reports_speed_and_survives_odd_value():
    from observe.checks import build_check
    from observe.checks.base import Result
    from observe.checks.snmp import IF_HIGH_SPEED

    cf = make_config([{"name": "s", "type": "snmp", "host": "127.0.0.1", "credential": "v2",
                       "mode": "interface", "interface": "7"}],
                     credentials={"v2": {"type": "snmpv2c", "community": "c"}})
    for raw, want in (("1000", 1000), ("junk", None)):
        c = build_check(cf.monitors[0], cf)

        async def fake_get(*oids: str, _raw: str = raw) -> dict[str, str]:
            return {o: (_raw if o.startswith(IF_HIGH_SPEED) else "1") for o in oids}
        c.get = fake_get  # type: ignore[method-assign]
        res = await c.run()
        assert res.result is Result.OK
        assert res.detail.get("speed_mbps") == want


async def test_bad_links_are_refused_and_audited(world):
    _, infra, m = world
    sid = await add(infra, switch_id(sys_name="mystery"))
    with pytest.raises(InfraError):
        await m.link_switch(sid, "no-such-monitor", "alice")
    with pytest.raises(InfraError):
        await m.link_switch("name:ghost", "ping-dist", "alice")
    with pytest.raises(InfraError):
        await m.link_switch(sid, "ping-dist", "")
    kinds = [r[0] for r in await infra.read(lambda d: d.execute(
        "SELECT kind FROM audit ORDER BY id").fetchall())]
    assert kinds == ["infra_switch_link_failed", "infra_switch_link_failed"]
    assert len(await m.unlinked()) == 1


# Findings --------------------------------------------------------------------------------


async def seed_field(infra: InfraService, sid: str, port: str, **props: Any) -> None:
    await infra.upsert_port(sid, port, if_index=3, now=1.0)
    for name, value in props.items():
        await infra.append_property(sid, port, name, value, source="field", now=2.0)


def reader(**live: LivePort):
    return lambda match: live.get(match.monitor)


async def test_each_conflict_kind_is_produced(world):
    _, infra, m = world
    sid = await add(infra, switch_id(CHASSIS), mgmt_addresses=["10.0.0.2"])
    await seed_field(infra, sid, "Gi1/0/5", link_speed_mbps=1000, vlan=20, poe_class="4",
                     poe_load_w=7.5)
    found = await m.findings(reader(**{"edge-gi5": LivePort(speed_mbps=100, vlan=30, poe_w=0.0)}))
    kinds = sorted(f.kind for f in found)
    assert kinds == ["poe_no_power", "speed_above_live", "vlan_mismatch"]
    assert all(f.severity == "warning" and f.port_key == "gi1/0/5" for f in found)

    # Re-patched: the same jack label moves to another port.
    await seed_field(infra, sid, "Gi1/0/6", jack_label="A-12")
    await infra.upsert_port(sid, "Gi1/0/9", now=1.0)
    await infra.append_property(sid, "Gi1/0/9", "jack_label", "A-12", source="field", now=3.0)
    again = [f for f in await m.findings(reader()) if f.kind == "repatched"]
    assert len(again) == 1 and again[0].severity == "info"
    assert again[0].port_key == "gi1/0/9" and "gi1/0/6" in again[0].message


async def test_no_conflict_when_live_agrees_or_is_unknown(world):
    _, infra, m = world
    sid = await add(infra, switch_id(CHASSIS), mgmt_addresses=["10.0.0.2"])
    await seed_field(infra, sid, "Gi1/0/5", link_speed_mbps=1000, vlan=20, poe_load_w=7.5)
    assert await m.findings(reader(**{"edge-gi5": LivePort(1000, 20, 6.0)})) == []
    assert await m.findings(reader()) == []
    assert await m.findings(reader(**{"edge-gi5": LivePort()})) == []


# Through the web app: access control, the audit trail, and no alerts ---------------------


class Web:
    def __init__(self, tmp_path):
        path = str(tmp_path / "w.db")
        self.store = Store(path)
        self.cfg = cfg(server={"db_path": path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
                               "argon2_parallelism": 1},
                       alerts=[{"name": "hook", "type": "webhook", "url": "http://127.0.0.1:9/x"}])
        self.alerter = Alerter(self.cfg)
        self.calls: list[Any] = []

        async def spy(*a: Any, **k: Any) -> None:
            self.calls.append(a)
        self.alerter.notify = spy  # type: ignore[method-assign]
        self.alerter._deliver = spy  # type: ignore[method-assign]
        self.sched = Scheduler(self.cfg, self.store, self.alerter)
        self.client = TestClient(create_app(self.cfg, self.store, self.sched, self.alerter),
                                 base_url="https://testserver")
        self.infra = InfraService(self.store)

    async def login(self, name: str, admin: bool):
        await auth.create_user(self.store, self.cfg, name, PASSWORD, admin)
        r = self.client.post("/api/login", json={"username": name, "password": PASSWORD})
        return {"X-CSRF-Token": r.json()["csrf"]}


@pytest.fixture
def web(tmp_path):
    w = Web(tmp_path)
    yield w
    w.client.close()
    w.store.close()


async def test_findings_endpoint_shows_conflicts_and_calls_no_alert_target(web):
    sid = switch_id(CHASSIS)
    await web.infra.upsert_switch(sid, mgmt_addresses=["10.0.0.2"], now=1.0)
    await seed_field(web.infra, sid, "Gi1/0/5", link_speed_mbps=1000, vlan=20)
    st = web.sched.states["edge-gi5"]
    st.observe(CheckResult.ok("up", detail={"speed_mbps": 100}))
    assert web.client.get("/api/v2/findings").status_code == 401
    await web.login("bob", admin=False)
    body = web.client.get("/api/v2/findings").json()
    assert [f["kind"] for f in body["items"]] == ["speed_above_live"]
    assert web.calls == []


async def test_link_routes_need_admin_and_csrf_and_audit(web):
    sid = switch_id(sys_name="mystery")
    await web.infra.upsert_switch(sid, now=1.0)
    payload = {"switch_id": sid, "monitor": "ping-dist"}
    assert web.client.post("/api/admin/infra/link", json=payload).status_code == 401
    csrf = await web.login("bob", admin=False)
    assert web.client.get("/api/v2/admin/infra/unlinked").status_code == 403
    assert web.client.post("/api/admin/infra/link", json=payload, headers=csrf).status_code == 403
    web.client.cookies.clear()
    csrf = await web.login("root", admin=True)
    assert [s["switch_id"] for s in web.client.get("/api/v2/admin/infra/unlinked").json()["items"]] == [sid]
    assert web.client.post("/api/admin/infra/link", json=payload).status_code == 403
    assert web.client.post("/api/admin/infra/link", json={"switch_id": sid},
                           headers=csrf).status_code == 422
    assert web.client.post("/api/admin/infra/link", json={**payload, "monitor": "nope"},
                           headers=csrf).status_code == 422
    assert web.client.post("/api/admin/infra/link", json=payload, headers=csrf).status_code == 200
    assert web.client.get("/api/v2/admin/infra/unlinked").json() == {"items": []}
    kinds = [r[0] for r in await web.infra.read(lambda d: d.execute(
        "SELECT kind FROM audit WHERE kind LIKE 'infra_%' ORDER BY id").fetchall())]
    assert kinds == ["infra_switch_link_failed", "infra_switch_linked"]
    assert web.calls == []
