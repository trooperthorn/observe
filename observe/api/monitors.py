"""Monitors, groups and the console status (docs/DATA-API-DESIGN.md section 4.2).

All of it comes from the scheduler's memory, which is the source of truth for state, so these
resources open no read connection except the one availability figure of a single monitor. Their
ETag carries a fingerprint of that memory (Scheduler.fingerprint), because a state can change
after the change counter of its poll was bumped.
"""

from __future__ import annotations

import functools
from typing import Any

from fastapi import Query

from .. import __version__
from .cursor import PageParams, encode
from .models import (GroupOut, GroupPage, MonitorCheckDetail, MonitorDetail, MonitorOut,
                     MonitorPage, StatusOut)
from .problems import ApiProblem
from .registry import ApiContext, ApiRegistry

# Fields a list may be sorted by, and whether each is a number (the rest are text).
SORT_FIELDS = {"slug": False, "name": False, "state": False, "group": False, "type": False,
               "since": True, "last_at": True}
ITEM_FIELDS = tuple(f for f in MonitorOut.model_fields)


def monitor_view(mon: Any, st: Any, sched: Any) -> dict[str, Any]:
    last = st.last
    effective, blocker = sched.rollup.effective(mon.slug)
    fc = sched.forecasts.get(mon.slug)
    target = getattr(mon, "url", None) or getattr(mon, "host", None) or getattr(mon, "query", "")
    port = getattr(mon, "port", None)
    if port and not getattr(mon, "url", None):
        target = f"{target}:{port}"
    return {
        "slug": mon.slug,
        "name": mon.name,
        "group": mon.group,
        "type": mon.type,
        "mode": getattr(mon, "mode", None),
        "target": target,
        "state": st.state.value,
        "degraded": st.degraded,
        "effective_state": effective,
        "blocked_by": blocker,
        "held_by": None if blocker else sched.rollup.degraded_parent(mon.slug),
        "depends_on": [p.name for p in sched.config.parents(mon)],
        "critical": mon.critical,
        "forecast": fc.as_dict() if fc else None,
        "since": st.since,
        "last_at": st.last_at,
        "result": last.result.value if last else None,
        "message": last.message if last else "waiting for first poll",
        "value": last.value if last else None,
        "unit": last.unit if last else "",
        "latency_ms": last.latency_ms if last else None,
        "detail": last.detail if last else {},
    }


def _sort_spec(text: str) -> list[tuple[str, int]]:
    spec: list[tuple[str, int]] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        name, direction = (part[1:], -1) if part.startswith("-") else (part, 1)
        if name not in SORT_FIELDS:
            raise ApiProblem(400, f"sort cannot use {name[:40]!r}; use {', '.join(SORT_FIELDS)}")
        if all(name != n for n, _ in spec):
            spec.append((name, direction))
    if all(n != "slug" for n, _ in spec):
        spec.append(("slug", 1))  # a total order, so a cursor names exactly one place
    return spec


def _key(view: dict[str, Any], spec: list[tuple[str, int]]) -> list[Any]:
    out: list[Any] = []
    for name, _ in spec:
        v = view[name]
        out.append(float(v if v is not None else -1.0) if SORT_FIELDS[name] else str(v or ""))
    return out


def _compare(a: list[Any], b: list[Any], spec: list[tuple[str, int]]) -> int:
    for x, y, (_, direction) in zip(a, b, spec):
        if x != y:
            return direction if x > y else -direction
    return 0


def list_monitors(ctx: ApiContext, page: PageParams,
                  state: str | None = Query(None, max_length=32, description="Effective state"),
                  group: str | None = Query(None, max_length=128),
                  type: str | None = Query(None, max_length=32),
                  q: str | None = Query(None, max_length=128,
                                        description="Text in the name or the target"),
                  sort: str = Query("slug", max_length=128,
                                    description="Fields, comma separated, - for descending: "
                                                + ", ".join(SORT_FIELDS)),
                  include: str | None = Query(None, max_length=64,
                                              description="detail adds the last check detail")
                  ) -> dict[str, Any]:
    """Monitors with their state, from the scheduler's memory."""
    sched = ctx.scheduler
    wanted = {i.strip() for i in (include or "").split(",") if i.strip()}
    if wanted - {"detail"}:
        raise ApiProblem(400, "include accepts only: detail")
    spec = _sort_spec(sort)
    needle = (q or "").lower()
    rows = []
    for m in sched.monitors:
        v = monitor_view(m, sched.states[m.slug], sched)
        if ((state and v["effective_state"] != state) or (group and v["group"] != group)
                or (type and v["type"] != type)
                or (needle and needle not in f"{v['name']} {v['target']}".lower())):
            continue
        rows.append((_key(v, spec), v))
    rows.sort(key=functools.cmp_to_key(lambda a, b: _compare(a[0], b[0], spec)))
    after = page.after(*[float if SORT_FIELDS[n] else str for n, _ in spec])
    if after is not None:
        rows = [r for r in rows if _compare(r[0], [float(x) if SORT_FIELDS[n] else x
                                                   for x, (n, _) in zip(after, spec)], spec) > 0]
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    items = [v if "detail" in wanted else {**v, "detail": None} for _, v in rows]
    return {"items": items, "next_cursor": encode(rows[-1][0]) if more else None}


async def get_monitor(ctx: ApiContext, slug: str) -> dict[str, Any]:
    """One monitor with its availability over the last day and week, from the summary views."""
    sched = ctx.scheduler
    mon = sched.by_slug.get(slug)
    if mon is None:
        raise ApiProblem(404, "unknown monitor")
    view = monitor_view(mon, sched.states[slug], sched)
    view["detail"] = None
    view["availability_24h"] = await ctx.store.availability(slug, 24, now=ctx.now)
    view["availability_7d"] = await ctx.store.availability(slug, 168, now=ctx.now)
    return view


def get_monitor_detail(ctx: ApiContext, slug: str) -> dict[str, Any]:
    """The last check detail of one monitor: ports, components, modes. It can be large, so the
    list leaves it out unless include=detail."""
    sched = ctx.scheduler
    mon = sched.by_slug.get(slug)
    if mon is None:
        raise ApiProblem(404, "unknown monitor")
    last = sched.states[slug].last
    return {"slug": slug, "detail": last.detail if last else {}}


def list_groups(ctx: ApiContext, page: PageParams) -> dict[str, Any]:
    """Groups and their rolled-up state, worst member first within a group."""
    states = ctx.scheduler.rollup.group_states()
    names = sorted(states)
    after = page.after(str)
    if after is not None:
        names = [n for n in names if n > after[0]]
    more = len(names) > page.limit
    names = names[:page.limit]
    items = [{"name": n, "state": states[n]["state"], "counts": states[n]["counts"],
              "worst": states[n]["worst"]} for n in names]
    return {"items": items, "next_cursor": encode([names[-1]]) if more and names else None}


def get_status(ctx: ApiContext) -> dict[str, Any]:
    """The Observe version and the delivery state of each alert target."""
    status = ctx.alerter.status if ctx.alerter is not None else {}
    return {"version": __version__,
            "alerts": [{"name": n, "type": a["type"], "sent": a["sent"],
                        "last_error": a["last_error"]} for n, a in sorted(status.items())]}


def alerts_fingerprint(runtime: Any) -> Any:
    status = runtime.alerter.status if runtime.alerter is not None else {}
    return tuple((n, a["sent"], a["last_error"]) for n, a in sorted(status.items()))


def register(api: ApiRegistry) -> None:
    sched = api.runtime.scheduler
    memory = sched.fingerprint if sched is not None else None
    api.resource("/monitors", list_monitors, MonitorPage, domains=("monitors", "metrics"),
                 tags=("monitors",), paginate=True, sparse=ITEM_FIELDS, memory=memory,
                 summary="List monitors")
    api.resource("/monitors/{slug}", get_monitor, MonitorDetail, domains=("monitors", "metrics"),
                 tags=("monitors",), memory=memory, summary="One monitor")
    api.resource("/monitors/{slug}/detail", get_monitor_detail, MonitorCheckDetail,
                 domains=("monitors",), tags=("monitors",), memory=memory,
                 summary="The last check detail of a monitor")
    api.resource("/groups", list_groups, GroupPage, domains=("monitors",), tags=("monitors",),
                 paginate=True, memory=memory, summary="List groups")
    api.resource("/status", get_status, StatusOut, tags=("status",),
                 memory=lambda: (alerts_fingerprint(api.runtime), __version__),
                 summary="Version and alert delivery")
