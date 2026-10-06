"""Home Assistant, UniFi Network and Protect, and Technitium against local
fakes in tests/fakes/servers.py that emit the documented response shapes."""

import pytest
import yaml

from observe.checks import build_check
from observe.checks.base import Result
from observe.config import Config
from observe.discovery import discover

from .fakes.servers import (HA_TOKEN, TECH_TOKEN, UNIFI_KEY, ha_routes, json_server,
                            leaf_cert, technitium_routes, unifi_routes)

CREDS = {
    "ha": {"type": "homeassistant", "token": HA_TOKEN},
    "habad": {"type": "homeassistant", "token": "wrong"},
    "unifi": {"type": "unifi", "api_key": UNIFI_KEY},
    "unifibad": {"type": "unifi", "api_key": "wrong"},
    "dns": {"type": "technitium", "token": TECH_TOKEN},
    "dnsbad": {"type": "technitium", "token": "wrong"},
}


def check(**mon):
    cfg = Config.model_validate({"defaults": {"timeout": 5}, "credentials": CREDS,
                                 "monitors": [{"name": "x", **mon}]})
    return build_check(cfg.monitors[0], cfg)


@pytest.fixture
def cert(tmp_path):
    return leaf_cert(tmp_path)


# ------------------------------------------------------------ Home Assistant


@pytest.fixture
def ha(cert):
    srv = json_server(ha_routes(), ("Authorization", f"Bearer {HA_TOKEN}"), cert)
    yield srv, cert[0]
    srv.shutdown()


def hcheck(ha, **kw):
    srv, ca = ha
    kw.setdefault("credential", "ha")
    return check(type="homeassistant", host="localhost", port=srv.server_address[1],
                 ca_bundle=ca, **kw)


async def test_ha_api_and_bad_token(ha):
    assert (await hcheck(ha, mode="api").run()).result is Result.OK
    bad = await hcheck(ha, mode="api", credential="habad").run()
    assert bad.result is Result.FAIL and "401" in bad.message


async def test_ha_entity_numeric_expect_and_unavailable(ha):
    t = await hcheck(ha, mode="entity", entity_id="sensor.nas_temp",
                     thresholds={"direction": "above", "warn": 40, "crit": 50}).run()
    assert t.result is Result.WARN and t.value == 41.5 and "°C" in t.message
    door = await hcheck(ha, mode="entity", entity_id="binary_sensor.front_door",
                        expect=["on"]).run()
    assert door.result is Result.FAIL and "expected" in door.message
    unk = await hcheck(ha, mode="entity", entity_id="light.hall").run()
    assert unk.result is Result.FAIL and "unknown" in unk.message
    missing = await hcheck(ha, mode="entity", entity_id="sensor.nope").run()
    assert missing.result is Result.FAIL and "404" in missing.message


async def test_ha_unavailable_filters_and_updates(ha):
    allu = await hcheck(ha, mode="unavailable").run()
    assert allu.value == 2
    ign = await hcheck(ha, mode="unavailable", ignore=["media_player.zone*"]).run()
    assert ign.value == 1 and ign.detail["unavailable"] == ["sensor.phyn_flow"]
    dom = await hcheck(ha, mode="unavailable", domains=["media_player"]).run()
    assert dom.detail["unavailable"] == ["media_player.zone4"]
    upd = await hcheck(ha, mode="updates").run()
    assert upd.result is Result.WARN and upd.value == 1


def test_ha_entity_id_is_validated():
    with pytest.raises(ValueError):
        check(type="homeassistant", host="h", credential="ha", mode="entity",
              entity_id="../api/services")


# ------------------------------------------------------------------- UniFi


@pytest.fixture
def unifi(cert):
    srv = json_server(unifi_routes(page_size=2), ("X-API-KEY", UNIFI_KEY), cert)
    yield srv, cert[0]
    srv.shutdown()


def ucheck(u, kind="unifi_network", **kw):
    srv, ca = u
    kw.setdefault("credential", "unifi")
    return check(type=kind, host="localhost", port=srv.server_address[1], ca_bundle=ca, **kw)


async def test_unifi_devices_summary_pages_and_ignore(unifi):
    res = await ucheck(unifi, mode="devices").run()
    assert res.result is Result.FAIL and "3/4 devices online" in res.message
    assert "Remote Flex" in res.message
    ok = await ucheck(unifi, mode="devices", ignore=["Remote Flex"]).run()
    assert ok.result is Result.OK and "3/3" in ok.message
    srv, _ = unifi
    assert any("offset=2" in p for p, _ in srv.seen)          # second page was requested


async def test_unifi_device_modes(unifi):
    gw = await ucheck(unifi, mode="device", device="Gateway").run()
    assert gw.result is Result.OK
    by_mac = await ucheck(unifi, mode="device", device="AA-BB-CC-00-00-02").run()
    assert by_mac.result is Result.OK and "Core Switch" in by_mac.message
    cpu = await ucheck(unifi, mode="device_cpu", device="Gateway").run()
    assert cpu.value == 23.4
    mem = await ucheck(unifi, mode="device_memory", device="Gateway").run()
    assert mem.value == 61.0
    off = await ucheck(unifi, mode="device", device="Remote Flex").run()
    assert off.result is Result.FAIL and "OFFLINE" in off.message
    fw = await ucheck(unifi, mode="firmware").run()
    assert fw.result is Result.WARN and "Core Switch" in fw.message


async def test_unifi_bad_key_and_unknown_site(unifi):
    bad = await ucheck(unifi, mode="devices", credential="unifibad").run()
    assert bad.result is Result.FAIL and "401" in bad.message
    site = await ucheck(unifi, mode="devices", site="Branch").run()
    assert site.result is Result.FAIL and "Default" in site.message


async def test_protect_cameras(unifi):
    cams = await ucheck(unifi, "unifi_protect", mode="cameras").run()
    assert cams.result is Result.FAIL and "Doorbell (DISCONNECTED)" in cams.message
    one = await ucheck(unifi, "unifi_protect", mode="camera", camera="Driveway").run()
    assert one.result is Result.OK
    info = await ucheck(unifi, "unifi_protect", mode="info").run()
    assert info.result is Result.OK and "6.2.88" in info.message


# -------------------------------------------------------------- Technitium


@pytest.fixture
def tech():
    srv = json_server(technitium_routes())
    yield srv
    srv.shutdown()


def tcheck(srv, **kw):
    kw.setdefault("credential", "dns")
    return check(type="technitium", host="127.0.0.1", port=srv.server_address[1], **kw)


async def test_technitium_stats_uses_bearer_header_not_url(tech):
    res = await tcheck(tech, mode="stats",
                       thresholds={"direction": "above", "warn": 2, "crit": 10}).run()
    assert res.result is Result.WARN and res.value == 3.0 and "42 blocked" in res.message
    path, headers = tech.seen[-1]
    assert "token=" not in path and headers.get("Authorization") == f"Bearer {TECH_TOKEN}"


async def test_technitium_legacy_query_token_and_bad_token(tech):
    legacy = await tcheck(tech, mode="stats", token_in_query=True).run()
    assert legacy.result is Result.OK and "token=" in tech.seen[-1][0]
    bad = await tcheck(tech, mode="stats", credential="dnsbad").run()
    assert bad.result is Result.FAIL and "invalid-token" in bad.message


async def test_technitium_update(tech):
    res = await tcheck(tech, mode="update").run()
    assert res.result is Result.WARN and "13.6 -> 14.0" in res.message


# --------------------------------------------------------------- discovery


def disc_cfg(creds, **extra):
    return Config.model_validate({"credentials": CREDS, "discovery": {
        "credentials": creds, "tcp_ports": [], "timeout": 2, **extra}})


async def test_discovery_unifi_network_and_protect(unifi):
    srv, ca = unifi
    cfg = disc_cfg(["unifi"], https_api_port=srv.server_address[1], ca_bundle=ca)
    text, _ = await discover(cfg, ["localhost"])
    ms = yaml.safe_load(text)["monitors"]
    summary = next(m for m in ms if m.get("mode") == "devices")
    assert summary["ignore"] == ["Remote Flex"]                # offline at discovery time
    per_device = {m["device"] for m in ms if m.get("mode") == "device"}
    assert per_device == {"Gateway", "Core Switch", "Hall AP"}
    assert {m["camera"] for m in ms if m.get("mode") == "camera"} == {"Driveway", "Doorbell"}
    merged = cfg.model_dump(exclude_none=True)
    merged["monitors"] = ms
    Config.model_validate(merged)


async def test_discovery_ha_over_https(ha):
    srv, ca = ha
    cfg = disc_cfg(["ha"], homeassistant_port=srv.server_address[1], ca_bundle=ca)
    text, _ = await discover(cfg, ["localhost"])
    modes = {m["mode"] for m in yaml.safe_load(text)["monitors"]
             if m["type"] == "homeassistant"}
    assert modes == {"api", "unavailable", "updates"}


async def test_discovery_withholds_token_on_plain_http_unless_allowed(tech):
    port = tech.server_address[1]
    text, report = await discover(disc_cfg(["dns"], technitium_port=port), ["127.0.0.1"])
    assert not any(m["type"] == "technitium" for m in yaml.safe_load(text)["monitors"] or [])
    assert not any("Authorization" in h for _, h in tech.seen)  # nothing was sent
    note = report["hosts"][0]["notes"]
    assert any("plain HTTP" in n and "not sent" in n for n in note)

    text, _ = await discover(disc_cfg(["dns"], technitium_port=port,
                                      api_require_valid_tls=False), ["127.0.0.1"])
    ms = [m for m in yaml.safe_load(text)["monitors"] if m["type"] == "technitium"]
    assert {m["mode"] for m in ms} == {"stats", "update"}
    assert all(m.get("https") is None for m in ms)             # default (false) not repeated


async def test_unifi_ports_mode_feeds_live_port(unifi):
    from observe.infra_match import LivePort
    res = await ucheck(unifi, mode="ports", device="Core Switch").run()
    assert res.result is Result.OK and "1/2 ports up" in res.message
    ports = res.detail["ports"]
    assert ports["1"]["speed_mbps"] == 1000 and ports["1"]["max_speed_mbps"] == 2500
    assert ports["1"]["state"] == "UP" and ports["1"]["poe"]["enabled"] is True
    assert ports["2"]["speed_mbps"] is None
    # The same lookup web.live_port performs for a unifi match.
    p = ports["1"]
    live = LivePort(p.get("speed_mbps"), p.get("vlan"), p.get("poe_w"))
    assert live.speed_mbps == 1000 and live.vlan is None and live.poe_w is None


async def test_unifi_ports_requires_device_and_offline(unifi):
    with pytest.raises(ValueError):
        check(type="unifi_network", host="h", credential="unifi", mode="ports")
    off = await ucheck(unifi, mode="ports", device="Remote Flex").run()
    assert off.result is Result.FAIL


async def test_unifi_ports_refuses_redirect_and_oversize(cert, monkeypatch):
    from observe.checks import apps
    base = "/proxy/network/integration/v1"
    sid = "site-1"
    routes = unifi_routes()
    sid = [r for r in routes if r.endswith("/devices")][0].split("/")[-2]
    dev = f"{base}/sites/{sid}/devices/dev-2"
    srv = json_server({**routes, dev: lambda q, h: (302, {"location": "http://127.0.0.1:1/x"})},
                      ("X-API-KEY", UNIFI_KEY), cert)
    try:
        res = await ucheck((srv, cert[0]), mode="ports", device="Core Switch").run()
        assert res.result is Result.FAIL and "redirect" in res.message
        assert not any(p.startswith("/x") for p, _ in srv.seen)
    finally:
        srv.shutdown()
    monkeypatch.setattr(apps, "UNIFI_DETAIL_MAX_BYTES", 50)
    srv = json_server(routes, ("X-API-KEY", UNIFI_KEY), cert)
    try:
        res = await ucheck((srv, cert[0]), mode="ports", device="Core Switch").run()
        assert res.result is Result.FAIL and "larger than 50 bytes" in res.message
    finally:
        srv.shutdown()


# ------------------------------------------------------------ body and page caps on every read


async def test_core_unifi_list_read_refuses_an_oversize_body(cert, monkeypatch):
    from observe.checks import apps
    monkeypatch.setattr(apps, "MAX_API_BODY", 300)
    srv = json_server(unifi_routes(), ("X-API-KEY", UNIFI_KEY), cert)
    try:
        res = await ucheck((srv, cert[0]), mode="devices").run()
        assert res.result is Result.FAIL and "larger than 300 bytes" in res.message
    finally:
        srv.shutdown()


async def test_core_unifi_list_read_stops_at_the_page_cap(cert, monkeypatch):
    from observe.checks import apps
    monkeypatch.setattr(apps, "MAX_UNIFI_PAGES", 3)
    base = "/proxy/network/integration/v1"
    routes = unifi_routes()
    sid = [r for r in routes if r.endswith("/devices")][0].split("/")[-2]
    endless = {"offset": 0, "limit": 1, "count": 1, "totalCount": 1_000_000_000,
               "data": [{"id": "d", "name": "x", "state": "ONLINE"}]}
    srv = json_server({**routes, f"{base}/sites/{sid}/devices": endless},
                      ("X-API-KEY", UNIFI_KEY), cert)
    try:
        res = await ucheck((srv, cert[0]), mode="devices").run()
        assert res.result is Result.FAIL and "more than 3 pages" in res.message
        assert sum(1 for p, _ in srv.seen if "/devices?" in p) == 3
    finally:
        srv.shutdown()


async def test_core_unifi_list_read_refuses_a_redirect(cert):
    base = "/proxy/network/integration/v1"
    srv = json_server({**unifi_routes(),
                       f"{base}/sites": lambda q, h: (302, {"location": "http://127.0.0.1:1/x"})},
                      ("X-API-KEY", UNIFI_KEY), cert)
    try:
        res = await ucheck((srv, cert[0]), mode="devices").run()
        assert res.result is Result.FAIL and "redirect" in res.message
    finally:
        srv.shutdown()


@pytest.mark.parametrize("mode,extra", [("api", {}), ("unavailable", {}), ("updates", {}),
                                        ("entity", {"entity_id": "sensor.nas_temp"})])
async def test_every_ha_mode_refuses_an_oversize_body(ha, monkeypatch, mode, extra):
    from observe.checks import apps
    monkeypatch.setattr(apps, "MAX_HA_BODY", 20)
    res = await hcheck(ha, mode=mode, **extra).run()
    assert res.result is Result.FAIL and "larger than 20 bytes" in res.message
