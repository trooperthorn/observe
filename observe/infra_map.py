"""Map data, link ageing and inferred dependencies (docs/FIELD-DATA.md).

Ageing. A link that nobody has confirmed for `map.stale_days` is stale and drawn faded. After
twice that it is hidden, but its row is kept. A link whose `closed_at` is set was contradicted
(InfraService closes it when a report puts the jack or the uplink somewhere else) and is
never drawn. Ageing is computed on read from `last_seen` and the injected clock; nothing here
rewrites a link.

Dependencies. A link between two matched monitors proposes that one depends on the other:
an endpoint on its access switch's port, and a switch whose port has role `uplink` on the
switch at the other end of that link. A proposal is strong when its link came from LLDP or CDP
and was confirmed within `stale_days`; anything else is weak. With `map.auto_depends` strong
proposals are applied, weak ones wait for an admin to accept or reject them, and a rejection
holds even for a strong one. The effective dependency set is the YAML plus the applied
edges, handed to Config.set_applied_dependencies, which the rollup and the scheduler already
read. An edge that would create a cycle with the effective set is refused and listed, never
applied. Applied edges are recomputed from the links, so a contradicted or hidden link takes
its edge away again. Nothing here creates, edits or enables a monitor.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import audit
from .config import Config
from .infra import InfraError, InfraService, current_ids_sql
from .infra_match import LiveReader, Matcher

DAY = 86400.0
STRONG_SOURCES = ("lldp", "cdp", "snmp_lldp")
STATE_WORDS = ("up", "warn", "down", "unreachable", "pending")
# Returns (effective state, name of the blocking ancestor or None) for a monitor slug, or None
# when the monitor is not running.
StateOf = Callable[[str], tuple[str, str | None] | None]


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


@dataclass(frozen=True)
class Proposal:
    child: str  # monitor slug that depends on parent
    parent: str
    source: str
    strong: bool
    link_id: int
    last_seen: float

    def as_dict(self, **extra: Any) -> dict[str, Any]:
        return {"child": self.child, "parent": self.parent, "source": self.source,
                "strong": self.strong, "link_id": self.link_id, "last_seen": self.last_seen,
                **extra}


@dataclass
class Plan:
    applied: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    refused: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    configured: list[dict[str, str]]

    def edges(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for e in self.applied:
            out.setdefault(e["child"], []).append(e["parent"])
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"applied": self.applied, "pending": self.pending, "refused": self.refused,
                "rejected": self.rejected, "configured": self.configured}


def _port_parts(ref: str) -> tuple[str, str]:
    sid, _, key = ref.partition("|")
    return sid, key


class MapService:
    def __init__(self, config: Config, infra: InfraService, matcher: Matcher,
                 state_of: StateOf, clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._infra = infra
        self._matcher = matcher
        self._state_of = state_of
        self._clock = clock

    # Dependencies -----------------------------------------------------------------------

    async def _proposals(self, now: float) -> list[Proposal]:
        stale_days = self._config.map.stale_days
        matches = await self._matcher.effective_matches()
        pushed = {m.host: m.slug for m in self._config.monitors
                  if m.type == "pushed_host" and m.enabled}
        enabled = {m.slug for m in self._config.monitors if m.enabled}

        def go(db: Any) -> tuple[list[Any], dict[str, str], dict[str, tuple[str, str]]]:
            links = db.execute(
                "SELECT id, a_kind, a_ref, b_kind, b_ref, source, last_seen, closed_at "
                "FROM infra_links ORDER BY id").fetchall()
            roles = {f"{r[0]}|{r[1]}": r[2] for r in db.execute(
                "SELECT switch_id, port_key, role FROM infra_ports").fetchall()}
            eps = {str(r[0]): (r[1], r[2]) for r in db.execute(
                "SELECT id, kind, ref FROM infra_endpoints").fetchall()}
            return links, roles, eps
        links, roles, endpoints = await self._infra.read(go)

        def monitor_of_endpoint(eid: str) -> str | None:
            kind, ref = endpoints.get(eid, ("", ""))
            slug = ref if kind == "monitor" else pushed.get(ref) if kind == "host" else None
            return slug if slug in enabled else None

        found: dict[tuple[str, str], Proposal] = {}
        for lid, ak, ar, bk, br, source, last_seen, closed_at in links:
            state = link_state(last_seen, closed_at, now, stale_days)
            if state in ("closed", "hidden"):
                continue
            strong = source in STRONG_SOURCES and state == "active"
            pair: tuple[str | None, str | None] = (None, None)
            ends = {ak: ar, bk: br}
            if {ak, bk} == {"endpoint", "port"}:
                child = monitor_of_endpoint(ends["endpoint"])
                parent = matches.get(_port_parts(ends["port"])[0])
                pair = (child, parent)
            elif ak == bk == "port":
                ra, rb = roles.get(ar, "unknown"), roles.get(br, "unknown")
                if (ra == "uplink") != (rb == "uplink"):
                    down, up = (ar, br) if ra == "uplink" else (br, ar)
                    pair = (matches.get(_port_parts(down)[0]), matches.get(_port_parts(up)[0]))
            child, parent = pair
            if not child or not parent or child == parent or child not in enabled \
                    or parent not in enabled:
                continue
            prop = Proposal(child, parent, source, strong, lid, last_seen)
            old = found.get((child, parent))
            if old is None or (prop.strong, prop.last_seen) > (old.strong, old.last_seen):
                found[(child, parent)] = prop
        return sorted(found.values(), key=lambda p: (p.child, p.parent))

    async def _decisions(self) -> dict[tuple[str, str], str]:
        def go(db: Any) -> list[Any]:
            return db.execute("SELECT child, parent, decision FROM infra_dependencies").fetchall()
        return {(c, p): d for c, p, d in await self._infra.read(go)}

    def _configured_edges(self) -> set[tuple[str, str]]:
        return {(m.slug, p.slug) for m in self._config.monitors
                for p in self._config.configured_parents(m)}

    @staticmethod
    def _reaches(edges: dict[str, set[str]], start: str, goal: str) -> bool:
        seen: set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur == goal:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(edges.get(cur, ()))
        return False

    async def plan(self, now: float | None = None) -> Plan:
        ts = self._clock() if now is None else now
        proposals = await self._proposals(ts)
        decisions = await self._decisions()
        auto = self._config.map.auto_depends
        graph: dict[str, set[str]] = {}
        configured = sorted(self._configured_edges())
        for c, p in configured:
            graph.setdefault(c, set()).add(p)
        have = set(configured)
        plan = Plan([], [], [], [], [{"child": c, "parent": p} for c, p in configured])
        # An admin's acceptance is applied before the automatic edges, so when two proposals
        # conflict it is the one the admin chose that stands.
        ordered = sorted(proposals, key=lambda p: (decisions.get((p.child, p.parent)) != "accepted",
                                                   p.child, p.parent))
        for prop in ordered:
            decision = decisions.get((prop.child, prop.parent))
            key = (prop.child, prop.parent)
            if decision == "rejected":
                plan.rejected.append(prop.as_dict())
                continue
            if key in have:
                continue  # the YAML already says so
            wanted = decision == "accepted" or (auto and prop.strong)
            if self._reaches(graph, prop.parent, prop.child):
                plan.refused.append(prop.as_dict(reason="would create a dependency cycle"))
            elif wanted:
                graph.setdefault(prop.child, set()).add(prop.parent)
                plan.applied.append(prop.as_dict(by="admin" if decision == "accepted"
                                                 else "auto"))
            else:
                plan.pending.append(prop.as_dict())
        for part in (plan.applied, plan.pending, plan.refused, plan.rejected):
            part.sort(key=lambda e: (e["child"], e["parent"]))
        return plan

    async def refresh(self, now: float | None = None) -> Plan:
        """Recompute the plan and make the applied edges the effective ones."""
        plan = await self.plan(now)
        self._config.set_applied_dependencies(plan.edges())
        return plan

    async def decide(self, child: str, parent: str, decision: str, actor: str,
                     remote: str = "") -> Plan:
        """An admin accepts or rejects a proposal. Audited either way."""
        if decision not in ("accepted", "rejected"):
            raise InfraError("decision must be accepted or rejected")
        if not actor:
            raise InfraError("deciding needs an actor")
        kind = "infra_depends_" + ("accepted" if decision == "accepted" else "rejected")
        path = "/api/admin/infra/depends/" + ("accept" if decision == "accepted" else "reject")
        detail = {"child": child[:140], "parent": parent[:140]}

        async def fail(reason: str) -> InfraError:
            await audit.record(self._infra._store, "infra_depends_failed", actor=actor,
                               method="POST", path=path, remote=remote,
                               detail={**detail, "decision": decision, "reason": reason})
            return InfraError(reason)

        plan = await self.plan()
        known = [e for part in (plan.applied, plan.pending, plan.refused, plan.rejected)
                 for e in part if (e["child"], e["parent"]) == (child, parent)]
        if not known:
            raise await fail("no such dependency proposal")
        if decision == "accepted" and any("reason" in e for e in known):
            raise await fail("would create a dependency cycle")
        ts = self._clock()

        def go(db: Any) -> None:
            db.execute(
                "INSERT INTO infra_dependencies (child, parent, decision, decided_by, decided_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(child, parent) DO UPDATE SET "
                "decision=excluded.decision, decided_by=excluded.decided_by, "
                "decided_at=excluded.decided_at", (child, parent, decision, actor, ts))
        await self._infra.write(go)
        await audit.record(self._infra._store, kind, actor=actor, method="POST", path=path,
                           status=200, remote=remote, detail=detail)
        return await self.refresh()

    # Map --------------------------------------------------------------------------------

    def monitor_state(self, slug: str | None) -> dict[str, Any]:
        return self._state(slug)

    def _state(self, slug: str | None) -> dict[str, Any]:
        got = self._state_of(slug) if slug else None
        if got is None:
            return {"state": "unknown", "blocked_by": None}
        return {"state": got[0], "blocked_by": got[1]}

    @staticmethod
    def _flag_anchors(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
        """Mark the top-level switches with `anchor`, which the graph view pulls to the centre.

        A switch is an anchor when it has a switch-to-switch link and none of those links is
        through its own uplink port. Switches with no such link are not anchors.
        """
        ports = {n["id"]: n for n in nodes if n["kind"] == "port"}
        linked: set[str] = set()
        below: set[str] = set()
        for e in edges:
            a, b = ports.get(e["a"]), ports.get(e["b"])
            if not a or not b or a["parent"] == b["parent"]:
                continue
            linked.update((a["parent"], b["parent"]))
            if a["role"] == "uplink" and b["role"] != "uplink":
                below.add(a["parent"])
            elif b["role"] == "uplink" and a["role"] != "uplink":
                below.add(b["parent"])
        for n in nodes:
            if n["kind"] == "switch":
                n["anchor"] = n["id"] in linked and n["id"] not in below

    async def map_data(self, site: str | None = None, building: str | None = None,
                       live: LiveReader | None = None, now: float | None = None) -> dict[str, Any]:
        """Nodes and edges with live state, optionally limited to a site and building.

        Switches, and the ports that carry a visible link or a patched jack, are nodes. A
        filter keeps the jacks of that site and building, the ports they reach, the switches of
        those ports, and the switches one uplink away, so the path toward the core stays in the
        picture. A `site` filter also keeps ports whose newest `site` property matches.
        """
        ts = self._clock() if now is None else now
        stale_days = self._config.map.stale_days
        matches = await self._matcher.effective_matches()
        pushed = {m.host: m.slug for m in self._config.monitors
                  if m.type == "pushed_host" and m.enabled}

        def go(db: Any) -> dict[str, list[Any]]:
            return {
                "switches": db.execute("SELECT switch_id, name FROM infra_switches "
                                       "ORDER BY switch_id").fetchall(),
                "ports": db.execute("SELECT switch_id, port_key, role FROM infra_ports "
                                    "ORDER BY switch_id, port_key").fetchall(),
                "jacks": db.execute("SELECT jack_key, room, site, switch_id, port_key "
                                    "FROM infra_jacks ORDER BY jack_key").fetchall(),
                "endpoints": db.execute("SELECT id, kind, ref, address FROM infra_endpoints "
                                        "ORDER BY id").fetchall(),
                "links": db.execute(
                    "SELECT id, a_kind, a_ref, b_kind, b_ref, source, confidence, first_seen, "
                    "last_seen, closed_at FROM infra_links ORDER BY id").fetchall(),
                "sites": db.execute(
                    "SELECT switch_id, port_key, value FROM port_properties WHERE id IN ("
                    + current_ids_sql("WHERE name='site'") + ")").fetchall(),
            }
        d = await self._infra.read(go)

        edges: list[dict[str, Any]] = []
        for lid, ak, ar, bk, br, source, conf, first, last, closed in d["links"]:
            state = link_state(last, closed, ts, stale_days)
            if state in ("closed", "hidden"):
                continue
            edges.append({"id": lid, "a": f"{ak}:{ar}", "b": f"{bk}:{br}", "source": source,
                          "confidence": conf, "state": state, "first_seen": first,
                          "last_seen": last, "age_days": round((ts - last) / DAY, 1)})

        port_site = {f"{s}|{k}": json.loads(v) for s, k, v in d["sites"]}
        jacks: dict[str, dict[str, Any]] = {}
        for key, room, jsite, sid, pkey in d["jacks"]:
            parts = key.split("/")
            jacks[key] = {"room": room, "site": jsite or (parts[0] if len(parts) == 5 else ""),
                          "building": parts[1] if len(parts) == 5 else "",
                          "port": f"{sid}|{pkey}" if sid and pkey else None}

        visible_ports = {r for e in edges for r in (e["a"], e["b"]) if r.startswith("port:")}
        visible_ports = {r[5:] for r in visible_ports}
        visible_ports |= {j["port"] for j in jacks.values() if j["port"]}

        keep_ports: set[str] | None = None
        keep_jacks: set[str] | None = None
        if site or building:
            keep_jacks = {k for k, j in jacks.items()
                          if (not site or j["site"] == site)
                          and (not building or j["building"] == building)}
            keep_ports = {jacks[k]["port"] for k in keep_jacks if jacks[k]["port"]}
            for e in edges:
                for x, y in ((e["a"], e["b"]), (e["b"], e["a"])):
                    if x.startswith("jack:") and x[5:] in keep_jacks and y.startswith("port:"):
                        keep_ports.add(y[5:])
            if site and not building:
                keep_ports |= {p for p, s in port_site.items() if s == site and p in visible_ports}
            keep_switches = {_port_parts(p)[0] for p in keep_ports}
            for e in edges:  # one uplink hop
                if e["a"].startswith("port:") and e["b"].startswith("port:"):
                    sa, sb = _port_parts(e["a"][5:])[0], _port_parts(e["b"][5:])[0]
                    if sa in keep_switches or sb in keep_switches:
                        keep_ports |= {e["a"][5:], e["b"][5:]}
            keep_switches = {_port_parts(p)[0] for p in keep_ports}
        else:
            keep_switches = {s for s, _ in d["switches"]}

        findings: dict[str, list[str]] = {}
        loud: set[str] = set()  # ports with an unacknowledged warning, as on the port page
        if live is not None:
            def acks(db: Any) -> dict[tuple[str, str, str], str]:
                return {(k, s, p): m for k, s, p, m in db.execute(
                    "SELECT kind, switch_id, port_key, message FROM infra_finding_acks")}
            acked = await self._infra.read(acks)
            for f in await self._matcher.findings(live):
                ref = f"{f.switch_id}|{f.port_key}"
                findings.setdefault(ref, []).append(f.kind)
                said = acked.get((f.kind, f.switch_id, f.port_key))
                if f.severity == "warning" and said != f.message:
                    loud.add(ref)

        nodes: list[dict[str, Any]] = []
        names = dict(d["switches"])
        for sid in sorted(keep_switches & set(names)):
            slug = matches.get(sid)
            nodes.append({"id": f"switch:{sid}", "kind": "switch", "label": names[sid] or sid,
                          "monitor": slug, **self._state(slug)})
        roles = {f"{s}|{k}": r for s, k, r in d["ports"]}
        for pref in sorted(visible_ports if keep_ports is None else keep_ports):
            if pref not in roles or _port_parts(pref)[0] not in keep_switches:
                continue
            sid, pkey = _port_parts(pref)
            pm = await self._matcher.port_matches(sid, pkey)
            states = [self._state(m.monitor) for m in pm]
            worst = max((s for s in states if s["state"] != "unknown"),
                        key=lambda s: STATE_WORDS.index(s["state"]) if s["state"] in STATE_WORDS
                        else -1, default=self._state(None))
            node = {"id": f"port:{pref}", "kind": "port", "label": pkey, "parent": f"switch:{sid}",
                    "role": roles[pref], "monitor": pm[0].monitor if pm else None, **worst,
                    "findings": findings.get(pref, [])}
            if pref in loud and node["state"] == "up":
                node["state"] = "warn"  # a passing check does not hide a field fault
            nodes.append(node)
        for key, j in jacks.items():
            if keep_jacks is not None and key not in keep_jacks:
                continue
            nodes.append({"id": f"jack:{key}", "kind": "jack", "label": key, "room": j["room"],
                          "site": j["site"], "building": j["building"], "port": j["port"],
                          "state": "unknown", "blocked_by": None})
        shown = {n["id"] for n in nodes}
        for eid, kind, ref, addr in d["endpoints"]:
            slug = ref if kind == "monitor" else pushed.get(ref) if kind == "host" else None
            if keep_ports is not None and not any(
                    f"endpoint:{eid}" in (e["a"], e["b"]) and ({e["a"], e["b"]} & shown)
                    for e in edges):
                continue
            nodes.append({"id": f"endpoint:{eid}", "kind": "endpoint", "label": ref,
                          "endpoint_kind": kind, "address": addr, "monitor": slug,
                          **self._state(slug)})
        shown = {n["id"] for n in nodes}
        edges = [e for e in edges if e["a"] in shown and e["b"] in shown]
        self._flag_anchors(nodes, edges)
        return {"nodes": nodes, "edges": edges, "stale_days": stale_days,
                "filter": {"site": site, "building": building}}
