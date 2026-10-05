"""Shared types for checks.

A check returns one CheckResult per run. It never raises for an ordinary
failure (refused, timed out, wrong answer); it returns FAIL with a message
that says what went wrong. An exception escaping a check is treated by the
scheduler as FAIL too, with the exception text, so a bug in one check can
not stall the others.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Config, Thresholds


class Result(str, enum.Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class CheckResult:
    result: Result
    message: str
    value: float | None = None  # the number thresholds apply to, if any
    unit: str = ""
    latency_ms: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def fail(cls, message: str, **kw: Any) -> "CheckResult":
        return cls(Result.FAIL, message, **kw)

    @classmethod
    def ok(cls, message: str, **kw: Any) -> "CheckResult":
        return cls(Result.OK, message, **kw)


def apply_thresholds(res: CheckResult, th: Thresholds | None) -> CheckResult:
    """Downgrade an OK result to WARN or FAIL based on its numeric value.

    Thresholds never upgrade a failure: a timed-out SNMP poll stays FAIL even
    if a stale value happened to be under the limit.
    """
    if th is None or res.result is Result.FAIL or res.value is None:
        return res
    v = res.value
    if th.direction == "above":
        crit = th.crit is not None and v >= th.crit
        warn = th.warn is not None and v >= th.warn
    else:
        crit = th.crit is not None and v <= th.crit
        warn = th.warn is not None and v <= th.warn
    if crit:
        res.result = Result.FAIL
        res.message += f" (critical threshold {th.crit:g}{res.unit} crossed)"
    elif warn:
        res.result = Result.WARN
        res.message += f" (warning threshold {th.warn:g}{res.unit} crossed)"
    return res


class Check:
    """Base class. Subclasses implement `probe`."""

    def __init__(self, monitor: Any, config: Config) -> None:
        self.monitor = monitor
        self.config = config
        self.timeout: float = config.effective(monitor, "timeout")

    def credential(self) -> Any:
        name = getattr(self.monitor, "credential", None)
        return self.config.credentials[name] if name else None

    def thresholds(self) -> Thresholds | None:
        return self.monitor.thresholds

    async def probe(self) -> CheckResult:  # pragma: no cover - abstract
        raise NotImplementedError

    async def run(self) -> CheckResult:
        start = time.perf_counter()
        res = await self.probe()
        if res.latency_ms is None:
            res.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        return apply_thresholds(res, self.thresholds())
