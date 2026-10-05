"""The update and cleanup scripts served by GET /t/{token}: rendering, syntax, guards, secrets.

The scripts are only rendered and syntax checked here. They are never run."""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib

import pytest

from observe import hosttasks, scripts, taskscripts

from .test_install_script import (CTX, MACHINE_ID, PUBLIC, STEP_KEY, check_syntax)  # noqa: F401

ALLOW = {"fans": [{"header": "pwm1", "min_duty_limit": 30}, "pwm2"],
         "services": ["smbd", "docker:scrutiny"], "reboot": True}


def task(kind="update", platform="linux", **over):
    base = dict(host="nas01", kind=kind, platform=platform, allowlist=ALLOW, rev=3,
                step_key=STEP_KEY)
    base.update(over)
    return hosttasks.RedeemedTask(**base)


def update(**over):
    return taskscripts.render_update(task("update", **over), CTX)


def cleanup(platform="linux", **over):
    return taskscripts.render_task(task("cleanup", platform, **over), CTX)


def powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


# ---------------------------------------------------------------- syntax


@pytest.mark.parametrize("platform", ["linux", "raspberry-pi"])
def test_update_script_passes_a_syntax_check(tmp_path, platform):
    script = update(platform=platform)
    check_syntax(script, tmp_path)
    assert script.rstrip().endswith('main "$@"') and script.count('main "$@"') == 1


@pytest.mark.parametrize("platform", ["linux", "raspberry-pi", "truenas"])
def test_cleanup_shell_scripts_pass_a_syntax_check(tmp_path, platform):
    script = cleanup(platform)
    check_syntax(script, tmp_path)
    assert script.rstrip().endswith('main "$@"') and script.count('main "$@"') == 1


def test_windows_cleanup_passes_the_powershell_parser(tmp_path):
    exe = powershell()
    if exe is None:
        pytest.skip("no PowerShell on this machine")
    path = tmp_path / "cleanup.ps1"
    path.write_bytes(cleanup("windows").encode("utf-8"))
    code = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$args[0],[ref]$t,[ref]$e);if($e.Count){$e|ForEach-Object{$_.ToString()};exit 1}")
    done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command",
                           "& { " + code + " }", str(path)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


def test_windows_cleanup_is_ascii_without_a_bom_and_has_no_exit():
    data = cleanup("windows").encode("utf-8")
    assert not data.startswith(b"\xef\xbb\xbf")
    data.decode("ascii")
    assert b"exit " not in data  # `exit` would close the console that ran irm | iex


# ---------------------------------------------------------------- the update guards


def test_update_has_the_install_guards_in_the_install_order_before_any_change():
    script = update()
    main = script[script.index("main() {"):]
    root = main.index("id -u")
    host = main.index('"$l_short" != "$want"')
    observe_id = main.index('"$here_id" = "$OBSERVE_MACHINE_ID"')
    observe_addr = main.index('"$addr" = "$mine"')
    has_install = main.index('[ ! -f "$ETC/control.toml" ]')
    first_change = min(main.index(c) for c in ("umask 077", "control.toml.new\"", "visudo",
                                                "systemctl restart"))
    assert root < host < observe_id < observe_addr < has_install < first_change
    # The guard code is the install's own, with only the heading line renamed.
    assert scripts._GUARDS.rstrip("\n").replace(
        "Observe install for", "Observe settings update for") in script
    assert "This command was made for the host" in script
    head = script[:script.index("main() {")]
    for forbidden in ("mkdir", "chown", "useradd", "systemctl", "visudo", "mv ", "install -o"):
        assert forbidden not in head
    assert [ln for ln in script.splitlines() if ln.strip()][-1] == 'main "$@"'


def test_update_names_the_machine_and_changes_only_the_allowlist_and_the_service():
    script = update()
    assert script.startswith(
        "#!/bin/sh\n# Observe settings update for nas01 (Linux server). Run on nas01 only.")
    assert "refuses to run on any other machine and on the Observe host" in script
    # It never touches the keys, the agent or the account.
    for absent in ("AGENT_KEY", "CONTROL_KEY", "agent.env", "control.env.new", "useradd", "docker ",
                   "pip install", "hostwatch-control.service"):
        assert absent not in script.split("main() {")[1], absent
    assert "systemctl restart hostwatch-control" in script
    assert script.index("visudo -c -f") < script.index("mv -f \"$ETC/control.toml.new\"") \
        < script.index("install -o root -g root -m 0440")


def test_update_writes_the_saved_allowlist_with_machine_id_before_any_table(tmp_path):
    script = update()
    nl = chr(10)
    start = script.index("  umask 077" + nl + "  {" + nl)
    block = script[start + len("  umask 077" + nl):script.index('  } > "$ETC/control.toml.new"')]
    sh = shutil.which("sh")
    assert sh, "sh is needed to render the control.toml body"
    done = subprocess.run([sh, "-c", "here_id=" + MACHINE_ID + nl + block + "}"],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    data = tomllib.loads(done.stdout)
    assert data["machine_id"] == MACHINE_ID and data["host"] == "nas01"
    assert data["observe_public_key"] == PUBLIC
    assert data["fan"]["headers"] == ["pwm1", "pwm2"] and data["fan"]["min_duty_floor"] == 30
    assert data["services"]["restart"] == ["smbd", "docker:scrutiny"]
    assert data["reboot"]["allow"] is True
    assert done.stdout.index("machine_id") < done.stdout.index("[fan]")


def test_update_with_an_empty_allowlist_writes_no_tables():
    script = update(allowlist={"fans": [], "services": [], "reboot": False})
    assert "[fan]" not in script and "[services]" not in script and "[reboot]" not in script


def test_the_step_key_is_used_only_in_its_assignment_and_notes_are_fixed_text():
    for script in (update(), cleanup(), cleanup("truenas")):
        assert script.count(STEP_KEY) == 1
        assert "set -x" not in script and "-K -" in script and "Bearer %s" in script
        for note in re.findall(r"^\s*(?:report|fail|refuse) \S+ (?:\S+ )?(\".*)$", script,
                               flags=re.M):
            assert "KEY" not in note
    assert cleanup("windows").count(STEP_KEY) == 1


# ---------------------------------------------------------------- the cleanup guards


@pytest.mark.parametrize("platform", ["linux", "raspberry-pi", "truenas"])
def test_cleanup_shell_guards_run_before_any_removal(platform):
    script = cleanup(platform)
    main = script[script.index("main() {"):]
    root = main.index("id -u")
    match = main.index("refuse cleanup_match")
    first_change = min(main.index(c) for c in ("rm -f", "rm -rf", "systemctl", "docker rm",
                                                "userdel") if c in main)
    assert root < match < first_change
    assert "Observe cleanup for $HOST_NAME" in main
    assert "has no Observe install made for that host name" in main
    assert "This command was made to remove the install for the host" in main
    head = script[:script.index("main() {")]
    for forbidden in ("rm ", "systemctl", "docker", "userdel", "mkdir"):
        assert forbidden not in head, forbidden
    assert [ln for ln in script.splitlines() if ln.strip()][-1] == 'main "$@"'


def test_cleanup_matches_the_install_by_the_host_name_it_was_made_for():
    linux = cleanup()
    assert 'grep -qxF "HOSTWATCH_HOST_NAME=$HOST_NAME" "$ETC/agent.env"' in linux
    assert 'grep -qxF "host = \\"$HOST_NAME\\"" "$ETC/control.toml"' in linux
    assert "agent_here" in linux and "control_here" in linux
    assert 'grep -qxF "HOSTWATCH_HOST_NAME=$HOST_NAME" "$envf"' in cleanup("truenas")
    win = cleanup("windows")
    assert "HOSTWATCH_HOST_NAME=$HostName" in win and "-ceq" in win
    assert win.index("IsInRole") < win.index("-ceq") < win.index("Stop-Service") \
        < win.index("Remove-Item")


def test_cleanup_does_not_use_the_hostname_or_observe_host_guards_and_says_why():
    # The machine to clean up is usually not the machine with that name, so the hostname guard
    # cannot apply. The match guard replaces it. See taskscripts.py.
    script = cleanup()
    assert '"$l_short" != "$want"' not in script and "OBSERVE_MACHINE_ID" not in script
    assert "Observe-host guard is not applied" in taskscripts.__doc__


def test_cleanup_names_the_machine_and_keeps_data():
    linux = cleanup()
    assert linux.startswith("#!/bin/sh\n# Observe cleanup for nas01 (Linux server).")
    assert "Run it only on the machine that holds that install" in linux
    assert "hostwatch-agent-data was kept" in linux
    assert cleanup("truenas").startswith("#!/bin/sh\n# Observe cleanup for nas01 (TrueNAS).")
    assert "Its data folder was kept" in cleanup("truenas")
    assert cleanup("windows").startswith("# Observe cleanup for nas01 (Windows).")
    # Only fixed paths are removed, never a path built from a value.
    for script in (cleanup(), cleanup("truenas")):
        for line in script.splitlines():
            if "rm -rf" in line:
                assert line.strip().startswith("rm -rf /opt/hostwatch-control"), line


# ---------------------------------------------------------------- hostile values


@pytest.mark.parametrize("bad", ["nas01; rm -rf /", "nas01'", "NAS01", "a b", "$(id)", "a`id`",
                                 "nas01\nx", "nas01\n", "", "x" * 64, "-nas"])
def test_hostile_host_names_are_not_rendered(bad):
    for platform, fn in (("linux", update), ("linux", cleanup), ("truenas", cleanup),
                         ("windows", cleanup)):
        with pytest.raises(scripts.ScriptError):
            if fn is update:
                update(host=bad)
            else:
                fn(platform, host=bad)


@pytest.mark.parametrize("allow", [
    {"fans": ["fan;id"]}, {"fans": ["fan\n"]}, {"services": ["smbd\n"]}, {"fans": ["a b"]},
    {"fans": [{"header": "f", "min_duty_limit": 101}]}, {"services": ["smbd'; id; '"]},
    {"services": ["a|b"]}, {"services": ["a..b"]}, {"services": ["$HOME"]}])
def test_hostile_allowlist_entries_are_not_rendered(allow):
    with pytest.raises(scripts.ScriptError):
        update(allowlist={"fans": [], "services": [], "reboot": False, **allow})


@pytest.mark.parametrize("ctx", [
    scripts.Context("https://x.lan'; id; '", MACHINE_ID, (), PUBLIC),
    scripts.Context("ftp://x.lan", MACHINE_ID, (), PUBLIC),
    scripts.Context("https://x.lan", "not-a-machine-id", (), PUBLIC),
    scripts.Context("https://x.lan", MACHINE_ID, ("1.2.3.4; id",), PUBLIC),
    scripts.Context("https://x.lan", MACHINE_ID, (), 'ed25519:"; id; "')])
def test_hostile_server_values_are_not_rendered(ctx):
    with pytest.raises(scripts.ScriptError):
        taskscripts.render_update(task("update"), ctx)


@pytest.mark.parametrize("over", [{"step_key": "wps_a b c d e f g h i j k"},
                                  {"step_key": "wpi_" + "c" * 20}])
def test_hostile_step_keys_are_not_rendered(over):
    with pytest.raises(scripts.ScriptError):
        update(**over)


def test_each_renderer_refuses_the_platforms_it_has_no_script_for():
    for platform in ("truenas", "windows"):
        with pytest.raises(scripts.ScriptError):
            update(platform=platform)
    with pytest.raises(scripts.ScriptError):
        taskscripts.render_cleanup_linux(task("cleanup", "windows"), CTX)
    with pytest.raises(scripts.ScriptError):
        taskscripts.render_cleanup_windows(task("cleanup", "linux"), CTX)
    with pytest.raises(scripts.ScriptError):
        taskscripts.render_cleanup_truenas(task("cleanup", "linux"), CTX)
    with pytest.raises(scripts.ScriptError):
        taskscripts.render_update(task("cleanup", "linux"), CTX)
    assert taskscripts.task_supported("update", "linux")
    assert not taskscripts.task_supported("update", "windows")
    assert taskscripts.task_supported("cleanup", "windows")
    assert not taskscripts.task_supported("cleanup", "freebsd")


def test_no_script_has_a_carriage_return_or_an_unfilled_marker():
    for script in (update(), cleanup(), cleanup("truenas"), cleanup("windows")):
        assert "\r" not in script and "@@" not in script


def test_update_script_text_names_the_update_and_keeps_a_backup_with_rollback():
    script = update()
    assert "Observe settings update for $HOST_NAME" in script
    assert "Observe install for" not in script
    main = script[script.index("main() {"):]
    assert (main.index('cp -p "$ETC/control.toml" "$ETC/control.toml.bak"')
            < main.index('mv -f "$ETC/control.toml.new"')
            < main.index("/etc/sudoers.d/hostwatch-control")
            < main.index('mv -f "$ETC/control.toml.bak" "$ETC/control.toml"')
            < main.index("report control_config ok") < main.index("systemctl restart"))


def test_cleanup_guard_comment_matches_its_guards():
    for platform in ("linux", "truenas"):
        script = cleanup(platform)
        assert "unless all three pass" not in script
        assert 'say "Observe install for' not in script and 'say "Observe cleanup for' in script


def test_windows_cleanup_leaves_the_control_install_alone_and_waits_for_the_service():
    win = cleanup("windows")
    assert "Remove-Item -LiteralPath $install" not in win
    assert "'venv'" in win and win.count("-Recurse") == 1
    assert "Stop-Service -Name $svc -Force -ErrorAction Stop" in win
    assert "WaitForStatus('Stopped'" in win
    assert win.index("WaitForStatus") < win.index("sc.exe delete") < win.index("Remove-Item")
    assert "partly removed" in win
