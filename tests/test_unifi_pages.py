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
API = "/api/plugins/unifi"
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
    for path in ("/devices", "/clients", "/protect"):
        r = env.client.get(API + path)
        assert r.status_code == 401 and "www-authenticate" not in r.headers
    js = (PKG / "static" / "unifi.js").read_text(encoding="utf-8")
    assert 'location.assign("/login")' in js and "whoami()" in js
    env.login()
    for path in ("/devices", "/clients", "/protect"):
        assert env.client.get(API + path).status_code == 200


def test_the_devices_page_marks_data_stale_after_twice_the_interval(env):
    env.login()
    plugin = env.inner.plugin
    interval = plugin.settings.interval
    got = env.client.get(API + "/devices").json()
    assert got["stale"] is False and got["last_update"] == 1000.0
    env.inner.now = 1000.0 + 2 * interval - 1
    assert env.client.get(API + "/devices").json()["stale"] is False
    env.inner.now = 1000.0 + 2 * interval + 1
    got = env.client.get(API + "/devices").json()
    assert got["stale"] is True and got["last_update"] == 1000.0
    js = (PKG / "static" / "unifi.js").read_text(encoding="utf-8")
    assert "d.stale" in js and "d.last_update" in js
    run(plugin.collect_devices(env.store))  # a good poll clears the marker
    assert env.client.get(API + "/devices").json()["stale"] is False


def test_navigation_entry_sits_under_network(env):
    env.login()
    body = env.client.get("/api/v2/plugins").json()
    mine = next(p for p in body["items"] if p["name"] == "unifi")
    assert {"label": "UniFi", "path": PAGE, "workspace": "network"} in mine["nav"]


def test_routes_return_devices_clients_and_cameras(env):
    env.login()
    devices = env.client.get(API + "/devices").json()["devices"]
    assert len(devices) == 2
    clients = env.client.get(API + "/clients").json()
    assert clients["total"] == 7 and clients["classic_configured"] is True
    states = {c["mac"]: c["connected"] for c in clients["clients"]}
    assert states["02:00:00:00:07:07"] is False and states["02:00:00:00:00:01"] is True
    cams = env.client.get(API + "/protect").json()
    assert cams["enabled"] is True and cams["cameras"][0]["recording"] is True


def test_hostile_strings_are_json_data_and_the_script_never_writes_markup(env):
    env.login()
    for path in ("/devices", "/clients", "/protect"):
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
    for needed in ("sortableTable", "statusChip", "kpi-row", "windowFor", "filterClients"):
        assert needed in js
    css = (PKG / "static" / "unifi.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", css)


def test_package_data_lists_the_page_and_script_files():
    text = (PKG.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "pages/*.html" in text and "static/*.js" in text and "static/*.css" in text


def test_protect_tab_is_explained_when_protect_is_off(tmp_path):
    e = Env(tmp_path, Full([]), protect=False)
    try:
        e.login()
        assert e.client.get(API + "/protect").json() == {"cameras": [], "enabled": False}
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
    for path in ("/devices", "/clients", "/cameras", "/devices/a/b"):
        r = env.client.get("/api/v2/unifi" + path)
        assert r.status_code == 401, path
    env.login()
    ops = {r.operation_id for r in env.client.app.state.v2_runtime.resources if r.owner == "unifi"}
    assert ops == {"unifi_devices", "unifi_device", "unifi_clients", "unifi_cameras"}


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
