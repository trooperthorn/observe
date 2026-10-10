"""GET /i/{token} and POST /api/enrol/step: the Linux and Raspberry Pi install script.

The TrueNAS and Windows scripts are tested in test_install_script_agents.py."""

from __future__ import annotations

import asyncio
import re
import shutil
import tomllib
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from observe import enrol, scripts
from observe.alerts import Alerter
from observe.plugins import LoadedPlugin, LoadedPlugins
from observe.scheduler import Scheduler
from observe.web import create_app

from .test_auth import Env
from .test_enrol_api import admin, audit_kinds, create, run_install, token_of

PUBLIC = "ed25519:" + "A" * 43 + "="
MACHINE_ID = "0123456789abcdef0123456789abcdef"
AGENT_KEY = "wpi_" + "a" * 12 + "_" + "b" * 43
CONTROL_KEY = "wpc_" + "c" * 12 + "_" + "d" * 43
STEP_KEY = "wps_" + "e" * 43
ALLOW = {"fans": [{"header": "pwm1", "min_duty_limit": 30}, "pwm2"],
         "services": ["smbd", "docker:scrutiny"], "reboot": True}
NO_ALLOW = {"fans": [], "services": [], "reboot": False}


def loaded_control() -> LoadedPlugins:
    fake = SimpleNamespace(name="control", public_key=PUBLIC)
    return LoadedPlugins((LoadedPlugin(fake, (), (), (), (), None, (), {}, None),))


class Served(Env):
    """The standard test environment, with a control plugin loaded so control can be served."""

    def __init__(self, tmp_path, plugins=None, **server) -> None:
        server.setdefault("public_url", "https://192.0.2.50:8443")
        super().__init__(tmp_path, **server)
        self.client.close()
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        app = create_app(self.cfg, self.store, sched, alerter, auth_clock=self.clock,
                         plugins=plugins if plugins is not None else loaded_control())
        app.state.observe_machine_id = MACHINE_ID
        self.client = TestClient(app, base_url="https://192.0.2.50:8443")


@pytest.fixture
def env(tmp_path):
    e = Served(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def red(**over):
    base = dict(host="nas01", platform="linux", agent_key=AGENT_KEY, control_key=CONTROL_KEY,
                step_key=STEP_KEY, allowlist=ALLOW)
    base.update(over)
    return enrol.Redeemed(**base)


CTX = scripts.Context("https://observe.lan:8443", MACHINE_ID, ("192.0.2.50",), PUBLIC)


def check_syntax(script: str, tmp_path) -> None:
    path = tmp_path / "install.sh"
    path.write_bytes(script.encode("utf-8"))
    for shell in ("bash", "sh"):
        exe = shutil.which(shell)
        assert exe, f"{shell} is needed for the syntax check"
        done = subprocess.run([exe, "-n", str(path)], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr


OPTIONS = {"agent and control": {}, "agent only": {"control_key": None, "allowlist": NO_ALLOW},
           "control only": {"agent_key": None},
           "control without allowlist": {"allowlist": NO_ALLOW}}


@pytest.mark.parametrize("platform", ["linux", "raspberry-pi"])
@pytest.mark.parametrize("option", sorted(OPTIONS))
def test_rendered_scripts_pass_a_syntax_check(tmp_path, platform, option):
    script = scripts.render_linux(red(platform=platform, **OPTIONS[option]), CTX)
    check_syntax(script, tmp_path)
    assert script.rstrip().endswith('main "$@"') and script.count('main "$@"') == 1


def test_control_option_has_the_account_venv_config_sudoers_and_unit_steps():
    script = scripts.render_linux(red(), CTX)
    for need in ("hostwatch-control", "venv", "git+https://github.com/trooperthorn/hostwatch.git",
                 "render_sudoers", "visudo -c -f", "/etc/hostwatch/control.toml",
                 "/etc/hostwatch/control.env", "systemctl restart hostwatch-control",
                 "chmod 0640", "observe_public_key", 'headers = ["pwm1", "pwm2"]',
                 "min_duty_floor = 30", 'restart = ["smbd", "docker:scrutiny"]', "allow = true",
                 "/run/thermalctl:/run/thermalctl:ro", "chmod 0600", "machine_id"):
        assert need in script, need
    assert script.index("visudo -c -f") < script.index("install -o root -g root -m 0440")
    agent_only = scripts.render_linux(red(control_key=None, allowlist=NO_ALLOW), CTX)
    assert "WANT_CONTROL=0" in agent_only and "WANT_AGENT=1" in agent_only
    assert "[fan]" not in agent_only


def test_guards_come_before_any_change():
    script = scripts.render_linux(red(), CTX)
    head = script[:script.index("main() {")]
    main = script[script.index("main() {"):script.index("install_agent() {")]
    root = main.index("id -u")
    host = main.index('"$l_short" != "$want"')
    observe_id = main.index('"$here_id" = "$OBSERVE_MACHINE_ID"')
    observe_addr = main.index('"$addr" = "$mine"')
    first_change = min(main.index(c) for c in ("mkdir", "install_agent", "install_control"))
    assert root < host < observe_id < observe_addr < first_change
    for part in ("hostname -s", "hostname -f", "lower"):
        assert part in main
    assert "This command was made for the host" in main
    assert "This machine's hostname is" in main
    for forbidden in ("useradd", "docker run", "pip install", "systemctl", "mkdir"):
        assert forbidden not in head
    last_code = [ln for ln in script.splitlines() if ln.strip()][-1]
    assert last_code == 'main "$@"'


def test_rerun_detection_and_idempotent_steps():
    script = scripts.render_linux(red(), CTX)
    assert "An earlier install was found" in script and "report rerun" in script
    assert "if ! id hostwatch-control" in script
    assert "docker rm -f hostwatch-agent" in script
    assert 'if [ ! -x "$VENV/bin/python" ]' in script


def test_keys_are_never_echoed_or_sent_in_a_report():
    script = scripts.render_linux(red(), CTX)
    for key in (AGENT_KEY, CONTROL_KEY, STEP_KEY):
        assert script.count(key) == 1  # only its single-quoted assignment
    assert "set -x" not in script
    for note in re.findall(r"^\s*(?:report|fail|refuse) \S+ (?:\S+ )?(\".*)$", script, flags=re.M):
        assert "KEY" not in note
    # redeem() is the one function that receives keys. It reads them from curl's reply with sed
    # and checks them; nothing in it prints or sends one.
    redeem_fn = script[script.index("redeem() {"):script.index("main() {")]
    for line in script.replace(redeem_fn, "").splitlines():
        if "_KEY" in line and "$" in line:
            assert line.lstrip().startswith(("printf", "[ -n"))
    for line in redeem_fn.splitlines():
        if "_KEY" in line:
            assert not re.search(r"\b(?:say|printf|echo|curl)\b.*\$(?:\{)?(?:STEP|AGENT|CONTROL)_KEY",
                                 line), line
    assert "-K -" in script and "Bearer %s" in script


@pytest.mark.parametrize("bad", ["nas01; rm -rf /", "nas01'", "NAS01", "a b", "$(id)", "a`id`",
                                 "nas01\nx", "nas01\n", "", "x" * 64, "-nas"])
def test_hostile_host_names_are_not_rendered(bad):
    with pytest.raises(scripts.ScriptError):
        scripts.render_linux(red(host=bad), CTX)


def test_the_update_table_follows_the_allowlist_and_never_turns_on_the_daemon_self_update():
    on = scripts.control_toml("nas01", {**NO_ALLOW, "update": True}, PUBLIC)
    assert on.endswith("\n[update]\nagent = true\ncontrol = false\n")
    off = scripts.control_toml("nas01", {**NO_ALLOW, "update": False}, PUBLIC)
    assert "[update]" not in off and "[update]" not in scripts.control_toml("nas01", NO_ALLOW, PUBLIC)
    script = scripts.render_linux(red(allowlist={**ALLOW, "update": True}), CTX)
    assert "[update]" in script and "control = false" in script
    with pytest.raises(scripts.ScriptError):
        scripts.render_linux(red(control_key=None, allowlist={**NO_ALLOW, "update": True}), CTX)


@pytest.mark.parametrize("allow", [
    {"fans": ["fan;id"]}, {"fans": ["fan\n"]}, {"services": ["smbd\n"]},
    {"services": ["docker:web\n"]}, {"fans": ["a b"]}, {"fans": ["$(id)"]},
    {"fans": [{"header": "f", "min_duty_limit": 101}]},
    {"services": ["smbd'; id; '"]}, {"services": ["a|b"]}, {"services": ["x\ny"]},
    {"services": ["a@b"]}, {"services": ["a..b"]}, {"services": ["-x"]},
    {"services": ["a`id`"]}, {"services": ["$HOME"]}])
def test_hostile_allowlist_entries_are_not_rendered(allow):
    with pytest.raises(scripts.ScriptError):
        scripts.render_linux(red(allowlist={**NO_ALLOW, **allow}), CTX)


@pytest.mark.parametrize("ctx", [
    scripts.Context("https://x.lan'; id; '", MACHINE_ID, (), PUBLIC),
    scripts.Context("ftp://x.lan", MACHINE_ID, (), PUBLIC),
    scripts.Context("https://x.lan", "not-a-machine-id", (), PUBLIC),
    scripts.Context("https://x.lan", MACHINE_ID, ("1.2.3.4; id",), PUBLIC),
    scripts.Context("https://x.lan", MACHINE_ID, (), 'ed25519:"; id; "')])
def test_hostile_server_values_are_not_rendered(ctx):
    with pytest.raises(scripts.ScriptError):
        scripts.render_linux(red(), ctx)


@pytest.mark.parametrize("over", [
    {"agent_key": "wpi_x'; id; '"}, {"control_key": "wpi_" + "c" * 20},
    {"step_key": "wps_a b c d e f g h i j k"}, {"platform": "windows"},
    {"platform": "truenas"}])
def test_hostile_keys_and_unsupported_platforms_are_not_rendered(over):
    with pytest.raises(scripts.ScriptError):
        scripts.render_linux(red(**over), CTX)


def test_service_names_must_match_the_control_daemon_at_create_time(env):
    hdr = admin(env)
    for i, bad in enumerate(("a@b", "a..b", "-x")):
        r = create(env, hdr, name=f"n{i}", allowlist={"services": [bad]})
        assert r.status_code == 422, bad


def test_fetch_serves_a_keyless_script_and_does_not_spend_the_token(env):
    hdr = admin(env)
    token = token_of(create(env, hdr, platform="linux"))
    for _ in range(3):
        got = env.client.get(f"/i/{token}")
        assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
        assert got.headers["content-type"].startswith("text/x-shellscript")
        assert got.text.startswith("#!/bin/sh\n# Observe install for nas01 (Linux server)")
        # No key is in the script: it carries the token and asks for keys after its guards.
        assert "wpi_" not in got.text and "wpc_" not in got.text and "wps_" not in got.text
        assert f"REDEEM_TOKEN='{token}'" in got.text and "STEP_KEY=''" in got.text
        assert "AGENT_KEY=''" in got.text and "CONTROL_KEY=''" in got.text
        assert "WANT_AGENT=1" in got.text and "WANT_CONTROL=1" in got.text
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]
    assert env.rows("SELECT fetched_at FROM enrolments") == [(None,)]
    assert "enrol_fetched" not in audit_kinds(env)


def test_redeem_spends_the_token_once_and_a_later_fetch_is_410(env):
    hdr = admin(env)
    token = token_of(create(env, hdr, platform="linux"))
    first = env.client.post("/api/enrol/redeem", json={"token": token})
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    keys = first.json()
    assert keys["host"] == "nas01"
    assert keys["agent_key"].startswith("wpi_") and keys["control_key"].startswith("wpc_")
    assert keys["step_key"].startswith("wps_")
    minted = env.rows("SELECT COUNT(*) FROM ingest_keys")
    assert minted == [(2,)]
    second = env.client.post("/api/enrol/redeem", json={"token": token})
    assert second.status_code == 410 and "wpi_" not in second.text
    assert env.client.get(f"/i/{token}").status_code == 410
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == minted
    assert audit_kinds(env).count("enrol_fetched") == 1
    assert audit_kinds(env).count("enrol_fetch_failed") == 2
    dump = repr(env.rows("SELECT * FROM audit")) + repr(env.rows("SELECT * FROM enrolments"))
    assert token not in dump and keys["agent_key"] not in dump and keys["step_key"] not in dump


@pytest.mark.parametrize("body", [{}, {"token": 5}, {"token": "garbage"}, {"token": "wpe_nonsense"},
                                  {"token": None}])
def test_redeem_refuses_an_unknown_or_malformed_token(env, body):
    assert env.client.post("/api/enrol/redeem", json=body).status_code == 410
    assert env.client.post("/api/enrol/redeem", content=b"{").status_code == 410
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]


def test_redeem_refuses_an_expired_token(env):
    hdr = admin(env)
    token = token_of(create(env, hdr, platform="linux"))
    asyncio.run(env.store.execute("UPDATE enrolments SET expires_at=?", (env.clock() - 1,)))
    assert env.client.post("/api/enrol/redeem", json={"token": token}).status_code == 410
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]


def test_a_hostile_host_header_never_reaches_the_command_or_the_script(env):
    hdr = admin(env)
    hostile = {"Host": "evil.example:9;curl x|sh"}
    made = env.client.post("/api/hosts", json={"name": "nas01", "platform": "linux",
                                               "agent": True, "control": False},
                           headers={**hdr, **hostile})
    assert made.status_code == 200
    command = made.json()["command"]
    assert "evil" not in command and "curl x" not in command
    assert command.splitlines()[1].startswith("curl -fsSL 'https://192.0.2.50:8443/i/wpe_")
    token = token_of(made)
    got = env.client.get(f"/i/{token}", headers=hostile)
    assert got.status_code == 200 and "evil" not in got.text
    assert "OBSERVE_URL='https://192.0.2.50:8443'" in got.text
    assert "OBSERVE_ADDRS='192.0.2.50'" in got.text
    regen = env.client.post("/api/hosts/nas01/enrolment/regenerate", json={},
                            headers={**hdr, **hostile})
    assert regen.status_code == 200 and "evil" not in regen.json()["command"]
    # The token was never spent by any of this.
    assert env.rows("SELECT fetched_at FROM enrolments") == [(None,)]


def test_fetched_script_carries_this_servers_guard_values(env):
    hdr = admin(env)
    text = env.client.get(f"/i/{token_of(create(env, hdr, platform='raspberry-pi'))}").text
    assert "(Raspberry Pi)" in text.splitlines()[1]
    assert "OBSERVE_URL='https://192.0.2.50:8443'" in text
    assert f"OBSERVE_MACHINE_ID='{MACHINE_ID}'" in text
    assert "OBSERVE_ADDRS='192.0.2.50'" in text
    assert PUBLIC in text and "HOST_NAME='nas01'" in text


def test_expired_unknown_and_garbage_tokens_are_410(env):
    hdr = admin(env)
    token = token_of(create(env, hdr, platform="linux"))
    asyncio.run(env.store.execute("UPDATE enrolments SET expires_at=?", (env.clock() - 1,)))
    for t in (token, "wpe_nonsense", "garbage"):
        assert env.client.get(f"/i/{t}").status_code == 410
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]


def test_control_on_an_agent_only_platform_does_not_burn_the_token(env):
    hdr = admin(env)
    token = token_of(create(env, hdr, name="tn01", platform="truenas"))
    assert env.client.get(f"/i/{token}").status_code == 409
    assert env.rows("SELECT fetched_at FROM enrolments WHERE host='tn01'") == [(None,)]
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]


def test_control_without_a_control_plugin_does_not_burn_the_token(tmp_path):
    e = Served(tmp_path, plugins=LoadedPlugins())
    try:
        hdr = admin(e)
        token = token_of(create(e, hdr, platform="linux"))
        assert e.client.get(f"/i/{token}").status_code == 409
        assert e.rows("SELECT fetched_at FROM enrolments") == [(None,)]
        agent = token_of(create(e, hdr, name="a01", platform="linux", control=False,
                                allowlist=None))
        assert e.client.get(f"/i/{agent}").status_code == 200
    finally:
        e.client.close()
        e.store.close()


def fetched_keys(env, platform="raspberry-pi"):
    hdr = admin(env)
    text = run_install(env, token_of(create(env, hdr, platform=platform))).text
    return (re.search(r"STEP_KEY='(wps_[^']+)'", text).group(1),
            re.search(r"AGENT_KEY='(wpi_[^']+)'", text).group(1))


def test_step_reports_need_the_step_key_and_show_in_progress(env):
    step_key, agent_key = fetched_keys(env)
    url = "/api/enrol/step"
    body = {"step": "hostname", "status": "ok", "note": "hostname matches"}
    assert env.client.post(url, json=body).status_code == 401
    for wrong in ("wps_wrong", agent_key):
        assert env.client.post(url, json=body, headers={
            "Authorization": f"Bearer {wrong}"}).status_code == 401
    auth = {"Authorization": f"Bearer {step_key}"}
    assert env.client.post(url, json=body, headers=auth).status_code == 204
    for bad in ({"step": "rm", "status": "ok"}, {"step": "agent", "status": "great"},
                {"step": "agent", "status": "ok", "extra": 1},
                {"step": "agent", "status": "ok", "note": 5}):
        assert env.client.post(url, json=bad, headers=auth).status_code == 401
    note = f"leaked {agent_key} and {step_key}"
    env.client.post(url, json={"step": "agent", "status": "failed", "note": note}, headers=auth)
    state = env.client.get("/api/v2/hosts/nas01/enrolment").json()
    assert [r["step"] for r in state["install"]] == ["hostname", "agent"]
    dump = repr(state) + repr(env.rows("SELECT * FROM enrolments")) + repr(
        env.rows("SELECT * FROM audit"))
    for secret in (agent_key, step_key, step_key.split("_", 1)[1]):
        assert secret not in dump
    assert "enrol_install_problem" in audit_kinds(env)


def test_step_key_expires_two_hours_after_the_fetch(env):
    step_key, _ = fetched_keys(env, platform="linux")
    late = env.clock() + enrol.STEP_TTL_S + 1
    assert asyncio.run(enrol.record_step(env.store, step_key, "done", "ok", "x", late)) is None
    soon = env.clock() + 5
    assert asyncio.run(enrol.record_step(env.store, step_key, "done", "ok", "x", soon)) == "nas01"


def test_machine_id_is_a_top_level_key_before_any_table(tmp_path):
    script = scripts.render_linux(red(), CTX)
    nl = chr(10)
    open_brace = "  umask 077" + nl + "  {" + nl
    start = script.index(open_brace, script.index("install_control() {"))
    block = script[start + len("  umask 077" + nl):script.index('  } > "$ETC/control.toml.new"')]
    sh = shutil.which("sh")
    assert sh, "sh is needed to render the control.toml body"
    done = subprocess.run([sh, "-c", "here_id=" + MACHINE_ID + nl + block + "}"],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    data = tomllib.loads(done.stdout)
    assert data["machine_id"] == MACHINE_ID
    assert data["host"] == "nas01"
    for table in ("fan", "services", "reboot"):
        assert "machine_id" not in data[table]
    assert done.stdout.index("machine_id") < done.stdout.index("[fan]")


def test_refused_step_reports_write_a_bounded_number_of_audit_rows(env):
    fetched_keys(env)
    for _ in range(5):
        assert env.client.post("/api/enrol/step", json={"step": "agent", "status": "ok"}
                               ).status_code == 401
    assert audit_kinds(env).count("enrol_step_refused") == 1


def test_script_replaces_and_reports_an_existing_agent_container_and_checks_writes():
    script = scripts.render_linux(red(), CTX)
    assert "An existing hostwatch-agent container was found" in script
    assert "com.docker.compose.project" in script
    assert '} || fail control_unit "cannot write the unit"' in script
    assert 'chmod 0755 "$ETC" || fail' in script
    # The live control files are replaced only after the sudoers rules passed visudo.
    assert script.index("visudo -c -f") < script.index('mv -f "$ETC/control.toml.new"')
