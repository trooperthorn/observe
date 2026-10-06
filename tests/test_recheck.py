"""The fast re-check window (docs/DATA-API-DESIGN.md section 10.3) and the state read back from
the summary views, on every storage backend.

The clock is a fake that the test moves, the probes are scripted fakes, and the reachability
probe of a pushed host is a recording fake, so nothing here waits or touches a network. The
`storage` fixture runs each case on SQLite, on the PostgreSQL dialect fake (no server) and,
when OBSERVE_TEST_PG_DSN is set, on PostgreSQL."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from observe.alerts import Alerter
from observe.checks.base import CheckResult, Result
from observe.checks.host import PushedHostCheck
from observe.ingest.schema import Batch
from observe.scheduler import Scheduler
from observe.storage import open_storage
from observe.state import MonitorState, State, Transition

from .conftest import make_config
from .dbq import settle
from .fakes.pg_fake import PgFakeStorage
from .test_storage import _store_on, live_pg

T0 = 1_700_000_000.0


@pytest.fixture(params=["sqlite", "postgres-fake", "postgres"])
def storage(request, tmp_path):
    if request.param == "postgres":
        with live_pg() as s:  # skipped without OBSERVE_TEST_PG_DSN
            yield s
        return
    s = PgFakeStorage() if request.param == "postgres-fake" else open_storage(str(tmp_path / "s.db"))
    yield s
    s.close()


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class Probe:
    """A scripted check: the test sets the next result."""

    def __init__(self) -> None:
        self.result = CheckResult.ok("up")
        self.rechecking = False
        self.seen_rechecking: list[bool] = []

    def thresholds(self):
        return None

    def silent(self) -> None:
        self.result = CheckResult.fail("no reply", unreachable=True)

    def answers(self) -> None:
        self.result = CheckResult.ok("up")

    async def run(self):
        self.seen_rechecking.append(self.rechecking)
        return self.result


def mon(name, **kw):
    return {"name": name, "type": "tcp", "host": "192.0.2.1", "port": 1, **kw}


class Env:
    def __init__(self, storage, monitors, **defaults):
        self.cfg = make_config(monitors, defaults={"failures_to_down": 3, "timeout": 1,
                                                   "interval": 60, **defaults})
        self.clock = Clock()
        self.store = _store_on(storage)
        self.sched = Scheduler(self.cfg, self.store, Alerter(self.cfg), clock=self.clock)
        self.sent: list[tuple[str, str, bool]] = []
        self.probes: dict[str, Probe] = {}
        for slug in self.sched.checks:
            self.probes[slug] = self.sched.checks[slug] = Probe()

        async def record(monitor, tr):
            self.sent.append((monitor.slug, tr.current.value, tr.degraded))

        self.sched.alerter.notify = record

    async def poll(self, slug, advance=0.0):
        self.clock.now += advance
        res = await self.sched.poll_once(self.sched.by_slug[slug])
        await asyncio.sleep(0)
        await asyncio.gather(*self.sched._pending_alerts)
        return res

    def st(self, slug) -> MonitorState:
        return self.sched.states[slug]

    def delay(self, slug) -> float:
        return self.sched.delay(self.sched.by_slug[slug])


# ---- failure into Degraded -----------------------------------------------------------------

async def test_a_missed_reply_is_degraded_warning_at_once(storage):
    env = Env(storage, [mon("a")])
    await env.poll("a")
    assert env.st("a").state is State.UP
    env.probes["a"].silent()
    await env.poll("a", 60)
    st = env.st("a")
    assert st.state is State.WARN and st.degraded
    assert st.last.message == "no reply"
    events = await env.store.events(monitor="a")
    assert events[0]["current"] == "warn"
    assert events[0]["message"].startswith("Degraded: not responding")
    # The monitor is now checked every 10 s instead of every 60 s.
    assert env.delay("a") == 10.0
    await env.poll("a", 10)
    assert env.probes["a"].seen_rechecking[-1] is True


async def test_a_wrong_answer_is_not_a_missed_reply(storage):
    env = Env(storage, [mon("a")], failures_to_down=2)
    await env.poll("a")
    env.probes["a"].result = CheckResult.fail("HTTP 500")  # it answered: not unreachable
    await env.poll("a", 60)
    assert not env.st("a").degraded and env.st("a").state is State.UP
    await env.poll("a", 60)
    assert env.st("a").state is State.DOWN and env.delay("a") == 60.0


async def test_degraded_is_handed_to_the_alerter_and_down_is_an_ordinary_alert(storage):
    env = Env(storage, [mon("a")])
    await env.poll("a")
    env.probes["a"].silent()
    await env.poll("a", 60)
    assert env.sent == [("a", "warn", True)]  # the alerter filters it by notify_degraded
    for _ in range(18):
        await env.poll("a", 10)
    assert env.st("a").state is State.DOWN
    assert env.sent[-1] == ("a", "down", False)


async def test_a_target_receives_degraded_notices_only_when_it_opts_in(monkeypatch):
    cfg = make_config(
        [mon("a")],
        alerts=[{"name": "quiet", "type": "webhook", "url": "http://127.0.0.1:1/x"},
                {"name": "loud", "type": "webhook", "url": "http://127.0.0.1:1/y",
                 "notify_degraded": True}])
    alerter = Alerter(cfg)
    delivered: list[tuple[str, str]] = []

    async def deliver(target, monitor, tr):
        delivered.append((target.name, tr.current.value))

    monkeypatch.setattr(alerter, "_deliver", deliver)
    m = cfg.monitors[0]
    await alerter.notify(m, Transition(State.UP, State.WARN, T0, "Degraded", degraded=True))
    await alerter.notify(m, Transition(State.WARN, State.UP, T0, "back", degraded=True))
    await alerter.notify(m, Transition(State.WARN, State.DOWN, T0, "down"))
    assert delivered == [("loud", "warn"), ("loud", "up"), ("quiet", "down"), ("loud", "down")]


# ---- recovery after good replies -----------------------------------------------------------

async def test_two_good_replies_return_it_to_up(storage):
    env = Env(storage, [mon("a")])
    await env.poll("a")
    env.probes["a"].silent()
    await env.poll("a", 60)
    env.probes["a"].answers()
    await env.poll("a", 10)
    assert env.st("a").state is State.WARN and env.st("a").degraded  # one lucky packet
    await env.poll("a", 10)
    st = env.st("a")
    assert st.state is State.UP and not st.degraded
    assert env.delay("a") == 60.0
    events = await env.store.events(monitor="a")
    assert events[0]["current"] == "up" and "20s" in events[0]["message"]
    # The recovery is a degraded notice, never an ordinary Up alert.
    assert env.sent[-1] == ("a", "up", True)


async def test_a_miss_between_replies_resets_the_count(storage):
    env = Env(storage, [mon("a")])
    await env.poll("a")
    probe = env.probes["a"]
    probe.silent()
    await env.poll("a", 60)
    for outcome in (probe.answers, probe.silent, probe.answers):
        outcome()
        await env.poll("a", 10)
    assert env.st("a").degraded and env.st("a").state is State.WARN
    await env.poll("a", 10)
    assert env.st("a").state is State.UP


# ---- Down when the window ends -------------------------------------------------------------

async def test_down_when_the_window_ends(storage):
    env = Env(storage, [mon("a")])
    await env.poll("a")
    env.probes["a"].silent()
    await env.poll("a", 60)
    for _ in range(17):
        await env.poll("a", 10)  # 170 s into the window
    assert env.st("a").state is State.WARN and env.st("a").degraded
    await env.poll("a", 10)  # 180 s
    st = env.st("a")
    assert st.state is State.DOWN and not st.degraded and st.alert_open
    assert [s for _, s, _ in env.sent] == ["warn", "down"]
    assert env.delay("a") == 60.0  # a host that stays down is not hammered
    events = await env.store.events(monitor="a")
    assert events[0]["current"] == "down" and "180s" in events[0]["message"]


async def test_recovery_from_down_alerts_up(storage):
    env = Env(storage, [mon("a")])
    await env.poll("a")
    env.probes["a"].silent()
    await env.poll("a", 60)
    await env.poll("a", 180)
    assert env.st("a").state is State.DOWN
    env.probes["a"].answers()
    await env.poll("a", 60)
    assert env.st("a").state is State.UP
    assert env.sent[-1] == ("a", "up", False)


async def test_a_window_of_zero_turns_the_recheck_off(storage):
    env = Env(storage, [mon("a")], recheck_window=0, failures_to_down=2)
    await env.poll("a")
    env.probes["a"].silent()
    await env.poll("a", 60)
    assert env.st("a").state is State.UP and not env.st("a").degraded
    await env.poll("a", 60)
    assert env.st("a").state is State.DOWN


# ---- a pushed host is re-checked by ping or TCP --------------------------------------------

def batch(host, ts):
    return Batch.model_validate({
        "schema_version": 1, "agent_version": "t", "host": host, "platform": "linux",
        "sent_at": ts, "sources": [{"source": "hwmon", "available": True}],
        "samples": [{"source": "hwmon", "metric": "cpu_temp_c", "value": 40.0, "unit": "C",
                     "labels": {}, "ts": ts}]})


class Pushed:
    def __init__(self, storage, **extra):
        self.env = Env(storage, [{"name": "nas", "type": "pushed_host", "host": "nas",
                                  "stale_after": 120, **extra}])
        self.calls: list[tuple[str, int | None]] = []
        self.alive = False

        async def reach(address, port, timeout):
            self.calls.append((address, port))
            return self.alive

        self.check = PushedHostCheck(self.env.sched.by_slug["nas"], self.env.cfg, self.env.store,
                                     clock=self.env.clock, reach=reach)
        self.env.sched.checks["nas"] = self.check

    async def push(self):
        await self.env.store.ingest_batch(batch("nas", self.env.clock.now), {},
                                          now=self.env.clock.now)

    async def poll(self, advance=0.0):
        return await self.env.poll("nas", advance)


async def test_a_missed_batch_starts_the_recheck_without_pinging_first(storage):
    p = Pushed(storage)
    await p.push()
    assert (await p.poll()).result is Result.OK
    res = await p.poll(121)
    assert res.result is Result.FAIL and res.unreachable
    assert p.env.st("nas").degraded and p.calls == []


async def test_the_recheck_pings_the_host_and_two_answers_recover_it(storage):
    p = Pushed(storage)
    await p.push()
    await p.poll()
    await p.poll(121)
    p.alive = True
    await p.poll(10)
    assert p.env.st("nas").degraded
    res = await p.poll(10)
    assert res.result is Result.OK and "answers ping" in res.message
    assert p.env.st("nas").state is State.UP
    assert p.calls == [("nas", None), ("nas", None)]


async def test_the_recheck_uses_tcp_and_the_address_when_configured(storage):
    p = Pushed(storage, address="192.0.2.9", recheck_port=22)
    await p.push()
    await p.poll()
    await p.poll(121)
    await p.poll(10)
    assert p.calls == [("192.0.2.9", 22)]


async def test_a_host_that_answers_ping_but_has_a_dead_agent_still_goes_down(storage):
    p = Pushed(storage)
    p.alive = True
    await p.push()
    await p.poll()
    await p.poll(121)
    await p.poll(10)
    await p.poll(10)
    assert p.env.st("nas").state is State.UP  # the answers carried it through the re-check
    for _ in range(10):  # the same stale batch is now a plain failure, not another re-check
        await p.poll(30)
    st = p.env.st("nas")
    assert st.state is State.DOWN and not st.degraded
    assert "agent is still silent" in st.last.message


async def test_a_fresh_batch_clears_the_answered_but_silent_memory(storage):
    p = Pushed(storage)
    p.alive = True
    await p.push()
    await p.poll()
    await p.poll(121)
    await p.poll(10)
    await p.poll(10)
    p.env.clock.now += 10
    await p.push()
    assert (await p.poll()).result is Result.OK
    res = await p.poll(121)  # silent again later: a new episode starts with a re-check
    assert res.unreachable and p.env.st("nas").degraded


async def test_a_pushed_host_that_never_answers_goes_down_after_the_window(storage):
    p = Pushed(storage)
    await p.push()
    await p.poll()
    await p.poll(121)
    for _ in range(18):
        await p.poll(10)
    st = p.env.st("nas")
    assert st.state is State.DOWN and not st.degraded
    assert len(p.calls) == 18
    await p.poll(60)  # fast checks stopped: a down host is not pinged again
    assert len(p.calls) == 18


async def test_a_batch_in_the_window_counts_as_a_good_reply(storage):
    p = Pushed(storage)
    await p.push()
    await p.poll()
    await p.poll(121)
    p.env.clock.now += 10
    await p.push()
    await p.poll()
    assert p.env.st("nas").degraded
    p.env.clock.now += 10
    await p.push()
    await p.poll()
    assert p.env.st("nas").state is State.UP and p.calls == []


# ---- dependencies --------------------------------------------------------------------------

def tree(storage, **defaults):
    return Env(storage, [mon("sw"), mon("srv", depends_on=["sw"])], **defaults)


async def test_a_child_does_not_start_its_own_recheck_while_the_parent_is_down(storage):
    env = tree(storage, failures_to_down=2)
    await env.poll("sw")
    await env.poll("srv")
    env.probes["sw"].silent()
    await env.poll("sw", 60)
    await env.poll("sw", 180)
    assert env.st("sw").state is State.DOWN
    env.sent.clear()
    env.probes["srv"].silent()
    await env.poll("srv", 10)
    assert not env.st("srv").degraded and env.st("srv").state is State.UP
    await env.poll("srv", 60)
    srv = env.st("srv")
    assert srv.state is State.DOWN and not srv.degraded
    assert env.sent == []  # the alert is suppressed: the parent is down
    assert env.sched.rollup.effective("srv") == ("unreachable", "sw")
    events = await env.store.events(monitor="srv")
    assert "alert suppressed: sw is down" in events[0]["message"]


async def test_a_childs_alert_is_held_while_the_parent_is_rechecked(storage):
    env = tree(storage, failures_to_down=1)
    await env.poll("sw")
    await env.poll("srv")
    env.probes["sw"].silent()
    await env.poll("sw", 60)
    assert env.st("sw").degraded
    env.sent.clear()
    env.probes["srv"].result = CheckResult.fail("HTTP 500")
    await env.poll("srv", 5)
    assert env.st("srv").state is State.DOWN and env.sent == []
    events = await env.store.events(monitor="srv")
    assert "alert held: sw is being re-checked" in events[0]["message"]
    # The parent answers again: the child is polled once more and, still failing, is alerted.
    env.probes["sw"].answers()
    await env.poll("sw", 5)
    await env.poll("sw", 5)
    assert env.st("sw").state is State.UP
    assert ("srv", "down", False) in env.sent


# ---- global and per-host settings ----------------------------------------------------------

async def test_a_per_host_override_beats_the_global_setting(storage):
    env = Env(storage, [mon("fast", recheck_window=30, recheck_interval=5, recheck_good=1),
                        mon("plain")], recheck_window=120, recheck_interval=20, recheck_good=3)
    assert env.st("fast").recheck_window == 30 and env.st("fast").recheck_good == 1
    assert env.st("plain").recheck_window == 120 and env.st("plain").recheck_good == 3
    for slug in ("fast", "plain"):
        await env.poll(slug)
        env.probes[slug].silent()
        await env.poll(slug, 60)
    assert env.delay("fast") == 5.0 and env.delay("plain") == 20.0
    for slug in ("fast", "plain"):
        await env.poll(slug, 30)  # 30 s into the windows
    assert env.st("fast").state is State.DOWN  # its 30 s window is over
    assert env.st("plain").state is State.WARN and env.st("plain").degraded
    env.probes["plain"].answers()
    await env.poll("plain", 20)
    await env.poll("plain", 20)
    assert env.st("plain").degraded  # the global setting needs three good replies
    await env.poll("plain", 20)
    assert env.st("plain").state is State.UP


def test_the_re_check_settings_are_validated():
    cfg = make_config([mon("a")])
    assert (cfg.defaults.recheck_interval, cfg.defaults.recheck_window,
            cfg.defaults.recheck_good) == (10.0, 180.0, 2)
    with pytest.raises(ValidationError):
        make_config([mon("a", recheck_interval=4)])
    with pytest.raises(ValidationError):
        make_config([mon("a")], defaults={"recheck_good": 0})
    with pytest.raises(ValidationError):
        make_config([mon("a", recheck_window=-1)])


# ---- state read from the summaries ---------------------------------------------------------

async def test_availability_and_the_hourly_series_come_from_the_summary_views(storage):
    st = _store_on(storage)
    for i, res in enumerate([CheckResult(Result.OK, "", value=10.0),
                             CheckResult(Result.OK, "", value=20.0),
                             CheckResult(Result.WARN, "", value=30.0),
                             CheckResult.fail("down", value=99.0)]):
        await st.record("m", T0 - 100 - i, res)
    await settle(storage)
    assert await st.availability("m", 1, now=T0) == 75.0
    assert await st.availability("m", 100, now=T0) == 75.0  # the hourly level
    hourly = await st.hourly_series("m", 1, now=T0)
    assert hourly and all(v < 99 for _, v in hourly)  # the failing value is not in the mean


async def test_a_restart_restores_the_state_from_the_latest_result(storage):
    env = Env(storage, [mon("a"), mon("b"), mon("old")])
    await env.poll("a")
    env.probes["b"].result = CheckResult(Result.WARN, "slow")
    await env.poll("b")
    await env.poll("old")
    await settle(storage)
    env.clock.now += 100  # inside three intervals
    fresh = Scheduler(env.cfg, env.store, Alerter(env.cfg), clock=env.clock)
    for slug in ("a", "b"):
        await fresh.restore(fresh.by_slug[slug])
    assert fresh.states["a"].state is State.UP
    assert fresh.states["b"].state is State.PENDING  # a problem is evaluated again and alerts
    env.clock.now += 3600  # a stale result is not trusted
    await fresh.restore(fresh.by_slug["old"])
    assert fresh.states["old"].state is State.PENDING
    assert not fresh.states["b"].alert_open


async def test_a_problem_that_continues_across_a_restart_still_alerts(storage):
    env = Env(storage, [mon("a")], failures_to_down=1, recheck_window=0)
    env.probes["a"].result = CheckResult.fail("refused")
    await env.poll("a")
    env.sent.clear()
    fresh = Scheduler(env.cfg, env.store, Alerter(env.cfg), clock=env.clock)
    sent = []

    async def record(monitor, tr):
        sent.append(tr.current.value)

    fresh.alerter.notify = record
    fresh.checks["a"] = env.probes["a"]
    await fresh.restore(fresh.by_slug["a"])
    await fresh.poll_once(fresh.by_slug["a"])
    await settle(storage)
    assert sent == ["down"]


async def test_history_from_before_the_series_existed_is_backfilled(storage):
    """A database from the previous version has poll rows and no monitor series."""
    st = _store_on(storage)
    rows = [(T0 - 100 * i, "ok" if i != 3 else "fail", 10.0 + i, 5.0, "") for i in range(1, 9)]
    for r in rows:
        await st.execute("INSERT INTO results VALUES (?,?,?,?,?,?)", ("m",) + r)
    await settle(storage)
    assert await st.availability("m", 1, now=T0) is None
    assert await st.backfill_monitor_series(batch=3) == 8
    await settle(storage)
    assert await st.availability("m", 1, now=T0) == 87.5
    assert await st.hourly_series("m", 1, now=T0)
    # A second start copies nothing, and newer live polls are not disturbed.
    assert await st.backfill_monitor_series() == 0
    await st.record("m", T0, CheckResult.ok("up"))
    assert await st.backfill_monitor_series() == 0


async def test_a_finished_backfill_is_not_repeated_after_raw_samples_are_pruned(storage):
    st = _store_on(storage)
    for i in range(1, 6):
        await st.execute("INSERT INTO results VALUES (?,?,?,?,?,?)",
                         ("m", T0 - 100 * i, "ok", 10.0, 5.0, ""))
    await settle(storage)
    assert await st.backfill_monitor_series() == 5
    await settle(storage)
    # Raw samples pruned earlier than the poll rows: the rows are older than the new minimum.
    await st.execute("DELETE FROM samples")
    await settle(storage)
    assert await st.backfill_monitor_series() == 0
