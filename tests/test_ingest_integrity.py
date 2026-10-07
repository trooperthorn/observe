"""Ingest integrity and abuse limits: replays after trim, reused idempotency keys, points with no
timestamp, clock skew, compression bombs and the cost of refusals (docs/DATA-API-DESIGN.md
section 6.4)."""

from __future__ import annotations

import asyncio
import gzip
import json
import time

import pytest

from observe.otlp import wire

from .otlp_build import gauge, metrics_request, number
from .test_otlp_ingest import JSON, PROTO, T0, Env, simple

DAY = 86400.0


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def levels(env: Env) -> list[list[tuple]]:
    return [env.rows(f"SELECT * FROM {t} ORDER BY series_id, bucket")
            for t in ("rollup_5m", "rollup_1h", "rollup_1d")]


def ten_points() -> dict:
    return metrics_request("nas01", {"s": [gauge("m", [number(float(i), T0 + i * 60)
                                                      for i in range(10)])]})


def test_a_replay_after_the_raw_rows_were_trimmed_changes_no_rollup(env):
    key = env.key("nas01")
    assert env.push(ten_points(), key).status_code == 200
    env.wall.now = T0 + 20 * DAY  # past raw (7 d) and 5 minute (14 d) retention
    asyncio.run(env.store.storage.apply_retention(now=env.wall.now, retention_days=7,
                                          audit_retention_days=365))
    before = levels(env)
    assert before[0] == [] and before[1] and before[2]  # only the 5 minute rows are gone
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]
    again = env.push(ten_points(), key)
    assert again.status_code == 200
    assert again.json()["partialSuccess"]["rejectedDataPoints"] == "10"  # late, not stored
    assert levels(env) == before
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]
    n, total = env.rows("SELECT n, sum_v FROM rollup_1d")[0]
    assert (n, total) == (10, 45.0)  # counted once


def test_a_reused_idempotency_key_with_another_body_is_409_and_audited(env):
    key = env.key("nas01")
    headers = {"Idempotency-Key": "report-1"}
    assert env.push(simple(value=1.0), key, headers=headers).status_code == 200
    r = env.push(simple(value=2.0, ts=T0 + 60), key, headers=headers)
    assert r.status_code == 409
    assert [row[0] for row in env.rows("SELECT value FROM samples")] == [1.0]
    audit = env.rows("SELECT status, detail FROM audit WHERE kind = 'ingest_denied'")
    assert audit and audit[-1][0] == 409
    assert "different body" in json.loads(audit[-1][1])["reason"]
    # The same bytes under the same key are still a resend.
    assert env.push(simple(value=1.0), key, headers=headers).status_code == 200
    assert env.rows("SELECT COUNT(*) FROM samples") == [(1,)]


def test_identical_bodies_without_timestamps_at_different_times_both_store(env):
    key = env.key("nas01")
    dp = number(1.0, 0)
    del dp["timeUnixNano"]
    req = metrics_request("nas01", {"s": [gauge("m", [dp])]})
    first = env.wall.now
    assert env.push(req, key).status_code == 200
    env.wall.now = first + 1.2
    assert env.push(req, key).status_code == 200
    assert [r[0] for r in env.rows("SELECT ts FROM samples ORDER BY ts")] == [
        round(first * 1000), round((first + 1.2) * 1000)]
    assert env.rows("SELECT last_seen FROM hosts") == [(first + 1.2,)]


def test_a_future_point_is_stored_at_receipt_and_does_not_freeze_latest(env):
    key = env.key("nas01")
    now = env.wall.now
    far = metrics_request("nas01", {"s": [gauge("m", [number(5.0, now + 299)])]})
    assert env.push(far, key).status_code == 200
    assert env.rows("SELECT ts FROM samples") == [(round(now * 1000),)]
    env.wall.now = now + 2
    near = metrics_request("nas01", {"s": [gauge("m", [number(6.0, now + 3)])]})
    assert env.push(near, key).status_code == 200
    assert env.rows("SELECT value FROM latest") == [(6.0,)]


def test_a_crafted_tiny_gzip_is_refused_before_decoding_in_bounded_time(env, monkeypatch):
    key = env.key("nas01")
    bomb = gzip.compress(b"\x0a\x00" * 2_000_000)
    assert len(bomb) < 20_000
    called = []
    monkeypatch.setattr(wire, "decode", lambda *a: called.append(a))
    start = time.perf_counter()
    r = env.push(bomb, key, headers={"Content-Type": PROTO, "Content-Encoding": "gzip"})
    assert r.status_code == 400 and called == []
    assert time.perf_counter() - start < 2.0


def test_the_worst_body_that_passes_the_size_checks_costs_bounded_work(env):
    """Empty messages are the cheapest bytes to build and the dearest to decode per byte. A body
    at the size cap is stopped by the work budget after a small, fixed amount of work."""
    key = env.key("nas01")
    worst = b"\x0a\x00" * 500_000  # 1 MB, the largest accepted body
    start = time.perf_counter()
    with pytest.raises(wire.WireError, match="too many fields"):
        wire.decode(worst, "MetricsRequest")
    assert time.perf_counter() - start < 1.0
    start = time.perf_counter()
    r = env.push(worst, key, headers={"Content-Type": PROTO})
    assert r.status_code == 400
    assert time.perf_counter() - start < 2.0


def test_a_request_of_the_largest_allowed_size_still_decodes():
    points = [number(float(i), T0 + i, {"a": "b", "c": str(i)}) for i in range(5000)]
    from .otlp_build import proto
    body = proto(metrics_request("nas01", {"s": [gauge("m", points)]}))
    assert wire.decode(body, "MetricsRequest")["resourceMetrics"]


def test_refused_bodies_count_extra_against_the_key_rate_limit(env):
    key = env.key("nas01")
    codes = [env.push(b"{", key, headers={"Content-Type": JSON}).status_code
             for _ in range(40)]
    assert codes[0] == 400 and 429 in codes  # 120 a minute, each refusal costs five
    assert codes.index(429) <= 25
