"""Dependency suppression and group rollup, driven by scripted check results."""

import asyncio

import pytest

from observe.alerts import Alerter
from observe.checks.base import CheckResult, Result
from observe.scheduler import Scheduler
from observe.store import Store

from .conftest import make_config


class Scripted:
    """Stand-in check whose next result is set by the test."""

    def __init__(self, result=Result.OK):
        self.result = result
        self.calls = 0

    def thresholds(self):
        return None

    async def run(self):
        self.calls += 1
        return CheckResult(self.result, self.result.value)


def mon(name, **kw):
    return {"name": name, "type": "tcp", "host": "192.0.2.1", "port": 1, **kw}


def build(monitors, f2d=1):
    cfg = make_config(monitors, defaults={"failures_to_down": f2d, "timeout": 1})
    sched = Scheduler(cfg, Store(":memory:"), Alerter(cfg))
    sent = []

    async def record(monitor, tr):
        sent.append((monitor.slug, tr.current.value, tr.message))

    sched.alerter.notify = record
    for slug in sched.checks:
        sched.checks[slug] = Scripted()
    return sched, sent


async def poll(sched, *slugs):
    for s in slugs:
        await sched.poll_once(sched.by_slug[s])
    await asyncio.sleep(0)  # let alert tasks run
    await asyncio.gather(*sched._pending_alerts)


# --------------------------------------------------------------- config


def test_unknown_parent_and_cycle_rejected():
    with pytest.raises(ValueError, match="unknown monitor"):
        make_config([mon("a", depends_on=["ghost"])])
    with pytest.raises(ValueError, match="cycle"):
        make_config([mon("a", depends_on=["b"]), mon("b", depends_on=["c"]),
                     mon("c", depends_on=["a"])])


def test_forecast_requires_thresholds():
    with pytest.raises(ValueError, match="forecast needs thresholds"):
        make_config([mon("a", forecast=True)])


# ---------------------------------------------------------- suppression


async def test_child_alert_suppressed_while_parent_down():
    sched, sent = build([mon("switch"), mon("server", depends_on=["switch"])])
    await poll(sched, "switch", "server")
    sent.clear()
    sched.checks["switch"].result = Result.FAIL
    sched.checks["server"].result = Result.FAIL
    await poll(sched, "switch", "server")
    assert sent == [("switch", "down", "fail")]
    assert sched.rollup.effective("server") == ("unreachable", "switch")
    events = await sched.store.events(10, "server")
    assert "alert suppressed: switch is down" in events[0]["message"]


async def test_child_failing_first_confirms_parent_on_demand():
    # failures_to_down=3: the server reaches DOWN before the switch's own
    # loop would have. The scheduler must poll the switch until confirmed.
    sched, sent = build([mon("switch"), mon("server", depends_on=["switch"])], f2d=3)
    for _ in range(3):
        await poll(sched, "switch", "server")
    sent.clear()
    sched.checks["switch"].result = Result.FAIL
    sched.checks["server"].result = Result.FAIL
    await poll(sched, "switch")          # switch: 1 failure, not yet DOWN
    for _ in range(3):
        await poll(sched, "server")      # server reaches DOWN on the 3rd
    assert sched.states["switch"].state.value == "down"
    assert [s for s, *_ in sent] == ["switch"]


async def test_transitive_unreachable_attributed_to_root_cause():
    sched, sent = build([mon("core"), mon("access", depends_on=["core"]),
                         mon("nas", depends_on=["access"])])
    await poll(sched, "core", "access", "nas")
    for s in ("core", "access", "nas"):
        sched.checks[s].result = Result.FAIL
    sent.clear()
    await poll(sched, "core", "access", "nas")
    assert sched.rollup.effective("nas") == ("unreachable", "core")
    assert [s for s, *_ in sent] == ["core"]


async def test_no_up_alert_for_suppressed_problem_and_release_on_parent_recovery():
    sched, sent = build([mon("switch"), mon("server", depends_on=["switch"]),
                         mon("printer", depends_on=["switch"])])
    await poll(sched, "switch", "server", "printer")
    for s in ("switch", "server", "printer"):
        sched.checks[s].result = Result.FAIL
    await poll(sched, "switch", "server", "printer")
    sent.clear()
    # Switch and server recover; printer is still broken on its own.
    sched.checks["switch"].result = Result.OK
    sched.checks["server"].result = Result.OK
    await poll(sched, "switch", "server")
    assert ("switch", "up", "ok") in sent
    assert not any(s == "server" for s, *_ in sent)          # never alerted, so no UP
    released = [m for s, st, m in sent if s == "printer"]
    assert released and "still down after switch recovered" in released[0]


# ---------------------------------------------------------------- groups


async def test_group_worst_of_and_non_critical():
    sched, _ = build([mon("fw", group="net"), mon("ap", group="net", critical=False),
                      mon("nas", group="storage")])
    await poll(sched, "fw", "ap", "nas")
    sched.checks["ap"].result = Result.FAIL
    await poll(sched, "ap")
    g = sched.rollup.group_states()
    assert g["net"]["state"] == "warn" and g["net"]["worst"] == ["ap"]
    assert g["storage"]["state"] == "up"
    sched.checks["fw"].result = Result.FAIL
    await poll(sched, "fw")
    assert sched.rollup.group_states()["net"]["state"] == "down"


async def test_never_polled_parent_is_checked_before_child_alerts():
    # Startup race: the child's first poll lands before the parent's.
    sched, sent = build([mon("switch"), mon("server", depends_on=["switch"])])
    sched.checks["switch"].result = Result.FAIL
    sched.checks["server"].result = Result.FAIL
    await poll(sched, "server")
    assert sched.checks["switch"].calls >= 1
    assert [s for s, *_ in sent] == ["switch"]
    assert sched.rollup.effective("server") == ("unreachable", "switch")
