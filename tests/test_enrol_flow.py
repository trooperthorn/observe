"""Enrolment survives the wrong machine and explains failures (docs/GUI-DESIGN.md section 3.10).

GET /i/{token} serves a script with no keys and spends nothing. The script's guards run first and
only then does it redeem the token. A refusing guard reports why, and the token stays usable. The
address in a command is never the Host header. An expired or used command is shown with a
Regenerate button, and a host that has not reported yet is listed as waiting for first data.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from observe import enrol, scripts
from observe.config import normalise_public_url

from .conftest import make_config
from .test_auth import Env
from .test_enrol_api import admin, audit_kinds, ingest_for, run_install, token_of
from .test_enrol_api import create as create_any
from .test_install_script import (CTX, Served, check_syntax, env, red)  # noqa: F401
from .test_install_script_agents import powershell, red as agent_red

STATIC = Path(__file__).parent.parent / "observe" / "static"


def read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def create(env, hdr, **over):
    """A Linux host with the agent and control by default (the shared default is TrueNAS, whose
    script cannot serve control)."""
    return create_any(env, hdr, **{"platform": "linux", **over})


def guard(env, token, step="hostname", found="ai-pi"):
    return env.client.post("/api/enrol/guard", json={"token": token, "step": step, "found": found})


def progress(env, host="nas01"):
    return env.client.get(f"/api/v2/hosts/{host}/enrolment").json()


# ------------------------------------------------------------------ 1. fetch is not redeem


def test_a_wrong_machine_guard_failure_leaves_the_token_valid_and_the_wizard_shows_why(env):
    hdr = admin(env)
    made = create(env, hdr, name="mediain-svr", platform="linux", control=False, allowlist=None)
    token = token_of(made)
    assert env.client.get(f"/i/{token}").status_code == 200
    # The script ran on ai-pi, its hostname guard refused, and it reported the refusal.
    r = guard(env, token, found="ai-pi")
    assert r.status_code == 204
    state = progress(env, "mediain-svr")
    assert state["guard"]["reason"] == "ran on ai-pi, expected mediain-svr"
    assert state["guard"]["step"] == "hostname"
    assert state["token_state"] == "valid" and not state["expired"]
    assert env.rows("SELECT fetched_at FROM enrolments") == [(None,)]
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]
    # The same command is still good on the right machine, and then the refusal is cleared.
    assert env.client.get(f"/i/{token}").status_code == 200
    assert env.client.post("/api/enrol/redeem", json={"token": token}).status_code == 200
    after = progress(env, "mediain-svr")
    assert after["guard"] is None and after["token_state"] == "used"
    assert audit_kinds(env).count("enrol_guard_refused") == 1
    assert token not in repr(env.rows("SELECT * FROM audit"))


def test_a_running_install_is_not_offered_regenerate_until_it_stalls(env):
    hdr = admin(env)
    token = token_of(create(env, hdr, name="mediain-svr", control=False, allowlist=None))
    run_install(env, token)
    state = progress(env, "mediain-svr")
    assert state["token_state"] == "used" and state["stalled"] is False
    env.clock.now += enrol.STALL_S + 1
    assert progress(env, "mediain-svr")["stalled"] is True


def test_the_settings_page_data_shows_the_refusal_and_the_command_state(env):
    hdr = admin(env)
    token = token_of(create(env, hdr))
    before = env.client.get("/api/v2/hosts/nas01/settings").json()["enrolment"]
    assert before["token_state"] == "valid" and before["guard"] is None
    guard(env, token, step="observe_host", found="")
    got = env.client.get("/api/v2/hosts/nas01/settings").json()["enrolment"]
    assert got["guard"]["step"] == "observe_host"
    assert "Observe host" in got["guard"]["reason"] and "nas01" in got["guard"]["reason"]
    assert got["token_state"] == "valid"


@pytest.mark.parametrize("body", [
    {}, {"token": 5, "step": "hostname"}, {"token": "wpe_nonsense", "step": "hostname"},
    {"token": "garbage", "step": "hostname"}, {"token": None, "step": "root"}])
def test_a_guard_report_needs_a_live_token(env, body):
    admin(env)
    assert env.client.post("/api/enrol/guard", json=body).status_code == 404
    assert env.client.post("/api/enrol/guard", content=b"{").status_code == 404
    assert "enrol_guard_refused" not in audit_kinds(env)


def test_a_guard_report_for_an_unknown_step_used_or_expired_token_changes_nothing(env):
    hdr = admin(env)
    token = token_of(create(env, hdr))
    assert guard(env, token, step="download").status_code == 404
    assert guard(env, token, step="rm -rf").status_code == 404
    assert progress(env)["guard"] is None
    asyncio.run(env.store.execute("UPDATE enrolments SET expires_at=?", (env.clock() - 1,)))
    assert guard(env, token).status_code == 404
    asyncio.run(env.store.execute("UPDATE enrolments SET expires_at=?", (env.clock() + 600,)))
    run_install(env, token)
    assert guard(env, token).status_code == 404
    assert progress(env)["guard"] is None


def test_the_reported_machine_name_is_cut_to_a_host_name_before_it_is_kept(env):
    hdr = admin(env)
    token = token_of(create(env, hdr))
    guard(env, token, found='ai pi;<b>rm -rf</b>$(id)`x`' + "z" * 100)
    reason = progress(env)["guard"]["reason"]
    assert re.fullmatch(r"ran on [A-Za-z0-9._-]{1,64}, expected nas01", reason), reason
    assert "<" not in reason and ";" not in reason and "$" not in reason
    # An empty or non-string name falls back to the fixed reason.
    guard(env, token, found="")
    assert progress(env)["guard"]["reason"] == "the host name does not match (expected nas01)"
    env.client.post("/api/enrol/guard", json={"token": token, "step": "root", "found": 7})
    assert "root" in progress(env)["guard"]["reason"]


def test_regenerate_clears_an_old_refusal(env):
    hdr = admin(env)
    token = token_of(create(env, hdr))
    guard(env, token)
    assert progress(env)["guard"] is not None
    assert env.client.post("/api/hosts/nas01/enrolment/regenerate", json={},
                           headers=hdr).status_code == 200
    assert progress(env)["guard"] is None
    assert guard(env, token).status_code == 404  # the old token is dead


def test_the_fetched_script_holds_no_key_and_redeem_gives_each_chosen_key_once(env):
    hdr = admin(env)
    token = token_of(create(env, hdr))
    got = run_install(env, token)
    assert re.search(r"STEP_KEY='wps_", got.text) and re.search(r"AGENT_KEY='wpi_", got.text)
    assert re.search(r"CONTROL_KEY='wpc_", got.text)
    assert env.client.post("/api/enrol/redeem", json={"token": token}).status_code == 410
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(2,)]


# ------------------------------------------------------------------ the scripts themselves


def deferred(**over):
    return red(redeem_token="wpe_" + "t" * 43, **over)


@pytest.mark.parametrize("platform", ["linux", "raspberry-pi"])
@pytest.mark.parametrize("option", ["agent and control", "agent only"])
def test_a_keyless_linux_script_passes_a_syntax_check(tmp_path, platform, option):
    over = {} if option == "agent and control" else {
        "control_key": None, "allowlist": {"fans": [], "services": [], "reboot": False}}
    script = scripts.render_linux(deferred(platform=platform, **over), CTX)
    check_syntax(script, tmp_path)
    assert "wpi_" not in script and "wpc_" not in script and "wps_" not in script
    assert "REDEEM_TOKEN='wpe_" in script and "STEP_KEY=''" in script


def test_a_keyless_truenas_script_passes_a_syntax_check(tmp_path):
    script = scripts.render_truenas(agent_red("truenas", redeem_token="wpe_" + "t" * 43), CTX)
    check_syntax(script, tmp_path)
    assert "wpi_" not in script and "REDEEM_TOKEN='wpe_" in script


def test_the_script_redeems_only_after_every_guard_and_before_any_change():
    script = scripts.render_linux(deferred(), CTX)
    main = script[script.index("main() {"):script.index("install_agent() {")]
    guards = [main.index(g) for g in ('id -u', '"$l_short" != "$want"',
                                      '"$here_id" = "$OBSERVE_MACHINE_ID"', '"$addr" = "$mine"')]
    redeem = main.index("\n  redeem\n")
    first_change = min(main.index(c) for c in ("mkdir", "install_agent", "install_control"))
    assert guards == sorted(guards) and guards[-1] < redeem < first_change
    # A refusal reports to the guard endpoint with the token, not with a step key.
    head = script[:script.index("main() {")]
    assert "/api/enrol/guard" in head and "/api/enrol/redeem" in head
    assert "FOUND=$l_short" in main
    # The token and the reply never reach a command line: curl reads the body on stdin.
    assert head.count("--data-binary @-") == 2
    assert "The command was not used up" in head


def test_the_powershell_script_redeems_after_its_guards_and_reports_refusals_with_the_token(tmp_path):
    script = scripts.render_windows(agent_red("windows", redeem_token="wpe_" + "t" * 43), CTX)
    assert "wpi_" not in script and "$RedeemToken = 'wpe_" in script
    guards = [script.index(g) for g in ("IsInRole", "$mine -notcontains $want", "$hereAddrs -contains $addr")]
    redeem = script.index("/api/enrol/redeem")
    assert guards == sorted(guards) and guards[-1] < redeem < script.index("Get-Command python")
    assert "Send-Guard $Step" in script and "/api/enrol/guard" in script
    exe = powershell()
    if exe is None:
        return
    import subprocess
    path = tmp_path / "install.ps1"
    path.write_bytes(script.encode("utf-8"))
    code = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$args[0],[ref]$t,[ref]$e);if($e.Count){$e|ForEach-Object{$_.ToString()};exit 1}")
    done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command",
                           "& { " + code + " }", str(path)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_bad_redeem_token_is_never_written_into_a_script():
    for bad in ("wpe_x'; id; '", "wpe_short", "nope", "wpe_" + "a" * 10 + "\n"):
        with pytest.raises(scripts.ScriptError):
            scripts.render_linux(red(redeem_token=bad), CTX)


# ------------------------------------------------------------------ 2. the address


@pytest.fixture
def bare(tmp_path):
    e = Served(tmp_path, public_url=None)
    yield e
    e.client.close()
    e.store.close()


def test_a_missing_address_is_asked_for_and_costs_no_token(bare):
    hdr = admin(bare)
    r = bare.client.post("/api/hosts", json={"name": "nas01", "platform": "linux", "agent": True},
                         headers=hdr)
    assert r.status_code == 409 and r.json()["code"] == "public_url_required"
    assert "address" in r.json()["detail"]
    assert bare.rows("SELECT COUNT(*) FROM enrolments") == [(0,)]


def test_the_admin_confirms_the_address_once_and_commands_use_it(bare):
    hdr = admin(bare)
    body = {"name": "nas01", "platform": "linux", "agent": True}
    assert bare.client.post("/api/hosts", json=body, headers=hdr).status_code == 409
    # Even a hostile Host header on the save does not become the address.
    saved = bare.client.put("/api/enrol/public-url", json={"url": "https://Observe.Lab:8443"},
                            headers={**hdr, "Host": "evil.example"})
    assert saved.status_code == 200 and saved.json() == {"url": "https://observe.lab:8443",
                                                         "source": "saved"}
    made = bare.client.post("/api/hosts", json=body, headers={**hdr, "Host": "evil.example"})
    assert made.status_code == 200
    seen = bare.client.get("/api/v2/hosts/nas01/settings").json()
    assert seen["public_url"] == {"url": "https://observe.lab:8443", "source": "saved"}
    assert made.json()["command"].splitlines()[1].startswith(
        "curl -fsSL 'https://observe.lab:8443/i/wpe_")
    assert "enrol_public_url_set" in audit_kinds(bare)
    token = token_of(made)
    got = bare.client.get(f"/i/{token}")
    assert "OBSERVE_URL='https://observe.lab:8443'" in got.text


@pytest.mark.parametrize("bad", [
    "http://localhost:8080", "http://LOCALHOST", "https://app.localhost", "http://127.0.0.1:8080",
    "http://127.1.2.3", "http://[::1]:8080", "http://0.0.0.0", "http://[::]",
    "https://observe.lab/path", "https://observe.lab/", "https://observe.lab?x=1",
    "https://observe.lab;id", "https://observe.lab|sh", "https://observe.lab'x", "https://x`id`",
    "https://$(id).lab", "https://observe.lab:99999", "https://observe.lab:0x", "ftp://observe.lab",
    "observe.lab", "", "https://", "https://user@observe.lab", "https://a b.lab",
    "https://observe.lab\nx", "https://observe..lab", 5, None, ["https://observe.lab"]])
def test_an_unsafe_or_loopback_address_is_refused_and_nothing_is_saved(bare, bad):
    hdr = admin(bare)
    r = bare.client.put("/api/enrol/public-url", json={"url": bad}, headers=hdr)
    assert r.status_code == 422, bad
    assert bare.rows("SELECT COUNT(*) FROM app_settings") == [(0,)]
    assert "enrol_public_url_failed" in audit_kinds(bare)
    with pytest.raises(ValueError):
        normalise_public_url(bad)


def test_the_address_route_is_admin_only_and_needs_csrf(bare):
    hdr = admin(bare)
    url = "/api/enrol/public-url"
    assert bare.client.put(url, json={"url": "https://observe.lab"}).status_code in (401, 403)
    bare.client.post("/api/logout", headers=hdr)
    bare.user("bob")
    bob = bare.csrf(bare.login("bob"))
    assert bare.client.put(url, json={"url": "https://observe.lab"}, headers=bob).status_code == 403
    assert bare.client.get(url).status_code == 405  # the address is read from the host settings
    assert bare.client.get("/api/v2/hosts/nas01/settings").status_code == 403
    assert bare.rows("SELECT COUNT(*) FROM app_settings") == [(0,)]


def test_a_configured_address_wins_and_cannot_be_changed_in_the_console(tmp_path):
    e = Env(tmp_path, public_url="https://observe.example.org")
    try:
        hdr = admin(e)
        r = e.client.put("/api/enrol/public-url", json={"url": "https://other.lab"}, headers=hdr)
        assert r.status_code == 409 and "server.public_url" in r.json()["detail"]
        made = create(e, hdr)
        assert "https://observe.example.org/i/wpe_" in made.json()["command"]
        assert e.client.get("/api/v2/hosts/nas01/settings").json()["public_url"] == {
            "url": "https://observe.example.org", "source": "config"}
    finally:
        e.client.close()
        e.store.close()


@pytest.mark.parametrize("bad", ["http://localhost:8080", "http://127.0.0.1", "https://x/y",
                                 "https://x;id", "ftp://x"])
def test_the_config_refuses_a_bad_public_url(bad):
    with pytest.raises(ValidationError):
        make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                    server={"public_url": bad})


def test_the_config_normalises_a_good_public_url():
    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                      server={"public_url": "HTTPS://Observe.Lab:8443"})
    assert cfg.server.public_url == "https://observe.lab:8443"
    assert make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}]).server.public_url is None


def test_every_command_route_asks_for_the_address_when_it_is_missing(bare):
    hdr = admin(bare)
    # An enrolment made while an address existed, then the saved address goes away.
    asyncio.run(enrol.set_public_url(bare.store, "https://observe.lab", bare.clock()))
    token = token_of(create(bare, hdr))
    run_install(bare, token)
    asyncio.run(bare.store.execute("DELETE FROM app_settings"))
    for path, body in (("/api/hosts/nas01/enrolment/reissue", {"confirmed": True}),
                       ("/api/hosts/nas01/tasks", {"kind": "cleanup", "confirmed": True})):
        r = bare.client.post(path, json=body, headers=hdr)
        assert r.status_code == 409 and r.json()["code"] == "public_url_required", path
    assert bare.rows("SELECT COUNT(*) FROM host_tasks") == [(0,)]
    # Reissue did not revoke anything, because it asked first.
    assert bare.rows("SELECT COUNT(*) FROM ingest_keys WHERE revoked_at IS NULL") == [(2,)]


def test_the_wizard_and_settings_page_ask_for_a_missing_address():
    for page, script in (("hosts-new.html", "hosts-new.js"), ("host-settings.html", "host-settings.js")):
        html, js = read(page), read(script)
        for ident in ("public-url-box", "public-url-form", "public-url", "public-url-status"):
            assert f'id="{ident}"' in html, (page, ident)
        assert "askPublicUrl(urlParts, csrf)" in js and "NEEDS_URL" in js
    helper = read("js/public-url.js")
    assert '"/api/enrol/public-url"' in helper and "validPublicUrl" in helper
    assert "code" in read("js/api.js")


# ------------------------------------------------------------------ 3. expired and used


def test_progress_names_the_command_state_valid_used_or_expired(env):
    hdr = admin(env)
    token = token_of(create(env, hdr))
    assert progress(env)["token_state"] == "valid"
    asyncio.run(env.store.execute("UPDATE enrolments SET expires_at=?", (env.clock() - 1,)))
    expired = progress(env)
    assert expired["token_state"] == "expired" and expired["expired"] is True
    assert env.client.get("/api/v2/hosts/nas01/settings").json()["enrolment"]["token_state"] == "expired"
    made = env.client.post("/api/hosts/nas01/enrolment/regenerate", json={}, headers=hdr)
    assert made.status_code == 200 and progress(env)["token_state"] == "valid"
    run_install(env, token_of(made))
    used = progress(env)
    assert used["token_state"] == "used" and not used["expired"]
    assert env.client.get("/api/v2/hosts/nas01/settings").json()["enrolment"]["token_state"] == "used"
    # Used: Regenerate is the reissue route, which revokes the keys the old command made.
    again = env.client.post("/api/hosts/nas01/enrolment/reissue", json={"confirmed": True},
                            headers=hdr)
    assert again.status_code == 200 and again.json()["keys_revoked"] == 2
    assert progress(env)["token_state"] == "valid"


def test_the_wizard_and_settings_page_show_expired_and_used_with_regenerate():
    logic = read("js/wizard-logic.js")
    assert 'kind: "expired"' in logic and 'kind: "used"' in logic
    assert 'progress.token_state === "used"' in logic and "Regenerate command" in logic
    wizard = read("hosts-new.js")
    assert "noticeFor(latest)" in wizard and "enrolment/${path}" in wizard
    assert 'latest.token_state === "used"' in wizard
    settings = read("host-settings.js")
    assert "noticeFor(" in settings and "drawTokenState" in settings
    assert 'id="token-state"' in read("host-settings.html")
    assert 'id="regen"' in read("host-settings.html") and 'id="regen"' in read("hosts-new.html")
    # Refusals on the wrong machine show in both places.
    assert "guardText(" in wizard and "guardText(" in settings
    assert 'id="guard"' in read("hosts-new.html") and 'id="guard-note"' in read("host-settings.html")


# ------------------------------------------------------------------ 4. waiting for first data


def hosts(env):
    listed = env.client.get("/api/v2/hosts").json()["items"]
    waiting = env.client.get("/api/v2/waiting-hosts").json()["items"]
    return {"hosts": listed, "waiting": waiting}


def test_an_enrolled_host_that_has_not_reported_is_listed_as_waiting_with_a_link(env):
    hdr = admin(env)
    token = token_of(create(env, hdr, control=False, allowlist=None))
    first = hosts(env)
    assert first["hosts"] == []
    (row,) = first["waiting"]
    assert row["host"] == "nas01" and row["state"] == "waiting"
    assert row["enrolment_url"] == "/hosts/nas01/settings"
    assert row["note"] == "command not run yet" and row["platform"] == "linux"
    guard(env, token)
    assert hosts(env)["waiting"][0]["note"].startswith("refused: ran on ai-pi")
    keys = run_install(env, token)
    assert hosts(env)["waiting"][0]["note"] == "install started, waiting for first data"
    # The first batch moves it into the hosts list.
    key = re.search(r"AGENT_KEY='([^']+)'", keys.text).group(1)
    assert ingest_for(env, "nas01", key).status_code in (200, 202, 204)
    after = hosts(env)
    assert after["waiting"] == [] and [h["host"] for h in after["hosts"]] == ["nas01"]


def test_an_expired_command_is_listed_as_waiting_with_the_way_out(env):
    hdr = admin(env)
    create(env, hdr)
    asyncio.run(env.store.execute("UPDATE enrolments SET expires_at=?", (env.clock() - 1,)))
    assert hosts(env)["waiting"][0]["note"] == "command expired, regenerate it"


def test_a_control_only_enrolment_is_not_waiting_for_agent_data(env):
    hdr = admin(env)
    create(env, hdr, agent=False)
    assert hosts(env)["waiting"] == []


def test_the_hosts_list_is_session_only(env):
    hdr = admin(env)
    create(env, hdr)
    env.client.post("/api/logout", headers=hdr)
    assert env.client.get("/api/v2/hosts").status_code in (401, 403)
    assert env.client.get("/api/v2/waiting-hosts").status_code in (401, 403)


def test_the_dashboard_has_a_waiting_panel_written_with_text_only():
    html, js = read("index.html"), read("app.js")
    assert 'id="waiting-panel"' in html and 'id="waiting"' in html
    assert "Waiting for first data" in html and "Waiting for first data" in js
    assert "/api/v2/waiting-hosts" in js and "renderWaiting()" in js
    assert "encodeURIComponent(w.host)" in js and "/settings" in js
    assert not re.search(r"\.innerHTML\s*=|insertAdjacentHTML", js)
