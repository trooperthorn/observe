"""The map, port and map admin pages: login, CSP-safe static markup, acknowledgement with CSRF
and admin, and hostile strings."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.checks.base import CheckResult
from observe.infra import InfraService
from observe.portkey import switch_id
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

PASSWORD = "correct horse battery"
STATIC = Path(__file__).parent.parent / "observe" / "static"
PAGES = {"/map": ["map.html", "pages/map.js"], "/port": ["port.html", "port.js"],
         "/admin/infra": ["infra-admin.html", "infra-admin.js"]}
HOSTILE = '<img src=x onerror="alert(1)">&"\'</script>'
SID = switch_id("aa:bb:cc:dd:ee:02")
MONITORS = [
    {"name": "edge sw", "type": "ping", "host": "10.0.0.2"},
    {"name": "edge gi5", "type": "snmp", "host": "10.0.0.2", "credential": "v2",
     "mode": "interface", "interface": "GigabitEthernet1/0/5"},
]


class Web:
    def __init__(self, tmp_path: Any) -> None:
        path = str(tmp_path / "w.db")
        self.store = Store(path)
        self.cfg = make_config(MONITORS, credentials={
            "v2": {"type": "snmpv2c", "community": "c"}},
            server={"db_path": path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
                    "argon2_parallelism": 1})
        self.sched = Scheduler(self.cfg, self.store, Alerter(self.cfg))
        self.client = TestClient(create_app(self.cfg, self.store, self.sched,
                                            Alerter(self.cfg)), base_url="https://testserver")
        self.infra = InfraService(self.store)

    async def login(self, name: str, admin: bool) -> dict[str, str]:
        await auth.create_user(self.store, self.cfg, name, PASSWORD, admin)
        r = self.client.post("/api/login", json={"username": name, "password": PASSWORD})
        return {"X-CSRF-Token": r.json()["csrf"]}

    async def seed(self, name: str = "edge") -> None:
        await self.infra.upsert_switch(SID, name=name, mgmt_addresses=["10.0.0.2"], now=1.0)
        await self.infra.upsert_port(SID, "Gi1/0/5", if_index=3, now=1.0)
        await self.infra.append_property(SID, "Gi1/0/5", "link_speed_mbps", 1000,
                                         unit="Mbit/s", source="field", report_id="r1",
                                         observed_at=5.0, now=6.0)
        self.sched.states["edge-gi5"].observe(CheckResult.ok("up", detail={"speed_mbps": 100}))

    def port(self, **extra: str) -> dict[str, Any]:
        r = self.client.get("/api/infra/port", params={"switch_id": SID, "port": "Gi1/0/5"})
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def web(tmp_path):
    w = Web(tmp_path)
    yield w
    w.client.close()
    w.store.close()


def test_pages_are_served_with_the_csp_and_hold_no_data(web):
    for path, files in PAGES.items():
        r = web.client.get(path)
        assert r.status_code == 200 and f'type="module" src="/static/{files[1]}"' in r.text
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert "script-src" not in r.headers["content-security-policy"]
        js = web.client.get(f"/static/{files[1]}")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]


async def test_data_behind_every_page_needs_a_login(web):
    await web.seed()
    for path in ("/api/infra/map", "/api/infra/port?switch_id=x&port=y",
                 "/api/infra/dependencies"):
        r = web.client.get(path)
        assert r.status_code == 401 and "www-authenticate" not in r.headers
    for path in ("/api/admin/infra/unlinked",):
        assert web.client.get(path).status_code == 401
    # Each page script sends a visitor without a session to the login page.
    for files in PAGES.values():
        js = (STATIC / files[1]).read_text(encoding="utf-8") + (
            STATIC / "js" / "api.js").read_text(encoding="utf-8")
        assert 'assign("/login")' in js
    await web.login("bob", admin=False)
    assert web.client.get("/api/infra/map").status_code == 200
    assert web.port()["port_key"] == "gi1/0/5"
    assert web.client.get("/api/infra/port", params={"switch_id": SID,
                                                     "port": "gi9"}).status_code == 404
    assert web.client.get("/api/admin/infra/unlinked").status_code == 403


def test_static_files_use_no_innerhtml_and_no_inline_script_or_style():
    sinks = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|"
                       r"new Function|setAttribute\(\s*[\"']on|setAttribute\(\s*[\"']style")
    for name in ("infra-common.js", "js/dom.js", "js/api.js", "pages/map.js", "port.js",
                 "infra-admin.js", "map.html",
                 "port.html", "infra-admin.html"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert not sinks.search(text), name
        assert not re.search(r"\sstyle\s*=|\son[a-z]+\s*=|javascript:", text, re.I), name
        if name.endswith(".html"):
            scripts = re.findall(r"<script\b[^>]*>", text)
            assert scripts and all('src="/static/' in s for s in scripts), name
            assert not re.search(r"<style|https?://", text), name
        else:
            assert "textContent" in text or name in ("infra-common.js", "js/api.js")


async def test_port_view_has_state_properties_history_findings_and_monitors(web):
    await web.seed()
    await web.infra.append_property(SID, "Gi1/0/5", "link_speed_mbps", 100, unit="Mbit/s",
                                    source="field", report_id="r2", observed_at=9.0, now=10.0)
    await web.infra.append_property(SID, "Gi1/0/5", "link_speed_mbps", 1000, unit="Mbit/s",
                                    source="field", report_id="r3", observed_at=11.0, now=12.0)
    await web.login("bob", admin=False)
    d = web.port()
    assert d["state"] == "warn"  # a passing check does not hide an unacknowledged warning
    assert d["monitors"][0]["monitor"] == "edge-gi5"
    assert d["monitors"][0]["live"]["speed_mbps"] == 100
    assert d["properties"]["link_speed_mbps"]["report_id"] == "r3"
    assert [h["report_id"] for h in d["history"]["link_speed_mbps"]] == ["r3", "r2", "r1"]
    assert [f["kind"] for f in d["findings"]] == ["speed_above_live"]
    assert d["findings"][0]["acknowledged"] is False


async def test_acknowledge_needs_admin_and_csrf_and_is_audited(web):
    await web.seed()
    pay = {"switch_id": SID, "port_key": "gi1/0/5", "kind": "speed_above_live"}
    url = "/api/admin/infra/findings/ack"
    assert web.client.post(url, json=pay).status_code == 401
    csrf = await web.login("bob", admin=False)
    assert web.client.post(url, json=pay, headers=csrf).status_code == 403
    web.client.cookies.clear()
    csrf = await web.login("root", admin=True)
    assert web.client.post(url, json=pay).status_code == 403  # no CSRF token
    assert web.client.post(url, json={"kind": "x"}, headers=csrf).status_code == 422
    assert web.client.post(url, json={**pay, "kind": "vlan_mismatch"},
                           headers=csrf).status_code == 422
    assert web.client.post(url, json=pay, headers=csrf).status_code == 200
    f = web.port()["findings"][0]
    assert f["acknowledged"] is True and f["acked_by"] == "root"
    assert web.port()["state"] == "up"
    kinds = [r[0] for r in await web.infra._run(lambda d: d.execute(
        "SELECT kind FROM audit WHERE kind LIKE 'infra_finding%' ORDER BY id").fetchall())]
    assert kinds == ["infra_finding_ack_failed", "infra_finding_acknowledged"]


async def test_a_changed_finding_is_new_again_after_an_acknowledgement(web):
    await web.seed()
    csrf = await web.login("root", admin=True)
    pay = {"switch_id": SID, "port_key": "gi1/0/5", "kind": "speed_above_live"}
    assert web.client.post("/api/admin/infra/findings/ack", json=pay,
                           headers=csrf).status_code == 200
    await web.infra.append_property(SID, "Gi1/0/5", "link_speed_mbps", 10000, source="field",
                                    observed_at=20.0, now=21.0)
    assert web.port()["findings"][0]["acknowledged"] is False


async def test_decisions_and_link_from_the_admin_page_need_admin_and_csrf(web):
    await web.seed()
    unlinked = switch_id(sys_name="mystery")
    await web.infra.upsert_switch(unlinked, now=1.0)
    csrf = await web.login("bob", admin=False)
    for url, body in (("/api/admin/infra/link", {"switch_id": unlinked, "monitor": "edge-sw"}),
                      ("/api/admin/infra/depends/accept", {"child": "a", "parent": "b"}),
                      ("/api/admin/infra/depends/reject", {"child": "a", "parent": "b"})):
        assert web.client.post(url, json=body, headers=csrf).status_code == 403
    web.client.cookies.clear()
    csrf = await web.login("root", admin=True)
    for url, body in (("/api/admin/infra/link", {"switch_id": unlinked, "monitor": "edge-sw"}),
                      ("/api/admin/infra/depends/accept", {"child": "a", "parent": "b"}),
                      ("/api/admin/infra/depends/reject", {"child": "a", "parent": "b"})):
        assert web.client.post(url, json=body).status_code == 403
    assert unlinked in [r["switch_id"] for r in web.client.get("/api/admin/infra/unlinked").json()]
    assert web.client.post("/api/admin/infra/link",
                           json={"switch_id": unlinked, "monitor": "edge-sw"},
                           headers=csrf).status_code == 200


async def test_hostile_strings_reach_the_pages_only_as_json_data(web):
    await web.seed(name=HOSTILE)
    await web.infra.append_property(SID, "Gi1/0/5", "custom.note", HOSTILE, source="admin",
                                    recorded_by=HOSTILE[:20], now=7.0)
    await web.infra.upsert_jack(HOSTILE[:60], room=HOSTILE[:40], now=1.0)
    await web.login("bob", admin=False)
    d = web.port()
    assert d["switch"]["name"] == HOSTILE
    assert d["properties"]["custom.note"]["value"] == HOSTILE
    r = web.client.get("/api/infra/port", params={"switch_id": SID, "port": "Gi1/0/5"})
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-content-type-options"] == "nosniff"
    mapped = web.client.get("/api/infra/map")
    assert mapped.headers["content-type"].startswith("application/json")
    assert any(n["label"] == HOSTILE for n in mapped.json()["nodes"])
    # The pages themselves are static and never contain stored strings.
    for path in PAGES:
        assert "onerror" not in web.client.get(path).text


def test_map_and_helpers_are_es_modules_with_one_shared_dom_helper():
    for name in ("map.html", "port.html", "infra-admin.html"):
        scripts = re.findall(r"<script\b[^>]*>", (STATIC / name).read_text(encoding="utf-8"))
        assert len(scripts) == 1 and 'type="module"' in scripts[0], name
    common = (STATIC / "infra-common.js").read_text(encoding="utf-8")
    assert 'from "/static/js/dom.js"' in common and "function el(" not in common
    for name in ("js/dom.js", "js/api.js", "infra-common.js"):
        assert "export " in (STATIC / name).read_text(encoding="utf-8"), name
    dom = (STATIC / "js" / "dom.js").read_text(encoding="utf-8")
    assert "export function clear(" in dom and "export function svg(" in dom
    mapjs = (STATIC / "pages" / "map.js").read_text(encoding="utf-8")
    assert "SVG_NS" not in mapjs and "svgEl(" in mapjs
    # The plugin pages load the helpers as a module too, never as a classic script.
    plugin = STATIC.parent.parent / "plugins" / "pockethernet" / "observe_pockethernet"
    for name in ("jack", "report", "reports"):
        html = (plugin / "pages" / f"{name}.html").read_text(encoding="utf-8")
        assert "infra-common.js" not in html and 'type="module"' in html, name
    plug = (plugin / "static" / "pockethernet.js").read_text(encoding="utf-8")
    assert 'from "/static/infra-common.js"' in plug
    assert not (STATIC / "map.js").exists()
