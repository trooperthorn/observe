"""Linux and Docker checks over SSH.

Transport: asyncssh, one connection per poll. Host keys are always verified
against the monitor's known_hosts file; there is no option to skip that for
a monitor. Agent use and agent forwarding are disabled, so a compromised
target can not borrow keys from whatever is running watchpost.

Every command is a fixed string. The only config values placed in a
command (mount point, systemd unit, container name, docker command) are
pattern-validated at config load AND passed through shlex.quote here, so a
value can not add shell syntax.

Linux values come from /proc and coreutils present on any distribution:
  cpu      /proc/stat sampled twice, 1 s apart; busy = 1 - idle delta / total delta
  memory   /proc/meminfo; used = 1 - MemAvailable / MemTotal (excludes reclaimable
           cache, unlike SNMP hrStorageRam on Linux)
  disk     df -P -k for the mount; the capacity column
  load     1-minute load average divided by nproc, as a percentage
  uptime   /proc/uptime; WARN once when it resets (reboot)
  service  systemctl is-active for a systemd unit
  command  your command; its last line must be a number

Docker, via the docker CLI on the host (the SSH account needs Docker access,
which is root-equivalent on that host; see THREAT-MODEL.md for a sudo rule
that narrows it):
  container  state, health, and restart count for one container
  summary    how many containers are unhealthy, or stopped despite a
             restart policy of always/unless-stopped
"""

from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any

import asyncssh

from ..config import SshCredential
from .base import Check, CheckResult, Result

SSH_ERRORS = (asyncssh.Error, OSError, asyncio.TimeoutError)


async def ssh_connect(host: str, port: int, cred: SshCredential, known_hosts: str | None,
                      timeout: float) -> asyncssh.SSHClientConnection:
    """known_hosts=None means "do not verify" and is only ever passed by
    discovery, for key-based credentials, with the key recorded for review."""
    if known_hosts is None and cred.password and not cred.private_key:
        raise ValueError("refusing to offer a password to an unverified host key")
    kwargs: dict[str, Any] = {
        "port": port, "username": cred.username, "known_hosts": known_hosts,
        "agent_path": None, "agent_forwarding": False,
        "connect_timeout": timeout, "login_timeout": timeout,
    }
    if cred.private_key:
        kwargs["client_keys"] = [cred.private_key]
        kwargs["passphrase"] = cred.passphrase
    if cred.password and known_hosts is not None:
        kwargs["password"] = cred.password
    else:
        kwargs["password"] = None
        kwargs["preferred_auth"] = "publickey"
    return await asyncssh.connect(host, **kwargs)


async def ssh_run(conn: asyncssh.SSHClientConnection, command: str,
                  timeout: float) -> tuple[int, str, str]:
    r = await asyncio.wait_for(conn.run(command, check=False), timeout)
    return (r.exit_status if r.exit_status is not None else 255,
            str(r.stdout or "").strip(), str(r.stderr or "").strip())


class _SshCheck(Check):
    async def run_commands(self, command: str) -> tuple[int, str, str]:
        m = self.monitor
        async with await ssh_connect(m.host, m.port, self.credential(), m.known_hosts,
                                     self.timeout) as conn:
            return await ssh_run(conn, command, self.timeout + 5)

    async def probe(self) -> CheckResult:
        try:
            return await self._probe()
        except asyncssh.HostKeyNotVerifiable:
            return CheckResult.fail(
                f"host key for {self.monitor.host} not in {self.monitor.known_hosts} "
                "or does not match it")
        except asyncssh.PermissionDenied:
            return CheckResult.fail("SSH authentication rejected")
        except asyncio.TimeoutError:
            return CheckResult.fail("SSH timed out")
        except (asyncssh.Error, OSError) as err:
            return CheckResult.fail(f"SSH: {type(err).__name__}: {err}")

    async def _probe(self) -> CheckResult:  # pragma: no cover - abstract
        raise NotImplementedError

    @staticmethod
    def first_err(err: str, rc: int) -> str:
        return err.splitlines()[0] if err else f"exit {rc}"


class LinuxCheck(_SshCheck):
    def __init__(self, monitor: Any, config: Any) -> None:
        super().__init__(monitor, config)
        self._last_uptime: float | None = None

    async def _probe(self) -> CheckResult:
        return await getattr(self, f"_mode_{self.monitor.mode}")()

    async def _mode_cpu(self) -> CheckResult:
        rc, out, err = await self.run_commands("head -n1 /proc/stat; sleep 1; head -n1 /proc/stat")
        lines = [ln.split() for ln in out.splitlines() if ln.startswith("cpu ")]
        if rc != 0 or len(lines) != 2:
            return CheckResult.fail(f"could not read /proc/stat: {self.first_err(err, rc)}")
        a, b = ([int(x) for x in ln[1:]] for ln in lines)
        total = sum(b) - sum(a)
        idle = (b[3] + b[4]) - (a[3] + a[4])  # idle + iowait
        if total <= 0:
            return CheckResult.fail("no CPU time elapsed between samples")
        pct = round((1 - idle / total) * 100, 1)
        return CheckResult.ok(f"CPU {pct:g}% busy", value=pct, unit="%")

    async def _mode_memory(self) -> CheckResult:
        rc, out, err = await self.run_commands("cat /proc/meminfo")
        info = {}
        for ln in out.splitlines():
            key, _, rest = ln.partition(":")
            if rest.strip():
                info[key] = int(rest.split()[0])
        if rc != 0 or "MemTotal" not in info or "MemAvailable" not in info:
            return CheckResult.fail(f"could not read MemAvailable: {self.first_err(err, rc)}")
        pct = round((1 - info["MemAvailable"] / info["MemTotal"]) * 100, 1)
        return CheckResult.ok(
            f"RAM {pct:g}% used ({(info['MemTotal'] - info['MemAvailable']) / 2**20:.1f} of "
            f"{info['MemTotal'] / 2**20:.1f} GiB, excluding reclaimable cache)",
            value=pct, unit="%")

    async def _mode_disk(self) -> CheckResult:
        m = self.monitor
        rc, out, err = await self.run_commands(f"df -P -k -- {shlex.quote(m.mount)}")
        rows = out.splitlines()
        if rc != 0 or len(rows) < 2:
            return CheckResult.fail(f"df {m.mount} failed: {self.first_err(err, rc)}")
        parts = rows[-1].split()
        if len(parts) < 6 or parts[5] != m.mount:
            got = parts[5] if len(parts) >= 6 else "?"
            return CheckResult.fail(f"{m.mount} is not a mount point (df reports {got})")
        pct = float(parts[4].rstrip("%"))
        free_gib = int(parts[3]) / 2**20
        return CheckResult.ok(f"{m.mount} {pct:g}% used, {free_gib:.1f} GiB free",
                              value=pct, unit="%")

    async def _mode_load(self) -> CheckResult:
        rc, out, err = await self.run_commands("cat /proc/loadavg; nproc")
        lines = out.splitlines()
        if rc != 0 or len(lines) < 2:
            return CheckResult.fail(f"could not read load: {self.first_err(err, rc)}")
        load1, cores = float(lines[0].split()[0]), int(lines[1])
        pct = round(load1 / max(cores, 1) * 100, 1)
        return CheckResult.ok(f"load {load1:g} on {cores} cores ({pct:g}% of capacity)",
                              value=pct, unit="%")

    async def _mode_uptime(self) -> CheckResult:
        rc, out, err = await self.run_commands("cat /proc/uptime")
        if rc != 0 or not out:
            return CheckResult.fail(f"could not read /proc/uptime: {self.first_err(err, rc)}")
        secs = float(out.split()[0])
        res = CheckResult.ok(f"up {secs / 86400:.2f} days", value=round(secs / 86400, 3),
                             unit=" days")
        if self._last_uptime is not None and secs < self._last_uptime:
            res.result = Result.WARN
            res.message = f"rebooted (uptime reset), now up {secs / 86400:.2f} days"
        self._last_uptime = secs
        return res

    async def _mode_service(self) -> CheckResult:
        unit = self.monitor.service
        rc, out, err = await self.run_commands(f"systemctl is-active -- {shlex.quote(unit)}")
        state = out.splitlines()[-1] if out else ""
        if rc == 0 and state == "active":
            return CheckResult.ok(f"{unit} is active")
        if not state:
            return CheckResult.fail(f"systemctl failed: {self.first_err(err, rc)}")
        return CheckResult.fail(f"{unit} is {state}")

    async def _mode_command(self) -> CheckResult:
        rc, out, err = await self.run_commands(self.monitor.command)
        if rc != 0:
            return CheckResult.fail(f"command failed: {self.first_err(err, rc)}")
        try:
            num = float(out.splitlines()[-1])
        except (ValueError, IndexError):
            return CheckResult.fail(f"expected a number, got {out[-80:]!r}")
        return CheckResult.ok(f"result {num:g}", value=num)


def docker_argv(docker_command: str, *args: str) -> str:
    return " ".join([*docker_command.split(), *(shlex.quote(a) for a in args)])


CONTAINER_FORMAT = "{{json .State}}|{{.RestartCount}}"
SUMMARY_FORMAT = ("{{.Name}}|{{.State.Status}}|{{.HostConfig.RestartPolicy.Name}}|"
                  "{{if .State.Health}}{{.State.Health.Status}}{{end}}")


class DockerCheck(_SshCheck):
    def __init__(self, monitor: Any, config: Any) -> None:
        super().__init__(monitor, config)
        self._last_restarts: int | None = None

    async def _probe(self) -> CheckResult:
        if self.monitor.mode == "container":
            return await self._container()
        return await self._summary()

    async def _container(self) -> CheckResult:
        m = self.monitor
        # Ask only for State and RestartCount. A bare `docker inspect` returns
        # the whole container config, including environment variables, which
        # routinely hold secrets; there is no reason to pull them over SSH.
        rc, out, err = await self.run_commands(docker_argv(
            m.docker_command, "inspect", "--type", "container", "--format",
            CONTAINER_FORMAT, "--", m.container))
        if rc != 0:
            return CheckResult.fail(f"docker inspect {m.container}: {self.first_err(err, rc)}")
        state_json, _, restarts_s = out.strip().rpartition("|")
        try:
            state = json.loads(state_json)
            restarts = int(restarts_s)
        except (json.JSONDecodeError, ValueError):
            return CheckResult.fail(f"unparseable docker inspect output {out[:80]!r}")
        status = state.get("Status", "unknown")
        health = (state.get("Health") or {}).get("Status")
        detail = {"status": status, "health": health, "restart_count": restarts,
                  "started_at": state.get("StartedAt")}
        restarted = self._last_restarts is not None and restarts > self._last_restarts
        grew_by = restarts - (self._last_restarts or 0)
        self._last_restarts = restarts
        if status != "running":
            code = state.get("ExitCode")
            return CheckResult.fail(
                f"{m.container} is {status}" + (f" (exit {code})" if code is not None else ""),
                detail=detail)
        if health == "unhealthy":
            return CheckResult.fail(f"{m.container} is running but unhealthy", detail=detail)
        res = CheckResult.ok(f"{m.container} running" + (f", {health}" if health else ""),
                             detail=detail)
        if health == "starting":
            res.result, res.message = Result.WARN, f"{m.container} health check starting"
        elif restarted:
            res.result = Result.WARN
            res.message = f"{m.container} restarted {grew_by}x since last poll ({restarts} total)"
        return res

    async def _summary(self) -> CheckResult:
        m = self.monitor
        ids_cmd = docker_argv(m.docker_command, "ps", "-aq", "--no-trunc")
        rc, out, err = await self.run_commands(ids_cmd)
        if rc != 0:
            return CheckResult.fail(f"docker ps: {self.first_err(err, rc)}")
        ids = out.split()
        if not ids:
            return CheckResult.ok("no containers", value=0.0)
        rc, out, err = await self.run_commands(
            docker_argv(m.docker_command, "inspect", "--format", SUMMARY_FORMAT, "--", *ids))
        if rc != 0:
            return CheckResult.fail(f"docker inspect: {self.first_err(err, rc)}")
        problems, running = [], 0
        for line in out.splitlines():
            name, status, policy, health = (line.split("|") + ["", "", "", ""])[:4]
            name = name.lstrip("/")
            running += status == "running"
            if health == "unhealthy":
                problems.append(f"{name} unhealthy")
            elif status != "running" and policy in ("always", "unless-stopped"):
                problems.append(f"{name} {status} (restart={policy})")
        msg = f"{running}/{len(ids)} running"
        if problems:
            msg += "; " + ", ".join(problems)
        res = CheckResult.ok(msg, value=float(len(problems)), detail={"problems": problems})
        if problems and self.monitor.thresholds is None:
            res.result = Result.FAIL
        return res
