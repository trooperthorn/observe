"""Application APIs: Home Assistant, UniFi Network, UniFi Protect, Technitium.

Home Assistant: REST at /api/ with `Authorization: Bearer <long-lived token>`.
GET /api/ answers {"message": "API running."}; GET /api/states lists every
entity with entity_id, state, attributes, last_changed.

UniFi Network and Protect: the Integration APIs on the console, behind
/proxy/network/integration/v1 and /proxy/protect/integration/v1, with an
`X-API-KEY` header. One key serves both. Network list endpoints return an
envelope (offset, limit, count, totalCount, data) and are paged here; Protect
list endpoints are read whether they return an envelope or a bare array.
Protect's Integration API reports connection state (CONNECTED, CONNECTING,
DISCONNECTED) but not recording state, so "camera connected" is the
strongest claim these checks make about a camera.

Technitium DNS: /api/dashboard/stats/get and /api/user/checkForUpdate. Every
response carries "status"; anything but "ok" is a failure with its message.
"""

from __future__ import annotations

import fnmatch
from typing import Any

import httpx

from .base import Check, CheckResult, Result
from .platforms import AuthFailed, api_ssl_context, pct


class _HttpApiCheck(Check):
    scheme_field = "https"

    def base_url(self) -> str:
        m = self.monitor
        https = getattr(m, "https", True)
        return f"{'https' if https else 'http'}://{m.host}:{m.port}"

    def headers(self) -> dict[str, str]:  # pragma: no cover - overridden
        return {}

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        m = self.monitor
        verify: Any = api_ssl_context(m.verify_tls, m.ca_bundle)
        async with httpx.AsyncClient(verify=verify, timeout=self.timeout) as c:
            r = await c.get(self.base_url() + path, headers=self.headers(), params=params)
        if r.status_code in (401, 403):
            raise AuthFailed(f"HTTP {r.status_code}")
        if r.status_code == 404:
            raise LookupError(f"{path} not found (404)")
        r.raise_for_status()
        return r.json()

    async def probe(self) -> CheckResult:
        try:
            return await self._probe()
        except AuthFailed as err:
            return CheckResult.fail(f"authentication rejected ({err})")
        except LookupError as err:
            return CheckResult.fail(str(err))
        except httpx.HTTPError as err:
            if "CERTIFICATE_VERIFY_FAILED" in str(err):
                return CheckResult.fail(f"TLS certificate did not validate: {err}")
            return CheckResult.fail(f"{type(err).__name__}: {err}")
        except ValueError as err:  # JSON decode
            return CheckResult.fail(f"unexpected response: {err}")

    async def _probe(self) -> CheckResult:  # pragma: no cover - abstract
        raise NotImplementedError


# ============================================================ Home Assistant


class HomeAssistantCheck(_HttpApiCheck):
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential().token}"}

    async def _probe(self) -> CheckResult:
        m = self.monitor
        if m.mode == "api":
            body = await self.get("/api/")
            if body.get("message") != "API running.":
                return CheckResult.fail(f"unexpected /api/ reply {body!r:.80}")
            return CheckResult.ok("API running")
        if m.mode == "entity":
            s = await self.get(f"/api/states/{m.entity_id}")
            return self._entity(s)
        states = await self.get("/api/states")
        if m.mode == "updates":
            pending = sorted(s["entity_id"] for s in states
                             if s["entity_id"].startswith("update.") and s.get("state") == "on")
            res = CheckResult.ok(f"{len(pending)} updates pending", value=float(len(pending)),
                                 detail={"pending": pending})
            if pending:
                res.message += ": " + ", ".join(pending[:5])
                if m.thresholds is None:
                    res.result = Result.WARN
            return res
        bad = []
        for s in states:
            eid = s["entity_id"]
            if s.get("state") != "unavailable":
                continue
            if m.domains and eid.split(".", 1)[0] not in m.domains:
                continue
            if any(fnmatch.fnmatch(eid, pat) for pat in m.ignore):
                continue
            bad.append(eid)
        bad.sort()
        res = CheckResult.ok(f"{len(bad)} unavailable entities", value=float(len(bad)),
                             detail={"unavailable": bad})
        if bad:
            res.message += ": " + ", ".join(bad[:5]) + (" ..." if len(bad) > 5 else "")
        return res

    def _entity(self, s: dict[str, Any]) -> CheckResult:
        m = self.monitor
        state = str(s.get("state"))
        name = (s.get("attributes") or {}).get("friendly_name") or m.entity_id
        unit = (s.get("attributes") or {}).get("unit_of_measurement") or ""
        if state in ("unavailable", "unknown"):
            return CheckResult.fail(f"{name} is {state}")
        if m.expect is not None and state not in m.expect:
            return CheckResult.fail(f"{name} is {state!r}, expected one of {m.expect}")
        try:
            value: float | None = float(state)
        except ValueError:
            value = None
        return CheckResult.ok(f"{name} = {state}{(' ' + unit) if unit and value is not None else ''}",
                              value=value, unit=f" {unit}" if unit else "",
                              detail={"last_changed": s.get("last_changed")})


# ============================================================ UniFi


async def unifi_list_all(client: httpx.AsyncClient, url: str,
                         headers: dict[str, str]) -> list[dict[str, Any]]:
    """Read every page of a UniFi Integration API list endpoint. Stops on an
    empty page or when totalCount is reached, whichever comes first, so a
    server that caps `limit` below what was asked is still read fully."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = await client.get(url, headers=headers, params={"offset": offset, "limit": 200})
        if r.status_code in (401, 403):
            raise AuthFailed(f"HTTP {r.status_code}")
        if r.status_code == 404:
            raise LookupError(f"{url} not found (404)")
        r.raise_for_status()
        body = r.json()
        if isinstance(body, list):
            return body
        page = body.get("data") or []
        out.extend(page)
        total = body.get("totalCount")
        if not page or total is None or len(out) >= int(total):
            return out
        offset += len(page)


class _UniFiCheck(_HttpApiCheck):
    def headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.credential().api_key, "Accept": "application/json"}

    async def list_all(self, path: str) -> list[dict[str, Any]]:
        m = self.monitor
        verify: Any = api_ssl_context(m.verify_tls, m.ca_bundle)
        async with httpx.AsyncClient(verify=verify, timeout=self.timeout) as c:
            return await unifi_list_all(c, self.base_url() + m.base_path + path, self.headers())

    @staticmethod
    def matches(item: dict[str, Any], want: str) -> bool:
        w = want.lower().replace("-", ":")
        return item.get("name") == want or str(item.get("macAddress") or item.get("mac") or "") \
            .lower().replace("-", ":") == w


class UniFiNetworkCheck(_UniFiCheck):
    async def site_id(self) -> str:
        sites = await self.list_all("/sites")
        m = self.monitor
        if m.site:
            match = [s for s in sites if s.get("name") == m.site]
            if not match:
                raise LookupError(f"site {m.site!r} not found; sites: "
                                  f"{[s.get('name') for s in sites]}")
            return str(match[0]["id"])
        if len(sites) != 1:
            raise LookupError(f"{len(sites)} sites; set site to one of "
                              f"{[s.get('name') for s in sites]}")
        return str(sites[0]["id"])

    async def _probe(self) -> CheckResult:
        m = self.monitor
        sid = await self.site_id()
        devices = await self.list_all(f"/sites/{sid}/devices")
        if m.mode == "devices":
            considered = [d for d in devices if d.get("name") not in m.ignore]
            down = sorted(d.get("name") or d.get("macAddress") for d in considered
                          if d.get("state") != "ONLINE")
            msg = f"{len(considered) - len(down)}/{len(considered)} devices online"
            if down:
                msg += "; not online: " + ", ".join(down)
            res = CheckResult.ok(msg, value=float(len(down)), detail={"not_online": down})
            if down and m.thresholds is None:
                res.result = Result.FAIL
            return res
        if m.mode == "firmware":
            upd = sorted(d.get("name") for d in devices
                         if d.get("firmwareUpdatable") and d.get("name") not in m.ignore)
            res = CheckResult.ok(f"{len(upd)} devices have firmware updates"
                                 + (": " + ", ".join(upd) if upd else ""), value=float(len(upd)))
            if upd and m.thresholds is None:
                res.result = Result.WARN
            return res
        dev = next((d for d in devices if self.matches(d, m.device)), None)
        if dev is None:
            return CheckResult.fail(f"device {m.device!r} not found")
        label = f"{dev.get('name')} ({dev.get('model')})"
        if dev.get("state") != "ONLINE":
            return CheckResult.fail(f"{label} is {dev.get('state')}")
        if m.mode == "device":
            return CheckResult.ok(f"{label} online, firmware {dev.get('firmwareVersion')}"
                                  + (" (update available)" if dev.get("firmwareUpdatable") else ""),
                                  detail={"ip": dev.get("ipAddress")})
        stats = await self.get(f"{m.base_path}/sites/{sid}/devices/{dev['id']}/statistics/latest")
        key = "cpuUtilizationPct" if m.mode == "device_cpu" else "memoryUtilizationPct"
        v = stats.get(key)
        if v is None:
            return CheckResult.fail(f"{label} reports no {key}")
        what = "CPU" if m.mode == "device_cpu" else "memory"
        days = (stats.get("uptimeSec") or 0) / 86400
        return CheckResult.ok(f"{label} {what} {float(v):g}%, up {days:.1f} days",
                              value=round(float(v), 1), unit="%")


class UniFiProtectCheck(_UniFiCheck):
    async def _probe(self) -> CheckResult:
        m = self.monitor
        if m.mode == "info":
            info = await self.get(m.base_path + "/meta/info")
            return CheckResult.ok(f"Protect {info.get('applicationVersion', '?')} responding")
        cams = await self.list_all("/cameras")
        if m.mode == "cameras":
            considered = [c for c in cams if c.get("name") not in m.ignore]
            down = sorted(f"{c.get('name')} ({c.get('state')})" for c in considered
                          if c.get("state") != "CONNECTED")
            msg = f"{len(considered) - len(down)}/{len(considered)} cameras connected"
            if down:
                msg += "; " + ", ".join(down)
            res = CheckResult.ok(msg, value=float(len(down)), detail={"not_connected": down})
            if down and m.thresholds is None:
                res.result = Result.FAIL
            return res
        cam = next((c for c in cams if self.matches(c, m.camera)), None)
        if cam is None:
            return CheckResult.fail(f"camera {m.camera!r} not found")
        state = cam.get("state")
        label = f"{cam.get('name')} ({cam.get('modelKey', 'camera')})"
        if state == "CONNECTED":
            return CheckResult.ok(f"{label} connected")
        if state == "CONNECTING":
            return CheckResult(Result.WARN, f"{label} connecting")
        return CheckResult.fail(f"{label} is {state}")


# ============================================================ Technitium


class TechnitiumCheck(_HttpApiCheck):
    def headers(self) -> dict[str, str]:
        if self.monitor.token_in_query:
            return {}
        return {"Authorization": f"Bearer {self.credential().token}"}

    async def call(self, path: str, **params: Any) -> dict[str, Any]:
        if self.monitor.token_in_query:
            params["token"] = self.credential().token
        body = await self.get(path, params=params)
        status = body.get("status")
        if status == "invalid-token":
            raise AuthFailed("invalid-token")
        if status != "ok":
            raise LookupError(f"Technitium returned status {status!r}: "
                              f"{body.get('errorMessage', '')}".strip())
        return body.get("response") or {}

    async def _probe(self) -> CheckResult:
        m = self.monitor
        if m.mode == "update":
            r = await self.call("/api/user/checkForUpdate")
            cur = r.get("currentVersion", "?")
            if r.get("updateAvailable"):
                res = CheckResult(Result.WARN, f"update available: {cur} -> "
                                  f"{r.get('updateVersion', '?')}", value=1.0)
                if m.thresholds is not None:
                    res.result = Result.OK
                return res
            return CheckResult.ok(f"version {cur} is current", value=0.0)
        r = await self.call("/api/dashboard/stats/get", type=m.range, utc="true")
        st = r.get("stats") or {}
        total = int(st.get("totalQueries") or 0)
        servfail = int(st.get("totalServerFailure") or 0)
        rate = pct(servfail, total) if total else 0.0
        label = "last hour" if m.range == "LastHour" else "last day"
        return CheckResult.ok(
            f"{total} queries {label}, {rate:g}% SERVFAIL, {st.get('totalBlocked', 0)} blocked, "
            f"{st.get('totalClients', 0)} clients",
            value=rate, unit="%",
            detail={k: st.get(k) for k in ("totalQueries", "totalNoError", "totalServerFailure",
                                           "totalNxDomain", "totalRefused", "totalBlocked",
                                           "totalClients", "cachedEntries")})
