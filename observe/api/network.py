"""The map, the ports and the field findings (docs/DATA-API-DESIGN.md section 4.2).

The map is read from `map_nodes` and `map_edges`, which the infrastructure writes and the 60
second hook keep current, so a request never recomputes anything. A port list reads `port_current`.
One port, and the findings, reuse the services the console already had (observe/infra_port.py and
observe/infra_match.py); findings are computed from the newest field properties and the live
monitor state, so their ETag includes the scheduler fingerprint.

Acknowledging a finding is a write. It needs an admin session (the CSRF header is checked by the
registry), is audited by the service, and rebuilds the map so the port stops showing a warning.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..infra import InfraError
from .cursor import PageParams, encode
from .models import Page, Ts
from .problems import ApiProblem
from .registry import ApiContext, ApiRegistry


class MapNode(BaseModel):
    """A switch, port, jack or endpoint. Its kind decides which other fields it has."""

    model_config = ConfigDict(extra="allow")
    id: str
    kind: str
    label: str
    state: str


class MapEdge(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    a: str
    b: str
    source: str
    state: str


class MapFilter(BaseModel):
    site: str | None = None
    building: str | None = None


class MapOut(BaseModel):
    nodes: list[MapNode]
    edges: list[MapEdge]
    stale_days: int
    filter: MapFilter


class MapNodePage(Page):
    items: list[MapNode]


class MapEdgePage(Page):
    items: list[MapEdge]


class PortRow(BaseModel):
    switch_id: str
    port_key: str
    switch_name: str
    raw_port_id: str
    role: str
    first_seen: Ts
    last_seen: Ts
    attrs: dict[str, Any]
    matches: list[Any]
    findings: list[Any]


class PortPage(Page):
    items: list[PortRow]


class PortDetail(BaseModel):
    """One port with its current properties, the history of each, its findings and the monitors
    that are matched to it."""

    model_config = ConfigDict(extra="allow")
    switch_id: str
    port_key: str
    raw_port_id: str
    role: str
    state: str
    switch: dict[str, Any]
    monitors: list[dict[str, Any]]
    properties: dict[str, Any]
    history: dict[str, Any]
    findings: list[dict[str, Any]]


class FindingOut(BaseModel):
    kind: str
    severity: str
    switch_id: str
    port_key: str
    message: str
    acknowledged: bool


class FindingList(BaseModel):
    items: list[FindingOut]


class AckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    switch_id: str = Field(min_length=1, max_length=140)
    port_key: str = Field(min_length=1, max_length=140)
    kind: str = Field(min_length=1, max_length=64)


class Acked(BaseModel):
    ok: bool


def _infra(ctx: ApiContext) -> Any:
    infra = getattr(ctx.runtime, "infra", None)
    if infra is None:
        raise ApiProblem(503, "the infrastructure services are not running")
    return infra


class MapFilters(BaseModel):
    site: str | None = Field(None, max_length=128, description="Only this site")
    building: str | None = Field(None, max_length=128, description="Only this building")


async def get_map(ctx: ApiContext, filters: MapFilters) -> dict[str, Any]:
    """Nodes and edges with live state. A site or building limits the picture to its jacks, the
    ports they reach, those switches and the switches one uplink away."""
    return await _infra(ctx).mapper.map_data(filters.site, filters.building)


class NodeFilters(BaseModel):
    kind: str | None = Field(None, max_length=16, description="switch, port, jack or endpoint")
    site: str | None = Field(None, max_length=128)


def list_nodes(db: Any, page: PageParams, filters: NodeFilters) -> dict[str, Any]:
    """Map nodes in id order."""
    after = page.after(str)
    where, args = ["id > ?"], [after[0] if after else ""]
    for column, value in (("kind", filters.kind), ("site", filters.site)):
        if value:
            where.append(f"{column} = ?")
            args.append(value)
    rows = db.execute(
        "SELECT id, kind, label, attrs, state FROM map_nodes WHERE " + " AND ".join(where)
        + " ORDER BY id LIMIT ?", (*args, page.limit + 1)).fetchall()
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    items = [{"id": r[0], "kind": r[1], "label": r[2], **json.loads(r[3]), "state": r[4]}
             for r in rows]
    return {"items": items, "next_cursor": encode([rows[-1][0]]) if more else None}


class EdgeFilters(BaseModel):
    source: str | None = Field(None, max_length=32, description="How the link was learned")
    state: str | None = Field(None, max_length=16)


def list_edges(db: Any, page: PageParams, filters: EdgeFilters) -> dict[str, Any]:
    """Map edges in id order."""
    after = page.after(str)
    where, args = ["id > ?"], [after[0] if after else ""]
    for column, value in (("kind", filters.source), ("state", filters.state)):
        if value:
            where.append(f"{column} = ?")
            args.append(value)
    rows = db.execute(
        "SELECT id, a, b, kind, attrs, state FROM map_edges WHERE " + " AND ".join(where)
        + " ORDER BY id LIMIT ?", (*args, page.limit + 1)).fetchall()
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    items = [{"id": int(r[0]), "a": r[1], "b": r[2], "source": r[3], "state": r[5],
              **json.loads(r[4])} for r in rows]
    return {"items": items, "next_cursor": encode([rows[-1][0]]) if more else None}


class PortFilters(BaseModel):
    switch_id: str | None = Field(None, max_length=140)
    role: str | None = Field(None, max_length=32)


def list_ports(db: Any, page: PageParams, filters: PortFilters) -> dict[str, Any]:
    """Ports with their current properties, findings and matched monitors, from `port_current`."""
    after = page.after(str, str)
    where, args = ["1=1"], []
    if after:
        where.append("(p.switch_id > ? OR (p.switch_id = ? AND p.port_key > ?))")
        args += [after[0], after[0], after[1]]
    for column, value in (("p.switch_id", filters.switch_id), ("p.role", filters.role)):
        if value:
            where.append(f"{column} = ?")
            args.append(value)
    rows = db.execute(
        "SELECT p.switch_id, p.port_key, s.name, p.raw_port_id, p.role, p.first_seen, "
        "p.last_seen, c.attrs, c.matches, c.findings FROM infra_ports p "
        "JOIN infra_switches s ON s.switch_id = p.switch_id "
        "LEFT JOIN port_current c ON c.switch_id = p.switch_id AND c.port_key = p.port_key "
        "WHERE " + " AND ".join(where) + " ORDER BY p.switch_id, p.port_key LIMIT ?",
        (*args, page.limit + 1)).fetchall()
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    items = [{"switch_id": r[0], "port_key": r[1], "switch_name": r[2], "raw_port_id": r[3],
              "role": r[4], "first_seen": r[5], "last_seen": r[6],
              "attrs": json.loads(r[7] or "{}"), "matches": json.loads(r[8] or "[]"),
              "findings": json.loads(r[9] or "[]")} for r in rows]
    return {"items": items,
            "next_cursor": encode([rows[-1][0], rows[-1][1]]) if more else None}


async def get_port(ctx: ApiContext, switch_id: str, port: str) -> dict[str, Any]:
    """One port: live state, properties and their history, findings and matched monitors."""
    infra = _infra(ctx)
    view = await infra.ports.port_view(switch_id, port, infra.live_port)
    if view is None:
        raise ApiProblem(404, "unknown port")
    return view


async def list_findings(ctx: ApiContext) -> dict[str, Any]:
    """Field conflicts, computed now. Nothing here raises an alert."""
    infra = _infra(ctx)
    found = [f.as_dict() for f in await infra.matcher.findings(infra.live_port)]
    acked = {(r[0], r[1], r[2]): r[3] for r in await ctx.store.fetch(
        "SELECT kind, switch_id, port_key, message FROM infra_finding_acks")}
    return {"items": [{**f, "acknowledged": acked.get(
        (f["kind"], f["switch_id"], f["port_key"])) == f["message"]} for f in found]}


async def ack_finding(ctx: ApiContext, body: AckBody) -> dict[str, Any]:
    """Acknowledge a finding that exists right now. Admin session and CSRF."""
    infra = _infra(ctx)
    try:
        await infra.ports.acknowledge(body.switch_id, body.port_key, body.kind, infra.live_port,
                                      ctx.principal.name)
    except InfraError as err:
        raise ApiProblem(409, str(err)) from None
    await infra.mapper.rebuild()  # an acknowledged finding no longer turns the port to warning
    return {"ok": True}


def register(api: ApiRegistry) -> None:
    runtime = api.runtime
    sched = runtime.scheduler

    def live() -> Any:
        return sched.fingerprint() if sched is not None else ""

    api.resource("/map", get_map, MapOut, domains=("map",), tags=("map",), filters=MapFilters,
                 anonymous=False, summary="The map for a site", operation_id="get_map")
    api.resource("/map/nodes", list_nodes, MapNodePage, domains=("map",), tags=("map",),
                 paginate=True, filters=NodeFilters, anonymous=False, summary="Map nodes")
    api.resource("/map/edges", list_edges, MapEdgePage, domains=("map",), tags=("map",),
                 paginate=True, filters=EdgeFilters, anonymous=False, summary="Map edges")
    api.resource("/ports", list_ports, PortPage, domains=("map", "ports"), tags=("ports",),
                 paginate=True, filters=PortFilters, anonymous=False, summary="List ports")
    api.resource("/ports/{switch_id}/{port:path}", get_port, PortDetail,
                 domains=("map", "ports"), memory=live, tags=("ports",), anonymous=False,
                 summary="One port", operation_id="get_port")
    api.resource("/findings", list_findings, FindingList, domains=("map", "ports"), memory=live,
                 tags=("ports",), anonymous=False, summary="Field findings")
    api.resource("/findings/ack", ack_finding, Acked, tags=("ports",), methods=("POST",),
                 roles=("admin",), anonymous=False, summary="Acknowledge a finding")
