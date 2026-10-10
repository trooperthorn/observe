"""The control resources of /api/v2 (docs/DATA-API-DESIGN.md sections 4.2 and 4.10).

Both are admin only and read only. Requesting, cancelling and the host's pull and result routes
stay where they are (the signed command flow of docs/CONTROL.md is unchanged). A read never
writes: a command that expired without an answer is reported as `unknown` from the clock, where
the legacy list also stored that state. Neither resource sends an ETag, because a pull or a
result changes no change domain.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from observe.api import ApiContext, ApiRegistry
from observe.api.cursor import PageParams, encode
from observe.api.models import Page, Ts
from observe.updates import supports_update

from .actions import COMPONENTS, CONTROLLERS, MODES, capabilities
from .queue import ACTIONS, EXPIRABLE_STATES, UPDATE


class CommandResult(BaseModel):
    state: str
    output: str
    output_truncated: bool
    duration_s: float | None = None
    received_at: Ts


class Command(BaseModel):
    id: str
    host: str
    action: str
    params: dict[str, Any]
    requested_by: str
    issued_at: Ts
    expires_at: Ts
    seq: int
    state: str
    result: CommandResult | None = None


class CommandPage(Page):
    items: list[Command]


class CommandFilters(BaseModel):
    host: str | None = Field(None, max_length=128, description="One host")


def list_commands(db: Any, ctx: ApiContext, page: PageParams,
                  filters: CommandFilters) -> dict[str, Any]:
    """Signed commands, newest first, with the state and the latest result of each."""
    import json
    after = page.after(int, str)
    where, args = ["1=1"], []
    if after:
        where.append("(issued_at < ? OR (issued_at = ? AND id > ?))")
        args += [after[0], after[0], after[1]]
    if filters.host:
        where.append("host = ?")
        args.append(filters.host)
    rows = db.execute(
        "SELECT id, host, action, params, requested_by, issued_at, expires_at, seq, state "
        "FROM control_commands WHERE " + " AND ".join(where)
        + " ORDER BY issued_at DESC, id LIMIT ?", (*args, page.limit + 1)).fetchall()
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    items = []
    for r in rows:
        res = db.execute(
            "SELECT state, output, output_truncated, duration_s, received_at "
            "FROM control_results WHERE command_id = ? ORDER BY id DESC LIMIT 1",
            (r[0],)).fetchone()
        state = r[8]
        if state in EXPIRABLE_STATES and r[6] <= int(ctx.now):
            state = "unknown"
        items.append({
            "id": r[0], "host": r[1], "action": r[2], "params": json.loads(r[3]),
            "requested_by": r[4], "issued_at": r[5], "expires_at": r[6], "seq": r[7],
            "state": state,
            "result": None if res is None else {
                "state": res[0], "output": res[1], "output_truncated": bool(res[2]),
                "duration_s": res[3], "received_at": res[4]}})
    return {"items": items,
            "next_cursor": encode([rows[-1][5], rows[-1][0]]) if more else None}


class Capabilities(BaseModel):
    host: str
    known: bool
    actions: list[str]
    capabilities: dict[str, Any]
    controllers: list[str]
    modes: list[str]
    components: list[str]


async def host_capabilities(ctx: ApiContext, host: str = "") -> dict[str, Any]:
    """The actions and what the host last reported, so a client offers only valid choices."""
    data = await ctx.store.latest_host(host) if host else None
    # agent.update is offered only to an agent whose control daemon runs it (observe/updates.py).
    actions = [a for a in ACTIONS if a != UPDATE
               or (data is not None and supports_update(str(data.get("agent_version") or "")))]
    return {"host": host, "known": data is not None, "actions": actions,
            "capabilities": capabilities(data["samples"] if data else []),
            "controllers": list(CONTROLLERS), "modes": list(MODES),
            "components": list(COMPONENTS)}


def register(api: ApiRegistry) -> None:
    api.resource("/control/commands", list_commands, CommandPage, tags=("control",),
                 roles=("admin",), paginate=True, filters=CommandFilters, etag=False,
                 operation_id="commands", summary="List control commands")
    api.resource("/control/capabilities", host_capabilities, Capabilities, tags=("control",),
                 roles=("admin",), etag=False, operation_id="capabilities",
                 summary="What a host can be asked to do")
