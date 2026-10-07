"""The UniFi resources of /api/v2 (docs/DATA-API-DESIGN.md sections 4.2 and 4.10).

The plugin registers them through `register_api`, so the core mounts them under /api/v2/unifi
with its own sign-in, role check, rate limit, ETag and read connection. A handler only reads its
own tables on the read connection it is given. Lists are paged by the primary key, so a row
written while a client pages never moves another row to a different page. Every string came from
the controller or from a client that named itself, and is only ever handed over as JSON.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from observe.api import ApiRegistry
from observe.api.cursor import PageParams, encode
from observe.api.models import Page, Ts
from observe.api.problems import ApiProblem

DOMAINS = ("unifi",)


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
    first_seen: Ts
    last_seen: Ts


class DevicePage(Page):
    items: list[Device]


class DeviceFilters(BaseModel):
    site: str | None = Field(None, max_length=128, description="One site id")
    state: str | None = Field(None, max_length=32)


DEVICE_COLUMNS = ("site_id, device_id, mac, name, model, state, ip, firmware, "
                  "firmware_updatable, first_seen, last_seen")


def _device(r: Any) -> dict[str, Any]:
    keys = [k.strip() for k in DEVICE_COLUMNS.split(",")]
    d = dict(zip(keys, r))
    fu = d["firmware_updatable"]
    d["firmware_updatable"] = None if fu is None else bool(fu)
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
    first_seen: Ts
    last_seen: Ts


class ClientPage(Page):
    items: list[Client]


class ClientFilters(BaseModel):
    site: str | None = Field(None, max_length=128, description="One site id")
    connected: bool | None = Field(None, description="Only connected (or not connected) clients")
    q: str | None = Field(None, max_length=128, description="Text in the name, MAC or address")


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
    rows = db.execute(
        "SELECT c.site_id, c.client_id, c.mac, c.name, c.ip, c.kind, c.connected, "
        "c.connected_at, c.ssid, c.sw_port, c.uplink_device_id, c.uplink_mac, d.name, "
        "c.first_seen, c.last_seen FROM unifi_clients c "
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
              "last_seen": r[14]} for r in rows]
    return {"items": items,
            "next_cursor": encode([rows[-1][0], rows[-1][1]]) if more else None}


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


def register(api: ApiRegistry) -> None:
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
