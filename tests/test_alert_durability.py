"""Durable alert delivery and the bounded writer queue (docs/DATA-API-DESIGN.md section 9).

An alert target that is down for ten minutes receives the alert once when it is back, a restart
neither loses a queued alert nor repeats the alert of a monitor that is already Down, and a full
writer queue answers ingest with 503 and Retry-After instead of growing memory. The clocks are
fakes and the delivery is a recording fake, so nothing here waits or touches a network."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

import pytest

from observe.alerts import ALERT_MAX_AGE_S, RETRY_CAP_S, Alerter, retry_delay
from observe.checks.base import CheckResult
from observe.scheduler import Scheduler
from observe.state import State, Transition
from observe.storage import StorageBusy, open_storage
from observe.storage.base import WRITE_QUEUE_LIMIT, WriteGate
from observe.storage.schema import MIGRATIONS, SCHEMA_VERSION, migrate
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
    # Store calls run on a worker thread, so yielding a fixed number of times does not wait for
    # them. The test waits for the real event instead: the queued row being marked done.
    delivered = asyncio.Event()
    mark_done = second.outbox_done

    async def done_and_signal(ids):
        await mark_done(ids)
        delivered.set()

    second.outbox_done = done_and_signal
    task = asyncio.create_task(again.run())  # the loop the scheduler starts
    await asyncio.wait_for(delivered.wait(), timeout=30)
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


async def test_a_recovery_never_overtakes_the_problem_it_ends(tmp_path):
    """Audit finding: Down failed and backed off to 300 s while the newer Up had a short
    back-off, so Up arrived first and the retained MQTT state read 'down' for a healthy monitor."""
    cfg, clock, target = config(), Clock(), Target()
    store = Store(str(tmp_path / "o.db"))
    alerter = Alerter(cfg, store, clock)
    wire(alerter, target)
    mon = cfg.monitors[0]
    await alerter.notify(mon, Transition(State.UP, State.DOWN, clock.now, "no reply"))
    while clock.now < T0 + 320:  # the target stays down; Down backs off to 300 s
        clock.now += 5
        await alerter.flush()
    await alerter.notify(mon, Transition(State.DOWN, State.UP, clock.now, "ok"))
    assert await store.outbox_depth() == 2 and target.received == []
    target.up = True
    clock.now += 10  # Up's own short delay has passed, Down's has not
    await alerter.flush()
    assert target.received == []  # Up waits behind the older Down
    for _ in range(100):
        clock.now += 5
        await alerter.flush()
    assert [b["state"] for b in target.received] == ["down", "up"]
    assert await store.outbox_depth() == 0
    store.close()


async def test_alerts_of_other_targets_are_not_held_back_by_a_failing_one(tmp_path):
    cfg = make_config([{"name": "nas", "type": "ping", "host": "127.0.0.1",
                        "failures_to_down": 1, "recheck_window": 0}],
                      alerts=[HOOK, {**HOOK, "name": "other"}])
    clock, got = Clock(), []
    store = Store(str(tmp_path / "o.db"))
    alerter = Alerter(cfg, store, clock)

    async def send(target, body):
        if target.name == "hook":
            raise ConnectionError("refused")
        got.append((target.name, body["state"]))
    alerter._send = send  # type: ignore[method-assign]
    await alerter.notify(cfg.monitors[0], Transition(State.UP, State.DOWN, T0, "x"))
    assert got == [("other", "down")] and await store.outbox_depth() == 1
    store.close()


async def test_an_alert_is_delivered_directly_when_the_outbox_cannot_take_it(tmp_path):
    cfg, clock, target = config(), Clock(), Target()
    target.up = True
    store = Store(str(tmp_path / "o.db"))
    alerter = Alerter(cfg, store, clock)
    wire(alerter, target)

    async def refuse(rows, now):
        raise StorageBusy("the write queue is full")
    store.outbox_add = refuse  # type: ignore[method-assign]
    await alerter.notify(cfg.monitors[0], Transition(State.UP, State.DOWN, T0, "no reply"))
    assert [b["state"] for b in target.received] == ["down"]
    store.close()


async def test_a_full_write_queue_does_not_cost_a_poll_result_or_an_alert(tmp_path):
    """Audit finding: the cap applied to the server's own writes, so a flood of pushes made
    poll_once raise before the Down transition and dropped the alert and its open mark."""
    cfg, clock, target = config(), Clock(), Target()
    target.up = True
    store = Store(str(tmp_path / "o.db"))
    gate = store.storage._gate
    gate._pending = gate.limit  # the queue is full of pushes
    sched = Scheduler(cfg, store, Alerter(cfg, None, clock), clock=clock)
    wire(sched.alerter, target)
    mon = cfg.monitors[0]
    sched.checks[mon.slug] = Failing()
    with pytest.raises(StorageBusy):  # ingest-style work is refused
        await store.storage.write(lambda db: None)
    await sched.poll_once(mon)
    await asyncio.gather(*sched._pending_alerts)
    assert (await store.fetch("SELECT COUNT(*) FROM samples"))[0][0] > 0  # stored
    assert [b["state"] for b in target.received] == ["down"]
    assert await store.open_alerts() == {mon.slug: ("down", clock.now)}
    store.close()


async def test_a_denial_audit_row_is_written_while_the_queue_is_full(tmp_path):
    store = Store(str(tmp_path / "o.db"))
    store.storage._gate._pending = store.storage._gate.limit
    await store.write_audit("ingest_denied", status=403)  # not StorageBusy
    assert (await store.fetch("SELECT COUNT(*) FROM audit"))[0][0] == 1
    store.close()


async def test_open_alert_marks_of_removed_monitors_are_pruned(tmp_path):
    cfg, clock = config(), Clock()
    store = Store(str(tmp_path / "o.db"))
    await store.set_alert_open("nas", "down", T0)
    await store.set_alert_open("gone", "down", T0)
    sched = Scheduler(cfg, store, Alerter(cfg, None, clock), clock=clock)
    await sched.restore(cfg.monitors[0])
    assert set(await store.open_alerts()) == {"nas"}
    assert sched.states["nas"].state is State.DOWN
    store.close()


def test_a_version_22_database_with_rows_migrates_to_23(tmp_path):
    path = str(tmp_path / "m.db")
    db = sqlite3.connect(path)
    newest = MIGRATIONS.pop(23)
    try:
        migrate(db)
    finally:
        MIGRATIONS[23] = newest
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 22
    db.execute("INSERT INTO events VALUES ('a', 1.0, 'up', 'down', 'fine')")
    db.commit()
    db.close()
    store = Store(path)
    assert [e["message"] for e in asyncio.run(store.events())] == ["fine"]
    asyncio.run(store.set_alert_open("a", "down", T0))
    assert asyncio.run(store.outbox_depth()) == 0
    store.close()
    db = sqlite3.connect(path)
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION == 23
    db.close()


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
        # The executor's own queue holds exactly the admitted units: one running, seven waiting.
        assert storage._writer._work_queue.qsize() <= 8
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


def test_a_refused_push_from_a_warm_key_is_a_503_that_adds_no_write(tmp_path):
    """Audit finding: with the key already verified, the refusal comes from ingest_batch itself.
    It used to be audited as a 500 through a critical write that waited behind the full queue."""
    env = IngestEnv(tmp_path)
    try:
        key = env.key("nas01")
        assert env.push(simple(), key).status_code == 200  # verifies the key, warms last_used
        before = len(env.rows("SELECT id FROM audit"))
        gate = env.store.storage._gate
        gate.limit = 2
        release = threading.Event()
        held = [gate.submit(env.store.storage._writer, release.wait, 30) for _ in range(2)]
        seen: list[int] = []
        real = gate.submit

        def spy(*a, **k):
            seen.append(gate.pending)
            return real(*a, **k)

        gate.submit = spy  # type: ignore[method-assign]
        r = env.push(simple(), key)
        gate.submit = real  # type: ignore[method-assign]
        assert r.status_code == 503 and r.headers["retry-after"] == "5"
        assert seen == [2] and gate.pending == 2  # the one refused submission, no audit unit
        release.set()
        for f in held:
            f.result(timeout=10)
        assert len(env.rows("SELECT id FROM audit")) == before  # no ingest_failed row
        assert env.rows("SELECT id FROM audit WHERE kind='ingest_failed'") == []
    finally:
        env.close()


async def test_a_collectors_writes_are_not_refused_while_the_queue_is_full(tmp_path):
    cfg = config()
    store = Store(str(tmp_path / "o.db"))
    store.storage._gate._pending = store.storage._gate.limit
    wrote: list[int] = []

    class Collector:
        name, interval, timeout = "c", 60, 5

        async def run(self, st):
            await st.storage.write(lambda db: wrote.append(1))
            raise asyncio.CancelledError

    sched = Scheduler(cfg, store, Alerter(cfg, None, Clock()))
    with pytest.raises(asyncio.CancelledError):
        await sched._collector_loop("p", Collector())
    assert wrote == [1]
    store.close()


async def test_the_open_mark_is_written_after_the_alert_is_queued(tmp_path):
    cfg, clock = config(), Clock()
    store = Store(str(tmp_path / "o.db"))
    sched = Scheduler(cfg, store, Alerter(cfg, None, clock), clock=clock)
    order: list[str] = []
    add, mark = store.outbox_add, store.set_alert_open

    async def add_spy(*a, **k):
        order.append("outbox")
        return await add(*a, **k)

    async def mark_spy(*a, **k):
        order.append("mark")
        return await mark(*a, **k)

    store.outbox_add, store.set_alert_open = add_spy, mark_spy  # type: ignore[method-assign]
    sched.alerter._send = Target().send  # type: ignore[method-assign]
    mon = cfg.monitors[0]
    sched.checks[mon.slug] = Failing()
    await sched.poll_once(mon)
    await asyncio.gather(*sched._pending_alerts)
    assert order[:2] == ["outbox", "mark"]
    store.close()


async def test_one_targets_backlog_does_not_hide_another_targets_due_alerts(tmp_path):
    store = Store(str(tmp_path / "o.db"))
    await store.outbox_add([("slow", "{}")] * 150 + [("fast", "{}")], T0)
    due = await store.outbox_due(T0, limit=100)
    assert sorted({r[1] for r in due}) == ["fast", "slow"]
    assert sum(r[1] == "slow" for r in due) == 100
    assert [r[0] for r in due] == sorted(r[0] for r in due)  # oldest first
    store.close()


@pytest.mark.parametrize("limit", [0, -1])
def test_the_queue_limit_must_be_positive(limit):
    with pytest.raises(ValueError):
        WriteGate(limit)
