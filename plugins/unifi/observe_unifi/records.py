"""The plugin's tables: the current snapshot of UniFi devices and clients.

One row per device or client, replaced on every poll. There is no per-poll history. A row keeps
`first_seen` from the poll that first saw it and `last_seen` from the latest poll that did, and
a row not seen for `retention_days` is deleted. `unifi_clients` is one row per MAC and
`unifi_cameras` one row per Protect camera; see clients.py.
"""

from __future__ import annotations

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


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def parse_device(site_id: str, raw: Any) -> Device | None:
    """One device row of the Integration API list. A row without a string id is skipped.

    Field names (id, macAddress, name, model, state, ipAddress, firmwareVersion,
    firmwareUpdatable) follow ha_Int_soc docs/UNIFI-LOCAL-API-CONTRACT.md and its fakes.
    UNVERIFIED against a live console: that firmwareUpdatable is on the list row; an absent or
    non-boolean value is stored as NULL (unknown), never as false. Also UNVERIFIED: that a device
    row names the device it is uplinked to, as `uplink.deviceId` or `uplinkDeviceId` (the contract
    verifies `uplinkDeviceId` only on client rows); when neither is a string the device has no
    device-level link.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    fu = raw.get("firmwareUpdatable")
    up = raw.get("uplink")
    uplink = _text(up.get("deviceId")) if isinstance(up, dict) else ""
    uplink = uplink or _text(raw.get("uplinkDeviceId"))
    return Device(site_id, raw["id"], _text(raw.get("macAddress")).lower(), _text(raw.get("name")),
                  _text(raw.get("model")), _text(raw.get("state")), _text(raw.get("ipAddress")),
                  _text(raw.get("firmwareVersion")), fu if isinstance(fu, bool) else None, uplink)


def _write(db: Conn, devices: list[Device], now: float) -> int:
    for d in devices:
        db.execute(
            """INSERT INTO unifi_devices (site_id, device_id, mac, name, model, state, ip,
               firmware, firmware_updatable, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (site_id, device_id) DO UPDATE SET
               mac=excluded.mac, name=excluded.name, model=excluded.model,
               state=excluded.state, ip=excluded.ip, firmware=excluded.firmware,
               firmware_updatable=excluded.firmware_updatable,
               last_seen=excluded.last_seen""",
            (d.site_id, d.device_id, d.mac, d.name, d.model, d.state, d.ip, d.firmware,
             None if d.firmware_updatable is None else int(d.firmware_updatable),
             now, now))
    return len(devices)


async def save_devices(store: Store, devices: Iterable[Device], now: float) -> int:
    """Upsert the polled devices in one transaction. `first_seen` is kept for a known device."""
    items = list(devices)
    return await store.storage.write(lambda db: _write(db, items, now), touches=("unifi",))


async def prune_unseen(store: Store, now: float, retention_days: int) -> int:
    """Delete devices, clients and cameras not seen for retention_days. Returns rows deleted."""
    cutoff = now - retention_days * 86400
    total = 0
    for table in ("unifi_devices", "unifi_clients", "unifi_cameras"):
        total += await store.storage.write(lambda db, t=table: db.execute(
            f"DELETE FROM {t} WHERE last_seen < ?", (cutoff,)).rowcount)
    return total
