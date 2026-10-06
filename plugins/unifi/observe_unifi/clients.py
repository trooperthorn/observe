"""Clients and Protect cameras: parsing, storage and the rows the pages read.

Clients. The Integration API `clients` list is the authority for who is connected now. Each row
is keyed by its lower-case MAC, so a client is one row however it was seen. The optional classic
controller (classic.py) enriches a connected client with its access point or switch MAC, switch
port number and SSID, and adds clients that are known but offline, with the time the console last
saw them. A client the console no longer lists as connected is marked not connected, never
deleted; the 30 day prune removes rows not seen for `retention_days`.

Field names. Verified against ha_Int_soc docs/UNIFI-LOCAL-API-CONTRACT.md: the Integration client
is `id`, `name`, `macAddress`, `ipAddress`, `connectedAt`, `type` and `uplinkDeviceId`. UNVERIFIED:
the values of `type` (WIRED and WIRELESS are expected), that `connectedAt` is ISO 8601, and every
classic field named in `parse_active_clients` and `offline_clients`. A field that is absent or of
another type is stored as unknown, never as a guess.

Cameras. Protect 7.2.105 `GET /cameras` is an unpaginated array (verified). `id`, `name`, `mac`,
`state` and `isConnected` follow the keys the HA SOC normalizer reads. `isRecording` is a boolean
on some firmwares and `recordingSettings.mode` on others (UNVERIFIED), so recording is stored
only when `isRecording` is a boolean and is otherwise unknown. No NVR storage route is verified,
so none is read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from observe.store import Store

MAX_ROWS = 5000  # the most clients or cameras a page request returns
_HEX = frozenset("0123456789abcdef")


def _text(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def norm_mac(v: Any) -> str:
    """A MAC as lower-case colon text, or an empty string when it is not twelve hex digits."""
    digits = "".join(c for c in _text(v).lower() if c not in ":-. ")
    if len(digits) != 12 or not set(digits) <= _HEX:
        return ""
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def _epoch(v: Any) -> float | None:
    """connectedAt as epoch seconds: an ISO 8601 string (unverified) or a number."""
    n = _num(v)
    if n is not None:
        return n / 1000.0 if n > 1e11 else n  # a millisecond epoch is also accepted
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class Client:
    site_id: str
    client_id: str
    mac: str
    name: str = ""
    ip: str = ""
    kind: str = ""  # wired, wireless or empty when unknown
    uplink_device_id: str = ""
    connected: bool | None = True
    connected_at: float | None = None
    ssid: str = ""
    uplink_mac: str = ""
    sw_port: int | None = None
    enriched: bool = False
    last_seen: float | None = None  # set only for an offline client, from the console


def parse_client(site_id: str, raw: Any) -> Client | None:
    """One row of the Integration `clients` list. A row with neither a MAC nor an id is skipped."""
    if not isinstance(raw, dict):
        return None
    mac = norm_mac(raw.get("macAddress"))
    cid = mac or _text(raw.get("id"))
    if not cid:
        return None
    kind = _text(raw.get("type")).lower()
    return Client(site_id, cid, mac, _text(raw.get("name")), _text(raw.get("ipAddress")),
                  kind if kind in ("wired", "wireless") else "",
                  _text(raw.get("uplinkDeviceId")), True, _epoch(raw.get("connectedAt")))


def parse_active_clients(rows: list[Any]) -> dict[str, dict[str, Any]]:
    """`stat/sta` rows by MAC. UNVERIFIED against a live console: `ap_mac` (a wireless client's
    access point), `sw_mac` and `sw_port` (a wired client's switch and port number), `essid`,
    `is_wired`, `hostname` and `name`. ha_Int_soc names `ap_mac` for the core client only."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        mac = norm_mac(r.get("mac"))
        if not mac:
            continue
        wired = r.get("is_wired") if isinstance(r.get("is_wired"), bool) else None
        first, second = ("sw_mac", "ap_mac") if wired else ("ap_mac", "sw_mac")
        out[mac] = {"uplink_mac": norm_mac(r.get(first)) or norm_mac(r.get(second)),
                    "sw_port": _int(r.get("sw_port")) if wired is not False else None,
                    "ssid": _text(r.get("essid")), "wired": wired,
                    "name": _text(r.get("name")) or _text(r.get("hostname"))}
    return out


def enrich(clients: list[Client], active: dict[str, dict[str, Any]],
           devices_by_mac: dict[str, str]) -> list[Client]:
    """Add classic detail to connected clients and resolve an uplink MAC to a device id."""
    out = []
    for c in clients:
        e = active.get(c.mac)
        if e is None:
            out.append(c)
            continue
        kind = c.kind or {True: "wired", False: "wireless"}.get(e["wired"], "")
        out.append(replace(
            c, kind=kind, name=c.name or e["name"], ssid=e["ssid"], uplink_mac=e["uplink_mac"],
            sw_port=e["sw_port"], enriched=True,
            uplink_device_id=c.uplink_device_id or devices_by_mac.get(e["uplink_mac"], "")))
    return out


def offline_clients(site_id: str, rows: Iterable[dict[str, Any]], connected: set[str],
                    now: float, retention_days: int) -> list[Client]:
    """Known but not connected clients from `parse_offline_clients`. One older than the retention
    is left out, so the prune does not delete it only for the next poll to add it back."""
    cutoff = now - retention_days * 86400
    out = []
    for r in rows:
        mac = norm_mac(r.get("mac"))
        seen = _num(r.get("last_seen"))  # UNVERIFIED: epoch seconds
        if not mac or mac in connected or (seen is not None and seen < cutoff):
            continue
        out.append(Client(site_id, mac, mac, _text(r.get("name")), connected=False,
                          last_seen=seen if seen is not None else now))
    return out


_UP = """INSERT INTO unifi_clients (site_id, client_id, mac, name, ip, kind, uplink_device_id,
  connected, connected_at, ssid, uplink_mac, sw_port, enriched, first_seen, last_seen)
  VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)
  ON CONFLICT (site_id, client_id) DO UPDATE SET mac=excluded.mac, name=excluded.name,
  ip=excluded.ip, kind=excluded.kind, uplink_device_id=excluded.uplink_device_id, connected=1,
  connected_at=excluded.connected_at, ssid=excluded.ssid, uplink_mac=excluded.uplink_mac,
  sw_port=excluded.sw_port, enriched=excluded.enriched, last_seen=excluded.last_seen"""
# An offline row keeps what is already known and moves only the name and the last seen time.
_OFF = """INSERT INTO unifi_clients (site_id, client_id, mac, name, connected, first_seen,
  last_seen) VALUES (?,?,?,?,0,?,?)
  ON CONFLICT (site_id, client_id) DO UPDATE SET connected=0,
  name=CASE WHEN excluded.name != '' THEN excluded.name ELSE name END,
  last_seen=MAX(last_seen, excluded.last_seen)"""


def _write_clients(store: Store, site_id: str, live: list[Client], off: list[Client],
                   now: float) -> int:
    with store._lock:
        db = store._db
        try:
            db.executemany(_UP, [
                (c.site_id, c.client_id, c.mac, c.name, c.ip, c.kind, c.uplink_device_id,
                 c.connected_at, c.ssid, c.uplink_mac, c.sw_port, int(c.enriched), now, now)
                for c in live])
            # Anything still marked connected that this poll did not write has left.
            db.execute("UPDATE unifi_clients SET connected=0 WHERE site_id=? AND connected=1 "
                       "AND last_seen < ?", (site_id, now))
            seen = [c.last_seen if c.last_seen is not None else now for c in off]
            db.executemany(_OFF, [(c.site_id, c.client_id, c.mac, c.name, s, s)
                                  for c, s in zip(off, seen)])
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return len(live) + len(off)


async def save_clients(store: Store, site_id: str, live: list[Client], off: list[Client],
                       now: float) -> int:
    """Upsert one poll in a single transaction. `first_seen` is kept for a known client."""
    return await asyncio.to_thread(_write_clients, store, site_id, live, off, now)


async def device_index(store: Store, site_id: str) -> dict[str, str]:
    """MAC to device id for the devices of a site, to resolve a classic uplink MAC."""
    def go() -> dict[str, str]:
        with store._lock:
            rows = store._db.execute(
                "SELECT mac, device_id FROM unifi_devices WHERE site_id=? AND mac != ''",
                (site_id,)).fetchall()
        return {norm_mac(m): d for m, d in rows if norm_mac(m)}
    return await asyncio.to_thread(go)


@dataclass(frozen=True)
class Camera:
    camera_id: str
    mac: str
    name: str
    model: str
    state: str
    connected: bool | None
    recording: bool | None


def parse_camera(raw: Any) -> Camera | None:
    """One row of the Protect `cameras` array. A row without a string id is skipped."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    conn, rec = raw.get("isConnected"), raw.get("isRecording")
    return Camera(raw["id"], norm_mac(raw.get("mac")), _text(raw.get("name")),
                  _text(raw.get("modelKey")) or _text(raw.get("model")),
                  _text(raw.get("state")).upper(), conn if isinstance(conn, bool) else None,
                  rec if isinstance(rec, bool) else None)


def _write_cameras(store: Store, cams: list[Camera], now: float) -> int:
    with store._lock:
        db = store._db
        try:
            db.executemany(
                """INSERT INTO unifi_cameras (camera_id, mac, name, model, state, connected,
                   recording, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (camera_id) DO UPDATE SET mac=excluded.mac, name=excluded.name,
                   model=excluded.model, state=excluded.state, connected=excluded.connected,
                   recording=excluded.recording, last_seen=excluded.last_seen""",
                [(c.camera_id, c.mac, c.name, c.model, c.state,
                  None if c.connected is None else int(c.connected),
                  None if c.recording is None else int(c.recording), now, now) for c in cams])
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return len(cams)


async def save_cameras(store: Store, cams: Iterable[Camera], now: float) -> int:
    return await asyncio.to_thread(_write_cameras, store, list(cams), now)


# ---- reads for the pages ----

def read_devices(store: Store) -> list[dict[str, Any]]:
    keys = ("site_id", "device_id", "mac", "name", "model", "state", "ip", "firmware",
            "firmware_updatable", "last_seen")
    with store._lock:
        rows = store._db.execute(
            f"SELECT {', '.join(keys)} FROM unifi_devices "
            "ORDER BY name COLLATE NOCASE, mac LIMIT ?", (MAX_ROWS,)).fetchall()
    out = [dict(zip(keys, r)) for r in rows]
    for d in out:
        fu = d["firmware_updatable"]
        d["firmware_updatable"] = None if fu is None else bool(fu)
    return out


def read_clients(store: Store) -> dict[str, Any]:
    """Clients with the name of the device they sit on, connected first. Capped at MAX_ROWS."""
    with store._lock:
        db = store._db
        total = db.execute("SELECT COUNT(*) FROM unifi_clients").fetchone()[0]
        rows = db.execute(
            """SELECT c.client_id, c.mac, c.name, c.ip, c.kind, c.connected, c.connected_at,
               c.ssid, c.sw_port, c.enriched, c.last_seen, c.uplink_device_id, c.uplink_mac,
               d.name, d.mac FROM unifi_clients c
               LEFT JOIN unifi_devices d ON d.site_id = c.site_id
                 AND ((c.uplink_device_id != '' AND d.device_id = c.uplink_device_id)
                      OR (c.uplink_device_id = '' AND c.uplink_mac != '' AND d.mac = c.uplink_mac))
               ORDER BY c.connected DESC, c.name COLLATE NOCASE, c.mac LIMIT ?""",
            (MAX_ROWS,)).fetchall()
    out = []
    for (cid, mac, name, ip, kind, conn, cat, ssid, port, enr, seen, up_id, up_mac, up_name,
         up_dev_mac) in rows:
        out.append({"client_id": cid, "mac": mac, "name": name, "ip": ip, "kind": kind,
                    "connected": None if conn is None else bool(conn), "connected_at": cat,
                    "ssid": ssid, "sw_port": port, "enriched": bool(enr), "last_seen": seen,
                    "uplink_device_id": up_id, "uplink_name": up_name or "",
                    "uplink_mac": up_dev_mac or up_mac})
    return {"total": total, "truncated": total > len(out), "clients": out}


def read_cameras(store: Store) -> dict[str, Any]:
    keys = ("camera_id", "mac", "name", "model", "state", "connected", "recording", "last_seen")
    with store._lock:
        rows = store._db.execute(
            f"SELECT {', '.join(keys)} FROM unifi_cameras "
            "ORDER BY name COLLATE NOCASE, camera_id LIMIT ?", (MAX_ROWS,)).fetchall()
    cams = []
    for r in rows:
        c = dict(zip(keys, r))
        for k in ("connected", "recording"):
            c[k] = None if c[k] is None else bool(c[k])
        cams.append(c)
    return {"cameras": cams}
