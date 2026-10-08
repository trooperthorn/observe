"""The durable-alert tables and queries on the PostgreSQL dialect (slice o8-alert-durability).

The statements run through the dialect fake, which applies the same rewrite as the PostgreSQL
backend and rejects SQLite-only spellings; the cases marked live also run on a real server when
OBSERVE_TEST_PG_DSN is set and skip without one."""

from __future__ import annotations


import pytest

from observe.storage import StorageBusy
from observe.storage.pg_dialect import translate_sql
from observe.storage.schema import ALERT_TABLES, MIGRATIONS

from .fakes.pg_fake import SQLITE_ONLY, PgFakeStorage, _outside_quotes
from .test_storage import _store_on, live_pg


@pytest.fixture(params=["fake", "live"])
def storage(request):
    if request.param == "fake":
        s = PgFakeStorage()
        yield s
        s.close()
        return
    with live_pg() as s:
        yield s


def test_step_23_creates_the_alert_tables_in_postgresql_text():
    assert MIGRATIONS[23] is ALERT_TABLES
    for stmt in ALERT_TABLES:
        out = translate_sql(stmt, has_args=False)
        assert not SQLITE_ONLY.search(_outside_quotes(out)), out
    assert "BIGSERIAL" in translate_sql(ALERT_TABLES[0], has_args=False) or \
        "GENERATED" in translate_sql(ALERT_TABLES[0], has_args=False)


async def test_the_outbox_keeps_the_order_of_a_target(storage):
    st = _store_on(storage)
    await st.outbox_add([("t", "down"), ("u", "down")], 100.0)
    await st.outbox_add([("t", "up")], 110.0)
    due = await st.outbox_due(120.0)
    assert [(r[1], r[2]) for r in due] == [("t", "down"), ("u", "down"), ("t", "up")]
    await st.outbox_retry(due[0][0], 1, 400.0, "refused")  # the older alert of t backs off
    due = await st.outbox_due(120.0)
    assert [(r[1], r[2]) for r in due] == [("u", "down")]  # t's newer alert waits behind it
    await st.outbox_done([due[0][0]])
    assert await st.outbox_depth() == 2


async def test_the_open_alert_mark_is_set_replaced_cleared_and_pruned(storage):
    st = _store_on(storage)
    await st.set_alert_open("a", "warn", 1.0)
    await st.set_alert_open("a", "down", 2.0)
    await st.set_alert_open("b", "down", 3.0)
    assert await st.open_alerts() == {"a": ("down", 2.0), "b": ("down", 3.0)}
    assert await st.prune_alert_open({"a"}) == 1
    await st.set_alert_open("a", None, 4.0)
    assert await st.open_alerts() == {}


async def test_critical_writes_pass_a_full_queue_and_ingest_is_refused(storage):
    storage._gate._pending = storage._gate.limit
    try:
        with pytest.raises(StorageBusy):
            await storage.write(lambda db: None)
        with pytest.raises(StorageBusy):
            storage.write_sync(lambda db: None)
        await storage.write(lambda db: None, critical=True)
    finally:
        storage._gate._pending = 0
