"""The Updates page (/admin/updates, README "Updating"): markup, the module scripts, the
text-only rule, the admin gate, the shell entry and the rules the page relies on."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .test_auth import Env
from .test_field_docs import ROOT, stored_bytes

STATIC = ROOT / "observe" / "static"
IDS = ["page", "msg", "version-line", "upstream-line", "update-state", "phases", "update-log",
       "updated-line", "update-observe", "observe-note", "update-all", "agents-summary",
       "agents-refused", "agents", "footer"]
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


def test_page_markup_loads_styles_in_order_and_one_module_then_the_shell(env):
    env.user("root", admin=True)
    assert env.login("root").status_code == 200
    r = env.client.get("/admin/updates")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    pos = [html.find(o) for o in ORDER]
    assert all(p >= 0 for p in pos) and pos == sorted(pos)
    for ident in ["summary", "shell-nav", *IDS]:
        assert f'id="{ident}"' in html, ident
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert len(scripts) == 2 and all('type="module"' in s for s in scripts)
    assert 'src="/static/admin-updates.js"' in scripts[0] and "/static/js/shell.js" in scripts[1]
    assert "<title>Updates - Observe</title>" in html
    assert html.replace("\r\n", "\n") == _read("admin-updates.html").replace("\r\n", "\n")
    # Both cards are labelled sections, the log is a labelled block and the live regions exist.
    assert html.count('<section class="card"') == 2 and 'aria-labelledby="observe-h"' in html
    assert 'aria-labelledby="agents-h"' in html and 'aria-label="Update log"' in html
    assert 'aria-live="polite"' in html and 'role="alert"' in html


def test_the_page_is_static_and_the_scripts_are_served_as_javascript(env):
    r = env.client.get("/admin/updates")
    assert r.status_code == 200 and "<script" in r.text  # the page itself holds no data
    for rel in ("admin-updates.js", "js/updates-logic.js"):
        js = env.client.get(f"/static/{rel}")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"], rel


def test_scripts_are_modules_that_write_only_text():
    for rel in ("admin-updates.js", "js/updates-logic.js"):
        js = _read(rel)
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert banned not in js, (rel, banned)
    page = _read("admin-updates.js")
    assert "textContent" in page and 'from "/static/js/dom.js"' in page
    assert 'from "/static/js/chips.js"' in page and 'from "/static/js/dialog.js"' in page
    assert 'from "/static/js/toast.js"' in page and 'from "/static/js/table.js"' in page
    assert "notAdmin(" in page and "whoami()" in page


def test_the_update_button_needs_the_typed_word_and_sends_the_documented_body():
    page = _read("admin-updates.js")
    assert 'name: "update"' in page and "typedConfirm(" in page
    assert '{ confirmed: true, confirm_text: "update" }' in page
    assert '"/api/admin/updates/observe"' in page
    dialog = _read("js/dialog.js")
    assert "typedLabel" in dialog and "typedMatches(input.value, typedName)" in dialog


def test_the_page_polls_fast_while_a_request_is_open_and_detects_the_new_version():
    page = _read("admin-updates.js")
    assert "versionChanged(loadedVersion, status.version)" in page
    assert "Updated to ${status.version}" in page
    assert "polling(status)" in page and "interval: fast ? 3000 : 30000" in page
    assert '"/api/v2/updates/status"' in page and '"/api/v2/updates/agents"' in page
    assert 'getAll("/api/v2/hosts")' in page


def test_agents_offer_update_per_host_update_all_and_the_install_only_link():
    page = _read("admin-updates.js")
    assert '"/api/plugins/control/request"' in page and 'action: "agent.update"' in page
    assert '"/api/plugins/control/update-agents"' in page and "updateAllSummary(" in page
    assert "install command only" in page and "settingsHref(r.host)" in page
    assert "control plugin not loaded" in page


def test_nav_lists_updates_under_admin_and_the_page_route_is_admin_only(env):
    shell = _read("js/shell.js")
    assert '{ workspace: "admin", label: "Updates", href: "/admin/updates", also: [], admin: true }' in shell
    # The page is static like the other admin pages; the data behind it needs an admin session.
    env.user("bob")
    assert env.login("bob").status_code == 200
    assert env.client.get("/admin/updates").status_code == 200
    assert env.client.get("/api/v2/updates/status").status_code == 403
    assert env.client.get("/api/v2/updates/agents").status_code == 403
    r = env.client.post("/api/admin/updates/observe", json={"confirmed": True, "confirm_text": "update"})
    assert r.status_code == 403


def test_css_uses_tokens_only_and_the_log_panel_wraps():
    css = _read("css/admin.css")
    block = css[css.index("pre.update-log"):css.index("#update-state")]
    assert "var(--o-" in block and "white-space: pre-wrap" in block and "max-height" in block
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block)


def test_new_files_use_lf_and_no_em_dashes_or_model_names():
    for rel in ("observe/static/admin-updates.html", "observe/static/admin-updates.js",
                "observe/static/js/updates-logic.js", "tests/test_ui_updates.py",
                "tests/js/updates.test.mjs"):
        raw = stored_bytes(ROOT / rel)
        text = raw.decode("utf-8")
        assert b"\r" not in raw and chr(0x2014) not in text, rel
        names = ("cla" + "ude", "op" + "us", "son" + "net", "hai" + "ku")
        assert not any(w in text.lower() for w in names), rel
