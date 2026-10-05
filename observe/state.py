"""Per-monitor state machine with confirmation counts.

This is the same idea as ipMonitor's "Up, Warn, Down" progression: a single
failed poll is not an outage. A monitor goes DOWN only after
`failures_to_down` consecutive FAIL results, WARN after that many
consecutive WARN-or-worse results, and back UP after `recoveries_to_up`
consecutive OK results.

PENDING is the state before enough evidence exists. A transition out of
PENDING into UP is recorded but not alerted, so a restart does not page you
with "everything recovered".
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field

from .checks.base import CheckResult, Result


class State(str, enum.Enum):
    PENDING = "pending"
    UP = "up"
    WARN = "warn"
    DOWN = "down"


@dataclass
class Transition:
    previous: State
    current: State
    at: float
    message: str

    @property
    def alertable(self) -> bool:
        return not (self.previous is State.PENDING and self.current is State.UP)


@dataclass
class MonitorState:
    failures_to_down: int
    recoveries_to_up: int
    state: State = State.PENDING
    since: float = field(default_factory=time.time)
    bad: int = 0
    warnish: int = 0
    good: int = 0
    last: CheckResult | None = None
    last_at: float | None = None
    # True once a DOWN/WARN alert has actually been sent, so the matching UP
    # alert goes out, and only then. Suppressed or never-alerted problems
    # recover silently.
    alert_open: bool = False

    def observe(self, res: CheckResult, now: float | None = None) -> Transition | None:
        now = time.time() if now is None else now
        self.last, self.last_at = res, now
        if res.result is Result.FAIL:
            self.bad += 1
            self.warnish += 1
            self.good = 0
        elif res.result is Result.WARN:
            self.bad = 0
            self.warnish += 1
            self.good = 0
        else:
            self.bad = self.warnish = 0
            self.good += 1

        target: State | None = None
        if self.bad >= self.failures_to_down:
            target = State.DOWN
        elif self.warnish >= self.failures_to_down and res.result is not Result.OK:
            target = State.WARN
        elif self.good >= self.recoveries_to_up:
            target = State.UP

        if target is None or target is self.state:
            return None
        tr = Transition(self.state, target, now, res.message)
        self.state, self.since = target, now
        return tr
