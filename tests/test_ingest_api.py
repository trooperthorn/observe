"""POST /api/ingest: auth, host binding, replay, limits, audit, boot classification."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from watchpost.alerts import Alerter
from watchpost.ingest.api import DenialAggregator, RateLimiter
from watchpost.ingest.boot import CLEAN, CRASH, UNKNOWN, classify_boot
from watchpost.ingest.keys import create_key, revoke_key
from watchpost.ingest.schema import MAX_BODY_BYTES, Event
from watchpost.scheduler import Scheduler
from watchpost.store import Store
from watchpost.web import create_app

from .conftest import make_config

FIXTURES = Path(__file__).parent / "fixtures" / "hostwatch"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Env:
    def __init__(self, tmp_path, rate: int = 120) -> None:
        self.path = str(tmp_path / "w.db")
        self.store = Store(self.path)
        self.cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                               server={"db_path": self.path, "ingest_rate_per_minute": rate})
        self.clock = Clock()
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        self.client = TestClient(create_app(self.cfg, self.store, sched, alerter,
                                            ingest_clock=self.clock))

    def key(self, host: str) -> str:
        return asyncio.run(create_key(self.store, host))[0]

    def post(self, body, key: str | None, **kw):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if isinstance(body, dict):
            return self.client.post("/api/ingest", json=body, headers=headers, **kw)
        return self.client.post("/api/ingest", content=body, headers=headers, **kw)

    def rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        db = sqlite3.connect(self.path)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def test_valid_batch_is_stored(env):
    key = env.key("nas01")
    r = env.post(fixture("batch_minimal"), key)
    assert r.status_code == 200, r.text
    assert r.json() == {"stored": 2, "events_stored": 0}
    assert env.rows("SELECT host, source, metric, value FROM host_samples ORDER BY metric") == [
        ("nas01", "rapl", "dram_watts", None), ("nas01", "rapl", "package_watts", 12.5)]
    sources = dict((s, (a, r)) for _, s, a, r, _ in env.rows("SELECT * FROM host_sources"))
    assert sources["rapl"] == (1, "") and sources["mdraid"][0] == 0
    (host,) = env.rows("SELECT host, platform, agent_version, confirmed FROM hosts")
    assert host == ("nas01", "linux", "0.9.0", 0)
    assert env.rows("SELECT last_used FROM ingest_keys")[0][0] is not None


def test_missing_or_wrong_key_is_401(env):
    key = env.key("nas01")
    body = fixture("batch_minimal")
    assert env.post(body, None).status_code == 401
    forged = key[:-1] + ("A" if key[-1] != "A" else "B")
    assert env.post(body, forged).status_code == 401
    assert env.post(body, "wpi_nosuchprefix_secret").status_code == 401
    assert env.client.post("/api/ingest", json=body,
                           headers={"Authorization": "Basic Zm9vOmJhcg=="}).status_code == 401
    assert env.rows("SELECT COUNT(*) FROM host_samples") == [(0,)]


def test_revoked_key_is_401(env):
    key = env.key("nas01")
    prefix = key.split("_")[1]
    assert asyncio.run(revoke_key(env.store, prefix))
    assert env.post(fixture("batch_minimal"), key).status_code == 401


def test_key_bound_to_another_host_is_403(env):
    key = env.key("nas02")
    r = env.post(fixture("batch_minimal"), key)  # the batch says nas01
    assert r.status_code == 403
    assert env.rows("SELECT COUNT(*) FROM host_samples") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM hosts") == [(0,)]


def test_duplicate_batch_replay_is_acknowledged_once(env):
    key = env.key("nas01")
    body = fixture("batch_with_events")
    first = env.post(body, key)
    assert first.json() == {"stored": 1, "events_stored": 2}
    again = env.post(body, key)
    assert again.status_code == 200
    assert again.json() == {"stored": 0, "events_stored": 0, "duplicate": True}
    assert env.rows("SELECT COUNT(*) FROM host_samples") == [(1,)]
    assert env.rows("SELECT COUNT(*) FROM host_events") == [(2,)]
    other = copy.deepcopy(body)
    other["batch_id"] = "2b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
    other["samples"][0]["ts"] += 60
    res = env.post(other, key).json()
    assert res["stored"] == 1 and res["events_stored"] == 0  # events dedup by key


def test_boot_and_crash_classification_from_fixture(env):
    key = env.key("nas01")
    assert env.post(fixture("batch_with_events"), key).status_code == 200
    rows = env.rows("SELECT dedup_key, kind, detail FROM host_events ORDER BY ts")
    by_key = {k: (kind, json.loads(d)) for k, kind, d in rows}
    assert by_key["boot:aaaa"][1]["classification"] == CLEAN
    assert by_key["boot:bbbb"][1]["classification"] == CRASH
    assert by_key["boot:bbbb"][1]["previous_boot_id"] == "aaaa"
    # hosts row follows the newest boot event: boot bbbb, previous boot crashed
    assert env.rows("SELECT boot_id, boot_ts, clean_shutdown FROM hosts") == [("bbbb", 1759999000.0, 0)]


@pytest.mark.parametrize("kind,expected,flag", [
    ("boot.clean_shutdown", CLEAN, 1),
    ("boot.kernel_panic", CRASH, 0),
    ("boot.watchdog_reset", CRASH, 0),
    ("boot.power_loss", CRASH, 0),
    ("boot.unknown_unclean", CRASH, 0),
    ("boot.unknown", UNKNOWN, None),
    ("boot.agent_stopped", UNKNOWN, None),
    ("boot.from_the_future", UNKNOWN, None),
])
def test_boot_kinds_reduce_to_three_states(env, kind, expected, flag):
    ev = Event(kind=kind, severity="info", source="boot", ts=5.0, title="t", dedup_key="boot:cc",
               boot_id="cc")
    assert classify_boot(ev) == expected
    key = env.key("nas01")
    body = fixture("batch_minimal")
    body["events"] = [{"kind": kind, "severity": "info", "source": "boot", "ts": 5.0,
                       "title": "t", "dedup_key": "boot:cc", "boot_id": "cc"}]
    assert env.post(body, key).status_code == 200
    assert env.rows("SELECT boot_id, clean_shutdown FROM hosts") == [("cc", flag)]


def test_non_boot_event_does_not_touch_boot_state(env):
    key = env.key("nas01")
    body = fixture("batch_minimal")
    body["events"] = [{"kind": "md.degraded", "severity": "critical", "source": "mdraid",
                       "ts": 5.0, "title": "t", "dedup_key": "md:1"}]
    assert env.post(body, key).status_code == 200
    assert env.rows("SELECT boot_id, clean_shutdown FROM hosts") == [(None, None)]
    detail = json.loads(env.rows("SELECT detail FROM host_events")[0][0])
    assert "classification" not in detail


def test_older_boot_event_does_not_overwrite_newer_state(env):
    key = env.key("nas01")
    body = fixture("batch_with_events")
    assert env.post(body, key).status_code == 200
    late = fixture("batch_minimal")
    late["events"] = [{"kind": "boot.clean_shutdown", "severity": "info", "source": "boot",
                       "ts": 1.0, "title": "old", "dedup_key": "boot:zzzz", "boot_id": "zzzz"}]
    assert env.post(late, key).status_code == 200
    assert env.rows("SELECT boot_id, clean_shutdown FROM hosts") == [("bbbb", 0)]


def test_invalid_body_is_422_and_stores_nothing(env):
    key = env.key("nas01")
    bad = fixture("batch_minimal")
    bad["unexpected"] = 1
    assert env.post(bad, key).status_code == 422
    worse = fixture("batch_minimal")
    worse["samples"][0]["value"] = "twelve"
    assert env.post(worse, key).status_code == 422
    assert env.post(b"not json", key).status_code == 422
    assert env.post(b"[1,2]", key).status_code == 422
    assert env.rows("SELECT COUNT(*) FROM host_samples") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM hosts") == [(0,)]


def test_oversized_body_is_413_and_authentication_comes_first(env):
    key = env.key("nas01")
    big = b" " * (MAX_BODY_BYTES + 1)
    assert env.post(big, key).status_code == 413
    # Without a key the body is never read and the answer is 401, not 413.
    assert env.post(big, None).status_code == 401


def test_rate_limit_returns_429_and_recovers(tmp_path):
    e = Env(tmp_path, rate=3)
    try:
        key = e.key("nas01")
        body = fixture("batch_minimal")
        codes = [e.post(body, key).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        assert e.post(body, key).headers["retry-after"] == "60"
        e.clock.now += 61
        assert e.post(body, key).status_code == 200
    finally:
        e.client.close()
        e.store.close()


def test_denials_are_audited_in_aggregate(env):
    body = fixture("batch_minimal")
    for _ in range(5):
        assert env.post(body, None).status_code == 401
    rows = env.rows("SELECT kind, actor, status, remote, detail FROM audit")
    assert len(rows) == 1  # five denials in one window, one row
    kind, actor, status, remote, detail = rows[0]
    assert (kind, status, remote) == ("ingest_denied", 401, "testclient")
    env.clock.now += 61
    assert env.post(body, None).status_code == 401
    rows = env.rows("SELECT detail FROM audit ORDER BY id")
    assert len(rows) == 2
    assert json.loads(rows[1][0])["denials_covered"] == 5


def test_audit_never_holds_the_key(env):
    key = env.key("nas02")
    assert env.post(fixture("batch_minimal"), key).status_code == 403
    secret = key.split("_", 2)[2]
    dump = json.dumps(env.rows("SELECT * FROM audit"))
    assert secret not in dump and key not in dump
    kind, actor, detail = env.rows("SELECT kind, actor, detail FROM audit")[0]
    assert actor == key.split("_")[1]
    assert json.loads(detail)["claimed_host"] == "nas01"


def test_ingest_is_not_behind_basic_auth_but_dashboard_still_is(tmp_path):
    e = Env(tmp_path)
    try:
        cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                          server={"basic_auth_user": "ops", "basic_auth_password": "s3cret",
                                  "db_path": e.path})
        alerter = Alerter(cfg)
        client = TestClient(create_app(cfg, e.store, Scheduler(cfg, e.store, alerter), alerter))
        key = e.key("nas01")
        r = client.post("/api/ingest", json=fixture("batch_minimal"),
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        assert client.get("/api/monitors").status_code == 401
        client.close()
    finally:
        e.client.close()
        e.store.close()


def test_ingest_key_does_not_open_other_endpoints(env):
    key = env.key("nas01")
    r = env.client.get("/api/events", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200  # no basic auth configured in this env; endpoint is the same as before
    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                      server={"basic_auth_user": "ops", "basic_auth_password": "s3cret",
                              "db_path": env.path})
    alerter = Alerter(cfg)
    client = TestClient(create_app(cfg, env.store, Scheduler(cfg, env.store, alerter), alerter))
    assert client.get("/api/events", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    client.close()


def test_get_on_ingest_is_not_allowed(env):
    assert env.client.get("/api/ingest").status_code == 405


def test_denial_aggregator_bounds_peers():
    clock = Clock()
    agg = DenialAggregator(clock)
    agg.MAX_PEERS = 2
    assert agg.note("a") == 0 and agg.note("b") == 0
    assert agg.note("c") == 0  # overflow bucket, first
    assert agg.note("d") is None  # shares the overflow bucket
    clock.now += 61
    assert agg.note("a") == 1


def test_rate_limiter_window():
    clock = Clock()
    rl = RateLimiter(2, clock)
    assert [rl.allow("a") for _ in range(3)] == [True, True, False]
    assert rl.allow("b")
    clock.now += 60
    assert rl.allow("a")
