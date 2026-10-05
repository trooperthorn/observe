"""The host settings page (docs/GUI-DESIGN.md section 3.11, slice S13): markup, modules, the pure
rules mirrored in Python (tests/js/settings.test.mjs holds the same cases for `node --test` in
CI), the host page link and the line endings of the new files."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from observe import hosttasks

from .test_enrol_api import admin
from .test_host_settings import enrol_host, secrets_of, settings, step
from .test_install_script import Served  # noqa: F401
from .test_install_script import env  # noqa: F401

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "observe" / "static"
BANNED = r"\.innerHTML\s*=|outerHTML|insertAdjacentHTML|document\.write|eval\("
HOSTILE = '<img src=x onerror="alert(1)">'
IDS = ["page", "msg", "crumbs", "title", "identity", "identity-list", "identity-note",
       "allowlist-card", "allowlist-state", "form-allow", "fan-list", "fan-new", "fan-add",
       "service-list", "service-new", "service-add", "reboot", "allow-error", "save",
       "update-again", "command-card", "command-h", "command-sub", "command-block", "cmd",
       "copy", "expiry", "command-note", "progress", "reports", "install-card", "regen",
       "cleanup", "danger", "revoke", "remove"]
ORDER = ['href="/static/css/tokens.css"', 'href="/static/css/base.css"',
         'href="/static/css/shell.css"', 'href="/static/css/components.css"',
         'href="/static/css/admin.css"',
         'href="/static/css/wizard.css"', 'href="/static/css/settings.css"']
NEW_FILES = ["host-settings.html", "host-settings.js", "js/settings-logic.js", "css/settings.css"]


def _read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


# ---- the page ------------------------------------------------------------------------------

def test_page_markup_ids_styles_and_scripts(env):
    admin(env)
    r = env.client.get("/hosts/nas01/settings")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    pos = [html.find(o) for o in ORDER]
    assert all(p >= 0 for p in pos) and pos == sorted(pos)
    for ident in ["summary", "shell-nav", *IDS]:
        assert f'id="{ident}"' in html, ident
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert len(scripts) == 2 and all('type="module"' in s for s in scripts)
    assert 'src="/static/host-settings.js"' in scripts[0] and "/static/js/shell.js" in scripts[1]
    assert "<title>Host settings - Observe</title>" in html
    assert "nas01" not in html and HOSTILE not in html  # the page holds no data
    assert env.client.get("/hosts/new").status_code == 200  # the fixed route still wins


def test_the_page_never_echoes_a_hostile_name_and_the_api_refuses_it(env):
    hdr = admin(env)
    assert HOSTILE not in env.client.get("/hosts/%3Cimg%20src%3Dx%3E/settings").text
    assert settings(env, "%3Cimg%20src%3Dx%3E").status_code == 404
    assert env.client.post("/api/hosts/x/remove", json={"confirm_host": "x"}, headers=hdr
                           ).status_code == 404


def test_the_danger_zone_is_a_closed_details_and_the_page_is_labelled():
    html = _read("host-settings.html")
    assert re.search(r'<details class="card danger-zone" id="danger"[^>]*>(?!.* open)', html)
    assert "<details class=\"card danger-zone\" id=\"danger\" open" not in html
    for section in ("identity", "allowlist-card", "command-card", "install-card"):
        assert f'id="{section}"' in html
    assert html.count("<fieldset") == html.count("<legend>") == 3
    for m in re.finditer(r"<input\b", html):
        before = html[:m.start()]
        assert before.rfind("<label") > before.rfind("</label>"), html[m.start():m.start() + 60]
    assert '<ol class="progress" id="progress" aria-live="polite">' in html
    assert 'id="reports" aria-live="polite"' in html
    assert '<pre class="cmd" id="cmd"' in html


def test_scripts_are_modules_that_write_only_text():
    for rel in ("host-settings.js", "js/settings-logic.js", "js/dialog.js", "host.js"):
        js = _read(rel)
        assert not re.search(BANNED, js), rel
        assert "pill" not in js and not re.search(r"\.style|setAttribute\(.style", js), rel
    page = _read("host-settings.js")
    assert 'import { el, clear } from "/static/js/dom.js"' in page
    assert "typedConfirm" in page and "confirmDialog" in page


def test_the_command_never_reaches_a_url_storage_or_a_toast():
    js = _read("host-settings.js")
    for banned in ("localStorage", "sessionStorage", "history.pushState", "history.replaceState",
                   "document.cookie"):
        assert banned not in js, banned
    for call in re.findall(r"toast\([^)]*\)", js):
        assert "command" not in call and "made" not in call, call
    assert "copyText(shown.made.command" in js
    assert '$("cmd").textContent = shown.made.command' in js


def test_the_save_shows_the_diff_and_needs_the_dialog_before_the_request():
    js = _read("host-settings.js")
    assert js.index("diffAllowlist(settings.allowlist, built.allowlist)") \
        < js.index("await confirmDialog(") < js.index('api("PUT", hostUrl("/allowlist")')
    assert "lines," in js and "confirmed: true" in js
    # The revoke and the remove need the typed host name, the reissue a confirm dialog.
    assert re.search(r'typedConfirm\(\{\s*title: `Revoke the keys of \$\{host\}\?`, name: host', js)
    assert re.search(r'typedConfirm\(\{\s*title: `Remove \$\{host\}\?`, name: host', js)
    assert 'settings.installed ? "/enrolment/reissue" : "/enrolment/regenerate"' in js
    assert "confirm_host: host" in js


def test_polling_is_every_three_seconds_stops_when_done_and_ignores_stale_replies():
    js = _read("host-settings.js")
    assert "const POLL_MS = 3000" in js and "setTimeout(tick, POLL_MS)" in js
    assert js.count("if (gen !== pollGen) return;") == 2
    assert "shown.finished = true" in js and "shouldPoll(settings, watching)" in js
    assert not re.search(r"setTimeout\(\s*[\"'`]", js)


def test_dialog_takes_an_optional_list_of_lines_and_draws_it_as_text():
    code = _read("js/dialog.js")
    assert 'el("ul", "dlg-lines")' in code and 'list.append(el("li", null, line))' in code
    assert "showModal()" in code and "opener.focus()" in code
    assert ".dlg-lines" in _read("css/components.css")


def test_css_uses_tokens_only():
    css = _read("css/settings.css") + _read("css/components.css")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", css)


def test_the_host_page_links_to_settings_for_admins_only():
    js = _read("host.js")
    assert 'if (isAdmin) {' in js and "/hosts/${encodeURIComponent(h.host)}/settings" in js
    assert 'fetch("/api/session")' in js and "is_admin" in js
    assert "Settings" in js


def test_new_files_are_served_with_the_right_content_type(env):
    for rel in NEW_FILES[1:]:
        r = env.client.get(f"/static/{rel}")
        assert r.status_code == 200, rel
        expect = "text/css" if rel.endswith(".css") else "javascript"
        assert expect in r.headers["content-type"], rel
        assert "Content-Security-Policy" in r.headers, rel


def test_new_files_use_lf_line_endings():
    paths = [STATIC / n for n in NEW_FILES] + [
        ROOT / "observe/hosttasks.py", ROOT / "observe/taskscripts.py", Path(__file__),
        ROOT / "tests/test_host_settings.py", ROOT / "tests/test_task_scripts.py",
        ROOT / "tests/js/settings.test.mjs"]
    for path in paths:
        assert bytes([13]) not in path.read_bytes(), path.name


# ---- the rules, mirrored from settings-logic.js -------------------------------------------

def limit_text(f):
    return "no lowest duty" if f.get("min_duty_limit") is None else f"lowest duty {f['min_duty_limit']}%"


def diff_allowlist(before, after):
    """The same rules and the same wording as diffAllowlist in settings-logic.js."""
    b = before or {"fans": [], "services": [], "reboot": False}
    a = after or {"fans": [], "services": [], "reboot": False}
    lines = []
    old = {f["header"]: f for f in b.get("fans", [])}
    new = {f["header"]: f for f in a.get("fans", [])}
    for name, f in new.items():
        if name not in old:
            lines.append(f"Add fan header {name} ({limit_text(f)})")
        elif old[name].get("min_duty_limit") != f.get("min_duty_limit"):
            lines.append(f"Change fan header {name}: {limit_text(old[name])} to {limit_text(f)}")
    lines += [f"Remove fan header {n}" for n in old if n not in new]
    old_s, new_s = b.get("services", []), a.get("services", [])
    lines += [f"Add service {s}" for s in new_s if s not in old_s]
    lines += [f"Remove service {s}" for s in old_s if s not in new_s]
    if bool(b.get("reboot")) != bool(a.get("reboot")):
        lines.append("Allow reboot" if a.get("reboot") else "Do not allow reboot")
    return lines


def test_the_diff_wording_in_python_matches_the_module():
    logic = _read("js/settings-logic.js")
    for text in ("Add fan header ${name} (${limitText(f)})", "Remove fan header ${name}",
                 "Add service ${s}", "Remove service ${s}", "Allow reboot", "Do not allow reboot",
                 "Change fan header ${name}: ${limitText(old.get(name))} to ${limitText(f)}",
                 "no lowest duty", "lowest duty ${f.min_duty_limit}%"):
        assert text in logic, text


@pytest.mark.parametrize("before, after, lines", [
    ({"fans": [{"header": "fan1"}], "services": ["smbd"], "reboot": False},
     {"fans": [{"header": "fan1"}], "services": ["smbd"], "reboot": False}, []),
    ({"fans": [{"header": "fan1"}], "services": ["smbd"], "reboot": False},
     {"fans": [{"header": "fan1"}, {"header": "fan3", "min_duty_limit": 20}], "services": [],
      "reboot": True},
     ["Add fan header fan3 (lowest duty 20%)", "Remove service smbd", "Allow reboot"]),
    ({"fans": [{"header": "fan1", "min_duty_limit": 20}], "services": [], "reboot": True},
     {"fans": [{"header": "fan1", "min_duty_limit": 30}], "services": ["nfs-server"],
      "reboot": False},
     ["Change fan header fan1: lowest duty 20% to lowest duty 30%", "Add service nfs-server",
      "Do not allow reboot"]),
    ({"fans": [{"header": "fan2"}], "services": [], "reboot": False},
     {"fans": [], "services": [], "reboot": False}, ["Remove fan header fan2"]),
])
def test_diff_cases(before, after, lines):
    assert diff_allowlist(before, after) == lines


def test_client_rules_reuse_the_wizard_rules_and_the_server_patterns():
    logic = _read("js/settings-logic.js")
    assert 'from "./wizard-logic.js"' in logic
    assert "parseLimit, validHeader, validService" in logic
    for state in ("none", "pending", "written", "applied"):
        assert f"{state}:" in logic
    for state in ("waiting", "fetched", "done", "failed", "expired"):
        assert f"{state}:" in logic
    # The states the server reports are the states the page can draw.
    src = (ROOT / "observe" / "hosttasks.py").read_text(encoding="utf-8")
    for state in ("waiting", "expired", "failed", "done", "fetched", "pending", "written", "applied"):
        assert f'"{state}"' in src, state


# ---- the settings payload a page draws ----------------------------------------------------

def test_a_failed_install_does_not_count_as_a_written_allowlist(env):
    hdr = admin(env)
    script = enrol_host(env, hdr)
    assert settings(env).json()["allowlist_status"]["state"] == "written"
    assert step(env, secrets_of(script)["STEP_KEY"], "control_config", "failed").status_code == 204
    assert settings(env).json()["allowlist_status"]["state"] == "pending"


def test_task_states_are_the_ones_the_page_draws(env):
    assert hosttasks._state(None, 100.0, [], 10.0) == "waiting"
    assert hosttasks._state(None, 100.0, [], 100.0) == "expired"
    assert hosttasks._state(5.0, 100.0, [], 10.0) == "fetched"
    assert hosttasks._state(5.0, 100.0, [{"step": "done", "status": "ok"}], 10.0) == "done"
    assert hosttasks._state(5.0, 100.0, [{"step": "agent", "status": "refused"},
                                         {"step": "done", "status": "ok"}], 10.0) == "failed"
    assert asyncio.run(hosttasks.latest(env.store, "nobody", 1.0)) is None
