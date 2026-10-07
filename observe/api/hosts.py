"""Hosts (docs/DATA-API-DESIGN.md section 4.2): the hardware view of every pushed host, a Home
Assistant host or an SNMP host, built from the latest table and the source status, and the hosts
that were enrolled in the console but have not sent a batch yet.

The view itself is built by observe/hostview.py, unchanged. This module finds the host's
monitor (which sets how long a silent host stays fresh), reads the latest values and writes the
timestamps of the result as RFC 3339 strings.
"""

from __future__ import annotations

from typing import Any

from fastapi import Query

from .. import enrol, hostview
from ..checks.host import LATEST_WINDOW_S
from .cursor import PageParams, encode
from .models import HostPage, HostView, WaitingPage, rfc3339
from .problems import ApiProblem
from .registry import ApiContext, ApiRegistry

# Keys of the host document that hold a unix timestamp. They are written as RFC 3339 strings.
TIME_KEYS = frozenset({"last_seen", "boot_ts", "ts", "updated"})


def _times(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (rfc3339(v) if k in TIME_KEYS and isinstance(v, (int, float))
                    and not isinstance(v, bool) else _times(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_times(v) for v in value]
    return value


class HostViews:
    """Finds the monitor behind each host name and builds its view."""

    def __init__(self, ctx: ApiContext) -> None:
        self.ctx = ctx
        sched = ctx.scheduler
        self.pushed = {m.host: m for m in sched.monitors if m.type == "pushed_host"}
        # A Home Assistant monitor in host mode ingests its own batches, so its host page uses
        # that monitor's polling interval for the stale window.
        self.ha_hosts = {m.host_name: m for m in sched.monitors
                         if m.type == "homeassistant" and m.mode == "host" and m.enabled}
        # An SNMP monitor with host_name stores its readings under that host. The shortest
        # interval sets the stale window.
        self.snmp_hosts: dict[str, Any] = {}
        for m in sched.monitors:
            if m.type == "snmp" and m.host_name and m.enabled:
                cur = self.snmp_hosts.get(m.host_name)
                if cur is None or (ctx.config.effective(m, "interval")
                                   < ctx.config.effective(cur, "interval")):
                    self.snmp_hosts[m.host_name] = m

    def names(self, rows: dict[str, dict[str, Any]]) -> list[str]:
        return sorted({*rows, *self.pushed, *self.ha_hosts, *self.snmp_hosts})

    async def view(self, host: str, row: dict[str, Any] | None) -> dict[str, Any] | None:
        ctx = self.ctx
        config, store, sched = ctx.config, ctx.store, ctx.scheduler
        mon = self.pushed.get(host)
        seen = row is not None
        ha_mon = (self.ha_hosts.get(host) or self.snmp_hosts.get(host)) if mon is None else None
        if row is None:
            if mon is None and ha_mon is None:
                return None
            # Listed in the YAML but no batch has ever arrived.
            row = {"host": host, "platform": "", "agent_version": "", "last_seen": 0.0,
                   "confirmed": 1}
            data = None
        now = ctx.now
        if mon is not None:
            stale_after = mon.stale_after or 3 * config.effective(mon, "interval")
        elif ha_mon is not None:
            stale_after = 3 * config.effective(ha_mon, "interval")
        else:
            stale_after = 3 * config.defaults.interval
        if seen:
            data = await store.latest_host(
                host, window=max(stale_after, LATEST_WINDOW_S), now=now,
                series=tuple((c.source, c.metric) for c in mon.components) if mon else ())
        state = None
        if mon is not None:
            st = sched.states[mon.slug]
            effective, blocker = sched.rollup.effective(mon.slug)
            state = {"slug": mon.slug, "name": mon.name, "state": st.state.value,
                     "effective_state": effective, "blocked_by": blocker}
        overrides = {(c.source, c.metric): c for c in mon.components} if mon else {}
        return hostview.build_host_view(
            row, data, await store.host_sources(host),
            await store.host_events(host, limit=50), now, stale_after, mon, overrides, state)


async def list_hosts(ctx: ApiContext, page: PageParams,
                     q: str | None = Query(None, max_length=128,
                                           description="Text in the host name")
                     ) -> dict[str, Any]:
    """Every host, with the status of each hardware section, sorted by name."""
    views = HostViews(ctx)
    rows = {r["host"]: r for r in await ctx.store.host_rows()}
    names = views.names(rows)
    if q:
        names = [n for n in names if q.lower() in n.lower()]
    after = page.after(str)
    if after is not None:
        names = [n for n in names if n > after[0]]
    out: list[dict[str, Any]] = []
    more = False
    for name in names:
        view = await views.view(name, rows.get(name))
        if view is None:
            continue
        if len(out) == page.limit:
            more = True
            break
        out.append(_times(hostview.summarize(view)))
    return {"items": out, "next_cursor": encode([out[-1]["host"]]) if more else None}


async def get_host(ctx: ApiContext, name: str) -> dict[str, Any]:
    """One host with every section, its sources and its latest events."""
    views = HostViews(ctx)
    rows = {r["host"]: r for r in await ctx.store.host_rows()}
    view = await views.view(name, rows.get(name))
    if view is None:
        raise ApiProblem(404, "unknown host")
    return _times(view)


async def list_waiting(ctx: ApiContext) -> dict[str, Any]:
    """Hosts enrolled in the console that have not sent a batch yet, each with the address of
    its enrolment page. A host that reported once is an ordinary host from then on."""
    views = HostViews(ctx)
    rows = {r["host"] for r in await ctx.store.host_rows()}
    listed = rows | set(views.pushed) | set(views.ha_hosts) | set(views.snmp_hosts)
    waiting = [w for w in await enrol.waiting_hosts(ctx.store, ctx.now) if w["host"] not in listed]
    return {"items": waiting}


FRESH_S = 10  # a host view shows ages and staleness, so its ETag changes at least this often


def register(api: ApiRegistry) -> None:
    sched = api.runtime.scheduler
    runtime = api.runtime

    def memory() -> Any:
        return (sched.fingerprint(), int(runtime.wall() // FRESH_S))

    domains = ("hosts", "metrics", "events")
    # Hardware inventory: a session or a read token, never an anonymous reader.
    api.resource("/hosts", list_hosts, HostPage, domains=domains, tags=("hosts",), paginate=True,
                 anonymous=False, memory=memory, summary="List hosts")
    api.resource("/waiting-hosts", list_waiting, WaitingPage, tags=("hosts",),
                 anonymous=False, etag=False, summary="Enrolled hosts that have not reported")
    api.resource("/hosts/{name:path}", get_host, HostView, domains=domains, tags=("hosts",),
                 anonymous=False, memory=memory, summary="One host")
