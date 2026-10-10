"""Hosts (docs/DATA-API-DESIGN.md section 4.2): the hardware view of every pushed host, a Home
Assistant host or an SNMP host, built from the latest table and the source status, and the hosts
that were enrolled in the console but have not sent a batch yet.

The view itself is built by observe/hostview.py, unchanged. This module finds the host's
monitor (which sets how long a silent host stays fresh), reads the latest values and writes the
timestamps of the result as RFC 3339 strings.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Query

from .. import enrol, hostview, ignored, tiers
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
        self._saved_rates: tuple[dict[str, float], dict[str, dict[str, float]]] | None = None
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

    def overrides(self, host: str) -> dict[tuple[str, str], Any]:
        """Thresholds that replace the built-in ones on this host's page. A pushed_host monitor's
        components apply to its own readings. A Home Assistant monitor in `unavailable` mode on
        the same instance sets the limits of the unavailable-entity count, so the host page grades
        the same crossing with the same severity the dashboard does."""
        mon = self.pushed.get(host)
        out: dict[tuple[str, str], Any] = {(c.source, c.metric): c for c in mon.components} \
            if mon else {}
        ha = self.ha_hosts.get(host)
        if ha is not None:
            for m in self.ctx.scheduler.monitors:
                if (m.type == "homeassistant" and m.mode == "unavailable" and m.enabled
                        and m.thresholds is not None and (m.host, m.port) == (ha.host, ha.port)):
                    out[(hostview.HA_POLL, "observe.ha.entity.unavailable")] = m.thresholds
                    break
        return out

    def names(self, rows: dict[str, dict[str, Any]]) -> list[str]:
        return sorted({*rows, *self.pushed, *self.ha_hosts, *self.snmp_hosts})

    async def _rates(self) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        if self._saved_rates is None:
            self._saved_rates = await tiers.load_rates(self.ctx.store)
        return self._saved_rates

    async def windows(self, host: str) -> tiers.Staleness:
        """How long the host may stay silent before it reads as stale, and how long a reading
        of each polling tier stays current. A Home Assistant or SNMP host is polled by Observe at
        its monitor's interval, so its tiers do not apply; an agent host follows its effective
        tier rates, the same windows as the pushed_host check."""
        config = self.ctx.config
        ha_mon = self.ha_hosts.get(host) or self.snmp_hosts.get(host)
        mon = self.pushed.get(host)
        if mon is None and ha_mon is not None:
            return tiers.Staleness(3 * config.effective(ha_mon, "interval"))
        glob, hosts = await self._rates()
        if mon is not None:
            return tiers.staleness(host, glob, hosts,
                                   base=3 * config.effective(mon, "interval"),
                                   explicit=mon.stale_after)
        return tiers.staleness(host, glob, hosts, base=3 * config.defaults.interval)

    def _monitor_state(self, mon: Any) -> dict[str, Any] | None:
        if mon is None:
            return None
        sched = self.ctx.scheduler
        st = sched.states[mon.slug]
        effective, blocker = sched.rollup.effective(mon.slug)
        return {"slug": mon.slug, "name": mon.name, "state": st.state.value,
                "effective_state": effective, "blocked_by": blocker}

    def _stub(self, host: str) -> dict[str, Any] | None:
        """The row of a host listed in the YAML to which no batch has ever arrived."""
        if host not in self.pushed and host not in self.ha_hosts and host not in self.snmp_hosts:
            return None
        return {"host": host, "platform": "", "agent_version": "", "last_seen": 0.0,
                "confirmed": 1}

    async def view(self, host: str, row: dict[str, Any] | None) -> dict[str, Any] | None:
        ctx = self.ctx
        store = ctx.store
        mon = self.pushed.get(host)
        seen = row is not None
        if row is None:
            row = self._stub(host)
            if row is None:
                return None
            data = None
        now = ctx.now
        stale_after = await self.windows(host)
        skip = (await ignored.load_ignored(store)).get(host, frozenset())
        if seen:
            data = await store.latest_host(
                host, window=max(stale_after.longest, LATEST_WINDOW_S), now=now,
                series=tuple((c.source, c.metric) for c in mon.components) if mon else ())
        overrides = self.overrides(host)
        return hostview.build_host_view(
            row, data, await store.host_sources(host),
            await store.host_events(host, limit=50), now, stale_after, mon, overrides,
            self._monitor_state(mon), skip)

    async def views(self, names: list[str], rows: dict[str, dict[str, Any]]
                    ) -> list[dict[str, Any]]:
        """The views of `names` in the same order, from a constant number of statements. Each
        view equals what `view` returns, except that the events hold only the alert-raising ones,
        which is all the list summary reads."""
        ctx = self.ctx
        now = ctx.now
        wanted: dict[str, tuple[float, tuple[tuple[str, str], ...]]] = {}
        windows = {name: await self.windows(name) for name in names}
        skips = await ignored.load_ignored(ctx.store)
        for name in names:
            if name in rows:
                mon = self.pushed.get(name)
                window = max(windows[name].longest, LATEST_WINDOW_S)
                wanted[name] = (now - window, tuple((c.source, c.metric) for c in mon.components)
                                if mon else ())
        found = await ctx.store.host_list_inputs(
            wanted, now - hostview.ALERT_WINDOW_S) if wanted else {}
        out: list[dict[str, Any]] = []
        for name in names:
            row = rows.get(name)
            mon = self.pushed.get(name)
            if row is None:
                row = self._stub(name)
                if row is None:
                    continue
            got = found.get(name)
            data = {"samples": got["samples"]} if got is not None and name in rows else None
            overrides = self.overrides(name)
            out.append(hostview.build_host_view(
                row, data, got["sources"] if got else {}, got["events"] if got else [], now,
                windows[name], mon, overrides, self._monitor_state(mon),
                skips.get(name, frozenset())))
        return out


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
    shown = await views.views(names[:page.limit + 1], rows)
    out = [_times(hostview.summarize(v)) for v in shown[:page.limit]]
    more = len(shown) > page.limit
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
# What a list row shows that moves with every batch and with the clock. The ETag holds the age
# only as a FRESH_S bucket, so a batch that changes nothing else does not change it.
MOVING_KEYS = ("last_seen", "age_seconds")


def register(api: ApiRegistry) -> None:
    sched = api.runtime.scheduler
    runtime = api.runtime
    kept: dict[str, Any] = {}

    def memory() -> Any:
        return (sched.fingerprint(), int(runtime.wall() // FRESH_S))

    async def list_memory() -> Any:
        """A digest of what the list shows. It is rebuilt only when a change counter, a monitor
        state or the FRESH_S bucket moved, and it ignores a batch that changed no shown value, so
        the ETag stays put while an agent pushes the same picture."""
        now = runtime.wall()
        seqs = runtime.store.storage.change_seqs()
        key = (tuple(seqs.get(d, 0) for d in ("hosts", "metrics", "events", "admin")),
               sched.fingerprint(), int(now // FRESH_S))
        if kept.get("key") == key:
            return kept["digest"], key[2]
        ctx = ApiContext(None, runtime, now)  # type: ignore[arg-type]
        views = HostViews(ctx)
        rows = {r["host"]: r for r in await runtime.store.host_rows()}
        shown = [hostview.summarize(v) for v in await views.views(views.names(rows), rows)]
        for row in shown:
            age = row["age_seconds"]
            row["age_bucket"] = None if age is None else int(age // FRESH_S)
            for k in MOVING_KEYS:
                del row[k]
        digest = hashlib.sha256(json.dumps(shown, sort_keys=True, default=str).encode()
                                ).hexdigest()[:16]
        kept["key"], kept["digest"] = key, digest
        return kept["digest"], key[2]

    # A tier rate change moves the stale windows, so the admin counter is part of the ETag.
    domains = ("hosts", "metrics", "events", "admin")
    # Hardware inventory: a session or a read token, never an anonymous reader. The list ETag is
    # the digest of the rows, not the change counters, which every batch moves.
    api.resource("/hosts", list_hosts, HostPage, domains=domains, tags=("hosts",), paginate=True,
                 anonymous=False, memory=list_memory, counters=False, summary="List hosts")
    api.resource("/waiting-hosts", list_waiting, WaitingPage, tags=("hosts",),
                 anonymous=False, etag=False, summary="Enrolled hosts that have not reported")
    api.resource("/hosts/{name:path}", get_host, HostView, domains=domains, tags=("hosts",),
                 anonymous=False, memory=memory, summary="One host")
