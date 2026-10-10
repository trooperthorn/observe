"""The UniFi network view data (slice 1): gateway selection, the uplink statistics, the classic
gateway and client fields, SSID readiness rows, the site status table and how everything
degrades without the classic account. Fixtures reuse the shapes of ha_Int_soc
tests/test_unifi_core.py (the UDM gateway with `uplink` and `network_table`) with both the
underscore spelling that module reads and the hyphenated one the classic console most likely
sends. Every classic field name and the statistics uplink node are unverified against a live
console; these tests pin the parsers, not the console."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from observe_unifi import resolve_wlans
from observe_unifi.classic import (OFFLINE_DEVICE_STATES, parse_devices, parse_networks,
                                   parse_wan_health, parse_wan_uplink, parse_wlans,
                                   resolve_client_vlan, select_gateway_row, uptime_to_seconds,
                                   wan_status)
from observe_unifi.clients import client_rates, client_totals, parse_active_clients
from observe_unifi.records import (counters, parse_device, parse_uplink_stats, select_gateway)

from .test_unifi_clients import Full, client_row, env_with, run
from .test_unifi_plugin import device

NOW = 1_700_000_000.0

# The ha_Int_soc gateway (tests/test_unifi_core.py `_gateway_device`), as a classic stat/device
# row, in the hyphenated spelling. `rx_bytes_r` (underscore) is the form unifi_core reads.
GATEWAY = {
    "mac": "AA:BB:CC:00:00:10", "name": "UDM", "model": "UDM-Pro", "type": "ugw",
    "ip": "10.0.0.1", "state": 1, "internet": True,
    "uplink": {"name": "eth8", "ip": "203.0.113.7", "up": True, "speed": 1000,
               "rx_bytes": 10_000, "tx_bytes": 5_000, "rx_bytes-r": 1_250_000,
               "tx_bytes-r": 625_000},
    "network_table": [
        {"_id": "n30", "name": "IoT", "vlan": 30, "vlan_enabled": True,
         "ip_subnet": "10.0.30.1/24", "purpose": "corporate"},
        {"_id": "n1", "name": "LAN", "vlan_enabled": False, "ip_subnet": "10.0.0.1/24"},
        {"_id": "bad"}, "junk"],
    "x_authkey": "DEVICE-SECRET-AUTHKEY",
}
HEALTH = [{"subsystem": "lan", "status": "ok"},
          {"subsystem": "wan", "status": "ok", "wan_ip": "192.0.2.9", "latency": 12,
           "uptime": 3600, "gw_name": ["UDM"]}]
WLANS = [
    {"_id": "w1", "name": "HomeWiFi", "enabled": True, "security": "wpapsk",
     "networkconf_id": "n1", "ap_group_mode": "all", "ap_group_ids": ["g1"], "is_guest": False,
     "wlan_band": "both", "hide_ssid": False, "mac_filter_enabled": False,
     "x_passphrase": "WLAN-SECRET"},
    {"_id": "w2", "name": "IoT", "enabled": True, "security": "wpapsk", "networkconf_id": "n30",
     "ap_group_mode": "specific", "ap_group_ids": ["g2"], "is_guest": False, "wlan_band": "2g",
     "hide_ssid": True, "mac_filter_enabled": True, "mac_filter_policy": "allow",
     "schedule_enabled": True},
    {"_id": "w3", "name": "Guest", "enabled": False, "security": "open", "is_guest": True,
     "wlan_band": "5g", "schedule": ["mon|0800-1700"]},
    {"_id": "w4"}, {"name": "no id"}, "junk",
]


# ---------------------------------------------------------- Integration: gateway and statistics

def test_select_gateway_prefers_a_declared_role_over_name_tokens():
    rows = [device(1, name="UDM-Pro"), device(2, type="gateway", name="Edge"), "junk"]
    assert select_gateway(rows)["id"] == "dev-2"
    assert select_gateway([device(1, name="Dream Machine"), device(2)])["id"] == "dev-1"
    assert select_gateway([device(1, model="UCG-Fiber")])["id"] == "dev-1"
    assert select_gateway([device(1, deviceType="Console")])["id"] == "dev-1"
    assert select_gateway([device(1, role="ugw")])["id"] == "dev-1"
    for token in ("udm", "ucg", "uxg", "udr", "udw"):
        assert select_gateway([device(2), device(3, model=token.upper() + "-X")])["id"] == "dev-3"
    assert select_gateway([device(1), device(2)]) is None
    assert select_gateway([]) is None


def test_uplink_statistics_are_parsed_with_either_envelope_and_degrade_to_unknown():
    body = {"uptimeSec": 10, "uplink": {"rxRateBps": 1500, "txRateBps": 250, "name": "eth8"}}
    assert parse_uplink_stats(body) == {"rx_rate_bps": 1500.0, "tx_rate_bps": 250.0,
                                        "port": "eth8", "up": None}
    wrapped = parse_uplink_stats({"data": {"uplink": {"rxRateBps": 1, "txRateBps": 2, "up": True}}})
    assert wrapped == {"rx_rate_bps": 1.0, "tx_rate_bps": 2.0, "port": "", "up": True}
    empty = {"rx_rate_bps": None, "tx_rate_bps": None, "port": "", "up": None}
    assert parse_uplink_stats(None) == empty
    assert parse_uplink_stats({"uplink": "x"}) == empty
    assert parse_uplink_stats({"uplink": {"up": "yes", "rxRateBps": "fast"}}) == empty
    assert parse_uplink_stats([]) == empty


def test_device_counters_are_searched_in_the_ha_soc_order_and_never_zero():
    assert counters({}) == {"rx_bytes": None, "tx_bytes": None, "rx_rate_bps": None,
                            "tx_rate_bps": None}
    nested = counters({"statistics": {"uplink": {"rxBytes": 7, "txBytes": 8, "rxRateBps": 9}}})
    assert nested == {"rx_bytes": 7, "tx_bytes": 8, "rx_rate_bps": 9.0, "tx_rate_bps": None}
    top = counters({"rxBytes": 1, "statistics": {"rxBytes": 2, "txBytes": 3}})
    assert (top["rx_bytes"], top["tx_bytes"]) == (1, None)  # the first node with a value wins
    assert counters({"rx_bytes": True, "tx_bytes": "9"})["rx_bytes"] is None
    d = parse_device("s", device(5, type="switch", role="", features=["SWITCHING", 3],
                                statistics={"rxBytes": 100, "txBytes": 200, "rxRateBps": 5.5}))
    assert (d.device_type, d.role, d.features) == ("switch", "", ("SWITCHING",))
    assert (d.rx_bytes, d.tx_bytes, d.rx_rate_bps, d.tx_rate_bps) == (100, 200, 5.5, None)
    assert parse_device("s", device(6, deviceType="ap")).device_type == "ap"
    assert parse_device("s", device(7)).features == ()


# ------------------------------------------------------------------ classic: gateway and WAN

def test_classic_gateway_row_gives_networks_and_the_wan_uplink_in_both_spellings():
    (gw,) = parse_devices([GATEWAY])
    assert gw["type"] == "ugw" and gw["model"] == "UDM-Pro" and gw["state"] == 1
    assert gw["networks"] == [
        {"id": "n30", "name": "IoT", "vlan": 30, "ip_subnet": "10.0.30.1/24"},
        {"id": "n1", "name": "LAN", "vlan": None, "ip_subnet": "10.0.0.1/24"}]
    assert gw["wan"] == {"ip": "203.0.113.7", "up": True, "port": "eth8",
                         "rx_rate_bps": 1_250_000.0, "tx_rate_bps": 625_000.0,
                         "rx_bytes": 10_000, "tx_bytes": 5_000, "internet": True}
    underscore = {**GATEWAY, "uplink": {"name": "eth8", "rx_bytes_r": 11, "tx_bytes_r": 12}}
    assert parse_wan_uplink(underscore)["rx_rate_bps"] == 11.0
    assert parse_wan_uplink(underscore)["tx_rate_bps"] == 12.0
    assert parse_wan_uplink({"uplink": {"rx_bytes-r": 1, "rx_bytes_r": 2}})["rx_rate_bps"] == 1.0
    assert parse_wan_uplink({})["up"] is None and parse_wan_uplink({"uplink": "x"})["ip"] == ""
    assert parse_networks({"network_table": "x"}) == []
    assert "x_authkey" not in json.dumps(gw)


def test_the_gateway_is_picked_among_classic_rows_by_type_then_tokens():
    sw = {"mac": "aa:bb:cc:00:00:01", "name": "Switch", "type": "usw", "model": "USW-24"}
    assert select_gateway_row(parse_devices([sw, GATEWAY]))["name"] == "UDM"
    named = {"mac": "aa:bb:cc:00:00:02", "name": "Dream Router", "type": "", "model": "UDR"}
    assert select_gateway_row(parse_devices([sw, named]))["name"] == "Dream Router"
    assert select_gateway_row(parse_devices([sw])) is None


def test_wan_status_takes_the_health_row_first_and_the_uplink_for_the_rest():
    (gw,) = parse_devices([GATEWAY])
    got = wan_status(parse_wan_health(HEALTH), gw)
    assert got == {"up": True, "ip": "192.0.2.9", "port": "eth8", "rx_rate_bps": 1_250_000.0,
                   "tx_rate_bps": 625_000.0, "latency_ms": 12.0, "gateways": ["UDM"]}
    down = wan_status({"status": "error", "wan_ip": "", "latency_ms": None, "uptime_s": None,
                       "gateways": []}, gw)
    assert down["up"] is False and down["ip"] == "203.0.113.7"  # the uplink fills the address
    only_uplink = wan_status(None, gw)
    assert only_uplink["up"] is True and only_uplink["latency_ms"] is None
    no_internet = wan_status(None, parse_devices([{**GATEWAY, "internet": "yes",
                                                   "uplink": {"up": False}}])[0])
    assert no_internet["up"] is False
    assert wan_status(None, None) == {"up": None, "ip": "", "port": "", "rx_rate_bps": None,
                                      "tx_rate_bps": None, "latency_ms": None, "gateways": []}
    assert OFFLINE_DEVICE_STATES >= {"DISCONNECTED", "HEARTBEAT_MISSED", "ISOLATED"}


# ------------------------------------------------------------------- classic: client fields

def test_uptime_follows_the_core_epoch_rule():
    assert uptime_to_seconds(3600, NOW) == 3600
    assert uptime_to_seconds(NOW - 7200, NOW) == 7200
    assert uptime_to_seconds("120", NOW) == 120
    assert uptime_to_seconds(None, NOW) is None
    assert uptime_to_seconds("junk", NOW) is None
    assert uptime_to_seconds(-5, NOW) is None


def test_resolve_client_vlan_direct_table_and_name_fallback():
    # The ha_Int_soc cases (tests/test_unifi_core.py), with the network name alongside.
    networks = [{"id": "n30", "name": "IoT", "vlan": 30}, {"id": "n1", "name": "LAN", "vlan": None}]
    assert resolve_client_vlan({"vlan": 7}, networks) == (7, "")
    assert resolve_client_vlan({"vlan": 7, "network": "Lab"}, networks) == (7, "Lab")
    assert resolve_client_vlan({"network_id": "n30"}, networks) == (30, "IoT")
    assert resolve_client_vlan({"network": "IoT"}, networks) == (30, "IoT")
    assert resolve_client_vlan({"network_id": "n1"}, networks) == (None, "LAN")
    assert resolve_client_vlan({"network": "Guest"}, networks) == (None, "Guest")
    assert resolve_client_vlan({}, networks) == (None, "")
    assert resolve_client_vlan({"vlan": "30"}, networks) == (None, "")  # a string is not a VLAN


def test_client_rates_choose_the_wired_or_wireless_pair_in_either_spelling():
    # The ha_Int_soc client_bandwidth cases, hyphenated as the console most likely sends them.
    wireless = {"is_wired": False, "rx_bytes-r": 1000.0, "tx_bytes-r": 500.0,
                "wired-rx_bytes-r": 1.0, "wired-tx_bytes-r": 2.0}
    assert client_rates(wireless, False) == (1000.0, 500.0)
    wired = {"is_wired": True, "rx_bytes-r": 1.0, "tx_bytes-r": 2.0,
             "wired-rx_bytes-r": 300.0, "wired-tx_bytes-r": 100.0}
    assert client_rates(wired, True) == (300.0, 100.0)
    # The wired bug: a client flagged wired carrying only the wireless pair still gets a value.
    assert client_rates({"rx_bytes-r": 42.0, "tx_bytes-r": 7.0}, True) == (42.0, 7.0)
    assert client_rates({"rx_bytes_r": 42, "tx_bytes_r": "7"}, False) == (42.0, 7.0)
    assert client_rates({}, False) == (None, None)
    assert client_rates({"rx_bytes-r": "fast"}, None) == (None, None)
    assert client_totals({"rx_bytes": 10, "tx_bytes": 20}, False) == (10, 20)
    assert client_totals({"wired-rx_bytes": 30, "rx_bytes": 10}, True) == (30, None)
    assert client_totals({"wired_rx_bytes": 30, "wired_tx_bytes": 40}, True) == (30, 40)


def test_active_clients_carry_vlan_network_uptime_and_counters():
    rows = [{"mac": "02:00:00:00:00:01", "is_wired": False, "ap_mac": "AA:BB:CC:00:00:02",
             "essid": "HomeWiFi", "vlan": 30, "network": "IoT", "network_id": "n30",
             "uptime": NOW - 600, "rx_bytes-r": 1000, "tx_bytes-r": 500, "rx_bytes": 9, "tx_bytes": 8},
            {"mac": "02:00:00:00:00:02", "is_wired": True, "sw_mac": "AA:BB:CC:00:00:01",
             "sw_port": 3, "uptime": 42, "wired-rx_bytes-r": 7, "wired-tx_bytes-r": 6}]
    got = parse_active_clients(rows, NOW)
    a, b = got["02:00:00:00:00:01"], got["02:00:00:00:00:02"]
    assert (a["vlan"], a["network"], a["network_id"], a["uptime_s"]) == (30, "IoT", "n30", 600)
    assert (a["rx_rate_bps"], a["tx_rate_bps"], a["rx_bytes"], a["tx_bytes"]) == (1000.0, 500.0, 9, 8)
    assert (b["vlan"], b["network"], b["uptime_s"]) == (None, "", 42)
    assert (b["rx_rate_bps"], b["tx_rate_bps"], b["rx_bytes"]) == (7.0, 6.0, None)
    assert parse_active_clients(rows)["02:00:00:00:00:02"]["uptime_s"] == 42  # no clock: raw


# --------------------------------------------------------------------------------- SSIDs

def test_wlanconf_rows_become_readiness_rows_without_the_passphrase():
    got = parse_wlans(WLANS)
    assert [w["name"] for w in got] == ["Guest", "HomeWiFi", "IoT"]  # sorted, malformed dropped
    home, iot, guest = got[1], got[2], got[0]
    assert home == {"id": "w1", "name": "HomeWiFi", "enabled": True, "security": "wpapsk",
                    "network_id": "n1", "ap_group_mode": "all", "ap_group_ids": ["g1"],
                    "guest": False, "band": "both", "hidden": False, "mac_filter": "off",
                    "scheduled": None}
    assert (iot["mac_filter"], iot["hidden"], iot["scheduled"], iot["band"]) == ("allow", True, True, "2g")
    assert (guest["enabled"], guest["guest"], guest["scheduled"], guest["network_id"]) == (
        False, True, True, "")
    assert parse_wlans([{"_id": "w", "name": "n", "mac_filter_enabled": True}])[0]["mac_filter"] == "on"
    assert "WLAN-SECRET" not in json.dumps(got)
    resolved = resolve_wlans(got, parse_networks(GATEWAY))
    assert [(w["network_name"], w["vlan"]) for w in resolved] == [("", None), ("LAN", None), ("IoT", 30)]
    assert all(w["ap_names"] == [] for w in resolved)


# ------------------------------------------------------------------- collectors and tables

def site_status(env: Any) -> dict[str, Any] | None:
    db = sqlite3.connect(env.path)
    try:
        cur = db.execute("SELECT * FROM unifi_site_status")
        row = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], row)) if row else None
    finally:
        db.close()


def table(env: Any, sql: str) -> list[tuple]:
    db = sqlite3.connect(env.path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def test_devices_poll_stores_the_gateway_uplink_rates_and_device_fields(tmp_path):
    gw = device(1, type="gateway", name="UDM", features=["GATEWAY", "SWITCHING"],
                statistics={"uplink": {"rxBytes": 10, "txBytes": 20}})
    console = Full([], devices=[gw, device(2, features=["ACCESS_POINT"])])
    console.stats["dev-1"] = {"uptimeSec": 5, "uplink": {"rxRateBps": 1500, "txRateBps": 250,
                                                         "name": "eth8", "up": True}}
    env = env_with(tmp_path, console)
    assert run(env.plugin.collect_devices(env.store)) == 2
    got = site_status(env)
    assert got["gateway_device_id"] == "dev-1" and got["wan_port"] == "eth8"
    assert (got["wan_rx_rate_bps"], got["wan_tx_rate_bps"], got["internet_up"]) == (1500.0, 250.0, 1)
    assert got["updated"] == env.now and got["classic_updated"] is None and got["wan_ip"] == ""
    paths = [r.url.path for r in console.requests]
    assert paths.count(f"/proxy/network/integration/v1/sites/site-1/devices/dev-1/statistics/latest") == 1
    assert not any(p.endswith("dev-2/statistics/latest") for p in paths)  # only the gateway
    rows = table(env, "SELECT device_id, device_type, features, rx_bytes, tx_bytes FROM unifi_devices "
                      "ORDER BY device_id")
    assert rows == [("dev-1", "gateway", '["GATEWAY", "SWITCHING"]', 10, 20),
                    ("dev-2", "", '["ACCESS_POINT"]', None, None)]


def test_a_refused_statistics_read_leaves_the_wan_unknown_and_keeps_the_poll(tmp_path):
    console = Full([], devices=[device(1, name="UDM-Pro")])  # no stats entry: 404
    env = env_with(tmp_path, console)
    assert run(env.plugin.collect_devices(env.store)) == 1
    got = site_status(env)
    assert got["gateway_device_id"] == "dev-1"
    assert (got["wan_rx_rate_bps"], got["wan_tx_rate_bps"], got["internet_up"]) == (None, None, None)
    console = Full([], devices=[device(1)])  # no gateway at all: no statistics request
    env = env_with(tmp_path, console)
    run(env.plugin.collect_devices(env.store))
    assert site_status(env)["gateway_device_id"] == ""
    assert not any(r.url.path.endswith("statistics/latest") for r in console.requests)


def test_a_device_id_that_is_not_a_plain_token_never_reaches_a_request_path(tmp_path):
    console = Full([], devices=[device(1, id="../../admin", type="gateway")])
    env = env_with(tmp_path, console)
    run(env.plugin.collect_devices(env.store))
    assert not any("statistics" in r.url.path for r in console.requests)


def test_classic_poll_writes_wan_networks_and_ssids_and_clients_resolve_their_vlan(tmp_path):
    console = Full([client_row(1, type="WIRELESS", uplinkDeviceId=""),
                    client_row(2, type="WIRED", uplinkDeviceId="")],
                   devices=[device(1, type="gateway", name="UDM"), device(2)])
    console.classic_devices = [GATEWAY]
    console.health = HEALTH
    console.wlans = WLANS
    console.active = [
        {"mac": "02:00:00:00:00:01", "is_wired": False, "ap_mac": "AA:BB:CC:00:00:02",
         "essid": "IoT", "network_id": "n30", "uptime": 90, "rx_bytes-r": 100, "tx_bytes-r": 50},
        {"mac": "02:00:00:00:00:02", "is_wired": True, "sw_mac": "AA:BB:CC:00:00:01",
         "sw_port": 3, "network": "LAN", "wired-rx_bytes": 7, "wired-tx_bytes": 8}]
    env = env_with(tmp_path, console, classic_credential="classic")
    run(env.plugin.collect_devices(env.store))
    assert run(env.plugin.collect_classic(env.store)) == 1
    got = site_status(env)
    assert got["gateway_device_id"] == "dev-1"  # the Integration column is kept
    assert (got["internet_up"], got["wan_ip"], got["wan_port"]) == (1, "192.0.2.9", "eth8")
    assert (got["wan_rx_rate_bps"], got["wan_tx_rate_bps"]) == (1_250_000.0, 625_000.0)
    assert got["wan_latency_ms"] == 12.0 and got["classic_updated"] == env.now
    assert [n["name"] for n in json.loads(got["networks"])] == ["IoT", "LAN"]
    wl = table(env, "SELECT wlan_id, name, enabled, network_name, vlan, mac_filter, hidden, "
                    "scheduled, ap_names FROM unifi_wlans ORDER BY wlan_id")
    assert wl == [("w1", "HomeWiFi", 1, "LAN", None, "off", 0, None, "[]"),
                  ("w2", "IoT", 1, "IoT", 30, "allow", 1, 1, "[]"),
                  ("w3", "Guest", 0, "", None, "", None, 1, "[]")]
    assert run(env.plugin.collect_clients(env.store)) == 2
    rows = table(env, "SELECT client_id, vlan, network, uptime_s, rx_rate_bps, tx_rate_bps, "
                      "rx_bytes, tx_bytes FROM unifi_clients ORDER BY client_id")
    assert rows == [("02:00:00:00:00:01", 30, "IoT", 90, 100.0, 50.0, None, None),
                    ("02:00:00:00:00:02", None, "LAN", None, None, None, 7, 8)]
    # A later classic poll with a shorter SSID list drops the row that left.
    console.wlans = WLANS[:1]
    env.now += 120
    run(env.plugin.collect_classic(env.store))
    assert table(env, "SELECT wlan_id FROM unifi_wlans") == [("w1",)]
    # The Integration rates win over the classic ones once both exist.
    console.stats["dev-1"] = {"uplink": {"rxRateBps": 1, "txRateBps": 2}}
    run(env.plugin.collect_devices(env.store))
    run(env.plugin.collect_classic(env.store))
    assert (site_status(env)["wan_rx_rate_bps"], site_status(env)["wan_tx_rate_bps"]) == (1.0, 2.0)
    sent = [(r.method, r.url.path) for r in console.requests]
    assert all(m == "GET" or p.startswith("/api/auth/") for m, p in sent)
    assert "x_passphrase" not in json.dumps(table(env, "SELECT * FROM unifi_wlans"))


def test_the_classic_poll_before_any_integration_poll_writes_no_site_rows(tmp_path):
    console = Full([], devices=[device(1)])
    console.classic_devices = [GATEWAY]
    console.wlans = WLANS
    env = env_with(tmp_path, console, classic_credential="classic")
    assert run(env.plugin.collect_classic(env.store)) == 1
    assert site_status(env) is None and table(env, "SELECT * FROM unifi_wlans") == []


def test_a_failed_classic_poll_keeps_the_stored_client_detail_for_the_new_columns(tmp_path):
    console = Full([client_row(1, type="WIRELESS", uplinkDeviceId="")])
    console.active = [{"mac": "02:00:00:00:00:01", "is_wired": False, "essid": "IoT", "vlan": 30,
                       "uptime": 90, "rx_bytes-r": 100, "tx_bytes-r": 50}]
    env = env_with(tmp_path, console, classic_credential="classic")
    run(env.plugin.collect_clients(env.store))
    env.now += 300
    console.classic_status = 500
    env.plugin.classic = None
    run(env.plugin.collect_clients(env.store))
    rows = table(env, "SELECT vlan, uptime_s, rx_rate_bps, tx_rate_bps FROM unifi_clients")
    assert rows == [(30, 90, 100.0, 50.0)]
    env.now += 300
    console.classic_status = 200
    console.active = [{"mac": "02:00:00:00:00:01", "is_wired": False, "essid": "IoT"}]
    env.plugin.classic = None
    run(env.plugin.collect_clients(env.store))
    assert table(env, "SELECT vlan, uptime_s, rx_rate_bps FROM unifi_clients") == [(None, None, None)]


def test_without_the_classic_account_the_new_columns_stay_unknown(tmp_path):
    console = Full([client_row(1)], devices=[device(1, type="gateway")])
    env = env_with(tmp_path, console)
    run(env.plugin.collect_devices(env.store))
    run(env.plugin.collect_clients(env.store))
    rows = table(env, "SELECT vlan, network, uptime_s, rx_rate_bps, rx_bytes, enriched FROM unifi_clients")
    assert rows == [(None, "", None, None, None, 0)]
    got = site_status(env)
    assert got["classic_updated"] is None and got["wan_ip"] == "" and got["networks"] == ""
    assert table(env, "SELECT * FROM unifi_wlans") == []
    assert not any(r.url.path.startswith("/proxy/network/api/") for r in console.requests)


def test_a_console_without_wlanconf_still_completes_the_classic_poll(tmp_path):
    console = Full([], devices=[device(1)])
    console.classic_devices = [GATEWAY]
    console.wlans = None  # type: ignore[assignment]  # the view answers 404
    env = env_with(tmp_path, console, classic_credential="classic")
    run(env.plugin.collect_devices(env.store))
    assert run(env.plugin.collect_classic(env.store)) == 1
    assert site_status(env)["wan_ip"] == "203.0.113.7"  # the uplink address, no health row
    assert table(env, "SELECT * FROM unifi_wlans") == []


@pytest.mark.parametrize("state", sorted(OFFLINE_DEVICE_STATES))
def test_offline_states_are_the_core_names(state):
    assert state.isupper()
