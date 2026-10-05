"""The dashboard restyle from docs/GUI-DESIGN.md section 3.2 and slice S5."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from observe.alerts import Alerter
from observe.scheduler import Scheduler
from observe.store import Store

from .conftest import make_config

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "observe" / "static"
HOSTILE = "<img src=x onerror=alert(1)>&\"'"

IDS = ["summary", "shell-nav", "kpi-row", "kpi-monitors", "kpi-down", "kpi-warn",
       "kpi-capacity", "tiles", "state-filter", "search", "groups", "capacity-panel",
       "capacity", "findings-panel", "findings", "events-panel", "events", "footer",
       "row-tpl"]


def _client(tmp_path: Path) -> TestClient:
    path = str(tmp_path / "d.db")
    store = Store(path)
    cfg = make_config([{"name": HOSTILE, "type": "ping", "host": "10.0.0.2",
                        "group": "<b>core</b>"}], server={"db_path": path})
    app = create_app_for(cfg, store)
    return TestClient(app, base_url="https://testserver")


def create_app_for(cfg, store):
    from observe.web import create_app
    return create_app(cfg, store, Scheduler(cfg, store, Alerter(cfg)), Alerter(cfg))


def test_dashboard_page_has_every_id_and_loads_the_modules_in_order(tmp_path: Path):
    html = _client(tmp_path).get("/").text
    for i in IDS:
        assert f'id="{i}"' in html, i
    order = ['href="/static/css/tokens.css"', 'href="/static/css/base.css"',
             'href="/static/css/shell.css"', 'href="/static/css/components.css"',
             'href="/static/css/dashboard.css"']
    pos = [html.find(o) for o in order]
    assert all(p >= 0 for p in pos) and pos == sorted(pos)
    assert '<script type="module" src="/static/app.js"></script>' in html
    assert 'id="kpi-down" class="kpi" type="button"' in html


def test_hostile_monitor_name_travels_api_to_page_as_text(tmp_path: Path):
    client = _client(tmp_path)
    mons = client.get("/api/monitors").json()["monitors"]
    assert mons[0]["name"] == HOSTILE and mons[0]["group"] == "<b>core</b>"
    # The page is static, so the name never appears in markup; the script only writes text.
    assert HOSTILE not in client.get("/").text
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"\.innerHTML\s*=|insertAdjacentHTML|document\.write|eval\(", js)
    assert 'querySelector(".name").textContent = m.name' in js
    assert 'el("span", "grp-name", name)' in js
    assert 'import { statusChip' in js and ".pill" not in js


def test_dashboard_files_are_served_with_lf_endings(tmp_path: Path):
    client = _client(tmp_path)
    for path, kind in (("/static/css/dashboard.css", "text/css"),
                       ("/static/app.js", "javascript")):
        r = client.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"]
    for rel in ["observe/static/css/dashboard.css", "observe/static/app.js",
                "observe/static/index.html", "tests/test_ui_dashboard.py"]:
        assert bytes([13]) not in (ROOT / rel).read_bytes(), rel


def test_dashboard_css_uses_tokens_only():
    css = (STATIC / "css" / "dashboard.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", css)
