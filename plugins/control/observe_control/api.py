"""The control resources of /api/v2 (docs/DATA-API-DESIGN.md sections 4.2 and 4.10).

Both are admin only and read only. Requesting, cancelling and the host's pull and result routes
stay where they are (the signed command flow of docs/CONTROL.md is unchanged). A read never
writes: a command that expired without an answer is reported as `unknown` from the clock, where
the legacy list also stored that state. Neither resource sends an ETag, because a pull or a
result changes no change domain.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from observe.api import ApiContext, ApiRegistry
from observe.api.cursor import PageParams, encode
from observe.api.models import Page, Ts

from .actions import MODES, capabilities, control_form
from .queue import EXPIRABLE_STATES


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


class ControlFanHeader(BaseModel):
    header: str = Field(description="The name in the allowlist, such as fan1")
    controller_id: str = Field(description="The controller's id for it, such as pwm1; a "
                                           "fan.set_floor names this one")
    min_duty_limit: int | None = Field(description="The limit saved for this header")
    floor: int = Field(description="The lowest min_duty the host accepts for this header")


class ControlAllowlist(BaseModel):
    fans: list[ControlFanHeader]
    services: list[str]
    reboot: bool
    update: bool
    min_duty_floor: int


class Capabilities(BaseModel):
    host: str
    known: bool
    available: bool = Field(description="Whether the host has a control daemon that has pulled")
    reason: str = Field(description="Why control is not available, or empty")
    actions: list[str] = Field(description="The actions this host may be asked to do now")
    capabilities: dict[str, Any]
    controller: str = Field(description="The fan controller the host's daemon drives")
    controllers: list[str]
    modes: list[str]
    components: list[str]
    allowlist: ControlAllowlist | None = Field(
        description="The saved allowlist, for a host enrolled with control; null when the "
                    "host's own file is not known to Observe")
    fan_headers: list[ControlFanHeader] | None = Field(
        description="The headers a fan.set_floor may name, or null when nothing is known")


async def control_caps(store: Any, host: str, data: dict[str, Any] | None) -> dict[str, Any]:
    """`control_form` for one host, from its latest batch, its enrolment and its wpc keys."""
    enrolled = await store.fetch(
        "SELECT platform, control, allowlist FROM enrolments WHERE host=?", (host,))
    keys = await store.fetch(
        "SELECT COUNT(*), MAX(last_used) FROM ingest_keys WHERE scope='wpc' "
        "AND revoked_at IS NULL AND host=?", (host,))
    count, last_used = keys[0] if keys else (0, None)
    platform = (enrolled[0][0] if enrolled else "") or str((data or {}).get("platform") or "")
    return control_form(
        platform=platform, agent_version=str((data or {}).get("agent_version") or ""),
        has_key=bool(count), pulled=last_used is not None, enrolled=bool(enrolled),
        control_chosen=bool(enrolled and enrolled[0][1]),
        allowlist=json.loads(enrolled[0][2]) if enrolled else None,
        reported=capabilities(data["samples"] if data else []))


async def host_capabilities(ctx: ApiContext, host: str = "") -> dict[str, Any]:
    """Whether the host has a control daemon, and the choices a request to it may make, so a
    client offers only requests the host's allowlist accepts. agent.update is offered only to
    a Linux or Raspberry Pi agent whose control daemon runs it (observe/updates.py)."""
    data = await ctx.store.latest_host(host) if host else None
    form = await control_caps(ctx.store, host, data) if data is not None else control_form(
        platform="", agent_version="", has_key=False, pulled=False, enrolled=False,
        control_chosen=False, allowlist=None, reported=capabilities([]))
    if data is None:
        form = {**form, "available": False, "reason": "this host has never reported",
                "actions": []}
    return {"host": host, "known": data is not None, "available": form["available"],
            "reason": form["reason"], "actions": form["actions"],
            "capabilities": {"thermalctl": form["thermalctl"], "headers": form["headers"]},
            "controller": form["controller"], "controllers": [form["controller"]],
            "modes": list(MODES), "components": form["components"],
            "allowlist": form["allowlist"], "fan_headers": form["fan_headers"]}


def register(api: ApiRegistry) -> None:
    api.resource("/control/commands", list_commands, CommandPage, tags=("control",),
                 roles=("admin",), paginate=True, filters=CommandFilters, etag=False,
                 operation_id="commands", summary="List control commands")
    api.resource("/control/capabilities", host_capabilities, Capabilities, tags=("control",),
                 roles=("admin",), etag=False, operation_id="capabilities",
                 summary="What a host can be asked to do")
