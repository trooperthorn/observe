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

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from observe.storage import Conn
from observe.store import Store

from .classic import resolve_client_vlan, uptime_to_seconds

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
    # Classic detail of a connected client (version 4). None is unknown, never zero.
    vlan: int | None = None
    network: str = ""
    uptime_s: int | None = None
    rx_rate_bps: float | None = None  # bytes per second as the console counts them
    tx_rate_bps: float | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None


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


def _rate(v: Any) -> float | None:
    """A classic counter that may be a number or a numeric string; None otherwise."""
    n = _num(v)
    if n is None and isinstance(v, str):
        try:
            n = float(v)
        except ValueError:
            n = None
    return n


def _pair(r: dict[str, Any], rx: str, tx: str) -> tuple[float | None, float | None]:
    """A rate pair under its hyphenated key or the underscore spelling ha_Int_soc reads."""
    rxv = _rate(r.get(rx)) if r.get(rx) is not None else _rate(r.get(rx.replace("-", "_")))
    txv = _rate(r.get(tx)) if r.get(tx) is not None else _rate(r.get(tx.replace("-", "_")))
    return rxv, txv


def client_rates(r: dict[str, Any], wired: bool | None) -> tuple[float | None, float | None]:
    """The live rate pair of a stat/sta row: a wired client's `wired-rx_bytes-r` pair first,
    a wireless client's `rx_bytes-r` pair first, and the other pair as the fallback (the core
    wired bug leaves a wired flag on a wireless client). Ported from unifi_core.client_bandwidth
    with both key spellings. Bytes per second, UNVERIFIED."""
    wired_pair = ("wired-rx_bytes-r", "wired-tx_bytes-r")
    air_pair = ("rx_bytes-r", "tx_bytes-r")
    for rx, tx in (wired_pair, air_pair) if wired else (air_pair, wired_pair):
        got = _pair(r, rx, tx)
        if got != (None, None):
            return got
    return None, None


def client_totals(r: dict[str, Any], wired: bool | None) -> tuple[int | None, int | None]:
    """Cumulative bytes of a stat/sta row, with the same wired-first rule (UNVERIFIED keys
    rx_bytes, tx_bytes, wired-rx_bytes, wired-tx_bytes)."""
    pairs = (("wired-rx_bytes", "wired-tx_bytes"), ("rx_bytes", "tx_bytes"))
    for rx, tx in pairs if wired else reversed(pairs):
        rxv, txv = _pair(r, rx, tx)
        if (rxv, txv) != (None, None):
            return (None if rxv is None else int(rxv)), (None if txv is None else int(txv))
    return None, None


def parse_active_clients(rows: list[Any], now: float | None = None) -> dict[str, dict[str, Any]]:
    """`stat/sta` rows by MAC. UNVERIFIED against a live console: `ap_mac` (a wireless client's
    access point), `sw_mac` and `sw_port` (a wired client's switch and port number), `essid`,
    `is_wired`, `hostname`, `name`, `vlan`, `network`, `network_id`, `uptime` and the byte
    counters (client_rates, client_totals). ha_Int_soc names `ap_mac` for the core client only.
    `uptime` is turned into seconds with the epoch rule when `now` is given."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        mac = norm_mac(r.get("mac"))
        if not mac:
            continue
        wired = r.get("is_wired") if isinstance(r.get("is_wired"), bool) else None
        first, second = ("sw_mac", "ap_mac") if wired else ("ap_mac", "sw_mac")
        rx_r, tx_r = client_rates(r, wired)
        rx_b, tx_b = client_totals(r, wired)
        out[mac] = {"uplink_mac": norm_mac(r.get(first)) or norm_mac(r.get(second)),
                    "sw_port": _int(r.get("sw_port")) if wired is not False else None,
                    "ssid": _text(r.get("essid")), "wired": wired,
                    "name": _text(r.get("name")) or _text(r.get("hostname")),
                    "vlan": _int(r.get("vlan")), "network": _text(r.get("network")),
                    "network_id": _text(r.get("network_id")),
                    "uptime_s": uptime_to_seconds(r.get("uptime"), now) if now is not None
                    else _int(r.get("uptime")),
                    "rx_rate_bps": rx_r, "tx_rate_bps": tx_r, "rx_bytes": rx_b, "tx_bytes": tx_b}
    return out


def enrich(clients: list[Client], active: dict[str, dict[str, Any]],
           devices_by_mac: dict[str, str],
           networks: list[dict[str, Any]] | None = None) -> list[Client]:
    """Add classic detail to connected clients and resolve an uplink MAC to a device id. The
    VLAN is resolved through the gateway's network table (`networks`) when the row has none."""
    out = []
    for c in clients:
        e = active.get(c.mac)
        if e is None:
            out.append(c)
            continue
        kind = c.kind or {True: "wired", False: "wireless"}.get(e["wired"], "")
        vlan, network = resolve_client_vlan(e, networks or [])
        out.append(replace(
            c, kind=kind, name=c.name or e["name"], ssid=e["ssid"], uplink_mac=e["uplink_mac"],
            sw_port=e["sw_port"], enriched=True,
            uplink_device_id=c.uplink_device_id or devices_by_mac.get(e["uplink_mac"], ""),
            vlan=vlan, network=network, uptime_s=e.get("uptime_s"),
            rx_rate_bps=e.get("rx_rate_bps"), tx_rate_bps=e.get("tx_rate_bps"),
            rx_bytes=e.get("rx_bytes"), tx_bytes=e.get("tx_bytes")))
    return out


def offline_clients(site_id: str, rows: Iterable[dict[str, Any]], connected: set[str],
                    now: float, retention_days: int) -> list[Client]:
    """Known but not connected clients from `parse_offline_clients`. One older than the retention
    is left out, so the prune does not delete it only for the next poll to add it back. A client
    with no `last_seen` is aged by the console's `first_seen` instead; with neither, it is kept
    with `last_seen` None, which the store does not refresh, so the prune can remove it."""
    cutoff = now - retention_days * 86400
    out = []
    for r in rows:
        mac = norm_mac(r.get("mac"))
        seen = _num(r.get("last_seen"))  # UNVERIFIED: epoch seconds
        first = _num(r.get("first_seen"))  # UNVERIFIED: epoch seconds
        age_from = seen if seen is not None else first
        if not mac or mac in connected or (age_from is not None and age_from < cutoff):
            continue
        out.append(Client(site_id, mac, mac, _text(r.get("name")), connected=False,
                          last_seen=seen))
    return out


def _up_sql(keep_classic: bool) -> str:
    """The upsert of a connected client. With `keep_classic`, a row without classic detail keeps
    the ssid, uplink MAC, switch port and their time already stored, because the classic read
    failed and a blank would be wrong. Otherwise the poll's values replace them."""
    def col(name: str) -> str:
        if not keep_classic:
            return f"{name}=excluded.{name}"
        return f"{name}=CASE WHEN excluded.enriched=1 THEN excluded.{name} ELSE unifi_clients.{name} END"
    classic = ("ssid", "uplink_mac", "sw_port", "vlan", "network", "uptime_s", "rx_rate_bps",
               "tx_rate_bps", "rx_bytes", "tx_bytes", "enriched", "classic_seen")
    return f"""INSERT INTO unifi_clients (site_id, client_id, mac, name, ip, kind, uplink_device_id,
  connected, connected_at, ssid, uplink_mac, sw_port, vlan, network, uptime_s, rx_rate_bps,
  tx_rate_bps, rx_bytes, tx_bytes, enriched, classic_seen, first_seen, last_seen)
  VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  ON CONFLICT (site_id, client_id) DO UPDATE SET mac=excluded.mac, name=excluded.name,
  ip=excluded.ip, kind=excluded.kind, connected=1, connected_at=excluded.connected_at,
  uplink_device_id=excluded.uplink_device_id,
  {", ".join(col(c) for c in classic)}, last_seen=excluded.last_seen"""


# An offline row keeps what is already known and moves only the name and the last seen time. When
# the console gave no last seen time, the stored time is not refreshed, so the row ages out by
# retention instead of looking new on every poll. That choice is made in Python, with two
# statements, because PostgreSQL cannot infer the type of a bare `? IS NULL` parameter.
def _off_sql(known: bool) -> str:
    seen = ", last_seen=MAX(unifi_clients.last_seen, excluded.last_seen)" if known else ""
    return f"""INSERT INTO unifi_clients (site_id, client_id, mac, name, connected, first_seen,
  last_seen) VALUES (?,?,?,?,0,?,?)
  ON CONFLICT (site_id, client_id) DO UPDATE SET connected=0,
  name=CASE WHEN excluded.name != '' THEN excluded.name ELSE unifi_clients.name END{seen}"""


_OFF_KNOWN, _OFF_UNKNOWN = _off_sql(True), _off_sql(False)


def _write_clients(db: Conn, site_id: str, live: list[Client], off: list[Client],
                   now: float, classic_ok: bool = True) -> int:
    db.executemany(_up_sql(not classic_ok), [
        (c.site_id, c.client_id, c.mac, c.name, c.ip, c.kind, c.uplink_device_id,
         c.connected_at, c.ssid, c.uplink_mac, c.sw_port, c.vlan, c.network, c.uptime_s,
         c.rx_rate_bps, c.tx_rate_bps, c.rx_bytes, c.tx_bytes, int(c.enriched),
         now if c.enriched else None, now, now)
        for c in live])
    # Anything still marked connected that this poll did not write has left.
    db.execute("UPDATE unifi_clients SET connected=0 WHERE site_id=? AND connected=1 "
               "AND last_seen < ?", (site_id, now))
    for known, sql in ((True, _OFF_KNOWN), (False, _OFF_UNKNOWN)):
        db.executemany(sql, [
            (c.site_id, c.client_id, c.mac, c.name, c.last_seen if known else now,
             c.last_seen if known else now)
            for c in off if (c.last_seen is not None) == known])
    return len(live) + len(off)


async def save_clients(store: Store, site_id: str, live: list[Client], off: list[Client],
                       now: float, classic_ok: bool = True) -> int:
    """Upsert one poll in a single transaction. `first_seen` is kept for a known client. With
    `classic_ok` false (the classic read failed) the stored classic detail is kept."""
    return await store.storage.write(
        lambda db: _write_clients(db, site_id, live, off, now, classic_ok), touches=("unifi",))


async def device_index(store: Store, site_id: str) -> dict[str, str]:
    """MAC to device id for the devices of a site, to resolve a classic uplink MAC."""
    rows = await store.storage.read(lambda db: db.execute(
        "SELECT mac, device_id FROM unifi_devices WHERE site_id=? AND mac != ''",
        (site_id,)).fetchall())
    return {norm_mac(m): d for m, d in rows if norm_mac(m)}


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


def _write_cameras(db: Conn, cams: list[Camera], now: float) -> int:
    db.executemany(
        """INSERT INTO unifi_cameras (camera_id, mac, name, model, state, connected,
           recording, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT (camera_id) DO UPDATE SET mac=excluded.mac, name=excluded.name,
           model=excluded.model, state=excluded.state, connected=excluded.connected,
           recording=excluded.recording, last_seen=excluded.last_seen""",
        [(c.camera_id, c.mac, c.name, c.model, c.state,
          None if c.connected is None else int(c.connected),
          None if c.recording is None else int(c.recording), now, now) for c in cams])
    return len(cams)


async def save_cameras(store: Store, cams: Iterable[Camera], now: float) -> int:
    items = list(cams)
    return await store.storage.write(lambda db: _write_cameras(db, items, now),
                                     touches=("unifi",))
