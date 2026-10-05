"""Turn an accepted field report into port properties and map edges, and rebuild them.

The report is the evidence; everything here is derived from it and can be derived again
(docs/FIELD-DATA.md). All writes go through the core's InfraService, so the plugin never touches
the infrastructure tables for a write; only the rebuild clears its own derived rows with SQL.

What a report yields:

- The neighbour (LLDP first, else CDP) names the switch and the port the tester was plugged
  into. A neighbour with no usable switch identity or no port yields nothing, because a property
  needs a port to belong to. The report stays stored as evidence.
- The site port id (else the location label, else the jack label property) names the jack. The
  jack is patched to that port and a `field_report` link joins them. A jack seen on another
  port than before closes the earlier link at once (the core does that in upsert_link).
- Each allowlisted property in the report is appended to the port with provenance: the report
  id, the key (prefix and device label) as `recorded_by`, and the tester serial as a property.
  A value equal to the newest one only moves `last_verified`.

The time used as `now` is the time the report was received, which the report store keeps as
`updated_at`. Live accept and rebuild use the same value, so a rebuild reproduces the same rows
apart from their autoincrement ids.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from watchpost.infra import InfraError, InfraService
from watchpost.portkey import LLDP_SUBTYPES, lldp_port_key, mac_digits, port_key, switch_id
from watchpost.store import Store

from .schema import Neighbor, Report, ReportError, parse_report

SOURCE = "pockethernet"  # the `source` of every property this plugin writes
LINK_SOURCE = "field_report"
LINK_CONFIDENCE = {"lldp": 0.9, "cdp": 0.8}

_UNITS = {
    "pair_1_2_length_m": "m", "pair_3_6_length_m": "m", "pair_4_5_length_m": "m",
    "pair_7_8_length_m": "m", "link_speed_mbps": "Mbps", "poe_voltage_v": "V",
    "poe_load_w": "W", "last_tested_at": "s",
}


@dataclass
class Derived:
    """What one report produced. `skipped` says why nothing was derived."""

    switch: str = ""
    port: str = ""
    jack: str = ""
    properties_added: int = 0
    properties_verified: int = 0
    skipped: str = ""

    def as_detail(self) -> dict[str, Any]:
        d: dict[str, Any] = {"properties_added": self.properties_added,
                             "properties_verified": self.properties_verified,
                             "jack_linked": bool(self.jack)}
        if self.skipped:
            d["skipped"] = self.skipped
        return d


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _identity(n: Neighbor) -> tuple[str, str, list[str], str, str] | None:
    """(switch id, display name, management addresses, vendor, platform), or None."""
    lldp, cdp = n.lldp, n.cdp
    mac = next((m for m in (lldp.chassis_id if lldp else None, n.device_id)
                if m and mac_digits(m)), None)
    name = _clean(n.system_name or (lldp.system_name if lldp else None)
                  or (cdp.device_id if cdp else None) or (n.device_id if not mac else None))
    if mac is None and not name:
        return None
    try:
        sid = switch_id(mac, name or None)
    except ValueError:
        return None
    addrs = [a for a in [*n.management_addresses, lldp.management_address if lldp else None,
                         *(cdp.addresses if cdp else [])] if a]
    platform = _clean((cdp.platform if cdp else None) or n.description)
    return sid, name, addrs, _clean(n.vendor), platform[:256]


def _port(n: Neighbor) -> tuple[str, str] | None:
    """(port key, raw port id) from the most exact source the neighbour offers."""
    lldp, cdp = n.lldp, n.cdp
    if lldp and lldp.port_id and lldp.port_id_subtype in LLDP_SUBTYPES:
        try:
            return lldp_port_key(lldp.port_id_subtype, lldp.port_id), lldp.port_id
        except ValueError:
            pass
    for raw in (n.port_id, lldp.port_id if lldp else None, cdp.port_id if cdp else None):
        if raw and raw.strip():
            try:
                return port_key(raw), raw
            except ValueError:
                continue
    return None


def _neighbor(report: Report) -> tuple[Neighbor, tuple[str, str, list[str], str, str],
                                       tuple[str, str]] | None:
    """The first neighbour, LLDP before CDP, that names both a switch and a port."""
    for protocol in ("lldp", "cdp"):
        for n in report.neighbors:
            if n.protocol != protocol:
                continue
            ident, port = _identity(n), _port(n)
            if ident and port:
                return n, ident, port
    return None


def _properties(report: Report, taken_ms: int, jack: str) -> list[tuple[str, Any]]:
    """The (name, value) pairs to append, in a fixed order, with fallbacks from the report."""
    values = {k: v for k, v in report.properties.model_dump().items() if v is not None}
    site = report.site
    for name, fallback in (("jack_label", jack), ("panel", site.panel if site else ""),
                           ("room", site.room if site else ""),
                           ("site", site.site if site else "")):
        if name not in values and _clean(fallback):
            values[name] = _clean(fallback)
    if "tester_serial" not in values:
        values["tester_serial"] = report.device.serial
    values.setdefault("last_tested_at_ms", taken_ms)
    out: list[tuple[str, Any]] = []
    for name, value in values.items():
        if name == "last_tested_at_ms":
            name, value = "last_tested_at", value / 1000.0
        elif name in ("poe_class", "tester_serial"):
            value = str(value)  # the core stores these as text
        out.append((name, value))
    return out


async def derive_report(infra: InfraService, report: Report, *, key_prefix: str, device: str,
                        taken_ms: int, now: float) -> Derived:
    """Write the switch, port, jack, link and properties one report implies. Idempotent."""
    result = Derived()
    found = _neighbor(report)
    if found is None:
        result.skipped = "no neighbour with a switch and a port"
        return result
    n, (sid, name, addrs, vendor, platform), (key, raw_port) = found
    site = report.site
    jack = _clean(site.port_id if site else "") or _clean(report.location_label) \
        or _clean(report.properties.jack_label)
    recorded_by = f"{key_prefix}:{device}"[:64]
    seen = taken_ms / 1000.0

    await infra.upsert_switch(sid, name=name, mgmt_addresses=addrs[:16], vendor=vendor,
                              platform=platform, now=now)
    await infra.upsert_port(sid, key, raw_port_id=raw_port, role="access", now=now)
    result.switch, result.port = sid, key
    if jack:
        await infra.upsert_jack(jack, room=_clean(site.room if site else ""),
                                site=_clean(site.site if site else ""), switch=sid, port=key,
                                now=now)
        await infra.upsert_link(infra.jack_ref(jack), infra.port_ref(sid, key),
                                source=LINK_SOURCE, confidence=LINK_CONFIDENCE[n.protocol],
                                now=now)
        result.jack = jack
    for pname, value in _properties(report, taken_ms, jack):
        added = await infra.append_property(
            sid, key, pname, value, unit=_UNITS.get(pname, ""), source=SOURCE,
            report_id=report.report_id, observed_at=seen, recorded_by=recorded_by, now=now)
        if added:
            result.properties_added += 1
        else:
            result.properties_verified += 1
    return result


@dataclass
class RebuildResult:
    reports: int = 0
    derived: int = 0
    skipped: int = 0
    failed: int = 0
    pruned: int = 0

    def as_detail(self) -> dict[str, Any]:
        return {"reports": self.reports, "derived": self.derived, "skipped": self.skipped,
                "failed": self.failed, "pruned": self.pruned}


def _clear_sync(store: Store) -> tuple[list[tuple[Any, ...]], int]:
    """Read the reports to replay and delete what they derived, in one transaction.

    Refuses (returns the pruned count with no rows) when a body was dropped by retention,
    because those reports could not be replayed and their properties would be lost.
    """
    with store._lock, store._db:
        pruned = store._db.execute(
            "SELECT COUNT(*) FROM field_reports WHERE body IS NULL").fetchone()[0]
        if pruned:
            return [], int(pruned)
        rows = store._db.execute(
            "SELECT source, report_id, key_prefix, taken_at_ms, updated_at, body "
            "FROM field_reports ORDER BY updated_at, source, report_id").fetchall()
        store._db.execute("DELETE FROM port_properties WHERE source=?", (SOURCE,))
        jacks = [r[0] for r in store._db.execute(
            "SELECT a_ref FROM infra_links WHERE source=? AND a_kind='jack'", (LINK_SOURCE,))]
        store._db.execute("DELETE FROM infra_links WHERE source=?", (LINK_SOURCE,))
        store._db.executemany(
            "UPDATE infra_jacks SET switch_id=NULL, port_key=NULL WHERE jack_key=?",
            [(j,) for j in jacks])
        return rows, 0


async def rebuild(store: Store) -> RebuildResult:
    """Clear everything derived from field reports and derive it again from the stored bodies.

    Switches, ports and jacks stay (other sources may share them, and upserts are idempotent);
    this plugin's properties and `field_report` links are deleted and replayed in the order the
    reports arrived. Refused with a nonzero `pruned` when retention dropped any body.
    """
    out = RebuildResult()
    rows, out.pruned = await asyncio.to_thread(_clear_sync, store)
    if out.pruned:
        return out
    infra = InfraService(store)
    for source, _report_id, key_prefix, taken_ms, updated_at, body in rows:
        out.reports += 1
        try:
            report = parse_report(bytes(body))
            res = await derive_report(infra, report, key_prefix=key_prefix, device=source,
                                      taken_ms=taken_ms, now=updated_at)
        except (ReportError, InfraError, ValueError):
            out.failed += 1
            continue
        if res.skipped:
            out.skipped += 1
        else:
            out.derived += 1
    return out
