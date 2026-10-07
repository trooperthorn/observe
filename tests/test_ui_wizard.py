"""The Add host wizard page from docs/GUI-DESIGN.md section 3.10 and slice S12, and the
regenerate route it uses when an install command expires."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from observe import enrol

from .test_auth import Env
from .test_enrol_api import admin, audit_kinds, create, redeem, token_of

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "observe" / "static"
BANNED = r"\.innerHTML\s*=|outerHTML|insertAdjacentHTML|document\.write|eval\("
HOSTILE = '<img src=x onerror="alert(1)">'
IDS = ["page", "msg", "steps", "expired", "regen", "step-host", "form-host", "host-name",
       "name-status", "platform", "pool", "platform-note", "step-agent", "form-agent",
       "agent-on", "control-on", "control-reason", "step-allowlist", "form-allow", "fan-list",
       "fan-new", "fan-add", "service-list", "service-new", "service-add", "reboot", "create",
       "step-install", "install-sub", "cmd", "copy", "expiry", "ran", "step-live", "progress",
       "reports", "done", "open-host", "again"]
ORDER = ['href="/static/css/tokens.css"', 'href="/static/css/base.css"',
         'href="/static/css/shell.css"', 'href="/static/css/components.css"',
         'href="/static/css/admin.css"',
         'href="/static/css/wizard.css"']
NEW_FILES = ["hosts-new.html", "hosts-new.js", "js/wizard-logic.js", "css/wizard.css"]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def _read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


# ---- the page ------------------------------------------------------------------------------

def test_page_markup_ids_styles_and_scripts(env):
    env.user("root", admin=True)
    assert env.login("root").status_code == 200
    r = env.client.get("/hosts/new")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    pos = [html.find(o) for o in ORDER]
    assert all(p >= 0 for p in pos) and pos == sorted(pos)
    for ident in ["summary", "shell-nav", *IDS]:
        assert f'id="{ident}"' in html, ident
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert len(scripts) == 2 and all('type="module"' in s for s in scripts)
    assert 'src="/static/hosts-new.js"' in scripts[0] and "/static/js/shell.js" in scripts[1]
    assert "<title>Add host - Observe</title>" in html
    assert HOSTILE not in html


def test_steps_list_is_an_ordered_list_of_five_numbered_names():
    html = _read("hosts-new.html")
    block = html[html.index('<ol class="steps"'):html.index("</ol>")]
    items = re.findall(r'<li data-step="(\w+)"><span class="step-n">(\d)</span>'
                       r'<span class="step-name">([^<]+)</span></li>', block)
    assert [i[0] for i in items] == ["host", "agent", "allowlist", "install", "live"]
    assert [i[1] for i in items] == ["1", "2", "3", "4", "5"]
    assert 'aria-current' in _read("hosts-new.js") and '"step"' in _read("hosts-new.js")


def test_progress_is_an_aria_live_list_and_every_step_panel_is_labelled():
    html = _read("hosts-new.html")
    assert re.search(r'<ol class="progress" id="progress" aria-live="polite">', html)
    assert 'id="reports" aria-live="polite"' in html
    for step in ("host", "agent", "allowlist", "install", "live"):
        assert f'<section class="card" id="step-{step}" aria-labelledby="step-{step}-h" hidden>' in html
    assert 'id="expired" role="alert"' in html


def test_fieldsets_have_legends_and_inputs_have_labels():
    html = _read("hosts-new.html")
    assert html.count("<fieldset") == html.count("<legend>") == 4
    for m in re.finditer(r"<input\b", html):
        before = html[:m.start()]
        assert before.rfind("<label") > before.rfind("</label>"), html[m.start():m.start() + 60]
    assert 'id="host-name"' in html and "aria-describedby" in html


def test_the_command_block_is_a_pre_filled_with_text_and_has_a_copy_button():
    html = _read("hosts-new.html")
    assert '<pre class="cmd" id="cmd"' in html and 'id="copy"' in html
    js = _read("hosts-new.js")
    assert '$("cmd").textContent = created.command' in js
    assert 'copyText(created.command, $("cmd"))' in js
    assert 'created.host} · ${created.platform_label}' in js


def test_scripts_are_modules_that_write_only_text():
    for rel in ("hosts-new.js", "js/wizard-logic.js"):
        js = _read(rel)
        assert not re.search(BANNED, js), rel
        assert "pill" not in js and not re.search(r"\.style|setAttribute\(.style", js), rel
    assert 'import { el, clear } from "/static/js/dom.js"' in _read("hosts-new.js")


def test_the_command_never_reaches_a_url_storage_or_a_toast():
    js = _read("hosts-new.js")
    for banned in ("localStorage", "sessionStorage", "history.pushState", "document.cookie"):
        assert banned not in js, banned
    # The one replaceState call carries only a step name.
    assert re.findall(r"history\.replaceState\([^)]*\)", js) == [
        'history.replaceState(null, "", `#${step}`)']
    assert "toast(" not in js
    # copyText toasts a fixed sentence, not the text it copies.
    helper = _read("js/admin-ui.js")
    assert "toast(text" not in helper and 'toast("Copied to the clipboard."' in helper


def test_polling_is_every_three_seconds_stops_when_done_and_ignores_stale_replies():
    js = _read("hosts-new.js")
    assert "const POLL_MS = 3000" in js and "interval: POLL_MS" in js
    assert "if (next.ready || next.expired) handle.stop();" in js
    assert js.count("if (signal.aborted) return;") == 1
    assert "setTimeout" not in js and "setInterval" not in js
    assert "/enrolment`" in js and 'const path = used ? "reissue" : "regenerate"' in js
    assert not re.search(r"setTimeout\(\s*[\"'`]", js)


def test_regenerate_is_offered_on_expiry_and_controls_follow_the_platform():
    js = _read("hosts-new.js")
    assert "noticeFor(latest)" in js and "$(\"regen\")" in js
    assert "p.expired" in _read("js/wizard-logic.js") or "progress.expired" in _read("js/wizard-logic.js")
    logic = _read("js/wizard-logic.js")
    assert 'platform === "windows"' in logic and 'platform === "truenas"' in logic
    assert 'pwm-fan' in logic
    assert "control-reason" in _read("hosts-new.html")


def test_wizard_css_uses_tokens_only_and_the_pulse_is_covered_by_reduced_motion():
    css = _read("css/wizard.css")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", css)
    assert "wiz-pulse" in css
    assert "prefers-reduced-motion" in _read("css/base.css")


def test_nav_lists_add_host_under_hosts_for_admins_only():
    shell = _read("js/shell.js")
    assert ('{ workspace: "hosts", label: "Add host", href: "/hosts/new", also: [], '
            'admin: true }') in shell


def test_new_files_are_served_with_the_right_content_type(env):
    for rel in NEW_FILES[1:]:
        r = env.client.get(f"/static/{rel}")
        assert r.status_code == 200, rel
        expect = "text/css" if rel.endswith(".css") else "javascript"
        assert expect in r.headers["content-type"], rel
        assert "Content-Security-Policy" in r.headers, rel


def test_new_files_use_lf_line_endings():
    paths = [STATIC / n for n in NEW_FILES] + [Path(__file__), ROOT / "tests/js/wizard.test.mjs"]
    for path in paths:
        assert bytes([13]) not in path.read_bytes(), path.name


def test_client_rules_match_the_server_rules():
    logic = _read("js/wizard-logic.js")
    pairs = {"NAME_RE": enrol._NAME, "HEADER_RE": enrol._HEADER, "SERVICE_RE": enrol._SERVICE,
             "POOL_RE": enrol.POOL}
    for const, server in pairs.items():
        m = re.search(rf"export const {const} = /(.+)/;", logic)
        assert m and m.group(1) == server.pattern, const
    for name, label in enrol.PLATFORMS.items():
        assert f'"{name}"' in logic or name in logic
        assert label in logic
    for platform in enrol.PLATFORMS:
        assert f'value="{platform}"' in _read("hosts-new.html")


# ---- access and hostile input --------------------------------------------------------------

def test_a_hostile_host_name_is_refused_by_the_server_and_never_echoed_in_the_page(env):
    hdr = admin(env)
    r = create(env, hdr, name=HOSTILE)
    assert r.status_code == 422 and HOSTILE not in r.text
    assert env.rows("SELECT COUNT(*) FROM enrolments") == [(0,)]
    assert HOSTILE not in env.client.get("/hosts/new").text
    assert audit_kinds(env).count("enrol_create_failed") == 1


def test_create_and_regenerate_are_admin_only_with_csrf(env):
    hdr = admin(env)
    assert create(env, hdr).status_code == 200
    url = "/api/hosts/nas01/enrolment/regenerate"
    assert env.client.post(url, json={}).status_code in (401, 403)  # no CSRF header
    env.client.post("/api/logout", headers=hdr)
    env.user("bob")
    bob = env.csrf(env.login("bob"))
    assert env.client.post(url, json={}, headers=bob).status_code == 403
    assert env.client.get("/api/v2/hosts/nas01/enrolment").status_code == 403
    assert create(env, bob, name="nas02").status_code == 403


def test_signed_out_visitor_gets_no_regenerate(env):
    r = env.client.post("/api/hosts/nas01/enrolment/regenerate", json={})
    assert r.status_code in (401, 403)


# ---- regenerate ----------------------------------------------------------------------------

def test_regenerate_after_expiry_issues_a_working_token_and_kills_the_old_one(env):
    hdr = admin(env)
    old = token_of(create(env, hdr))
    env.clock.now += enrol.TOKEN_TTL_S
    assert env.client.get("/api/v2/hosts/nas01/enrolment").json()["state"] == "expired"
    r = env.client.post("/api/hosts/nas01/enrolment/regenerate", json={}, headers=hdr)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    new = token_of(r)
    assert new != old and body["host"] == "nas01" and body["platform_label"] == "TrueNAS"
    assert body["command"].startswith("# Observe install for nas01 (TrueNAS).")
    assert body["ttl_s"] == enrol.TOKEN_TTL_S
    assert body["expires_at"] == env.clock() + enrol.TOKEN_TTL_S
    prog = env.client.get("/api/v2/hosts/nas01/enrolment").json()
    assert prog["state"] == "waiting" and prog["expired"] is False
    assert redeem(env, old) is None
    got = redeem(env, new)
    assert got is not None and got.host == "nas01" and got.control_key is not None
    assert got.allowlist["fans"] == [{"header": "fan1"}, {"header": "fan2", "min_duty_limit": 20}]
    assert got.allowlist["services"] == ["smbd", "docker:scrutiny"] and got.allowlist["reboot"]


def test_regenerate_before_expiry_also_revokes_the_old_token(env):
    hdr = admin(env)
    old = token_of(create(env, hdr))
    r = env.client.post("/api/hosts/nas01/enrolment/regenerate", json={}, headers=hdr)
    assert r.status_code == 200
    assert redeem(env, old) is None and redeem(env, token_of(r)) is not None


def test_regenerate_is_refused_once_the_script_was_fetched_or_for_an_unknown_host(env):
    hdr = admin(env)
    token = token_of(create(env, hdr))
    assert redeem(env, token) is not None
    r = env.client.post("/api/hosts/nas01/enrolment/regenerate", json={}, headers=hdr)
    assert r.status_code == 404
    r = env.client.post("/api/hosts/ghost/enrolment/regenerate", json={}, headers=hdr)
    assert r.status_code == 404
    assert audit_kinds(env).count("enrol_regenerate_failed") == 2


def test_regenerate_keeps_the_truenas_pool_and_audits_without_the_token(env):
    hdr = admin(env)
    create(env, hdr, control=False, allowlist=None, pool="Tank")
    r = env.client.post("/api/hosts/nas01/enrolment/regenerate", json={"pool": "Tank"},
                        headers=hdr)
    assert "?pool=Tank" in r.json()["command"]
    r = env.client.post("/api/hosts/nas01/enrolment/regenerate", json={"pool": "a/b"},
                        headers=hdr)
    assert "?pool" not in r.json()["command"]
    token = token_of(r)
    rows = env.rows("SELECT kind, detail FROM audit WHERE kind='enrol_regenerated'")
    assert len(rows) == 2 and all(token not in str(row) for row in rows)
    assert "enrol_regenerated" in audit_kinds(env)


def test_regenerate_does_not_log_the_token(env, caplog):
    hdr = admin(env)
    create(env, hdr)
    with caplog.at_level("DEBUG"):
        r = env.client.post("/api/hosts/nas01/enrolment/regenerate", json={}, headers=hdr)
    assert token_of(r) not in caplog.text


def test_regenerated_enrolment_expiry_is_audited_again(env):
    hdr = admin(env)
    create(env, hdr)
    env.clock.now += enrol.TOKEN_TTL_S
    env.client.get("/api/v2/hosts/nas01/enrolment")
    env.client.post("/api/hosts/nas01/enrolment/regenerate", json={}, headers=hdr)
    env.clock.now += enrol.TOKEN_TTL_S
    env.client.get("/api/v2/hosts/nas01/enrolment")
    assert audit_kinds(env).count("enrol_expired") == 2
