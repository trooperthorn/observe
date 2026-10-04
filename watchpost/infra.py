"""Core service API for the infrastructure map (docs/FIELD-DATA.md).

Plugins and core code call this to upsert switches, ports, jacks, links and endpoints and to
append typed port properties. Port names are normalised by watchpost/portkey.py first, so the
callers may pass whatever spelling they have. Switch ids come from portkey.switch_id().

Port properties are append-only. The current value of a property is its newest row. Writing
a value identical to the newest one adds no row; it moves that row's last_verified forward,
so the page can say "confirmed again on ...". Only allowlisted names are accepted, plus
`custom.<name>` for hand-entered properties; a custom write needs an actor and is written to
the audit log. Values are checked for type and length. A property can only be written for a
port that exists, and nothing here creates or changes a monitor.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Callable
from typing import Any

from .portkey import lldp_port_key, port_key, switch_id
from .store import Store

__all__ = ["InfraError", "InfraService", "PROPERTY_TYPES", "UnknownPropertyError",
           "lldp_port_key", "port_key", "switch_id"]

ROLES = ("access", "uplink", "unknown")
LINK_SOURCES = ("lldp", "cdp", "field_report", "snmp_lldp", "config")
LINK_KINDS = ("port", "jack", "endpoint")
ENDPOINT_KINDS = ("monitor", "host", "field")
VALUE_MAX = 512
TEXT_MAX = 256
CUSTOM_NAME = re.compile(r"^custom\.[a-z][a-z0-9_]{0,39}$")
SWITCH_ID = re.compile(r"^(mac:[0-9a-f]{12}|name:[^|\x00-\x1f]{1,128})$")

_PAIRS = ("1_2", "3_6", "4_5", "7_8")
# Property name to the Python type its value must have. Custom properties are text.
PROPERTY_TYPES: dict[str, type] = {
    "jack_label": str, "panel": str, "room": str, "site": str, "cable_verdict": str,
    **{f"pair_{p}_length_m": float for p in _PAIRS},
    "pair_fault": str, "link_speed_mbps": int, "duplex": str, "poe_class": str,
    "poe_voltage_v": float, "poe_load_w": float, "vlan": int, "voice_vlan": int,
    "dhcp_ok": bool, "dns_ok": bool, "last_tested_at": float, "tester_serial": str,
}


class InfraError(ValueError):
    """A request the infrastructure service refuses; the message never holds a secret."""


class UnknownPropertyError(InfraError):
    """The property name is neither allowlisted nor a well-formed custom.<name>."""


def _text(value: Any, what: str, limit: int = TEXT_MAX) -> str:
    if not isinstance(value, str):
        raise InfraError(f"{what} must be text")
    if any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in value):
        raise InfraError(f"{what} contains control characters")
    if len(value) > limit:
        raise InfraError(f"{what} is too long")
    return value.strip()


def _norm_switch(sid: str) -> str:
    if not isinstance(sid, str) or not SWITCH_ID.match(sid):
        raise InfraError("switch_id must come from switch_id()")
    return sid


def _key(port: str) -> str:
    try:
        return port_key(port)
    except ValueError as exc:
        raise InfraError(str(exc)) from exc


def _canonical(name: str, value: Any) -> str:
    """Validate the value for a property and return its canonical JSON text."""
    want = PROPERTY_TYPES.get(name, str)
    if want is bool:
        ok = isinstance(value, bool)
    elif want is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif want is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        if ok:
            if not math.isfinite(value):
                raise InfraError(f"{name} must be a finite number")
            value = float(value)
    else:
        ok = isinstance(value, str)
    if not ok:
        raise InfraError(f"{name} must be of type {want.__name__}")
    if isinstance(value, str):
        value = _text(value, name, VALUE_MAX)
    return json.dumps(value, sort_keys=True)


class InfraService:
    def __init__(self, store: Store) -> None:
        self._store = store

    def _tx(self, fn: Callable[[Any], Any]) -> Any:
        with self._store._lock, self._store._db:
            return fn(self._store._db)

    async def _run(self, fn: Callable[[Any], Any]) -> Any:
        return await asyncio.to_thread(self._tx, fn)

    # Entities ---------------------------------------------------------------------------

    async def upsert_switch(self, sid: str, *, name: str = "",
                            mgmt_addresses: list[str] | None = None, vendor: str = "",
                            platform: str = "", now: float | None = None) -> str:
        sid = _norm_switch(sid)
        name, vendor, platform = (_text(name, "name"), _text(vendor, "vendor"),
                                  _text(platform, "platform"))
        addrs = sorted({_text(a, "address", 64) for a in (mgmt_addresses or []) if a})
        if len(addrs) > 16:
            raise InfraError("too many management addresses")
        ts = time.time() if now is None else now

        def go(db: Any) -> None:
            db.execute(
                "INSERT INTO infra_switches (switch_id, name, mgmt_addresses, vendor, platform, "
                "first_seen, last_seen) VALUES (?,?,?,?,?,?,?) ON CONFLICT(switch_id) DO UPDATE SET "
                "name=CASE WHEN excluded.name != '' THEN excluded.name ELSE name END, "
                "mgmt_addresses=CASE WHEN excluded.mgmt_addresses != '[]' "
                "THEN excluded.mgmt_addresses ELSE mgmt_addresses END, "
                "vendor=CASE WHEN excluded.vendor != '' THEN excluded.vendor ELSE vendor END, "
                "platform=CASE WHEN excluded.platform != '' THEN excluded.platform ELSE platform END, "
                "last_seen=MAX(last_seen, excluded.last_seen)",
                (sid, name, json.dumps(addrs), vendor, platform, ts, ts))
        await self._run(go)
        return sid

    async def upsert_port(self, sid: str, port: str, *, raw_port_id: str = "",
                          if_index: int | None = None, unifi_index: int | None = None,
                          role: str = "unknown", now: float | None = None) -> str:
        """Create or refresh a port; `port` may be any spelling and the key is returned."""
        sid = _norm_switch(sid)
        key = _key(port)
        if role not in ROLES:
            raise InfraError("role must be access, uplink or unknown")
        for label, n in (("if_index", if_index), ("unifi_index", unifi_index)):
            if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
                raise InfraError(f"{label} must be a non-negative integer")
        raw = _text(raw_port_id, "raw_port_id")
        ts = time.time() if now is None else now

        def go(db: Any) -> None:
            if db.execute("SELECT 1 FROM infra_switches WHERE switch_id=?", (sid,)).fetchone() is None:
                raise InfraError("unknown switch")
            db.execute(
                "INSERT INTO infra_ports (switch_id, port_key, raw_port_id, if_index, unifi_index, "
                "role, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(switch_id, port_key) DO UPDATE SET "
                "raw_port_id=CASE WHEN excluded.raw_port_id != '' THEN excluded.raw_port_id "
                "ELSE raw_port_id END, if_index=COALESCE(excluded.if_index, if_index), "
                "unifi_index=COALESCE(excluded.unifi_index, unifi_index), "
                "role=CASE WHEN excluded.role != 'unknown' THEN excluded.role ELSE role END, "
                "last_seen=MAX(last_seen, excluded.last_seen)",
                (sid, key, raw, if_index, unifi_index, role, ts, ts))
        await self._run(go)
        return key

    async def upsert_jack(self, jack_key: str, *, room: str = "", site: str = "",
                          switch: str | None = None, port: str | None = None,
                          now: float | None = None) -> str:
        """Create or refresh a jack, optionally patched to an existing port."""
        jack = _text(jack_key, "jack_key", 128)
        if not jack:
            raise InfraError("jack_key is empty")
        room, site = _text(room, "room"), _text(site, "site")
        if (switch is None) != (port is None):
            raise InfraError("a patched jack needs both a switch and a port")
        sid = _norm_switch(switch) if switch is not None else None
        key = _key(port) if port is not None else None
        ts = time.time() if now is None else now

        def go(db: Any) -> None:
            if sid is not None and db.execute(
                    "SELECT 1 FROM infra_ports WHERE switch_id=? AND port_key=?",
                    (sid, key)).fetchone() is None:
                raise InfraError("unknown port")
            db.execute(
                "INSERT INTO infra_jacks (jack_key, room, site, switch_id, port_key, first_seen, "
                "last_seen) VALUES (?,?,?,?,?,?,?) ON CONFLICT(jack_key) DO UPDATE SET "
                "room=CASE WHEN excluded.room != '' THEN excluded.room ELSE room END, "
                "site=CASE WHEN excluded.site != '' THEN excluded.site ELSE site END, "
                "switch_id=COALESCE(excluded.switch_id, switch_id), "
                "port_key=COALESCE(excluded.port_key, port_key), "
                "last_seen=MAX(last_seen, excluded.last_seen)",
                (jack, room, site, sid, key, ts, ts))
        await self._run(go)
        return jack

    async def upsert_endpoint(self, kind: str, ref: str, *, mac: str = "", address: str = "",
                              now: float | None = None) -> int:
        """Create or refresh an endpoint and return its id."""
        if kind not in ENDPOINT_KINDS:
            raise InfraError("endpoint kind must be monitor, host or field")
        ref = _text(ref, "ref", 128)
        if not ref:
            raise InfraError("ref is empty")
        mac, address = _text(mac, "mac", 32), _text(address, "address", 64)
        ts = time.time() if now is None else now

        def go(db: Any) -> int:
            db.execute(
                "INSERT INTO infra_endpoints (kind, ref, mac, address, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(kind, ref) DO UPDATE SET "
                "mac=CASE WHEN excluded.mac != '' THEN excluded.mac ELSE mac END, "
                "address=CASE WHEN excluded.address != '' THEN excluded.address ELSE address END, "
                "last_seen=MAX(last_seen, excluded.last_seen)", (kind, ref, mac, address, ts, ts))
            return int(db.execute("SELECT id FROM infra_endpoints WHERE kind=? AND ref=?",
                                  (kind, ref)).fetchone()[0])
        return int(await self._run(go))

    @staticmethod
    def port_ref(sid: str, port: str) -> tuple[str, str]:
        """The (kind, ref) end of a link for a port."""
        return "port", f"{_norm_switch(sid)}|{_key(port)}"

    @staticmethod
    def jack_ref(jack_key: str) -> tuple[str, str]:
        return "jack", _text(jack_key, "jack_key", 128)

    @staticmethod
    def endpoint_ref(endpoint_id: int) -> tuple[str, str]:
        return "endpoint", str(int(endpoint_id))

    async def upsert_link(self, end_a: tuple[str, str], end_b: tuple[str, str], *, source: str,
                          confidence: float = 1.0, now: float | None = None) -> int:
        """Create or confirm an edge between two existing ends, built with port_ref, jack_ref or
        endpoint_ref. Confirming an edge reopens it and moves last_seen forward. Returns its id."""
        if source not in LINK_SOURCES:
            raise InfraError("unknown link source")
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not 0.0 <= confidence <= 1.0):
            raise InfraError("confidence must be between 0 and 1")
        ends = sorted((str(k), str(r)) for k, r in (end_a, end_b))
        for kind, _ in ends:
            if kind not in LINK_KINDS:
                raise InfraError("link end kind must be port, jack or endpoint")
        if ends[0] == ends[1]:
            raise InfraError("a link needs two different ends")
        ts = time.time() if now is None else now

        def exists(db: Any, kind: str, ref: str) -> bool:
            if kind == "port":
                sid, _, key = ref.partition("|")
                q, a = "SELECT 1 FROM infra_ports WHERE switch_id=? AND port_key=?", (sid, key)
            elif kind == "jack":
                q, a = "SELECT 1 FROM infra_jacks WHERE jack_key=?", (ref,)
            else:
                q, a = "SELECT 1 FROM infra_endpoints WHERE id=?", (ref,)
            return db.execute(q, a).fetchone() is not None

        def go(db: Any) -> int:
            for kind, ref in ends:
                if not exists(db, kind, ref):
                    raise InfraError(f"unknown {kind} in link")
            db.execute(
                "INSERT INTO infra_links (a_kind, a_ref, b_kind, b_ref, source, confidence, "
                "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(a_kind, a_ref, b_kind, b_ref, source) DO UPDATE SET "
                "confidence=excluded.confidence, closed_at=NULL, "
                "last_seen=MAX(last_seen, excluded.last_seen)",
                (*ends[0], *ends[1], source, float(confidence), ts, ts))
            return int(db.execute(
                "SELECT id FROM infra_links WHERE a_kind=? AND a_ref=? AND b_kind=? AND b_ref=? "
                "AND source=?", (*ends[0], *ends[1], source)).fetchone()[0])
        return int(await self._run(go))

    # Port properties --------------------------------------------------------------------

    async def append_property(self, sid: str, port: str, name: str, value: Any, *, unit: str = "",
                              source: str, report_id: str = "", observed_at: float | None = None,
                              recorded_by: str = "", now: float | None = None) -> bool:
        """Record a property of a port. Returns True when a new history row was added and False
        when the value equalled the newest one and only last_verified moved."""
        if not isinstance(name, str) or (name not in PROPERTY_TYPES
                                         and not CUSTOM_NAME.match(name)):
            raise UnknownPropertyError("unknown property name")
        custom = name not in PROPERTY_TYPES
        sid = _norm_switch(sid)
        key = _key(port)
        text = _canonical(name, value)
        unit, source = _text(unit, "unit", 16), _text(source, "source", 64)
        report_id = _text(report_id, "report_id", 128)
        recorded_by = _text(recorded_by, "recorded_by", 64)
        if not source:
            raise InfraError("source is required")
        if custom and not recorded_by:
            raise InfraError("a custom property needs recorded_by")
        ts = time.time() if now is None else now
        seen = ts if observed_at is None else float(observed_at)

        def go(db: Any) -> bool:
            if db.execute("SELECT 1 FROM infra_ports WHERE switch_id=? AND port_key=?",
                          (sid, key)).fetchone() is None:
                raise InfraError("unknown port")
            last = db.execute(
                "SELECT id, value, unit FROM port_properties WHERE switch_id=? AND port_key=? "
                "AND name=? ORDER BY id DESC LIMIT 1", (sid, key, name)).fetchone()
            if last is not None and last[1] == text and last[2] == unit:
                db.execute("UPDATE port_properties SET last_verified=MAX(last_verified, ?) "
                           "WHERE id=?", (max(ts, seen), last[0]))
                return False
            db.execute(
                "INSERT INTO port_properties (switch_id, port_key, name, value, unit, source, "
                "report_id, observed_at, recorded_at, recorded_by, last_verified) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, key, name, text, unit, source, report_id, seen, ts, recorded_by,
                 max(ts, seen)))
            return True

        added = bool(await self._run(go))
        if custom:
            await self._store.write_audit(
                "port_property_custom", actor=recorded_by,
                detail={"switch_id": sid, "port_key": key, "name": name, "added": added})
        return added

    async def current_properties(self, sid: str, port: str) -> dict[str, dict[str, Any]]:
        """The newest row for each property name of a port."""
        sid, key = _norm_switch(sid), _key(port)

        def go(db: Any) -> list[tuple[Any, ...]]:
            return db.execute(
                "SELECT name, value, unit, source, report_id, observed_at, recorded_at, "
                "recorded_by, last_verified FROM port_properties WHERE id IN ("
                "SELECT MAX(id) FROM port_properties WHERE switch_id=? AND port_key=? "
                "GROUP BY name) ORDER BY name", (sid, key)).fetchall()
        return {r[0]: self._row(r) for r in await self._run(go)}

    async def property_history(self, sid: str, port: str, name: str,
                               limit: int = 200) -> list[dict[str, Any]]:
        """Every recorded value of one property, newest first."""
        sid, key = _norm_switch(sid), _key(port)
        limit = max(1, min(int(limit), 1000))

        def go(db: Any) -> list[tuple[Any, ...]]:
            return db.execute(
                "SELECT name, value, unit, source, report_id, observed_at, recorded_at, "
                "recorded_by, last_verified FROM port_properties WHERE switch_id=? AND port_key=? "
                "AND name=? ORDER BY id DESC LIMIT ?", (sid, key, name, limit)).fetchall()
        return [self._row(r) for r in await self._run(go)]

    @staticmethod
    def _row(r: tuple[Any, ...]) -> dict[str, Any]:
        keys = ("name", "value", "unit", "source", "report_id", "observed_at", "recorded_at",
                "recorded_by", "last_verified")
        row = dict(zip(keys, r))
        row["value"] = json.loads(row["value"])
        return row
