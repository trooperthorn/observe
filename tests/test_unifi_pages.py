"""The UniFi page: static shells, a login for the data, navigation, hostile strings, the static
guard rules for its files, and the windowing and filter rules the table script uses."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config
from .test_auth import Clock
from .test_unifi_clients import Full, client_row, run
from .test_unifi_plugin import CREDS, Env as PluginEnv

PASSWORD = "correct horse battery"
ROOT = Path(__file__).parent.parent
PKG = ROOT / "plugins" / "unifi" / "observe_unifi"
HOSTILE = '<img src=x onerror="alert(1)">&"\'</script>'
API = "/api/v2/unifi"
PAGE = "/plugins/unifi"


class Env:
    def __init__(self, tmp_path: Any, console: Full, **settings: Any) -> None:
        self.inner = PluginEnv(tmp_path, console, {"classic_credential": "classic", "protect": True,
                                                   **settings})
        self.inner.plugin.transport = httpx.MockTransport(console)
        self.store = self.inner.store
        sched = Scheduler(self.inner.cfg, self.store, Alerter(self.inner.cfg))
        self.cfg = make_config(
            [{"name": "p", "type": "ping", "host": "127.0.0.1"}], plugins=["unifi"],
            plugin_settings=self.inner.cfg.plugin_settings, credentials=CREDS,
            server={"db_path": self.inner.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
                    "argon2_parallelism": 1})
        self.client = TestClient(
            create_app(self.cfg, self.store, sched, Alerter(self.cfg), plugins=self.inner.loaded,
                       auth_clock=Clock()), base_url="https://testserver")

    def collect(self) -> None:
        p = self.inner.plugin
        run(p.collect_devices(self.store))
        run(p.collect_clients(self.store))
        run(p.collect_protect(self.store))

    def login(self) -> None:
        run(auth.create_user(self.store, self.cfg, "bob", PASSWORD, False))
        r = self.client.post("/api/login", json={"username": "bob", "password": PASSWORD})
        assert r.status_code == 200

    def close(self) -> None:
        self.client.close()
        self.store.close()


@pytest.fixture
def env(tmp_path):
    console = Full([client_row(i) for i in range(5)] + [client_row(9, name=HOSTILE)])
    console.cameras = [{"id": "cam-1", "name": HOSTILE, "state": "CONNECTED",
                        "isConnected": True, "isRecording": True}]
    console.devices = [{**d, "name": HOSTILE if d["id"] == "dev-1" else d["name"]}
                       for d in console.devices]
    console.known = [{"mac": "02:00:00:00:07:07", "name": "Printer", "last_seen": 999.0}]
    e = Env(tmp_path, console)
    e.collect()
    yield e
    e.close()


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


def test_the_page_is_a_static_shell_and_the_data_needs_a_login(env):
    r = env.client.get(PAGE)
    assert r.status_code == 200 and "unifi.js" in r.text
    assert "Client 1" not in r.text and "AA:BB:CC" not in r.text  # no data in the shell
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert env.client.get("/plugins/unifi/static/unifi.js").status_code == 200
    assert env.client.get("/plugins/unifi/static/vlist-core.js").status_code == 200
    for path in ("/status", "/devices", "/clients", "/cameras"):
        assert env.client.get(API + path).status_code == 401
    js = (PKG / "static" / "unifi.js").read_text(encoding="utf-8")
    assert "/api/v2/unifi" in js and "/api/plugins" not in js and "whoami()" in js
    env.login()
    for path in ("/status", "/devices", "/clients", "/cameras"):
        assert env.client.get(API + path).status_code == 200
    for path in ("/devices", "/clients", "/protect"):  # the old page routes are gone
        assert env.client.get("/api/plugins/unifi" + path).status_code == 404


def test_the_status_marks_devices_stale_after_twice_the_interval(env):
    env.login()
    plugin = env.inner.plugin
    interval = plugin.settings.interval
    got = env.client.get(API + "/status").json()
    assert got["devices_stale"] is False and got["devices_updated"].endswith("Z")
    env.inner.now = 1000.0 + 2 * interval - 1
    assert env.client.get(API + "/status").json()["devices_stale"] is False
    env.inner.now = 1000.0 + 2 * interval + 1
    got = env.client.get(API + "/status").json()
    assert got["devices_stale"] is True and got["now"].endswith("Z")
    js = (PKG / "static" / "unifi.js").read_text(encoding="utf-8")
    assert "devices_stale" in js and "devices_updated" in js
    run(plugin.collect_devices(env.store))  # a good poll clears the marker
    assert env.client.get(API + "/status").json()["devices_stale"] is False


def test_the_status_carries_the_stale_windows_and_the_settings(env):
    env.login()
    s = env.inner.plugin.settings
    got = env.client.get(API + "/status").json()
    assert got["clients_stale_after"] == 2.5 * s.clients_interval
    assert got["protect_stale_after"] == 2.5 * s.protect_interval
    assert got["protect_enabled"] is True and got["classic_configured"] is True
    assert "etag" not in env.client.get(API + "/status").headers  # it depends on the clock


def test_navigation_entry_sits_under_network(env):
    env.login()
    body = env.client.get("/api/v2/plugins").json()
    mine = next(p for p in body["items"] if p["name"] == "unifi")
    assert {"label": "UniFi", "path": PAGE, "workspace": "network"} in mine["nav"]


def test_resources_return_devices_clients_and_cameras(env):
    env.login()
    devices = env.client.get(API + "/devices").json()["items"]
    assert len(devices) == 2
    clients = env.client.get(API + "/clients").json()["items"]
    assert len(clients) == 7
    states = {c["mac"]: c["connected"] for c in clients}
    assert states["02:00:00:00:07:07"] is False and states["02:00:00:00:00:01"] is True
    cams = env.client.get(API + "/cameras").json()["items"]
    assert cams[0]["recording"] is True


def test_hostile_strings_are_json_data_and_the_script_never_writes_markup(env):
    env.login()
    for path in ("/devices", "/clients", "/cameras"):
        r = env.client.get(API + path)
        assert r.headers["content-type"].startswith("application/json")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert HOSTILE in strings(r.json()), path
    sinks = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|"
                       r"new Function|setAttribute\(\s*[\"']on|setAttribute\(\s*[\"']style|\.style\b")
    for name in ("unifi.js", "vlist-core.js"):
        js = (PKG / "static" / name).read_text(encoding="utf-8")
        assert not sinks.search(js), name
    js = (PKG / "static" / "unifi.js").read_text(encoding="utf-8")
    assert "textContent" in js or 'el("' in js  # text goes in through the el helper
    html = (PKG / "pages" / "unifi.html").read_text(encoding="utf-8")
    assert not re.search(r"\sstyle\s*=|\son[a-z]+\s*=|javascript:|<style|https?://", html, re.I)


def test_the_page_uses_shared_components_and_tokens_only():
    html = (PKG / "pages" / "unifi.html").read_text(encoding="utf-8")
    for href in ("/static/css/components.css", "/static/css/admin.css",
                 "/plugins/unifi/static/unifi.css"):
        assert f'href="{href}"' in html
    assert 'class="admin-main"' in html and "/static/js/shell.js" in html
    assert "<title>UniFi - Observe</title>" in html
    js = (PKG / "static" / "unifi.js").read_text(encoding="utf-8")
    for needed in ("sortableTable", "statusChip", "kpi-row", "windowFor", "filterClients",
                   "formatValue", "neutralChip", "monoTag"):
        assert needed in js
    css = (PKG / "static" / "unifi.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", css)
    tokens = (ROOT / "observe" / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
    for name in set(re.findall(r"var\((--[\w-]+)", css)):
        assert re.search(rf"{re.escape(name)}\s*:", tokens), name  # every token exists in both themes


# ---- the network view (slice 3): what the page builds, read from the sources ----------------

def _js(name: str) -> str:
    text = (PKG / "static" / name).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)(^|\s)//.*$", r"\1", text)


def test_the_page_builds_the_five_tiles_from_the_overview_resource():
    js = _js("unifi.js")
    assert f'get(`${{API}}/overview`)' in js
    assert '"kpi-row five"' in js
    for label in ("Network status", "Internet", "WAN bandwidth", "Wireless clients", "Total clients"):
        assert f'"{label}"' in js, label
    assert "`site ${o.site_id}`" in js and "`WAN ${o.wan.ip}`" in js
    assert "`${o.wired_clients} wired`" in js and "`${o.device_count} network devices`" in js
    # Status words are chips with an icon, never colour alone.
    assert 'statusChip("up", "Online")' in js and 'statusChip("down", "Offline")' in js
    assert 'statusChip("up", "Connected")' in js and 'statusChip("down", "Down")' in js
    css = (PKG / "static" / "unifi.css").read_text(encoding="utf-8")
    assert ".kpi-row.five" in css and "repeat(5, minmax(0, 1fr))" in css


def test_rates_are_formatted_as_rates_and_totals_as_bytes_with_the_shared_formatter():
    js = _js("unifi.js")
    # The HA SOC view formatted per-second rates as byte totals; here a rate is bytes per
    # second shown in bits per second and a total is bytes, both through format.js.
    assert 'formatValue(bytesPerSecond * 8, "bit/s")' in js
    assert 'formatValue(v, "By")' in js
    assert 'formatValue(s, "s")' in js  # uptime as a duration
    assert "uptimeSeconds(c, now)" in js
    assert "toFixed" not in js  # no private byte or rate formatter


def test_clients_per_ssid_bars_need_no_inline_style_and_set_the_table_filter():
    js = _js("unifi.js")
    assert 'el("progress")' in js and "bar.max = max" in js and "bar.value = r.count" in js
    assert '"ssid-name"' in js and 'setAttribute("aria-pressed"' in js
    assert "applySsid(ssidFilter)" in js and 'location.hash = "clients"' in js
    assert "Clients per SSID" in js and "click to filter the table" in js


def test_clients_tab_has_the_vlan_and_ssid_filters_and_the_eight_columns():
    js = _js("unifi.js")
    assert 'COLS = ["Client", "IPv4", "MAC", "VLAN", "SSID", "Uptime", "Bandwidth", "Last seen"]' in js
    assert 'select("VLAN"' in js and 'select("SSID"' in js and 'select("State"' in js
    assert "vlanOptions(all)" in js and "ssidOptions(all)" in js
    assert "filterClients(all, { q: q.value, state: state.value, vlan: vlan.value, ssid: ssid.value })" in js
    assert '"wireless"' in js  # the subline word
    assert "windowFor(scroll.scrollTop" in js  # still virtualised
    assert "const DASH = \"—\"" in js  # the em dash of a classic-only value


def test_devices_tab_has_search_the_firmware_words_totals_and_25_50_100_pages():
    js = _js("unifi.js")
    assert "DEVICE_PAGES = [25, 50, 100]" in js and "pageSizes: DEVICE_PAGES" in js
    for label in ('"Device"', '"IPv4"', '"MAC"', '"VLAN"', '"Model"', '"Firmware"', '"Bandwidth"',
                  '"Last seen"'):
        assert label in js, label
    assert 'statusChip("warn", "Update available")' in js
    assert 'statusChip("up", "Up to date")' in js
    assert 'statusChip("unavailable", "Not reported")' in js
    assert "Search devices" in js
    # The management VLAN is not read, so the column says so instead of showing zero.
    assert "management VLAN" in js.lower() or "management VLAN is not read" in js
    core = (ROOT / "observe" / "static" / "js" / "table-core.js").read_text(encoding="utf-8")
    assert "export function pageSlice(rows, page, size, sizes = PAGE_SIZES)" in core
    table = (ROOT / "observe" / "static" / "js" / "table.js").read_text(encoding="utf-8")
    assert "pageSlice(sorted, page, size, sizes)" in table


def test_wifi_join_section_copies_the_ha_soc_intro_and_builds_one_card_per_ssid():
    js = _js("unifi.js")
    intro = ("No UniFi source records an association attempt or an authentication failure, so "
             "nothing here says a client failed. What it shows is the configuration that decides "
             "whether a join is permitted, which access points carry each SSID, and the wireless "
             "clients the controller knows but is not carrying now.")
    assert intro in js
    assert '"Wi-Fi Join Diagnostics"' in js and '"Known but not connected"' in js
    for label in ('"Network"', '"Radios"', '"Permitted APs"', '"Carrying clients now"'):
        assert label in js, label
    assert "`${w.network_name} (VLAN ${w.vlan})`" in js
    assert 'statusChip("up", "Enabled")' in js and 'statusChip("down", "Disabled")' in js
    assert 'neutralChip("Guest")' in js and "neutralChip(w.security)" in js
    assert 'el("li", `finding ${f.severity}`)' in js and 'el("span", "sev", f.severity)' in js
    assert "w.summary" in js and "defaultSsid(d.wlans)" in js
    assert f'get(`${{API}}/wlans`)' in js and f'getAll(`${{API}}/absent-clients`)' in js
    for col in ('"Client"', '"MAC"', '"Last SSID"', '"Last seen"'):
        assert col in js, col
    css = (PKG / "static" / "unifi.css").read_text(encoding="utf-8")
    for rule in (".finding.blocking .sev", ".finding.possible .sev", ".wifi-ssid", ".wifi-grid"):
        assert rule in css, rule


def test_every_classic_only_value_degrades_to_a_dash_and_the_note_explains_why():
    js = _js("unifi.js")
    assert "CLASSIC_NOTE" in js and "classic controller account" in js
    assert "if (!d.classic_configured) notes.push(note(CLASSIC_NOTE))" in js
    assert "if (!d.classic_configured) {" in js  # the Wi-Fi section
    assert "status.classic_configured" in js
    assert js.count("dash()") >= 6


def test_tabs_default_to_clients_and_keep_devices_wifi_and_protect():
    js = _js("unifi.js")
    assert 'TABS = [["clients", "Clients"], ["devices", "Devices"], ["wifi", "Wi-Fi join"], ["protect", "Protect"]]' in js
    assert 'return LOADERS[h] ? h : "clients"' in js


def test_package_data_lists_the_page_and_script_files():
    text = (PKG.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "pages/*.html" in text and "static/*.js" in text and "static/*.css" in text


def test_protect_tab_is_explained_when_protect_is_off(tmp_path):
    e = Env(tmp_path, Full([]), protect=False)
    try:
        e.login()
        assert e.client.get(API + "/status").json()["protect_enabled"] is False
        assert e.client.get(API + "/cameras").json()["items"] == []
    finally:
        e.close()


# ------------------------------------------------- windowing and filters (node, when present)

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_window_and_filter_rules_in_node():
    script = (
        f'import {{ windowFor, filterClients, attachment }} from "{(PKG / "static" / "vlist-core.js").as_uri()}";\n'
        "const rows = Array.from({ length: 500 }, (_, i) => ({ name: 'c' + i, mac: 'm' + i, ip: '', ssid: '',"
        " uplink_name: i % 2 ? 'AP' : 'SW', kind: i % 2 ? 'wireless' : 'wired', connected: i % 5 !== 0, stale: i === 1,"
        " sw_port: i % 2 ? null : 3 }));\n"
        "const w = windowFor(4000, 480, 40, 500);\n"
        "const top = windowFor(0, 480, 40, 500);\n"
        "const end = windowFor(99999, 480, 40, 500);\n"
        "console.log(JSON.stringify({ w, top, end, none: windowFor(0, 480, 40, 0),"
        " f: filterClients(rows, { q: 'C42' }).length, k: filterClients(rows, { kind: 'wired' }).length,"
        " s: filterClients(rows, { state: 'offline' }).length,"
        " st: filterClients(rows, { state: 'stale' }).length,"
        " both: filterClients(rows, { kind: 'wireless', state: 'connected', q: 'ap' }).length,"
        " a: attachment(rows[0]), b: attachment(rows[1]) }));\n")
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True,
                         text=True, timeout=60, check=True).stdout
    got = json.loads(out)
    assert got["w"] == {"start": 94, "end": 118, "top": 3760, "bottom": 15280}
    assert got["top"]["start"] == 0 and got["top"]["top"] == 0
    assert got["end"]["end"] == 500 and got["end"]["bottom"] == 0
    assert got["none"] == {"start": 0, "end": 0, "top": 0, "bottom": 0}
    assert got["f"] == 11 and got["k"] == 250 and got["s"] == 100
    assert got["st"] == 1  # a connected row flagged stale is not counted as connected
    assert got["both"] == 199 and got["a"] == "SW port 3" and got["b"] == "AP"


# ---- the same data on /api/v2/unifi -------------------------------------------------------

def test_v2_resources_are_mounted_by_the_plugin_and_need_a_credential(env):
    for path in ("/status", "/devices", "/clients", "/cameras", "/devices/a/b", "/overview",
                 "/wlans", "/absent-clients"):
        r = env.client.get("/api/v2/unifi" + path)
        assert r.status_code == 401, path
    env.login()
    ops = {r.operation_id for r in env.client.app.state.v2_runtime.resources if r.owner == "unifi"}
    assert ops == {"unifi_status", "unifi_devices", "unifi_device", "unifi_clients",
                   "unifi_cameras", "unifi_overview", "unifi_wlans", "unifi_absent_clients"}


def test_v2_devices_page_by_cursor_and_one_device_by_id(env):
    env.login()
    first = env.client.get("/api/v2/unifi/devices?limit=1").json()
    assert len(first["items"]) == 1 and first["next_cursor"]
    rest = env.client.get(f"/api/v2/unifi/devices?limit=1&cursor={first['next_cursor']}").json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    both = first["items"] + rest["items"]
    assert both[0]["device_id"] != both[1]["device_id"]
    assert both[0]["last_seen"].endswith("Z") and isinstance(both[0]["firmware_updatable"],
                                                             (bool, type(None)))
    one = both[0]
    got = env.client.get(f"/api/v2/unifi/devices/{one['site_id']}/{one['device_id']}")
    assert got.status_code == 200 and got.json() == one
    assert env.client.get("/api/v2/unifi/devices/nope/none").status_code == 404
    only = env.client.get(f"/api/v2/unifi/devices?site={one['site_id']}").json()["items"]
    assert {d["site_id"] for d in only} == {one["site_id"]}
    assert env.client.get("/api/v2/unifi/devices?site=nowhere").json()["items"] == []
    assert env.client.get("/api/v2/unifi/devices?limit=9999").status_code == 400


def test_v2_clients_filter_by_connection_and_text_and_keep_hostile_text_as_data(env):
    env.login()
    every = env.client.get("/api/v2/unifi/clients?limit=500").json()["items"]
    assert len(every) == 7
    on = env.client.get("/api/v2/unifi/clients?connected=true").json()["items"]
    off = env.client.get("/api/v2/unifi/clients?connected=false").json()["items"]
    assert on and off and len(on) + len(off) == len(every)
    assert all(c["connected"] is True for c in on) and all(c["connected"] is not True for c in off)
    hit = env.client.get("/api/v2/unifi/clients", params={"q": "<img"}).json()["items"]
    assert [c["name"] for c in hit] == [HOSTILE]
    assert env.client.get("/api/v2/unifi/clients", params={"q": "%"}).json()["items"] == []
    paged: list[str] = []
    cursor = None
    while True:
        url = "/api/v2/unifi/clients?limit=3" + (f"&cursor={cursor}" if cursor else "")
        page = env.client.get(url).json()
        paged += [c["client_id"] for c in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert paged == [c["client_id"] for c in every]
    r = env.client.get("/api/v2/unifi/clients?limit=1")
    assert r.headers["content-type"].startswith("application/json")
    assert env.client.get("/api/v2/unifi/clients?limit=1",
                          headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_v2_cameras(env):
    env.login()
    cams = env.client.get("/api/v2/unifi/cameras").json()["items"]
    assert [c["camera_id"] for c in cams] == ["cam-1"]
    assert cams[0]["recording"] is True and cams[0]["name"] == HOSTILE
