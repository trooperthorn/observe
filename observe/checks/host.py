"""Pushed hosts as monitors.

Nothing is polled. The host agent pushes OTLP metrics and logs to POST /v1/metrics and
/v1/logs, and this check reads the newest data from the store. Each component (one source and
metric, plus any required source) is Good, Warning or Critical, and the host
is as bad as its worst component. The result then goes through the ordinary
state machine, so `failures_to_down` confirmation, dependencies, groups,
alerts and /metrics apply with no special cases.

A component names the OpenTelemetry scope (`hostwatch.collector.<source>`) and metric of the
points it grades; its name in the result is the short collector id and the metric. `require_sources`
names collector ids, as the agent reports them in `observe.source`.

Mapping: Critical is FAIL (DOWN once confirmed), Warning is WARN, Good is OK.
No batch within the silence limit is FAIL, the same as an unreachable host. The limit is the
monitor's `stale_after` when set, else the larger of three monitor intervals and the window of the
host's effective availability tier (observe/tiers.py). A component whose newest sample is older
than the window of its own tier is stale, also FAIL, even when a recent batch arrived (an outbox
replay or a lagging agent clock). The tier rates are read on every probe, so a change in the
console applies without a restart.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

from icmplib import async_ping
from icmplib.exceptions import ICMPLibError

from .. import ignored, tiers
from ..config import Config, Thresholds
from ..otelnames import short_source
from ..units import value_text
from .base import Check, CheckResult, Result

GOOD, WARNING, CRITICAL, STALE = "good", "warning", "critical", "stale"
_RANK = {GOOD: 0, WARNING: 1, CRITICAL: 2, STALE: 2}


def grade(value: float, th: Thresholds) -> str:
    """Good, Warning or Critical for one value. Same comparison as apply_thresholds."""
    if th.direction == "above":
        crit = th.crit is not None and value >= th.crit
        warn = th.warn is not None and value >= th.warn
    else:
        crit = th.crit is not None and value <= th.crit
        warn = th.warn is not None and value <= th.warn
    return CRITICAL if crit else WARNING if warn else GOOD


# Readings older than this are never needed for a current value, so the latest-value
# query does not read them (the larger of this and the stale window is used).
LATEST_WINDOW_S = 900.0


# Resolved host names of the re-check, so that a 10 second re-check does not do a DNS lookup
# every time. An address that is already an IP literal is never looked up.
RESOLVE_TTL_S = 600.0
_resolved: dict[str, tuple[str, float]] = {}


async def _target(address: str) -> str:
    """The IP address to ping or connect to. A literal address is used as it is; a name is
    resolved once and the answer is kept for RESOLVE_TTL_S seconds. A name that does not
    resolve is returned unchanged, so the probe fails the same way it always did."""
    try:
        ipaddress.ip_address(address)
        return address
    except ValueError:
        pass
    now = time.monotonic()
    hit = _resolved.get(address)
    if hit is not None and hit[1] > now:
        return hit[0]
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(address, None, type=socket.SOCK_STREAM)
    except OSError:
        return address
    ip = str(infos[0][4][0])
    _resolved[address] = (ip, now + RESOLVE_TTL_S)
    return ip


async def reachable(address: str, port: int | None, timeout: float) -> bool:
    """Whether the host answers: one ICMP echo, or a TCP connect when a port is given. Used for
    the fast re-check of a pushed host, which does not answer polls (section 10.3). The answer
    says only that the host is on the network, never that its agent is running."""
    address = await _target(address)
    if port is not None:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(address, port), timeout)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True
    try:
        host = await async_ping(address, count=1, timeout=timeout, privileged=False)
    except (ICMPLibError, OSError):
        return False
    return bool(host.is_alive)


def grade_components(monitor: Any, samples: list[dict[str, Any]],
                     sources: dict[str, dict[str, Any]], windows: tiers.Staleness, now: float,
                     boot_ts: float | None, clean_shutdown: int | None
                     ) -> tuple[dict[str, str], dict[str, str]]:
    """The level and reason of each component a pushed_host monitor lists in the YAML: its
    configured thresholds, its required sources and a recent crash. A configured component whose
    newest reading is older than its tier window is stale, which counts as Critical so a replay
    or a lagging clock cannot read as healthy; a stale reading is never graded against its
    thresholds."""
    components: dict[str, str] = {}
    reasons: dict[str, str] = {}

    def mark(name: str, level: str, why: str) -> None:
        if _RANK[level] >= _RANK[components.get(name, GOOD)]:
            components[name] = level
            reasons[name] = why

    for th in monitor.components:
        name = f"{short_source(th.source)}.{th.metric}"
        components.setdefault(name, GOOD)
        for s in samples:
            if s["source"] != th.source or s["metric"] != th.metric or s["value"] is None:
                continue  # a null value is unavailable, never zero
            window = windows.for_scope(s["source"])
            if now - s["ts"] > window:
                mark(name, STALE, f"last reading {now - s['ts']:.0f}s old "
                                  f"(limit {window:.0f}s)")
                continue
            level = grade(s["value"], th)
            if level != GOOD:
                limit = th.crit if level == CRITICAL else th.warn
                mark(name, level, f"{value_text(s['value'], s['unit'])} past "
                                  f"{value_text(limit, s['unit'])}")
    for src in monitor.require_sources:
        info = sources.get(src)
        if info is None or not info["available"]:
            why = (info or {}).get("reason") or "not reported"
            mark(src, WARNING, f"source unavailable: {why}")
        else:
            components.setdefault(src, GOOD)
    if (clean_shutdown == 0 and boot_ts is not None
            and 0 <= now - boot_ts <= monitor.crash_hold_s):
        mark("boot", CRITICAL if monitor.crash_result == "fail" else WARNING,
             f"previous boot ended in a crash {now - boot_ts:.0f}s ago "
             f"(held for {monitor.crash_hold_s:.0f}s)")
    return components, reasons


def verdict_result(view: dict[str, Any], age: float) -> CheckResult:
    """The check result of a host view: the same state and the same reason the Hosts list, the
    host page and its header show, so no page can call the host good while another calls it
    critical (observe/hostview.py decides; this only maps it)."""
    detail = {"age_seconds": age, "components": view["components"]["levels"],
              "status": view["status"], "status_reason": view["status_reason"]}
    if view["status"] == GOOD:
        return CheckResult.ok(f"{view['host']}: all components good", value=age, unit="s",
                              detail=detail)
    result = Result.FAIL if view["status"] == CRITICAL else Result.WARN
    return CheckResult(result, view["status_reason"], value=age, unit="s", detail=detail)


class PushedHostCheck(Check):
    def __init__(self, monitor: Any, config: Config, store: Any,
                 clock: Callable[[], float] = time.time,
                 reach: Callable[[str, int | None, float], Awaitable[bool]] = reachable) -> None:
        super().__init__(monitor, config)
        self.store = store
        self.clock = clock
        self.reach = reach
        self.base_stale: float = 3 * config.effective(monitor, "interval")
        self.stale_after: float = monitor.stale_after or self.base_stale

    async def staleness(self) -> tiers.Staleness:
        """The stale windows from the host's effective tier rates, read fresh each probe."""
        glob, hosts = await tiers.load_rates(self.store)
        return tiers.staleness(self.monitor.host, glob, hosts, base=self.base_stale,
                               explicit=self.monitor.stale_after)

    def thresholds(self) -> Thresholds | None:
        return None  # the component thresholds are applied here, not to one value

    async def _missed(self, age: float) -> CheckResult:
        """No batch within the stale window. The first miss is a failure that starts the fast
        re-check. While the monitor is being re-checked, the host's own address is pinged (or its
        port connected to). An answer shows that the host is reachable but does not make the
        agent a responder: the result stays an unanswered failure, marked reachable-but-silent
        in its detail and message, so the window runs out and the host goes Down with a reason
        that says the agent is silent. Only a new batch is a good reply."""
        m = self.monitor
        detail: dict[str, Any] = {"age_seconds": age, "components": {}}
        text = f"no batch from {m.host} for {age:.0f}s (limit {self.stale_after:.0f}s)"
        if self.rechecking:
            address = m.address or m.host
            port = m.recheck_port
            how = f"TCP port {port}" if port is not None else "ping"
            if await self.reach(address, port, self.timeout):
                detail["reachable"] = True
                detail["reachable_but_silent"] = True
                text += f"; {address} answers {how} but the agent is silent"
            else:
                detail["reachable"] = False
                text += f", and {address} does not answer {how}"
        return CheckResult.fail(text, value=age, unit="s", detail=detail, unreachable=True)

    async def probe(self) -> CheckResult:
        m = self.monitor
        now = self.clock()
        windows = await self.staleness()
        self.stale_after = windows.batch
        data = await self.store.latest_host(
            m.host, window=max(windows.longest, LATEST_WINDOW_S), now=now,
            series=tuple((c.source, c.metric) for c in m.components))
        if data is None:
            return CheckResult.fail(f"no batch ever received from {m.host}",
                                    detail={"components": {}})
        age = now - data["last_seen"]
        if age > self.stale_after:
            return await self._missed(age)

        from .. import hostview  # hostview imports this module's grading

        row = {"host": m.host, "platform": data.get("platform", ""),
               "agent_version": data.get("agent_version", ""), "last_seen": data["last_seen"],
               "boot_ts": data.get("boot_ts"), "clean_shutdown": data.get("clean_shutdown"),
               "confirmed": 1}
        sources = await self.store.host_sources(m.host) if hasattr(self.store, "host_sources") \
            else {k: {**v, "updated": data["last_seen"]} for k, v in data["sources"].items()}
        events = await self.store.host_events(m.host, since=now - hostview.ALERT_WINDOW_S) \
            if hasattr(self.store, "host_events") else []
        skip = (await ignored.load_ignored(self.store)).get(m.host, frozenset())
        view = hostview.build_host_view(
            row, data, sources, events, now, windows, m,
            {(c.source, c.metric): c for c in m.components}, None, skip)
        return verdict_result(view, age)
