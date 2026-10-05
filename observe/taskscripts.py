"""Scripts for the settings tasks, served by GET /t/{token} (docs/GUI-DESIGN.md section 3.11).

`render_update` rewrites control.toml and the sudoers rules on a Linux or Raspberry Pi host and
restarts the control service. `render_cleanup_*` undo an install on the machine that holds it.
Neither carries a key. They are built from the same helper functions and guard text as the
install scripts in scripts.py, and every value is validated and single quoted there, so no value
can carry shell syntax into a script. A value that fails raises ScriptError.

The update has the same three guards as the install: root, the hostname is the enrolled name,
and the machine is not the Observe host. The cleanup has the root guard and a match guard
instead. It runs on the machine that holds the install made for the named host, which may not be
the machine with that name (that is the mistake it undoes), so the hostname guard cannot apply.
It refuses a machine with no install made for that host before it changes anything. The
Observe-host guard is not applied either, because undoing a mistaken install on the Observe
host is a legitimate use; the match guard still stops it unless an install for that host is there.
"""

from __future__ import annotations

from .enrol import PLATFORMS
from .hosttasks import CLEANUP_PLATFORMS, UPDATE_PLATFORMS, RedeemedTask
from .scripts import (_GUARDS, _HELPERS, _KEY, _MACHINE_ID, _NAME, _URL, MACHINE_ID_LINE, Context,
                      ScriptError, _need, _pq, _q, control_toml, usable_address)

_ROOT_GUARD = _GUARDS[:_GUARDS.index("  want=$(lower")].replace(
    "Observe install for", "Observe cleanup for").replace(
    "unless all three pass", "unless the root guard and the match guard below pass")
_UPDATE_GUARDS = _GUARDS.replace("Observe install for", "Observe settings update for")


def task_supported(kind: str, platform: str) -> bool:
    """True when a task of this kind has a script for this platform."""
    return platform in (UPDATE_PLATFORMS if kind == "update" else CLEANUP_PLATFORMS)


def _common(task: RedeemedTask, ctx: Context, kind: str,
            platforms: tuple[str, ...]) -> tuple[str, str, str, str, list[str]]:
    """Validate what the task scripts share. Returns the host name, the Observe URL, the Observe
    machine id, the step key and the usable Observe addresses."""
    if task.kind != kind or task.platform not in platforms:
        raise ScriptError("no script for this platform")
    name = _need(_NAME, task.host, "the host name")
    url = _need(_URL, ctx.observe_url, "the Observe address")
    mid = ctx.observe_machine_id
    if mid:
        _need(_MACHINE_ID, mid, "the Observe machine id")
    addrs = [usable_address(a) for a in ctx.observe_addrs]
    if any(a is None for a in addrs):
        raise ScriptError("an Observe address is not a usable IP address")
    step = _need(_KEY, task.step_key, "the step key") if task.step_key else ""
    if step and not step.startswith("wps_"):
        raise ScriptError("a key has the wrong marker")
    return name, url, mid, step, [a for a in addrs if a]


_UP_HEAD = r"""#!/bin/sh
# Observe settings update for @@NAME@@ (@@LABEL@@). Run on @@NAME@@ only.
# It refuses to run on any other machine and on the Observe host. It prints no key.
# It rewrites the control allowlist (control.toml and the sudoers rules) and restarts the
# control service. It does not touch the keys. The old control.toml is kept as control.toml.bak.
set -u
umask 022
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

HOST_NAME=@@Q_NAME@@
OBSERVE_URL=@@Q_URL@@
OBSERVE_MACHINE_ID=@@Q_MID@@
OBSERVE_ADDRS=@@Q_ADDRS@@
STEP_KEY=@@Q_STEPKEY@@
ETC=/etc/hostwatch
VENV=/opt/hostwatch-control/venv
"""

_UP_MAIN = r"""
main() {
@@GUARDS@@
  # 2. This machine has a control install to update. Still nothing has changed.
  if [ ! -f "$ETC/control.toml" ] || [ ! -f "$ETC/control.env" ] || [ ! -x "$VENV/bin/python" ]; then
    refuse control_config "no control install" "this machine has no control install to update. Use the install command from the settings page of $HOST_NAME instead."
  fi
  trap 'rm -f "$ETC/control.toml.new"' EXIT

  umask 077
  {
@@TOML@@
  } > "$ETC/control.toml.new" || fail control_config "cannot write settings" "cannot write control.toml"
  umask 022
  chown root:hostwatch-control "$ETC/control.toml.new" && chmod 0640 "$ETC/control.toml.new" ||
    fail control_config "cannot set modes" "cannot secure control.toml"

  # The sudoers rules are rendered from the new file and pass visudo before anything live is
  # replaced, so a failed render changes nothing.
  sudoers_tmp=$(mktemp) || fail sudoers "no temporary file" "cannot create a temporary file"
  "$VENV/bin/python" -c 'import sys; from hostwatch.control import config, actions_linux; sys.stdout.write(actions_linux.render_sudoers(config.load("/etc/hostwatch/control.toml.new")))' > "$sudoers_tmp" ||
    { rm -f "$sudoers_tmp"; fail sudoers "render failed" "could not render the sudoers rules"; }
  if ! visudo -c -f "$sudoers_tmp" >/dev/null 2>&1; then
    rm -f "$sudoers_tmp"
    fail sudoers "visudo rejected the rules" "visudo rejected the generated rules, so nothing was changed"
  fi
  # The old file is kept as control.toml.bak (a hand edit on the host is not lost), and it is put
  # back if the rules cannot be installed, so the file and the rules never disagree.
  backed=0
  if [ -f "$ETC/control.toml" ]; then
    cp -p "$ETC/control.toml" "$ETC/control.toml.bak" ||
      { rm -f "$sudoers_tmp"; fail control_config "cannot back up settings" "cannot keep a copy of control.toml, so nothing was changed"; }
    backed=1
  fi
  mv -f "$ETC/control.toml.new" "$ETC/control.toml" ||
    { rm -f "$sudoers_tmp"; fail control_config "cannot move settings into place" "cannot move control.toml into place"; }
  if ! install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/hostwatch-control; then
    rm -f "$sudoers_tmp"
    if [ "$backed" = 1 ]; then mv -f "$ETC/control.toml.bak" "$ETC/control.toml" || say "Could not put the old control.toml back. It is at $ETC/control.toml.bak." >&2; fi
    fail sudoers "install failed" "could not install the sudoers rules; the old control.toml was put back"
  fi
  report control_config ok "control settings written"
  rm -f "$sudoers_tmp"
  report sudoers ok "sudoers checked and installed"

  systemctl restart hostwatch-control ||
    fail control_unit "service did not restart" "systemd could not restart hostwatch-control"
  report control_unit ok "control service restarted"
  report done ok "update finished"
  say "Done. The control service now uses the new allowlist. Observe shows progress for $HOST_NAME."
}

main "$@"
"""


def render_update(task: RedeemedTask, ctx: Context) -> str:
    """The settings update script (Linux and Raspberry Pi). Raises ScriptError on any unsafe
    value."""
    name, url, mid, step, addrs = _common(task, ctx, "update", UPDATE_PLATFORMS)
    body = control_toml(name, task.allowlist, ctx.public_key)
    lines = [f"    printf '%s\\n' {_q(line)}" for line in body.splitlines()]
    # machine_id is a top-level key, so it goes before the first [table] header.
    lines.insert(2, MACHINE_ID_LINE)
    script = _UP_HEAD + _HELPERS + _UP_MAIN
    fills = {"@@NAME@@": name, "@@LABEL@@": PLATFORMS[task.platform], "@@Q_NAME@@": _q(name),
             "@@Q_URL@@": _q(url), "@@Q_MID@@": _q(mid), "@@Q_ADDRS@@": _q(" ".join(addrs)),
             "@@Q_STEPKEY@@": _q(step)}
    for marker, value in fills.items():
        script = script.replace(marker, value)
    # The multi-line fills go last so a value can never be mistaken for a marker.
    return script.replace("@@GUARDS@@", _UPDATE_GUARDS.rstrip("\n")).replace("@@TOML@@", "\n".join(lines))


_CL_HEAD = r"""#!/bin/sh
# Observe cleanup for @@NAME@@ (@@LABEL@@). Removes what the Observe install script put on THIS
# machine for @@NAME@@. Run it only on the machine that holds that install.
# It refuses to run on a machine with no install made for @@NAME@@, and nothing is changed then.
# It prints no key. The agent's data volume is kept.
set -u
umask 022
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

HOST_NAME=@@Q_NAME@@
OBSERVE_URL=@@Q_URL@@
STEP_KEY=@@Q_STEPKEY@@
ETC=/etc/hostwatch
"""

_CL_MAIN = r"""
main() {
@@ROOT@@
  # 2. Match guard. This machine must hold an install that was made for $HOST_NAME. The agent
  # settings name the host in HOSTWATCH_HOST_NAME and control.toml names it in host. A machine
  # with neither, or with an install for another host, is refused and nothing is changed.
  agent_here=0
  control_here=0
  if [ -f "$ETC/agent.env" ] && grep -qxF "HOSTWATCH_HOST_NAME=$HOST_NAME" "$ETC/agent.env"; then agent_here=1; fi
  if [ -f "$ETC/control.toml" ] && grep -qxF "host = \"$HOST_NAME\"" "$ETC/control.toml"; then control_here=1; fi
  if [ "$agent_here" = 0 ] && [ "$control_here" = 0 ]; then
    say "This command was made to remove the install for the host: $HOST_NAME" >&2
    say "This machine ($(hostname 2>/dev/null)) has no Observe install made for that host name." >&2
    refuse cleanup_match "no install for this host" "there is nothing here to clean up for $HOST_NAME. Run the command on the machine that holds the install."
  fi
  report cleanup_match ok "an install for this host is here"

  # 3. Remove. Each part is removed only when it belongs to this host.
  if [ "$control_here" = 1 ]; then
    systemctl disable --now hostwatch-control >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/hostwatch-control.service /etc/sudoers.d/hostwatch-control ||
      fail cleanup_control "cannot remove the unit or rules" "cannot remove the control service file or sudoers rules"
    systemctl daemon-reload >/dev/null 2>&1 || true
    rm -f "$ETC/control.toml" "$ETC/control.toml.new" "$ETC/control.env" "$ETC/control.env.new" ||
      fail cleanup_control "cannot remove settings" "cannot remove the control settings"
    rm -rf /opt/hostwatch-control ||
      fail cleanup_control "cannot remove the install folder" "cannot remove /opt/hostwatch-control"
    if id hostwatch-control >/dev/null 2>&1; then userdel hostwatch-control >/dev/null 2>&1 || say "The hostwatch-control account could not be removed. Remove it by hand."; fi
    report cleanup_control ok "control removed"
    say "Control removed."
  fi
  if [ "$agent_here" = 1 ]; then
    if command -v docker >/dev/null 2>&1; then
      docker rm -f hostwatch-agent hostwatch-agent-prev >/dev/null 2>&1 || true
    fi
    rm -f "$ETC/agent.env" "$ETC/agent.env.new" ||
      fail cleanup_agent "cannot remove settings" "cannot remove the agent settings"
    report cleanup_agent ok "agent removed"
    say "Agent container and settings removed. Its data volume hostwatch-agent-data was kept."
  fi
  rmdir "$ETC" >/dev/null 2>&1 || true
  report cleanup_files ok "files removed"
  report done ok "cleanup finished"
  say "Done. This machine no longer has the Observe install for $HOST_NAME."
  say "Use Regenerate command on the settings page of $HOST_NAME for the right machine. That also revokes the old keys."
}

main "$@"
"""

_CT_MAIN = r"""
main() {
@@ROOT@@
  # 2. Match guard. A TrueNAS install keeps agent.env on the pool; one made for $HOST_NAME names it.
  found=
  for envf in /mnt/*/hostwatch/agent.env; do
    [ -f "$envf" ] || continue
    if grep -qxF "HOSTWATCH_HOST_NAME=$HOST_NAME" "$envf"; then found="$found $(dirname "$envf")"; fi
  done
  if [ -z "$found" ]; then
    say "This command was made to remove the install for the host: $HOST_NAME" >&2
    say "This machine ($(hostname 2>/dev/null)) has no Observe install made for that host name." >&2
    refuse cleanup_match "no install for this host" "there is nothing here to clean up for $HOST_NAME. Run the command on the machine that holds the install."
  fi
  report cleanup_match ok "an install for this host is here"

  say "If you created the hostwatch app in the TrueNAS web UI, stop and delete it in Apps first."
  for dir in $found; do
    rm -f "$dir/agent.env" "$dir/agent.env.new" "$dir/compose.yaml" "$dir/compose.yaml.new" ||
      fail cleanup_files "cannot remove files" "cannot remove the files in $dir"
    say "Removed the settings and compose file in $dir. Its data folder was kept."
  done
  report cleanup_files ok "files removed"
  report done ok "cleanup finished"
  say "Done. This machine no longer has the Observe install for $HOST_NAME."
}

main "$@"
"""


def _cleanup_sh(task: RedeemedTask, ctx: Context, platforms: tuple[str, ...], main: str) -> str:
    name, url, _mid, step, _addrs = _common(task, ctx, "cleanup", platforms)
    script = _CL_HEAD + _HELPERS + main
    fills = {"@@NAME@@": name, "@@LABEL@@": PLATFORMS[task.platform], "@@Q_NAME@@": _q(name),
             "@@Q_URL@@": _q(url), "@@Q_STEPKEY@@": _q(step)}
    for marker, value in fills.items():
        script = script.replace(marker, value)
    return script.replace("@@ROOT@@", _ROOT_GUARD.rstrip("\n"))


def render_cleanup_linux(task: RedeemedTask, ctx: Context) -> str:
    return _cleanup_sh(task, ctx, UPDATE_PLATFORMS, _CL_MAIN)


def render_cleanup_truenas(task: RedeemedTask, ctx: Context) -> str:
    return _cleanup_sh(task, ctx, ("truenas",), _CT_MAIN)


_CW = r"""# Observe cleanup for @@NAME@@ (Windows). Removes what the Observe install script put on THIS
# machine for @@NAME@@. Run it only on the machine that holds that install.
# It refuses to run on a machine with no install made for @@NAME@@. It prints no key.
# The data folder is kept. A hostwatch-control install is not touched.
function Invoke-ObserveCleanup {
  $ErrorActionPreference = 'Stop'
  $HostName = @@P_NAME@@
  $ObserveUrl = @@P_URL@@
  $StepKey = @@P_STEPKEY@@

  # Report one step to Observe. The key goes in a header inside this process, never on a command
  # line. Notes are fixed text. A failed report is ignored.
  function Send-Step([string]$Step, [string]$Status, [string]$Note) {
    if (-not $StepKey) { return }
    try {
      [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
      $json = @{ step = $Step; status = $Status; note = $Note } | ConvertTo-Json -Compress
      Invoke-RestMethod -Uri "$ObserveUrl/api/enrol/step" -Method Post -ContentType 'application/json' -Headers @{ Authorization = "Bearer $StepKey" } -Body $json -TimeoutSec 10 | Out-Null
    } catch { }
  }
  function Stop-Refuse([string]$Step, [string]$Note, [string]$Message) {
    Write-Host "REFUSED: $Message" -ForegroundColor Red
    Write-Host 'Nothing was changed.' -ForegroundColor Red
    Send-Step $Step 'refused' $Note
    throw [System.OperationCanceledException]::new('stopped')
  }
  function Stop-Fail([string]$Step, [string]$Note, [string]$Message) {
    Write-Host "FAILED: $Message" -ForegroundColor Red
    Send-Step $Step 'failed' $Note
    throw [System.OperationCanceledException]::new('stopped')
  }

  try {
    # 1. Guards. Nothing after this block runs unless both pass, and they change nothing.
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
      Stop-Refuse 'root' 'not run as administrator' 'run this in an elevated PowerShell (Run as administrator).'
    }
    Write-Host "Observe cleanup for $HostName"
    Send-Step 'root' 'ok' 'running as administrator'

    $dataDir = Join-Path $env:ProgramData 'hostwatch'
    $envFile = Join-Path $dataDir 'agent.env'
    $line = "HOSTWATCH_HOST_NAME=$HostName"
    $owned = $false
    if (Test-Path -LiteralPath $envFile) {
      $owned = [bool](Get-Content -LiteralPath $envFile | Where-Object { $_ -ceq $line })
    }
    if (-not $owned) {
      Write-Host "This command was made to remove the install for the host: $HostName" -ForegroundColor Red
      Write-Host "This machine ($($env:COMPUTERNAME)) has no Observe install made for that host name." -ForegroundColor Red
      Stop-Refuse 'cleanup_match' 'no install for this host' "there is nothing here to clean up for $HostName. Run the command on the machine that holds the install."
    }
    Send-Step 'cleanup_match' 'ok' 'an install for this host is here'

    # 2. Remove the service, the environment and the protected settings file.
    $svc = 'hostwatch-agent'
    if (Get-Service -Name $svc -ErrorAction SilentlyContinue) {
      # The service must be stopped before anything is deleted, so a service that holds its
      # files stops the cleanup with the install still whole.
      try {
        Stop-Service -Name $svc -Force -ErrorAction Stop
        (Get-Service -Name $svc).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
      } catch { Stop-Fail 'cleanup_agent' 'service did not stop' 'the hostwatch-agent service did not stop, so nothing was removed.' }
      & sc.exe delete $svc | Out-Null
    }
    # Only the agent's own folder, as hostwatch's uninstall script does. A hostwatch-control
    # folder (control-venv) in the same install folder is not Observe's and is left alone.
    $venv = Join-Path (Join-Path $env:ProgramFiles 'hostwatch') 'venv'
    try {
      if (Test-Path -LiteralPath $venv) { Remove-Item -LiteralPath $venv -Recurse -Force }
      Remove-Item -LiteralPath $envFile -Force
    } catch { Stop-Fail 'cleanup_agent' 'cannot remove files' 'could not remove the agent files. The service was already deleted, so the install is partly removed. Close what holds the files and run the command again.' }
    Send-Step 'cleanup_agent' 'ok' 'agent removed'
    Send-Step 'cleanup_files' 'ok' 'files removed'
    Send-Step 'done' 'ok' 'cleanup finished'
    Write-Host "Done. This machine no longer has the Observe install for $HostName. The data folder was kept."
  }
  catch [System.OperationCanceledException] { }
  catch {
    Write-Host 'FAILED: the cleanup stopped with an unexpected error.' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Send-Step 'cleanup_agent' 'failed' 'unexpected error'
  }
}

Invoke-ObserveCleanup
"""


def render_cleanup_windows(task: RedeemedTask, ctx: Context) -> str:
    name, url, _mid, step, _addrs = _common(task, ctx, "cleanup", ("windows",))
    script = _CW
    fills = {"@@NAME@@": name, "@@P_NAME@@": _pq(name), "@@P_URL@@": _pq(url),
             "@@P_STEPKEY@@": _pq(step)}
    for marker, value in fills.items():
        script = script.replace(marker, value)
    return script


def render_task(task: RedeemedTask, ctx: Context) -> str:
    """The script for a task's kind and platform."""
    if task.kind == "update":
        return render_update(task, ctx)
    if task.platform == "truenas":
        return render_cleanup_truenas(task, ctx)
    if task.platform == "windows":
        return render_cleanup_windows(task, ctx)
    return render_cleanup_linux(task, ctx)
