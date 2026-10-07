"""The console as a client of the v2 API (docs/DATA-API-DESIGN.md sections 5 and 11, slice O-9):
the JavaScript unit tests, the new admin pages, the saved rules route and the guard that keeps a
page from going back to a removed route or to its own timer."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_auth import Env

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "observe" / "static"
NODE = shutil.which("node")
RULES = "/api/admin/rules"
RULES_READ = "/api/v2/admin/settings/rules"

PAGES = {
    "/admin/tiers": ("admin-tiers.html", "admin-tiers.js", ["tiers-form", "tiers-global", "tiers-hosts"]),
    "/admin/retention": ("admin-retention.html", "admin-retention.js",
                         ["retention-form", "retention-global", "retention-overrides", "add-override"]),
    "/admin/recheck": ("admin-recheck.html", "admin-recheck.js",
                       ["recheck-form", "recheck-global", "recheck-overrides"]),
    "/admin/rules": ("admin-rules.html", "admin-rules.js",
                     ["rules-list", "rules-save", "rule-form", "rule-fields"]),
    "/admin/storage": ("admin-storage.html", "admin-storage.js", ["backend-line", "levels", "seqs"]),
}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def _admin(env):
    env.user("root", admin=True)
    return env.csrf(env.login("root"))


def _read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def _rule(**over):
    rule = {"id": "r1", "kind": "consecutive", "metric": "cpu.temp", "condition": "above",
            "warn": 70, "crit": 85, "x": 3}
    rule.update(over)
    return rule


# ---- the JavaScript unit tests ----------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_javascript_unit_tests_pass():
    out = subprocess.run([NODE, "--test", "tests/js"], cwd=ROOT, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-1000:]
    assert "fail 0" in out.stdout


# ---- the new admin pages ----------------------------------------------------------------------

@pytest.mark.parametrize("route", sorted(PAGES))
def test_an_admin_page_is_static_and_loads_its_module_then_the_shell(env, route):
    html_file, entry, ids = PAGES[route]
    _admin(env)
    r = env.client.get(route)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert r.text == _read(html_file)
    for ident in ["summary", "shell-nav", "page", "msg", *ids]:
        assert f'id="{ident}"' in r.text, ident
    scripts = re.findall(r"<script\b[^>]*>", r.text)
    assert len(scripts) == 2 and all('type="module"' in s for s in scripts)
    assert f'src="/static/{entry}"' in scripts[0] and "/static/js/shell.js" in scripts[1]
    assert "- Observe</title>" in r.text
    assert "csrf" not in r.text.lower()
    assert env.client.get(f"/static/{entry}").status_code == 200


@pytest.mark.parametrize("route", sorted(PAGES))
def test_an_admin_page_script_asks_for_an_admin_and_reads_through_the_client(route):
    entry = PAGES[route][1]
    js = _read(entry)
    gate = _read("js/settings-page.js") if "settingsPage(" in js else js
    assert "notAdmin(" in gate and "whoami()" in gate
    assert 'from "/static/js/api.js"' in js or 'from "/static/js/settings-page.js"' in js
    assert "/api/v2/admin/settings/" in js
    assert "fetch(" not in js and "setInterval" not in js and "innerHTML" not in js


def test_the_settings_pages_save_through_the_puts_that_exist():
    expected = {"admin-tiers.js": "/api/admin/tiers", "admin-retention.js": "/api/admin/retention",
                "admin-recheck.js": "/api/admin/recheck", "admin-rules.js": "/api/admin/rules"}
    for rel, put in expected.items():
        assert put in _read(rel), rel
    assert "/api/v2/admin/settings/storage" in _read("admin-storage.js")
    assert "poller(" in _read("admin-storage.js")


def test_a_viewer_gets_the_page_shell_but_every_document_it_reads_is_refused(env):
    env.user("bob")
    env.login("bob")
    for route in PAGES:
        assert env.client.get(route).status_code == 200  # the page holds no data
    for name in ("tiers", "retention", "recheck", "rules", "storage"):
        assert env.client.get(f"/api/v2/admin/settings/{name}").status_code == 403, name


# ---- the saved rules route --------------------------------------------------------------------

def test_the_rules_route_needs_an_admin_session_and_the_csrf_token(env):
    assert env.client.put(RULES, json={"rules": []}).status_code in (401, 403)
    env.user("bob")
    hdr = env.csrf(env.login("bob"))
    assert env.client.put(RULES, json={"rules": []}, headers=hdr).status_code == 403
    env.client.cookies.clear()
    admin = _admin(env)
    assert env.client.put(RULES, json={"rules": []}).status_code == 403
    assert env.client.put(RULES, json={"rules": []}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert env.client.put(RULES, json={"rules": []}, headers=admin).status_code == 200
    assert env.rows("SELECT COUNT(*) FROM app_settings WHERE key='rules.config'") == [(1,)]


def test_saved_rules_are_read_back_from_v2_and_audited_with_old_and_new(env):
    hdr = _admin(env)
    first = env.client.get(RULES_READ)
    assert first.json() == {"rules": [], "max_rules": 500}
    r = env.client.put(RULES, headers=hdr, json={"rules": [_rule()]})
    assert r.status_code == 200
    assert r.json()["rules"][0]["id"] == "r1" and r.json()["max_rules"] == 500
    got = env.client.get(RULES_READ).json()
    assert [x["id"] for x in got["rules"]] == ["r1"] and got["rules"][0]["warn"] == 70
    assert env.client.get(RULES_READ).headers.get("etag") != first.headers.get("etag")
    env.client.put(RULES, headers=hdr, json={"rules": []})
    rows = env.rows("SELECT actor, detail FROM audit WHERE kind='rules_changed' ORDER BY id")
    assert [a for a, _ in rows] == ["root", "root"]
    first_change = json.loads(rows[0][1])
    assert first_change["old"] == [] and first_change["new"][0]["id"] == "r1"
    assert json.loads(rows[1][1])["new"] == []


def test_a_refused_rule_is_422_audited_and_changes_nothing(env):
    hdr = _admin(env)
    for body in ({"rules": [_rule(kind="nope")]}, {"rules": [_rule(), _rule()]},
                 {"rules": [_rule(crit=50)]}, {"rules": "x"}, {"rules": [_rule(extra=1)]}):
        r = env.client.put(RULES, headers=hdr, json=body)
        assert r.status_code == 422 and r.json()["detail"], body
    assert env.rows("SELECT COUNT(*) FROM app_settings WHERE key='rules.config'") == [(0,)]
    assert len(env.rows("SELECT id FROM audit WHERE kind='rules_failed'")) == 5
    assert env.rows("SELECT id FROM audit WHERE kind='rules_changed'") == []


def test_an_ingest_key_cannot_save_rules(env):
    import asyncio

    from observe.ingest.keys import create_key
    _admin(env)
    env.client.cookies.clear()
    key = asyncio.run(create_key(env.store, "h", scope="wpi"))[0]
    r = env.client.put(RULES, json={"rules": []}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code in (401, 403)
    assert env.rows("SELECT COUNT(*) FROM app_settings WHERE key='rules.config'") == [(0,)]


# ---- removed routes ---------------------------------------------------------------------------

def test_the_removed_legacy_reads_answer_not_found_for_an_admin(env):
    _admin(env)
    for path in ("/api/session", "/api/admin/users", "/api/admin/keys", "/api/admin/retention",
                 "/api/admin/recheck", "/api/admin/tiers", "/api/plugins/control/commands",
                 "/api/plugins/control/capabilities", "/api/infra/dependencies",
                 "/api/admin/infra/unlinked", "/api/enrol/public-url", "/api/hosts/nas01/enrolment",
                 "/api/hosts/nas01/settings", "/api/plugins/unifi/devices",
                 "/api/plugins/unifi/clients", "/api/plugins/unifi/protect",
                 "/api/plugins/pockethernet/reports", "/api/plugins/pockethernet/report",
                 "/api/plugins/pockethernet/jack"):
        assert env.client.get(path).status_code in (404, 405), path


def test_the_v2_session_gives_what_the_pages_need(env):
    hdr = _admin(env)
    me = env.client.get("/api/v2/session").json()
    assert me["username"] == "root" and me["is_admin"] is True
    assert me["csrf"] == hdr["X-CSRF-Token"] and me["kind"] == "session"


def test_disabling_or_demoting_a_user_shows_on_the_next_v2_read(env):
    admin = _admin(env)
    env.user("bob", admin=True)
    uid = env.rows("SELECT id FROM users WHERE username='bob'")[0][0]
    other = type(env.client)(env.client.app, base_url="https://testserver")
    try:
        assert other.post("/api/login", json={"username": "bob", "password": "correct horse battery"}
                          ).status_code == 200
        assert other.get("/api/v2/admin/users").status_code == 200
        env.client.post(f"/api/admin/users/{uid}/admin", json={"value": False}, headers=admin)
        assert other.get("/api/v2/admin/users").status_code == 403
        env.client.post(f"/api/admin/users/{uid}/disabled", json={"value": True}, headers=admin)
        assert other.get("/api/v2/session").status_code == 401
    finally:
        other.close()


# ---- the guard: pages use the client and nothing else -----------------------------------------

PAGE_SCRIPTS = sorted(p for p in STATIC.rglob("*.js"))
PLUGIN_SCRIPTS = sorted((ROOT / "plugins").glob("*/*/static/*.js"))


def _code(path: Path) -> str:
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    return re.sub(r"(?m)(^|\s)//.*$", r"\1", text)


def test_only_the_client_the_login_page_and_the_control_writes_call_fetch():
    users = sorted(p.name for p in PAGE_SCRIPTS if re.search(r"\bfetch\(", _code(p)))
    assert users == ["api.js", "host-control.js", "login.js"]
    control = _code(STATIC / "host-control.js")
    assert "ctlApi(\"GET\"" not in control and "/api/v2/control/" in control


def test_no_page_uses_a_removed_route_a_timer_of_its_own_or_the_old_reader():
    banned = {
        "the removed session read": re.compile(r"""["'`]/api/session\b"""),
        "the removed key and user lists": re.compile(r"""api\(\s*["']GET["']\s*,\s*["'`]/api/admin/(keys|users)"""),
        "the old v2 reader": re.compile(r"static/js/v2\.js|\bgetJson\("),
        "an interval timer": re.compile(r"\bsetInterval\s*\("),
    }
    for path in PAGE_SCRIPTS:
        code = _code(path)
        for label, pattern in banned.items():
            assert not pattern.search(code), f"{path.name}: {label}"
    # The settings routes keep only their PUT; a page reads the document from /api/v2.
    for path in PAGE_SCRIPTS:
        for m in re.finditer(r"""api\(\s*["'](\w+)["']\s*,\s*["'`](/api/admin/(?:retention|recheck|tiers|rules))""",
                             _code(path)):
            assert m.group(1) == "PUT", f"{path.name}: {m.group(0)}"


def test_the_pages_that_refresh_use_the_poller_with_the_domains_they_read():
    expected = {
        "app.js": ["monitors", "events"], "host.js": ["hosts"], "port.js": ["ports"],
        "pages/map.js": ["map"],
    }
    for rel, domains in expected.items():
        code = _code(STATIC / rel)
        assert "poller(" in code, rel
        for d in domains:
            assert f'"{d}"' in code, (rel, d)
    for rel in ("admin-storage.js", "host-control.js", "hosts-new.js", "host-settings.js"):
        assert "poller(" in _code(STATIC / rel), rel


def test_a_refresh_that_fails_throws_so_the_poller_can_back_off():
    for rel in ("app.js", "host.js", "port.js", "pages/map.js"):
        code = _code(STATIC / rel)
        assert "throw e;" in code and "observe unreachable, retrying" in code, rel


def test_the_plugin_pages_read_the_v2_resources_through_the_client():
    unifi = _code(ROOT / "plugins/unifi/observe_unifi/static/unifi.js")
    assert "/api/v2/unifi" in unifi and "whoami" in unifi and "/api/plugins" not in unifi
    field = _code(ROOT / "plugins/pockethernet/observe_pockethernet/static/pockethernet.js")
    assert "/api/v2/pockethernet" in field and "/api/plugins" not in field
    assert (STATIC / "js" / "v2.js").exists() is False
    assert PLUGIN_SCRIPTS
    for path in PLUGIN_SCRIPTS:
        assert not re.search(r"fetch\(|setInterval\s*\(", _code(path)), path.name


# A change still goes to the route that makes it; every read is /api/v2. The pattern catches a
# GET named as a method, a bare read helper and a raw fetch with a path that is not v2.
LEGACY_READ = re.compile(
    r"""(?:api\(\s*["']GET["']\s*,|getAll?\(|fetch\()\s*["'`]/api/(?!v2/)""")


def test_no_script_reads_from_a_route_that_is_not_v2():
    for path in PAGE_SCRIPTS + PLUGIN_SCRIPTS:
        if path.name == "login.js":  # the sign-in is a POST, not a read
            continue
        assert not LEGACY_READ.search(_code(path)), path.name
    assert LEGACY_READ.search('api("GET", "/api/hosts/x/settings")')
    assert LEGACY_READ.search("fetch(`/api/plugins/unifi/devices`)")
    assert not LEGACY_READ.search('api("GET", `/api/v2/hosts/x/settings`)')
    assert not LEGACY_READ.search('api("PUT", "/api/enrol/public-url", csrf, body)')
