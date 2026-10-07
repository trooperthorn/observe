"""Map data, link ageing and inferred dependencies."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.checks.base import CheckResult
from observe.infra import InfraError, InfraService
from observe.infra_map import MapService
from observe.map_tables import DAY, link_state
from observe.infra_match import Matcher
from observe.portkey import switch_id
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

PASSWORD = "correct horse battery"
MONITORS = [
    {"name": "core sw", "type": "ping", "host": "10.0.0.1"},
    {"name": "edge sw", "type": "ping", "host": "10.0.0.2"},
    {"name": "server", "type": "ping", "host": "10.0.0.50"},
    {"name": "printer", "type": "ping", "host": "10.0.0.60"},
]
T0 = 1_000_000.0
CORE = switch_id("aa:bb:cc:dd:ee:01")
EDGE = switch_id("aa:bb:cc:dd:ee:02")


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class World:
    def __init__(self, tmp_path: Any, **extra: Any) -> None:
        path = str(tmp_path / "w.db")
        self.store = Store(path)
        self.cfg = make_config(MONITORS, server={
            "db_path": path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
            "argon2_parallelism": 1}, **extra)
        self.infra = InfraService(self.store)
        self.sched = Scheduler(self.cfg, self.store, Alerter(self.cfg))
        self.clock = Clock()
        self.matcher = Matcher(self.cfg, self.infra)

        def state_of(slug: str) -> Any:
            return self.sched.rollup.effective(slug) if slug in self.sched.states else None
        self.map = MapService(self.cfg, self.infra, self.matcher, state_of, self.clock)

    async def build(self) -> None:
        """A core and an edge switch, an uplink port on the edge and an access port."""
        await self.infra.upsert_switch(CORE, mgmt_addresses=["10.0.0.1"], now=T0)
        await self.infra.upsert_switch(EDGE, mgmt_addresses=["10.0.0.2"], now=T0)
        await self.confirm()
        await self.infra.upsert_port(CORE, "Gi1/0/1", role="access", now=T0)
        await self.infra.upsert_port(EDGE, "Gi1/0/48", role="uplink", now=T0)
        await self.infra.upsert_port(EDGE, "Gi1/0/5", role="access", now=T0)

    async def confirm(self) -> None:
        """An admin confirms the two address matches, which are only proposals otherwise."""
        await self.matcher.link_switch(CORE, "core-sw", "admin")
        await self.matcher.link_switch(EDGE, "edge-sw", "admin")

    async def uplink(self, source: str = "lldp", now: float = T0) -> int:
        return await self.infra.upsert_link(
            InfraService.port_ref(EDGE, "Gi1/0/48"), InfraService.port_ref(CORE, "Gi1/0/1"),
            source=source, now=now)

    async def endpoint(self, slug: str, source: str = "lldp", now: float = T0) -> int:
        eid = await self.infra.upsert_endpoint("monitor", slug, now=now)
        await self.infra.upsert_link(InfraService.endpoint_ref(eid),
                                     InfraService.port_ref(EDGE, "Gi1/0/5"),
                                     source=source, now=now)
        return eid

    async def data(self, site: str | None = None, building: str | None = None,
                   now: float = T0) -> dict[str, Any]:
        """The map as a reader sees it: the tables are rebuilt at `now` (the 60 second hook does
        this in the app) and read in two statements."""
        await self.map.rebuild(now)
        return await self.map.map_data(site, building)

    async def scalar(self, sql: str, *args: Any) -> Any:
        return await self.infra.read(lambda d: d.execute(sql, args).fetchone()[0])


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.store.close()


def pairs(deps: dict[str, Any], part: str) -> list[tuple[str, str]]:
    return [(e["child"], e["parent"]) for e in deps[part]]


# Ageing ----------------------------------------------------------------------------------


def test_link_state_boundaries():
    d = 90
    assert link_state(0, None, d * DAY, d) == "active"
    assert link_state(0, None, d * DAY + 1, d) == "stale"
    assert link_state(0, None, 2 * d * DAY, d) == "stale"
    assert link_state(0, None, 2 * d * DAY + 1, d) == "hidden"
    assert link_state(0, 5.0, 1.0, d) == "closed"


async def test_stale_and_hidden_ageing_with_a_fake_clock(w):
    await w.build()
    await w.uplink(now=T0)
    m = await w.data(now=T0 + 89 * DAY)
    assert [e["state"] for e in m["edges"]] == ["active"]
    m = await w.data(now=T0 + 91 * DAY)
    assert [e["state"] for e in m["edges"]] == ["stale"]
    assert m["edges"][0]["age_days"] == 91.0
    assert (await w.data(now=T0 + 181 * DAY))["edges"] == []
    # The row was kept, and confirming the link brings it back.
    assert await w.scalar("SELECT COUNT(*) FROM infra_links") == 1
    await w.uplink(now=T0 + 182 * DAY)
    m = await w.data(now=T0 + 182 * DAY)
    assert [e["state"] for e in m["edges"]] == ["active"]


async def test_stale_days_comes_from_config(tmp_path):
    world = World(tmp_path, map={"stale_days": 10})
    try:
        await world.build()
        await world.uplink(now=T0)
        assert (await world.data(now=T0 + 11 * DAY))["edges"][0]["state"] == "stale"
        assert (await world.data(now=T0 + 21 * DAY))["edges"] == []
    finally:
        world.store.close()


async def test_a_contradicting_report_closes_the_old_edge(w):
    await w.build()
    await w.infra.upsert_port(EDGE, "Gi1/0/6", now=T0)
    await w.infra.upsert_jack("hq/b1/r1/p1/05", room="r1", site="hq", now=T0)
    jack = InfraService.jack_ref("hq/b1/r1/p1/05")
    old = await w.infra.upsert_link(jack, InfraService.port_ref(EDGE, "Gi1/0/5"),
                                    source="field_report", now=T0)
    assert [e["id"] for e in (await w.data(now=T0))["edges"]] == [old]
    new = await w.infra.upsert_link(jack, InfraService.port_ref(EDGE, "Gi1/0/6"),
                                    source="field_report", now=T0 + 60)
    assert [e["id"] for e in (await w.data(now=T0 + 60))["edges"]] == [new]
    assert await w.scalar("SELECT closed_at FROM infra_links WHERE id=?", old) == T0 + 60
    assert await w.scalar("SELECT closed_at FROM infra_links WHERE id=?", new) is None
    # An uplink seen against a different neighbour is contradicted the same way.
    await w.infra.upsert_port(CORE, "Gi1/0/2", now=T0)
    first = await w.uplink(now=T0)
    await w.infra.upsert_link(InfraService.port_ref(EDGE, "Gi1/0/48"),
                              InfraService.port_ref(CORE, "Gi1/0/2"), source="lldp", now=T0 + 90)
    assert await w.scalar("SELECT closed_at FROM infra_links WHERE id=?", first) == T0 + 90
    # Several endpoints on one port do not contradict each other.
    await w.endpoint("server", now=T0)
    await w.endpoint("printer", now=T0)
    assert await w.scalar("SELECT COUNT(*) FROM infra_links WHERE a_kind='endpoint' "
                          "AND closed_at IS NULL") == 2


async def test_an_admin_config_link_is_not_closed_by_a_report(w):
    await w.build()
    await w.infra.upsert_port(CORE, "Gi1/0/2", now=T0)
    pinned = await w.uplink(source="config", now=T0)
    await w.infra.upsert_link(InfraService.port_ref(EDGE, "Gi1/0/48"),
                              InfraService.port_ref(CORE, "Gi1/0/2"), source="lldp", now=T0 + 5)
    assert await w.scalar("SELECT closed_at FROM infra_links WHERE id=?", pinned) is None


# Map shape and live state ----------------------------------------------------------------


async def test_map_json_shape_with_live_state(w):
    await w.build()
    await w.uplink()
    await w.endpoint("server")
    await w.infra.upsert_jack("hq/b1/r1/p1/05", room="r1", site="hq", switch=EDGE,
                              port="Gi1/0/5", now=T0)
    await w.infra.upsert_link(InfraService.jack_ref("hq/b1/r1/p1/05"),
                              InfraService.port_ref(EDGE, "Gi1/0/5"), source="field_report",
                              now=T0)
    w.sched.states["core-sw"].observe(CheckResult.ok("up"))
    w.sched.states["edge-sw"].observe(CheckResult.fail("no reply"))
    await w.map.refresh(T0)
    m = await w.data(now=T0)
    assert set(m) == {"nodes", "edges", "stale_days", "filter"}
    by_id = {n["id"]: n for n in m["nodes"]}
    core, edge = by_id[f"switch:{CORE}"], by_id[f"switch:{EDGE}"]
    assert (core["kind"], core["monitor"], core["state"]) == ("switch", "core-sw", "up")
    assert (edge["monitor"], edge["state"], edge["blocked_by"]) == ("edge-sw", "down", None)
    assert by_id[f"port:{EDGE}|gi1/0/48"]["parent"] == f"switch:{EDGE}"
    assert by_id[f"port:{EDGE}|gi1/0/48"]["role"] == "uplink"
    ep = next(n for n in m["nodes"] if n["kind"] == "endpoint")
    assert ep["monitor"] == "server" and ep["state"] == "pending"
    assert by_id["jack:hq/b1/r1/p1/05"]["building"] == "b1"
    assert {e["source"] for e in m["edges"]} == {"lldp", "field_report"}
    # The uplinked edge switch sits below the core, so only the core is an anchor.
    assert core["anchor"] is True and edge["anchor"] is False
    assert {x for e in m["edges"] for x in (e["a"], e["b"])} <= set(by_id)
    # Without a matched monitor the state is spelled "unknown", never guessed.
    assert by_id[f"port:{EDGE}|gi1/0/5"]["state"] == "unknown"


async def test_map_filters_by_site_and_building(w):
    await w.build()
    other = switch_id("aa:bb:cc:dd:ee:09")
    await w.infra.upsert_switch(other, name="far", now=T0)
    await w.infra.upsert_port(other, "Gi0/1", now=T0)
    await w.infra.upsert_jack("hq/b1/r1/p1/05", room="r1", site="hq", switch=EDGE,
                              port="Gi1/0/5", now=T0)
    await w.infra.upsert_jack("dc/b9/r2/p1/01", room="r2", site="dc", switch=other,
                              port="Gi0/1", now=T0)
    await w.uplink()

    def switches(m: dict[str, Any]) -> set[str]:
        return {n["id"][7:] for n in m["nodes"] if n["kind"] == "switch"}
    assert switches(await w.data(now=T0)) == {CORE, EDGE, other}
    hq = await w.data("hq", now=T0)
    assert switches(hq) == {CORE, EDGE}  # the uplink hop stays in view
    assert [n["label"] for n in hq["nodes"] if n["kind"] == "jack"] == ["hq/b1/r1/p1/05"]
    assert switches(await w.data("dc", "b9", now=T0)) == {other}
    assert (await w.data("hq", "b2", now=T0))["nodes"] == []
    assert hq["filter"] == {"site": "hq", "building": None}


# Dependencies ----------------------------------------------------------------------------


async def test_auto_applied_lldp_edge_makes_a_downstream_monitor_unreachable(w):
    await w.build()
    await w.uplink(source="lldp")
    await w.endpoint("server", source="cdp")
    plan = await w.map.refresh(T0)
    assert pairs(plan.as_dict(), "applied") == [("edge-sw", "core-sw"), ("server", "edge-sw")]
    assert all(e["by"] == "auto" for e in plan.applied)
    assert [p.slug for p in w.cfg.parents(w.cfg.resolve_monitor("server"))] == ["edge-sw"]
    # The core switch is DOWN, so everything behind it is UNREACHABLE and names the core.
    for slug in ("core-sw", "edge-sw", "server"):
        w.sched.states[slug].observe(CheckResult.fail("no reply"))
    assert w.sched.rollup.effective("core-sw") == ("down", None)
    assert w.sched.rollup.effective("edge-sw") == ("unreachable", "core sw")
    assert w.sched.rollup.effective("server") == ("unreachable", "core sw")


async def test_auto_depends_off_applies_nothing_and_lists_everything(tmp_path):
    world = World(tmp_path, map={"auto_depends": False})
    try:
        await world.build()
        await world.uplink()
        plan = (await world.map.refresh(T0)).as_dict()
        assert plan["applied"] == []
        assert pairs(plan, "pending") == [("edge-sw", "core-sw")]
        assert world.cfg.parents(world.cfg.resolve_monitor("edge-sw")) == []
    finally:
        world.store.close()


async def test_a_weak_edge_needs_acceptance(w):
    await w.build()
    await w.uplink(source="field_report")
    plan = (await w.map.refresh(T0)).as_dict()
    assert plan["applied"] == []
    assert pairs(plan, "pending") == [("edge-sw", "core-sw")]
    assert plan["pending"][0]["strong"] is False
    assert w.cfg.parents(w.cfg.resolve_monitor("edge-sw")) == []
    after = await w.map.decide("edge-sw", "core-sw", "accepted", "root", "10.1.1.1")
    assert pairs(after.as_dict(), "applied") == [("edge-sw", "core-sw")]
    assert after.applied[0]["by"] == "admin"
    assert [p.slug for p in w.cfg.parents(w.cfg.resolve_monitor("edge-sw"))] == ["core-sw"]
    rows = await w.infra.read(lambda d: d.execute(
        "SELECT kind, actor FROM audit WHERE kind LIKE 'infra_depends%'").fetchall())
    assert rows == [("infra_depends_accepted", "root")]


async def test_a_direction_that_cannot_be_told_proposes_nothing(w):
    await w.build()
    await w.infra.upsert_port(EDGE, "Gi1/0/48", role="access", now=T0)
    await w.uplink()
    plan = (await w.map.refresh(T0)).as_dict()
    assert plan["applied"] == [] and plan["pending"] == []


async def test_a_stale_lldp_edge_is_weak_and_a_rejection_holds(w):
    await w.build()
    await w.uplink(source="lldp", now=T0)
    stale = (await w.map.refresh(T0 + 100 * DAY)).as_dict()
    assert stale["applied"] == [] and pairs(stale, "pending") == [("edge-sw", "core-sw")]
    fresh = (await w.map.refresh(T0 + 10 * DAY)).as_dict()
    assert pairs(fresh, "applied") == [("edge-sw", "core-sw")]
    gone = await w.map.decide("edge-sw", "core-sw", "rejected", "root")
    assert gone.applied == [] and gone.pending == []
    assert pairs(gone.as_dict(), "rejected") == [("edge-sw", "core-sw")]
    assert w.cfg.parents(w.cfg.resolve_monitor("edge-sw")) == []


async def test_a_contradicted_link_takes_its_applied_edge_away(w):
    await w.build()
    await w.uplink(source="lldp")
    assert pairs((await w.map.refresh(T0)).as_dict(), "applied") == [("edge-sw", "core-sw")]
    # The edge switch now reports another neighbour, a switch that no monitor matches.
    far = switch_id("aa:bb:cc:dd:ee:09")
    await w.infra.upsert_switch(far, now=T0)
    await w.infra.upsert_port(far, "Gi0/1", role="access", now=T0)
    await w.infra.upsert_link(InfraService.port_ref(EDGE, "Gi1/0/48"),
                              InfraService.port_ref(far, "Gi0/1"), source="lldp", now=T0 + 5)
    plan = (await w.map.refresh(T0 + 6)).as_dict()
    assert plan["applied"] == []
    assert w.cfg.parents(w.cfg.resolve_monitor("edge-sw")) == []


async def test_a_cycle_is_refused_and_listed(w):
    # The YAML already says edge depends on core; the map now claims the reverse.
    w.cfg.monitors[1].depends_on = ["core-sw"]
    await w.infra.upsert_switch(CORE, mgmt_addresses=["10.0.0.1"], now=T0)
    await w.infra.upsert_switch(EDGE, mgmt_addresses=["10.0.0.2"], now=T0)
    await w.confirm()
    await w.infra.upsert_port(CORE, "Gi1/0/1", role="uplink", now=T0)
    await w.infra.upsert_port(EDGE, "Gi1/0/48", role="access", now=T0)
    await w.uplink(source="lldp")
    plan = (await w.map.refresh(T0)).as_dict()
    assert plan["applied"] == []
    assert pairs(plan, "refused") == [("core-sw", "edge-sw")]
    assert plan["refused"][0]["reason"] == "would create a dependency cycle"
    assert w.cfg.parents(w.cfg.resolve_monitor("core-sw")) == []
    with pytest.raises(InfraError, match="cycle"):
        await w.map.decide("core-sw", "edge-sw", "accepted", "root")
    kinds = [r[0] for r in await w.infra.read(lambda d: d.execute(
        "SELECT kind FROM audit WHERE kind LIKE 'infra_depends%'").fetchall())]
    assert kinds == ["infra_depends_failed"]


async def test_deciding_on_an_unknown_proposal_is_refused(w):
    await w.build()
    with pytest.raises(InfraError, match="no such"):
        await w.map.decide("edge-sw", "core-sw", "accepted", "root")


# Through the web app ---------------------------------------------------------------------


@pytest.fixture
def web(tmp_path):
    world = World(tmp_path)
    world.client = TestClient(
        create_app(world.cfg, world.store, world.sched, Alerter(world.cfg),
                   map_clock=world.clock),
        base_url="https://testserver")
    yield world
    world.client.close()
    world.store.close()


async def login(web: World, name: str, admin: bool) -> dict[str, str]:
    await auth.create_user(web.store, web.cfg, name, PASSWORD, admin)
    r = web.client.post("/api/login", json={"username": name, "password": PASSWORD})
    return {"X-CSRF-Token": r.json()["csrf"]}


async def test_map_routes_need_a_session_and_decisions_need_admin_and_csrf(web):
    await web.build()
    await web.uplink(source="field_report")
    for path in ("/api/infra/map", "/api/infra/dependencies"):
        assert web.client.get(path).status_code == 401
    csrf = await login(web, "bob", admin=False)
    body = web.client.get("/api/infra/map?site=nowhere").json()
    assert body["nodes"] == [] and body["filter"]["site"] == "nowhere"
    assert len(web.client.get("/api/infra/map").json()["nodes"]) >= 2
    deps = web.client.get("/api/infra/dependencies").json()
    assert [(e["child"], e["parent"]) for e in deps["pending"]] == [("edge-sw", "core-sw")]
    pay = {"child": "edge-sw", "parent": "core-sw"}
    accept, reject = "/api/admin/infra/depends/accept", "/api/admin/infra/depends/reject"
    assert web.client.post(accept, json=pay, headers=csrf).status_code == 403
    web.client.cookies.clear()
    csrf = await login(web, "root", admin=True)
    assert web.client.post(accept, json=pay).status_code == 403
    assert web.client.post(accept, json={"child": "x"}, headers=csrf).status_code == 422
    assert web.client.post(accept, json={"child": "a", "parent": "b"},
                           headers=csrf).status_code == 422
    assert web.client.post(accept, json=pay, headers=csrf).status_code == 200
    assert web.client.get("/api/infra/dependencies").json()["applied"][0]["by"] == "admin"
    assert web.client.post(reject, json=pay, headers=csrf).status_code == 200
    assert web.client.get("/api/infra/dependencies").json()["applied"] == []
