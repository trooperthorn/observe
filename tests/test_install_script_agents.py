"""The TrueNAS and Windows install scripts (agent only): rendering, guards, secrets, routes."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess

import pytest

from observe import enrol, scripts

from .test_enrol_api import admin, audit_kinds, create, run_install, token_of
from .test_install_script import (AGENT_KEY, CONTROL_KEY, CTX, MACHINE_ID, NO_ALLOW, PUBLIC,
                                  STEP_KEY, Served, check_syntax, env)  # noqa: F401


def red(platform, **over):
    base = dict(host="nas01", platform=platform, agent_key=AGENT_KEY, control_key=None,
                step_key=STEP_KEY, allowlist=NO_ALLOW)
    base.update(over)
    return enrol.Redeemed(**base)


def tn(**over):
    return scripts.render_truenas(red("truenas", **over), CTX)


def win(**over):
    return scripts.render_windows(red("windows", **over), CTX)


def powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


def agent_only(env, hdr, name, platform, **extra):
    return create(env, hdr, name=name, platform=platform, control=False, allowlist=None, **extra)


# ---------------------------------------------------------------- syntax


@pytest.mark.parametrize("pool", ["", "Apps", "tank", "my-pool_2.x"])
def test_truenas_script_passes_a_syntax_check(tmp_path, pool):
    script = scripts.render_truenas(red("truenas"), CTX, pool)
    check_syntax(script, tmp_path)
    assert script.rstrip().endswith('main "$@"') and script.count('main "$@"') == 1
    assert f"POOL='{pool or 'Apps'}'" in script


def test_windows_script_passes_the_powershell_parser(tmp_path):
    exe = powershell()
    if exe is None:
        pytest.skip("no PowerShell on this machine")
    path = tmp_path / "install.ps1"
    path.write_bytes(win().encode("utf-8"))
    code = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$args[0],[ref]$t,[ref]$e);if($e.Count){$e|ForEach-Object{$_.ToString()};exit 1}")
    done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command",
                           "& { " + code + " }", str(path)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


# ---------------------------------------------------------------- guards


def test_truenas_guards_run_before_any_change():
    script = tn()
    head = script[:script.index("main() {")]
    main = script[script.index("main() {"):script.index("write_env() {")]
    root = main.index("id -u")
    host = main.index('"$l_short" != "$want"')
    observe_id = main.index('"$here_id" = "$OBSERVE_MACHINE_ID"')
    observe_addr = main.index('"$addr" = "$mine"')
    first_change = min(main.index(c) for c in ("mkdir", "chown", "write_env", "write_compose"))
    assert root < host < observe_id < observe_addr < main.index('[ ! -d "$base" ]') < first_change
    assert "This command was made for the host" in main and "TrueNAS web UI of $HOST_NAME" in main
    for forbidden in ("mkdir", "chown", "chmod", "mv ", "docker", "midclt call"):
        assert forbidden not in head
    assert [ln for ln in script.splitlines() if ln.strip()][-1] == 'main "$@"'


def test_truenas_guard_text_is_the_linux_guard_text():
    linux = scripts.render_linux(red("linux", control_key=None), CTX)
    guards = scripts._GUARDS.rstrip("\n")
    assert guards in linux and guards in tn()


def test_windows_guards_run_before_any_change():
    script = win()
    admin_check = script.index("IsInRole")
    host = script.index("$mine -notcontains $want")
    machine = script.index("$hereId -eq $ObserveMachineId")
    addrs = script.index("$hereAddrs -contains $addr")
    first_change = min(script.index(c) for c in ("New-Item", "Invoke-WebRequest", "Expand-Archive",
                                                   "& $installer", "& $remover"))
    assert admin_check < host < machine < addrs < first_change
    assert "This command was made for the host" in script
    assert script.rstrip().endswith("Invoke-ObserveInstall")
    assert script.count("Invoke-ObserveInstall") == 2  # the definition and the last line
    assert "exit " not in script  # `exit` would close the console that ran irm | iex


def test_scripts_name_the_machine_in_their_header():
    assert tn().startswith("#!/bin/sh\n# Observe install for nas01 (TrueNAS). Run on nas01 only.")
    assert win().startswith("# Observe install for nas01 (Windows). Run on nas01 only.")


def test_truenas_writes_the_files_hostwatch_documents():
    script = tn()
    for need in ("$dir/agent.env", "$dir/compose.yaml", "HOSTWATCH_HUB_URL=%s",
                 "HOSTWATCH_INGEST_KEY=%s", "HOSTWATCH_HOST_NAME=%s", "chmod 0400",
                 "chown 10001:10001", "ghcr.io/trooperthorn/hostwatch:edge", "pull_policy: always",
                 "network_mode: host", "read_only: true", "cap_drop:", "no-new-privileges:true",
                 "/sys:/host/sys:ro", "HOSTWATCH_ROLE: agent", "Install via YAML",
                 "base=/mnt/$POOL", "dir=$base/hostwatch"):
        assert need in script, need
    assert "docker run" not in script and "HOSTWATCH_CONTROL" not in script


def test_windows_uses_the_hostwatch_installer_with_a_secure_string():
    script = win()
    assert "deploy\\windows\\install.ps1" in script
    assert "ConvertTo-SecureString $AgentKey -AsPlainText -Force" in script
    assert "-IngestKey $secure" in script
    assert "deploy\\windows\\uninstall.ps1" in script
    assert scripts.WINDOWS_SOURCE in script


# ---------------------------------------------------------------- secrets


def test_truenas_keys_are_never_echoed_or_sent_in_a_report():
    script = tn()
    for key in (AGENT_KEY, STEP_KEY):
        assert script.count(key) == 1
    assert "set -x" not in script
    # redeem() alone receives the keys, and only reads them from curl's reply with sed.
    redeem_fn = script[script.index("redeem() {"):script.index("main() {")]
    for line in script.replace(redeem_fn, "").splitlines():
        if "_KEY" in line and "$" in line:
            assert line.lstrip().startswith(("printf", "[ -n"))
        if line.lstrip().startswith(("say ", "report ", "fail ", "refuse ")):
            assert "KEY" not in line and "$AGENT" not in line
    assert "-K -" in script


def test_windows_keys_are_never_echoed_or_sent_in_a_report():
    script = win()
    for key in (AGENT_KEY, STEP_KEY):
        assert script.count(key) == 1
    for line in script.splitlines():
        s = line.strip()
        if s.startswith(("Write-Host", "Stop-Refuse", "Stop-Fail", "Send-Step ")):
            assert "$AgentKey" not in s and "$secure" not in s and "$StepKey" not in s
        if "$AgentKey" in s:
            assert s.startswith(("$AgentKey = ", "$secure = ConvertTo-SecureString"))
    assert script.count("-AsPlainText") == 1
    assert "Start-Transcript" not in script and "Write-Verbose" not in script


def test_windows_script_is_ascii_and_has_no_bom():
    text = win()
    text.encode("ascii")
    assert not text.startswith("﻿")


# ---------------------------------------------------------------- hostile input


BAD_NAMES = ["nas01; rm -rf /", "nas01'", "NAS01", "a b", "$(id)", "a`id`", "nas01\nx", "",
             "x" * 64, "-nas", "n';calc;'"]


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_hostile_host_names_are_not_rendered(bad):
    with pytest.raises(scripts.ScriptError):
        scripts.render_truenas(red("truenas", host=bad), CTX)
    with pytest.raises(scripts.ScriptError):
        scripts.render_windows(red("windows", host=bad), CTX)


@pytest.mark.parametrize("pool", ["a b", "a;id", "a'b", "$(id)", "a`id`", "-x", "a/b", "a\nb",
                                  "..", "a..b", "x" * 65, "pé", "a|b", "a$HOME"])
def test_hostile_pool_names_are_not_rendered(pool):
    assert not scripts.valid_pool(pool)
    with pytest.raises(scripts.ScriptError):
        scripts.render_truenas(red("truenas"), CTX, pool)
    with pytest.raises(enrol.EnrolError):
        enrol.parse_pool(pool, "truenas")


def test_a_pool_belongs_to_truenas_only():
    with pytest.raises(enrol.EnrolError):
        enrol.parse_pool("tank", "linux")
    assert enrol.parse_pool(None, "linux") == "" and enrol.parse_pool("", "windows") == ""


@pytest.mark.parametrize("ctx", [
    scripts.Context("https://x.lan'; id; '", MACHINE_ID, (), PUBLIC),
    scripts.Context("https://x.lan\"; id; \"", MACHINE_ID, (), PUBLIC),
    scripts.Context("ftp://x.lan", MACHINE_ID, (), PUBLIC),
    scripts.Context("https://x.lan", "not-a-machine-id", (), PUBLIC),
    scripts.Context("https://x.lan", MACHINE_ID, ("1.2.3.4; id",), PUBLIC),
    scripts.Context("https://x.lan", MACHINE_ID, ("$(id)",), PUBLIC)])
def test_hostile_server_values_are_not_rendered(ctx):
    with pytest.raises(scripts.ScriptError):
        scripts.render_truenas(red("truenas"), ctx)
    with pytest.raises(scripts.ScriptError):
        scripts.render_windows(red("windows"), ctx)


@pytest.mark.parametrize("over", [
    {"agent_key": "wpi_x'; id; '"}, {"agent_key": "wpi_" + "a" * 11 + "$(id)"},
    {"agent_key": "wpc_" + "a" * 20}, {"agent_key": None}, {"agent_key": ""},
    {"step_key": "wps_a b c d e f g h i j k"}, {"step_key": "wps_" + "a" * 12 + "'; id; '"},
    {"control_key": CONTROL_KEY},
    {"allowlist": {"fans": ["pwm1"], "services": [], "reboot": False}},
    {"allowlist": {"fans": [], "services": ["smbd"], "reboot": False}},
    {"allowlist": {"fans": [], "services": [], "reboot": True}}])
def test_hostile_keys_and_control_are_not_rendered(over):
    with pytest.raises(scripts.ScriptError):
        scripts.render_truenas(red("truenas", **over), CTX)
    with pytest.raises(scripts.ScriptError):
        scripts.render_windows(red("windows", **over), CTX)


def test_each_renderer_refuses_the_other_platforms():
    with pytest.raises(scripts.ScriptError):
        scripts.render_truenas(red("windows"), CTX)
    with pytest.raises(scripts.ScriptError):
        scripts.render_windows(red("truenas"), CTX)
    for other in ("truenas", "windows"):
        with pytest.raises(scripts.ScriptError):
            scripts.render_linux(red(other), CTX)
    assert scripts.render(red("truenas"), CTX).startswith("#!/bin/sh")
    assert scripts.render(red("windows"), CTX).startswith("# Observe install")


def test_powershell_quote_doubling():
    assert scripts._pq("a'b") == "'a''b'"


# ---------------------------------------------------------------- routes


def test_truenas_fetch_uses_the_pool_in_the_query(env):
    hdr = admin(env)
    made = agent_only(env, hdr, "tn01", "truenas", pool="tank")
    assert made.json()["command"].splitlines()[1].endswith("?pool=tank' | sudo sh")
    got = env.client.get(f"/i/{token_of(made)}", params={"pool": "tank"})
    assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
    assert got.headers["content-type"].startswith("text/x-shellscript")
    assert "POOL='tank'" in got.text and "(TrueNAS)" in got.text.splitlines()[1]
    assert f"OBSERVE_MACHINE_ID='{MACHINE_ID}'" in got.text


def test_truenas_default_pool_is_apps(env):
    token = token_of(agent_only(env, admin(env), "tn01", "truenas"))
    assert "POOL='Apps'" in env.client.get(f"/i/{token}").text


def test_a_hostile_pool_query_is_refused_before_the_token_is_spent(env):
    token = token_of(agent_only(env, admin(env), "tn01", "truenas"))
    keys = env.rows("SELECT COUNT(*) FROM ingest_keys")
    for bad in ("a;id", "a b", "$(id)", "..", "x" * 65):
        r = env.client.get(f"/i/{token}", params={"pool": bad})
        assert r.status_code == 400 and "wpi_" not in r.text
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == keys
    assert env.client.get(f"/i/{token}").status_code == 200


def test_create_validates_the_pool(env):
    hdr = admin(env)
    for i, (platform, pool) in enumerate((("truenas", "a;id"), ("truenas", "a b"),
                                          ("linux", "tank"), ("truenas", 5))):
        r = agent_only(env, hdr, f"p{i}", platform, pool=pool)
        assert r.status_code == 422, (platform, pool)
    assert env.rows("SELECT COUNT(*) FROM enrolments") == [(0,)]


def test_windows_fetch_serves_a_keyless_powershell_script_and_redeem_spends_once(env):
    hdr = admin(env)
    made = agent_only(env, hdr, "win01", "windows")
    assert made.json()["command"].splitlines()[1].startswith("irm 'https://192.0.2.50:8443/i/wpe_")
    token = token_of(made)
    first = env.client.get(f"/i/{token}")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert first.headers["content-type"].startswith("text/plain")
    assert first.text.startswith("# Observe install for win01 (Windows). Run on win01 only.")
    assert "$ObserveUrl = 'https://192.0.2.50:8443'" in first.text
    assert f"$ObserveMachineId = '{MACHINE_ID}'" in first.text
    assert "wpi_" not in first.text and "wpc_" not in first.text
    assert f"$RedeemToken = '{token}'" in first.text and "$StepKey = ''" in first.text
    assert env.client.get(f"/i/{token}").status_code == 200  # fetching spends nothing
    assert env.client.post("/api/enrol/redeem", json={"token": token}).json()[
        "agent_key"].startswith("wpi_")
    second = env.client.get(f"/i/{token}")
    assert second.status_code == 410 and "wpi_" not in second.text
    assert audit_kinds(env).count("enrol_fetched") == 1


def test_windows_with_control_is_refused_at_create(env):
    r = create(env, admin(env), name="win01", platform="windows", control=True)
    assert r.status_code == 422


def test_truenas_with_control_is_409_and_keeps_the_token(env):
    token = token_of(create(env, admin(env), name="tn01", platform="truenas"))
    assert env.client.get(f"/i/{token}").status_code == 409
    assert env.rows("SELECT fetched_at FROM enrolments WHERE host='tn01'") == [(None,)]
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]


def test_a_missing_address_gets_a_body_that_is_safe_in_powershell(tmp_path):
    e = Served(tmp_path, public_url=None)
    try:
        token = asyncio.run(enrol.create_enrolment(
            e.store, enrol.parse_spec({"name": "win01", "platform": "windows", "agent": True}),
            "root", e.clock()))
        bad = e.client.get(f"/i/{token}")
        assert bad.status_code == 409 and bad.text.startswith("Write-Host") and "exit" not in bad.text
        assert e.rows("SELECT fetched_at FROM enrolments") == [(None,)]
    finally:
        e.client.close()
        e.store.close()


def test_progress_steps_from_the_new_scripts_are_accepted(env):
    hdr = admin(env)
    token = token_of(agent_only(env, hdr, "tn01", "truenas"))
    text = run_install(env, token).text
    step = re.search(r"STEP_KEY='(wps_[^']+)'", text).group(1)
    for name, status in (("pool", "ok"), ("compose", "ok"), ("app", "skipped"),
                         ("download", "ok")):
        r = env.client.post("/api/enrol/step", json={"step": name, "status": status, "note": "x"},
                            headers={"Authorization": f"Bearer {step}"})
        assert r.status_code in (200, 204), (name, r.text)
    got = {i["step"]: i["status"]
           for i in env.client.get("/api/hosts/tn01/enrolment", headers=hdr).json()["install"]}
    assert got == {"pool": "ok", "compose": "ok", "app": "skipped", "download": "ok"}
