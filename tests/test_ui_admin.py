"""The admin, audit, map admin and port restyle from docs/GUI-DESIGN.md sections 3.5 to 3.8
and slice S7."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .test_auth import Env

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "observe" / "static"
BANNED = r"\.innerHTML\s*=|outerHTML|insertAdjacentHTML|document\.write|eval\("
HOSTILE = '<img src=x onerror="alert(1)">'
# route, html file, entry module, ids the markup must carry
PAGES = {
    "/admin": ("admin.html", "admin.js",
               ["page", "msg", "newkey", "newkey-value", "newkey-copy", "key-form", "keys",
                "user-form", "users", "audit-link"]),
    "/audit": ("audit.html", "audit.js",
               ["page", "msg", "f-actor", "f-kind", "f-status", "f-range", "audit"]),
    "/admin/infra": ("infra-admin.html", "infra-admin.js",
                     ["page", "msg", "unlinked", "pending", "decided"]),
    "/port": ("port.html", "port.js", ["page", "footer"]),
}
ORDER = ['href="/static/css/tokens.css"', 'href="/static/css/base.css"',
         'href="/static/css/shell.css"', 'href="/static/css/components.css"',
         'href="/static/css/admin.css"']


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def _read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("route", sorted(PAGES))
def test_page_markup_loads_styles_in_order_and_one_module_then_the_shell(env, route):
    html_file, entry, ids = PAGES[route]
    env.user("root", admin=True)
    assert env.login("root").status_code == 200
    r = env.client.get(route)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    pos = [html.find(o) for o in ORDER]
    assert all(p >= 0 for p in pos) and pos == sorted(pos)
    for ident in ["summary", "shell-nav", *ids]:
        assert f'id="{ident}"' in html, ident
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert len(scripts) == 2 and all('type="module"' in s for s in scripts)
    assert f'src="/static/{entry}"' in scripts[0] and "/static/js/shell.js" in scripts[1]
    assert "<title>" in html and "- Observe</title>" in html
    assert html == _read(html_file).replace("\r\n", "\n") or html == _read(html_file)


def test_audit_page_route_is_static_and_its_script_is_served(env):
    r = env.client.get("/audit")
    assert r.status_code == 200 and HOSTILE not in r.text
    js = env.client.get("/static/audit.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert env.client.get("/static/css/admin.css").status_code == 200


def test_admin_pages_link_audit_and_nav_lists_it_for_admins_only():
    assert 'href="/audit"' in _read("admin.html")
    shell = _read("js/shell.js")
    assert 'label: "Audit", href: "/audit"' in shell and 'workspace: "admin"' in shell
    assert "NAV.filter((n) => isAdmin || !n.admin)" in shell


def test_viewer_is_refused_every_admin_api_the_pages_use(env):
    env.user("bob")
    assert env.login("bob").status_code == 200
    for path in ("/api/v2/audit", "/api/v2/admin/keys", "/api/v2/admin/users",
                 "/api/v2/admin/config", "/api/v2/admin/settings/tiers",
                 "/api/v2/admin/settings/storage", "/api/v2/admin/settings/retention",
                 "/api/v2/admin/settings/recheck", "/api/v2/admin/settings/rules",
                 "/api/v2/admin/infra/unlinked"):
        assert env.client.get(path).status_code == 403, path
    # The pages are static, so a viewer who opens them gets the "admin account needed" card.
    for rel in ("admin.js", "audit.js", "infra-admin.js"):
        assert "notAdmin(" in _read(rel), rel
    assert 'role", "alert"' in _read("js/admin-ui.js")


def test_unsigned_visitor_gets_no_audit_data(env):
    r = env.client.get("/api/v2/audit")
    assert r.status_code in (401, 403)


def test_audit_api_returns_hostile_detail_as_json_only(env):
    env.user("root", admin=True)
    resp = env.login("root")
    hdr = env.csrf(resp)
    env.client.post("/api/admin/keys", json={"host": "bad host"}, headers=hdr)
    rows = env.client.get("/api/v2/audit", params={"limit": 500}).json()["items"]
    assert rows and {"ts", "actor", "kind", "status", "detail"} <= set(rows[0])
    assert HOSTILE not in env.client.get("/audit").text


@pytest.mark.parametrize("rel", ["admin.js", "audit.js", "infra-admin.js", "port.js",
                                 "js/admin-ui.js"])
def test_scripts_are_modules_that_write_only_text_and_build_no_pills(rel):
    js = _read(rel)
    assert "import " in js and not re.search(BANNED, js), rel
    assert "pill" not in js and not re.search(r"\.style|setAttribute\(.style", js), rel


def test_admin_pages_use_shared_components():
    admin = _read("admin.js")
    for needle in ("sortableTable", "confirmDialog", "toast(", "statusChip", "copyText"):
        assert needle in admin, needle
    assert "Revoke" in admin and "Disable" in admin and "readonly" in _read("admin.html")
    infra = _read("infra-admin.js")
    for needle in ("sortableTable", "confirmDialog", "toast(", '"Accept"', '"Reject"'):
        assert needle in infra, needle
    audit = _read("audit.js")
    for needle in ("sortableTable", "statusChip", "aria-pressed", "defaultSize: 25",
                   "detail-mono"):
        assert needle in audit, needle
    port = _read("port.js")
    assert "stateChip" in port and "sortableTable" in port and 'href = "/map"' in port
    assert "stateChip" in _read("infra-common.js") and "statePill" not in _read("infra-common.js")


def test_admin_css_uses_tokens_only_and_wraps_audit_detail():
    css = _read("css/admin.css")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", css)
    assert ".detail-mono" in css and "overflow-wrap: anywhere" in css


def test_new_files_use_lf_line_endings():
    for rel in ("observe/static/audit.html", "observe/static/audit.js", "observe/static/admin.js",
                "observe/static/admin.html", "observe/static/css/admin.css",
                "observe/static/js/admin-ui.js", "observe/static/port.js",
                "observe/static/infra-admin.js", "tests/test_ui_admin.py"):
        assert bytes([13]) not in (ROOT / rel).read_bytes(), rel
