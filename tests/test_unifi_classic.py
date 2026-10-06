"""The optional classic UniFi client: the transport spy that allows only login, logout and GETs,
secret hygiene, the 401 re-login rule and backoff, and fixture parsing. The console is an httpx
mock transport. The classic field names in the fixtures are unverified against a live console."""

from __future__ import annotations

import asyncio
import logging
from importlib.metadata import EntryPoint
from typing import Any

import httpx
import pytest

from observe.plugins import GROUP, load_plugins
from observe.store import Store
from observe_unifi import UniFiPlugin, plugin as unifi_plugin
from observe_unifi.classic import (MAX_BODY_BYTES, ClassicBackedOff, ClassicClient, parse_devices,
                                   parse_offline_clients, parse_wan_health)
from observe_unifi.client import AuthRejected, UniFiError

from .conftest import make_config

PASSWORD = "pw-secret-classic"
COOKIE = "cookie-secret-token"
CSRF = "csrf-secret-token"
SITE = "default"
PREFIX = f"/proxy/network/api/s/{SITE}/"

# Unverified shapes: port_table, poe_power, poe_class, native_vlan, tagged_vlan_mgmt,
# lldp_table, uplink and the stat/health wan row.
DEVICE = {
    "mac": "AA:BB:CC:00:00:01", "name": "Switch 1",
    "port_table": [
        {"port_idx": 1, "name": "Port 1", "up": True, "poe_enable": True, "poe_power": "6.42",
         "poe_class": "Class 3", "native_vlan": 10, "native_networkconf_id": "net-10",
         "tagged_vlan_mgmt": "custom", "excluded_networkconf_ids": ["net-20"]},
        {"port_idx": 2, "up": False, "poe_power": "0.00"},
        {"port_idx": "bad"},
        "junk",
    ],
    "lldp_table": [{"local_port_idx": 1, "chassis_id": "DD:EE:FF:00:00:02",
                    "port_id": "Gi0/1", "chassis_name": "core"}, {"chassis_id": "x"}],
    "uplink": {"uplink_mac": "11:22:33:44:55:66", "port_idx": 25, "uplink_remote_port": 7},
}
HEALTH = [{"subsystem": "lan", "status": "ok"},
          {"subsystem": "wan", "status": "ok", "wan_ip": "192.0.2.9", "latency": 12,
           "uptime": 3600, "gw_name": ["UDM"]}]
KNOWN = [{"mac": "AA:AA:AA:00:00:01", "name": "Phone", "last_seen": 1700000000},
         {"mac": "AA:AA:AA:00:00:02", "hostname": "tv"}]
ACTIVE = [{"mac": "aa:aa:aa:00:00:02"}]
DATA = {"stat/device": [DEVICE], "stat/sta": ACTIVE, "rest/user": KNOWN, "stat/health": HEALTH}


def run(coro):
    return asyncio.run(coro)


class Console:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.login_status = 200
        self.reject_gets = 0  # the next N GETs answer 401
        self.status_override: int | None = None
        self.location: str | None = None
        self.logins = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        p = request.url.path
        if request.method == "POST" and p == "/api/auth/login":
            if self.login_status != 200:
                return httpx.Response(self.login_status, json={})
            self.logins += 1
            return httpx.Response(200, json={}, headers={
                "set-cookie": f"TOKEN={COOKIE}; path=/; secure; httponly", "x-csrf-token": CSRF})
        if request.method == "POST" and p == "/api/auth/logout":
            return httpx.Response(200, json={})
        if request.method == "GET" and p.startswith(PREFIX):
            if self.status_override is not None:
                headers = {"Location": self.location} if self.location else {}
                return httpx.Response(self.status_override, headers=headers, json={})
            if self.reject_gets > 0:
                self.reject_gets -= 1
                return httpx.Response(401, json={})
            assert request.headers["x-csrf-token"] == CSRF
            assert COOKIE in request.headers["cookie"]
            rows = DATA.get(p[len(PREFIX):])
            if rows is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": rows})
        return httpx.Response(404, json={})


def client(console: Console, now: list[float] | None = None) -> ClassicClient:
    t = now if now is not None else [1000.0]
    return ClassicClient("https://console.invalid", "viewer", PASSWORD, SITE, False, None, 5.0,
                         httpx.MockTransport(console), lambda: t[0], 120.0)


def test_only_login_logout_and_gets_are_ever_sent():
    console = Console()
    c = client(console)

    async def go():
        for path in ("stat/device", "stat/sta", "rest/user", "stat/health"):
            await c.get(path)
        await c.logout()

    run(go())
    sent = [(r.method, r.url.path) for r in console.requests]
    assert sent[0] == ("POST", "/api/auth/login") and sent[-1] == ("POST", "/api/auth/logout")
    for method, path in sent[1:-1]:
        assert method == "GET" and path.startswith(PREFIX)
    assert {m for m, _ in sent} == {"GET", "POST"}
    assert sum(1 for m, _ in sent if m == "POST") == 2
    assert console.logins == 1  # the session is reused


def test_a_path_outside_the_four_reads_is_refused_before_any_request():
    console = Console()
    c = client(console)
    for bad in ("rest/networkconf", "../../x", "cmd/devmgr", "stat/device/../x"):
        with pytest.raises(UniFiError, match="not allowed"):
            run(c.get(bad))
    assert console.requests == []


def test_a_bad_site_name_is_refused():
    with pytest.raises(UniFiError):
        ClassicClient("https://c.invalid", "u", "p", "../x", True, None, 5.0)


def test_secrets_never_reach_logs_errors_or_repr(caplog):
    caplog.set_level(logging.DEBUG)
    console = Console()
    console.login_status = 401
    c = client(console)
    with pytest.raises(AuthRejected) as err:
        run(c.get("stat/device"))
    text = " ".join([str(err.value), repr(err.value), repr(c), caplog.text])
    ok = Console()
    c2 = client(ok)
    run(c2.get("stat/device"))
    text += " " + repr(c2) + caplog.text
    bad = Console()
    bad.status_override = 500
    c3 = client(bad)
    with pytest.raises(httpx.HTTPStatusError) as err3:
        run(c3.get("stat/device"))
    text += str(err3.value)
    for secret in (PASSWORD, COOKIE, CSRF):
        assert secret not in text


def test_a_401_triggers_one_relogin_and_a_retry():
    console = Console()
    c = client(console)
    run(c.get("stat/device"))
    console.reject_gets = 1
    assert run(c.get("stat/sta")) == ACTIVE
    assert console.logins == 2


def test_a_second_401_backs_off_and_sends_nothing_until_the_pause_ends():
    console = Console()
    now = [1000.0]
    c = client(console, now)
    run(c.get("stat/device"))
    console.reject_gets = 2  # the GET and the retry after the re-login
    with pytest.raises(AuthRejected):
        run(c.get("stat/sta"))
    assert console.logins == 2
    sent = len(console.requests)
    with pytest.raises(ClassicBackedOff):
        run(c.get("stat/sta"))
    now[0] += 119
    with pytest.raises(ClassicBackedOff):
        run(c.get("stat/sta"))
    assert len(console.requests) == sent
    now[0] += 2  # past the 120 s pause
    assert run(c.get("stat/sta")) == ACTIVE
    assert console.logins == 3


def test_a_rejected_login_backs_off_and_doubles():
    console = Console()
    console.login_status = 403
    now = [0.0]
    c = client(console, now)
    with pytest.raises(AuthRejected):
        run(c.get("stat/device"))
    now[0] = 121
    with pytest.raises(AuthRejected):
        run(c.get("stat/device"))
    now[0] = 121 + 239  # the second pause is 240 s
    with pytest.raises(ClassicBackedOff):
        run(c.get("stat/device"))
    assert len(console.requests) == 2


def test_redirects_are_not_followed_and_oversize_is_refused():
    console = Console()
    c = client(console)
    run(c.get("stat/device"))
    console.status_override, console.location = 302, "https://elsewhere.invalid/x"
    with pytest.raises(UniFiError, match="redirect"):
        run(c.get("stat/device"))
    assert all(r.url.host == "console.invalid" for r in console.requests)

    big = ClassicClient("https://console.invalid", "u", "p", SITE, False, None, 5.0,
                        httpx.MockTransport(lambda r: httpx.Response(
                            200, headers={"x-csrf-token": "t", "set-cookie": "TOKEN=a"},
                            content=b" " * (MAX_BODY_BYTES + 1))
                            if r.method == "POST" else httpx.Response(
                                200, content=b" " * (MAX_BODY_BYTES + 1))))
    with pytest.raises(UniFiError, match="larger"):
        run(big.get("stat/device"))


def test_logout_forgets_the_session_even_when_the_console_is_down():
    def boom(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/logout":
            raise httpx.ConnectError("down")
        return Console()(request)

    c = ClassicClient("https://console.invalid", "u", PASSWORD, SITE, False, None, 5.0,
                      httpx.MockTransport(boom))
    run(c.login())
    run(c.logout())
    assert c._cookie is None and c._csrf is None


def test_fixture_parsing_of_poe_vlan_lldp_and_uplink():
    (dev,) = parse_devices([DEVICE, {"name": "no mac"}, "junk"])
    assert dev["mac"] == "aa:bb:cc:00:00:01"
    p1 = dev["ports"]["1"]
    assert p1["poe_w"] == 6.42 and p1["poe_class"] == "Class 3" and p1["poe_enabled"] is True
    assert p1["native_vlan"] == 10 and p1["tagged_mode"] == "custom"
    assert p1["excluded_network_ids"] == ["net-20"]
    assert dev["ports"]["2"]["poe_w"] == 0.0 and dev["ports"]["2"]["native_vlan"] is None
    assert set(dev["ports"]) == {"1", "2"}
    assert dev["lldp"] == [{"local_port": 1, "chassis_id": "dd:ee:ff:00:00:02",
                            "port_id": "Gi0/1", "name": "core"}]
    assert dev["uplink"] == {"mac": "11:22:33:44:55:66", "local_port": 25, "remote_port": 7}


def test_fixture_parsing_of_wan_health_and_offline_clients():
    assert parse_wan_health(HEALTH) == {"status": "ok", "wan_ip": "192.0.2.9", "latency_ms": 12.0,
                                        "uptime_s": 3600.0, "gateways": ["UDM"]}
    assert parse_wan_health([{"subsystem": "lan"}]) is None
    assert parse_offline_clients(KNOWN, ACTIVE) == [
        {"mac": "aa:aa:aa:00:00:01", "name": "Phone", "last_seen": 1700000000.0}]


# ---------------------------------------------------------------- through the plugin

CREDS = {"unifi": {"type": "unifi", "api_key": "key-secret-value"},
         "classic": {"type": "unifi_classic", "username": "viewer", "password": PASSWORD}}


@pytest.fixture(autouse=True)
def reset_plugin():
    yield
    unifi_plugin.transport = None
    unifi_plugin.classic = None
    unifi_plugin.clock = UniFiPlugin().clock


def plugin_env(tmp_path, with_classic: bool):
    s = {"host": "console.invalid", "credential": "unifi", "verify_tls": False}
    if with_classic:
        s["classic_credential"] = "classic"
    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}], plugins=["unifi"],
                      plugin_settings={"unifi": s}, credentials=CREDS,
                      server={"db_path": str(tmp_path / "w.db")})
    loaded = load_plugins(cfg, lambda: [EntryPoint("unifi", "observe_unifi:plugin", GROUP)])
    Store(str(tmp_path / "w.db"), loaded)
    return loaded.get("unifi").plugin


def test_plugin_snapshot_logs_in_reads_and_logs_out(tmp_path):
    console = Console()
    p = plugin_env(tmp_path, True)
    p.transport = httpx.MockTransport(console)

    async def go():
        snap = await p.classic_snapshot()
        await p.close()
        return snap

    snap = run(go())
    assert snap["wan"]["wan_ip"] == "192.0.2.9"
    assert [o["mac"] for o in snap["offline_clients"]] == ["aa:aa:aa:00:00:01"]
    assert snap["devices"][0]["uplink"]["local_port"] == 25
    methods = [(r.method, r.url.path) for r in console.requests]
    assert methods.count(("POST", "/api/auth/login")) == 1
    assert methods[-1] == ("POST", "/api/auth/logout")
    assert all(m == "GET" or path.startswith("/api/auth/") for m, path in methods)


def test_plugin_without_a_classic_credential_sends_nothing(tmp_path):
    console = Console()
    p = plugin_env(tmp_path, False)
    p.transport = httpx.MockTransport(console)
    assert run(p.classic_snapshot()) == {}
    run(p.close())
    assert console.requests == []
