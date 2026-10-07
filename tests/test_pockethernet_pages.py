"""The Pockethernet report list, report detail and jack pages: login, navigation, hostile strings,
retention, and field findings produced by real uploads."""

from __future__ import annotations

import json
import re
from urllib.parse import quote
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.plugins import GROUP, load_plugins
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app
from observe_pockethernet.keys import create_field_key
from observe_pockethernet.reports import prune_evidence

from .conftest import make_config
from .test_auth import Clock
from .test_pockethernet_otlp import FIXTURE, TAKEN_S, URL, report_request, run

PASSWORD = "correct horse battery"
PKG = Path(__file__).parent.parent / "plugins" / "pockethernet" / "observe_pockethernet"
JACK = FIXTURE["site"]["port_id"]
HOSTILE = '<img src=x onerror="alert(1)">&"\'</script>'
V2 = "/api/v2/pockethernet"
PAGES = ["/plugins/pockethernet", "/plugins/pockethernet/report", "/plugins/pockethernet/jack"]


class Env:
    def __init__(self, tmp_path: Any, **server: Any) -> None:
        self.path = str(tmp_path / "w.db")
        self.cfg = make_config(
            [{"name": "p", "type": "ping", "host": "127.0.0.1"}],
            plugins=["pockethernet"], plugin_settings={"pockethernet": {}},
            server={"db_path": self.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
                    "argon2_parallelism": 1, **server})
        loaded = load_plugins(self.cfg, lambda: [EntryPoint(
            "pockethernet", "observe_pockethernet:plugin", GROUP)])
        self.store = Store(self.path, loaded)
        self.alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, self.alerter)
        self.clock = Clock()
        self.clock.now = TAKEN_S + 100
        self.client = TestClient(
            create_app(self.cfg, self.store, sched, self.alerter, plugins=loaded,
                       auth_clock=self.clock), base_url="https://testserver")
        self.key, _ = run(create_field_key(self.store, "sean-pixel"))

    def upload(self, body: dict[str, Any]) -> dict[str, Any]:
        r = self.client.post(URL, json=report_request(json.dumps(body).encode()),
                             headers={"Authorization": f"Bearer {self.key}"})
        assert r.status_code == 200, r.text
        assert r.json() == {}, r.text  # nothing was refused
        return r.json()

    def login(self, name: str = "bob", admin: bool = False) -> None:
        run(auth.create_user(self.store, self.cfg, name, PASSWORD, admin))
        r = self.client.post("/api/login", json={"username": name, "password": PASSWORD})
        assert r.status_code == 200

    def close(self) -> None:
        self.client.close()
        self.store.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def strings(value: Any) -> list[str]:
    """Every string anywhere in a decoded JSON value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


def report(report_id: str, **props: Any) -> dict[str, Any]:
    return {**FIXTURE, "report_id": report_id,
            "properties": {**FIXTURE["properties"], **props}}


def test_pages_are_static_shells_behind_the_core_and_the_data_needs_a_login(env):
    env.upload(FIXTURE)
    for path in PAGES:
        r = env.client.get(path)
        assert r.status_code == 200 and "pockethernet.js" in r.text
        assert FIXTURE["report_id"] not in r.text and "sean-pixel" not in r.text
        assert "default-src 'self'" in r.headers["content-security-policy"]
    assert env.client.get("/plugins/pockethernet/static/pockethernet.js").status_code == 200
    for path in ("/reports", "/reports/sean-pixel/x", "/jacks/x"):
        r = env.client.get(V2 + path)
        assert r.status_code == 401
    js = (PKG / "static" / "pockethernet.js").read_text(encoding="utf-8")
    assert "/api/v2/pockethernet" in js and "/api/plugins" not in js and "whoami()" in js
    assert env.client.get("/api/plugins/pockethernet/reports").status_code in (401, 404)
    env.login()
    assert env.client.get(V2 + "/reports").status_code == 200
    assert env.client.get("/api/plugins/pockethernet/reports").status_code == 404


def test_pages_ask_for_basic_auth_when_it_is_configured(tmp_path):
    e = Env(tmp_path, basic_auth_user="ops", basic_auth_password="pw")
    try:
        for path in PAGES:
            assert e.client.get(path).status_code == 401
        assert e.client.get(PAGES[0], auth=("ops", "pw")).status_code == 200
    finally:
        e.close()


def test_navigation_entry_comes_from_the_plugin_through_the_core(env):
    assert env.client.get("/api/v2/plugins").status_code == 401
    env.login()
    body = env.client.get("/api/v2/plugins").json()
    mine = next(p for p in body["items"] if p["name"] == "pockethernet")
    assert {"label": "Field reports", "path": "/plugins/pockethernet",
            "workspace": "network"} in mine["nav"]
    assert "shell-nav" in (Path(__file__).parent.parent / "observe" / "static"
                           / "index.html").read_text(encoding="utf-8")


def test_report_list_has_summary_ports_and_paging(env):
    env.upload(FIXTURE)
    env.clock.now += 60
    env.upload(report("second-report", link_speed_mbps=100))
    env.login()
    d = env.client.get(V2 + "/reports").json()
    first, second = d["items"]  # newest first
    assert second["report_id"] == FIXTURE["report_id"] and second["source"] == "sean-pixel"
    assert second["port_id"] == JACK and second["tester_serial"] == 1234567
    assert second["taken_at"].endswith("Z") and not second["body_pruned"]
    assert second["ports"][0]["port_key"] == "gi1/0/5"
    page = env.client.get(V2 + "/reports", params={"limit": 1}).json()
    assert [r["report_id"] for r in page["items"]] == ["second-report"] and page["next_cursor"]


def test_report_detail_returns_typed_sections_and_raw_steps_and_tool_results(env):
    env.upload(FIXTURE)
    env.login()
    d = env.client.get(V2 + f"/reports/sean-pixel/{FIXTURE['report_id']}").json()
    assert d["revision"] == 1 and d["ports"][0]["switch_id"].startswith("mac:")
    body = d["body"]
    assert body["link"]["speed_mbps"] == 1000 and body["poe"]["poe_class"] == 4
    assert body["steps"][0]["step"] == "WIREMAP" and body["tool_results"][0]["tool"] == "ping"
    assert env.client.get(V2 + f"/reports/other/{FIXTURE['report_id']}"
                          ).status_code == 404  # another phone's source is another report


def test_pruned_report_keeps_its_summary_and_has_no_body(env):
    env.upload(FIXTURE)
    env.login()
    run(prune_evidence(env.store, env.clock.now + 400 * 86400, 365))
    d = env.client.get(V2 + f"/reports/sean-pixel/{FIXTURE['report_id']}").json()
    assert d["body"] is None and d["body_pruned"] is True and d["status"] == "complete"


def test_jack_page_shows_current_port_history_links_and_reports(env):
    env.upload(FIXTURE)
    env.clock.now += 60
    moved = report("moved-report")
    n = json.loads(json.dumps(FIXTURE["neighbors"][0]))
    n["port_id"] = n["lldp"]["port_id"] = "Gi1/0/6"
    moved["neighbors"] = [n]
    env.upload(moved)
    env.login()
    d = env.client.get(V2 + f"/jacks/{JACK}").json()
    assert d["jack_key"] == JACK and d["room"] == "Room 204" and d["port_key"] == "gi1/0/6"
    assert [h["port_key"] for h in d["history"]] == ["gi1/0/6", "gi1/0/5"]
    states = {l["port"].split("|")[1]: l["closed_at"] for l in d["links"]}
    assert states["gi1/0/5"] is not None and states["gi1/0/6"] is None
    assert {r["report_id"] for r in d["reports"]} == {FIXTURE["report_id"], "moved-report"}
    assert env.client.get(V2 + "/jacks/nope").status_code == 404


def test_hostile_strings_are_json_data_and_the_scripts_never_write_markup(env):
    n = json.loads(json.dumps(FIXTURE["neighbors"][0]))
    n["system_name"] = n["lldp"]["system_name"] = HOSTILE
    hostile = {**FIXTURE, "report_id": "hostile-report", "notes": HOSTILE,
               "preset_name": HOSTILE, "neighbors": [n],
               "site": {**FIXTURE["site"], "room": HOSTILE, "port_id": HOSTILE.replace("\n", "")},
               "location_label": HOSTILE}
    env.upload(hostile)
    env.login()
    for path, query in ((V2 + "/reports", {}),
                        (V2 + "/reports/sean-pixel/hostile-report", {}),
                        (V2 + "/jacks/" + quote(HOSTILE, safe=""), {})):
        r = env.client.get(path, params=query)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/json")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert HOSTILE in strings(r.json()), path
    # The page files are fixed markup, and the script writes with textContent only.
    sinks = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|"
                       r"new Function|setAttribute\(\s*[\"']on|setAttribute\(\s*[\"']style")
    js = (PKG / "static" / "pockethernet.js").read_text(encoding="utf-8")
    assert not sinks.search(js) and "textContent" in js
    for name in ("reports", "report", "jack"):
        html = (PKG / "pages" / f"{name}.html").read_text(encoding="utf-8")
        assert not re.search(r"\sstyle\s*=|\son[a-z]+\s*=|javascript:|<style|https?://", html, re.I)
        scripts = re.findall(r"<script\b[^>]*>", html)
        assert scripts and all(re.search(r'src="/(static|plugins/pockethernet/static)/', s)
                               for s in scripts)


def test_package_data_lists_the_page_and_script_files():
    text = (PKG.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "pages/*.html" in text and "static/*.js" in text
    assert sorted(p.name for p in (PKG / "pages").glob("*.html")) == [
        "jack.html", "report.html", "reports.html"]


# Each field change finding, produced by two real uploads of the Pockethernet report.
UPLOADS = [
    ("speed_drop", {"link_speed_mbps": 100}),
    ("cable_fault", {"pair_fault": "pair 3-6 open"}),
    ("length_change", {"pair_1_2_length_m": 70.0}),
    ("poe_drop", {"poe_load_w": 1.0}),
    ("vlan_change", {"vlan": 40}),
    ("dhcp_fail", {"dhcp_ok": False}),
    ("verdict_worse", {"cable_verdict": "fail"}),
]


@pytest.mark.parametrize("kind,change", UPLOADS)
def test_second_report_with_a_worse_value_raises_the_finding_and_no_alert(
        env, monkeypatch, kind, change):
    async def no_alerts(*a: Any, **k: Any) -> None:
        raise AssertionError("a field finding must never call an alert target")
    monkeypatch.setattr(Alerter, "notify", no_alerts)
    env.upload(report("first-report", **({"pair_fault": "none"} if kind == "cable_fault" else {})))
    env.login()
    assert env.client.get("/api/v2/findings").json()["items"] == []
    env.clock.now += 60
    env.upload(report("second-report", **change))
    found = env.client.get("/api/v2/findings").json()["items"]
    assert [f["kind"] for f in found] == [kind]
    assert found[0]["switch_id"].startswith("mac:") and found[0]["port_key"] == "gi1/0/5"
    port = env.client.get(f"/api/v2/ports/{found[0]['switch_id']}/Gi1/0/5").json()
    assert [f["kind"] for f in port["findings"]] == [kind]
    assert env.alerter.status == {}


def test_pages_use_the_shared_components_and_the_plugin_stylesheet():
    for name in ("reports", "report", "jack"):
        html = (PKG / "pages" / f"{name}.html").read_text(encoding="utf-8")
        for href in ("/static/css/components.css", "/static/css/admin.css",
                     "/plugins/pockethernet/static/pockethernet.css"):
            assert f'href="{href}"' in html
        assert 'class="admin-main"' in html and "/static/js/shell.js" in html
    js = (PKG / "static" / "pockethernet.js").read_text(encoding="utf-8")
    for needed in ("sortableTable", "statusChip", "wiremap(", "kpi-row", "Uploaded by"):
        assert needed in js
    css = (PKG / "static" / "pockethernet.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", css)
    assert "static/*.css" in (PKG.parent / "pyproject.toml").read_text(encoding="utf-8")


def test_wiremap_labels_every_pair_by_number_and_colour_name():
    js = (PKG / "static" / "pockethernet.js").read_text(encoding="utf-8")
    for pair, colour in (("1-2", "orange"), ("3-6", "green"), ("4-5", "blue"), ("7-8", "brown")):
        assert f'pair: "{pair}"' in js and f'colour: "{colour}"' in js
    assert "Pair ${p.pair} ${p.colour}" in js


def test_report_list_carries_the_cable_verdict_for_the_fails_column(env):
    env.upload(FIXTURE)
    env.clock.now += 60
    env.upload(report("failing-report", cable_verdict="fail"))
    env.login()
    got = {r["report_id"]: r["verdict"] for r in env.client.get(V2 + "/reports").json()["items"]}
    assert got == {FIXTURE["report_id"]: "pass", "failing-report": "fail"}


# ---- the resources themselves -----------------------------------------------------------------


def test_v2_resources_need_a_credential_and_belong_to_the_plugin(env):
    for path in ("/reports", f"/reports/sean-pixel/{FIXTURE['report_id']}", "/jacks/x"):
        assert env.client.get(V2 + path).status_code == 401, path
    env.login()
    owned = {r.operation_id for r in env.client.app.state.v2_runtime.resources
             if r.owner == "pockethernet"}
    assert owned == {"pockethernet_reports", "pockethernet_report", "pockethernet_jack"}


def test_v2_report_list_pages_newest_first_and_shows_ports_and_verdict(env):
    env.upload(FIXTURE)
    env.clock.now += 60
    env.upload(report("second-report", link_speed_mbps=100))
    env.clock.now += 60
    env.upload(report("third-report", link_speed_mbps=100))
    env.login()
    first = env.client.get(V2 + "/reports?limit=2").json()
    assert [r["report_id"] for r in first["items"]] == ["third-report", "second-report"]
    assert first["next_cursor"]
    last = env.client.get(V2 + f"/reports?limit=2&cursor={first['next_cursor']}").json()
    assert [r["report_id"] for r in last["items"]] == [FIXTURE["report_id"]]
    assert last["next_cursor"] is None
    old = last["items"][0]
    assert old["source"] == "sean-pixel" and old["port_id"] == JACK
    assert old["ports"][0]["port_key"] == "gi1/0/5" and old["body_pruned"] is False
    assert old["taken_at"].endswith("Z") and old["received_at"].endswith("Z")
    assert env.client.get(V2 + "/reports?site=nowhere").json()["items"] == []
    assert env.client.get(V2 + "/reports?limit=100000").status_code == 400
    assert env.client.get(V2 + "/reports?cursor=garbage").status_code == 400


def test_v2_report_detail_body_and_a_pruned_report_has_none(env):
    env.upload(FIXTURE)
    env.login()
    url = V2 + f"/reports/sean-pixel/{FIXTURE['report_id']}"
    d = env.client.get(url).json()
    assert d["revision"] == 1 and d["body"]["link"]["speed_mbps"] == 1000
    assert d["ports"][0]["switch_id"].startswith("mac:")
    assert env.client.get(V2 + f"/reports/other/{FIXTURE['report_id']}").status_code == 404
    run(prune_evidence(env.store, env.clock.now + 400 * 86400, 365))
    d = env.client.get(url).json()
    assert d["body"] is None and d["body_pruned"] is True and d["status"] == "complete"


def test_v2_jack_has_history_links_and_reports(env):
    env.upload(FIXTURE)
    env.login()
    d = env.client.get(V2 + f"/jacks/{JACK}").json()
    assert d["jack_key"] == JACK and d["room"] == "Room 204"
    assert [h["port_key"] for h in d["history"]] == ["gi1/0/5"]
    assert env.client.get(V2 + "/jacks/nope").status_code == 404


def test_v2_report_strings_stay_json_and_an_upload_key_cannot_read(env):
    n = json.loads(json.dumps(FIXTURE["neighbors"][0]))
    n["system_name"] = n["lldp"]["system_name"] = HOSTILE
    env.upload({**FIXTURE, "report_id": "hostile-report", "notes": HOSTILE, "neighbors": [n]})
    r = env.client.get(V2 + "/reports", headers={"Authorization": f"Bearer {env.key}"})
    assert r.status_code == 401  # a field key reads nothing
    env.login()
    got = env.client.get(V2 + "/reports/sean-pixel/hostile-report")
    assert got.headers["content-type"].startswith("application/json")
    assert HOSTILE in strings(got.json())
