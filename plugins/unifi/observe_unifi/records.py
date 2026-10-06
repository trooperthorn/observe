"""The plugin's tables: the current snapshot of UniFi devices and clients.

One row per device or client, replaced on every poll. There is no per-poll history. A row keeps
`first_seen` from the poll that first saw it and `last_seen` from the latest poll that did, and
a row not seen for `retention_days` is deleted. `unifi_clients` is created here for the client
collector of a later slice; this slice writes only `unifi_devices`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from observe.plugins import Migration
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


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def parse_device(site_id: str, raw: Any) -> Device | None:
    """One device row of the Integration API list. A row without a string id is skipped.

    Field names (id, macAddress, name, model, state, ipAddress, firmwareVersion,
    firmwareUpdatable) follow ha_Int_soc docs/UNIFI-LOCAL-API-CONTRACT.md and its fakes.
    UNVERIFIED against a live console: that firmwareUpdatable is on the list row; an absent or
    non-boolean value is stored as NULL (unknown), never as false.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    fu = raw.get("firmwareUpdatable")
    return Device(site_id, raw["id"], _text(raw.get("macAddress")).lower(), _text(raw.get("name")),
                  _text(raw.get("model")), _text(raw.get("state")), _text(raw.get("ipAddress")),
                  _text(raw.get("firmwareVersion")), fu if isinstance(fu, bool) else None)


def _write(store: Store, devices: list[Device], now: float) -> int:
    with store._lock:
        db = store._db
        try:
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
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return len(devices)


async def save_devices(store: Store, devices: Iterable[Device], now: float) -> int:
    """Upsert the polled devices in one transaction. `first_seen` is kept for a known device."""
    return await asyncio.to_thread(_write, store, list(devices), now)


async def prune_unseen(store: Store, now: float, retention_days: int) -> int:
    """Delete devices and clients not seen for retention_days. Returns rows deleted."""
    cutoff = now - retention_days * 86400
    total = 0
    for table in ("unifi_devices", "unifi_clients"):
        total += await asyncio.to_thread(
            store._delete, f"DELETE FROM {table} WHERE last_seen < ?", (cutoff,))
    return total
