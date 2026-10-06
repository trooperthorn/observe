"""An optional read-only client for the classic UniFi Network controller API.

The Integration API does not give PoE watts, per-port VLAN, LLDP neighbours, uplink port numbers,
WAN monitors or offline clients, so this client logs in to the UniFi OS console with a dedicated
local view-only account. The rules:

- The only requests ever sent are POST /api/auth/login, POST /api/auth/logout and GETs under
  /proxy/network/api/s/{site}/ for stat/device, stat/sta, rest/user and stat/health.
- The session cookie and the X-CSRF-Token live in this object's memory only. They are never
  written to disk, logged, put in a row, or put in an error message; neither is the password.
- A 401 on a GET triggers one fresh login and one retry. If that fails too, or the login itself is
  rejected, the client backs off (a pause that doubles up to 30 minutes) and sends nothing until
  the pause is over. A login answered 429, a 5xx or anything else but 200 backs off the same way.
  A 403 on a read after a good login is a permission problem, so it is not retried with a fresh
  login; it backs off and raises ClassicForbidden. The failure count resets only when a read
  succeeds, so a login that works but whose reads are refused keeps growing its pause.
- Redirects are never followed and each response is capped at MAX_BODY_BYTES.

Field names under the classic API (port_table, poe_power, lldp_table, uplink and so on) are NOT
verified against a live console: ha_Int_soc does not read this API, so they follow the widely seen
UniFi OS shapes. Each parser marks the unverified fields in a comment and drops a malformed row.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from .client import AuthRejected, UniFiError, ssl_context

MAX_BODY_BYTES = 8_000_000
MAX_BACKOFF_S = 1800.0
READ_PATHS = ("stat/device", "stat/sta", "rest/user", "stat/health")
SITE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LOGIN_PATH = "/api/auth/login"
LOGOUT_PATH = "/api/auth/logout"


class ClassicBackedOff(UniFiError):
    """The client is pausing after a rejected login and sent no request."""


class ClassicForbidden(AuthRejected):
    """The console accepted the login but refused a read with 403: the account lacks permission."""


class ClassicClient:
    def __init__(self, base_url: str, username: str, password: str, site: str, verify: bool,
                 ca_bundle: str | None, timeout: float,
                 transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] | None = None,
                 base_pause: float = 120.0) -> None:
        if not SITE_RE.match(site):
            raise UniFiError("the classic site name has characters that are not allowed")
        self.base_url = base_url.rstrip("/")
        self.site = site
        self._user = username
        self._password = password
        self._timeout = timeout
        self._transport = transport
        self._verify = (verify, ca_bundle)
        self._clock = clock or time.monotonic
        self._base_pause = base_pause
        self._cookie: str | None = None  # "TOKEN=..." in memory only
        self._csrf: str | None = None
        self._failures = 0
        self._retry_at = 0.0

    def __repr__(self) -> str:  # never show the secrets, even in a traceback
        return f"ClassicClient(site={self.site!r}, logged_in={self._cookie is not None})"

    def _session(self) -> httpx.AsyncClient:
        if self._transport is not None:
            return httpx.AsyncClient(transport=self._transport, timeout=self._timeout,
                                     follow_redirects=False)
        return httpx.AsyncClient(verify=ssl_context(*self._verify), timeout=self._timeout,
                                 follow_redirects=False)

    def _back_off(self) -> None:
        self._failures += 1
        pause = min(self._base_pause * 2 ** (self._failures - 1), MAX_BACKOFF_S)
        self._retry_at = self._clock() + pause

    def _check_pause(self) -> None:
        now = self._clock()
        if now < self._retry_at:
            raise ClassicBackedOff(f"pausing after a rejected login, retry in "
                                   f"{self._retry_at - now:.0f}s")

    async def login(self) -> None:
        self._check_pause()
        self._cookie = None
        self._csrf = None
        body = {"username": self._user, "password": self._password, "remember": False}
        async with self._session() as c:
            r = await c.post(self.base_url + LOGIN_PATH, json=body,
                             headers={"Accept": "application/json"})
        if r.status_code in (400, 401, 403):
            self._back_off()
            raise AuthRejected(f"classic login rejected (HTTP {r.status_code})")
        if 300 <= r.status_code < 400:
            raise UniFiError(f"login answered a redirect (HTTP {r.status_code}); "
                             "redirects are not followed")
        if r.status_code != 200:  # 429, 5xx and the rest: pause instead of retrying every poll
            self._back_off()
            raise UniFiError(f"classic login failed (HTTP {r.status_code})")
        token = r.cookies.get("TOKEN")
        csrf = r.headers.get("x-csrf-token")
        if not token or not csrf:
            self._back_off()
            raise UniFiError("classic login answered without a session cookie and CSRF token")
        self._cookie = f"TOKEN={token}"
        self._csrf = csrf  # the failure count resets only when a read succeeds

    async def logout(self) -> None:
        """Best effort; the session is forgotten even if the console cannot be reached."""
        cookie, csrf = self._cookie, self._csrf
        self._cookie = self._csrf = None
        if cookie is None:
            return
        try:
            async with self._session() as c:
                await c.post(self.base_url + LOGOUT_PATH,
                             headers={"Cookie": cookie, "X-CSRF-Token": csrf or ""})
        except httpx.HTTPError:
            pass

    async def _get_once(self, path: str) -> Any:
        headers = {"Cookie": self._cookie or "", "X-CSRF-Token": self._csrf or "",
                   "Accept": "application/json"}
        url = f"{self.base_url}/proxy/network/api/s/{self.site}/{path}"
        async with self._session() as c:
            async with c.stream("GET", url, headers=headers) as r:
                if r.status_code == 403:
                    raise ClassicForbidden("HTTP 403: the classic account may not read this")
                if r.status_code == 401:
                    raise AuthRejected("HTTP 401")
                if 300 <= r.status_code < 400:
                    raise UniFiError(f"{path} answered a redirect (HTTP {r.status_code}); "
                                     "redirects are not followed")
                if r.status_code == 404:
                    raise UniFiError(f"{path} not found (404)")
                r.raise_for_status()
                new = r.headers.get("x-updated-csrf-token")
                if new:
                    self._csrf = new
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BODY_BYTES:
                        raise UniFiError(f"{path} response is larger than {MAX_BODY_BYTES} bytes")
        try:
            doc = json.loads(bytes(body))
        except ValueError as err:
            raise UniFiError(f"{path} did not return JSON") from err
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, list):
            raise UniFiError(f"{path} did not return a data list")
        return data

    async def get(self, path: str) -> list[Any]:
        """GET one of READ_PATHS. Logs in on first use; on a 401 logs in again once and retries."""
        if path not in READ_PATHS:
            raise UniFiError("that classic path is not allowed")
        self._check_pause()
        if self._cookie is None:
            await self.login()
        try:
            data = await self._get_once(path)
        except ClassicForbidden:
            self._back_off()  # a new login cannot fix a missing permission
            raise
        except AuthRejected:
            data = None
        if data is None:
            await self.login()  # one re-login; a rejection here backs off and raises
            try:
                data = await self._get_once(path)
            except AuthRejected:
                self._cookie = self._csrf = None
                self._back_off()
                raise
        self._failures = 0
        self._retry_at = 0.0
        return data


# ---- parsers. Every field name below is unverified against a live console. ----

def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _str(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def parse_ports(device: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`port_table` of a stat/device row, keyed by the string port_idx. poe_power is a string of
    watts on the classic API; poe_class, speed (link speed in Mbit/s) and the VLAN fields
    (native_vlan, native_networkconf_id, tagged_vlan_mgmt, excluded_networkconf_ids) are
    unverified."""
    out: dict[str, dict[str, Any]] = {}
    table = device.get("port_table")
    for p in table if isinstance(table, list) else []:
        if not isinstance(p, dict) or _int(p.get("port_idx")) is None:
            continue
        excluded = p.get("excluded_networkconf_ids")
        out[str(p["port_idx"])] = {
            "name": _str(p.get("name")),
            "up": p.get("up") if isinstance(p.get("up"), bool) else None,
            # A port that is down has no current link speed; the console may keep the last one.
            "speed_mbps": _int(p.get("speed")) if p.get("up") is True else None,
            "poe_w": _num(p.get("poe_power")),
            "poe_class": _str(p.get("poe_class")) or None,
            "poe_enabled": p.get("poe_enable") if isinstance(p.get("poe_enable"), bool) else None,
            "native_vlan": _int(p.get("native_vlan")),
            "native_network_id": _str(p.get("native_networkconf_id")) or None,
            "tagged_mode": _str(p.get("tagged_vlan_mgmt")) or None,
            "excluded_network_ids": [x for x in excluded if isinstance(x, str)]
            if isinstance(excluded, list) else [],
        }
    return out


def parse_lldp(device: dict[str, Any]) -> list[dict[str, Any]]:
    """`lldp_table` of a stat/device row. chassis_id, port_id, chassis_name and local_port_idx are
    unverified."""
    out: list[dict[str, Any]] = []
    table = device.get("lldp_table")
    for n in table if isinstance(table, list) else []:
        if not isinstance(n, dict) or _int(n.get("local_port_idx")) is None:
            continue
        out.append({"local_port": n["local_port_idx"],
                    "chassis_id": _str(n.get("chassis_id")).lower(),
                    "port_id": _str(n.get("port_id")), "name": _str(n.get("chassis_name"))})
    return out


def parse_uplink(device: dict[str, Any]) -> dict[str, Any] | None:
    """`uplink` of a stat/device row: the uplink MAC and the local and remote port numbers.
    uplink_remote_port and port_idx are unverified."""
    up = device.get("uplink")
    if not isinstance(up, dict):
        return None
    return {"mac": _str(up.get("uplink_mac")).lower(), "local_port": _int(up.get("port_idx")),
            "remote_port": _int(up.get("uplink_remote_port"))}


def parse_devices(rows: list[Any]) -> list[dict[str, Any]]:
    out = []
    for d in rows:
        if not isinstance(d, dict) or not _str(d.get("mac")):
            continue
        out.append({"mac": d["mac"].lower(), "name": _str(d.get("name")),
                    "ports": parse_ports(d), "lldp": parse_lldp(d), "uplink": parse_uplink(d)})
    return out


def parse_wan_health(rows: list[Any]) -> dict[str, Any] | None:
    """The `wan` subsystem row of stat/health. status, wan_ip, latency, uptime and gw_name are
    unverified."""
    for r in rows:
        if isinstance(r, dict) and r.get("subsystem") == "wan":
            gws = r.get("gw_name")
            return {"status": _str(r.get("status")), "wan_ip": _str(r.get("wan_ip")),
                    "latency_ms": _num(r.get("latency")), "uptime_s": _num(r.get("uptime")),
                    "gateways": [g for g in gws if isinstance(g, str)]
                    if isinstance(gws, list) else []}
    return None


def parse_offline_clients(known: list[Any], active: list[Any]) -> list[dict[str, Any]]:
    """Known clients (rest/user) whose MAC is not in the active list (stat/sta)."""
    live = {_str(c.get("mac")).lower() for c in active if isinstance(c, dict)}
    out = []
    for u in known:
        if not isinstance(u, dict):
            continue
        mac = _str(u.get("mac")).lower()
        if mac and mac not in live:
            out.append({"mac": mac, "name": _str(u.get("name")) or _str(u.get("hostname")),
                        "last_seen": _num(u.get("last_seen")),
                        "first_seen": _num(u.get("first_seen"))})
    return out
