"""The host list read (Store.host_list_inputs) on SQLite, the PostgreSQL dialect fake and, when
OBSERVE_TEST_PG_DSN is set, a live server: the same answers as the per host reads, in four
statements, with PostgreSQL text."""

from __future__ import annotations

import json
import time

import pytest

from .fakes.pg_fake import PgFakeStorage, _outside_quotes
from .test_storage import _store_on, put, storage  # noqa: F401  (storage is a fixture)


@pytest.fixture
def pg():
    s = PgFakeStorage()
    yield s
    s.close()


async def seed(storage_, now: float) -> None:
    for host in ("a", "b", "c"):
        await storage_.execute("INSERT INTO hosts (host, first_seen, last_seen, clean_shutdown) "
                               "VALUES (?, 1, ?, 1)", (host, now))
    await put(storage_, [(now - 5, "a", "cpu", "temp", "{}", 1.0, "C"),
                         (now - 5, "a", "cpu", "temp", '{"core":"1"}', 2.0, "C"),
                         (now - 5000, "a", "disk", "used", "{}", 7.5, "%"),
                         (now - 5000, "b", "disk", "used", "{}", 8.5, "%"),
                         (now - 9, "c", "cpu", "temp", "{}", 4.0, "C")])
    for host, source, avail, reason in (("a", "cpu", 1, ""), ("a", "raid", 0, "no tool"),
                                        ("b", "cpu", 1, "")):
        await storage_.execute("INSERT INTO host_sources (host, source, available, reason, "
                               "updated) VALUES (?,?,?,?,?)", (host, source, avail, reason, now))
    for i in range(60):  # 60 events: only the 50 newest of a host count
        sev = "warning" if i == 0 else "info"
        await storage_.execute(
            "INSERT INTO host_events (host, ts, kind, severity, source, title, detail, dedup_key,"
            " boot_id) VALUES ('a', ?, 'k', ?, 's', 't', '{}', ?, NULL)",
            (now - 1000 + i, sev, f"d{i}"))
    await storage_.execute(
        "INSERT INTO host_events (host, ts, kind, severity, source, title, detail, dedup_key, "
        "boot_id) VALUES ('b', ?, 'k', 'critical', 's', 't', '{\"x\": 1}', 'b1', NULL)", (now - 5,))


async def check(st, now: float) -> None:
    await seed(st.storage, now)
    wanted = {"a": (now - 900, (("disk", "used"),)), "b": (now - 900, ()), "c": (now - 900, ())}
    got = await st.host_list_inputs(wanted, now - 86400)
    for host, (since, series_) in wanted.items():
        old = await st.latest_host(host, window=900, now=now, series=series_)
        key = lambda s: (s["source"], s["metric"], json.dumps(s["labels"]))  # noqa: E731
        assert sorted(got[host]["samples"], key=key) == sorted(old["samples"], key=key)
        assert got[host]["sources"] == await st.host_sources(host)
        alerts = [e for e in await st.host_events(host, limit=50)
                  if e["severity"] in ("warning", "critical")]
        assert got[host]["events"] == alerts
    assert any(s["metric"] == "used" and s["value"] == 7.5 for s in got["a"]["samples"])
    assert [s["value"] for s in got["b"]["samples"]] == [8.5]  # the window is empty: newest row
    assert got["a"]["events"] == []  # the warning is the 60th newest, outside the 50
    assert got["b"]["events"][0]["detail"] == {"x": 1}


async def test_the_host_list_read_matches_the_per_host_reads_on_every_backend(storage):  # noqa: F811
    await check(_store_on(storage), time.time())


async def test_the_host_list_read_sends_four_postgresql_statements(pg):
    st = _store_on(pg)
    now = time.time()
    await check(st, now)
    pg.statements.clear()
    await st.host_list_inputs({"a": (now - 900, ())}, now - 86400)
    assert len(pg.statements) == 4
    assert all("?" not in _outside_quotes(s) for s in pg.statements)
    assert any("ROW_NUMBER() OVER (PARTITION BY host" in s for s in pg.statements)
