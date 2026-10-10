"""Per-monitor state machine with confirmation counts.

This is the same idea as ipMonitor's "Up, Warn, Down" progression: a single
failed poll is not an outage. A monitor goes DOWN only after
`failures_to_down` consecutive FAIL results, WARN after that many
consecutive WARN-or-worse results, and back UP after `recoveries_to_up`
consecutive OK results.

A missed reply is handled differently (docs/DATA-API-DESIGN.md section 10.3). The first failed
check that means "nothing answered" moves the monitor to WARN at once, marked degraded with the
message "Degraded: not responding" (a monitor that is already Warning starts the same re-check
without a second WARN transition), and the scheduler re-checks it every `recheck_interval`
seconds. `recheck_good` good replies in a row return it to UP. After a recovery, a new episode
cannot start for `degraded_cooldown` seconds; a miss inside the cooldown is judged by the
ordinary counts, so a host that flaps does not produce a Degraded and Up pair every cycle. If the window of `recheck_window`
seconds ends with no recovery it goes DOWN. A window of 0 turns this off and the counts above
apply to every failure.

PENDING is the state before enough evidence exists. A transition out of
PENDING into UP is recorded but not alerted, so a restart does not page you
with "everything recovered".

The state each monitor was in before a restart is kept in the database (monitor_state) and
handed back as `prior`. The monitor still starts PENDING and earns its state with the ordinary
counts, but when the state it reaches is the prior one it takes it silently, with the prior
`since`: no event, no alert, and the duration keeps counting from the real transition. A
different state is a real change across the restart and is logged and alerted as a transition
from the prior state (up -> down, not pending -> down).
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
    # Part of a re-check episode: its start, or its recovery. Only alert targets that opt in to
    # degraded notices receive these. The Down that ends an episode is an ordinary alert.
    degraded: bool = False

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
    # The fast re-check (section 10.3).
    recheck_window: float = 0.0
    recheck_good: int = 2
    degraded: bool = False
    degraded_since: float = 0.0
    replies: int = 0
    # The least seconds between the recovery that ended one Degraded episode and the start of the
    # next; `degraded_ended` is that recovery's time (None before the first episode ends).
    degraded_cooldown: float = 0.0
    degraded_ended: float | None = None
    # True once a DOWN/WARN alert has actually been sent, so the matching UP
    # alert goes out, and only then. Suppressed or never-alerted problems
    # recover silently.
    alert_open: bool = False
    # The state, and its since, from before a restart; used only while the monitor is PENDING.
    prior: tuple[State, float] | None = None

    @property
    def settled(self) -> State:
        """The state the monitor is known to be in: its prior state while it is PENDING after a
        restart, its state otherwise."""
        if self.state is State.PENDING and self.prior is not None:
            return self.prior[0]
        return self.state

    def _enter(self, state: State, now: float, message: str,
               degraded: bool = False) -> Transition | None:
        """Move to `state`. Out of PENDING after a restart, reaching the prior state is no
        transition (None), and reaching another one is a transition from the prior state."""
        previous = self.state
        if previous is State.PENDING and self.prior is not None:
            (previous, since), self.prior = self.prior, None
            if previous is state:
                self.state, self.since = state, since
                return None
        self.state, self.since = state, now
        return Transition(previous, state, now, message, degraded)

    def _recheck(self, res: CheckResult, now: float) -> Transition | None | bool:
        """One result of a monitor in its re-check window. False means the window does not
        decide this result and the ordinary counts apply."""
        elapsed = now - self.degraded_since
        if res.result is Result.FAIL and res.unreachable:
            self.replies = 0
            self.bad += 1
            self.warnish += 1
            self.good = 0
            if elapsed < self.recheck_window:
                return None
            self.degraded = False
            return self._enter(State.DOWN, now,
                               f"no reply for {elapsed:.0f}s of re-checks: {res.message}")
        if res.result is Result.OK:
            self.replies += 1
            if self.replies < self.recheck_good:
                return None
            self.degraded = False
            self.degraded_ended = now
            self.bad = self.warnish = 0
            self.good = self.replies
            self.replies = 0
            return self._enter(State.UP, now, f"replied again after {elapsed:.0f}s of "
                                              f"degraded: {res.message}", degraded=True)
        # A reply that is wrong or past a threshold is not "not responding". The re-check ends
        # and the ordinary counts decide.
        self.degraded = False
        self.replies = 0
        return False

    def observe(self, res: CheckResult, now: float | None = None, *,
                allow_recheck: bool = True) -> Transition | None:
        """Fold one result into the state. `allow_recheck` is False while a parent is Down: the
        child then shows Unreachable through that parent and does not start its own re-check."""
        now = time.time() if now is None else now
        self.last, self.last_at = res, now
        if self.degraded:
            outcome = self._recheck(res, now)
            if outcome is not False:
                return outcome
        elif (allow_recheck and self.recheck_window > 0 and res.result is Result.FAIL
              and res.unreachable and self.settled is not State.DOWN
              and (self.degraded_ended is None
                   or now - self.degraded_ended >= self.degraded_cooldown)):
            self.degraded, self.degraded_since, self.replies = True, now, 0
            self.bad = self.warnish = 1
            self.good = 0
            if self.settled is State.WARN:
                # Already warning: the fast re-check starts, but WARN to WARN is no transition,
                # so the event log and the alerts carry no second Degraded notice.
                if self.state is State.PENDING:
                    self._enter(State.WARN, now, "")  # the prior Warning, taken silently
                return None
            return self._enter(State.WARN, now, f"Degraded: not responding ({res.message})",
                               degraded=True)
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
        return self._enter(target, now, res.message)
