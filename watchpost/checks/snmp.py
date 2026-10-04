"""SNMP checks using the net-snmp command line tools.

Why shell out instead of a Python SNMP stack: net-snmp is the reference
implementation, its USM (v3) handling is mature, and its behaviour is easy to
reproduce by hand. Every probe here can be re-run at a shell with the exact
argv shown in the monitor detail (secrets redacted), which is how you debug
"works in my NMS, fails here" without guessing.

Known trade-off (see THREAT-MODEL.md): community strings and v3 passphrases
appear in the child process argv for the life of each poll, visible to other
processes in the same PID namespace. The container has its own PID namespace
and runs nothing else.

Output is requested with -On (numeric OIDs), -Oq (no type labels),
-Oe (enums as integers), -Ot (raw timeticks), -OU (no units), and -m ""
(load no MIBs), so parsing never depends on which MIB files are installed.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from ..config import SnmpV2Credential, SnmpV3Credential
from .base import Check, CheckResult, Result

SYS_UPTIME = ".1.3.6.1.2.1.1.3.0"
IF_DESCR = ".1.3.6.1.2.1.2.2.1.2"
IF_OPER_STATUS = ".1.3.6.1.2.1.2.2.1.8"
IF_NAME = ".1.3.6.1.2.1.31.1.1.1.1"
IF_HC_IN_OCTETS = ".1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT_OCTETS = ".1.3.6.1.2.1.31.1.1.1.10"
IF_HIGH_SPEED = ".1.3.6.1.2.1.31.1.1.1.15"  # Mbit/s
HR_PROCESSOR_LOAD = ".1.3.6.1.2.1.25.3.3.1.2"
HR_STORAGE_TYPE = ".1.3.6.1.2.1.25.2.3.1.2"
HR_STORAGE_UNITS = ".1.3.6.1.2.1.25.2.3.1.4"
HR_STORAGE_SIZE = ".1.3.6.1.2.1.25.2.3.1.5"
HR_STORAGE_USED = ".1.3.6.1.2.1.25.2.3.1.6"
HR_STORAGE_RAM = ".1.3.6.1.2.1.25.2.1.2"

IF_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant",
             6: "notPresent", 7: "lowerLayerDown"}
_NO_VALUE = ("No Such Object", "No Such Instance", "No more variables")


class SnmpError(Exception):
    pass


class SnmpCheck(Check):
    def __init__(self, monitor: Any, config: Any) -> None:
        super().__init__(monitor, config)
        self._if_index: str | None = None
        self._last_octets: tuple[float, int, int] | None = None  # (t, in, out)
        self._last_uptime: int | None = None

    # ------------------------------------------------------------ transport

    def _auth_args(self) -> tuple[list[str], list[str]]:
        """Return (argv fragment, list of secret strings to redact)."""
        cred = self.credential()
        if isinstance(cred, SnmpV2Credential):
            return ["-v2c", "-c", cred.community], [cred.community]
        assert isinstance(cred, SnmpV3Credential)
        args = ["-v3", "-l", cred.level, "-u", cred.username,
                "-a", cred.auth_protocol, "-A", cred.auth_password]
        secrets = [cred.auth_password]
        if cred.level == "authPriv" and cred.priv_password:
            args += ["-x", cred.priv_protocol, "-X", cred.priv_password]
            secrets.append(cred.priv_password)
        return args, secrets

    async def _run(self, tool: str, oids: list[str]) -> list[tuple[str, str]]:
        auth, secrets = self._auth_args()
        # Timeout per request, no net-snmp retries: the scheduler's
        # failures_to_down already provides confirmation.
        argv = [tool, "-m", "", "-On", "-OqetU", "-t", str(self.timeout), "-r", "0", *auth,
                f"udp:{self.monitor.host}:{self.monitor.port}", *oids]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "SNMP_PERSISTENT_DIR": "/tmp/watchpost-snmp", "HOME": "/tmp"}
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout * 3 + 5)
        except asyncio.TimeoutError:
            proc.kill()
            raise SnmpError(f"{tool} did not exit")
        if proc.returncode != 0:
            text = err.decode(errors="replace").strip().splitlines()
            reason = text[-1] if text else f"{tool} exited {proc.returncode}"
            for s in secrets:
                reason = reason.replace(s, "***")
            raise SnmpError(reason)
        rows: list[tuple[str, str]] = []
        for line in out.decode(errors="replace").splitlines():
            oid, _, value = line.partition(" ")
            value = value.strip()
            if any(value.startswith(n) for n in _NO_VALUE):
                continue
            rows.append((oid, value.strip('"')))
        return rows

    async def get(self, *oids: str) -> dict[str, str]:
        return dict(await self._run("snmpget", list(oids)))

    async def walk(self, oid: str) -> dict[str, str]:
        """Return {index_suffix: value} for a table column."""
        rows = await self._run("snmpwalk", [oid])
        prefix = oid + "."
        return {o[len(prefix):]: v for o, v in rows if o.startswith(prefix)}

    # ------------------------------------------------------------------ modes

    async def probe(self) -> CheckResult:
        try:
            return await getattr(self, f"_mode_{self.monitor.mode}")()
        except SnmpError as err:
            return CheckResult.fail(f"SNMP: {err}")

    async def _mode_oid(self) -> CheckResult:
        oid = self.monitor.oid if self.monitor.oid.startswith(".") else "." + self.monitor.oid
        vals = await self.get(oid)
        if oid not in vals:
            return CheckResult.fail(f"{oid} not present on agent")
        raw = vals[oid]
        try:
            num: float | None = float(raw)
        except ValueError:
            num = None
        return CheckResult.ok(f"{oid} = {raw}", value=num, detail={"raw": raw})

    async def _mode_uptime(self) -> CheckResult:
        vals = await self.get(SYS_UPTIME)
        if SYS_UPTIME not in vals:
            return CheckResult.fail("sysUpTime not present on agent")
        ticks = int(vals[SYS_UPTIME])
        days = ticks / 100 / 86400
        res = CheckResult.ok(f"up {days:.2f} days", value=round(days, 3), unit=" days")
        if self._last_uptime is not None and ticks < self._last_uptime:
            res.result = Result.WARN
            res.message = f"agent restarted (uptime reset), now up {days:.2f} days"
        self._last_uptime = ticks
        return res

    async def _resolve_if_index(self) -> str | None:
        want = self.monitor.interface
        if want.isdigit():
            return want
        for column in (IF_NAME, IF_DESCR):
            for idx, name in (await self.walk(column)).items():
                if name == want:
                    return idx
        return None

    async def _mode_interface(self) -> CheckResult:
        if self._if_index is None:
            self._if_index = await self._resolve_if_index()
            if self._if_index is None:
                return CheckResult.fail(f"interface {self.monitor.interface!r} not found")
        i = self._if_index
        oids = [f"{IF_OPER_STATUS}.{i}", f"{IF_HC_IN_OCTETS}.{i}",
                f"{IF_HC_OUT_OCTETS}.{i}", f"{IF_HIGH_SPEED}.{i}"]
        vals = await self.get(*oids)
        status_raw = vals.get(oids[0])
        if status_raw is None:
            self._if_index = None  # index may have moved after a reboot; re-resolve
            return CheckResult.fail(f"ifIndex {i} vanished from agent")
        status = int(status_raw)
        label = IF_STATUS.get(status, str(status))
        if status != 1:
            return CheckResult.fail(f"{self.monitor.interface} is {label}",
                                    detail={"ifIndex": i})
        now = asyncio.get_running_loop().time()
        detail: dict[str, Any] = {"ifIndex": i}
        link_speed = int(vals.get(oids[3], "0") or 0)
        if link_speed > 0:
            detail["speed_mbps"] = link_speed  # read by the infrastructure findings
        msg = f"{self.monitor.interface} up"
        value = None
        if oids[1] in vals and oids[2] in vals:
            cur_in, cur_out = int(vals[oids[1]]), int(vals[oids[2]])
            if self._last_octets:
                t0, in0, out0 = self._last_octets
                dt_s = now - t0
                if dt_s > 0 and cur_in >= in0 and cur_out >= out0:
                    in_bps = (cur_in - in0) * 8 / dt_s
                    out_bps = (cur_out - out0) * 8 / dt_s
                    detail.update(in_bps=round(in_bps), out_bps=round(out_bps))
                    msg += f", in {in_bps/1e6:.2f} / out {out_bps/1e6:.2f} Mbit/s"
                    speed_mbps = int(vals.get(oids[3], "0") or 0)
                    if speed_mbps > 0:
                        value = round(max(in_bps, out_bps) / (speed_mbps * 1e6) * 100, 2)
                        msg += f" ({value:.1f}% of {speed_mbps} Mbit/s)"
            self._last_octets = (now, cur_in, cur_out)
        else:
            msg += " (no 64-bit counters; rate unavailable)"
        return CheckResult.ok(msg, value=value, unit="%", detail=detail)

    async def _mode_cpu(self) -> CheckResult:
        loads = [int(v) for v in (await self.walk(HR_PROCESSOR_LOAD)).values()]
        if not loads:
            return CheckResult.fail("hrProcessorLoad not exposed by this agent")
        avg = sum(loads) / len(loads)
        return CheckResult.ok(f"CPU {avg:.0f}% avg over {len(loads)} cores",
                              value=round(avg, 1), unit="%", detail={"per_core": loads})

    async def _mode_memory(self) -> CheckResult:
        types = await self.walk(HR_STORAGE_TYPE)
        ram = [idx for idx, t in types.items() if t == HR_STORAGE_RAM]
        if not ram:
            return CheckResult.fail("no hrStorageRam entry exposed by this agent")
        i = ram[0]
        vals = await self.get(f"{HR_STORAGE_SIZE}.{i}", f"{HR_STORAGE_USED}.{i}",
                              f"{HR_STORAGE_UNITS}.{i}")
        size = int(vals.get(f"{HR_STORAGE_SIZE}.{i}", 0))
        used = int(vals.get(f"{HR_STORAGE_USED}.{i}", 0))
        units = int(vals.get(f"{HR_STORAGE_UNITS}.{i}", 1))
        if size <= 0:
            return CheckResult.fail("hrStorageRam reports zero size")
        pct = used / size * 100
        return CheckResult.ok(
            f"RAM {pct:.0f}% used ({used*units/2**30:.1f} of {size*units/2**30:.1f} GiB)",
            value=round(pct, 1), unit="%",
        )
