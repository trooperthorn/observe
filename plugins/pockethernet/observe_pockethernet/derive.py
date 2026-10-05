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
- A switch or port that already exists is never overwritten: live LLDP or SNMP data owns the
  name, addresses, vendor, platform and port role (an uplink stays an uplink). A port the report
  creates starts as `access`.
- Each allowlisted property in the report is appended to the port with provenance: the report
  id, the key (prefix and device label) as `recorded_by`, and the tester serial as a property.
  A value equal to the current one only moves `last_verified`.
- A report whose status is `cancelled` or `aborted` derives nothing at all: no switch, port,
  jack, link or property, because a partial run can carry zero speeds and half-read values. It
  stays stored as evidence, and the report page still lists it. Only `complete` reports derive.

Order. Every time written to the map (last_seen, link confirmation, ageing, the jack patch,
`observed_at`, `last_verified`, `last_tested_at`) is the report's corrected observation time,
after clock-skew correction, never the time it was received. The core treats the newest
observation as current, so a late or resent older report adds history but does not replace a
newer current value, does not move the jack patch, and does not refresh a link. The receive
time only fills `recorded_at`. Live accept and rebuild use the same values, so a rebuild
reproduces the same rows apart from their autoincrement ids.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from watchpost.infra import InfraError, InfraService
from watchpost.portkey import LLDP_SUBTYPES, lldp_port_key, mac_digits, port_key, switch_id
from watchpost.store import Store

from .schema import Neighbor, Report, ReportError, parse_report

log = logging.getLogger(__name__)

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


def recorded_by_for(key_prefix: str, device: str) -> str:
    """The `recorded_by` of every property a report from this key and device writes."""
    return f"{key_prefix}:{device}"[:64]


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


DERIVING_STATUS = "complete"


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
    if "last_tested_at_ms" in values:
        # The phone's own test time shifts by the same skew as the report time.
        values["last_tested_at_ms"] = max(
            0, values["last_tested_at_ms"] + (taken_ms - report.taken_at_ms))
    else:
        values["last_tested_at_ms"] = taken_ms
    out: list[tuple[str, Any]] = []
    for name, value in values.items():
        if name == "last_tested_at_ms":
            name, value = "last_tested_at", value / 1000.0
        elif name in ("poe_class", "tester_serial"):
            value = str(value)  # the core stores these as text
        out.append((name, value))
    return out


def _jack(report: Report) -> str:
    site = report.site
    return _clean(site.port_id if site else "") or _clean(report.location_label) \
        or _clean(report.properties.jack_label)


@dataclass(frozen=True)
class Footprint:
    """Where a report's derived data sits: its port and its jack."""

    port: tuple[str, str] | None = None
    jack: str = ""


def footprint(body: bytes | None) -> Footprint:
    """The port and jack a stored body derives to; empty when it cannot be parsed."""
    if body is None:
        return Footprint()
    try:
        report = parse_report(bytes(body))
    except (ReportError, ValueError):
        return Footprint()
    found = _neighbor(report)
    if found is None:
        return Footprint()
    return Footprint((found[1][0], found[2][0]), _jack(report))


def retract_rows(db: sqlite3.Connection, report_id: str, recorded_by: str, fp: Footprint) -> None:
    """Remove what one report derived: its properties, and the field_report link and jack patch
    of its jack. Other reports that shared those are replayed by replay_siblings. The caller
    holds the store lock and the transaction."""
    db.execute("DELETE FROM port_properties WHERE source=? AND report_id=? AND recorded_by=?",
               (SOURCE, report_id, recorded_by))
    if fp.port is not None:
        # A verification the retracted rows lent to this port's rows is not evidence any more.
        db.execute("UPDATE port_properties SET last_verified=observed_at "
                   "WHERE source=? AND switch_id=? AND port_key=?", (SOURCE, *fp.port))
    if fp.jack:
        db.execute("DELETE FROM infra_links WHERE source=? AND a_kind='jack' AND a_ref=?",
                   (LINK_SOURCE, fp.jack))
        db.execute("UPDATE infra_jacks SET switch_id=NULL, port_key=NULL WHERE jack_key=?",
                   (fp.jack,))


def _sibling_rows(store: Store) -> list[tuple[Any, ...]]:
    with store._lock:
        return store._db.execute(
            "SELECT source, report_id, key_prefix, taken_at_ms, updated_at, body "
            "FROM field_reports WHERE body IS NOT NULL "
            "ORDER BY taken_at_ms, updated_at, source, report_id").fetchall()


async def replay_siblings(store: Store, fp: Footprint, own: tuple[str, str]) -> Derived | None:
    """Derive again, in observation order, every stored report on the same port or jack as a
    retracted one, and the report `own` itself, whose stored body is the new revision.

    The core records an equal value only as a verification of the newest row, so a report whose
    value matched the retracted report's has no row of its own; replaying it writes the row a
    rebuild would, and replaying in rebuild order gives the same row owners. Returns what
    deriving `own` produced."""
    rows = await asyncio.to_thread(_sibling_rows, store)
    infra = InfraService(store)
    result: Derived | None = None
    for source, report_id, key_prefix, taken_ms, updated_at, body in rows:
        mine = (source, report_id) == own
        other = footprint(body)
        if not (mine or (fp.port is not None and other.port == fp.port)
                or (fp.jack and other.jack == fp.jack)):
            continue
        res = await derive_report(infra, parse_report(bytes(body)), key_prefix=key_prefix,
                                  device=source, taken_ms=taken_ms, now=updated_at)
        if mine:
            result = res
    return result


async def derive_report(infra: InfraService, report: Report, *, key_prefix: str, device: str,
                        taken_ms: int, now: float) -> Derived:
    """Write the switch, port, jack, link and properties one report implies. Idempotent."""
    result = Derived()
    if report.status != DERIVING_STATUS:
        result.skipped = f"report status is {report.status}"
        return result
    found = _neighbor(report)
    if found is None:
        result.skipped = "no neighbour with a switch and a port"
        return result
    n, (sid, name, addrs, vendor, platform), (key, raw_port) = found
    site = report.site
    jack = _jack(report)
    recorded_by = recorded_by_for(key_prefix, device)
    seen = taken_ms / 1000.0  # the corrected observation time; `now` is only recorded_at

    # Field data never overwrites what live sources know: a switch or port that already exists
    # keeps its name, addresses, vendor, platform and role, and only has its last-seen moved.
    if await infra.switch_exists(sid):
        await infra.upsert_switch(sid, now=seen)
    else:
        await infra.upsert_switch(sid, name=name, mgmt_addresses=addrs[:16], vendor=vendor,
                                  platform=platform, now=seen)
    role = "unknown" if await infra.port_exists(sid, key) else "access"
    await infra.upsert_port(sid, key, raw_port_id=raw_port, role=role, now=seen)
    result.switch, result.port = sid, key
    if jack:
        await infra.upsert_jack(jack, room=_clean(site.room if site else ""),
                                site=_clean(site.site if site else ""), switch=sid, port=key,
                                now=seen)
        await infra.upsert_link(infra.jack_ref(jack), infra.port_ref(sid, key),
                                source=LINK_SOURCE, confidence=LINK_CONFIDENCE[n.protocol],
                                now=seen)
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
    failures: list[dict[str, str]] = field(default_factory=list)

    def as_detail(self) -> dict[str, Any]:
        d: dict[str, Any] = {"reports": self.reports, "derived": self.derived,
                             "skipped": self.skipped, "failed": self.failed,
                             "pruned": self.pruned}
        if self.failures:
            d["failed_reports"] = self.failures
        return d


class _InlineInfra(InfraService):
    """InfraService that runs on the caller's thread inside the caller's open transaction.

    The rebuild holds the store lock and one transaction for its whole run, so each call here
    must neither take the lock again nor commit.
    """

    async def _run(self, fn: Any) -> Any:
        return fn(self._store._db)


def _rebuild_sync(store: Store) -> RebuildResult:
    out = RebuildResult()
    with store._lock:
        db = store._db
        pruned = db.execute("SELECT COUNT(*) FROM field_reports WHERE body IS NULL").fetchone()[0]
        if pruned:
            out.pruned = int(pruned)
            return out
        rows = db.execute(
            "SELECT source, report_id, key_prefix, taken_at_ms, updated_at, body "
            "FROM field_reports ORDER BY taken_at_ms, updated_at, source, report_id").fetchall()
        try:
            db.execute("DELETE FROM port_properties WHERE source=?", (SOURCE,))
            jacks = [r[0] for r in db.execute(
                "SELECT a_ref FROM infra_links WHERE source=? AND a_kind='jack'", (LINK_SOURCE,))]
            db.execute("DELETE FROM infra_links WHERE source=?", (LINK_SOURCE,))
            db.executemany(
                "UPDATE infra_jacks SET switch_id=NULL, port_key=NULL WHERE jack_key=?",
                [(j,) for j in jacks])
            infra = _InlineInfra(store)

            async def replay() -> None:
                for source, report_id, key_prefix, taken_ms, updated_at, body in rows:
                    out.reports += 1
                    try:
                        report = parse_report(bytes(body))
                        res = await derive_report(infra, report, key_prefix=key_prefix,
                                                  device=source, taken_ms=taken_ms,
                                                  now=updated_at)
                    except (ReportError, InfraError, ValueError, sqlite3.Error):
                        out.failed += 1
                        out.failures.append({"source": source, "report_id": report_id})
                        continue
                    if res.skipped:
                        out.skipped += 1
                    else:
                        out.derived += 1

            asyncio.run(replay())
            if out.failed:
                db.rollback()  # keep the old derived data; the caller reports the failures
                return out
            db.execute("UPDATE field_reports SET derive_status='ok'")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return out


async def rebuild(store: Store) -> RebuildResult:
    """Clear everything derived from field reports and derive it again from the stored bodies.

    Switches, ports and jacks stay (other sources may share them, and upserts are idempotent);
    this plugin's properties and `field_report` links are deleted and replayed in the order the
    reports were observed, all in one transaction. When any body fails to parse or derive, the
    transaction is rolled back, the old derived data stays and `failures` names the reports.
    Refused with a nonzero `pruned` when retention dropped any body.
    """
    return await asyncio.to_thread(_rebuild_sync, store)


@dataclass
class RetryResult:
    retried: int = 0
    derived: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def as_detail(self) -> dict[str, Any]:
        d: dict[str, Any] = {"retried": self.retried, "derived": self.derived,
                             "failed": self.failed}
        if self.skipped:
            d["skipped"] = self.skipped
        if self.failures:
            d["failed_reports"] = self.failures
        return d


def _pending_sync(store: Store) -> list[tuple[Any, ...]]:
    with store._lock:
        return store._db.execute(
            "SELECT source, report_id, revision, key_prefix, taken_at_ms, updated_at, body "
            "FROM field_reports WHERE derive_status = 'failed' "
            "ORDER BY taken_at_ms, updated_at, source, report_id").fetchall()


def _retract_sync(store: Store, report_id: str, recorded_by: str, fp: Footprint) -> None:
    with store._lock, store._db:
        retract_rows(store._db, report_id, recorded_by, fp)


async def retry_failed(store: Store) -> RetryResult:
    """Derive again every stored report whose derivation failed. A report still marked pending
    is mid-upload and is left alone; a rebuild covers one a crash left behind."""
    from .reports import mark_derive_status  # reports imports this module for its constants

    out = RetryResult()
    infra = InfraService(store)
    for source, report_id, revision, key_prefix, taken_ms, updated_at, body in             await asyncio.to_thread(_pending_sync, store):
        out.retried += 1
        try:
            if body is None:
                raise ReportError("the report body was dropped by retention", 400)
            report = parse_report(bytes(body))
            # Rows a half-finished attempt left are retracted first, so the retry is idempotent.
            fp = footprint(body)
            await asyncio.to_thread(_retract_sync, store, report_id,
                                    recorded_by_for(key_prefix, source), fp)
            res = await replay_siblings(store, fp, (source, report_id))
            if res is None:  # the report's own row is always replayed; guard anyway
                res = await derive_report(infra, report, key_prefix=key_prefix, device=source,
                                          taken_ms=taken_ms, now=updated_at)
        except Exception:
            log.exception("retry of field report %s failed", report_id)
            out.failed += 1
            out.failures.append({"source": source, "report_id": report_id})
            await mark_derive_status(store, source, report_id, revision, "failed")
            continue
        if res.skipped:
            out.skipped += 1
        else:
            out.derived += 1
        await mark_derive_status(store, source, report_id, revision, "ok")
    return out
