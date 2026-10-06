"""The map feed: UniFi devices, ports and links written into the infrastructure map.

Two feeds, both through the core InfraService (docs/FIELD-DATA.md), never by SQL of their own:

- feed_integration takes the Integration API device list. Each device becomes a switch keyed by
  its chassis MAC (switch_id), with its name, address and model. A device that names the device
  it is uplinked to gets a device-level link of source `config`, drawn between a port named
  `uplink` on the child and a port named `to-<child mac>` on the parent, because a link joins
  ports. That placeholder link is skipped when a link with real port numbers already joins the
  two devices.
- feed_classic takes the parsed classic views (classic.py) and is used only when the optional
  classic credential is set. It adds the ports, keyed by unifi_port_key(port_idx) with
  `unifi_index` set, the properties link_speed_mbps, poe_class, poe_load_w and vlan with source
  `unifi`, a `config` link for the uplink port numbers and an `lldp` link for each LLDP
  neighbour that is a known UniFi device or an already known switch.

Properties go through InfraService.append_property, which adds a history row only when the value
differs from this source's last one and otherwise only moves last_verified. A neighbour that is
not already known is not created, so cameras and phones that speak LLDP do not become switches.
Nothing here creates or changes a monitor. A malformed row is counted and skipped; it never
stops the rest of the feed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from observe.infra import InfraError, InfraService
from observe.portkey import mac_digits, port_key, switch_id, unifi_port_key
from observe.store import Store

from .records import Device

SOURCE = "unifi"
VENDOR = "Ubiquiti"
UPLINK_PORT = "uplink"
CONFIG_CONFIDENCE = 0.8
DEVICE_LINK_CONFIDENCE = 0.5


@dataclass
class FeedResult:
    switches: int = 0
    ports: int = 0
    properties_added: int = 0
    links: int = 0
    skipped: int = 0

    def as_detail(self) -> dict[str, Any]:
        return {"switches": self.switches, "ports": self.ports,
                "properties_added": self.properties_added, "links": self.links,
                "skipped": self.skipped}


def _sid(mac: str) -> str | None:
    return switch_id(mac) if mac_digits(mac) else None


async def _joined_by_ports(infra: InfraService, a: str, b: str) -> bool:
    """True when an open link with real port numbers joins switches a and b."""
    def go(db: Any) -> list[Any]:
        return db.execute(
            "SELECT a_ref, b_ref FROM infra_links WHERE a_kind='port' AND b_kind='port' "
            "AND closed_at IS NULL AND (a_ref LIKE ? OR b_ref LIKE ?)",
            (f"{a}|%", f"{a}|%")).fetchall()
    for ra, rb in await infra._run(go):
        sa, _, ka = ra.partition("|")
        sb, _, kb = rb.partition("|")
        placeholder = any(k == UPLINK_PORT or k.startswith("to-") for k in (ka, kb))
        if {sa, sb} == {a, b} and not placeholder:
            return True
    return False


async def feed_integration(store: Store, devices: Iterable[Device], now: float) -> FeedResult:
    infra = InfraService(store)
    result = FeedResult()
    by_id: dict[str, tuple[str, Device]] = {}
    for d in devices:
        sid = _sid(d.mac)
        if sid is None:
            result.skipped += 1
            continue
        try:
            await infra.upsert_switch(sid, name=d.name, mgmt_addresses=[d.ip] if d.ip else [],
                                      vendor=VENDOR, platform=d.model, now=now)
        except InfraError:
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
            if await _joined_by_ports(infra, child_sid, parent_sid):
                continue
            parent_port = port_key(f"to-{child_sid[4:]}")
            await infra.upsert_port(child_sid, UPLINK_PORT, raw_port_id="uplink", role="uplink",
                                    now=now)
            await infra.upsert_port(parent_sid, parent_port,
                                    raw_port_id=f"link to {d.name or child_sid}", now=now)
            await infra.upsert_link(infra.port_ref(child_sid, UPLINK_PORT),
                                    infra.port_ref(parent_sid, parent_port), source="config",
                                    confidence=DEVICE_LINK_CONFIDENCE, now=now)
            result.links += 1
        except (InfraError, ValueError):
            result.skipped += 1
    return result


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


async def feed_classic(store: Store, devices: list[dict[str, Any]], now: float) -> FeedResult:
    infra = InfraService(store)
    result = FeedResult()
    sids: dict[str, str] = {}
    for dev in devices:
        sid = _sid(dev["mac"])
        if sid is None:
            result.skipped += 1
            continue
        try:
            await infra.upsert_switch(sid, name=dev["name"], vendor=VENDOR, now=now)
        except InfraError:
            result.skipped += 1
            continue
        sids[dev["mac"]] = sid
        result.switches += 1

    for dev in devices:
        sid = sids.get(dev["mac"])
        if sid is None:
            continue
        up = dev["uplink"]
        uplink_idx = up["local_port"] if up else None
        for idx_text, p in dev["ports"].items():
            idx = int(idx_text)
            try:
                key = await infra.upsert_port(
                    sid, unifi_port_key(idx), raw_port_id=p["name"] or f"Port {idx}",
                    unifi_index=idx, role="uplink" if idx == uplink_idx else "unknown", now=now)
                result.ports += 1
            except (InfraError, ValueError):
                result.skipped += 1
                continue
            for name, value, unit in _props(p):
                try:
                    if await infra.append_property(sid, key, name, value, unit=unit,
                                                   source=SOURCE, observed_at=now, now=now):
                        result.properties_added += 1
                except InfraError:
                    result.skipped += 1

    for dev in devices:
        sid = sids.get(dev["mac"])
        if sid is None:
            continue
        await _uplink_link(infra, sid, dev, sids, now, result)
        for n in dev["lldp"]:
            await _lldp_link(infra, sid, n, sids, now, result)
    return result


async def _uplink_link(infra: InfraService, sid: str, dev: dict[str, Any],
                       sids: dict[str, str], now: float, result: FeedResult) -> None:
    up = dev["uplink"]
    if not up or up["local_port"] is None or up["remote_port"] is None:
        return
    parent = sids.get(up["mac"])
    if parent is None or parent == sid:
        return
    try:
        mine = await infra.upsert_port(sid, unifi_port_key(up["local_port"]),
                                       unifi_index=up["local_port"], role="uplink", now=now)
        theirs = await infra.upsert_port(parent, unifi_port_key(up["remote_port"]),
                                         unifi_index=up["remote_port"], now=now)
        await infra.upsert_link(infra.port_ref(sid, mine), infra.port_ref(parent, theirs),
                                source="config", confidence=CONFIG_CONFIDENCE, now=now)
        result.links += 1
        # The device-level placeholder from the Integration feed is now redundant.
        await infra.close_link(infra.port_ref(sid, UPLINK_PORT),
                               infra.port_ref(parent, port_key(f"to-{sid[4:]}")),
                               source="config", now=now)
    except (InfraError, ValueError):
        result.skipped += 1


async def _lldp_link(infra: InfraService, sid: str, n: dict[str, Any], sids: dict[str, str],
                     now: float, result: FeedResult) -> None:
    remote = _sid(n["chassis_id"])
    if remote is None or remote == sid:
        return
    try:
        known = remote in sids.values() or await infra.switch_exists(remote)
        if not known:
            return  # a phone or camera is not a switch; it is not created from LLDP
        raw = n["port_id"]
        remote_unifi = remote in sids.values()
        if raw.isdigit() and remote_unifi:
            # No raw id: the port keeps the name the device itself reported.
            theirs = await infra.upsert_port(remote, unifi_port_key(int(raw)),
                                             unifi_index=int(raw), now=now)
        else:
            theirs = await infra.upsert_port(remote, raw, raw_port_id=raw, now=now)
        mine = await infra.upsert_port(sid, unifi_port_key(n["local_port"]),
                                       unifi_index=n["local_port"], now=now)
        await infra.upsert_link(infra.port_ref(sid, mine), infra.port_ref(remote, theirs),
                                source="lldp", confidence=1.0, now=now)
        result.links += 1
    except (InfraError, ValueError):
        result.skipped += 1
