"""The plugin's tables: the current snapshot of UniFi devices and clients.

One row per device or client, replaced on every poll. There is no per-poll history. A row keeps
`first_seen` from the poll that first saw it and `last_seen` from the latest poll that did, and
a row not seen for `retention_days` is deleted. `unifi_clients` is one row per MAC and
`unifi_cameras` one row per Protect camera; see clients.py. `unifi_site_status` is one row per
site (gateway, Internet, WAN, the gateway's network table) and `unifi_wlans` one row per SSID;
both are current state, replaced on every poll.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from observe.plugins import Migration
from observe.storage import Conn
from observe.store import Store

MIGRATIONS = (
    Migration(1, (
        """CREATE TABLE IF NOT EXISTS unifi_devices (
  site_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  mac TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT '',
  firmware TEXT NOT NULL DEFAULT '',
  firmware_updatable INTEGER,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  PRIMARY KEY (site_id, device_id)
)""",
        "CREATE INDEX IF NOT EXISTS unifi_devices_seen ON unifi_devices (last_seen)",
        """CREATE TABLE IF NOT EXISTS unifi_clients (
  site_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  mac TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  uplink_device_id TEXT NOT NULL DEFAULT '',
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  PRIMARY KEY (site_id, client_id)
)""",
        "CREATE INDEX IF NOT EXISTS unifi_clients_seen ON unifi_clients (last_seen)",
    )),
    # Version 2: what the clients and Protect collectors keep (docs/FIELD-DATA.md). `connected`
    # is NULL when unknown. `client_id` is the lower-case MAC when the row has one, so a client
    # is one row however it was seen.
    Migration(2, (
        "ALTER TABLE unifi_clients ADD COLUMN connected INTEGER",
        "ALTER TABLE unifi_clients ADD COLUMN connected_at REAL",
        "ALTER TABLE unifi_clients ADD COLUMN ssid TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE unifi_clients ADD COLUMN uplink_mac TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE unifi_clients ADD COLUMN sw_port INTEGER",
        "ALTER TABLE unifi_clients ADD COLUMN enriched INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS unifi_cameras (
  camera_id TEXT NOT NULL PRIMARY KEY,
  mac TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '',
  connected INTEGER,
  recording INTEGER,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL
)""",
        "CREATE INDEX IF NOT EXISTS unifi_cameras_seen ON unifi_cameras (last_seen)",
    )),
    # Version 3: when the classic detail (ssid, uplink MAC, switch port) was last read. A failed
    # classic poll keeps the old detail and this time instead of erasing it.
    Migration(3, (
        "ALTER TABLE unifi_clients ADD COLUMN classic_seen REAL",
    )),
    # Version 4: the network view. Client VLAN, network, uptime, rates and byte totals (classic);
    # device type, role, features, rates and totals (Integration list row, where given); one
    # site status row (gateway, Internet, WAN) written by the devices and classic collectors,
    # each only to its own columns; and the SSIDs (classic rest/wlanconf) for Wi-Fi readiness.
    # Rates are bytes per second as the console reports them (UNVERIFIED unit, see
    # docs/FIELD-DATA.md); a NULL is unknown, never zero.
    Migration(4, (
        "ALTER TABLE unifi_clients ADD COLUMN vlan INTEGER",
        "ALTER TABLE unifi_clients ADD COLUMN network TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE unifi_clients ADD COLUMN uptime_s INTEGER",
        "ALTER TABLE unifi_clients ADD COLUMN rx_rate_bps REAL",
        "ALTER TABLE unifi_clients ADD COLUMN tx_rate_bps REAL",
        "ALTER TABLE unifi_clients ADD COLUMN rx_bytes INTEGER",
        "ALTER TABLE unifi_clients ADD COLUMN tx_bytes INTEGER",
        "ALTER TABLE unifi_devices ADD COLUMN device_type TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE unifi_devices ADD COLUMN role TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE unifi_devices ADD COLUMN features TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE unifi_devices ADD COLUMN rx_bytes INTEGER",
        "ALTER TABLE unifi_devices ADD COLUMN tx_bytes INTEGER",
        "ALTER TABLE unifi_devices ADD COLUMN rx_rate_bps REAL",
        "ALTER TABLE unifi_devices ADD COLUMN tx_rate_bps REAL",
        """CREATE TABLE IF NOT EXISTS unifi_site_status (
  site_id TEXT NOT NULL PRIMARY KEY,
  gateway_device_id TEXT NOT NULL DEFAULT '',
  internet_up INTEGER,
  wan_ip TEXT NOT NULL DEFAULT '',
  wan_port TEXT NOT NULL DEFAULT '',
  wan_rx_rate_bps REAL,
  wan_tx_rate_bps REAL,
  wan_latency_ms REAL,
  networks TEXT NOT NULL DEFAULT '',
  updated REAL,
  classic_updated REAL
)""",
        """CREATE TABLE IF NOT EXISTS unifi_wlans (
  site_id TEXT NOT NULL,
  wlan_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  enabled INTEGER,
  security TEXT NOT NULL DEFAULT '',
  network_id TEXT NOT NULL DEFAULT '',
  network_name TEXT NOT NULL DEFAULT '',
  vlan INTEGER,
  ap_group_mode TEXT NOT NULL DEFAULT '',
  ap_names TEXT NOT NULL DEFAULT '',
  guest INTEGER,
  band TEXT NOT NULL DEFAULT '',
  hidden INTEGER,
  mac_filter TEXT NOT NULL DEFAULT '',
  scheduled INTEGER,
  updated REAL NOT NULL,
  PRIMARY KEY (site_id, wlan_id)
)""",
    )),
    # Version 5: when a device was last ONLINE. `last_seen` moves with every poll that lists the
    # device, offline or not, so it said nothing about an offline device (bug plan WP8). NULL
    # for a device never seen online; the page then shows no time rather than the poll time.
    Migration(5, (
        "ALTER TABLE unifi_devices ADD COLUMN online_at REAL",
        "UPDATE unifi_devices SET online_at = last_seen WHERE state = 'ONLINE'",
    )),
    # Version 6: the site's name from the Integration /sites list, so the overview names the
    # site instead of showing its id. Empty until the next devices poll.
    Migration(6, (
        "ALTER TABLE unifi_site_status ADD COLUMN site_name TEXT NOT NULL DEFAULT ''",
    )),
)


@dataclass(frozen=True)
class Device:
    site_id: str
    device_id: str
    mac: str
    name: str
    model: str
    state: str
    ip: str
    firmware: str
    firmware_updatable: bool | None
    # The id of the device this one is uplinked to, for the map feed. Not stored in the table.
    uplink_device_id: str = ""
    # Version 4 columns. `device_type` is the row's `type` or `deviceType`, `role` its `role`
    # (UNVERIFIED: the Network 10.4.57 contract lists no role field; both are kept as given so
    # the gateway can be picked the way ha_Int_soc picks it). `features` is the row's list
    # (ACCESS_POINT, SWITCHING, GATEWAY ... UNVERIFIED values), kept as JSON text.
    device_type: str = ""
    role: str = ""
    features: tuple[str, ...] = ()
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_rate_bps: float | None = None
    tx_rate_bps: float | None = None
    # The port of the parent device this one is uplinked through, when the row or detail names
    # it (UPLINK_PORT_KEYS, UNVERIFIED). The map feed then names that port "Port N" instead of a
    # placeholder. Not stored in the table.
    uplink_port_idx: int | None = None


# Keys of the `uplink` object that may give the parent's port index (UNVERIFIED: the Network
# 10.4.57 contract shows only uplink.deviceId; these are the spellings other UniFi APIs use).
UPLINK_PORT_KEYS = ("portIdx", "portIndex", "port_idx", "uplinkPortIdx")


def uplink_port_of(up: Any) -> int | None:
    """The parent's port index from an `uplink` object, or None."""
    if not isinstance(up, dict):
        return None
    for key in UPLINK_PORT_KEYS:
        v = up.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1024:
            return v
    return None


GATEWAY_ROLES = ("gateway", "console", "ugw")
# A model or name token that marks a gateway, consulted only when no device declares a role.
GATEWAY_TOKENS = ("gateway", "udm", "uxg", "usg", "ugw", "ucg", "udr", "uxr", "udw", "efg",
                  "dream", "console")


# The map device type of a UniFi device (observe.infra.DEVICE_TYPES). Names are compared
# without case, spaces, dashes or underscores, so ACCESS_POINT, accessPoint, "UCG Fiber",
# "UCG-Fiber" and "UCGFIBER" each read the same (UNVERIFIED which spelling a console sends).
FEATURE_TYPES = (("switching", "switch"), ("accesspoint", "access_point"))
ROLE_TYPES = {"gateway": "gateway", "console": "gateway", "ugw": "gateway", "udm": "gateway",
              "uxg": "gateway", "switch": "switch", "usw": "switch", "accesspoint": "access_point",
              "ap": "access_point", "uap": "access_point", "bridge": "bridge", "ubb": "bridge",
              "udb": "bridge"}
# Model codes that settle the type even against a SWITCHING or ACCESS_POINT feature: a UniFi
# gateway or console switches too (a UCG Fiber was drawn as a switch), and a Device Bridge
# (UDB, e.g. "UDB Pro" or UDBPRO) or Building Bridge (UBB) carries an access point radio.
MODEL_STRONG = (("gateway", ("ucg", "udm", "udr", "uxg", "efg", "usg", "udw", "uxr")),
                ("bridge", ("udb", "ubb")))
MODEL_TYPES = (("access_point", ("u6", "u7", "uap", "ual")),
               ("switch", ("usw", "us8", "us16", "us24", "us48")))
LEGACY_SWITCH = "us-"  # US-8-60W and the other first generation switches, matched as written


def _squash(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _model_kind(model: str, table: tuple[tuple[str, tuple[str, ...]], ...]) -> str:
    name = _squash(model or "")
    for kind, prefixes in table:
        if name.startswith(prefixes):
            return kind
    return ""


def device_type_of(features: Iterable[str] = (), device_type: str = "", role: str = "",
                   model: str = "") -> str:
    """gateway, switch, access_point, bridge or other, or empty when nothing says. In order: a
    GATEWAY feature or a gateway or bridge type or role; then a gateway model code (UCG, UDM,
    UDR, UXG, EFG, USG, UDW, UXR) or bridge code (UDB, UBB), which win over a SWITCHING or
    ACCESS_POINT feature because those devices also switch or carry a radio; then the
    SWITCHING and ACCESS_POINT features; then any other type or role; then an access point
    (U6, U7, UAP, UAL) or switch (USW, US-) model. A device that names features or a type
    that none of these match is other."""
    feats = {_squash(f) for f in features if isinstance(f, str)}
    if "gateway" in feats:
        return "gateway"
    roles = [ROLE_TYPES.get(_squash(text or ""), "") for text in (device_type, role)]
    for kind in roles:
        if kind in ("gateway", "bridge"):
            return kind
    strong = _model_kind(model, MODEL_STRONG)
    if strong:
        return strong
    for feat, kind in FEATURE_TYPES:
        if feat in feats:
            return kind
    for kind in roles:
        if kind:
            return kind
    weak = _model_kind(model, MODEL_TYPES)
    if weak:
        return weak
    if (model or "").strip().lower().startswith(LEGACY_SWITCH):
        return "switch"
    return "other" if feats or device_type or role else ""


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _first(obj: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = obj.get(k)
        if v not in (None, ""):
            return v
    return None


def counters(raw: dict[str, Any]) -> dict[str, Any]:
    """Byte totals and rates of a device or statistics row, searched at the top level and under
    `statistics`, `uplink` and `statistics.uplink` (the HA SOC search order). Keys rxBytes,
    txBytes, rxRateBps, txRateBps and their snake_case and hyphenated classic forms. UNVERIFIED
    against a live console: the contract notes the counters as nested under `statistics` and a
    device's under `statistics.uplink` without having seen them. Rates are taken as bytes per
    second (the Bps of the key), also unverified."""
    nodes: list[dict[str, Any]] = [raw]
    for key in ("statistics", "uplink"):
        node = raw.get(key)
        if isinstance(node, dict):
            nodes.append(node)
            inner = node.get("uplink")
            if isinstance(inner, dict):
                nodes.append(inner)
    out: dict[str, Any] = {"rx_bytes": None, "tx_bytes": None, "rx_rate_bps": None,
                           "tx_rate_bps": None}
    for node in nodes:
        rx = _int(_first(node, "rxBytes", "rx_bytes"))
        tx = _int(_first(node, "txBytes", "tx_bytes"))
        rxr = _num(_first(node, "rxRateBps", "rx_rate_bps", "rx_bytes-r", "rx_bytes_r"))
        txr = _num(_first(node, "txRateBps", "tx_rate_bps", "tx_bytes-r", "tx_bytes_r"))
        if out["rx_bytes"] is None and out["tx_bytes"] is None and (rx is not None or tx is not None):
            out["rx_bytes"], out["tx_bytes"] = rx, tx
        if (out["rx_rate_bps"] is None and out["tx_rate_bps"] is None
                and (rxr is not None or txr is not None)):
            out["rx_rate_bps"], out["tx_rate_bps"] = rxr, txr
    return out


def gateway_by_role(raw: dict[str, Any]) -> bool:
    """The strong signal: the row's own type, deviceType or role says gateway."""
    role = str(_first(raw, "type", "deviceType", "role") or "").lower()
    return role in GATEWAY_ROLES


def gateway_by_tokens(raw: dict[str, Any]) -> bool:
    """The weak fallback: a gateway marker in the type, model, name or role text."""
    blob = " ".join(str(_first(raw, k) or "").lower()
                    for k in ("type", "model", "shortname", "name", "deviceType", "role"))
    return any(tok in blob for tok in GATEWAY_TOKENS)


def select_gateway(rows: list[Any]) -> dict[str, Any] | None:
    """The site's gateway among the Integration device rows, the way ha_Int_soc picks it: a
    declared role wins; model and name tokens are consulted only when no row declares one."""
    dicts = [r for r in rows if isinstance(r, dict)]
    for r in dicts:
        if gateway_by_role(r):
            return r
    for r in dicts:
        if gateway_by_tokens(r):
            return r
    return None


def parse_uplink_stats(stats: Any) -> dict[str, Any]:
    """The WAN side of `GET /devices/{id}/statistics/latest` for the gateway: the uplink rates
    and the uplink interface name, and `up` when the node carries a boolean. The route is in the
    Network 10.4.57 contract; the `uplink` node's keys (rxRateBps, txRateBps, name) are
    UNVERIFIED against a live console. Every value is None when absent."""
    out: dict[str, Any] = {"rx_rate_bps": None, "tx_rate_bps": None, "port": "", "up": None}
    if not isinstance(stats, dict):
        return out
    body = stats.get("data") if isinstance(stats.get("data"), dict) else stats
    node = body.get("uplink") if isinstance(body.get("uplink"), dict) else None
    if node is None:
        return out
    c = counters({"uplink": node})
    out["rx_rate_bps"], out["tx_rate_bps"] = c["rx_rate_bps"], c["tx_rate_bps"]
    out["port"] = _text(_first(node, "name", "ifname", "interface", "port"))
    up = _first(node, "up", "isUp", "connected")
    out["up"] = up if isinstance(up, bool) else None
    return out


def parse_device(site_id: str, raw: Any) -> Device | None:
    """One device row of the Integration API list. A row without a string id is skipped.

    Field names (id, macAddress, name, model, state, ipAddress, firmwareVersion,
    firmwareUpdatable) follow ha_Int_soc docs/UNIFI-LOCAL-API-CONTRACT.md and its fakes.
    UNVERIFIED against a live console: that firmwareUpdatable is on the list row; an absent or
    non-boolean value is stored as NULL (unknown), never as false. A live console's list rows
    carry no uplink (bug plan WP4: the map had devices but no links), so the devices poll reads
    `uplink.deviceId` from each device's detail and sets it on the row before this runs
    (`UniFiPlugin._uplinks`); a row that names `uplink.deviceId` or `uplinkDeviceId` itself is used
    as it is. When neither is a string the device has no device-level link.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    fu = raw.get("firmwareUpdatable")
    up = raw.get("uplink")
    uplink = _text(up.get("deviceId")) if isinstance(up, dict) else ""
    uplink = uplink or _text(raw.get("uplinkDeviceId"))
    feats = raw.get("features")
    c = counters(raw)
    return Device(site_id, raw["id"], _text(raw.get("macAddress")).lower(), _text(raw.get("name")),
                  _text(raw.get("model")), _text(raw.get("state")), _text(raw.get("ipAddress")),
                  _text(raw.get("firmwareVersion")), fu if isinstance(fu, bool) else None, uplink,
                  _text(raw.get("type")) or _text(raw.get("deviceType")), _text(raw.get("role")),
                  tuple(str(f) for f in feats if isinstance(f, str)) if isinstance(feats, list)
                  else (), c["rx_bytes"], c["tx_bytes"], c["rx_rate_bps"], c["tx_rate_bps"],
                  uplink_port_of(up))


def write_devices(db: Conn, devices: list[Device], now: float) -> int:
    """Upsert the polled devices on an open unit."""
    for d in devices:
        db.execute(
            """INSERT INTO unifi_devices (site_id, device_id, mac, name, model, state, ip,
               firmware, firmware_updatable, device_type, role, features, rx_bytes, tx_bytes,
               rx_rate_bps, tx_rate_bps, first_seen, last_seen, online_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (site_id, device_id) DO UPDATE SET
               mac=excluded.mac, name=excluded.name, model=excluded.model,
               state=excluded.state, ip=excluded.ip, firmware=excluded.firmware,
               firmware_updatable=excluded.firmware_updatable,
               device_type=excluded.device_type, role=excluded.role, features=excluded.features,
               rx_bytes=excluded.rx_bytes, tx_bytes=excluded.tx_bytes,
               rx_rate_bps=excluded.rx_rate_bps, tx_rate_bps=excluded.tx_rate_bps,
               last_seen=excluded.last_seen,
               online_at=COALESCE(excluded.online_at, unifi_devices.online_at)""",
            (d.site_id, d.device_id, d.mac, d.name, d.model, d.state, d.ip, d.firmware,
             None if d.firmware_updatable is None else int(d.firmware_updatable),
             d.device_type, d.role, json.dumps(list(d.features)) if d.features else "",
             d.rx_bytes, d.tx_bytes, d.rx_rate_bps, d.tx_rate_bps, now, now,
             now if d.state.upper() == "ONLINE" else None))
    return len(devices)


def _flag(v: bool | None) -> int | None:
    return None if v is None else int(v)


def write_site_status_integration(db: Conn, site_id: str, gateway_device_id: str,
                                  wan: dict[str, Any], now: float) -> None:
    """The devices poll's share of `unifi_site_status`: the gateway and the uplink rates, port
    and (when the statistics node says) Internet state, and the site's name when `wan` carries
    `site_name` (from the /sites list). The classic columns are left as they are. With no
    gateway the row still records the poll time, so the overview can say it ran."""
    db.execute(
        """INSERT INTO unifi_site_status (site_id, gateway_device_id, wan_port, wan_rx_rate_bps,
           wan_tx_rate_bps, internet_up, updated, site_name) VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT (site_id) DO UPDATE SET gateway_device_id=excluded.gateway_device_id,
           site_name=CASE WHEN excluded.site_name != '' THEN excluded.site_name
                          ELSE unifi_site_status.site_name END,
           wan_port=CASE WHEN excluded.wan_port != '' THEN excluded.wan_port
                         ELSE unifi_site_status.wan_port END,
           wan_rx_rate_bps=excluded.wan_rx_rate_bps, wan_tx_rate_bps=excluded.wan_tx_rate_bps,
           internet_up=CASE WHEN excluded.internet_up IS NULL THEN unifi_site_status.internet_up
                            ELSE excluded.internet_up END,
           updated=excluded.updated""",
        (site_id, gateway_device_id, wan.get("port") or "", wan.get("rx_rate_bps"),
         wan.get("tx_rate_bps"), _flag(wan.get("up")), now, _text(wan.get("site_name"))))


def write_site_status_classic(db: Conn, site_id: str, wan: dict[str, Any],
                              networks: list[dict[str, Any]], now: float) -> None:
    """The classic poll's share: Internet state, WAN address, latency, and the uplink port and
    rates when the Integration poll gave none, plus the gateway's network table as JSON for VLAN
    resolution. `wan` is `classic.wan_status`'s shape."""
    db.execute(
        """INSERT INTO unifi_site_status (site_id, internet_up, wan_ip, wan_port, wan_rx_rate_bps,
           wan_tx_rate_bps, wan_latency_ms, networks, classic_updated)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT (site_id) DO UPDATE SET
           internet_up=CASE WHEN excluded.internet_up IS NULL THEN unifi_site_status.internet_up
                            ELSE excluded.internet_up END,
           wan_ip=excluded.wan_ip,
           wan_port=CASE WHEN unifi_site_status.wan_port = '' THEN excluded.wan_port
                         ELSE unifi_site_status.wan_port END,
           wan_rx_rate_bps=CASE WHEN unifi_site_status.updated IS NULL
                                OR unifi_site_status.wan_rx_rate_bps IS NULL
                                THEN excluded.wan_rx_rate_bps
                                ELSE unifi_site_status.wan_rx_rate_bps END,
           wan_tx_rate_bps=CASE WHEN unifi_site_status.updated IS NULL
                                OR unifi_site_status.wan_tx_rate_bps IS NULL
                                THEN excluded.wan_tx_rate_bps
                                ELSE unifi_site_status.wan_tx_rate_bps END,
           wan_latency_ms=excluded.wan_latency_ms, networks=excluded.networks,
           classic_updated=excluded.classic_updated""",
        (site_id, _flag(wan.get("up")), wan.get("ip") or "", wan.get("port") or "",
         wan.get("rx_rate_bps"), wan.get("tx_rate_bps"), wan.get("latency_ms"),
         json.dumps(networks) if networks else "", now))


def read_networks(db: Conn, site_id: str) -> list[dict[str, Any]]:
    """The gateway's network table stored by the last classic poll, or an empty list."""
    row = db.execute("SELECT networks FROM unifi_site_status WHERE site_id=?",
                     (site_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        got = json.loads(row[0])
    except ValueError:
        return []
    return [n for n in got if isinstance(n, dict)] if isinstance(got, list) else []


def write_wlans(db: Conn, site_id: str, wlans: list[dict[str, Any]], now: float) -> int:
    """Replace the site's SSID rows with this poll's (`classic.parse_wlans` shape, with
    `network_name`, `vlan` and `ap_names` resolved by the caller)."""
    db.executemany(
        """INSERT INTO unifi_wlans (site_id, wlan_id, name, enabled, security, network_id,
           network_name, vlan, ap_group_mode, ap_names, guest, band, hidden, mac_filter,
           scheduled, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (site_id, wlan_id) DO UPDATE SET name=excluded.name,
           enabled=excluded.enabled, security=excluded.security, network_id=excluded.network_id,
           network_name=excluded.network_name, vlan=excluded.vlan,
           ap_group_mode=excluded.ap_group_mode, ap_names=excluded.ap_names,
           guest=excluded.guest, band=excluded.band, hidden=excluded.hidden,
           mac_filter=excluded.mac_filter, scheduled=excluded.scheduled,
           updated=excluded.updated""",
        [(site_id, w["id"], w["name"], _flag(w["enabled"]), w["security"], w["network_id"],
          w.get("network_name", ""), w.get("vlan"), w["ap_group_mode"],
          json.dumps(w.get("ap_names", [])), _flag(w["guest"]), w["band"], _flag(w["hidden"]),
          w["mac_filter"], _flag(w["scheduled"]), now) for w in wlans])
    db.execute("DELETE FROM unifi_wlans WHERE site_id=? AND updated < ?", (site_id, now))
    return len(wlans)


async def prune_unseen(store: Store, now: float, retention_days: int) -> int:
    """Delete devices, clients and cameras not seen for retention_days. Returns rows deleted."""
    cutoff = now - retention_days * 86400
    total = 0
    for table in ("unifi_devices", "unifi_clients", "unifi_cameras"):
        total += await store.storage.write(lambda db, t=table: db.execute(
            f"DELETE FROM {t} WHERE last_seen < ?", (cutoff,)).rowcount)
    return total
