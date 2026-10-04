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

import time
from collections.abc import Callable
from typing import Any

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


class PushedHostCheck(Check):
    def __init__(self, monitor: Any, config: Config, store: Any,
                 clock: Callable[[], float] = time.time) -> None:
        super().__init__(monitor, config)
        self.store = store
        self.clock = clock
        interval = config.effective(monitor, "interval")
        self.stale_after: float = monitor.stale_after or 3 * interval

    def thresholds(self) -> Thresholds | None:
        return None  # the component thresholds are applied here, not to one value

    async def probe(self) -> CheckResult:
        m = self.monitor
        now = self.clock()
        data = await self.store.latest_host(m.host)
        if data is None:
            return CheckResult.fail(f"no batch ever received from {m.host}",
                                    detail={"components": {}})
        age = now - data["last_seen"]
        if age > self.stale_after:
            return CheckResult.fail(
                f"no batch from {m.host} for {age:.0f}s (limit {self.stale_after:.0f}s)",
                value=age, unit="s", detail={"age_seconds": age, "components": {}})

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
