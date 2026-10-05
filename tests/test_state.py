from observe.checks.base import CheckResult, Result, apply_thresholds
from observe.config import Thresholds
from observe.state import MonitorState, State

OK, WARN, FAIL = (CheckResult(r, "") for r in (Result.OK, Result.WARN, Result.FAIL))


def run(st, seq):
    return [t and (t.previous, t.current) for t in (st.observe(r) for r in seq)]


def test_confirmation_before_down_and_recovery():
    st = MonitorState(failures_to_down=3, recoveries_to_up=2)
    out = run(st, [OK, OK, FAIL, FAIL, OK, FAIL, FAIL, FAIL, OK, OK])
    assert out == [None, (State.PENDING, State.UP), None, None, None, None, None,
                   (State.UP, State.DOWN), None, (State.DOWN, State.UP)]


def test_pending_to_up_is_not_alertable_but_pending_to_down_is():
    st = MonitorState(1, 1)
    assert not st.observe(OK).alertable
    st = MonitorState(1, 1)
    assert st.observe(FAIL).alertable


def test_warn_then_fail_escalates():
    st = MonitorState(2, 1)
    out = run(st, [OK, WARN, WARN, FAIL, FAIL, WARN, WARN, OK])
    assert out == [(State.PENDING, State.UP), None, (State.UP, State.WARN), None,
                   (State.WARN, State.DOWN), (State.DOWN, State.WARN), None,
                   (State.WARN, State.UP)]


def test_thresholds_above_and_below_and_never_upgrade():
    th = Thresholds(direction="above", warn=80, crit=95)
    assert apply_thresholds(CheckResult(Result.OK, "", value=85), th).result is Result.WARN
    assert apply_thresholds(CheckResult(Result.OK, "", value=95), th).result is Result.FAIL
    below = Thresholds(direction="below", warn=21, crit=7)
    assert apply_thresholds(CheckResult(Result.OK, "", value=10), below).result is Result.WARN
    assert apply_thresholds(CheckResult(Result.FAIL, "", value=1), th).result is Result.FAIL
