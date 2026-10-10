"""The state kept across a restart (monitor_state): a monitor that comes back in the state it was
in before is neither logged nor alerted and keeps its since; a real change across the restart
is logged and alerted as a change from the kept state."""

from __future__ import annotations

import asyncio
import sqlite3

from observe.alerts import Alerter
from observe.checks.base import CheckResult, Result
from observe.scheduler import Scheduler
from observe.state import MonitorState, State, Transition
from observe.storage.schema import MIGRATIONS, migrate
from observe.store import Store

from .conftest import enqueue_stub
from .dbq import settle
from .test_recheck import Env, mon, storage  # noqa: F401 - storage is a fixture

OK, WARN = CheckResult(Result.OK, "fine"), CheckResult(Result.WARN, "slow")
FAIL = CheckResult(Result.FAIL, "refused")


def restart(env: Env) -> tuple[Scheduler, list[tuple[str, str]]]:
    """A second scheduler on the same store, with the same scripted probes."""
    fresh = Scheduler(env.cfg, env.store, Alerter(env.cfg), clock=env.clock)
    sent: list[tuple[str, str]] = []

    async def record(monitor, tr):
        sent.append((tr.previous.value, tr.current.value))

    fresh.alerter.enqueue = enqueue_stub(record)
    for slug, probe in env.probes.items():
        fresh.checks[slug] = probe
    return fresh, sent


async def poll(sched: Scheduler, slug: str, times: int = 1) -> None:
    for _ in range(times):
        sched.clock.now += 60
        await sched.poll_once(sched.by_slug[slug])
    await asyncio.gather(*sched._pending_alerts)


async def test_up_before_and_after_a_long_restart_is_quiet_and_keeps_its_since(storage):
    env = Env(storage, [mon("a")], recheck_window=0)
    await env.poll("a")
    since = env.st("a").since
    await settle(storage)
    env.clock.now += 3600  # far past three intervals: the last result is not trusted
    fresh, sent = restart(env)
    await fresh.restore(fresh.by_slug["a"])
    assert fresh.states["a"].state is State.PENDING
    await poll(fresh, "a")
    assert fresh.states["a"].state is State.UP and fresh.states["a"].since == since
    assert sent == [] and len(await env.store.events(monitor="a")) == 1  # only the first


async def test_a_fresh_up_comes_back_at_once_with_its_transition_time(storage):
    env = Env(storage, [mon("a")])
    await env.poll("a")
    since = env.st("a").since
    await env.poll("a", 60)
    await settle(storage)
    env.clock.now += 30
    fresh, _ = restart(env)
    await fresh.restore(fresh.by_slug["a"])
    assert fresh.states["a"].state is State.UP
    assert fresh.states["a"].since == since  # not the time of the last poll


async def test_a_quiet_outage_is_not_announced_again_after_a_restart(storage):
    env = Env(storage, [mon("a")], failures_to_down=1, recheck_window=0)
    env.probes["a"].result = FAIL
    await env.poll("a")
    since = env.st("a").since
    # The alert was suppressed (or held) before the restart, so no open mark exists.
    await env.store.set_alert_open("a", None, env.clock.now)
    await settle(storage)
    fresh, sent = restart(env)
    await fresh.restore(fresh.by_slug["a"])
    assert fresh.states["a"].state is State.PENDING
    await poll(fresh, "a", 2)
    st = fresh.states["a"]
    assert st.state is State.DOWN and st.since == since
    assert sent == []
    assert [e["current"] for e in await env.store.events(monitor="a")] == ["down"]


async def test_a_real_change_across_a_restart_is_logged_and_alerted(storage):
    env = Env(storage, [mon("a")], failures_to_down=1, recheck_window=0)
    await env.poll("a")
    await settle(storage)
    env.clock.now += 3600
    fresh, sent = restart(env)
    await fresh.restore(fresh.by_slug["a"])
    env.probes["a"].result = FAIL
    await poll(fresh, "a")
    assert fresh.states["a"].state is State.DOWN
    assert fresh.states["a"].since == env.clock.now
    assert sent == [("up", "down")]
    newest = (await env.store.events(monitor="a"))[0]
    assert (newest["previous"], newest["current"]) == ("up", "down")
    # The new state is kept in turn.
    assert (await env.store.monitor_states())["a"] == ("down", env.clock.now)


async def test_a_recovery_across_a_restart_is_logged(storage):
    env = Env(storage, [mon("a")], failures_to_down=1, recheck_window=0)
    env.probes["a"].result = WARN
    await env.poll("a")
    await env.store.set_alert_open("a", None, env.clock.now)
    await settle(storage)
    fresh, sent = restart(env)
    await fresh.restore(fresh.by_slug["a"])
    env.probes["a"].answers()
    await poll(fresh, "a")
    newest = (await env.store.events(monitor="a"))[0]
    assert (newest["previous"], newest["current"]) == ("warn", "up")
    assert sent == []  # no Warning alert went out, so no recovery alert either


async def test_kept_states_of_removed_monitors_are_pruned(storage):
    env = Env(storage, [mon("a")], failures_to_down=1, recheck_window=0)
    await env.poll("a")
    await env.store.record_event("gone", Transition(State.PENDING, State.UP, env.clock.now, "x"))
    assert set(await env.store.monitor_states()) == {"a", "gone"}
    fresh, _ = restart(env)
    await fresh.restore(fresh.by_slug["a"])
    assert set(await env.store.monitor_states()) == {"a"}


# ---- the state machine alone ----------------------------------------------------------------

def test_reaching_the_prior_state_is_no_transition():
    st = MonitorState(2, 1, prior=(State.DOWN, 5.0))
    assert st.observe(FAIL, 100.0) is None
    assert st.observe(FAIL, 160.0) is None
    assert st.state is State.DOWN and st.since == 5.0 and st.prior is None
    tr = st.observe(OK, 220.0)
    assert (tr.previous, tr.current) == (State.DOWN, State.UP)


def test_another_state_is_a_transition_from_the_prior_one():
    st = MonitorState(1, 1, prior=(State.UP, 5.0))
    tr = st.observe(FAIL, 100.0)
    assert (tr.previous, tr.current, tr.at) == (State.UP, State.DOWN, 100.0)
    assert tr.alertable and st.since == 100.0


def test_a_missed_reply_of_a_prior_outage_starts_no_degraded_episode():
    st = MonitorState(2, 1, recheck_window=60, prior=(State.DOWN, 5.0))
    miss = CheckResult.fail("no reply", unreachable=True)
    assert st.observe(miss, 100.0) is None and not st.degraded
    assert st.observe(miss, 110.0) is None
    assert st.state is State.DOWN and st.since == 5.0


def test_a_missed_reply_of_a_prior_warning_is_taken_silently():
    st = MonitorState(2, 1, recheck_window=60, prior=(State.WARN, 5.0))
    miss = CheckResult.fail("no reply", unreachable=True)
    assert st.observe(miss, 100.0) is None
    assert st.state is State.WARN and st.degraded and st.since == 5.0


def test_a_missed_reply_after_a_prior_up_is_the_degraded_notice():
    st = MonitorState(2, 1, recheck_window=60, prior=(State.UP, 5.0))
    tr = st.observe(CheckResult.fail("no reply", unreachable=True), 100.0)
    assert (tr.previous, tr.current, tr.degraded) == (State.UP, State.WARN, True)


# ---- the upgrade ----------------------------------------------------------------------------

def test_the_upgrade_seeds_the_kept_state_from_the_event_log(tmp_path):
    path = str(tmp_path / "m.db")
    db = sqlite3.connect(path)
    newer = {v: MIGRATIONS.pop(v) for v in [v for v in MIGRATIONS if v > 23]}
    try:
        migrate(db)
    finally:
        MIGRATIONS.update(newer)
    db.executemany("INSERT INTO events VALUES (?,?,?,?,?)", [
        ("a", 1.0, "pending", "up", ""), ("a", 2.0, "up", "down", ""),
        ("a", 3.0, "down", "down", "still down"), ("b", 4.0, "pending", "warn", "")])
    db.commit()
    db.close()
    store = Store(path)
    try:
        assert asyncio.run(store.monitor_states()) == {"a": ("down", 2.0), "b": ("warn", 4.0)}
    finally:
        store.close()
