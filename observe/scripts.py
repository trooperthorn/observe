"""Install scripts served by GET /i/{token} (docs/GUI-DESIGN.md section 3.10).

This module renders the POSIX sh script for the linux and raspberry-pi platforms (render_linux),
a POSIX sh script for TrueNAS SCALE (render_truenas) and a PowerShell script for Windows
(render_windows). TrueNAS and Windows are agent only. Every value
is validated here against a strict pattern, whatever the caller already checked, and then
written inside single quotes, so no value can carry shell syntax into the script. A value that
fails raises ScriptError and nothing is rendered.

The script is served without keys and without spending the token (GET /i/{token}). It carries the
token, and after every guard has passed it posts the token to the redeem endpoint, which spends it
and returns the keys, so a guard that refuses leaves the command valid. A refusing guard reports
the reason with the token to /api/enrol/guard. The script is written so it cannot be run on the
wrong machine. Before it changes anything it
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

from .enrol import DEFAULT_POOL, PLATFORMS, POOL, Redeemed, controller_id

LINUX_PLATFORMS = ("linux", "raspberry-pi")
SCRIPT_PLATFORMS = ("linux", "raspberry-pi", "truenas", "windows")
# Platforms whose script installs the agent only. Control needs the Linux account, sudoers and
# systemd path, which a TrueNAS appliance does not offer and Windows does not have yet (Q10).
AGENT_ONLY_PLATFORMS = ("truenas", "windows")

_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HEADER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,31}$")
_SERVICE = re.compile(r"^(?:docker:)?[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_URL = re.compile(r"^https?://(?:[A-Za-z0-9.-]{1,253}|\[[0-9a-fA-F:]{2,45}\])(?::[0-9]{1,5})?$")
_TOKEN = re.compile(r"^wpe_[A-Za-z0-9_-]{10,200}$")
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
        if not isinstance(header, str) or not _HEADER.fullmatch(header):
            raise ScriptError("a fan header has characters that are not allowed; hostwatch-control "
                              "accepts letters, digits, underscore and hyphen only, so remove "
                              "or rename that header in the allowlist")
        # The daemon's header list holds the controller's ids (enrol.controller_id), the names
        # thermalctl knows and the names a signed fan.set_floor carries.
        if controller_id(header) not in headers:
            headers.append(controller_id(header))
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
    if allow.get("update") is True:
        # agent.update may replace the agent container. The daemon's own self-update stays off:
        # it would restart the daemon that runs the command (docs/CONTROL.md, "[update]").
        lines += ["", "[update]", "agent = true", "control = false"]
    return "\n".join(lines) + "\n"


MACHINE_ID_LINE = r"""    if [ -n "$here_id" ]; then printf 'machine_id = "%s"\n' "$here_id"; fi"""

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
REDEEM_TOKEN=@@Q_REDEEM@@
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

# Before the token is redeemed there is no step key, so a refusal is reported with the token. The
# token is not spent by a refusal. FOUND is the name this machine gave itself, cut to the
# characters a host name has; the server cuts it again.
guard_report() { # step
  [ -n "$REDEEM_TOKEN" ] || return 0
  found=$(printf '%s' "${FOUND:-}" | tr -cd 'A-Za-z0-9._-' | cut -c1-64)
  printf '{"token":"%s","step":"%s","found":"%s"}' "$REDEEM_TOKEN" "$1" "$found" |
    curl -fsS -m 10 -X POST -H 'Content-Type: application/json' --data-binary @- \
      "$OBSERVE_URL/api/enrol/guard" >/dev/null 2>&1 || true
}

refuse() { # step, note for Observe, message for the screen
  say "REFUSED: $3" >&2
  say "Nothing was changed." >&2
  [ -n "$STEP_KEY" ] && { report "$1" refused "$2"; exit 1; }
  guard_report "$1"
  [ -n "$REDEEM_TOKEN" ] && say "The command was not used up. Run it on the right machine." >&2
  exit 1
}

# Spend the token and fetch the keys. It runs only after every guard passed. The token goes to
# curl on its standard input, never on a command line, and the reply is read with sed, never run.
redeem() {
  [ -n "$REDEEM_TOKEN" ] || return 0
  reply=$(printf '{"token":"%s"}' "$REDEEM_TOKEN" |
    curl -fsS -m 20 -X POST -H 'Content-Type: application/json' --data-binary @- \
      "$OBSERVE_URL/api/enrol/redeem" 2>/dev/null) ||
    { say "FAILED: Observe did not accept this command. It was already used or has expired. Make a new one in the Observe console." >&2; exit 1; }
  STEP_KEY=$(printf '%s' "$reply" | sed -n 's/.*"step_key":"\([^"]*\)".*/\1/p')
  AGENT_KEY=$(printf '%s' "$reply" | sed -n 's/.*"agent_key":"\([^"]*\)".*/\1/p')
  CONTROL_KEY=$(printf '%s' "$reply" | sed -n 's/.*"control_key":"\([^"]*\)".*/\1/p')
  reply=
  if [ -z "$STEP_KEY" ] ||
    { [ "${WANT_AGENT:-1}" = 1 ] && [ -z "$AGENT_KEY" ]; } ||
    { [ "${WANT_CONTROL:-0}" = 1 ] && [ -z "$CONTROL_KEY" ]; }; then
    say "FAILED: Observe sent an incomplete reply. Nothing was changed." >&2
    exit 1
  fi
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

  want=$(lower "$HOST_NAME")
  l_plain=$(lower "$(hostname 2>/dev/null)")
  l_short=$(lower "$(hostname -s 2>/dev/null || hostname 2>/dev/null | cut -d. -f1)")
  l_fqdn=$(lower "$(hostname -f 2>/dev/null || hostname 2>/dev/null)")
  if [ "$l_plain" != "$want" ] && [ "$l_short" != "$want" ] && [ "$l_fqdn" != "$want" ]; then
    say "This command was made for the host: $HOST_NAME" >&2
    say "This machine's hostname is:          $l_fqdn (short name $l_short)" >&2
    FOUND=$l_short
    refuse hostname "hostname does not match" "this is not $HOST_NAME. Run the command on $HOST_NAME."
  fi

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

  # The guards passed: spend the token, then tell Observe how far this run got.
  redeem
  report root ok "running as root"
  report hostname ok "hostname matches"
  report observe_host ok "not the Observe host"

  # 2. Rerun detection. The steps below replace what they own, so a second run updates in place.
  if [ -f "$ETC/agent.env" ] || [ -f "$ETC/control.toml" ] || [ -f "$ETC/control.env" ]; then
    say "An earlier install was found. It will be updated in place."
    report rerun ok "updating an existing install"
  fi
  mkdir -p "$ETC" || fail root "cannot create the settings folder" "cannot create $ETC"
  chmod 0755 "$ETC" || fail root "cannot set the settings folder mode" "cannot set the mode of $ETC"

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
    say "An existing hostwatch-agent container was found and will be replaced."
    project=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' hostwatch-agent 2>/dev/null || true)
    if [ -n "$project" ]; then
      say "It is managed by Docker Compose (project $project). Its data volume is kept, not deleted, and the new agent uses the volume hostwatch-agent-data."
    fi
    report rerun ok "replacing an existing agent container"
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

  { mkdir -p /opt/hostwatch-control && chmod 0755 /opt/hostwatch-control; } ||
    fail control_install "cannot create the install folder" "cannot create /opt/hostwatch-control"
  if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV" || fail control_install "venv failed" "could not create the Python environment"
  fi
  "$VENV/bin/pip" install --quiet --upgrade "hostwatch[control] @ $SOURCE" >/dev/null 2>&1 ||
    fail control_install "pip install failed" "could not install hostwatch[control] from github.com/trooperthorn/hostwatch"
  report control_install ok "hostwatch control installed"

  umask 077
  {
@@TOML@@
  } > "$ETC/control.toml.new" || fail control_config "cannot write settings" "cannot write control.toml"
  umask 022
  # Both files are root:hostwatch-control 0640, which is the install contract in hostwatch's
  # deploy-agents.md. systemd reads control.env as root, so 0600 root:root would also work.
  # They are written beside the live files and moved into place only after the sudoers rules
  # have rendered and passed visudo, so a failed render changes nothing that is live.
  chown root:hostwatch-control "$ETC/control.toml.new" && chmod 0640 "$ETC/control.toml.new" ||
    fail control_config "cannot set modes" "cannot secure control.toml"
  umask 077
  {
    printf 'HOSTWATCH_CONTROL_URL=%s\n' "$OBSERVE_URL"
    printf 'HOSTWATCH_CONTROL_KEY=%s\n' "$CONTROL_KEY"
  } > "$ETC/control.env.new" || fail control_config "cannot write settings" "cannot write control.env"
  umask 022
  chown root:hostwatch-control "$ETC/control.env.new" && chmod 0640 "$ETC/control.env.new" ||
    fail control_config "cannot set modes" "cannot secure control.env"

  sudoers_tmp=$(mktemp) || fail sudoers "no temporary file" "cannot create a temporary file"
  "$VENV/bin/python" -c 'import sys; from hostwatch.control import config, actions_linux; sys.stdout.write(actions_linux.render_sudoers(config.load("/etc/hostwatch/control.toml.new")))' > "$sudoers_tmp" ||
    { rm -f "$sudoers_tmp"; fail sudoers "render failed" "could not render the sudoers rules"; }
  if ! visudo -c -f "$sudoers_tmp" >/dev/null 2>&1; then
    rm -f "$sudoers_tmp"
    fail sudoers "visudo rejected the rules" "visudo rejected the generated rules, so none were installed"
  fi
  { mv -f "$ETC/control.toml.new" "$ETC/control.toml" && mv -f "$ETC/control.env.new" "$ETC/control.env"; } ||
    { rm -f "$sudoers_tmp"; fail control_config "cannot move settings into place" "cannot move the control settings into place"; }
  report control_config ok "control settings written"
  if ! install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/hostwatch-control; then
    rm -f "$sudoers_tmp"
    fail sudoers "install failed" "could not install the sudoers rules"
  fi
  rm -f "$sudoers_tmp"
  report sudoers ok "sudoers checked and installed"

  {
    cat > /etc/systemd/system/hostwatch-control.service <<'UNIT_EOF'
@@UNIT@@UNIT_EOF
  } || fail control_unit "cannot write the unit" "cannot write the service file"
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


def _redeem_token(red: Redeemed) -> str:
    """The token the script posts to the redeem endpoint, or "" when the keys are in the script.

    With a token, the key fields of `red` only say which keys are wanted: their text is not
    written into the script, which fetches the real keys after its guards pass."""
    if not red.redeem_token:
        return ""
    return _need(_TOKEN, red.redeem_token, "the install token")


def render_linux(red: Redeemed, ctx: Context) -> str:
    """The install script for one redeemed enrolment. Raises ScriptError on any unsafe value."""
    if red.platform not in LINUX_PLATFORMS:
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
    redeem = _redeem_token(red)
    if (agent and not agent.startswith("wpi_")) or (control and not control.startswith("wpc_")):
        raise ScriptError("a key has the wrong marker")
    if not agent and not control:
        raise ScriptError("choose the agent, control or both")
    toml = "    :"
    unit = ""
    if control:
        body = control_toml(name, red.allowlist, ctx.public_key)
        # machine_id is a top-level key, so it goes before the first [table] header. A key after
        # a header belongs to that table, and the daemon would not see it.
        out = [f"    printf '%s\\n' {_q(line)}" for line in body.splitlines()]
        out.insert(2, MACHINE_ID_LINE)
        toml = "\n".join(out)
        unit = _UNIT
    elif red.allowlist.get("fans") or red.allowlist.get("services") or red.allowlist.get("reboot") \
            or red.allowlist.get("update"):
        raise ScriptError("an allowlist needs control")
    script = _HEAD + _BODY
    fills = {"@@NAME@@": name, "@@LABEL@@": PLATFORMS[red.platform],
             "@@Q_NAME@@": _q(name), "@@Q_URL@@": _q(url), "@@Q_MID@@": _q(mid),
             "@@Q_ADDRS@@": _q(" ".join(a for a in addrs if a)), "@@Q_REDEEM@@": _q(redeem),
             "@@Q_STEPKEY@@": _q("" if redeem else step),
             "@@Q_AGENTKEY@@": _q("" if redeem else agent),
             "@@Q_CONTROLKEY@@": _q("" if redeem else control),
             "@@AGENT@@": "1" if agent else "0", "@@CONTROL@@": "1" if control else "0",
             "@@Q_IMAGE@@": _q(IMAGE), "@@Q_SOURCE@@": _q(SOURCE)}
    for marker, value in fills.items():
        script = script.replace(marker, value)
    # The multi-line fills go last so a value can never be mistaken for a marker.
    return script.replace("@@TOML@@", toml).replace("@@UNIT@@", unit)


# ---------------------------------------------------------------------------------------------
# TrueNAS SCALE and Windows (agent only)
# ---------------------------------------------------------------------------------------------

# hostwatch's default branch is master; a main.zip would not exist and the Windows install would fail.
WINDOWS_SOURCE = "https://github.com/trooperthorn/hostwatch/archive/refs/heads/master.zip"

# The helper functions and the three guards are the Linux script's own text, cut out of it, so
# every platform runs the same guard code in the same order.
_HELPERS = _BODY[:_BODY.index("main() {")]
_GUARDS = _BODY[_BODY.index("  # 1. Guards."):_BODY.index("  # 2. Rerun detection.")]


def valid_pool(pool: str) -> bool:
    """True for an empty pool (the default is used) or a safe TrueNAS pool name."""
    return pool == "" or (POOL.fullmatch(pool) is not None and ".." not in pool)


def media_type(platform: str) -> str:
    return "text/plain; charset=utf-8" if platform == "windows" else "text/x-shellscript"


def error_body(platform: str, message: str = "Observe could not build this script") -> str:
    """What the script URL returns when Observe could not build the script. It must be safe to
    run in the shell that fetched it: `exit` would close a PowerShell console. `message` is fixed
    text from the server, never a request value."""
    if platform == "windows":
        return f"Write-Host '{message}. Nothing was changed.'\n"
    return f"echo '{message}' >&2; exit 1\n"


def _pq(value: str) -> str:
    """Single-quote one value for PowerShell, where a quote is doubled."""
    return "'" + value.replace("'", "''") + "'"


def _agent_only(red: Redeemed, ctx: Context, platform: str) -> tuple[str, str, str, str, str, list[str]]:
    """Validate what both agent-only scripts share. Returns the host name, the Observe URL, the
    Observe machine id, the step key, the agent key and the usable Observe addresses."""
    if red.platform != platform:
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
    _redeem_token(red)
    if not agent.startswith("wpi_"):
        raise ScriptError("this platform needs an agent key")
    if red.control_key or red.allowlist.get("fans") or red.allowlist.get("services") \
            or red.allowlist.get("reboot") or red.allowlist.get("update"):
        raise ScriptError("control is not available for this platform")
    return name, url, mid, step, agent, [a for a in addrs if a]


_TN_HEAD = r"""#!/bin/sh
# Observe install for @@NAME@@ (TrueNAS). Run on @@NAME@@ only.
# It refuses to run on any other machine and on the Observe host. It prints no key.
# It writes files on the pool; one step is done by you in the TrueNAS web UI and is printed
# at the end. Run it again to update the files in place.
set -u
umask 022
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

HOST_NAME=@@Q_NAME@@
OBSERVE_URL=@@Q_URL@@
OBSERVE_MACHINE_ID=@@Q_MID@@
OBSERVE_ADDRS=@@Q_ADDRS@@
REDEEM_TOKEN=@@Q_REDEEM@@
STEP_KEY=@@Q_STEPKEY@@
AGENT_KEY=@@Q_AGENTKEY@@
POOL=@@Q_POOL@@
IMAGE=@@Q_IMAGE@@
"""

_TN_MAIN = r"""
main() {
@@GUARDS@@
  # 2. This looks like TrueNAS, and the pool exists. Still nothing has changed.
  if ! command -v midclt >/dev/null 2>&1; then
    say "Note: midclt was not found, so this may not be TrueNAS SCALE. Continuing because the host name matched."
  fi
  base=/mnt/$POOL
  if [ ! -d "$base" ]; then
    say "Pools and datasets under /mnt:" >&2
    ls -1 /mnt >&2 2>/dev/null || true
    refuse pool "pool not found" "there is no pool named $POOL on $HOST_NAME. Ask Observe for a new command and choose the pool that exists."
  fi
  report pool ok "pool found"
  dir=$base/hostwatch

  # 3. Rerun detection. The files below are replaced, never appended to.
  if [ -f "$dir/agent.env" ] || [ -f "$dir/compose.yaml" ]; then
    say "An earlier install was found in $dir. Its files will be updated in place."
    report rerun ok "updating existing files"
  fi

  mkdir -p "$dir/data" || fail agent "cannot create the folder" "cannot create $dir"
  chown 10001:10001 "$dir/data" || fail agent "cannot set the data folder owner" "cannot set the owner of $dir/data"
  chmod 0755 "$dir" || fail agent "cannot set the folder mode" "cannot set the mode of $dir"
  write_env
  write_compose
  report agent ok "agent settings written"
  report compose ok "compose file written"

  say ""
  say "=================================================================="
  say " One step is left, and it is done in the TrueNAS web UI of $HOST_NAME."
  say " Do it on $HOST_NAME only."
  say ""
  say "   1. Open Apps, then Discover Apps, then the three dot menu,"
  say "      then Install via YAML."
  say "   2. Name the app: hostwatch"
  say "   3. Paste the whole of this file, which holds no key:"
  say "        $dir/compose.yaml"
  say "      You can print it with: cat $dir/compose.yaml"
  say "   4. Save. Observe shows $HOST_NAME when the agent sends data."
  say ""
  say " The agent key is in $dir/agent.env (root only). It was not printed."
  say " Optional extras, such as the TrueNAS API source and energy counters,"
  say " are in docs/deploy-truenas.md of the hostwatch repository."
  say "=================================================================="
  report app skipped "waiting for the manual step in the TrueNAS web UI"
  report done ok "files written"
  say "Done. Observe shows progress for $HOST_NAME."
}

write_env() {
  rm -f "$dir/agent.env.new"
  umask 077
  {
    printf 'HOSTWATCH_HUB_URL=%s\n' "$OBSERVE_URL"
    printf 'HOSTWATCH_INGEST_KEY=%s\n' "$AGENT_KEY"
    printf 'HOSTWATCH_HOST_NAME=%s\n' "$HOST_NAME"
  } > "$dir/agent.env.new" || fail agent "cannot write settings" "cannot write the agent settings"
  umask 022
  # Docker reads env_file as root before the container starts, so root only is enough.
  chmod 0400 "$dir/agent.env.new" && mv -f "$dir/agent.env.new" "$dir/agent.env" ||
    fail agent "cannot secure settings" "cannot secure the agent settings"
}

write_compose() {
  gid=$(getent group systemd-journal 2>/dev/null | cut -d: -f3)
  case $gid in ''|*[!0-9]*) gid= ;; esac
  rm -f "$dir/compose.yaml.new"
  {
    cat <<COMPOSE_HEAD
# hostwatch agent for $HOST_NAME, written by the Observe install script.
# It holds no key: the hub address and the key are in agent.env beside this file.
services:
  hostwatch:
    image: $IMAGE
    pull_policy: always
    container_name: hostwatch
    restart: unless-stopped
    network_mode: host
    user: "10001:10001"
    read_only: true
    privileged: false
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    env_file:
      - $dir/agent.env
    environment:
      HOSTWATCH_ROLE: agent
      HOSTWATCH_HOST_NAME: $HOST_NAME
COMPOSE_HEAD
    if [ -n "$gid" ]; then
      printf '      HOSTWATCH_JOURNAL_GID: "%s"\n' "$gid"
      printf '    group_add:\n      - "%s"\n' "$gid"
    fi
    printf '    volumes:\n      - /sys:/host/sys:ro\n'
    if [ -d /var/log/journal ]; then printf '      - /var/log/journal:/host/journal:ro\n'; fi
    if [ -d /run/log/journal ]; then printf '      - /run/log/journal:/host/journal-volatile:ro\n'; fi
    if [ -d /sys/fs/pstore ]; then printf '      - /sys/fs/pstore:/host/pstore:ro\n'; fi
    printf '      - %s/data:/data\n' "$dir"
  } > "$dir/compose.yaml.new" || fail compose "cannot write the compose file" "cannot write the compose file"
  chmod 0644 "$dir/compose.yaml.new" && mv -f "$dir/compose.yaml.new" "$dir/compose.yaml" ||
    fail compose "cannot place the compose file" "cannot place the compose file"
}

main "$@"
"""


def render_truenas(red: Redeemed, ctx: Context, pool: str = "") -> str:
    """The install script for a TrueNAS SCALE host. Raises ScriptError on any unsafe value."""
    name, url, mid, step, agent, addrs = _agent_only(red, ctx, "truenas")
    redeem = _redeem_token(red)
    pool = pool or DEFAULT_POOL
    if not valid_pool(pool):
        raise ScriptError("the pool name has characters that are not allowed")
    script = _TN_HEAD + _HELPERS + _TN_MAIN
    fills = {"@@NAME@@": name, "@@Q_NAME@@": _q(name), "@@Q_URL@@": _q(url), "@@Q_MID@@": _q(mid),
             "@@Q_ADDRS@@": _q(" ".join(addrs)), "@@Q_REDEEM@@": _q(redeem),
             "@@Q_STEPKEY@@": _q("" if redeem else step),
             "@@Q_AGENTKEY@@": _q("" if redeem else agent), "@@Q_POOL@@": _q(pool),
             "@@Q_IMAGE@@": _q(IMAGE)}
    for marker, value in fills.items():
        script = script.replace(marker, value)
    return script.replace("@@GUARDS@@", _GUARDS.rstrip("\n"))


_WIN = r"""# Observe install for @@NAME@@ (Windows). Run on @@NAME@@ only.
# It refuses to run on any other machine and on the Observe host. It prints no key.
# The agent is installed with hostwatch's own installer; control is not available on Windows yet.
function Invoke-ObserveInstall {
  $ErrorActionPreference = 'Stop'
  $HostName = @@P_NAME@@
  $ObserveUrl = @@P_URL@@
  $ObserveMachineId = @@P_MID@@
  $ObserveAddrs = @(@@P_ADDRS@@)
  $RedeemToken = @@P_REDEEM@@
  $StepKey = @@P_STEPKEY@@
  $AgentKey = @@P_AGENTKEY@@
  $SourceUrl = @@P_SOURCE@@

  # Report one step to Observe. The key goes in a header inside this process, never on a command
  # line. Notes are fixed text, so no key can be carried in one. A failed report is ignored.
  function Send-Step([string]$Step, [string]$Status, [string]$Note) {
    if (-not $StepKey) { return }
    try {
      [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
      $json = @{ step = $Step; status = $Status; note = $Note } | ConvertTo-Json -Compress
      Invoke-RestMethod -Uri "$ObserveUrl/api/enrol/step" -Method Post -ContentType 'application/json' -Headers @{ Authorization = "Bearer $StepKey" } -Body $json -TimeoutSec 10 | Out-Null
    } catch { }
  }
  # Before the token is redeemed there is no step key, so a refusal is reported with the token.
  # The token is not spent by a refusal. $Found is the name this machine gave itself.
  $Found = ''
  function Send-Guard([string]$Step) {
    if (-not $RedeemToken -or $StepKey) { return }
    try {
      [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
      $seen = ([string]$Found) -replace '[^A-Za-z0-9._-]', ''
      if ($seen.Length -gt 64) { $seen = $seen.Substring(0, 64) }
      $json = @{ token = $RedeemToken; step = $Step; found = $seen } | ConvertTo-Json -Compress
      Invoke-RestMethod -Uri "$ObserveUrl/api/enrol/guard" -Method Post -ContentType 'application/json' -Body $json -TimeoutSec 10 | Out-Null
    } catch { }
  }
  function Stop-Refuse([string]$Step, [string]$Note, [string]$Message) {
    Write-Host "REFUSED: $Message" -ForegroundColor Red
    Write-Host 'Nothing was changed.' -ForegroundColor Red
    if ($StepKey) { Send-Step $Step 'refused' $Note } else {
      Send-Guard $Step
      if ($RedeemToken) { Write-Host 'The command was not used up. Run it on the right machine.' -ForegroundColor Red }
    }
    throw [System.OperationCanceledException]::new('stopped')
  }
  function Stop-Fail([string]$Step, [string]$Note, [string]$Message) {
    Write-Host "FAILED: $Message" -ForegroundColor Red
    Send-Step $Step 'failed' $Note
    throw [System.OperationCanceledException]::new('stopped')
  }

  $work = $null
  try {
    # 1. Guards. Nothing after this block runs unless all three pass, and they change nothing.
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
      Stop-Refuse 'root' 'not run as administrator' 'run this in an elevated PowerShell (Run as administrator).'
    }
    Write-Host "Observe install for $HostName"

    $want = $HostName.ToLowerInvariant()
    $dnsName = ''
    $fqdn = ''
    try { $dnsName = [Net.Dns]::GetHostName() } catch { }
    try { $fqdn = [Net.Dns]::GetHostEntry($dnsName).HostName } catch { }
    $mine = @($env:COMPUTERNAME, $dnsName, $fqdn, ($fqdn -split '\.')[0]) | Where-Object { $_ } | ForEach-Object { $_.ToLowerInvariant() }
    if ($mine -notcontains $want) {
      Write-Host "This command was made for the host: $HostName" -ForegroundColor Red
      Write-Host "This machine's name is:             $($env:COMPUTERNAME) (full name $fqdn)" -ForegroundColor Red
      $Found = $env:COMPUTERNAME
      Stop-Refuse 'hostname' 'hostname does not match' "this is not $HostName. Run the command on $HostName."
    }

    $hereId = ''
    try { $hereId = ((Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid).MachineGuid -replace '-', '').ToLowerInvariant() } catch { }
    if ($ObserveMachineId -and $hereId -and $hereId -eq $ObserveMachineId) {
      Stop-Refuse 'observe_host' 'machine id is the Observe host' 'this is the Observe host itself. Install agents on the machines to be monitored, not here.'
    }
    $hereAddrs = @()
    try {
      foreach ($nic in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        foreach ($ua in $nic.GetIPProperties().UnicastAddresses) { $hereAddrs += ($ua.Address.ToString() -split '%')[0] }
      }
    } catch { }
    $resolved = @()
    try { $resolved = @([Net.Dns]::GetHostAddresses(([Uri]$ObserveUrl).DnsSafeHost) | ForEach-Object { ($_.ToString() -split '%')[0] }) } catch { }
    foreach ($addr in (@($ObserveAddrs) + $resolved)) {
      if ($hereAddrs -contains $addr) {
        Stop-Refuse 'observe_host' 'address is the Observe host' "this machine has the address of the Observe host ($addr). Install agents on the machines to be monitored, not here."
      }
    }

    # The guards passed: spend the token, then tell Observe how far this run got.
    if ($RedeemToken) {
      $reply = $null
      try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $body = @{ token = $RedeemToken } | ConvertTo-Json -Compress
        $reply = Invoke-RestMethod -Uri "$ObserveUrl/api/enrol/redeem" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 20
      } catch { }
      if (-not $reply -or -not $reply.step_key -or -not $reply.agent_key) {
        Stop-Fail 'agent' 'redeem failed' 'Observe did not accept this command. It was already used or has expired. Make a new one in the Observe console.'
      }
      $StepKey = [string]$reply.step_key
      $AgentKey = [string]$reply.agent_key
      $reply = $null
    }
    Send-Step 'root' 'ok' 'running as administrator'
    Send-Step 'hostname' 'ok' 'hostname matches'
    Send-Step 'observe_host' 'ok' 'not the Observe host'

    # 2. Checks that change nothing.
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
      Stop-Fail 'agent' 'python not found' 'Python 3 is needed and was not found on the path. Install Python, then ask Observe for a new command.'
    }
    $existing = Get-Service -Name 'hostwatch-agent' -ErrorAction SilentlyContinue

    # 3. Download hostwatch and run its own installer.
    $work = Join-Path ([IO.Path]::GetTempPath()) ('observe-install-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    Write-Host 'Downloading hostwatch.'
    try {
      [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
      $zip = Join-Path $work 'hostwatch.zip'
      Invoke-WebRequest -Uri $SourceUrl -OutFile $zip -UseBasicParsing
      Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $work 'src') -Force
    } catch { Stop-Fail 'download' 'download failed' 'could not download hostwatch from github.com/trooperthorn/hostwatch.' }
    $src = Get-ChildItem -LiteralPath (Join-Path $work 'src') -Directory | Select-Object -First 1
    $installer = if ($src) { Join-Path $src.FullName 'deploy\windows\install.ps1' } else { '' }
    if (-not $installer -or -not (Test-Path -LiteralPath $installer)) {
      Stop-Fail 'download' 'installer not in the download' 'the download did not hold deploy\windows\install.ps1.'
    }
    Send-Step 'download' 'ok' 'hostwatch downloaded'

    if ($existing) {
      Write-Host 'An earlier install was found. It will be removed with the uninstall.ps1 of hostwatch (its data folder is kept) and installed again.'
      Send-Step 'rerun' 'ok' 'updating an existing install'
      $remover = Join-Path $src.FullName 'deploy\windows\uninstall.ps1'
      try { & $remover -Force } catch { Stop-Fail 'rerun' 'removing the earlier install failed' 'could not remove the earlier install.' }
    }

    Write-Host 'Installing the hostwatch agent service.'
    try {
      # The key is handed over as a SecureString object. It is on no command line.
      $secure = ConvertTo-SecureString $AgentKey -AsPlainText -Force
      & $installer -HubUrl $ObserveUrl -HostName $HostName -IngestKey $secure -SourcePath $src.FullName -Force
    } catch { Stop-Fail 'agent' 'installer failed' 'the installer of hostwatch failed. Its messages are above.' }
    $service = Get-Service -Name 'hostwatch-agent' -ErrorAction SilentlyContinue
    if (-not $service -or $service.Status -ne 'Running') {
      Stop-Fail 'agent' 'service is not running' 'the hostwatch-agent service is not running.'
    }
    Send-Step 'agent' 'ok' 'agent service running'
    Send-Step 'done' 'ok' 'install finished'
    Write-Host "Done. Observe shows progress for $HostName."
  }
  catch [System.OperationCanceledException] { }
  catch {
    Write-Host 'FAILED: the install stopped with an unexpected error.' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Send-Step 'agent' 'failed' 'unexpected error'
  }
  finally {
    $AgentKey = $null
    if ($work -and (Test-Path -LiteralPath $work)) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
  }
}

Invoke-ObserveInstall
"""


def render_windows(red: Redeemed, ctx: Context) -> str:
    """The PowerShell install script for a Windows host. Raises ScriptError on any unsafe value."""
    name, url, mid, step, agent, addrs = _agent_only(red, ctx, "windows")
    redeem = _redeem_token(red)
    script = _WIN
    fills = {"@@NAME@@": name, "@@P_NAME@@": _pq(name), "@@P_URL@@": _pq(url),
             "@@P_MID@@": _pq(mid), "@@P_ADDRS@@": ", ".join(_pq(a) for a in addrs),
             "@@P_REDEEM@@": _pq(redeem), "@@P_STEPKEY@@": _pq("" if redeem else step),
             "@@P_AGENTKEY@@": _pq("" if redeem else agent),
             "@@P_SOURCE@@": _pq(WINDOWS_SOURCE)}
    for marker, value in fills.items():
        script = script.replace(marker, value)
    return script


def render(red: Redeemed, ctx: Context, pool: str = "") -> str:
    """The script for the enrolment's platform."""
    if red.platform == "truenas":
        return render_truenas(red, ctx, pool)
    if red.platform == "windows":
        return render_windows(red, ctx)
    return render_linux(red, ctx)
