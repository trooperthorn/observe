"""The current map, kept in `map_nodes`, `map_edges` and `port_current` (docs/DATA-API-DESIGN.md
section 2.6, slice O-5).

`rebuild` runs inside a write unit, on that unit's own connection, so the rows a feed, a report
or an admin edit changes and the map rows that show them commit together and a reader never
sees one without the other. It only reads the infrastructure tables and the rows it wrote
before, so it needs no clock, config or monitor state of its own: the live columns (a node's
monitor, state, blocked_by and findings, a port's matches and findings) come from an `Overlay`
that the 60 second map hook computes (observe/infra_map.py), and a write-time rebuild carries
the previous values over for every id that is still in the map. Only rows that differ from the
stored ones are written, so an unchanged map costs reads and no writes.

Link ageing is computed here from the injected `now` and `stale_days`; a link that is closed or
hidden is not an edge. The unfiltered map is stored; a site or building filter is applied when
the map is read (`MapService.map_data`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .infra_sql import current_ids_sql
from .storage import Conn

DAY = 86400.0
LIVE_KEYS = ("monitor", "blocked_by", "findings")


def link_state(last_seen: float, closed_at: float | None, now: float, stale_days: int) -> str:
    """active, stale, hidden or closed."""
    if closed_at is not None:
        return "closed"
    age = now - last_seen
    if age > 2 * stale_days * DAY:
        return "hidden"
    if age > stale_days * DAY:
        return "stale"
    return "active"


@dataclass
class Overlay:
    """Live values by node id, and by (switch_id, port_key) for port_current. A node with no
    entry is shown as unknown."""

    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    ports: dict[tuple[str, str], tuple[list[Any], list[Any]]] = field(default_factory=dict)


@dataclass(frozen=True)
class Changed:
    nodes: int = 0
    edges: int = 0
    ports: int = 0

    def __bool__(self) -> bool:
        return bool(self.nodes or self.edges or self.ports)


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _seq(db: Conn, domain: str) -> int:
    """The value the domain's counter takes when this unit commits."""
    return int(db.execute("SELECT seq FROM change_seq WHERE domain=?", (domain,)).fetchone()[0]) + 1


def _carried(db: Conn) -> Overlay:
    out = Overlay()
    for nid, attrs, state in db.execute("SELECT id, attrs, state FROM map_nodes").fetchall():
        old = json.loads(attrs)
        out.nodes[nid] = {"state": state, **{k: old[k] for k in LIVE_KEYS if k in old}}
    for sid, key, matches, findings in db.execute(
            "SELECT switch_id, port_key, matches, findings FROM port_current").fetchall():
        out.ports[(sid, key)] = (json.loads(matches), json.loads(findings))
    return out


def _site_of(jack_key: str, site: str) -> tuple[str, str]:
    parts = jack_key.split("/")
    return site or (parts[0] if len(parts) == 5 else ""), parts[1] if len(parts) == 5 else ""


def _sync(db: Conn, table: str, columns: tuple[str, ...], key: tuple[str, ...],
          old: dict[Any, tuple[Any, ...]], new: dict[Any, tuple[Any, ...]], seq: int) -> int:
    """Make `table` hold `new`: write the rows that are new or differ, delete the rest. `new`
    maps the key to the non-key columns; returns how many rows changed."""
    rest = tuple(c for c in columns if c not in key)
    upserts = [(*(k if isinstance(k, tuple) else (k,)), *vals, seq)
               for k, vals in new.items() if old.get(k) != vals]
    gone = [k if isinstance(k, tuple) else (k,) for k in old if k not in new]
    if upserts:
        marks = ",".join("?" * (len(columns) + 1))
        sets = ", ".join(f"{c}=excluded.{c}" for c in (*rest, "seq"))
        db.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}, seq) VALUES ({marks}) "
            f"ON CONFLICT({', '.join(key)}) DO UPDATE SET {sets}", upserts)
    if gone:
        where = " AND ".join(f"{c}=?" for c in key)
        db.executemany(f"DELETE FROM {table} WHERE {where}", gone)
    return len(upserts) + len(gone)


def rebuild(db: Conn, now: float, stale_days: int, overlay: Overlay | None = None) -> Changed:
    """Bring the three map tables in line with the infrastructure tables. See the module text."""
    live = overlay if overlay is not None else _carried(db)
    switches = db.execute("SELECT switch_id, name, device_type, platform, mgmt_addresses "
                          "FROM infra_switches ORDER BY switch_id").fetchall()
    ports = db.execute("SELECT switch_id, port_key, role FROM infra_ports "
                       "ORDER BY switch_id, port_key").fetchall()
    jacks = db.execute("SELECT jack_key, room, site, switch_id, port_key FROM infra_jacks "
                       "ORDER BY jack_key").fetchall()
    endpoints = db.execute("SELECT id, kind, ref, address FROM infra_endpoints "
                           "ORDER BY id").fetchall()
    links = db.execute(
        "SELECT id, a_kind, a_ref, b_kind, b_ref, source, confidence, first_seen, last_seen, "
        "closed_at FROM infra_links ORDER BY id").fetchall()
    sites = {f"{s}|{k}": json.loads(v) for s, k, v in db.execute(
        "SELECT switch_id, port_key, value FROM port_properties WHERE id IN ("
        + current_ids_sql("WHERE name='site'") + ")").fetchall()}
    props: dict[tuple[str, str], dict[str, Any]] = {}
    for sid, key, name, value, unit, source, seen, verified in db.execute(
            "SELECT switch_id, port_key, name, value, unit, source, observed_at, last_verified "
            "FROM port_properties WHERE id IN (" + current_ids_sql() + ")").fetchall():
        props.setdefault((sid, key), {})[name] = {
            "value": json.loads(value), "unit": unit, "source": source,
            "observed_at": seen, "last_verified": verified}

    edges: dict[str, tuple[Any, ...]] = {}
    ends: set[str] = set()
    for lid, ak, ar, bk, br, source, conf, first, last, closed in links:
        state = link_state(last, closed, now, stale_days)
        if state in ("closed", "hidden"):
            continue
        a, b = f"{ak}:{ar}", f"{bk}:{br}"
        edges[str(lid)] = (a, b, source, _dump({
            "confidence": conf, "first_seen": first, "last_seen": last,
            "age_days": round((now - last) / DAY, 1)}), state)
        ends.update((a, b))

    role = {f"{s}|{k}": r for s, k, r in ports}
    visible = {r[5:] for r in ends if r.startswith("port:")}
    visible |= {f"{sid}|{pkey}" for _, _, _, sid, pkey in jacks if sid and pkey}

    def node(nid: str, kind: str, label: str, site: str, attrs: dict[str, Any],
             with_monitor: bool = True) -> tuple[Any, ...]:
        o = live.nodes.get(nid, {})
        base: dict[str, Any] = {"blocked_by": o.get("blocked_by")}
        if with_monitor:
            base["monitor"] = o.get("monitor")
        return kind, label, site, {**base, **attrs}, o.get("state", "unknown")

    nodes: dict[str, tuple[Any, ...]] = {}
    for sid, name, device_type, platform, addrs in switches:
        # device_type is what the feed classified the device as, empty when no feed knew; the
        # map page names and places a switch by it (observe/static/js/map-logic.js).
        first = json.loads(addrs or "[]")
        nodes[f"switch:{sid}"] = node(f"switch:{sid}", "switch", name or sid, "", {
            "device_type": device_type, "platform": platform,
            "address": first[0] if first else ""})
    for pref in sorted(visible):
        if pref not in role:
            continue
        sid = pref.partition("|")[0]
        site = sites.get(pref)
        o = live.nodes.get(f"port:{pref}", {})
        nodes[f"port:{pref}"] = node(
            f"port:{pref}", "port", pref.partition("|")[2], site if isinstance(site, str) else "",
            {"parent": f"switch:{sid}", "role": role[pref], "findings": o.get("findings", [])})
    for key, room, jsite, sid, pkey in jacks:
        site, building = _site_of(key, jsite)
        nodes[f"jack:{key}"] = node(
            f"jack:{key}", "jack", key, site,
            {"room": room, "site": site, "building": building,
             "port": f"{sid}|{pkey}" if sid and pkey else None}, with_monitor=False)
    for eid, kind, ref, addr in endpoints:
        nodes[f"endpoint:{eid}"] = node(
            f"endpoint:{eid}", "endpoint", ref, "", {"endpoint_kind": kind, "address": addr})
    edges = {i: e for i, e in edges.items() if e[0] in nodes and e[1] in nodes}

    stored_nodes = {nid: (k, label, site, attrs, state) for nid, k, label, site, attrs, state in
                    db.execute("SELECT id, kind, label, site, attrs, state FROM map_nodes")}
    stored_edges = {i: (a, b, k, attrs, state) for i, a, b, k, attrs, state in
                    db.execute("SELECT id, a, b, kind, attrs, state FROM map_edges")}
    stored_ports = {(s, k): (attrs, m, f) for s, k, attrs, m, f in db.execute(
        "SELECT switch_id, port_key, attrs, matches, findings FROM port_current")}

    new_nodes = {nid: (k, label, site, _dump(attrs), state)
                 for nid, (k, label, site, attrs, state) in nodes.items()}
    new_ports: dict[tuple[str, str], tuple[str, str, str]] = {}
    for sid, key, _ in ports:
        matches, findings = live.ports.get((sid, key), ([], []))
        new_ports[(sid, key)] = (_dump(props.get((sid, key), {})), _dump(matches), _dump(findings))

    n_seq, p_seq = _seq(db, "map"), _seq(db, "ports")
    return Changed(
        _sync(db, "map_nodes", ("id", "kind", "label", "site", "attrs", "state"), ("id",),
              stored_nodes, new_nodes, n_seq),
        _sync(db, "map_edges", ("id", "a", "b", "kind", "attrs", "state"), ("id",),
              stored_edges, edges, n_seq),
        _sync(db, "port_current", ("switch_id", "port_key", "attrs", "matches", "findings"),
              ("switch_id", "port_key"), stored_ports, new_ports, p_seq))


def rebuild_and_touch(db: Conn, storage: Any, now: float, stale_days: int,
                      overlay: Overlay | None = None) -> Changed:
    """`rebuild`, then bump the `map` and `ports` change counters of the open unit for the
    tables that really changed, so an unchanged map keeps its counters and its ETags."""
    changed = rebuild(db, now, stale_days, overlay)
    domains = tuple(d for d, n in (("map", changed.nodes + changed.edges),
                                   ("ports", changed.ports)) if n)
    if domains:
        storage.write_sync(lambda _db: None, touches=domains)
    return changed
