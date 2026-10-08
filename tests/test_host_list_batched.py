"""GET /api/v2/hosts builds the list in a constant number of statements, returns exactly what the
one host at a time build returned, and keeps its ETag while the list shows the same picture.

Measured on the 50 host fixture below (SQLite, one statement per execute): the old build took
414 statements, the batched build 5, and 10 hosts take the same 5."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from observe import hostview
from observe.api.cursor import PageParams
from observe.api.hosts import HostViews, _times, list_hosts
from observe.api.registry import ApiContext
from observe.otelnames import collector_scope

from .api_env import START, ApiEnv, host_batch
from .test_one_transaction import Counting


def event(kind: str, severity: str, ts: float, key: str) -> dict[str, Any]:
    return {"kind": kind, "severity": severity, "source": "journal", "ts": ts, "title": kind,
            "dedup_key": key, "detail": {"k": key}}


def picture(i: int, ts: float | None = None) -> Any:
    """The batch of host i: a good, a warning or a stale picture, alerts inside and outside the
    alert window."""
    name = f"h{i:03d}"
    if ts is None:
        ts = START - 5 if i % 7 else START - 5000  # every seventh host has gone silent
    samples = [
        {"source": collector_scope("cpu"), "metric": "system.cpu.utilization",
         "value": 0.12 + (0.85 if i % 6 == 0 else 0.0), "unit": "1", "labels": {}, "ts": ts},
        {"source": collector_scope("cpu"), "metric": "system.cpu.load_average.1m",
         "value": 0.5, "unit": "{thread}", "labels": {}, "ts": ts - 20}]
    if i == 1:  # a configured component that went silent is graded from its own newest reading
        samples.append({"source": collector_scope("hwmon"), "metric": "hw.temperature",
                        "value": 60.0, "unit": "Cel", "labels": {}, "ts": ts - 3000})
    events = []
    if i % 3 == 0:
        events.append(event("md.degraded", "warning", START - 100, f"w{i}"))
    if i % 5 == 0:
        events.append(event("old.alert", "critical", START - 200_000, f"o{i}"))
    if i % 4 == 0:
        events.append(event("note", "info", START - 50, f"n{i}"))
    return host_batch(name, ts=ts, samples=samples, events=events)


def fill(env: ApiEnv, count: int) -> None:
    for i in range(count):
        env.push(picture(i))


COMPONENTS = [{"source": "hostwatch.collector.hwmon", "metric": "hw.temperature",
               "direction": "above", "warn": 70, "crit": 90}]
MONITORS = [{"name": "pm", "type": "pushed_host", "host": "h001", "interval": 30,
             "stale_after": 120, "components": COMPONENTS},
            {"name": "never", "type": "pushed_host", "host": "zz-never", "interval": 30}]


def make_env(tmp_path, count: int) -> ApiEnv:
    env = ApiEnv(tmp_path, monitors=MONITORS)
    fill(env, count)
    return env


def count_statements(env: ApiEnv) -> list[str]:
    sink: list[str] = []
    storage = env.store.storage
    read = storage.read

    async def counted_read(unit: Any) -> Any:  # fetchall goes through read too
        return await read(lambda db: unit(Counting(db, sink)))
    storage.read = counted_read
    return sink


def context(env: ApiEnv) -> ApiContext:
    return ApiContext(None, env.app.state.v2_runtime, env.wall.now)  # type: ignore[arg-type]


async def old_list(ctx: ApiContext) -> list[dict[str, Any]]:
    """The previous build: every host through HostViews.view, 12 or so statements each."""
    views = HostViews(ctx)
    rows = {r["host"]: r for r in await ctx.store.host_rows()}
    out = []
    for name in views.names(rows):
        view = await views.view(name, rows.get(name))
        if view is not None:
            out.append(_times(hostview.summarize(view)))
    return out


def test_the_statement_count_does_not_grow_with_the_hosts(tmp_path):
    counts = {}
    old_count = 0
    for n in (10, 50):
        env = make_env(tmp_path / str(n), n)
        try:
            ctx = context(env)
            sink = count_statements(env)
            body = asyncio.run(list_hosts(ctx, PageParams(100, None), None))
            counts[n] = len(sink)
            assert len(body["items"]) == n + 1  # the hosts and the one that never reported
            if n == 50:
                sink.clear()
                asyncio.run(old_list(ctx))
                old_count = len(sink)
        finally:
            env.close()
    assert counts[10] == counts[50] == 5
    assert old_count > 10 * counts[50]


def test_the_batched_list_equals_the_old_list_byte_for_byte(tmp_path):
    env = make_env(tmp_path, 30)
    try:
        ctx = context(env)
        new = asyncio.run(list_hosts(ctx, PageParams(100, None), None))["items"]
        old = asyncio.run(old_list(ctx))
        assert len(new) == 31
        assert json.dumps(new, sort_keys=False) == json.dumps(old, sort_keys=False)
        states = {h["status"] for h in new}
        assert {"good", "warning", "critical"} <= states  # the fixture covers the grades
        # The same rows come over HTTP, and a page is a prefix of the full list.
        http = env.get("/hosts?limit=7", headers=env.token()).json()
        assert http["items"] == new[:7] and http["next_cursor"]
    finally:
        env.close()


def etag(env: ApiEnv, headers: dict[str, str]) -> str:
    r = env.get("/hosts", headers=headers)
    assert r.status_code == 200
    return r.headers["ETag"]


def test_an_ingest_that_changes_nothing_shown_keeps_the_etag(tmp_path):
    env = make_env(tmp_path, 5)
    try:
        headers = env.token()
        first = etag(env, headers)
        # The agent pushes the same picture one second later: new batch, new last_seen.
        env.wall.advance(1)
        env.push(picture(2, ts=START - 5))
        assert etag(env, headers) == first
        assert env.get("/hosts", headers={**headers, "If-None-Match": first}).status_code == 304
        # A value that changes the grade is shown, so the ETag moves.
        env.wall.advance(1)
        env.push(host_batch("h002", ts=START - 4, samples=[
            {"source": collector_scope("cpu"), "metric": "system.cpu.utilization", "value": 0.99,
             "unit": "1", "labels": {}, "ts": START - 4}]))
        changed = etag(env, headers)
        assert changed != first
        # An alert event is shown too.
        env.wall.advance(1)
        env.push(host_batch("h003", ts=START - 3, events=[event("x", "critical", START - 3, "c")]))
        assert etag(env, headers) != changed
    finally:
        env.close()


def test_the_etag_still_moves_with_the_clock_bucket(tmp_path):
    env = make_env(tmp_path, 3)
    try:
        headers = env.token()
        first = etag(env, headers)
        env.wall.advance(60)  # ages are shown, so a minute later the list is a new picture
        assert etag(env, headers) != first
    finally:
        env.close()


@pytest.mark.parametrize("count", [0, 1])
def test_small_lists(tmp_path, count):
    env = ApiEnv(tmp_path)
    try:
        fill(env, count)
        body = env.get("/hosts", headers=env.token()).json()
        assert [h["host"] for h in body["items"]] == [f"h{i:03d}" for i in range(count)]
    finally:
        env.close()
