"""The UniFi resources of /api/v2 (docs/DATA-API-DESIGN.md sections 4.2 and 4.10).

The plugin registers them through `register_api`, so the core mounts them under /api/v2/unifi
with its own sign-in, role check, rate limit, ETag and read connection. A handler only reads its
own tables on the read connection it is given. Lists are paged by the primary key, so a row
written while a client pages never moves another row to a different page. Every string came from
the controller or from a client that named itself, and is only ever handed over as JSON.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from observe.api import ApiRegistry
from observe.api.cursor import PageParams, encode
from observe.api.models import Page, Ts
from observe.api.problems import ApiProblem

from .classic import OFFLINE_DEVICE_STATES
from .readiness import ssid_findings, summary_of

if TYPE_CHECKING:  # pragma: no cover
    from . import UniFiPlugin

DOMAINS = ("unifi",)
ONLINE_DEVICE_STATES = frozenset({"ONLINE", "CONNECTED"})
UNKNOWN_SSID = "(unknown SSID)"
# A client or camera row is stale when its last_seen is older than this many poll intervals.
STALE_FACTOR = 2.5
# The devices list is stale when the devices collector has not succeeded within this many
# intervals.
DEVICES_STALE_FACTOR = 2.0


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _more(rows: list[Any], limit: int) -> tuple[list[Any], bool]:
    return rows[:limit], len(rows) > limit


class Device(BaseModel):
    site_id: str
    device_id: str
    mac: str
    name: str
    model: str
    state: str
    ip: str
    firmware: str
    firmware_updatable: bool | None = None
    firmware_status: str = Field(description="Up to date, Update available or unknown")
    device_type: str = Field(description="The row's type or deviceType as given; empty when "
                                         "absent (unverified field)")
    rx_bytes: int | None = Field(None, description="Cumulative bytes received, when the "
                                                  "console gives them (unverified)")
    tx_bytes: int | None = None
    rx_rate_bps: float | None = Field(None, description="Bytes per second as the console "
                                                       "counts them (unverified unit)")
    tx_rate_bps: float | None = None
    first_seen: Ts
    last_seen: Ts


class DevicePage(Page):
    items: list[Device]


class DeviceFilters(BaseModel):
    site: str | None = Field(None, max_length=128, description="One site id")
    state: str | None = Field(None, max_length=32)


DEVICE_COLUMNS = ("site_id, device_id, mac, name, model, state, ip, firmware, "
                  "firmware_updatable, device_type, rx_bytes, tx_bytes, rx_rate_bps, "
                  "tx_rate_bps, first_seen, last_seen")


def firmware_status(updatable: bool | None) -> str:
    return "unknown" if updatable is None else ("Update available" if updatable else "Up to date")


def _device(r: Any) -> dict[str, Any]:
    keys = [k.strip() for k in DEVICE_COLUMNS.split(",")]
    d = dict(zip(keys, r))
    fu = d["firmware_updatable"]
    d["firmware_updatable"] = None if fu is None else bool(fu)
    d["firmware_status"] = firmware_status(d["firmware_updatable"])
    return d


def list_devices(db: Any, page: PageParams, filters: DeviceFilters) -> dict[str, Any]:
    """UniFi devices by site and id."""
    after = page.after(str, str)
    where, args = ["1=1"], []
    if after:
        where.append("(site_id > ? OR (site_id = ? AND device_id > ?))")
        args += [after[0], after[0], after[1]]
    for column, value in (("site_id", filters.site), ("state", filters.state)):
        if value:
            where.append(f"{column} = ?")
            args.append(value)
    rows = db.execute(
        f"SELECT {DEVICE_COLUMNS} FROM unifi_devices WHERE " + " AND ".join(where)
        + " ORDER BY site_id, device_id LIMIT ?", (*args, page.limit + 1)).fetchall()
    rows, more = _more(rows, page.limit)
    return {"items": [_device(r) for r in rows],
            "next_cursor": encode([rows[-1][0], rows[-1][1]]) if more else None}


def get_device(db: Any, site_id: str, device_id: str) -> dict[str, Any]:
    """One device."""
    row = db.execute(f"SELECT {DEVICE_COLUMNS} FROM unifi_devices WHERE site_id = ? AND "
                     "device_id = ?", (site_id, device_id)).fetchone()
    if row is None:
        raise ApiProblem(404, "unknown device")
    return _device(row)


class Client(BaseModel):
    site_id: str
    client_id: str
    mac: str
    name: str
    ip: str
    kind: str
    connected: bool | None = None
    connected_at: Ts | None = None
    ssid: str
    sw_port: int | None = None
    uplink_device_id: str
    uplink_mac: str
    uplink_name: str
    vlan: int | None = Field(None, description="Classic detail; null without it")
    network: str = Field(description="The network name the classic console gave, or empty")
    uptime_s: int | None = Field(None, description="Classic uptime in seconds; a client "
                                                  "derives it from connected_at when null")
    rx_rate_bps: float | None = Field(None, description="Bytes per second as the console "
                                                       "counts them (unverified unit)")
    tx_rate_bps: float | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    first_seen: Ts
    last_seen: Ts


class ClientPage(Page):
    items: list[Client]


class ClientFilters(BaseModel):
    site: str | None = Field(None, max_length=128, description="One site id")
    connected: bool | None = Field(None, description="Only connected (or not connected) clients")
    q: str | None = Field(None, max_length=128, description="Text in the name, MAC or address")
    vlan: int | None = Field(None, ge=0, le=4095, description="Only clients on this VLAN")
    ssid: str | None = Field(None, max_length=128, description="Only clients on this SSID")


def list_clients(db: Any, page: PageParams, filters: ClientFilters) -> dict[str, Any]:
    """Clients with the name of the device they sit on."""
    after = page.after(str, str)
    where, args = ["1=1"], []
    if after:
        where.append("(c.site_id > ? OR (c.site_id = ? AND c.client_id > ?))")
        args += [after[0], after[0], after[1]]
    if filters.site:
        where.append("c.site_id = ?")
        args.append(filters.site)
    if filters.connected is True:
        where.append("c.connected = 1")
    elif filters.connected is False:
        where.append("(c.connected = 0 OR c.connected IS NULL)")
    if filters.q:
        like = f"%{_like(filters.q.lower())}%"
        where.append("(LOWER(c.name) LIKE ? ESCAPE '\\' OR LOWER(c.mac) LIKE ? ESCAPE '\\' "
                     "OR c.ip LIKE ? ESCAPE '\\')")
        args += [like, like, like]
    if filters.vlan is not None:
        where.append("c.vlan = ?")
        args.append(filters.vlan)
    if filters.ssid:
        where.append("c.ssid = ?")
        args.append(filters.ssid)
    rows = db.execute(
        "SELECT c.site_id, c.client_id, c.mac, c.name, c.ip, c.kind, c.connected, "
        "c.connected_at, c.ssid, c.sw_port, c.uplink_device_id, c.uplink_mac, d.name, "
        "c.first_seen, c.last_seen, c.vlan, c.network, c.uptime_s, c.rx_rate_bps, "
        "c.tx_rate_bps, c.rx_bytes, c.tx_bytes FROM unifi_clients c "
        "LEFT JOIN unifi_devices d ON d.site_id = c.site_id "
        "AND ((c.uplink_device_id != '' AND d.device_id = c.uplink_device_id) "
        "OR (c.uplink_device_id = '' AND c.uplink_mac != '' AND d.mac = c.uplink_mac)) "
        "WHERE " + " AND ".join(where) + " ORDER BY c.site_id, c.client_id LIMIT ?",
        (*args, page.limit + 1)).fetchall()
    rows, more = _more(rows, page.limit)
    items = [{"site_id": r[0], "client_id": r[1], "mac": r[2], "name": r[3], "ip": r[4],
              "kind": r[5], "connected": None if r[6] is None else bool(r[6]),
              "connected_at": r[7], "ssid": r[8], "sw_port": r[9], "uplink_device_id": r[10],
              "uplink_mac": r[11], "uplink_name": r[12] or "", "first_seen": r[13],
              "last_seen": r[14], "vlan": r[15], "network": r[16] or "", "uptime_s": r[17],
              "rx_rate_bps": r[18], "tx_rate_bps": r[19], "rx_bytes": r[20], "tx_bytes": r[21]}
             for r in rows]
    return {"items": items,
            "next_cursor": encode([rows[-1][0], rows[-1][1]]) if more else None}


# ---- the network view: overview, SSIDs, absent clients ------------------------------------

class SiteFilter(BaseModel):
    site: str | None = Field(None, max_length=128, description="One site id; the latest "
                                                              "polled site when omitted")


class SsidCount(BaseModel):
    ssid: str
    count: int


class Wan(BaseModel):
    ip: str = Field(description="The WAN address from the classic health row or gateway "
                                "uplink; empty when unknown")
    port: str = Field(description="The uplink interface name; empty when unknown")
    rx_rate_bps: float | None = Field(None, description="Bytes per second as the console "
                                                       "counts them (unverified unit)")
    tx_rate_bps: float | None = None
    latency_ms: float | None = None


class Gateway(BaseModel):
    device_id: str
    name: str
    state: str


class Overview(BaseModel):
    now: Ts
    site_id: str | None = None
    status: str = Field(description="online, offline or unknown, from the gateway's state")
    gateway: Gateway | None = None
    internet_up: bool | None = None
    wan: Wan
    wireless_clients: int
    wired_clients: int
    total_clients: int
    device_count: int
    clients_per_ssid: list[SsidCount]
    devices_updated: Ts | None = None
    devices_stale: bool
    clients_updated: Ts | None = Field(None, description="The newest connected client row")
    classic_updated: Ts | None = Field(None, description="The last good classic poll")
    classic_stale: bool
    classic_configured: bool
    classic_note: str | None = None


def _site_of(db: Any, wanted: str | None) -> str | None:
    """The site to describe: the one asked for, else the site row polled most recently, else
    the site of the newest device row."""
    if wanted:
        return wanted
    row = db.execute("SELECT site_id FROM unifi_site_status ORDER BY "
                     "COALESCE(updated, 0) DESC, site_id LIMIT 1").fetchone()
    if row is None:
        row = db.execute("SELECT site_id FROM unifi_devices ORDER BY last_seen DESC, site_id "
                         "LIMIT 1").fetchone()
    return row[0] if row else None


def network_status(state: str | None) -> str:
    """online, offline or unknown from a gateway's state word. The offline names are the core
    unifi ones plus the Integration OFFLINE; the stricter signal wins."""
    word = (state or "").upper()
    if word in OFFLINE_DEVICE_STATES:
        return "offline"
    if word in ONLINE_DEVICE_STATES:
        return "online"
    return "unknown"


def build_overview(plugin: UniFiPlugin) -> Any:
    def overview(db: Any, filters: SiteFilter) -> dict[str, Any]:
        """The five tiles and the clients per SSID in one read. Counts are of connected clients;
        wired is every connected client that is not wireless (the HA SOC rule). The Internet
        and WAN values come from the classic poll and the gateway statistics, and are null
        without them. It depends on the clock and collector memory, so it sends no ETag."""
        now = plugin.wall()
        s = plugin.settings
        site = _site_of(db, filters.site)
        status = db.execute(
            "SELECT gateway_device_id, internet_up, wan_ip, wan_port, wan_rx_rate_bps, "
            "wan_tx_rate_bps, wan_latency_ms, updated, classic_updated FROM unifi_site_status "
            "WHERE site_id = ?", (site,)).fetchone() if site else None
        gw_row = None
        if status and status[0]:
            gw_row = db.execute("SELECT device_id, name, state FROM unifi_devices WHERE "
                                "site_id = ? AND device_id = ?", (site, status[0])).fetchone()
        counts = db.execute(
            "SELECT COUNT(*), SUM(CASE WHEN kind = 'wireless' THEN 1 ELSE 0 END) "
            "FROM unifi_clients WHERE site_id = ? AND connected = 1", (site or "",)).fetchone()
        total, wireless = int(counts[0] or 0), int(counts[1] or 0)
        per_ssid = db.execute(
            "SELECT ssid, COUNT(*) FROM unifi_clients WHERE site_id = ? AND connected = 1 "
            "AND kind = 'wireless' GROUP BY ssid ORDER BY COUNT(*) DESC, ssid",
            (site or "",)).fetchall()
        devices = db.execute("SELECT COUNT(*), MAX(last_seen) FROM unifi_devices WHERE "
                             "site_id = ?", (site or "",)).fetchone()
        newest_client = db.execute("SELECT MAX(last_seen) FROM unifi_clients WHERE site_id = ? "
                                   "AND connected = 1", (site or "",)).fetchone()[0]
        updated, stale = devices_freshness(devices[1], plugin.devices_ok_at, now, s.interval)
        classic_at = status[8] if status else None
        return {
            "now": now, "site_id": site,
            "status": network_status(gw_row[2]) if gw_row else "unknown",
            "gateway": {"device_id": gw_row[0], "name": gw_row[1], "state": gw_row[2]}
            if gw_row else None,
            "internet_up": None if not status or status[1] is None else bool(status[1]),
            "wan": {"ip": status[2] if status else "", "port": status[3] if status else "",
                    "rx_rate_bps": status[4] if status else None,
                    "tx_rate_bps": status[5] if status else None,
                    "latency_ms": status[6] if status else None},
            "wireless_clients": wireless, "wired_clients": total - wireless,
            "total_clients": total, "device_count": int(devices[0] or 0),
            "clients_per_ssid": [{"ssid": r[0] or UNKNOWN_SSID, "count": r[1]} for r in per_ssid],
            "devices_updated": updated, "devices_stale": stale,
            "clients_updated": newest_client, "classic_updated": classic_at,
            "classic_stale": classic_at is not None
            and now - classic_at > DEVICES_STALE_FACTOR * s.interval,
            "classic_configured": s.classic_credential is not None,
            "classic_note": plugin.classic_note,
        }
    return overview


class Finding(BaseModel):
    code: str
    severity: str = Field(description="blocking, possible or unknown")
    message: str


class Wlan(BaseModel):
    site_id: str
    wlan_id: str
    name: str
    enabled: bool | None = None
    security: str
    network_name: str
    vlan: int | None = None
    band: str
    hidden: bool | None = None
    guest: bool | None = None
    mac_filter: str = Field(description="off, allow, deny or on; empty when unknown")
    ap_group_mode: str
    ap_names: list[str] = Field(description="Permitted access points by name; empty when the "
                                            "mode is all or the groups cannot be resolved")
    scheduled: bool | None = None
    carrying_aps: list[str] = Field(description="Access points with a connected client on "
                                                "this SSID now")
    client_count: int
    findings: list[Finding]
    summary: str = Field(description="Plain language: what refuses a client, or nothing")
    updated: Ts


class WlanList(BaseModel):
    items: list[Wlan]


def list_wlans(db: Any, filters: SiteFilter) -> dict[str, Any]:
    """SSID readiness: the stored rest/wlanconf rows with the access points carrying each SSID
    now (connected wireless clients joined to device names) and the conditions that refuse a
    client. Nothing here says a client failed; see readiness.py."""
    site = _site_of(db, filters.site)
    rows = db.execute(
        "SELECT site_id, wlan_id, name, enabled, security, network_name, vlan, band, hidden, "
        "guest, mac_filter, ap_group_mode, ap_names, scheduled, updated FROM unifi_wlans "
        "WHERE site_id = ? ORDER BY LOWER(name), wlan_id", (site or "",)).fetchall()
    carrying = db.execute(
        "SELECT c.ssid, d.name, COUNT(*) FROM unifi_clients c "
        "LEFT JOIN unifi_devices d ON d.site_id = c.site_id "
        "AND ((c.uplink_device_id != '' AND d.device_id = c.uplink_device_id) "
        "OR (c.uplink_device_id = '' AND c.uplink_mac != '' AND d.mac = c.uplink_mac)) "
        "WHERE c.site_id = ? AND c.connected = 1 AND c.kind = 'wireless' AND c.ssid != '' "
        "GROUP BY c.ssid, d.name", (site or "",)).fetchall()
    aps: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for ssid, ap, n in carrying:
        counts[ssid] = counts.get(ssid, 0) + n
        if ap:
            aps.setdefault(ssid, set()).add(ap)
    items = []
    for r in rows:
        flag = lambda v: None if v is None else bool(v)  # noqa: E731
        try:
            names = json.loads(r[12]) if r[12] else []
        except ValueError:
            names = []
        w = {"site_id": r[0], "wlan_id": r[1], "name": r[2], "enabled": flag(r[3]),
             "security": r[4], "network_name": r[5], "vlan": r[6], "band": r[7],
             "hidden": flag(r[8]), "guest": flag(r[9]), "mac_filter": r[10],
             "ap_group_mode": r[11], "ap_names": [n for n in names if isinstance(n, str)],
             "scheduled": flag(r[13]), "carrying_aps": sorted(aps.get(r[2], ())),
             "client_count": counts.get(r[2], 0), "updated": r[14]}
        w["findings"] = ssid_findings(w)
        w["summary"] = summary_of(w["findings"])
        items.append(w)
    return {"items": items}


class AbsentClient(BaseModel):
    site_id: str
    client_id: str
    mac: str
    name: str
    kind: str
    ssid: str = Field(description="The SSID the client was last seen on, or empty")
    first_seen: Ts
    last_seen: Ts


class AbsentPage(Page):
    items: list[AbsentClient]


def list_absent_clients(db: Any, page: PageParams, filters: SiteFilter) -> dict[str, Any]:
    """Clients the console knows but is not carrying now, wireless or of unknown kind, most
    recently seen first. A wired client is left out, as in HA SOC build_absent_clients. A
    client that never associated appears in no collection at all, so its absence here is not
    evidence that it is fine."""
    site = _site_of(db, filters.site)
    after = page.after(float, str)
    where = ["site_id = ?", "connected = 0", "kind != 'wired'"]
    args: list[Any] = [site or ""]
    if after:
        where.append("(last_seen < ? OR (last_seen = ? AND client_id > ?))")
        args += [after[0], after[0], after[1]]
    rows = db.execute(
        "SELECT site_id, client_id, mac, name, kind, ssid, first_seen, last_seen FROM "
        "unifi_clients WHERE " + " AND ".join(where)
        + " ORDER BY last_seen DESC, client_id LIMIT ?", (*args, page.limit + 1)).fetchall()
    rows, more = _more(rows, page.limit)
    items = [{"site_id": r[0], "client_id": r[1], "mac": r[2], "name": r[3], "kind": r[4],
              "ssid": r[5], "first_seen": r[6], "last_seen": r[7]} for r in rows]
    return {"items": items,
            "next_cursor": encode([rows[-1][7], rows[-1][1]]) if more else None}


class Camera(BaseModel):
    camera_id: str
    mac: str
    name: str
    model: str
    state: str
    connected: bool | None = None
    recording: bool | None = None
    first_seen: Ts
    last_seen: Ts


class CameraPage(Page):
    items: list[Camera]


def list_cameras(db: Any, page: PageParams) -> dict[str, Any]:
    """Protect cameras."""
    after = page.after(str)
    rows = db.execute(
        "SELECT camera_id, mac, name, model, state, connected, recording, first_seen, last_seen "
        "FROM unifi_cameras WHERE camera_id > ? ORDER BY camera_id LIMIT ?",
        (after[0] if after else "", page.limit + 1)).fetchall()
    rows, more = _more(rows, page.limit)
    items = [{"camera_id": r[0], "mac": r[1], "name": r[2], "model": r[3], "state": r[4],
              "connected": None if r[5] is None else bool(r[5]),
              "recording": None if r[6] is None else bool(r[6]), "first_seen": r[7],
              "last_seen": r[8]} for r in rows]
    return {"items": items, "next_cursor": encode([rows[-1][0]]) if more else None}


def devices_freshness(newest_seen: float | None, ok_at: float | None, now: float,
                      interval: float) -> tuple[float | None, bool]:
    """The time of the last good devices poll and whether it is older than twice the interval.
    After a restart the in-memory time is unknown, so the newest stored last_seen stands in."""
    last = ok_at if ok_at is not None else newest_seen
    return last, last is not None and now - last > DEVICES_STALE_FACTOR * interval


class Status(BaseModel):
    now: Ts = Field(description="The server clock, so a client judges staleness without its own.")
    devices_updated: Ts | None = Field(None, description="The last good devices poll.")
    devices_stale: bool
    clients_stale_after: float = Field(description="Seconds after which a connected client row "
                                                   "that was not refreshed is stale.")
    protect_stale_after: float
    protect_enabled: bool
    classic_configured: bool
    classic_note: str | None = None


def build_status(plugin: UniFiPlugin) -> Any:
    def status(db: Any) -> dict[str, Any]:
        """What the page needs to judge the lists: the clock, the staleness windows and whether
        the optional classic account and Protect are set up. It depends on the clock and on
        collector memory, so it sends no ETag."""
        now = plugin.wall()
        newest = db.execute("SELECT MAX(last_seen) FROM unifi_devices").fetchone()[0]
        updated, stale = devices_freshness(newest, plugin.devices_ok_at, now,
                                           plugin.settings.interval)
        s = plugin.settings
        return {"now": now, "devices_updated": updated, "devices_stale": stale,
                "clients_stale_after": STALE_FACTOR * s.clients_interval,
                "protect_stale_after": STALE_FACTOR * s.protect_interval,
                "protect_enabled": s.protect,
                "classic_configured": s.classic_credential is not None,
                "classic_note": plugin.classic_note}
    return status


def register(api: ApiRegistry, plugin: UniFiPlugin) -> None:
    api.resource("/unifi/status", build_status(plugin), Status, etag=False, tags=("unifi",),
                 operation_id="status", summary="UniFi poll freshness and settings")
    api.resource("/unifi/devices", list_devices, DevicePage, domains=DOMAINS, tags=("unifi",),
                 paginate=True, filters=DeviceFilters, operation_id="devices",
                 summary="List UniFi devices")
    api.resource("/unifi/devices/{site_id}/{device_id}", get_device, Device, domains=DOMAINS,
                 tags=("unifi",), operation_id="device", summary="One UniFi device")
    api.resource("/unifi/clients", list_clients, ClientPage, domains=DOMAINS, tags=("unifi",),
                 paginate=True, filters=ClientFilters, operation_id="clients",
                 summary="List UniFi clients")
    api.resource("/unifi/cameras", list_cameras, CameraPage, domains=DOMAINS, tags=("unifi",),
                 paginate=True, operation_id="cameras", summary="List Protect cameras")
    api.resource("/unifi/overview", build_overview(plugin), Overview, etag=False,
                 filters=SiteFilter, tags=("unifi",), operation_id="overview",
                 summary="Network status, Internet, WAN, client counts and clients per SSID")
    api.resource("/unifi/wlans", list_wlans, WlanList, domains=DOMAINS, filters=SiteFilter,
                 tags=("unifi",), operation_id="wlans", summary="SSID readiness")
    api.resource("/unifi/absent-clients", list_absent_clients, AbsentPage, domains=DOMAINS,
                 paginate=True, filters=SiteFilter, tags=("unifi",),
                 operation_id="absent_clients", summary="Known but not connected clients")
