"""Storage and hypervisor platforms: TrueNAS, Proxmox VE, VMware vSphere.

TrueNAS: JSON-RPC 2.0 over WebSocket at wss://host/api/current, the API
TrueNAS documents as supported from 25.04 (the REST API is deprecated in
25.04 and removed in 26). Authentication is auth.login_ex with the
API_KEY_PLAIN mechanism and a user-linked API key. TrueNAS CORE (13.x) has
no JSON-RPC endpoint and is not supported.

Proxmox VE: REST at https://host:8006/api2/json, authenticated with an API
token in the header `Authorization: PVEAPIToken=USER@REALM!TOKENID=SECRET`.
One call to /cluster/resources returns nodes, guests, and storage, on a
single node or a cluster.

vSphere: pyVmomi against ESXi or vCenter. Properties are fetched by explicit
path through the PropertyCollector rather than by walking managed objects,
which is both cheaper and tolerant of properties a server leaves unset. Each
poll logs in and logs out, because ESXi limits concurrent sessions and a
leaked session per poll would exhaust them.

TLS: all three are verified by default. A device with a self-signed
certificate can be pinned by putting that certificate in `ca_bundle`:
partial-chain verification is enabled so a single leaf is accepted as a
trust anchor, and the hostname check still applies.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from .base import Check, CheckResult, Result

VSPHERE_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="vsphere")


def api_ssl_context(verify: bool, ca_bundle: str | None) -> ssl.SSLContext:
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context(cafile=ca_bundle)
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


class AuthFailed(Exception):
    """The endpoint answered and rejected the credential."""


def pct(used: float | None, total: float | None) -> float | None:
    if used is None or not total:
        return None
    return round(used / total * 100, 1)


# =================================================================== TrueNAS

ALERT_LEVELS = ["INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"]


class TrueNASClient:
    def __init__(self, host: str, port: int, username: str, api_key: str,
                 ctx: ssl.SSLContext, timeout: float) -> None:
        self.url = f"wss://{host}:{port}/api/current"
        self.username, self.api_key = username, api_key
        self.ctx, self.timeout = ctx, timeout
        self._id = 0
        self._ws: Any = None

    async def __aenter__(self) -> "TrueNASClient":
        self._ws = await ws_connect(self.url, ssl=self.ctx, open_timeout=self.timeout,
                                    close_timeout=2, max_size=8 * 2**20)
        res = await self.call("auth.login_ex", {"mechanism": "API_KEY_PLAIN",
                                                "username": self.username,
                                                "api_key": self.api_key})
        rtype = (res or {}).get("response_type")
        if rtype != "SUCCESS":
            await self._ws.close()
            raise AuthFailed(f"auth.login_ex returned {rtype}")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def call(self, method: str, *params: Any) -> Any:
        self._id += 1
        my_id = self._id
        await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": my_id, "method": method,
                                        "params": list(params)}))
        async with asyncio.timeout(self.timeout + 10):
            while True:
                msg = json.loads(await self._ws.recv())
                if msg.get("id") != my_id:
                    continue  # event notifications carry no id
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(f"{method}: {err.get('message')} "
                                       f"({(err.get('data') or {}).get('reason', '')})".strip())
                return msg.get("result")


class TrueNASCheck(Check):
    async def probe(self) -> CheckResult:
        m = self.monitor
        cred = self.credential()
        ctx = api_ssl_context(m.verify_tls, m.ca_bundle)
        try:
            async with TrueNASClient(m.host, m.port, cred.username, cred.api_key, ctx,
                                     self.timeout) as tn:
                if m.mode == "alerts":
                    return self._alerts(await tn.call("alert.list"))
                pools = await tn.call("pool.query")
        except AuthFailed as err:
            return CheckResult.fail(f"TrueNAS authentication failed: {err}")
        except ssl.SSLCertVerificationError as err:
            return CheckResult.fail(f"TLS certificate did not validate: {err.verify_message}")
        except (OSError, WebSocketException, TimeoutError, RuntimeError,
                json.JSONDecodeError) as err:
            return CheckResult.fail(f"TrueNAS API: {type(err).__name__}: {err}")
        if m.mode == "pool":
            match = [p for p in pools if p.get("name") == m.pool]
            if not match:
                return CheckResult.fail(f"pool {m.pool!r} not found")
            return self._pool(match[0])
        return self._pools(pools)

    @staticmethod
    def _pool_state(p: dict[str, Any]) -> tuple[Result, str]:
        name, status = p.get("name"), p.get("status")
        if not p.get("healthy", False):
            detail = p.get("status_detail") or status
            return Result.FAIL, f"{name} {status}: {detail}"
        if p.get("warning"):
            return Result.WARN, f"{name} {status} with warnings: {p.get('status_detail') or ''}"
        return Result.OK, f"{name} {status}"

    def _pool(self, p: dict[str, Any]) -> CheckResult:
        state, msg = self._pool_state(p)
        used = pct(p.get("allocated"), p.get("size"))
        if used is not None:
            msg += f", {used:g}% used ({(p.get('free') or 0) / 2**40:.2f} TiB free)"
        return CheckResult(state, msg, value=used, unit="%",
                           detail={"status": p.get("status"), "healthy": p.get("healthy")})

    def _pools(self, pools: list[dict[str, Any]]) -> CheckResult:
        if not pools:
            return CheckResult.fail("no pools returned")
        states = [self._pool_state(p) for p in pools]
        worst = max(states, key=lambda s: ["ok", "warn", "fail"].index(s[0].value))
        used = [u for u in (pct(p.get("allocated"), p.get("size")) for p in pools) if u is not None]
        bad = [msg for st, msg in states if st is not Result.OK]
        msg = f"{len(pools)} pools" + ("; " + "; ".join(bad) if bad else ", all healthy")
        if used:
            msg += f"; fullest {max(used):g}% used"
        return CheckResult(worst[0], msg, value=max(used) if used else None, unit="%")

    def _alerts(self, alerts: list[dict[str, Any]]) -> CheckResult:
        floor = ALERT_LEVELS.index(self.monitor.min_alert_level)
        active = [a for a in alerts or [] if not a.get("dismissed")
                  and a.get("level") in ALERT_LEVELS
                  and ALERT_LEVELS.index(a["level"]) >= floor]
        if not active:
            return CheckResult.ok(f"no active alerts at {self.monitor.min_alert_level} or above",
                                  value=0.0)
        worst = max(ALERT_LEVELS.index(a["level"]) for a in active)
        texts = [f"{a['level']}: {str(a.get('formatted') or a.get('klass'))[:100]}"
                 for a in active[:5]]
        res = CheckResult(Result.FAIL if worst >= ALERT_LEVELS.index("CRITICAL") else Result.WARN,
                          f"{len(active)} active alerts; " + " | ".join(texts),
                          value=float(len(active)), detail={"alerts": texts})
        return res


# =================================================================== Proxmox


class ProxmoxCheck(Check):
    async def resources(self) -> list[dict[str, Any]]:
        m = self.monitor
        cred = self.credential()
        verify: Any = api_ssl_context(m.verify_tls, m.ca_bundle)
        async with httpx.AsyncClient(verify=verify, timeout=self.timeout) as c:
            r = await c.get(f"https://{m.host}:{m.port}/api2/json/cluster/resources",
                            headers={"Authorization":
                                     f"PVEAPIToken={cred.token_id}={cred.secret}"})
        if r.status_code == 401:
            raise AuthFailed("token rejected (401)")
        r.raise_for_status()
        data = r.json().get("data")
        if not data:
            raise AuthFailed("token authenticated but sees no resources; grant PVEAuditor "
                             "(or disable privilege separation on the token)")
        return data

    async def probe(self) -> CheckResult:
        try:
            items = await self.resources()
        except AuthFailed as err:
            return CheckResult.fail(f"Proxmox: {err}")
        except httpx.HTTPError as err:
            if "CERTIFICATE_VERIFY_FAILED" in str(err):
                return CheckResult.fail(f"TLS certificate did not validate: {err}")
            return CheckResult.fail(f"Proxmox API: {type(err).__name__}: {err}")
        m = self.monitor
        if m.mode.startswith("node"):
            return self._node(items)
        if m.mode == "guest":
            return self._guest(items)
        return self._storage(items)

    def _node(self, items: list[dict[str, Any]]) -> CheckResult:
        m = self.monitor
        n = next((i for i in items if i.get("type") == "node" and i.get("node") == m.node), None)
        if n is None:
            return CheckResult.fail(f"node {m.node!r} not in cluster resources")
        if n.get("status") != "online":
            return CheckResult.fail(f"node {m.node} is {n.get('status')}")
        if m.mode == "node_cpu":
            v = round(float(n.get("cpu") or 0) * 100, 1)
            return CheckResult.ok(f"{m.node} CPU {v:g}% of {n.get('maxcpu')} cores",
                                  value=v, unit="%")
        if m.mode == "node_memory":
            v = pct(n.get("mem"), n.get("maxmem"))
            return CheckResult.ok(f"{m.node} RAM {v:g}% used", value=v, unit="%")
        days = (n.get("uptime") or 0) / 86400
        return CheckResult.ok(f"{m.node} online, up {days:.1f} days", value=round(days, 2),
                              unit=" days")

    def _guest(self, items: list[dict[str, Any]]) -> CheckResult:
        want = self.monitor.guest
        g = next((i for i in items if i.get("type") in ("qemu", "lxc")
                  and (i.get("name") == want or str(i.get("vmid")) == want)), None)
        if g is None:
            return CheckResult.fail(f"guest {want!r} not found")
        kind = "VM" if g["type"] == "qemu" else "container"
        label = f"{kind} {g.get('name')} ({g.get('vmid')}) on {g.get('node')}"
        detail = {"status": g.get("status"), "node": g.get("node"), "hastate": g.get("hastate")}
        if g.get("status") != "running":
            return CheckResult.fail(f"{label} is {g.get('status')}", detail=detail)
        if g.get("hastate") == "error":
            return CheckResult.fail(f"{label} HA state is error", detail=detail)
        v = round(float(g.get("cpu") or 0) * 100, 1)
        mem = pct(g.get("mem"), g.get("maxmem"))
        msg = f"{label} running, CPU {v:g}%" + (f", RAM {mem:g}%" if mem is not None else "")
        return CheckResult.ok(msg, value=v, unit="%", detail=detail)

    def _storage(self, items: list[dict[str, Any]]) -> CheckResult:
        m = self.monitor
        matches = [i for i in items if i.get("type") == "storage" and i.get("storage") == m.storage
                   and (m.node is None or i.get("node") == m.node)]
        if not matches:
            return CheckResult.fail(f"storage {m.storage!r} not found")
        s = matches[0]
        if s.get("status") != "available":
            return CheckResult.fail(f"storage {m.storage} on {s.get('node')} is {s.get('status')}")
        v = pct(s.get("disk"), s.get("maxdisk"))
        return CheckResult.ok(f"storage {m.storage} {v:g}% used", value=v, unit="%")


# =================================================================== vSphere

HOST_PATHS = ["name", "overallStatus", "runtime.connectionState", "runtime.inMaintenanceMode",
              "summary.quickStats.overallCpuUsage", "summary.hardware.cpuMhz",
              "summary.hardware.numCpuCores", "summary.quickStats.overallMemoryUsage",
              "summary.hardware.memorySize", "summary.quickStats.uptime"]
DS_PATHS = ["name", "summary.capacity", "summary.freeSpace", "summary.accessible",
            "overallStatus"]
VM_PATHS = ["name", "runtime.powerState", "guestHeartbeatStatus", "overallStatus",
            "summary.quickStats.overallCpuUsage", "summary.runtime.maxCpuUsage",
            "summary.quickStats.guestMemoryUsage", "summary.config.memorySizeMB",
            "config.template", "runtime.host"]


def vsphere_collect(host: str, port: int, user: str, pwd: str, ctx: ssl.SSLContext | None,
                    kinds: dict[str, list[str]]) -> dict[str, list[dict[str, Any]]]:
    """Blocking. Log in, read the requested properties for each managed
    object type, log out. Returns {kind: [ {path: value}, ... ]}."""
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim, vmodl

    types = {"host": vim.HostSystem, "datastore": vim.Datastore, "vm": vim.VirtualMachine}
    kw: dict[str, Any] = {"host": host, "port": port, "user": user, "pwd": pwd}
    if ctx is None:
        kw["disableSslCertValidation"] = True
    else:
        kw["sslContext"] = ctx
    try:
        si = SmartConnect(**kw)
    except vim.fault.InvalidLogin as err:
        raise AuthFailed(err.msg or "invalid login") from err
    try:
        content = si.RetrieveContent()
        pc_t = vmodl.query.PropertyCollector
        out: dict[str, list[dict[str, Any]]] = {}
        for kind, paths in kinds.items():
            view = content.viewManager.CreateContainerView(content.rootFolder,
                                                           [types[kind]], True)
            try:
                spec = pc_t.FilterSpec(
                    objectSet=[pc_t.ObjectSpec(obj=view, skip=True, selectSet=[
                        pc_t.TraversalSpec(name="v", path="view", skip=False,
                                           type=vim.view.ContainerView)])],
                    propSet=[pc_t.PropertySpec(type=types[kind], pathSet=paths)])
                rows: list[dict[str, Any]] = []
                res = content.propertyCollector.RetrievePropertiesEx([spec],
                                                                     pc_t.RetrieveOptions())
                while res is not None:
                    for obj in res.objects:
                        row = {p.name: p.val for p in obj.propSet}
                        if "runtime.host" in row and row["runtime.host"] is not None:
                            row["runtime.host"] = row["runtime.host"].name
                        rows.append(row)
                    res = (content.propertyCollector.ContinueRetrievePropertiesEx(res.token)
                           if res.token else None)
                out[kind] = rows
            finally:
                view.Destroy()
        return out
    finally:
        Disconnect(si)


class VSphereCheck(Check):
    async def collect(self, kinds: dict[str, list[str]]) -> dict[str, list[dict[str, Any]]]:
        m = self.monitor
        cred = self.credential()
        ctx = api_ssl_context(True, m.ca_bundle) if m.verify_tls else None
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(
            VSPHERE_POOL, vsphere_collect, m.host, m.port, cred.username, cred.password, ctx,
            kinds), self.timeout + 30)

    async def probe(self) -> CheckResult:
        m = self.monitor
        kind = {"datastore": "datastore", "vm": "vm"}.get(m.mode, "host")
        paths = {"host": HOST_PATHS, "datastore": DS_PATHS, "vm": VM_PATHS}[kind]
        try:
            rows = (await self.collect({kind: paths}))[kind]
        except AuthFailed as err:
            return CheckResult.fail(f"vSphere authentication failed: {err}")
        except ssl.SSLCertVerificationError as err:
            return CheckResult.fail(f"TLS certificate did not validate: {err.verify_message}")
        except asyncio.TimeoutError:
            return CheckResult.fail("vSphere API timed out")
        except Exception as err:  # noqa: BLE001 - pyVmomi raises many unrelated types
            return CheckResult.fail(f"vSphere API: {type(err).__name__}: {err}")
        if m.entity:
            rows = [r for r in rows if r.get("name") == m.entity]
        elif kind == "host" and len(rows) > 1:
            return CheckResult.fail(f"{len(rows)} hosts visible; set entity to choose one")
        if not rows:
            return CheckResult.fail(f"{kind} {m.entity!r} not found")
        return getattr(self, f"_{kind}")(rows[0])

    def _host(self, h: dict[str, Any]) -> CheckResult:
        m = self.monitor
        name = h.get("name")
        conn = str(h.get("runtime.connectionState"))
        if conn != "connected":
            return CheckResult.fail(f"host {name} is {conn}")
        if m.mode == "host_cpu":
            total = (h.get("summary.hardware.cpuMhz") or 0) * \
                (h.get("summary.hardware.numCpuCores") or 0)
            v = pct(h.get("summary.quickStats.overallCpuUsage"), total)
            return CheckResult.ok(f"{name} CPU {v:g}% of {total} MHz", value=v, unit="%")
        if m.mode == "host_memory":
            total_mb = (h.get("summary.hardware.memorySize") or 0) / 2**20
            v = pct(h.get("summary.quickStats.overallMemoryUsage"), total_mb)
            return CheckResult.ok(f"{name} RAM {v:g}% of {total_mb / 1024:.0f} GiB",
                                  value=v, unit="%")
        status = str(h.get("overallStatus"))
        days = (h.get("summary.quickStats.uptime") or 0) / 86400
        msg = f"host {name} connected, status {status}, up {days:.1f} days"
        if status == "red":
            return CheckResult.fail(msg + " (a red alarm is active on the host)")
        res = CheckResult.ok(msg, value=round(days, 2), unit=" days")
        if h.get("runtime.inMaintenanceMode"):
            res.result, res.message = Result.WARN, msg + ", in maintenance mode"
        elif status == "yellow":
            res.result = Result.WARN
        return res

    def _datastore(self, d: dict[str, Any]) -> CheckResult:
        name = d.get("name")
        if not d.get("summary.accessible"):
            return CheckResult.fail(f"datastore {name} is not accessible")
        cap, free = d.get("summary.capacity") or 0, d.get("summary.freeSpace") or 0
        v = pct(cap - free, cap)
        return CheckResult.ok(f"datastore {name} {v:g}% used, {free / 2**40:.2f} TiB free",
                              value=v, unit="%")

    def _vm(self, v: dict[str, Any]) -> CheckResult:
        name = v.get("name")
        power = str(v.get("runtime.powerState"))
        detail = {"power": power, "host": v.get("runtime.host"),
                  "heartbeat": str(v.get("guestHeartbeatStatus"))}
        if power != "poweredOn":
            return CheckResult.fail(f"VM {name} is {power}", detail=detail)
        hb = v.get("guestHeartbeatStatus")
        cpu = pct(v.get("summary.quickStats.overallCpuUsage"),
                  v.get("summary.runtime.maxCpuUsage"))
        msg = f"VM {name} powered on" + (f", CPU {cpu:g}%" if cpu is not None else "")
        hb_s = str(hb) if hb is not None else None
        if hb_s == "red":
            return CheckResult.fail(msg + ", guest heartbeat red (guest OS unresponsive)",
                                    detail=detail)
        res = CheckResult.ok(msg, value=cpu, unit="%", detail=detail)
        if hb_s == "yellow":
            res.result, res.message = Result.WARN, msg + ", guest heartbeat intermittent"
        elif hb_s in (None, "gray"):
            res.message += " (no guest heartbeat: VMware Tools not running)"
        return res
