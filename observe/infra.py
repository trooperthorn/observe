"""Core service API for the infrastructure map (docs/FIELD-DATA.md).

Plugins and core code call this to upsert switches, ports, jacks, links and endpoints and to
append typed port properties. Port names are normalised by observe/portkey.py first, so the
callers may pass whatever spelling they have. Switch ids come from portkey.switch_id().

Port properties are append-only. The current value of a property is the row with the newest
`observed_at` (the later row id breaks a tie), never the row written last, so a late or resent
older observation adds history but cannot replace a newer current value. A value is compared
with the newest row of the same source only, so a device feed (source `unifi`) and a field test
each keep their own history and one never confirms the other. Writing a value
identical to that source's current one adds no row; it moves that row's last_verified forward to the
observation time, so the page can say "confirmed again on ...". Only allowlisted names are accepted, plus
`custom.<name>` for hand-entered properties; a custom write needs an actor and is written to
the audit log. Values are checked for type and length. A property can only be written for a
port that exists, and nothing here creates or changes a monitor.

`InfraTx` holds every write as a plain function of an open transaction connection, so a feed or
a report can run a whole cycle inside one write unit (docs/DATA-API-DESIGN.md section 1.2, slice
O-5) and put a SAVEPOINT around each item. `InfraService` runs the same operations as one write
unit each. Every unit that changes the infrastructure tables also brings the map tables
(observe/map_tables.py) up to date before it commits.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from typing import Any

from . import map_tables
from .infra_sql import current_ids_sql
from .portkey import lldp_port_key, port_key, switch_id, unifi_port_key
from .store import Store

__all__ = ["current_ids_sql", "InfraError", "InfraService", "InfraTx", "PROPERTY_TYPES",
           "UnknownPropertyError", "lldp_port_key", "write_cycle", "port_key", "switch_id", "unifi_port_key"]

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


class InfraTx:
    """The infrastructure writes and lookups on one open transaction connection. Nothing here
    opens, commits or rolls back a transaction; the caller's unit does."""

    def __init__(self, db: Any) -> None:
        self.conn = db

    # Entities ---------------------------------------------------------------------------

    def upsert_switch(self, sid: str, *, name: str = "", mgmt_addresses: list[str] | None = None,
                      vendor: str = "", platform: str = "", now: float | None = None) -> str:
        sid = _norm_switch(sid)
        name, vendor, platform = (_text(name, "name"), _text(vendor, "vendor"),
                                  _text(platform, "platform"))
        addrs = sorted({_text(a, "address", 64) for a in (mgmt_addresses or []) if a})
        if len(addrs) > 16:
            raise InfraError("too many management addresses")
        ts = time.time() if now is None else now
        self.conn.execute(
            "INSERT INTO infra_switches AS t (switch_id, name, mgmt_addresses, vendor, platform, "
            "first_seen, last_seen) VALUES (?,?,?,?,?,?,?) ON CONFLICT(switch_id) DO UPDATE SET "
            "name=CASE WHEN excluded.name != '' THEN excluded.name ELSE t.name END, "
            "mgmt_addresses=CASE WHEN excluded.mgmt_addresses != '[]' "
            "THEN excluded.mgmt_addresses ELSE t.mgmt_addresses END, "
            "vendor=CASE WHEN excluded.vendor != '' THEN excluded.vendor ELSE t.vendor END, "
            "platform=CASE WHEN excluded.platform != '' THEN excluded.platform ELSE t.platform END, "
            "last_seen=MAX(t.last_seen, excluded.last_seen)",
            (sid, name, json.dumps(addrs), vendor, platform, ts, ts))
        return sid

    def upsert_port(self, sid: str, port: str, *, raw_port_id: str = "",
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
        if self.conn.execute("SELECT 1 FROM infra_switches WHERE switch_id=?",
                            (sid,)).fetchone() is None:
            raise InfraError("unknown switch")
        self.conn.execute(
            "INSERT INTO infra_ports AS t (switch_id, port_key, raw_port_id, if_index, unifi_index, "
            "role, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(switch_id, port_key) DO UPDATE SET "
            "raw_port_id=CASE WHEN excluded.raw_port_id != '' THEN excluded.raw_port_id "
            "ELSE t.raw_port_id END, if_index=COALESCE(excluded.if_index, t.if_index), "
            "unifi_index=COALESCE(excluded.unifi_index, t.unifi_index), "
            "role=CASE WHEN excluded.role != 'unknown' THEN excluded.role ELSE t.role END, "
            "last_seen=MAX(t.last_seen, excluded.last_seen)",
            (sid, key, raw, if_index, unifi_index, role, ts, ts))
        return key

    def switch_exists(self, sid: str) -> bool:
        sid = _norm_switch(sid)
        return bool(self.conn.execute("SELECT 1 FROM infra_switches WHERE switch_id=?",
                                     (sid,)).fetchone())

    def port_exists(self, sid: str, port: str) -> bool:
        sid, key = _norm_switch(sid), _key(port)
        return bool(self.conn.execute(
            "SELECT 1 FROM infra_ports WHERE switch_id=? AND port_key=?", (sid, key)).fetchone())

    def upsert_jack(self, jack_key: str, *, room: str = "", site: str = "",
                    switch: str | None = None, port: str | None = None,
                    now: float | None = None) -> str:
        """Create or refresh a jack, optionally patched to an existing port. An observation older
        than the jack's last_seen never moves an existing patch."""
        jack = _text(jack_key, "jack_key", 128)
        if not jack:
            raise InfraError("jack_key is empty")
        room, site = _text(room, "room"), _text(site, "site")
        if (switch is None) != (port is None):
            raise InfraError("a patched jack needs both a switch and a port")
        sid = _norm_switch(switch) if switch is not None else None
        key = _key(port) if port is not None else None
        ts = time.time() if now is None else now
        if sid is not None and self.conn.execute(
                "SELECT 1 FROM infra_ports WHERE switch_id=? AND port_key=?",
                (sid, key)).fetchone() is None:
            raise InfraError("unknown port")
        self.conn.execute(
            "INSERT INTO infra_jacks AS t (jack_key, room, site, switch_id, port_key, first_seen, "
            "last_seen) VALUES (?,?,?,?,?,?,?) ON CONFLICT(jack_key) DO UPDATE SET "
            "room=CASE WHEN excluded.room != '' AND (excluded.last_seen >= t.last_seen "
            "OR t.room = '') THEN excluded.room ELSE t.room END, "
            "site=CASE WHEN excluded.site != '' AND (excluded.last_seen >= t.last_seen "
            "OR t.site = '') THEN excluded.site ELSE t.site END, "
            "switch_id=CASE WHEN excluded.last_seen >= t.last_seen OR t.switch_id IS NULL "
            "THEN COALESCE(excluded.switch_id, t.switch_id) ELSE t.switch_id END, "
            "port_key=CASE WHEN excluded.last_seen >= t.last_seen OR t.switch_id IS NULL "
            "THEN COALESCE(excluded.port_key, t.port_key) ELSE t.port_key END, "
            "last_seen=MAX(t.last_seen, excluded.last_seen)",
            (jack, room, site, sid, key, ts, ts))
        return jack

    def upsert_endpoint(self, kind: str, ref: str, *, mac: str = "", address: str = "",
                        now: float | None = None) -> int:
        """Create or refresh an endpoint and return its id."""
        if kind not in ENDPOINT_KINDS:
            raise InfraError("endpoint kind must be monitor, host or field")
        ref = _text(ref, "ref", 128)
        if not ref:
            raise InfraError("ref is empty")
        mac, address = _text(mac, "mac", 32), _text(address, "address", 64)
        ts = time.time() if now is None else now
        self.conn.execute(
            "INSERT INTO infra_endpoints AS t (kind, ref, mac, address, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(kind, ref) DO UPDATE SET "
            "mac=CASE WHEN excluded.mac != '' THEN excluded.mac ELSE t.mac END, "
            "address=CASE WHEN excluded.address != '' THEN excluded.address ELSE t.address END, "
            "last_seen=MAX(t.last_seen, excluded.last_seen)", (kind, ref, mac, address, ts, ts))
        return int(self.conn.execute("SELECT id FROM infra_endpoints WHERE kind=? AND ref=?",
                                    (kind, ref)).fetchone()[0])

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

    def _end_exists(self, kind: str, ref: str) -> bool:
        if kind == "port":
            sid, _, key = ref.partition("|")
            q, a = "SELECT 1 FROM infra_ports WHERE switch_id=? AND port_key=?", (sid, key)
        elif kind == "jack":
            q, a = "SELECT 1 FROM infra_jacks WHERE jack_key=?", (ref,)
        else:
            q, a = "SELECT 1 FROM infra_endpoints WHERE id=?", (ref,)
        return self.conn.execute(q, a).fetchone() is not None

    def upsert_link(self, end_a: tuple[str, str], end_b: tuple[str, str], *, source: str,
                    confidence: float = 1.0, now: float | None = None) -> int:
        """Create or confirm an edge between two existing ends, built with port_ref, jack_ref or
        endpoint_ref. Confirming an edge reopens it and moves last_seen forward, unless the edge was
        closed after the time given, in which case an older confirmation changes nothing.
        Returns its id."""
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
        for kind, ref in ends:
            if not self._end_exists(kind, ref):
                raise InfraError(f"unknown {kind} in link")
        self.conn.execute(
            "INSERT INTO infra_links AS t (a_kind, a_ref, b_kind, b_ref, source, confidence, "
            "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(a_kind, a_ref, b_kind, b_ref, source) DO UPDATE SET "
            "confidence=CASE WHEN excluded.last_seen >= t.last_seen THEN excluded.confidence "
            "ELSE t.confidence END, "
            "closed_at=CASE WHEN t.closed_at IS NOT NULL AND t.closed_at > excluded.last_seen "
            "THEN t.closed_at ELSE NULL END, "
            "last_seen=MAX(t.last_seen, excluded.last_seen)",
            (*ends[0], *ends[1], source, float(confidence), ts, ts))
        self._close_contradicted(ends, source, ts)
        return int(self.conn.execute(
            "SELECT id FROM infra_links WHERE a_kind=? AND a_ref=? AND b_kind=? AND b_ref=? "
            "AND source=?", (*ends[0], *ends[1], source)).fetchone()[0])

    def close_link(self, end_a: tuple[str, str], end_b: tuple[str, str], *, source: str,
                   now: float | None = None) -> bool:
        """Close one open edge a feed has replaced with a better one (for example a link with
        real port numbers in place of a device-level placeholder). Returns True when an open
        edge was closed. A later confirmation of the same edge reopens it, as for any edge."""
        ends = sorted((str(k), str(r)) for k, r in (end_a, end_b))
        ts = time.time() if now is None else now
        cur = self.conn.execute(
            "UPDATE infra_links SET closed_at=? WHERE a_kind=? AND a_ref=? AND b_kind=? "
            "AND b_ref=? AND source=? AND closed_at IS NULL", (ts, *ends[0], *ends[1], source))
        return bool(cur.rowcount)

    def _close_contradicted(self, ends: list[tuple[str, str]], source: str, ts: float) -> None:
        """A report that puts a jack on another port, or a port against another neighbour,
        closes the older link at once. Only point-to-point kinds contradict: a jack has one
        port and a port has one uplink neighbour. Endpoints do not (a port may serve several),
        and admin `config` links are neither closed nor closing."""
        db = self.conn
        kinds = {ends[0][0], ends[1][0]}
        if source == "config" or kinds not in ({"jack", "port"}, {"port"}):
            return
        mine = {tuple(ends[0]), tuple(ends[1])}
        for end in (ends[0], ends[1]):
            if kinds == {"jack", "port"} and end[0] != "jack":
                continue
            rows = db.execute(
                "SELECT id, a_kind, a_ref, b_kind, b_ref, last_seen FROM infra_links "
                "WHERE closed_at IS NULL "
                "AND source != 'config' AND ((a_kind=? AND a_ref=?) OR (b_kind=? AND b_ref=?))",
                (*end, *end)).fetchall()
            for lid, ak, ar, bk, br, last_seen in rows:
                pair = {(ak, ar), (bk, br)}
                if pair == mine or {ak, bk} != kinds:
                    continue
                if last_seen > ts:
                    # The other link was confirmed after this observation, so this one is the
                    # stale side: it is closed at once and the newer link stays open.
                    db.execute("UPDATE infra_links SET closed_at=? WHERE a_kind=? AND a_ref=? "
                               "AND b_kind=? AND b_ref=? AND source=? AND closed_at IS NULL",
                               (ts, *ends[0], *ends[1], source))
                    continue
                db.execute("UPDATE infra_links SET closed_at=? WHERE id=?", (ts, lid))

    # Port properties --------------------------------------------------------------------

    def append_property(self, sid: str, port: str, name: str, value: Any, *, unit: str = "",
                        source: str, report_id: str = "", observed_at: float | None = None,
                        recorded_by: str = "", now: float | None = None) -> bool:
        """Record a property of a port. Returns True when a new history row was added and False
        when the value equalled the current one and only last_verified moved. An observation
        older than the current row is added to the history and leaves the current row alone.
        A custom property is audited by InfraService, never here."""
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
        db = self.conn
        if db.execute("SELECT 1 FROM infra_ports WHERE switch_id=? AND port_key=?",
                      (sid, key)).fetchone() is None:
            raise InfraError("unknown port")
        last = db.execute(
            "SELECT id, value, unit, observed_at FROM port_properties WHERE switch_id=? AND port_key=? "
            "AND name=? AND source=? ORDER BY observed_at DESC, id DESC LIMIT 1",
            (sid, key, name, source)).fetchone()
        if last is not None and last[3] <= seen:
            if last[1] == text and last[2] == unit:
                db.execute("UPDATE port_properties SET last_verified=MAX(last_verified, ?) "
                           "WHERE id=?", (seen, last[0]))
                return False
        elif last is not None and db.execute(
                "SELECT 1 FROM port_properties WHERE switch_id=? AND port_key=? AND name=? "
                "AND source=? AND observed_at=? AND value=? AND unit=?",
                (sid, key, name, source, seen, text, unit)).fetchone() is not None:
            return False  # this older observation is already in the history
        db.execute(
            "INSERT INTO port_properties (switch_id, port_key, name, value, unit, source, "
            "report_id, observed_at, recorded_at, recorded_by, last_verified) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, key, name, text, unit, source, report_id, seen, ts, recorded_by, seen))
        return True


async def write_cycle(store: Store, fn: Callable[[Any], Any], *, now: float,
                      touches: tuple[str, ...] = ()) -> Any:
    """Run one feed cycle as one write unit: `fn(db)` does the whole cycle (use InfraTx for the
    map writes and a SAVEPOINT per item), then the map tables are brought up to date before the
    single commit. `now` is the cycle's observation time, which link ageing is measured against.
    `touches` names the change domains the cycle changed besides the map; `map` and `ports` are
    bumped only when the map tables really changed."""
    def unit(db: Any) -> Any:
        out = fn(db)
        map_tables.rebuild_and_touch(db, store.storage, now, store.map_stale_days)
        return out
    return await store.storage.write(unit, touches=touches)


class InfraService:
    """The same operations as InfraTx, each as one write unit on the storage writer."""

    def __init__(self, store: Store, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock  # the time link ageing is measured against when a write rebuilds the map

    @property
    def storage(self) -> Any:
        return self._store.storage

    def set_stale_days(self, days: int) -> None:
        """How long an unconfirmed link stays drawn; the map service sets it from the config."""
        self._store.map_stale_days = days

    async def write(self, fn: Callable[[Any], Any], *, rebuild: bool = True) -> Any:
        """Run `fn(db)` as one write unit on the storage writer. Unless `rebuild` is False the
        unit also brings the map tables up to date before it commits."""
        def unit(db: Any) -> Any:
            out = fn(db)
            if rebuild:
                self.rebuild_map(db)
            return out
        return await self._store.storage.write(unit)

    def rebuild_map(self, db: Any, now: float | None = None) -> map_tables.Changed:
        """Update the map tables inside the open unit, keeping the live columns they hold."""
        return map_tables.rebuild_and_touch(
            db, self._store.storage, self._clock() if now is None else now,
            self._store.map_stale_days)

    async def read(self, fn: Callable[[Any], Any]) -> Any:
        """Run `fn(db)` on a read-only pooled connection."""
        return await self._store.storage.read(fn)

    # Entities ---------------------------------------------------------------------------

    async def upsert_switch(self, sid: str, *, name: str = "",
                            mgmt_addresses: list[str] | None = None, vendor: str = "",
                            platform: str = "", now: float | None = None) -> str:
        return str(await self.write(lambda db: InfraTx(db).upsert_switch(
            sid, name=name, mgmt_addresses=mgmt_addresses, vendor=vendor, platform=platform,
            now=now)))

    async def upsert_port(self, sid: str, port: str, *, raw_port_id: str = "",
                          if_index: int | None = None, unifi_index: int | None = None,
                          role: str = "unknown", now: float | None = None) -> str:
        """Create or refresh a port; `port` may be any spelling and the key is returned."""
        return str(await self.write(lambda db: InfraTx(db).upsert_port(
            sid, port, raw_port_id=raw_port_id, if_index=if_index, unifi_index=unifi_index,
            role=role, now=now)))

    async def switch_exists(self, sid: str) -> bool:
        sid = _norm_switch(sid)
        return bool(await self.read(lambda db: InfraTx(db).switch_exists(sid)))

    async def port_exists(self, sid: str, port: str) -> bool:
        sid, key = _norm_switch(sid), _key(port)
        return bool(await self.read(lambda db: InfraTx(db).port_exists(sid, key)))

    async def upsert_jack(self, jack_key: str, *, room: str = "", site: str = "",
                          switch: str | None = None, port: str | None = None,
                          now: float | None = None) -> str:
        """Create or refresh a jack, optionally patched to an existing port. An observation older
        than the jack's last_seen never moves an existing patch."""
        return str(await self.write(lambda db: InfraTx(db).upsert_jack(
            jack_key, room=room, site=site, switch=switch, port=port, now=now)))

    async def upsert_endpoint(self, kind: str, ref: str, *, mac: str = "", address: str = "",
                              now: float | None = None) -> int:
        """Create or refresh an endpoint and return its id."""
        return int(await self.write(lambda db: InfraTx(db).upsert_endpoint(
            kind, ref, mac=mac, address=address, now=now)))

    port_ref = staticmethod(InfraTx.port_ref)
    jack_ref = staticmethod(InfraTx.jack_ref)
    endpoint_ref = staticmethod(InfraTx.endpoint_ref)

    async def upsert_link(self, end_a: tuple[str, str], end_b: tuple[str, str], *, source: str,
                          confidence: float = 1.0, now: float | None = None) -> int:
        """Create or confirm an edge between two existing ends; see InfraTx.upsert_link."""
        return int(await self.write(lambda db: InfraTx(db).upsert_link(
            end_a, end_b, source=source, confidence=confidence, now=now)))

    async def close_link(self, end_a: tuple[str, str], end_b: tuple[str, str], *, source: str,
                         now: float | None = None) -> bool:
        """Close one open edge a feed has replaced with a better one; see InfraTx.close_link."""
        return bool(await self.write(lambda db: InfraTx(db).close_link(
            end_a, end_b, source=source, now=now)))

    # Port properties --------------------------------------------------------------------

    async def append_property(self, sid: str, port: str, name: str, value: Any, *, unit: str = "",
                              source: str, report_id: str = "", observed_at: float | None = None,
                              recorded_by: str = "", now: float | None = None) -> bool:
        """Record a property of a port. Returns True when a new history row was added and False
        when the value equalled the current one and only last_verified moved. An observation
        older than the current row is added to the history and leaves the current row alone."""
        added = bool(await self.write(lambda db: InfraTx(db).append_property(
            sid, port, name, value, unit=unit, source=source, report_id=report_id,
            observed_at=observed_at, recorded_by=recorded_by, now=now)))
        if name not in PROPERTY_TYPES:
            await self._store.write_audit(
                "port_property_custom", actor=recorded_by,
                detail={"switch_id": _norm_switch(sid), "port_key": _key(port), "name": name,
                        "added": added})
        return added

    async def current_properties(self, sid: str, port: str) -> dict[str, dict[str, Any]]:
        """The row with the newest observation for each property name of a port."""
        sid, key = _norm_switch(sid), _key(port)

        def go(db: Any) -> list[tuple[Any, ...]]:
            return db.execute(
                "SELECT name, value, unit, source, report_id, observed_at, recorded_at, "
                "recorded_by, last_verified FROM port_properties WHERE id IN ("
                + current_ids_sql("WHERE switch_id=? AND port_key=?") + ") ORDER BY name",
                (sid, key)).fetchall()
        return {r[0]: self._row(r) for r in await self.read(go)}

    async def property_history(self, sid: str, port: str, name: str,
                               limit: int = 200) -> list[dict[str, Any]]:
        """Every recorded value of one property, newest first."""
        sid, key = _norm_switch(sid), _key(port)
        limit = max(1, min(int(limit), 1000))

        def go(db: Any) -> list[tuple[Any, ...]]:
            return db.execute(
                "SELECT name, value, unit, source, report_id, observed_at, recorded_at, "
                "recorded_by, last_verified FROM port_properties WHERE switch_id=? AND port_key=? "
                "AND name=? ORDER BY observed_at DESC, id DESC LIMIT ?", (sid, key, name, limit)).fetchall()
        return [self._row(r) for r in await self.read(go)]

    @staticmethod
    def _row(r: tuple[Any, ...]) -> dict[str, Any]:
        keys = ("name", "value", "unit", "source", "report_id", "observed_at", "recorded_at",
                "recorded_by", "last_verified")
        row = dict(zip(keys, r))
        row["value"] = json.loads(row["value"])
        return row
