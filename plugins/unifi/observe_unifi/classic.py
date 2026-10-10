"""An optional read-only client for the classic UniFi Network controller API.

The Integration API does not give PoE watts, per-port VLAN, LLDP neighbours, uplink port numbers,
WAN monitors or offline clients, so this client logs in to the UniFi OS console with a dedicated
local view-only account. The rules:

- The only requests ever sent are POST /api/auth/login, POST /api/auth/logout and GETs under
  /proxy/network/api/s/{site}/ for stat/device, stat/sta, rest/user, stat/health and
  rest/wlanconf.
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
from .records import GATEWAY_TOKENS

MAX_BODY_BYTES = 8_000_000
MAX_BACKOFF_S = 1800.0
READ_PATHS = ("stat/device", "stat/sta", "rest/user", "stat/health", "rest/wlanconf")
# At or above this a classic `uptime` is an epoch moment, not a duration (the core unifi rule).
EPOCH_THRESHOLD = 1_000_000_000
# aiounifi device states that mean the device is not reachable now (ha_Int_soc unifi_core).
OFFLINE_DEVICE_STATES = frozenset({"DISCONNECTED", "HEARTBEAT_MISSED", "ISOLATED", "OFFLINE"})
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


def _first(obj: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = obj.get(k)
        if v not in (None, ""):
            return v
    return None


def parse_networks(device: dict[str, Any]) -> list[dict[str, Any]]:
    """`network_table` of the gateway's stat/device row: id, name, VLAN and subnet of each
    network, for VLAN resolution (resolve_client_vlan). _id, name, vlan and ip_subnet are
    unverified; ha_Int_soc reads the same keys from aiounifi's raw device."""
    out: list[dict[str, Any]] = []
    table = device.get("network_table")
    for n in table if isinstance(table, list) else []:
        if not isinstance(n, dict) or not _str(n.get("name")):
            continue
        out.append({"id": _str(n.get("_id")), "name": n["name"], "vlan": _int(n.get("vlan")),
                    "ip_subnet": _str(n.get("ip_subnet"))})
    return out


def parse_wan_uplink(device: dict[str, Any]) -> dict[str, Any]:
    """The WAN side of a gateway's `uplink` on stat/device: address, state, interface name and
    the rates. ha_Int_soc's unifi_core reads `rx_bytes_r`; the classic console most likely sends
    the hyphenated `rx_bytes-r`, so both spellings are accepted (UNVERIFIED either way). Rates
    are bytes per second as the console counts them. `internet` is the device-level boolean the
    same module reads, when it is a boolean."""
    up = device.get("uplink")
    up = up if isinstance(up, dict) else {}
    state = up.get("up")
    internet = device.get("internet")
    return {"ip": _str(up.get("ip")), "up": state if isinstance(state, bool) else None,
            "port": _str(up.get("name")),
            "rx_rate_bps": _num(_first(up, "rx_bytes-r", "rx_bytes_r")),
            "tx_rate_bps": _num(_first(up, "tx_bytes-r", "tx_bytes_r")),
            "rx_bytes": _int(up.get("rx_bytes")), "tx_bytes": _int(up.get("tx_bytes")),
            "internet": internet if isinstance(internet, bool) else None}


def parse_devices(rows: list[Any]) -> list[dict[str, Any]]:
    out = []
    for d in rows:
        if not isinstance(d, dict) or not _str(d.get("mac")):
            continue
        out.append({"mac": d["mac"].lower(), "name": _str(d.get("name")),
                    "type": _str(d.get("type")), "model": _str(d.get("model")),
                    "state": d.get("state"),
                    "ports": parse_ports(d), "lldp": parse_lldp(d), "uplink": parse_uplink(d),
                    "networks": parse_networks(d), "wan": parse_wan_uplink(d)})
    return out


def is_gateway_row(dev: dict[str, Any]) -> bool:
    """A parsed classic device that is the gateway: `type` ugw (the classic role), else a
    gateway token in the model or name (the same tokens records.select_gateway uses)."""
    if dev.get("type", "").lower() in ("ugw", "gateway", "console"):
        return True
    blob = f"{dev.get('model', '')} {dev.get('name', '')}".lower()
    return any(tok in blob for tok in GATEWAY_TOKENS)


def select_gateway_row(devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The gateway among parsed classic devices: a declared type first, then name tokens."""
    for dev in devices:
        if dev.get("type", "").lower() in ("ugw", "gateway", "console"):
            return dev
    for dev in devices:
        if is_gateway_row(dev):
            return dev
    return None


def wan_status(health: dict[str, Any] | None, gateway: dict[str, Any] | None) -> dict[str, Any]:
    """One WAN picture from the stat/health wan row and the gateway's uplink, in the shape
    records.write_site_status_classic stores. The health row decides `up` (status ok) and the
    address; the uplink fills what the health row lacks and gives the port and rates."""
    out: dict[str, Any] = {"up": None, "ip": "", "port": "", "rx_rate_bps": None,
                           "tx_rate_bps": None, "latency_ms": None, "gateways": []}
    if health:
        status = health.get("status", "").lower()
        if status:
            out["up"] = status == "ok"
        out["ip"] = health.get("wan_ip") or ""
        out["latency_ms"] = health.get("latency_ms")
        out["gateways"] = health.get("gateways") or []
    wan = (gateway or {}).get("wan") or {}
    if out["up"] is None:
        out["up"] = wan.get("internet") if wan.get("internet") is not None else wan.get("up")
    out["ip"] = out["ip"] or wan.get("ip") or ""
    out["port"] = wan.get("port") or ""
    out["rx_rate_bps"], out["tx_rate_bps"] = wan.get("rx_rate_bps"), wan.get("tx_rate_bps")
    return out


def uptime_to_seconds(value: Any, now: float) -> int | None:
    """A classic `uptime` as seconds: below EPOCH_THRESHOLD it already is a duration, at or
    above it is the epoch moment the client came up (the core unifi rule)."""
    n = _num(value)
    if n is None or n < 0:
        return None
    if n < EPOCH_THRESHOLD:
        return int(n)
    return max(0, int(now - n))


def resolve_client_vlan(client: dict[str, Any],
                        networks: list[dict[str, Any]]) -> tuple[int | None, str]:
    """A client's (vlan, network name): the raw `vlan` first, else the gateway's network table
    by `network_id` then by `network` name, else only the network name. Ported from
    ha_Int_soc unifi_core.resolve_client_vlan; a network without a VLAN resolves to its name."""
    vlan = _int(client.get("vlan"))
    name = _str(client.get("network"))
    if vlan is not None:
        return vlan, name
    net_id = _str(client.get("network_id"))
    for net in networks:
        if (net_id and net.get("id") == net_id) or (name and net.get("name") == name):
            return _int(net.get("vlan")), _str(net.get("name")) or name
    return None, name


def parse_wlans(rows: list[Any]) -> list[dict[str, Any]]:
    """`rest/wlanconf` rows: what decides whether a client may join. Every key (_id, name,
    enabled, security, networkconf_id, ap_group_ids, ap_group_mode, is_guest, wlan_band,
    hide_ssid, mac_filter_enabled, mac_filter_policy, schedule_enabled, schedule) is UNVERIFIED
    against a live console; ha_Int_soc reads only id, name and enabled from aiounifi's wlans.
    The passphrase (x_passphrase) is never read."""
    out: list[dict[str, Any]] = []
    for w in rows:
        if not isinstance(w, dict) or not _str(w.get("name")) or not _str(w.get("_id")):
            continue
        enabled = w.get("enabled")
        guest = w.get("is_guest")
        hidden = w.get("hide_ssid")
        mac_on = w.get("mac_filter_enabled")
        groups = w.get("ap_group_ids")
        sched = w.get("schedule_enabled")
        if sched is None and isinstance(w.get("schedule"), list):
            sched = bool(w["schedule"])
        out.append({
            "id": w["_id"], "name": w["name"],
            "enabled": enabled if isinstance(enabled, bool) else None,
            "security": _str(w.get("security")).lower(),
            "network_id": _str(w.get("networkconf_id")),
            "ap_group_mode": _str(w.get("ap_group_mode")).lower(),
            "ap_group_ids": [g for g in groups if isinstance(g, str)]
            if isinstance(groups, list) else [],
            "guest": guest if isinstance(guest, bool) else None,
            "band": _str(w.get("wlan_band")).lower(),
            "hidden": hidden if isinstance(hidden, bool) else None,
            "mac_filter": (_str(w.get("mac_filter_policy")).lower() or "on") if mac_on is True
            else ("off" if mac_on is False else ""),
            "scheduled": sched if isinstance(sched, bool) else None,
        })
    return sorted(out, key=lambda w: w["name"].lower())


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
    """Known clients (rest/user) whose MAC is not in the active list (stat/sta). `is_wired`
    (UNVERIFIED on rest/user) is kept as `wired` so the absent list can leave wired ones out."""
    live = {_str(c.get("mac")).lower() for c in active if isinstance(c, dict)}
    out = []
    for u in known:
        if not isinstance(u, dict):
            continue
        mac = _str(u.get("mac")).lower()
        if mac and mac not in live:
            wired = u.get("is_wired")
            out.append({"mac": mac, "name": _str(u.get("name")) or _str(u.get("hostname")),
                        "last_seen": _num(u.get("last_seen")),
                        "first_seen": _num(u.get("first_seen")),
                        "wired": wired if isinstance(wired, bool) else None})
    return out
