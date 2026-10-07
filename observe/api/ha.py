"""Home Assistant instances (docs/DATA-API-DESIGN.md section 4.2): the hosts that report a
Home Assistant section, with the supervisor, container, integration, repair and backup sections
of their host view.

There is no Home Assistant plugin: the data arrives as OpenTelemetry points and logs (pushed by
ha_Int_soc, or polled by a Home Assistant monitor in host mode) and the host view already grades
it. An instance is a host with at least one of these sections reporting, so a Linux host is not
listed. Detail needs a session or a token; it is never anonymous.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ..hostview import _sources_of
from .hosts import FRESH_S, HostViews, _times
from .cursor import PageParams, encode
from .models import IsoTs, Page
from .problems import ApiProblem
from .registry import ApiContext, ApiRegistry

SECTIONS = ("ha", "containers", "integrations", "repairs", "backups")
# A host is a Home Assistant instance when one of these has ever reported.
MARKERS = ("ha", "integrations", "repairs", "backups")


class HaInstance(BaseModel):
    host: str
    platform: str
    agent_version: str
    heard: bool
    last_seen: IsoTs | None = None
    stale: bool
    status: str
    status_reason: str
    sections: dict[str, str]


class HaInstancePage(Page):
    items: list[HaInstance]


class HaInstanceDetail(HaInstance):
    """The summary plus the sections, each with its status, state and graded items."""

    model_config = ConfigDict(extra="allow")


def _is_instance(view: dict[str, Any]) -> bool:
    return any(view[n]["state"] != "not_reported" for n in MARKERS)


def _summary(view: dict[str, Any]) -> dict[str, Any]:
    keys = ("host", "platform", "agent_version", "heard", "last_seen", "stale", "status",
            "status_reason")
    return {**{k: view[k] for k in keys}, "sections": {n: view[n]["status"] for n in SECTIONS}}


async def list_instances(ctx: ApiContext, page: PageParams) -> dict[str, Any]:
    """Hosts that report Home Assistant data, sorted by name."""
    views = HostViews(ctx)
    rows = {r["host"]: r for r in await ctx.store.host_rows()}
    after = page.after(str)
    out: list[dict[str, Any]] = []
    more = False
    for name in views.names(rows):
        if after is not None and name <= after[0]:
            continue
        view = await views.view(name, rows.get(name))
        if view is None or not _is_instance(view):
            continue
        if len(out) == page.limit:
            more = True
            break
        out.append(_times(_summary(view)))
    return {"items": out, "next_cursor": encode([out[-1]["host"]]) if more else None}


async def get_instance(ctx: ApiContext, name: str) -> dict[str, Any]:
    """One instance: supervisor and core state, containers, integrations, repairs and backups."""
    views = HostViews(ctx)
    rows = {r["host"]: r for r in await ctx.store.host_rows()}
    view = await views.view(name, rows.get(name))
    if view is None or not _is_instance(view):
        raise ApiProblem(404, "unknown Home Assistant instance")
    out = _summary(view)
    out.update({n: view[n] for n in SECTIONS})
    wanted = {src for n in SECTIONS for src in _sources_of(n)}
    out["sources"] = [s for s in view["sources"] if s["source"] in wanted]
    out["events"] = view["events"]
    return _times(out)


def register(api: ApiRegistry) -> None:
    sched = api.runtime.scheduler
    if sched is None:
        return
    runtime = api.runtime

    def memory() -> Any:
        return (sched.fingerprint(), int(runtime.wall() // FRESH_S))

    domains = ("hosts", "metrics", "events", "ha")
    api.resource("/ha/instances", list_instances, HaInstancePage, domains=domains, tags=("ha",),
                 paginate=True, anonymous=False, memory=memory,
                 summary="List Home Assistant instances")
    api.resource("/ha/instances/{name:path}", get_instance, HaInstanceDetail, domains=domains,
                 tags=("ha",), anonymous=False, memory=memory,
                 summary="One Home Assistant instance")
