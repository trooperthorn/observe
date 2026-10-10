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
    # True for a failure that means "no reply" (nothing answered), as opposed to a reply that
    # was wrong or past a threshold. Only these start the fast re-check (section 10.3).
    unreachable: bool = False

    @classmethod
    def fail(cls, message: str, **kw: Any) -> "CheckResult":
        return cls(Result.FAIL, message, **kw)

    @classmethod
    def ok(cls, message: str, **kw: Any) -> "CheckResult":
        return cls(Result.OK, message, **kw)


def unit_suffix(unit: str) -> str:
    """A unit as it follows a number in a message. A UCUM annotation such as "{entity}" names
    what is counted and is left out ("3 entities" is in the message already)."""
    return "" if unit.strip().startswith("{") else unit


def threshold_level(value: float, th: Thresholds, held: Result = Result.OK) -> Result:
    """OK, WARN or FAIL for one value. `held` is the level the thresholds gave the previous
    value: a threshold that held then stays until the value is back past it by its band
    (Thresholds.hysteresis), so a value hovering at the threshold does not flap."""
    def past(limit: float | None, holding: bool) -> bool:
        if limit is None:
            return False
        band = th.band(limit) if holding else 0.0
        return value >= limit - band if th.direction == "above" else value <= limit + band
    if past(th.crit, held is Result.FAIL):
        return Result.FAIL
    if past(th.warn, held is not Result.OK):
        return Result.WARN
    return Result.OK


_RANK = {Result.OK: 0, Result.WARN: 1, Result.FAIL: 2}


def apply_thresholds(res: CheckResult, th: Thresholds | None,
                     held: Result = Result.OK, level: Result | None = None) -> CheckResult:
    """Downgrade an OK result to WARN or FAIL based on its numeric value. `held` is the level
    the thresholds gave the previous value (threshold_level).

    Thresholds never upgrade a failure: a timed-out SNMP poll stays FAIL even
    if a stale value happened to be under the limit.
    """
    if th is None or res.result is Result.FAIL or res.value is None:
        return res
    if level is None:
        level = threshold_level(res.value, th, held)
    unit = unit_suffix(res.unit)
    if level is Result.FAIL:
        res.result = Result.FAIL
        res.message += f" (critical threshold {th.crit:g}{unit} crossed)"
    elif level is Result.WARN:
        res.result = Result.WARN
        res.message += f" (warning threshold {th.warn:g}{unit} crossed)"
    return res


class Check:
    """Base class. Subclasses implement `probe`."""

    def __init__(self, monitor: Any, config: Config) -> None:
        self.monitor = monitor
        self.config = config
        self.timeout: float = config.effective(monitor, "timeout")
        # Set by the scheduler before each run: the monitor is in its fast re-check window.
        self.rechecking: bool = False
        # The level the thresholds gave the last value, for their hysteresis, and how many
        # polls in a row the value has been back below that level (Thresholds.clear_polls).
        self.threshold_held: Result = Result.OK
        self.threshold_clearing: int = 0

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
        th = self.thresholds()
        if th is None or res.result is Result.FAIL or res.value is None:
            return res
        held = self.threshold_held
        level = threshold_level(res.value, th, held)
        if _RANK[level] < _RANK[held]:
            # Back inside the band: the held level clears only after clear_polls such polls.
            self.threshold_clearing += 1
            if self.threshold_clearing < th.clear_after():
                level = held
            else:
                self.threshold_clearing = 0
        else:
            self.threshold_clearing = 0
        self.threshold_held = level
        return apply_thresholds(res, th, held, level)
