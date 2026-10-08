"""Durable alert delivery and the bounded writer queue (docs/DATA-API-DESIGN.md section 9).

An alert target that is down for ten minutes receives the alert once when it is back, a restart
neither loses a queued alert nor repeats the alert of a monitor that is already Down, and a full
writer queue answers ingest with 503 and Retry-After instead of growing memory. The clocks are
fakes and the delivery is a recording fake, so nothing here waits or touches a network."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from observe.alerts import ALERT_MAX_AGE_S, RETRY_CAP_S, Alerter, retry_delay
from observe.checks.base import CheckResult
from observe.scheduler import Scheduler
from observe.state import State, Transition
from observe.storage import StorageBusy, open_storage
from observe.storage.base import WRITE_QUEUE_LIMIT, WriteGate
from observe.store import Store

from .conftest import make_config
from .test_otlp_ingest import Env as IngestEnv
from .test_otlp_ingest import simple

T0 = 1_700_000_000.0
HOOK = {"name": "hook", "type": "webhook", "url": "http://127.0.0.1:1/x"}


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class Target:
    """A recording stand-in for the alert target: down until `up` is set."""

    def __init__(self) -> None:
        self.up = False
        self.attempts = 0
        self.received: list[dict] = []

    async def send(self, target, body) -> None:
        self.attempts += 1
        if not self.up:
            raise ConnectionError("connection refused")
        self.received.append(body)


def config():
    return make_config([{"name": "nas", "type": "ping", "host": "127.0.0.1",
                         "failures_to_down": 1, "recheck_window": 0}], alerts=[HOOK])


def wire(alerter: Alerter, target: Target) -> None:
    alerter._send = target.send  # type: ignore[method-assign]


async def test_a_target_down_for_ten_minutes_receives_the_alert_once(tmp_path):
    cfg, clock, target = config(), Clock(), Target()
    store = Store(str(tmp_path / "o.db"))
    alerter = Alerter(cfg, store, clock)
    wire(alerter, target)
    await alerter.notify(cfg.monitors[0], Transition(State.UP, State.DOWN, T0, "no reply"))
    assert target.attempts == 1 and await store.outbox_depth() == 1
    assert alerter.status["hook"]["last_error"].startswith("ConnectionError")
    # Ten minutes in which the outbox is looked at every five seconds, as run() does.
    for _ in range(120):
        clock.now += 5
        await alerter.flush()
    assert await store.outbox_depth() == 1 and target.received == []
    # Backed off: far fewer attempts than looks (10, 20, 40, 80, 160 and then every 300 s).
    assert 5 <= target.attempts <= 9, target.attempts
    target.up = True
    clock.now += RETRY_CAP_S
    assert await alerter.flush() == 1
    for _ in range(5):
        clock.now += RETRY_CAP_S
        await alerter.flush()
    assert len(target.received) == 1 and target.received[0]["state"] == "down"
    assert await store.outbox_depth() == 0
    assert alerter.status["hook"]["sent"] == 1 and alerter.status["hook"]["last_error"] is None
    store.close()


async def test_a_queued_alert_survives_a_restart_and_is_delivered_once(tmp_path):
    cfg, clock, target = config(), Clock(), Target()
    path = str(tmp_path / "o.db")
    first = Store(path)
    alerter = Alerter(cfg, first, clock)
    wire(alerter, target)
    await alerter.notify(cfg.monitors[0], Transition(State.UP, State.DOWN, T0, "no reply"))
    assert await first.outbox_depth() == 1
    first.close()

    second = Store(path)
    again = Alerter(cfg, second, clock)
    wire(again, target)
    target.up = True
    clock.now += 60
    task = asyncio.create_task(again.run())  # the loop the scheduler starts
    for _ in range(500):
        if target.received and await second.outbox_depth() == 0:
            break
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert [b["monitor"] for b in target.received] == ["nas"]
    assert await second.outbox_depth() == 0
    second.close()


async def test_an_alert_for_a_removed_target_or_a_day_old_is_dropped(tmp_path):
    cfg, clock, target = config(), Clock(), Target()
    store = Store(str(tmp_path / "o.db"))
    alerter = Alerter(cfg, store, clock)
    wire(alerter, target)
    await alerter.notify(cfg.monitors[0], Transition(State.UP, State.DOWN, T0, "old"))
    await store.outbox_add([("gone", json.dumps({"state": "down"}))], clock.now)
    clock.now += ALERT_MAX_AGE_S + 1
    target.up = True
    assert await alerter.flush() == 0
    assert target.received == [] and await store.outbox_depth() == 0
    store.close()


def test_the_retry_delay_doubles_to_its_cap():
    assert [retry_delay(n) for n in (1, 2, 3, 4, 5, 6, 7, 50)] == [
        10, 20, 40, 80, 160, 300, 300, 300]


class Failing:
    rechecking = False

    async def run(self):
        return CheckResult.fail("refused")

    def thresholds(self):
        return None


async def test_a_monitor_that_is_down_is_not_alerted_again_after_a_restart(tmp_path):
    cfg, clock, target = config(), Clock(), Target()
    target.up = True
    path = str(tmp_path / "o.db")
    mon = cfg.monitors[0]

    first = Store(path)
    sched = Scheduler(cfg, first, Alerter(cfg, None, clock), clock=clock)
    wire(sched.alerter, target)
    sched.checks[mon.slug] = Failing()
    await sched.poll_once(mon)
    await asyncio.gather(*sched._pending_alerts)
    assert [b["state"] for b in target.received] == ["down"]
    first.close()

    second = Store(path)
    fresh = Scheduler(cfg, second, Alerter(cfg, None, clock), clock=clock)
    wire(fresh.alerter, target)
    fresh.checks[mon.slug] = Failing()
    await fresh.restore(mon)
    assert fresh.states[mon.slug].state is State.DOWN
    for _ in range(3):
        clock.now += 60
        await fresh.poll_once(mon)
    await asyncio.gather(*fresh._pending_alerts)
    assert [b["state"] for b in target.received] == ["down"]  # no second Down alert
    second.close()


# ---- the bounded writer queue --------------------------------------------------------------

def test_a_full_write_queue_refuses_more_work_and_stays_bounded(tmp_path):
    storage = open_storage(str(tmp_path / "w.db"))
    try:
        assert storage._gate.limit == WRITE_QUEUE_LIMIT == 256
        storage._gate.limit = 8
        release = threading.Event()
        ran: list[int] = []
        # The writer is held by one unit; the others wait behind it.
        held = [storage._gate.submit(storage._writer, release.wait, 30) for _ in range(8)]
        refused = 0
        for i in range(10_000):
            try:
                storage.write_sync(lambda db, i=i: ran.append(i))
            except StorageBusy:
                refused += 1
        assert refused == 10_000 and storage._gate.pending == 8  # nothing piled up
        release.set()
        for f in held:
            f.result(timeout=10)
        assert storage._gate.pending == 0
        storage.write_sync(lambda db: ran.append(-1))  # accepted again once it drained
        assert ran == [-1]
    finally:
        storage.close()


def test_ingest_answers_503_with_retry_after_while_the_writer_queue_is_full(tmp_path):
    env = IngestEnv(tmp_path)
    try:
        key = env.key("nas01")
        gate = env.store.storage._gate
        gate.limit = 2
        release = threading.Event()
        held = [gate.submit(env.store.storage._writer, release.wait, 30) for _ in range(2)]
        r = env.push(simple(), key)
        assert r.status_code == 503 and r.headers["retry-after"] == "5"
        assert gate.pending == 2
        assert env.stored() == []
        release.set()
        for f in held:
            f.result(timeout=10)
        assert env.push(simple(), key).status_code == 200  # the producer's retry is accepted
        assert env.stored()
    finally:
        env.close()


@pytest.mark.parametrize("limit", [0, -1])
def test_the_queue_limit_must_be_positive(limit):
    with pytest.raises(ValueError):
        WriteGate(limit)
