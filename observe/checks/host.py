"""Pushed hosts as monitors.

Nothing is polled. The host agent pushes batches to POST /api/ingest, and this
check reads the newest one from the store. Each component (one source and
metric, plus any required source) is Good, Warning or Critical, and the host
is as bad as its worst component. The result then goes through the ordinary
state machine, so `failures_to_down` confirmation, dependencies, groups,
alerts and /metrics apply with no special cases.

Mapping: Critical is FAIL (DOWN once confirmed), Warning is WARN, Good is OK.
No batch within `stale_after` seconds is FAIL, the same as an unreachable host.
A component whose newest sample is older than `stale_after` is stale, also FAIL, even
when a recent batch arrived (an outbox replay or a lagging agent clock).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from icmplib import async_ping
from icmplib.exceptions import ICMPLibError

from ..config import Config, Thresholds
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


async def reachable(address: str, port: int | None, timeout: float) -> bool:
    """Whether the host answers: one ICMP echo, or a TCP connect when a port is given. Used for
    the fast re-check of a pushed host, which does not answer polls (section 10.3)."""
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


class PushedHostCheck(Check):
    def __init__(self, monitor: Any, config: Config, store: Any,
                 clock: Callable[[], float] = time.time,
                 reach: Callable[[str, int | None, float], Awaitable[bool]] = reachable) -> None:
        super().__init__(monitor, config)
        self.store = store
        self.clock = clock
        self.reach = reach
        interval = config.effective(monitor, "interval")
        self.stale_after: float = monitor.stale_after or 3 * interval
        # The batch time that a ping or TCP answer already carried through a re-check. A host
        # that answers while its agent stays silent is not healthy, so the same stale batch is a
        # plain failure the next time and the ordinary counts take it to Down.
        self._answered_for: float | None = None

    def thresholds(self) -> Thresholds | None:
        return None  # the component thresholds are applied here, not to one value

    async def _missed(self, age: float, last_seen: float) -> CheckResult:
        """No batch within the stale window. The first miss is a failure that starts the fast
        re-check. While the monitor is being re-checked, the host's own address is pinged (or its
        port connected to): an answer is a good reply, because a batch may simply be late."""
        m = self.monitor
        detail = {"age_seconds": age, "components": {}}
        text = f"no batch from {m.host} for {age:.0f}s (limit {self.stale_after:.0f}s)"
        if self.rechecking:
            address = m.address or m.host
            port = m.recheck_port
            how = f"TCP port {port}" if port is not None else "ping"
            if await self.reach(address, port, self.timeout):
                self._answered_for = last_seen
                return CheckResult.ok(f"{text}, but {address} answers {how}", value=age, unit="s",
                                      detail=detail)
            text += f", and {address} does not answer {how}"
        if not self.rechecking and self._answered_for == last_seen:
            text += "; the host answered a re-check but the agent is still silent"
            return CheckResult.fail(text, value=age, unit="s", detail=detail)
        return CheckResult.fail(text, value=age, unit="s", detail=detail, unreachable=True)

    async def probe(self) -> CheckResult:
        m = self.monitor
        now = self.clock()
        data = await self.store.latest_host(
            m.host, window=max(self.stale_after, LATEST_WINDOW_S), now=now,
            series=tuple((c.source, c.metric) for c in m.components))
        if data is None:
            return CheckResult.fail(f"no batch ever received from {m.host}",
                                    detail={"components": {}})
        age = now - data["last_seen"]
        if age > self.stale_after:
            return await self._missed(age, data["last_seen"])
        self._answered_for = None

        components: dict[str, str] = {}
        reasons: dict[str, str] = {}

        def mark(name: str, level: str, why: str) -> None:
            if _RANK[level] >= _RANK[components.get(name, GOOD)]:
                components[name] = level
                reasons[name] = why

        for th in m.components:
            name = f"{th.source}.{th.metric}"
            components.setdefault(name, GOOD)
            for s in data["samples"]:
                if s["source"] != th.source or s["metric"] != th.metric or s["value"] is None:
                    continue  # a null value is unavailable, never zero
                if now - s["ts"] > self.stale_after:
                    mark(name, STALE, f"last reading {now - s['ts']:.0f}s old "
                                      f"(limit {self.stale_after:.0f}s)")
                    continue
                level = grade(s["value"], th)
                if level != GOOD:
                    limit = th.crit if level == CRITICAL else th.warn
                    mark(name, level, f"{s['value']:g}{s['unit']} past {limit:g}")
        for src in m.require_sources:
            info = data["sources"].get(src)
            if info is None or not info["available"]:
                why = (info or {}).get("reason") or "not reported"
                mark(src, WARNING, f"source unavailable: {why}")
            else:
                components.setdefault(src, GOOD)

        boot_ts = data.get("boot_ts")
        if data.get("clean_shutdown") == 0 and boot_ts is not None                 and 0 <= now - boot_ts <= m.crash_hold_s:
            mark("boot", CRITICAL if m.crash_result == "fail" else WARNING,
                 f"previous boot ended in a crash {now - boot_ts:.0f}s ago "
                 f"(held for {m.crash_hold_s:.0f}s)")

        worst = max(components.values(), key=_RANK.__getitem__, default=GOOD)
        detail = {"age_seconds": age, "components": components}
        if worst == GOOD:
            return CheckResult.ok(f"{m.host}: all components good", value=age, unit="s",
                                  detail=detail)
        bad = "; ".join(f"{n}: {reasons[n]}" for n in sorted(components)
                        if components[n] == worst)
        result = Result.FAIL if worst in (CRITICAL, STALE) else Result.WARN
        return CheckResult(result, f"{m.host}: {worst}, {bad}", value=age, unit="s",
                           detail=detail)
