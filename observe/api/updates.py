"""The Updates page's reads of /api/v2 (README "Updating"): the running version and commit,
the upstream check, the state of an update request, and the agents table.

Both resources are admin only. Neither sends an ETag: the state comes from files the host
helper writes, which no change counter covers. The request itself is written by
`POST /api/admin/updates/observe` (admin session and CSRF, observe/web.py), not here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import __version__, updates
from .models import Ts
from .registry import ApiContext, ApiRegistry


class GitHubOut(BaseModel):
    enabled: bool
    ok: bool = Field(description="False means the last check failed or none was made yet.")
    checked_at: Ts | None = None
    latest_commit: str = ""
    latest_tag: str = ""
    repo: str


class OpenRequest(BaseModel):
    id: str
    phase: str
    requested_at: str | None = None


class UpdateState(BaseModel):
    id: str
    phase: str
    message: str
    started_at: Ts | None = None
    updated_at: Ts
    old_commit: str = ""
    new_commit: str = ""
    failed_in: str = Field("", description="The phase a failed update stopped in.")
    log: list[str]
    stale: bool


class UpdateStatus(BaseModel):
    version: str
    commit: str
    update_dir: str
    github: GitHubOut
    request: OpenRequest | None = None
    state: UpdateState | None = None
    phases: list[str]


async def update_status(ctx: ApiContext) -> dict[str, Any]:
    """The running version and commit, the hourly upstream check (when it is on), the open
    request if there is one, and the host helper's progress."""
    now = ctx.now
    check = ctx.runtime.update_check
    return {"version": __version__, "commit": updates.git_commit(),
            "update_dir": ctx.config.server.update_dir,
            "github": await check.refresh(now),
            "request": updates.open_request(ctx.config.server.update_dir, now),
            "state": updates.read_state(ctx.config.server.update_dir, now),
            "phases": list(updates.PHASES)}


class AgentRow(BaseModel):
    host: str
    platform: str
    agent_version: str
    control: bool = Field(description="The host has an active control (wpc) key.")
    control_pulled: bool
    last_pull: Ts | None = None
    pull_age_s: float | None = None
    eligible: bool = Field(description="An agent update can be queued for this host.")
    reason: str = Field(description="Why not, when eligible is false.")


class AgentList(BaseModel):
    items: list[AgentRow]


async def list_agents(ctx: ApiContext) -> dict[str, Any]:
    """Every host that has pushed, with its control daemon state and whether an agent update
    can be queued for it. Pair it with /hosts for the status columns."""
    return {"items": await updates.agent_rows(ctx.store, ctx.now)}


def register(api: ApiRegistry) -> None:
    api.resource("/updates/status", update_status, UpdateStatus, tags=("updates",),
                 roles=("admin",), anonymous=False, etag=False, operation_id="update_status",
                 summary="Version, upstream check and update progress")
    api.resource("/updates/agents", list_agents, AgentList, tags=("updates",),
                 roles=("admin",), anonymous=False, etag=False, operation_id="update_agents",
                 summary="Agent versions and control daemons, for updates")
