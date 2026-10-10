"""The Hosts list page (/hosts): the page and its route, the navigation entry, the breadcrumbs
that point at it, the readable host link on the dashboard, and the table's row logic.

The page is static and holds no data; hosts.js reads GET /api/v2/hosts and
GET /api/v2/waiting-hosts and writes every value with textContent. tests/js/hosts.test.mjs holds
the row-logic cases and is run here too when Node is on the path.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_host_views import FULL, FULL_SOURCES, Env, batch

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "observe" / "static"
HOSTILE = "<img src=x onerror=alert(1)>&\"'"
NEW_FILES = ["hosts.html", "hosts.js", "js/hosts-logic.js"]
ORDER = ['href="/static/css/tokens.css"', 'href="/static/css/base.css"',
         'href="/static/css/shell.css"', 'href="/static/css/components.css"',
         'href="/static/css/admin.css"']
CSP_PARTS = ("default-src 'self'", "object-src 'none'", "base-uri 'none'",
             "frame-ancestors 'none'", "form-action 'none'")
BANNED = r"\.innerHTML\s*=|outerHTML|insertAdjacentHTML|document\.write|eval\("


def _read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def _nav_table() -> list[dict[str, object]]:
    shell = _read("js/shell.js")
    block = shell[shell.index("export const NAV = ["):]
    block = block[:block.index("];")]
    rows = []
    for m in re.finditer(r'\{ workspace: "(\w+)", label: "([^"]+)", href: "([^"]+)", '
                         r'also: \[([^\]]*)\], admin: (true|false) \}', block):
        rows.append({"workspace": m[1], "label": m[2], "href": m[3],
                     "also": re.findall(r'"([^"]+)"', m[4]), "admin": m[5] == "true"})
    return rows


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, monitors=[{"name": "nas", "type": "pushed_host", "host": "nas01"},
                                {"name": HOSTILE, "type": "pushed_host", "host": HOSTILE}])
    yield e
    e.close()


# ---- the page and its route ------------------------------------------------------------------

def test_hosts_page_serves_with_the_csp_header_and_the_shell(env):
    # The page holds no data, so it is served without a session like /host; the script sends a
    # visitor without one to /login.
    r = env.client.get("/hosts", follow_redirects=False)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    csp = r.headers.get("Content-Security-Policy", "")
    for part in CSP_PARTS:
        assert part in csp, part
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"
    html = r.text
    assert "<title>Hosts - Observe</title>" in html
    for ident in ["shell-header", "summary", "shell-nav", "page", "hosts-card", "q", "hosts", "footer"]:
        assert f'id="{ident}"' in html, ident
    pos = [html.find(o) for o in ORDER]
    assert all(p >= 0 for p in pos) and pos == sorted(pos)
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert len(scripts) == 2 and all('type="module"' in s for s in scripts)
    assert 'src="/static/hosts.js"' in scripts[0] and "/static/js/shell.js" in scripts[1]
    assert "pushed_host" in html  # the card says what makes a host alert


def test_new_files_are_served_with_the_right_type_and_lf_endings(env):
    for rel in NEW_FILES:
        r = env.client.get(f"/static/{rel}")
        assert r.status_code == 200, rel
        expect = "text/html" if rel.endswith(".html") else "javascript"
        assert expect in r.headers["content-type"], rel
        assert "Content-Security-Policy" in r.headers, rel
    for rel in [f"observe/static/{n}" for n in NEW_FILES] + [
            "tests/test_ui_hosts.py", "tests/js/hosts.test.mjs"]:
        assert bytes([13]) not in (ROOT / rel).read_bytes(), rel


def test_the_list_route_keeps_the_add_host_and_settings_routes(env):
    env.login()
    assert env.client.get("/hosts/new").status_code == 200
    assert env.client.get("/hosts/nas01/settings").status_code == 200
    assert "hosts-new.js" in env.client.get("/hosts/new").text
    assert "hosts.js" in env.client.get("/hosts").text


# ---- navigation and breadcrumbs --------------------------------------------------------------

def test_nav_has_a_hosts_entry_that_owns_the_host_page():
    table = _nav_table()
    hosts = [r for r in table if r["href"] == "/hosts"]
    assert hosts == [{"workspace": "hosts", "label": "Hosts", "href": "/hosts",
                      "also": ["/host"], "admin": False}]
    dashboard = next(r for r in table if r["href"] == "/")
    assert dashboard["also"] == []
    assert "/host" not in {r["href"] for r in table}  # a host page is reached by name
    labels = [r["label"] for r in table if r["workspace"] == "hosts"]
    assert labels == ["Hosts", "Add host"]


def test_breadcrumbs_point_at_the_hosts_list():
    host = _read("host.js")
    assert 'const back = el("a", null, "Hosts");\n  back.href = "/hosts";' in host
    settings = _read("host-settings.js")
    assert 'const home = el("a", null, "Hosts");\n  home.href = "/hosts";' in settings


def test_dashboard_host_link_is_readable():
    html = _read("index.html")
    assert '<a class="hostlink" hidden>Host page</a>' in html
    assert ">hardware<" not in html
    css = _read("css/dashboard.css")
    rule = re.search(r"a\.hostlink\s*\{([^}]*)\}", css)
    assert rule, "dashboard.css lost the hostlink rule"
    body = rule.group(1)
    assert "var(--o-accent)" in body and "--o-text-muted" not in body
    assert "a.hostlink:hover" in css
    # The link is still filled per row from the monitor target, never from markup.
    js = _read("app.js")
    assert 'link.hidden = m.type !== "pushed_host"' in js
    assert "link.href = `/hosts/${encodeURIComponent(m.target)}`" in js


# ---- the table -------------------------------------------------------------------------------

def test_hosts_script_uses_the_shared_table_and_chips_and_writes_text_only():
    js = _read("hosts.js")
    assert not re.search(BANNED, js)
    assert 'import { sortableTable } from "/static/js/table.js";' in js
    assert 'import { statusChip, neutralChip } from "/static/js/chips.js";' in js
    assert 'import { el } from "/static/js/dom.js";' in js
    assert 'getAll("/api/v2/hosts")' in js and 'get("/api/v2/waiting-hosts")' in js
    for label in ["Host", "Platform", "Status", "Last report", "Agent", "Monitor", "Detail", "Settings"]:
        assert f'label: "{label}"' in js, label
    assert 'if (isAdmin) cols.push({ key: "settings"' in js
    assert 'r.waiting ? "Enrolment" : "Settings"' in js
    assert 'neutralChip("Listed")' in js and 'muted("Not listed")' in js
    assert 'domains: ["hosts"]' in js
    for target in re.findall(r'from "([^"]+)"', js):
        assert target.startswith("/static/js/"), target
    logic = _read("js/hosts-logic.js")
    assert "import" not in logic and "document" not in logic  # pure, so Node can run it
    for name in ["hostRow", "waitingRow", "hostRows", "filterRows", "summaryText", "ageText"]:
        assert f"export function {name}(" in logic, name
    assert "/hosts/${encodeURIComponent(name)}" in logic
    assert "/hosts/${encodeURIComponent(name)}/settings" in logic


def test_hostile_host_name_travels_api_to_page_as_text(env):
    env.push(batch(host="nas01", samples=FULL, sources=FULL_SOURCES))
    env.login()
    items = env.client.get("/api/v2/hosts").json()["items"]
    assert {h["host"] for h in items} == {HOSTILE, "nas01"}
    heard = next(h for h in items if h["host"] == "nas01")
    silent = next(h for h in items if h["host"] == HOSTILE)
    # The columns the table shows are all in the list row, so the page needs no per-host read.
    for key in ["host", "platform", "status", "status_reason", "age_seconds", "heard", "stale",
                "agent_version", "monitored", "monitor"]:
        assert key in heard and key in silent, key
    assert heard["monitored"] is True and heard["agent_version"] == "0.9.0"
    assert silent["heard"] is False and silent["monitored"] is True
    assert env.client.get("/api/v2/waiting-hosts").json() == {"items": []}
    # The page is static, so the name never appears in markup; the script only writes text.
    assert HOSTILE not in env.client.get("/hosts").text
    js = _read("hosts.js")
    assert 'const a = el("a", null, r.name);' in js and 'el("span", null, r.name)' in js


def test_waiting_hosts_carry_an_enrolment_link_for_the_table(env):
    env.login()
    r = env.client.get("/api/v2/waiting-hosts")
    assert r.status_code == 200 and r.json() == {"items": []}
    logic = _read("js/hosts-logic.js")
    assert 'status: "waiting"' in logic and "w.enrolment_url" in logic
    assert 'waiting: "pending"' in logic  # a waiting host shows the pending chip


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on the path")
def test_row_logic_passes_under_node():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "js" / "hosts.test.mjs")],
                       capture_output=True, text=True, cwd=str(ROOT), check=False)
    assert r.returncode == 0, r.stdout + r.stderr
