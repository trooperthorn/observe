"""The UniFi map feed: devices as switches keyed by chassis MAC, ports with unifi_index and
properties written only on change, links from the uplink device, uplink port numbers and LLDP,
the portkey rule for "Port N", auto-match to a unifi_network monitor, and findings against
Pockethernet field data. The console is an httpx mock transport; the classic field names in the
fixtures are unverified against a live console."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from observe.infra import InfraService
from observe.infra_match import Matcher
from observe.portkey import port_key, switch_id, unifi_port_key
from observe.store import Store
from observe_unifi.classic import parse_devices
from observe_unifi.feed import feed_classic, feed_integration
from observe_unifi.records import parse_device

from .conftest import make_config
from .test_pockethernet_upload import FIXTURE, TAKEN_S, Env as FieldEnv
from .test_unifi_classic import COOKIE, CSRF, PREFIX
from .test_unifi_plugin import BASE, SITE, Console, Env, device, run

MAC1, MAC2 = "AA:BB:CC:00:00:01", "AA:BB:CC:00:00:02"
SID1, SID2 = switch_id(MAC1), switch_id(MAC2)
NOW = TAKEN_S + 100


def port(idx: int, **kw: Any) -> dict[str, Any]:
    return {"port_idx": idx, "name": f"Port {idx}", "up": True, **kw}


def classic_dev(mac: str, name: str, ports: list[dict[str, Any]], lldp=None, uplink=None):
    # Unverified shapes: port_table, speed, poe_power, poe_class, native_vlan, lldp_table, uplink.
    return {"mac": mac, "name": name, "port_table": ports, "lldp_table": lldp or [],
            "uplink": uplink}


def classic_data() -> list[dict[str, Any]]:
    return [
        classic_dev(MAC1, "Switch 1",
                    [port(1, speed=1000, poe_enable=True, poe_power="0.00", poe_class="Class 3",
                          native_vlan=10), port(2, up=False, speed=0), port(25, speed=10000)],
                    uplink={"uplink_mac": MAC2.lower(), "port_idx": 25, "uplink_remote_port": 7}),
        classic_dev(MAC2, "Core", [port(7, speed=10000)],
                    lldp=[{"local_port_idx": 7, "chassis_id": MAC1.lower(), "port_id": "25",
                           "chassis_name": "Switch 1"},
                          {"local_port_idx": 7, "chassis_id": "de:ad:be:ef:00:99",
                           "port_id": "eth0", "chassis_name": "camera"}]),
    ]


def parsed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return parse_devices(rows)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "w.db"))
    yield s
    s.close()


def q(store: Store, sql: str, *args: Any) -> list[tuple]:
    return store._exec(sql, args)


def integration_devices():
    return [parse_device("s", device(1, uplink={"deviceId": "dev-2"})),
            parse_device("s", device(2)),
            parse_device("s", {"id": "x", "macAddress": "not a mac"})]


# --------------------------------------------------------------- port keys


def test_unifi_port_n_and_index_n_are_one_key_and_no_other():
    assert unifi_port_key(5) == port_key("Port 5") == port_key("PORT  5") == "port5"
    assert len({unifi_port_key(5), unifi_port_key(50), port_key("5"), port_key("Gi1/0/5"),
                port_key("eth5")}) == 5
    assert port_key(unifi_port_key(5)) == unifi_port_key(5)  # idempotent
    for bad in (-1, True, "5", None):
        with pytest.raises(ValueError):
            unifi_port_key(bad)  # type: ignore[arg-type]


def test_the_same_port_number_on_two_switches_never_collides(store):
    async def go():
        await feed_classic(store, parsed([
            classic_dev(MAC1, "a", [port(1, native_vlan=10)]),
            classic_dev(MAC2, "b", [port(1, native_vlan=20)])]), NOW)
        infra = InfraService(store)
        return (await infra.current_properties(SID1, "Port 1"),
                await infra.current_properties(SID2, "Port 1"))
    one, two = run(go())
    assert one["vlan"]["value"] == 10 and two["vlan"]["value"] == 20
    assert len(q(store, "SELECT 1 FROM infra_ports")) == 2


# --------------------------------------------------------------- switches and links


def test_each_device_becomes_a_switch_keyed_by_chassis_mac(store):
    res = run(feed_integration(store, integration_devices(), NOW))
    assert (res.switches, res.skipped) == (2, 1)
    rows = q(store, "SELECT switch_id, name, mgmt_addresses, vendor, platform FROM infra_switches "
                    "ORDER BY switch_id")
    assert rows == [(SID1, "Dev 1", '["192.0.2.1"]', "Ubiquiti", "USW"),
                    (SID2, "Dev 2", '["192.0.2.2"]', "Ubiquiti", "USW")]


def test_uplink_device_id_makes_a_config_link_once(store):
    run(feed_integration(store, integration_devices(), NOW))
    run(feed_integration(store, integration_devices(), NOW + 60))
    (link,) = q(store, "SELECT a_ref, b_ref, source, closed_at FROM infra_links")
    assert link[2:] == ("config", None)
    assert {link[0], link[1]} == {f"{SID1}|uplink", f"{SID2}|to-{SID1[4:]}"}
    assert q(store, "SELECT role FROM infra_ports WHERE switch_id=? AND port_key='uplink'",
             SID1) == [("uplink",)]


def test_device_link_is_skipped_when_port_numbers_are_known(store):
    run(feed_classic(store, parsed(classic_data()), NOW))
    run(feed_integration(store, integration_devices(), NOW + 1))
    refs = {tuple(sorted(r)) for r in q(store, "SELECT a_ref, b_ref FROM infra_links "
                                               "WHERE source='config'")}
    assert refs == {tuple(sorted((f"{SID1}|port25", f"{SID2}|port7")))}


def test_classic_feed_writes_ports_properties_and_links(store):
    res = run(feed_classic(store, parsed(classic_data()), NOW))
    assert res.switches == 2
    ports = q(store, "SELECT switch_id, port_key, raw_port_id, unifi_index, role FROM infra_ports "
                     "ORDER BY switch_id, unifi_index")
    assert ports == [(SID1, "port1", "Port 1", 1, "unknown"),
                     (SID1, "port2", "Port 2", 2, "unknown"),
                     (SID1, "port25", "Port 25", 25, "uplink"),
                     (SID2, "port7", "Port 7", 7, "unknown")]
    infra = InfraService(store)
    props = run(infra.current_properties(SID1, "port1"))
    assert {k: (v["value"], v["unit"], v["source"]) for k, v in props.items()} == {
        "link_speed_mbps": (1000, "Mbps", "unifi"), "poe_class": ("Class 3", "", "unifi"),
        "poe_load_w": (0.0, "W", "unifi"), "vlan": (10, "", "unifi")}
    # A port that is down reports no speed, never zero.
    assert "link_speed_mbps" not in run(infra.current_properties(SID1, "port2"))
    links = {(r[2], tuple(sorted((r[0], r[1])))) for r in q(
        store, "SELECT a_ref, b_ref, source FROM infra_links")}
    ends = tuple(sorted((f"{SID1}|port25", f"{SID2}|port7")))
    assert links == {("config", ends), ("lldp", ends)}
    # The camera that speaks LLDP is not a switch.
    assert len(q(store, "SELECT 1 FROM infra_switches")) == 2


def test_properties_are_written_only_on_change(store):
    run(feed_classic(store, parsed(classic_data()), NOW))
    n = len(q(store, "SELECT 1 FROM port_properties"))
    again = run(feed_classic(store, parsed(classic_data()), NOW + 120))
    assert again.properties_added == 0
    assert len(q(store, "SELECT 1 FROM port_properties")) == n
    assert q(store, "SELECT last_verified FROM port_properties WHERE name='vlan' "
                    "AND switch_id=?", SID1) == [(NOW + 120,)]
    changed = classic_data()
    changed[0]["port_table"][0]["native_vlan"] = 30
    assert run(feed_classic(store, parsed(changed), NOW + 240)).properties_added == 1
    hist = run(InfraService(store).property_history(SID1, "port1", "vlan"))
    assert [h["value"] for h in hist] == [30, 10]


def test_a_malformed_row_is_skipped_not_fatal(store):
    rows = classic_data()
    rows[0]["port_table"][0]["name"] = "x" * 400  # longer than raw_port_id may be
    res = run(feed_classic(store, parsed(rows), NOW))
    assert res.skipped >= 1 and res.switches == 2
    assert q(store, "SELECT 1 FROM infra_ports WHERE port_key='port7'")


def test_a_second_source_keeps_its_own_history(store):
    async def go():
        infra = InfraService(store)
        await infra.upsert_switch(SID1, now=1.0)
        await infra.upsert_port(SID1, "Port 1", now=1.0)
        assert await infra.append_property(SID1, "Port 1", "vlan", 20, source="field", now=2.0)
        # The same value from another source is its own row, not a confirmation of the first.
        assert await infra.append_property(SID1, "Port 1", "vlan", 20, source="unifi", now=3.0)
        assert not await infra.append_property(SID1, "Port 1", "vlan", 20, source="unifi",
                                               now=4.0)
    run(go())
    assert q(store, "SELECT source, last_verified FROM port_properties ORDER BY id") == [
        ("field", 2.0), ("unifi", 4.0)]


# --------------------------------------------------------------- matching


def matcher(store: Store) -> Matcher:
    mons = [{"name": "uni sw1", "type": "unifi_network", "host": "unifi.lab", "credential": "u",
             "mode": "ports", "device": MAC1}]
    cfg = make_config(mons, credentials={"u": {"type": "unifi", "api_key": "k"}})
    return Matcher(cfg, InfraService(store))


async def test_feed_switch_auto_matches_a_unifi_network_monitor(store):
    await feed_integration(store, integration_devices(), NOW)
    await feed_classic(store, parsed(classic_data()), NOW)
    m = matcher(store)
    assert await m.effective_matches() == {SID1: "uni-sw1", SID2: None}
    (hit,) = await m.port_matches(SID1, "Port 1")
    assert (hit.kind, hit.monitor, hit.detail) == ("unifi", "uni-sw1", "1")
    assert await m.port_matches(SID2, "Port 7") == []


# --------------------------------------------------------------- findings with field data


def field_report(env: FieldEnv) -> dict[str, Any]:
    n = json.loads(json.dumps(FIXTURE["neighbors"][0]))
    n.update(device_id=MAC1, port_id="Port 1", system_name="Switch 1", description="Port 1",
             source_mac=MAC1)
    n["lldp"].update(chassis_id=MAC1, port_id="Port 1", system_name="Switch 1",
                     port_description="Port 1")
    return env.report(neighbors=[n])


@pytest.fixture
def field(tmp_path):
    e = FieldEnv(tmp_path)
    yield e
    e.close()


def kinds(store: Store, now: float) -> list[str]:
    found = asyncio.run(matcher(store).findings(lambda match: None, now))
    return sorted(f.kind for f in found)


def test_pockethernet_lands_on_the_same_port_as_the_feed(field):
    run(feed_classic(field.store, parsed(classic_data()), NOW))
    assert field.post(field_report(field)).json()["result"] == "accepted"
    assert q(field.store, "SELECT unifi_index FROM infra_ports WHERE switch_id=? AND "
                          "port_key='port1'", SID1) == [(1,)]
    names = {r[0] for r in q(field.store, "SELECT name FROM port_properties WHERE switch_id=? "
                                          "AND port_key='port1' AND source='pockethernet'", SID1)}
    assert "vlan" in names


def test_classic_data_yields_vlan_speed_and_poe_findings(field):
    # The tester saw VLAN 20, 1000 Mbit/s and verified PoE class 4; the switch reports VLAN 10,
    # 100 Mbit/s and no power.
    run(feed_classic(field.store, parsed([classic_dev(
        MAC1, "Switch 1", [port(1, speed=100, poe_power="0.00", poe_class="Class 3",
                                native_vlan=10)])]), NOW))
    field.post(field_report(field))
    assert kinds(field.store, NOW + 10) == ["poe_no_power", "speed_above_live", "vlan_mismatch"]


def test_findings_stay_silent_without_classic_data(field):
    run(feed_integration(field.store, integration_devices(), NOW))
    field.post(field_report(field))
    assert kinds(field.store, NOW + 10) == []


def test_stale_device_data_is_not_used_and_agreement_is_silent(field):
    run(feed_classic(field.store, parsed([classic_dev(
        MAC1, "Switch 1", [port(1, speed=100, native_vlan=10)])]), NOW))
    field.post(field_report(field))
    assert kinds(field.store, NOW + 10) == ["speed_above_live", "vlan_mismatch"]
    assert kinds(field.store, NOW + 10_000) == []  # not confirmed for a long time
    run(feed_classic(field.store, parsed([classic_dev(
        MAC1, "Switch 1", [port(1, speed=1000, poe_power="6.4", native_vlan=20)])]), NOW + 60))
    assert kinds(field.store, NOW + 70) == []  # the switch now agrees with the field test


def test_device_properties_never_count_as_field_data(store):
    run(feed_classic(store, parsed(classic_data()), NOW))
    assert kinds(store, NOW + 1) == []  # nothing to compare: only the device spoke


# --------------------------------------------------------------- end to end through the plugin


class Both:
    """One console for the Integration API and the classic API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        p = request.url.path
        if p == "/api/auth/login":
            return httpx.Response(200, json={}, headers={
                "set-cookie": f"TOKEN={COOKIE}; path=/; secure; httponly", "x-csrf-token": CSRF})
        if p.startswith(PREFIX):
            data = {"stat/device": classic_data(), "stat/sta": [], "rest/user": [],
                    "stat/health": []}.get(p[len(PREFIX):])
            return httpx.Response(200, json={"data": data})
        off = int(request.url.params.get("offset", "0"))
        lim = int(request.url.params.get("limit", "25"))
        rows = ([SITE] if p == f"{BASE}/sites" else
                [device(1, uplink={"deviceId": "dev-2"}), device(2)])
        page = rows[off:off + lim]
        return httpx.Response(200, json={"offset": off, "limit": lim, "count": len(page),
                                         "totalCount": len(rows), "data": page})


def test_both_collectors_feed_the_map_and_only_get_requests_follow_the_login(tmp_path):
    console = Both()
    env = Env(tmp_path, Console([]), {"classic_credential": "classic"})
    env.plugin.transport = httpx.MockTransport(console)
    assert [c.name for c in env.plugin.collectors()] == ["devices", "classic"]
    assert run(env.plugin.collect_devices(env.store)) == 2
    assert run(env.plugin.collect_classic(env.store)) == 2
    sent = [(r.method, r.url.path) for r in console.requests]
    assert [p for m, p in sent if m != "GET"] == ["/api/auth/login"]
    assert len(env.rows("infra_switches")) == 2
    assert len(env.rows("infra_ports")) == 6  # four real ports and the two placeholder ports
    open_links = [(q_[5]) for q_ in env.rows("infra_links") if q_[9] is None]
    assert sorted(open_links) == ["config", "lldp"]
    # The device-level placeholder was closed when the real port numbers arrived.
    (closed,) = [q_ for q_ in env.rows("infra_links") if q_[9] is not None]
    assert "uplink" in (closed[2] + closed[4])


def test_no_classic_collector_without_the_credential(tmp_path):
    env = Env(tmp_path, Console([device(1)]))
    assert [c.name for c in env.plugin.collectors()] == ["devices"]
    assert run(env.plugin.collect_devices(env.store)) == 1
    assert env.rows("infra_ports") == []
    assert run(env.plugin.collect_classic(env.store)) == 0
