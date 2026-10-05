"""The console shell and navigation from docs/GUI-DESIGN.md section 2.3 and slice S3."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from observe.plugins import NAV_WORKSPACES, NavEntry, PluginBase, PluginError, load_plugins

from .test_auth import Env
from .test_plugins import config, ep, installed

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "observe" / "static"
PAGES = sorted([*STATIC.glob("*.html"),
                *ROOT.glob("plugins/*/*/pages/*.html")])
PAGES = [p for p in PAGES if p.name != "login.html"]
SHELL = (STATIC / "js" / "shell.js").read_text(encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def _nav_table() -> list[dict[str, object]]:
    block = SHELL[SHELL.index("export const NAV = ["):]
    block = block[:block.index("];")]
    rows = []
    for m in re.finditer(r'\{ workspace: "(\w+)", label: "([^"]+)", href: "([^"]+)", '
                         r'also: \[([^\]]*)\], admin: (true|false) \}', block):
        rows.append({"workspace": m[1], "label": m[2], "href": m[3],
                     "also": re.findall(r'"([^"]+)"', m[4]), "admin": m[5] == "true"})
    return rows


@pytest.mark.parametrize("path", PAGES, ids=[p.name for p in PAGES])
def test_every_signed_in_page_has_the_shell_mounts_and_module(path: Path):
    html = path.read_text(encoding="utf-8")
    assert '<header id="shell-header"' in html
    assert '<div id="summary" aria-live="polite"></div>' in html
    assert '<nav id="shell-nav" aria-label="Main"></nav>' in html
    assert '<script type="module" src="/static/js/shell.js"></script>' in html
    assert '<link rel="stylesheet" href="/static/css/shell.css">' in html
    assert "plugin-nav" not in html


def test_login_page_has_no_navigation():
    html = (STATIC / "login.html").read_text(encoding="utf-8")
    assert "shell.js" not in html and "<nav" not in html


def test_shell_script_is_served_as_javascript(env):
    r = env.client.get("/static/js/shell.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert env.client.get("/static/css/shell.css").status_code == 200


def test_nav_table_names_only_real_pages_and_marks_admin_ones(env):
    table = _nav_table()
    assert len(table) >= 4
    env.user("root", admin=True)
    assert env.login("root").status_code == 200
    for row in table:
        assert env.client.get(str(row["href"])).status_code == 200, row
    admin_only = {r["href"] for r in table if r["admin"]}
    assert admin_only == {"/admin", "/admin/infra", "/audit"}
    assert {r["workspace"] for r in table} <= NAV_WORKSPACES
    assert "O" in SHELL and 'el("span", "shell-badge", "O")' in SHELL


def test_viewer_session_is_not_admin_and_the_shell_filters_on_it(env):
    env.user("bob")
    assert env.login("bob").status_code == 200
    assert env.client.get("/api/session").json()["is_admin"] is False
    # The shell drops entries flagged admin unless the session says is_admin.
    assert "NAV.filter((n) => isAdmin || !n.admin)" in SHELL


def test_plugin_nav_hides_admin_entries_from_viewers_and_carries_the_workspace():
    loaded = load_plugins(config(plugins=["echo"]), installed(ep("echo", "echo_plugin")))
    viewer = loaded.nav(False)
    admin = loaded.nav(True)
    assert [n["label"] for n in viewer] == ["Echo"]
    assert [n["label"] for n in admin] == ["Echo", "Echo admin"]
    assert all(n["workspace"] == "network" for n in admin)


def _load_with_nav(entry: NavEntry):
    class Bad(PluginBase):
        name = "bad"
        core_versions = ">=1"

        def nav_entries(self):
            return [entry]

    mod = type(sys)("tests.fakes.bad_plugin")
    mod.plugin = Bad()
    sys.modules[mod.__name__] = mod
    try:
        return load_plugins(config(plugins=["bad"]), installed(ep("bad", "bad_plugin")))
    finally:
        del sys.modules[mod.__name__]


def test_plugin_nav_workspace_is_validated():
    ok = _load_with_nav(NavEntry("Fine", "/plugins/bad", workspace="reports"))
    assert ok.nav(False)[0]["workspace"] == "reports"
    with pytest.raises(PluginError, match="workspace"):
        _load_with_nav(NavEntry("Bad", "/plugins/bad", workspace="elsewhere"))


def test_pockethernet_entry_sits_under_network():
    from observe_pockethernet import plugin
    assert plugin.nav_entries()[0].workspace == "network"
