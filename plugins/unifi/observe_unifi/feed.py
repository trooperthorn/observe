"""The map feed: UniFi devices, ports and links written into the infrastructure map.

Two feeds, both through the core `InfraTx` (docs/FIELD-DATA.md), never by SQL of their own for a
write, and each one cycle in one write unit and so one commit (docs/DATA-API-DESIGN.md section
1.2, slice O-5):

- feed_integration takes the Integration API device list. Each device becomes a switch keyed by
  its chassis MAC (switch_id), with its name, address and model. A device that names the device
  it is uplinked to gets a device-level link of source `config`, drawn between a port named
  `uplink` on the child and a port named `to-<child mac>` on the parent, because a link joins
  ports. That placeholder link is skipped when a link with real port numbers already joins the
  two devices. The poll's device rows (`save_devices`) go in the same unit as the feed.
- feed_classic takes the parsed classic views (classic.py) and is used only when the optional
  classic credential is set. It adds the ports, keyed by unifi_port_key(port_idx) with
  `unifi_index` set, the properties link_speed_mbps, poe_class, poe_load_w and vlan with source
  `unifi`, a `config` link for the uplink port numbers and an `lldp` link for each LLDP
  neighbour that is a known UniFi device or an already known switch.

Every device, port and link is one item with its own SAVEPOINT. An item that fails is rolled
back to its savepoint, counted in `skipped`, logged and left behind, and the rest of the cycle is kept
and committed. A database failure is not an item failure: it fails the cycle. The map tables are brought up to date in the same unit, so a reader never sees
the feed's rows without the map that shows them.

Properties go through InfraTx.append_property, which adds a history row only when the value
differs from this source's last one and otherwise only moves last_verified. A neighbour that is
not already known is not created, so cameras and phones that speak LLDP do not become switches.
Nothing here creates or changes a monitor.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from observe.infra import InfraError, InfraTx, write_cycle
from observe.portkey import mac_digits, port_key, switch_id, unifi_port_key
from observe.storage import Conn, savepoint
from observe.store import Store

from .records import Device, write_devices, write_site_status_integration

log = logging.getLogger(__name__)

SOURCE = "unifi"
VENDOR = "Ubiquiti"
UPLINK_PORT = "uplink"
CONFIG_CONFIDENCE = 0.8
DEVICE_LINK_CONFIDENCE = 0.5
# What a malformed or refused item can raise. Anything else, a database failure included, stops
# the cycle so the poll fails and is reported instead of every item being skipped unseen.
ITEM_ERRORS = (InfraError, ValueError, KeyError, TypeError)


@dataclass
class FeedResult:
    switches: int = 0
    ports: int = 0
    properties_added: int = 0
    links: int = 0
    skipped: int = 0

    def add(self, other: "FeedResult") -> None:
        self.switches += other.switches
        self.ports += other.ports
        self.properties_added += other.properties_added
        self.links += other.links
        self.skipped += other.skipped

    def as_detail(self) -> dict[str, Any]:
        return {"switches": self.switches, "ports": self.ports,
                "properties_added": self.properties_added, "links": self.links,
                "skipped": self.skipped}


def _sid(mac: str) -> str | None:
    return switch_id(mac) if mac_digits(mac) else None


def _joined_by_ports(db: Conn, a: str, b: str) -> bool:
    """True when an open link with real port numbers joins switches a and b."""
    rows = db.execute(
        "SELECT a_ref, b_ref FROM infra_links WHERE a_kind='port' AND b_kind='port' "
        "AND closed_at IS NULL AND (a_ref LIKE ? OR b_ref LIKE ?)",
        (f"{a}|%", f"{a}|%")).fetchall()
    for ra, rb in rows:
        sa, _, ka = ra.partition("|")
        sb, _, kb = rb.partition("|")
        placeholder = any(k == UPLINK_PORT or k.startswith("to-") for k in (ka, kb))
        if {sa, sb} == {a, b} and not placeholder:
            return True
    return False


def integrate_devices(db: Conn, devices: Iterable[Device], now: float) -> FeedResult:
    """The Integration feed on an open unit: one savepoint per device and per link."""
    tx = InfraTx(db)
    result = FeedResult()
    by_id: dict[str, tuple[str, Device]] = {}
    for d in devices:
        sid = _sid(d.mac)
        if sid is None:
            result.skipped += 1
            continue
        try:
            with savepoint(db, "device"):
                tx.upsert_switch(sid, name=d.name, mgmt_addresses=[d.ip] if d.ip else [],
                                 vendor=VENDOR, platform=d.model, now=now)
        except ITEM_ERRORS:
            result.skipped += 1
            continue
        by_id[d.device_id] = (sid, d)
        result.switches += 1
    for child_sid, d in by_id.values():
        parent = by_id.get(d.uplink_device_id)
        if parent is None or parent[0] == child_sid:
            continue
        parent_sid = parent[0]
        try:
            with savepoint(db, "link"):
                if _joined_by_ports(db, child_sid, parent_sid):
                    continue
                parent_port = port_key(f"to-{child_sid[4:]}")
                tx.upsert_port(child_sid, UPLINK_PORT, raw_port_id="uplink", role="uplink",
                               now=now)
                tx.upsert_port(parent_sid, parent_port,
                               raw_port_id=f"link to {d.name or child_sid}", now=now)
                tx.upsert_link(tx.port_ref(child_sid, UPLINK_PORT),
                               tx.port_ref(parent_sid, parent_port), source="config",
                               confidence=DEVICE_LINK_CONFIDENCE, now=now)
            result.links += 1
        except ITEM_ERRORS:
            result.skipped += 1
    return result


def _noted(feed: str, result: FeedResult) -> FeedResult:
    if result.skipped:
        log.warning("UniFi %s feed skipped %d malformed item(s); the rest of the cycle was kept",
                    feed, result.skipped)
    return result


async def feed_integration(store: Store, devices: Iterable[Device], now: float, *,
                           save_devices: bool = False,
                           site_status: tuple[str, str, dict[str, Any]] | None = None
                           ) -> FeedResult:
    """One Integration cycle in one transaction. With `save_devices` the poll's device rows
    (the plugin's `unifi_devices` table) are written in the same unit, and `site_status`
    (site id, gateway id, uplink statistics) goes to `unifi_site_status` with them."""
    items = list(devices)

    def cycle(db: Conn) -> FeedResult:
        if save_devices:
            write_devices(db, items, now)
        if site_status is not None:
            write_site_status_integration(db, site_status[0], site_status[1], site_status[2], now)
        return integrate_devices(db, items, now)
    return _noted("integration", await write_cycle(store, cycle, now=now, touches=("unifi",)))


def _props(p: dict[str, Any]) -> list[tuple[str, Any, str]]:
    """(name, value, unit) for what a classic port row reports. A value that is absent is not
    written; unknown is never zero. A port that is down reports no speed."""
    out: list[tuple[str, Any, str]] = []
    speed = p.get("speed_mbps")
    if p.get("up") is True and isinstance(speed, int) and speed > 0:
        out.append(("link_speed_mbps", speed, "Mbps"))
    if p.get("poe_class"):
        out.append(("poe_class", p["poe_class"], ""))
    if p.get("poe_w") is not None:
        out.append(("poe_load_w", float(p["poe_w"]), "W"))
    if p.get("native_vlan") is not None:
        out.append(("vlan", p["native_vlan"], ""))
    return out


def _ports(db: Conn, tx: InfraTx, sid: str, dev: dict[str, Any], now: float) -> FeedResult:
    """The ports of one device and their properties, a savepoint per port."""
    out = FeedResult()
    up = dev["uplink"]
    uplink_idx = up["local_port"] if up else None
    for idx_text, p in dev["ports"].items():
        try:
            with savepoint(db, "port"):
                idx = int(idx_text)
                key = tx.upsert_port(
                    sid, unifi_port_key(idx), raw_port_id=p["name"] or f"Port {idx}",
                    unifi_index=idx, role="uplink" if idx == uplink_idx else "unknown", now=now)
                out.ports += 1
                for name, value, unit in _props(p):
                    try:
                        # A refused property raises before it writes, so it needs no savepoint.
                        if tx.append_property(sid, key, name, value, unit=unit, source=SOURCE,
                                              observed_at=now, now=now):
                            out.properties_added += 1
                    except InfraError:
                        out.skipped += 1
        except (InfraError, ValueError):  # the port's own rows went back with its savepoint
            out.skipped += 1
    return out


def integrate_classic(db: Conn, devices: list[dict[str, Any]], now: float) -> FeedResult:
    """The classic feed on an open unit: one savepoint per device in each of the three passes
    (the switch, its ports and properties, its links), and one per port and per link inside."""
    tx = InfraTx(db)
    result = FeedResult()
    sids: dict[str, str] = {}
    for dev in devices:
        try:
            with savepoint(db, "device"):
                sid = _sid(dev["mac"])
                if sid is None:
                    result.skipped += 1
                    continue
                tx.upsert_switch(sid, name=dev["name"], vendor=VENDOR, now=now)
        except ITEM_ERRORS:
            result.skipped += 1
            continue
        sids[dev["mac"]] = sid
        result.switches += 1

    for dev in devices:
        sid = sids.get(dev.get("mac", ""))
        if sid is None:
            continue
        try:
            with savepoint(db, "device"):
                part = _ports(db, tx, sid, dev, now)
        except ITEM_ERRORS:
            result.skipped += 1
            continue
        result.add(part)

    for dev in devices:
        sid = sids.get(dev.get("mac", ""))
        if sid is None:
            continue
        try:
            with savepoint(db, "device"):
                part = FeedResult()
                _uplink_link(db, tx, sid, dev, sids, now, part)
                for n in dev["lldp"]:
                    _lldp_link(db, tx, sid, n, sids, now, part)
        except ITEM_ERRORS:
            result.skipped += 1
            continue
        result.add(part)
    return result


async def feed_classic(store: Store, devices: list[dict[str, Any]], now: float) -> FeedResult:
    """One classic cycle in one transaction."""
    return _noted("classic", await write_cycle(
        store, lambda db: integrate_classic(db, devices, now), now=now, touches=("unifi",)))


def _uplink_link(db: Conn, tx: InfraTx, sid: str, dev: dict[str, Any], sids: dict[str, str],
                 now: float, result: FeedResult) -> None:
    up = dev["uplink"]
    if not up or up["local_port"] is None or up["remote_port"] is None:
        return
    parent = sids.get(up["mac"])
    if parent is None or parent == sid:
        return
    try:
        with savepoint(db, "link"):
            mine = tx.upsert_port(sid, unifi_port_key(up["local_port"]),
                                  unifi_index=up["local_port"], role="uplink", now=now)
            theirs = tx.upsert_port(parent, unifi_port_key(up["remote_port"]),
                                    unifi_index=up["remote_port"], now=now)
            tx.upsert_link(tx.port_ref(sid, mine), tx.port_ref(parent, theirs),
                           source="config", confidence=CONFIG_CONFIDENCE, now=now)
            # The device-level placeholder from the Integration feed is now redundant.
            tx.close_link(tx.port_ref(sid, UPLINK_PORT),
                          tx.port_ref(parent, port_key(f"to-{sid[4:]}")),
                          source="config", now=now)
            result.links += 1
    except (InfraError, ValueError):
        result.skipped += 1


def _lldp_link(db: Conn, tx: InfraTx, sid: str, n: dict[str, Any], sids: dict[str, str],
               now: float, result: FeedResult) -> None:
    remote = _sid(n["chassis_id"])
    if remote is None or remote == sid:
        return
    try:
        with savepoint(db, "link"):
            known = remote in sids.values() or tx.switch_exists(remote)
            if not known:
                return  # a phone or camera is not a switch; it is not created from LLDP
            raw = n["port_id"]
            remote_unifi = remote in sids.values()
            if raw.isdigit() and remote_unifi:
                # No raw id: the port keeps the name the device itself reported.
                theirs = tx.upsert_port(remote, unifi_port_key(int(raw)),
                                        unifi_index=int(raw), now=now)
            else:
                theirs = tx.upsert_port(remote, raw, raw_port_id=raw, now=now)
            mine = tx.upsert_port(sid, unifi_port_key(n["local_port"]),
                                  unifi_index=n["local_port"], now=now)
            tx.upsert_link(tx.port_ref(sid, mine), tx.port_ref(remote, theirs),
                           source="lldp", confidence=1.0, now=now)
            result.links += 1
    except (InfraError, ValueError):
        result.skipped += 1
