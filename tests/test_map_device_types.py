"""Bug plan WP4: the map names each device by its type and puts the gateway in the top tier.

The UniFi feed classifies each device (records.device_type_of: features, then type or role, then
the model name) and stores it on the switch row; the map JSON carries it as `device_type`. A
switch from LLDP or SNMP has no type and is shown as before.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from observe_unifi.records import device_type_of

from observe.infra import InfraError, InfraService
from observe.portkey import switch_id
from observe.store import Store

from .test_unifi_network import WithDetails, api_env
from .test_unifi_plugin import device


@pytest.mark.parametrize("args, want", [
    ((["GATEWAY", "SWITCHING"], "", "", "USW-Pro-24"), "gateway"),
    ((["SWITCHING", "ACCESS_POINT"], "", "", ""), "switch"),
    ((["accessPoint"], "", "", ""), "access_point"),
    (((), "gateway", "", ""), "gateway"),
    (((), "", "console", ""), "gateway"),
    (((), "uap", "", "U7PG2"), "access_point"),
    (((), "usw", "", "US8P60"), "switch"),
    (((), "ubb", "", ""), "bridge"),
    (((), "", "", "UCG-Fiber"), "gateway"),
    (((), "", "", "UDM Pro"), "gateway"),
    (((), "", "", "UXG-Lite"), "gateway"),
    (((), "", "", "UDR"), "gateway"),
    (((), "", "", "U7 Pro"), "access_point"),
    (((), "", "", "U6-LR"), "access_point"),
    (((), "", "", "UAP-AC-Pro"), "access_point"),
    (((), "", "", "UAL6"), "access_point"),
    (((), "", "", "USW Pro Max 16 PoE"), "switch"),
    (((), "", "", "US-8-60W"), "switch"),
    ((["PROTECT"], "", "", "UNVR"), "other"),
    (((), "", "", "Catalyst 9300"), ""),
    (((), "", "", ""), ""),
    # The reported devices: Ranchero-Fiber (a UCG Fiber) and MainUDBPro (a UniFi Device Bridge
    # Pro) were drawn as switches. A gateway or bridge model code wins over SWITCHING or
    # ACCESS_POINT, in any case and spacing.
    ((["SWITCHING"], "", "", "UCG-Fiber"), "gateway"),
    ((["switching"], "", "", "UCG Fiber"), "gateway"),
    ((["SWITCHING"], "", "", "UCGF"), "gateway"),
    ((["switching", "accessPoint"], "", "", "ucg fiber"), "gateway"),
    ((["SWITCHING"], "", "", "UDM-Pro-Max"), "gateway"),
    ((["SWITCHING"], "", "", "UDR7"), "gateway"),
    ((["SWITCHING"], "", "", "UXG Pro"), "gateway"),
    ((["SWITCHING"], "", "", "EFG"), "gateway"),
    ((["SWITCHING"], "usw", "", "UCG-Ultra"), "gateway"),
    ((["SWITCHING"], "", "", "UDB Pro"), "bridge"),
    ((["ACCESS_POINT"], "", "", "UDBPRO"), "bridge"),
    ((["switching"], "", "", "UDB-Pro"), "bridge"),
    ((["ACCESS_POINT"], "", "", "UBB-XG"), "bridge"),
    ((["SWITCHING"], "udb", "", ""), "bridge"),
    ((["SWITCHING"], "", "bridge", ""), "bridge"),
    # A switch or access point keeps its type.
    ((["SWITCHING"], "", "", "USW Flex"), "switch"),
    ((["ACCESS_POINT"], "", "", "U7 Pro"), "access_point"),
])
def test_device_type_prefers_features_then_type_then_model(args, want):
    assert device_type_of(*args) == want


def test_a_switch_keeps_its_type_when_a_feed_without_one_refreshes_it(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    infra = InfraService(store)
    try:
        sid = asyncio.run(infra.upsert_switch(switch_id("aa:bb:cc:dd:ee:01"), name="gw",
                                              device_type="gateway", now=1.0))
        asyncio.run(infra.upsert_switch(sid, name="gw", now=2.0))  # an LLDP sighting
        row = asyncio.run(infra.read(lambda db: db.execute(
            "SELECT device_type FROM infra_switches WHERE switch_id=?", (sid,)).fetchone()))
        assert row[0] == "gateway"
        with pytest.raises(InfraError):
            asyncio.run(infra.upsert_switch(sid, device_type="router", now=3.0))
    finally:
        store.close()


SEVEN = [
    device(1, name="UCG Fiber", model="UCG-Fiber", features=["GATEWAY", "SWITCHING"]),
    device(2, name="Core switch", model="USW-Pro-Max-24", features=["SWITCHING"]),
    device(3, name="Lab switch", model="USW-Lite-8-PoE", features=["SWITCHING"]),
    device(4, name="AP hall", model="U7-Pro", features=["ACCESS_POINT"]),
    device(5, name="AP office", model="U6-LR", features=["ACCESS_POINT"]),
    device(6, name="AP lab", model="U6-Lite", features=["ACCESS_POINT"]),
    device(7, name="AP garden", model="UAP-AC-M"),  # no features: the model says
]
UPLINKS = {"dev-2": "dev-1", "dev-3": "dev-2", "dev-4": "dev-2", "dev-5": "dev-2",
           "dev-6": "dev-3", "dev-7": "dev-1"}


def seven_device_map(tmp_path: Any) -> dict[str, Any]:
    """The map JSON for a console like the one in the bug report: a UCG Fiber gateway, two
    switches and four access points, all uplinked."""
    env = api_env(tmp_path, WithDetails(SEVEN, UPLINKS))
    try:
        # The map ages links against the real clock when the app reads it, so poll at that time.
        env.inner.now = time.time()
        env.collect()
        # A switch only LLDP knows about: no type, shown as a switch as before.
        asyncio.run(InfraService(env.store).upsert_switch(
            switch_id("aa:bb:cc:dd:ee:99"), name="lldp sw"))
        got = env.client.get("/api/v2/map")
        assert got.status_code == 200
        return got.json()  # type: ignore[no-any-return]
    finally:
        env.close()


def test_the_map_json_names_each_unifi_device_by_its_type(tmp_path):
    data = seven_device_map(tmp_path)
    switches = {n["label"]: n for n in data["nodes"] if n["kind"] == "switch"}
    assert {k: v["device_type"] for k, v in switches.items()} == {
        "UCG Fiber": "gateway", "Core switch": "switch", "Lab switch": "switch",
        "AP hall": "access_point", "AP office": "access_point", "AP lab": "access_point",
        "AP garden": "access_point", "lldp sw": ""}
    gw = switches["UCG Fiber"]
    assert gw["platform"] == "UCG-Fiber" and gw["address"] == "192.0.2.1"
    # The gateway is the top of the chain: the anchor the graph pulls to the centre, and no
    # uplink port of its own, which is what the tiers view places in Core.
    assert gw["anchor"] is True
    assert not any(n["anchor"] for k, n in switches.items() if k != "UCG Fiber")
    ports = [n for n in data["nodes"] if n["kind"] == "port" and n["parent"] == gw["id"]]
    assert ports and all(p["role"] != "uplink" for p in ports)
    assert len(data["edges"]) == 6
