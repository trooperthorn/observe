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
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..httpclient import http_client
from ..portkey import mac_digits
from . import ha_host
from .base import Check, CheckResult, Result
from .platforms import AuthFailed, http_api_context, pct


MAX_API_BODY = 4_000_000  # per response, the same cap the UniFi plugin client uses
MAX_UNIFI_PAGES = 50  # a list longer than 200 * MAX_UNIFI_PAGES rows is refused, not truncated


class BodyTooLarge(ValueError):
    """A reply was larger than the byte cap, or a list ran past the page cap."""


async def read_json_capped(c: httpx.AsyncClient, url: str, headers: dict[str, str],
                           params: dict[str, Any] | None, limit: int, what: str) -> Any:
    """GET one JSON document. Redirects are refused, because one would carry the credential
    header elsewhere, and the body is refused once it grows past `limit` bytes."""
    async with c.stream("GET", url, headers=headers, params=params,
                        follow_redirects=False) as r:
        if r.status_code in (401, 403):
            raise AuthFailed(f"HTTP {r.status_code}")
        if r.status_code == 404:
            raise LookupError(f"{what} not found (404)")
        if 300 <= r.status_code < 400:
            raise LookupError(f"{what} answered a redirect (HTTP {r.status_code}); "
                              "redirects are not followed")
        r.raise_for_status()
        buf = bytearray()
        async for chunk in r.aiter_bytes():
            buf += chunk
            if len(buf) > limit:
                raise BodyTooLarge(f"{what} response is larger than {limit} bytes")
    return json.loads(bytes(buf))


class _HttpApiCheck(Check):
    scheme_field = "https"

    def body_limit(self) -> int:
        return MAX_API_BODY

    def base_url(self) -> str:
        m = self.monitor
        https = getattr(m, "https", True)
        return f"{'https' if https else 'http'}://{m.host}:{m.port}"

    def headers(self) -> dict[str, str]:  # pragma: no cover - overridden
        return {}

    async def get(self, path: str, params: dict[str, Any] | None = None,
                  limit: int | None = None) -> Any:
        m = self.monitor
        verify: Any = http_api_context(m.verify_tls, m.ca_bundle)
        async with http_client(verify, self.timeout) as c:
            return await read_json_capped(c, self.base_url() + path, self.headers(), params,
                                          self.body_limit() if limit is None else limit, path)

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


MAX_HA_BODY = 16 * 1024 * 1024  # a large install lists about 4 MB of states


class HomeAssistantCheck(_HttpApiCheck):
    def __init__(self, monitor: Any, config: Any, store: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        super().__init__(monitor, config)
        self.store = store
        self.clock = clock

    def body_limit(self) -> int:
        return MAX_HA_BODY

    async def _host(self) -> CheckResult:
        if self.store is None:
            return CheckResult.fail("host mode needs the Observe store")
        cfg = await self.get("/api/config")
        states = await self.get("/api/states")
        if not isinstance(cfg, dict) or not isinstance(states, list):
            return CheckResult.fail("unexpected /api/config or /api/states shape")
        now = self.clock()
        batch = ha_host.build_batch(self.monitor.host_name, cfg, states, now)
        await self.store.ingest_batch(batch, {}, now=now, critical=True)
        return CheckResult.ok(
            f"{self.monitor.host_name}: {len(states)} entities, {len(batch.samples)} readings",
            value=float(len(states)), unit="{entity}", detail={"samples": len(batch.samples)})

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential().token}"}

    async def _probe(self) -> CheckResult:
        m = self.monitor
        if m.mode == "host":
            return await self._host()
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
                             if s["entity_id"].startswith("update.")
                             and ha_host.update_is_pending(s))
            res = CheckResult.ok(f"{len(pending)} updates pending", value=float(len(pending)),
                                 unit="{update}", detail={"pending": pending})
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
                             unit="{entity unavailable}", detail={"unavailable": bad})
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


async def unifi_list_all(client: httpx.AsyncClient, url: str, headers: dict[str, str],
                         limit: int | None = None) -> list[dict[str, Any]]:
    """Read every page of a UniFi Integration API list endpoint. Stops on an
    empty page or when totalCount is reached, whichever comes first, so a
    server that caps `limit` below what was asked is still read fully. Each
    page is capped in bytes and the list in pages; past either it is refused."""
    out: list[dict[str, Any]] = []
    offset = 0
    for _ in range(MAX_UNIFI_PAGES):
        body = await read_json_capped(client, url, headers, {"offset": offset, "limit": 200},
                                      MAX_API_BODY if limit is None else limit, url)
        if isinstance(body, list):
            return body
        page = body.get("data") or []
        out.extend(page)
        total = body.get("totalCount")
        if not page or total is None or len(out) >= int(total):
            return out
        offset += len(page)
    raise BodyTooLarge(f"{url} has more than {MAX_UNIFI_PAGES} pages; refusing to read further")


UNIFI_DETAIL_MAX_BYTES = 1_000_000  # a device detail body larger than this is refused


class _UniFiCheck(_HttpApiCheck):
    def headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.credential().api_key, "Accept": "application/json"}

    async def list_all(self, path: str) -> list[dict[str, Any]]:
        m = self.monitor
        verify: Any = http_api_context(m.verify_tls, m.ca_bundle)
        async with http_client(verify, self.timeout) as c:
            return await unifi_list_all(c, self.base_url() + m.base_path + path, self.headers())

    async def get_capped(self, path: str, limit: int | None = None) -> Any:
        """GET one JSON object under the smaller device detail cap."""
        return await self.get(path, limit=UNIFI_DETAIL_MAX_BYTES if limit is None else limit)

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
            # The state of each device, so a map node linked to this aggregate monitor shows its
            # own device's state, not the monitor's (observe/infra_map.py).
            each = [{"name": d.get("name"), "mac": mac_digits(str(d.get("macAddress") or "")),
                     "state": d.get("state")} for d in considered]
            res = CheckResult.ok(msg, value=float(len(down)), unit="{device offline}",
                                 detail={"not_online": down, "devices": each})
            if down and m.thresholds is None:
                res.result = Result.FAIL
            return res
        if m.mode == "firmware":
            upd = sorted(d.get("name") for d in devices
                         if d.get("firmwareUpdatable") and d.get("name") not in m.ignore)
            res = CheckResult.ok(f"{len(upd)} devices have firmware updates"
                                 + (": " + ", ".join(upd) if upd else ""), value=float(len(upd)),
                                 unit="{firmware update}")
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
        if m.mode == "ports":
            return await self._ports(sid, dev, label)
        stats = await self.get(f"{m.base_path}/sites/{sid}/devices/{dev['id']}/statistics/latest")
        key = "cpuUtilizationPct" if m.mode == "device_cpu" else "memoryUtilizationPct"
        v = stats.get(key)
        if v is None:
            return CheckResult.fail(f"{label} reports no {key}")
        what = "CPU" if m.mode == "device_cpu" else "memory"
        days = (stats.get("uptimeSec") or 0) / 86400
        return CheckResult.ok(f"{label} {what} {float(v):g}%, up {days:.1f} days",
                              value=round(float(v), 1), unit="%")


    async def _ports(self, sid: str, dev: dict[str, Any], label: str) -> CheckResult:
        """Per-port state from GET /devices/{id}. The port list is read from
        interfaces.ports with idx, state, speedMbps, maxSpeedMbps and poe; this
        shape is unverified against a live console. VLAN and PoE watts are not
        in this API, so they stay None (unknown, never zero)."""
        m = self.monitor
        body = await self.get_capped(f"{m.base_path}/sites/{sid}/devices/{dev['id']}")
        if not isinstance(body, dict):
            raise ValueError("device detail is not an object")
        ifaces = body.get("interfaces")
        raw = ifaces.get("ports") if isinstance(ifaces, dict) else None
        ports: dict[str, dict[str, Any]] = {}
        for p in raw if isinstance(raw, list) else []:
            if not isinstance(p, dict) or not isinstance(p.get("idx"), int)                     or isinstance(p.get("idx"), bool):
                continue
            ports[str(p["idx"])] = {
                "speed_mbps": p.get("speedMbps"), "max_speed_mbps": p.get("maxSpeedMbps"),
                "state": p.get("state"), "poe": p.get("poe"), "vlan": None, "poe_w": None}
        up = sum(1 for v in ports.values() if v["state"] == "UP")
        return CheckResult.ok(f"{label} {up}/{len(ports)} ports up", value=float(up),
                              unit="{port up}", detail={"ports": ports})


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
            res = CheckResult.ok(msg, value=float(len(down)), unit="{camera disconnected}",
                                 detail={"not_connected": down})
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
                                  f"{r.get('updateVersion', '?')}", value=1.0, unit="{update}")
                if m.thresholds is not None:
                    res.result = Result.OK
                return res
            return CheckResult.ok(f"version {cur} is current", value=0.0, unit="{update}")
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
