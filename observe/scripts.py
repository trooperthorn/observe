"""Install scripts served by GET /i/{token} (docs/GUI-DESIGN.md section 3.10).

This module renders the POSIX sh script for the linux and raspberry-pi platforms. Every value
is validated here against a strict pattern, whatever the caller already checked, and then
written inside single quotes, so no value can carry shell syntax into the script. A value that
fails raises ScriptError and nothing is rendered.

The script is written so it cannot be run on the wrong machine. Before it changes anything it
checks that it runs as root, that the machine's hostname (short or fully qualified, any case)
is the enrolled host name, and that it is not running on the Observe host itself. It reports
each step back to Observe with the step key of the redeemed token. Keys are written to files
with root-only modes and are never printed, never put on a command line and never sent in a
report. The whole body is one function that runs on the last line, so a download cut short
runs nothing.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

from .enrol import PLATFORMS, Redeemed

SCRIPT_PLATFORMS = ("linux", "raspberry-pi")

_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HEADER = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_SERVICE = re.compile(r"^(?:docker:)?[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_URL = re.compile(r"^https?://(?:[A-Za-z0-9.-]{1,253}|\[[0-9a-fA-F:]{2,45}\])(?::[0-9]{1,5})?$")
_KEY = re.compile(r"^(wpi|wpc|wps)_[A-Za-z0-9_-]{10,200}$")
_PUBLIC_KEY = re.compile(r"^ed25519:[A-Za-z0-9+/]{43}=$")
_MACHINE_ID = re.compile(r"^[0-9a-f]{32}$")
IMAGE = "ghcr.io/trooperthorn/hostwatch:edge"
SOURCE = "git+https://github.com/trooperthorn/hostwatch.git"


class ScriptError(ValueError):
    """A value that cannot safely go into the script."""


@dataclass(frozen=True)
class Context:
    """What the server knows that the enrolment row does not."""

    observe_url: str  # origin the console was reached at
    observe_machine_id: str  # empty when Observe cannot read one (a container, usually)
    observe_addrs: tuple[str, ...]  # non-loopback addresses Observe listens on or was reached at
    public_key: str  # the control plugin's ed25519 public key, empty when control is not served


def _q(value: str) -> str:
    """Single-quote one value. The validators already exclude quotes; this is the second layer."""
    return "'" + value.replace("'", "'\\''") + "'"


def _need(pattern: re.Pattern[str], value: Any, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ScriptError(f"{what} has characters that are not allowed")
    return value


def usable_address(text: str) -> str | None:
    """A normalised IP text for the Observe-host guard, or None for loopback, wildcard or a name."""
    try:
        ip = ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return None
    if ip.is_loopback or ip.is_unspecified:
        return None
    return str(ip)


def _toml_list(items: list[str]) -> str:
    return "[" + ", ".join('"' + i + '"' for i in items) + "]"


def control_toml(host: str, allow: dict[str, Any], public_key: str) -> str:
    """The body of control.toml (the script adds machine_id from the local machine)."""
    fans, services = allow.get("fans", []), allow.get("services", [])
    headers: list[str] = []
    limits: list[int] = []
    for fan in fans:
        header = fan["header"] if isinstance(fan, dict) else fan
        headers.append(_need(_HEADER, header, "a fan header"))
        if isinstance(fan, dict) and fan.get("min_duty_limit") is not None:
            limit = fan["min_duty_limit"]
            if type(limit) is not int or not 0 <= limit <= 100:
                raise ScriptError("min_duty_limit must be a whole number from 0 to 100")
            limits.append(limit)
    for svc in services:
        _need(_SERVICE, svc, "a service")
        if ".." in svc:
            raise ScriptError("a service has characters that are not allowed")
    lines = [f'observe_public_key = "{_need(_PUBLIC_KEY, public_key, "the public key")}"',
             f'host = "{host}"']
    if headers:
        # control.toml holds one floor for the whole fan table. The strictest per-header limit
        # is used, so a remote request never goes below any header's own limit.
        lines += ["", "[fan]", 'controller = "thermalctl"', f"headers = {_toml_list(headers)}",
                  f"min_duty_floor = {max(limits) if limits else 0}", "min_duty_ceiling = 100",
                  "allow_mode_change = false"]
    if services:
        lines += ["", "[services]", f"restart = {_toml_list(list(services))}"]
    if allow.get("reboot") is True:
        lines += ["", "[reboot]", "allow = true", "delay_s = 60"]
    return "\n".join(lines) + "\n"


_UNIT = """[Unit]
Description=hostwatch control daemon
Documentation=https://github.com/trooperthorn/hostwatch
After=network-online.target
Wants=network-online.target
ConditionPathExists=/etc/hostwatch/control.toml

[Service]
Type=simple
User=hostwatch-control
Group=hostwatch-control
EnvironmentFile=/etc/hostwatch/control.env
ExecStart=/opt/hostwatch-control/venv/bin/python -m hostwatch control run
Restart=on-failure
RestartSec=10
StateDirectory=hostwatch-control
StateDirectoryMode=0700
Environment=HOSTWATCH_CONTROL_DATA_DIR=/var/lib/hostwatch-control
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictRealtime=true
LockPersonality=true
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
"""

_HEAD = r"""#!/bin/sh
# Observe install for @@NAME@@ (@@LABEL@@). Run on @@NAME@@ only.
# It refuses to run on any other machine and on the Observe host. It prints no key.
# Run it again to update an existing install in place.
set -u
umask 022
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

HOST_NAME=@@Q_NAME@@
OBSERVE_URL=@@Q_URL@@
OBSERVE_MACHINE_ID=@@Q_MID@@
OBSERVE_ADDRS=@@Q_ADDRS@@
STEP_KEY=@@Q_STEPKEY@@
AGENT_KEY=@@Q_AGENTKEY@@
CONTROL_KEY=@@Q_CONTROLKEY@@
WANT_AGENT=@@AGENT@@
WANT_CONTROL=@@CONTROL@@
IMAGE=@@Q_IMAGE@@
SOURCE=@@Q_SOURCE@@
ETC=/etc/hostwatch
VENV=/opt/hostwatch-control/venv
"""

_BODY = r"""
say() { printf '%s\n' "$*"; }

# Report one step to Observe. The step key goes in a curl config read from stdin, so it is on no
# command line. Notes are fixed text, never a variable, so no key can be carried in one.
report() {
  [ -n "$STEP_KEY" ] || return 0
  printf 'header = "Authorization: Bearer %s"\n' "$STEP_KEY" |
    curl -fsS -m 10 -K - -X POST -H 'Content-Type: application/json' \
      --data "{\"step\":\"$1\",\"status\":\"$2\",\"note\":\"$3\"}" \
      "$OBSERVE_URL/api/enrol/step" >/dev/null 2>&1 || true
}

fail() { # step, note for Observe, message for the screen
  say "FAILED: $3" >&2
  report "$1" failed "$2"
  exit 1
}

refuse() { # step, note for Observe, message for the screen
  say "REFUSED: $3" >&2
  say "Nothing was changed." >&2
  report "$1" refused "$2"
  exit 1
}

lower() { printf '%s' "$1" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz'; }

local_addrs() {
  if command -v ip >/dev/null 2>&1; then
    ip -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1
  elif command -v hostname >/dev/null 2>&1; then
    hostname -I 2>/dev/null | tr ' ' '\n'
  fi
}

main() {
  # 1. Guards. Nothing after this block runs unless all three pass, and they change nothing.
  if [ "$(id -u)" -ne 0 ]; then
    refuse root "not run as root" "run this as root, for example with sudo."
  fi
  say "Observe install for $HOST_NAME"
  report root ok "running as root"

  want=$(lower "$HOST_NAME")
  l_plain=$(lower "$(hostname 2>/dev/null)")
  l_short=$(lower "$(hostname -s 2>/dev/null || hostname 2>/dev/null | cut -d. -f1)")
  l_fqdn=$(lower "$(hostname -f 2>/dev/null || hostname 2>/dev/null)")
  if [ "$l_plain" != "$want" ] && [ "$l_short" != "$want" ] && [ "$l_fqdn" != "$want" ]; then
    say "This command was made for the host: $HOST_NAME" >&2
    say "This machine's hostname is:          $l_fqdn (short name $l_short)" >&2
    refuse hostname "hostname does not match" "this is not $HOST_NAME. Run the command on $HOST_NAME."
  fi
  report hostname ok "hostname matches"

  here_id=$(cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id 2>/dev/null || true)
  here_addrs=$(local_addrs)
  if [ -n "$OBSERVE_MACHINE_ID" ] && [ "$here_id" = "$OBSERVE_MACHINE_ID" ]; then
    refuse observe_host "machine id is the Observe host" "this is the Observe host itself. Install agents on the machines to be monitored, not here."
  fi
  url_host=${OBSERVE_URL#*://}
  case $url_host in
    '['*) url_host=${url_host#\[}; url_host=${url_host%%]*} ;;
    *) url_host=${url_host%%:*} ;;
  esac
  resolved=$(getent hosts "$url_host" 2>/dev/null | awk '{print $1}' || true)
  for addr in $OBSERVE_ADDRS $resolved; do
    for mine in $here_addrs; do
      if [ "$addr" = "$mine" ]; then
        refuse observe_host "address is the Observe host" "this machine has the address of the Observe host ($addr). Install agents on the machines to be monitored, not here."
      fi
    done
  done
  report observe_host ok "not the Observe host"

  # 2. Rerun detection. The steps below replace what they own, so a second run updates in place.
  if [ -f "$ETC/agent.env" ] || [ -f "$ETC/control.toml" ] || [ -f "$ETC/control.env" ]; then
    say "An earlier install was found. It will be updated in place."
    report rerun ok "updating an existing install"
  fi
  mkdir -p "$ETC" || fail root "cannot create the settings folder" "cannot create $ETC"
  chmod 0755 "$ETC"

  if [ "$WANT_AGENT" = 1 ]; then install_agent; fi
  if [ "$WANT_CONTROL" = 1 ]; then install_control; fi
  report done ok "install finished"
  say "Done. Observe shows progress for $HOST_NAME."
}

install_agent() {
  if ! command -v docker >/dev/null 2>&1; then
    fail agent "docker not found" "Docker is not installed. Install Docker, then ask Observe for a new command."
  fi
  say "Installing the hostwatch agent container."
  rm -f "$ETC/agent.env.new"
  umask 077
  {
    printf 'HOSTWATCH_ROLE=agent\n'
    printf 'HOSTWATCH_HUB_URL=%s\n' "$OBSERVE_URL"
    printf 'HOSTWATCH_INGEST_KEY=%s\n' "$AGENT_KEY"
    printf 'HOSTWATCH_HOST_NAME=%s\n' "$HOST_NAME"
  } > "$ETC/agent.env.new" || fail agent "cannot write settings" "cannot write the agent settings"
  umask 022
  chmod 0600 "$ETC/agent.env.new" && mv -f "$ETC/agent.env.new" "$ETC/agent.env" ||
    fail agent "cannot secure settings" "cannot secure the agent settings"
  docker pull "$IMAGE" >/dev/null 2>&1 || say "Could not pull $IMAGE, using a local copy if there is one."
  # Keep the old container until the new one runs, so a failed start does not leave no agent.
  docker rm -f hostwatch-agent-prev >/dev/null 2>&1 || true
  had_old=0
  if docker inspect hostwatch-agent >/dev/null 2>&1; then
    docker stop hostwatch-agent >/dev/null 2>&1 || true
    docker rename hostwatch-agent hostwatch-agent-prev >/dev/null 2>&1 &&
      had_old=1 || docker rm -f hostwatch-agent >/dev/null 2>&1 || true
  fi
  set -- --detach --name hostwatch-agent --restart unless-stopped --network host \
    --user 10001:10001 --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges:true --env-file "$ETC/agent.env" \
    --volume /sys:/host/sys:ro --volume hostwatch-agent-data:/data
  gid=$(getent group systemd-journal 2>/dev/null | cut -d: -f3)
  if [ -n "$gid" ]; then set -- "$@" --group-add "$gid" -e "HOSTWATCH_JOURNAL_GID=$gid"; fi
  if [ -d /var/log/journal ]; then set -- "$@" --volume /var/log/journal:/host/journal:ro; fi
  if [ -d /run/log/journal ]; then set -- "$@" --volume /run/log/journal:/host/journal-volatile:ro; fi
  if [ -d /run/thermalctl ]; then set -- "$@" --volume /run/thermalctl:/run/thermalctl:ro; fi
  if ! docker run "$@" "$IMAGE" >/dev/null 2>&1; then
    docker rm -f hostwatch-agent >/dev/null 2>&1 || true
    if [ "$had_old" = 1 ]; then
      docker rename hostwatch-agent-prev hostwatch-agent >/dev/null 2>&1 &&
        docker start hostwatch-agent >/dev/null 2>&1 || true
    fi
    fail agent "container did not start" "docker could not start the agent container. Any earlier agent was put back."
  fi
  docker rm -f hostwatch-agent-prev >/dev/null 2>&1 || true
  report agent ok "agent container running"
  say "Agent container started."
}

install_control() {
  for tool in python3 git useradd visudo systemctl; do
    command -v "$tool" >/dev/null 2>&1 || fail control_account "missing tool" "$tool is needed for control and was not found"
  done
  if ! id hostwatch-control >/dev/null 2>&1; then
    nologin=/usr/sbin/nologin
    [ -x "$nologin" ] || nologin=/sbin/nologin
    useradd --system --no-create-home --shell "$nologin" hostwatch-control ||
      fail control_account "account not created" "could not create the hostwatch-control account"
  fi
  report control_account ok "account present"

  mkdir -p /opt/hostwatch-control && chmod 0755 /opt/hostwatch-control
  if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV" || fail control_install "venv failed" "could not create the Python environment"
  fi
  "$VENV/bin/pip" install --quiet --upgrade "hostwatch[control] @ $SOURCE" >/dev/null 2>&1 ||
    fail control_install "pip install failed" "could not install hostwatch[control] from github.com/trooperthorn/hostwatch"
  report control_install ok "hostwatch control installed"

  umask 077
  {
@@TOML@@
    if [ -n "$here_id" ]; then printf 'machine_id = "%s"\n' "$here_id"; fi
  } > "$ETC/control.toml.new" || fail control_config "cannot write settings" "cannot write control.toml"
  umask 022
  # The daemon runs as hostwatch-control and must read these, so they are root:hostwatch-control 0640.
  { chown root:hostwatch-control "$ETC/control.toml.new" && chmod 0640 "$ETC/control.toml.new" &&
      mv -f "$ETC/control.toml.new" "$ETC/control.toml"; } ||
    fail control_config "cannot set modes" "cannot secure control.toml"
  umask 077
  {
    printf 'HOSTWATCH_CONTROL_URL=%s\n' "$OBSERVE_URL"
    printf 'HOSTWATCH_CONTROL_KEY=%s\n' "$CONTROL_KEY"
  } > "$ETC/control.env.new" || fail control_config "cannot write settings" "cannot write control.env"
  umask 022
  { chown root:hostwatch-control "$ETC/control.env.new" && chmod 0640 "$ETC/control.env.new" &&
      mv -f "$ETC/control.env.new" "$ETC/control.env"; } ||
    fail control_config "cannot set modes" "cannot secure control.env"
  report control_config ok "control settings written"

  sudoers_tmp=$(mktemp) || fail sudoers "no temporary file" "cannot create a temporary file"
  "$VENV/bin/python" -c 'import sys; from hostwatch.control import config, actions_linux; sys.stdout.write(actions_linux.render_sudoers(config.load("/etc/hostwatch/control.toml")))' > "$sudoers_tmp" ||
    { rm -f "$sudoers_tmp"; fail sudoers "render failed" "could not render the sudoers rules"; }
  if ! visudo -c -f "$sudoers_tmp" >/dev/null 2>&1; then
    rm -f "$sudoers_tmp"
    fail sudoers "visudo rejected the rules" "visudo rejected the generated rules, so none were installed"
  fi
  if ! install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/hostwatch-control; then
    rm -f "$sudoers_tmp"
    fail sudoers "install failed" "could not install the sudoers rules"
  fi
  rm -f "$sudoers_tmp"
  report sudoers ok "sudoers checked and installed"

  cat > /etc/systemd/system/hostwatch-control.service <<'UNIT_EOF'
@@UNIT@@UNIT_EOF
  chmod 0644 /etc/systemd/system/hostwatch-control.service ||
    fail control_unit "cannot set unit mode" "cannot set the mode of the service file"
  systemctl daemon-reload &&
    systemctl enable hostwatch-control >/dev/null 2>&1 &&
    systemctl restart hostwatch-control ||
    fail control_unit "service did not start" "systemd could not start hostwatch-control"
  report control_unit ok "control service started"
  say "Control daemon started."
}

main "$@"
"""


def render_linux(red: Redeemed, ctx: Context) -> str:
    """The install script for one redeemed enrolment. Raises ScriptError on any unsafe value."""
    if red.platform not in SCRIPT_PLATFORMS:
        raise ScriptError("no install script for this platform")
    name = _need(_NAME, red.host, "the host name")
    url = _need(_URL, ctx.observe_url, "the Observe address")
    mid = ctx.observe_machine_id
    if mid:
        _need(_MACHINE_ID, mid, "the Observe machine id")
    addrs = [usable_address(a) for a in ctx.observe_addrs]
    if any(a is None for a in addrs):
        raise ScriptError("an Observe address is not a usable IP address")
    step = _need(_KEY, red.step_key, "the step key") if red.step_key else ""
    agent = _need(_KEY, red.agent_key, "the agent key") if red.agent_key else ""
    control = _need(_KEY, red.control_key, "the control key") if red.control_key else ""
    if (agent and not agent.startswith("wpi_")) or (control and not control.startswith("wpc_")):
        raise ScriptError("a key has the wrong marker")
    if not agent and not control:
        raise ScriptError("choose the agent, control or both")
    toml = "    :"
    unit = ""
    if control:
        body = control_toml(name, red.allowlist, ctx.public_key)
        toml = "\n".join(f"    printf '%s\\n' {_q(line)}" for line in body.splitlines())
        unit = _UNIT
    elif red.allowlist.get("fans") or red.allowlist.get("services") or red.allowlist.get("reboot"):
        raise ScriptError("an allowlist needs control")
    script = _HEAD + _BODY
    fills = {"@@NAME@@": name, "@@LABEL@@": PLATFORMS[red.platform],
             "@@Q_NAME@@": _q(name), "@@Q_URL@@": _q(url), "@@Q_MID@@": _q(mid),
             "@@Q_ADDRS@@": _q(" ".join(a for a in addrs if a)), "@@Q_STEPKEY@@": _q(step),
             "@@Q_AGENTKEY@@": _q(agent), "@@Q_CONTROLKEY@@": _q(control),
             "@@AGENT@@": "1" if agent else "0", "@@CONTROL@@": "1" if control else "0",
             "@@Q_IMAGE@@": _q(IMAGE), "@@Q_SOURCE@@": _q(SOURCE)}
    for marker, value in fills.items():
        script = script.replace(marker, value)
    # The multi-line fills go last so a value can never be mistaken for a marker.
    return script.replace("@@TOML@@", toml).replace("@@UNIT@@", unit)
