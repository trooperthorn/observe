"""Field reports as OTLP log records: the wpf key, the plugin handler, caps, clocks and audit."""

from __future__ import annotations

import asyncio
import gzip
import json
import sqlite3
import zlib
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from observe.alerts import Alerter
from observe.ingest.keys import create_key
from observe.plugins import (GROUP, KeyScope, PluginBase, PluginError, PluginRouter,
                               load_plugins)
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app
from observe_pockethernet.keys import create_field_key
from observe_pockethernet.otlp import EVENT, correct_clock
from observe_pockethernet.reports import prune_evidence

from .conftest import make_config
from .otlp_build import gauge, log_record, logs_request, metrics_request, number
from .test_auth import Clock

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "pockethernet" / "report_v1.json")
                     .read_text(encoding="utf-8"))
TAKEN_S = FIXTURE["taken_at_ms"] / 1000
URL = "/v1/logs"


def run(coro):
    return asyncio.run(coro)


def dump(body: dict[str, Any]) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def report_request(raw: bytes, ts: float = TAKEN_S) -> dict[str, Any]:
    """The OTLP logs request a phone sends for one report: the report JSON is the record body."""
    return logs_request(None, [log_record(EVENT, ts, raw.decode("utf-8"))], scope="pockethernet")


class Posted:
    """A response, with what the handler said about the report read back from the audit row, so
    a test can ask for the result of a report as it asked the old upload route."""

    def __init__(self, resp: Any, handled: dict[str, Any] | None) -> None:
        self.resp, self.handled = resp, handled
        self.status_code, self.headers, self.text = resp.status_code, resp.headers, resp.text

    def json(self) -> Any:
        h = self.handled
        if h is None:
            return self.resp.json()
        out = {"result": h["result"], "report_id": h["report_id"], "revision": h["stored_revision"],
               "clock_corrected": h["clock_corrected"], "taken_at_ms": h["taken_at_ms"]}
        if h.get("derive_status"):
            out["derive_status"] = h["derive_status"]
        return out


class Env:
    def __init__(self, tmp_path, rate: int = 300, settings: dict[str, Any] | None = None) -> None:
        self.path = str(tmp_path / "w.db")
        self.cfg = make_config(
            [{"name": "p", "type": "ping", "host": "127.0.0.1"}],
            plugins=["pockethernet"],
            plugin_settings={"pockethernet": settings or {}},
            server={"db_path": self.path, "ingest_rate_per_minute": rate})
        loaded = load_plugins(self.cfg, lambda: [EntryPoint(
            "pockethernet", "observe_pockethernet:plugin", GROUP)])
        self.store = Store(self.path, loaded)
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        self.clock = Clock()
        self.clock.now = TAKEN_S + 100  # the phone's report was taken 100 s ago
        self.client = TestClient(
            create_app(self.cfg, self.store, sched, alerter, plugins=loaded,
                       auth_clock=self.clock), base_url="https://testserver")
        self.key, self.info = run(create_field_key(self.store, "sean-pixel"))

    def send(self, request: Any, key: str | None = "default",
             headers: dict[str, str] | None = None, gzipped: bool = False):
        """Post an OTLP request (a dict, or raw bytes) to /v1/logs as JSON."""
        raw = dump(request) if isinstance(request, dict) else request
        h = {"Content-Type": "application/json"}
        token = self.key if key == "default" else key
        if token:
            h["Authorization"] = f"Bearer {token}"
        if gzipped:
            raw, h["Content-Encoding"] = gzip.compress(raw), "gzip"
        return self.client.post(URL, content=raw, headers={**h, **(headers or {})})

    def post(self, body: bytes | dict[str, Any], key: str | None = "default",
             headers: dict[str, str] | None = None, gzipped: bool = False):
        """Post one report as an OTLP log record. A dict is dumped as the report JSON."""
        raw = dump(body) if isinstance(body, dict) else body
        before = self.rows("SELECT COUNT(*) FROM audit WHERE kind='plugin_request'")[0][0]
        resp = self.send(report_request(raw), key, headers, gzipped)
        handled = None
        after = self.rows("SELECT COUNT(*) FROM audit WHERE kind='plugin_request'")[0][0]
        if resp.status_code == 200 and after > before:
            detail = json.loads(self.rows("SELECT detail FROM audit WHERE kind='plugin_request' "
                                          "ORDER BY id DESC LIMIT 1")[0][0])
            handled = detail["handled"][0] if detail.get("handled") else None
        return Posted(resp, handled)

    def rows(self, sql: str, *args: Any):
        db = sqlite3.connect(self.path)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def report(self, **changes: Any) -> dict[str, Any]:
        return {**FIXTURE, **changes}

    def close(self) -> None:
        self.client.close()
        self.store.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def rejected(resp: Any) -> tuple[int, str]:
    """(rejected log records, message) from a JSON partial success, (0, '') when it is empty."""
    ps = resp.json().get("partialSuccess", {})
    return int(ps.get("rejectedLogRecords", 0)), ps.get("errorMessage", "")


def test_an_empty_request_checks_the_key_and_stores_nothing(env):
    r = env.send(b'{"resourceLogs":[]}')
    assert r.status_code == 200 and r.json() == {}
    assert env.send(b'{"resourceLogs":[]}', key=None).status_code == 401
    assert env.rows("SELECT * FROM field_reports") == []
    assert env.rows("SELECT * FROM hosts") == []


def test_accepts_a_report_and_stores_the_exact_body_as_evidence(env):
    raw = dump(FIXTURE)
    r = env.post(raw)
    assert r.status_code == 200
    assert r.json() == {"result": "accepted", "report_id": FIXTURE["report_id"], "revision": 1,
                        "clock_corrected": False, "taken_at_ms": FIXTURE["taken_at_ms"]}
    (row,) = env.rows("SELECT source, report_id, revision, taken_at_ms, clock_corrected, "
                      "tester_serial, site, port_id, body FROM field_reports")
    assert row[:8] == ("sean-pixel", FIXTURE["report_id"], 1, FIXTURE["taken_at_ms"], 0,
                       1234567, "HQ", "HQ/Main/Room 204/PP-A/12")
    assert bytes(row[8]) == raw


def test_same_report_again_is_a_duplicate_and_changes_nothing(env):
    assert env.post(FIXTURE).json()["result"] == "accepted"
    before = env.rows("SELECT * FROM field_reports")
    r = env.post(env.report(notes="edited but the same revision"))
    assert r.status_code == 200 and r.json()["result"] == "duplicate"
    assert env.rows("SELECT * FROM field_reports") == before


def test_higher_revision_replaces_and_lower_is_ignored(env):
    env.post(env.report(revision=2, notes="second"))
    r = env.post(env.report(revision=3, notes="third"))
    assert r.json()["result"] == "replaced" and r.json()["revision"] == 3
    (row,) = env.rows("SELECT revision, revisions_seen, body FROM field_reports")
    assert row[0] == 3 and row[1] == 2 and json.loads(bytes(row[2]))["notes"] == "third"
    r = env.post(env.report(revision=1, notes="old"))
    assert r.status_code == 200
    assert r.json() == {"result": "ignored", "report_id": FIXTURE["report_id"], "revision": 3,
                        "clock_corrected": False, "taken_at_ms": FIXTURE["taken_at_ms"]}
    (row,) = env.rows("SELECT revision, revisions_seen, body FROM field_reports")
    assert row[0] == 3 and row[1] == 2 and json.loads(bytes(row[2]))["notes"] == "third"


def test_another_device_cannot_replace_or_hide_a_report(env):
    other, _ = run(create_field_key(env.store, "other-phone"))
    env.post(FIXTURE)
    r = env.post(env.report(revision=9, notes="forged"), key=other)
    assert r.json()["result"] == "accepted"
    rows = env.rows("SELECT source, revision FROM field_reports ORDER BY source")
    assert rows == [("other-phone", 9), ("sean-pixel", 1)]


def test_future_clock_is_set_to_now_and_flagged(env):
    future = int((env.clock.now + 3600) * 1000)
    r = env.post(env.report(taken_at_ms=future))
    assert r.json()["clock_corrected"] is True
    assert r.json()["taken_at_ms"] == int(env.clock.now * 1000)
    (row,) = env.rows("SELECT taken_at_ms, reported_taken_at_ms, clock_corrected "
                      "FROM field_reports")
    assert row == (int(env.clock.now * 1000), future, 1)


def test_small_future_drift_is_kept(env):
    near = int((env.clock.now + 299) * 1000)
    r = env.post(env.report(taken_at_ms=near))
    assert r.json()["clock_corrected"] is False and r.json()["taken_at_ms"] == near


def test_slow_phone_clock_is_corrected_from_the_sent_header(env):
    behind = 7200  # the phone clock is two hours slow
    taken = int((env.clock.now - 100 - behind) * 1000)
    sent = int((env.clock.now - behind) * 1000)
    r = env.post(env.report(taken_at_ms=taken), headers={"X-Report-Sent-Ms": str(sent)})
    assert r.status_code == 200 and r.json()["clock_corrected"] is True
    assert r.json()["taken_at_ms"] == int((env.clock.now - 100) * 1000)
    (row,) = env.rows("SELECT reported_taken_at_ms FROM field_reports")
    assert row == (taken,)


def test_sent_header_within_the_skew_limit_changes_nothing(env):
    sent = int((env.clock.now - 299) * 1000)
    r = env.post(FIXTURE, headers={"X-Report-Sent-Ms": str(sent)})
    assert r.json()["clock_corrected"] is False
    assert r.json()["taken_at_ms"] == FIXTURE["taken_at_ms"]


def test_bad_sent_header_is_refused(env):
    for value in ("abc", "-5", "1.5", "99999999999999999999"):
        assert env.post(FIXTURE, headers={"X-Report-Sent-Ms": value}).status_code == 400
    assert env.rows("SELECT * FROM field_reports") == []


def test_correct_clock_rules():
    now = 1_000_000.0
    now_ms = 1_000_000_000
    assert correct_clock(now_ms - 10**9, now, None) == (now_ms - 10**9, False)
    assert correct_clock(now_ms + 300_000, now, None) == (now_ms + 300_000, False)
    assert correct_clock(now_ms + 300_001, now, None) == (now_ms, True)
    assert correct_clock(5, now, now_ms + 10_000_000)[0] == 0  # never negative
    # A phone that is ten minutes fast is brought back by the whole difference.
    assert correct_clock(now_ms + 600_000, now, now_ms + 600_000) == (now_ms, True)


def test_gzip_request_is_accepted_and_the_report_is_stored_inflated(env):
    raw = dump(FIXTURE)
    r = env.post(raw, gzipped=True)
    assert r.status_code == 200 and r.json()["result"] == "accepted"
    (row,) = env.rows("SELECT body FROM field_reports")
    assert bytes(row[0]) == raw


def test_the_core_gzip_and_size_caps_apply_to_reports(env):
    bomb = gzip.compress(b"0" * (5 * 1024 * 1024))
    r = env.send(bomb, headers={"Content-Encoding": "gzip"})
    assert r.status_code == 413 and "inflated" in r.json()["detail"]
    assert env.send(dump(FIXTURE), headers={"Content-Encoding": "br"}).status_code == 415
    assert env.send(zlib.compress(dump(FIXTURE)),
                    headers={"Content-Encoding": "gzip"}).status_code == 400
    assert env.send(b"x" * (1_048_576 + 1)).status_code == 413
    assert env.rows("SELECT * FROM field_reports") == []


def test_missing_wrong_and_revoked_keys_are_401(env):
    request = report_request(dump(FIXTURE))
    assert env.send(request, key=None).status_code == 401
    r = env.send(request, key="wpf_000000000000_nothing")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    from observe.ingest.keys import revoke_key
    run(revoke_key(env.store, env.info.prefix))
    assert env.send(request).status_code == 401
    assert env.rows("SELECT * FROM field_reports") == []


def test_a_field_key_writes_no_host_data(env):
    """A wpf key is for field reports only: host.name in its metrics does not make a host, and
    log records that no plugin handles are refused."""
    metrics = metrics_request("nas01", {"rapl": [gauge("package_watts", [number(1.0, TAKEN_S)])]})
    r = env.client.post("/v1/metrics", json=metrics,
                        headers={"Authorization": f"Bearer {env.key}"})
    assert r.status_code == 200
    assert env.rows("SELECT * FROM hosts") == []
    assert [(n, k) for n, k, _ in env.rows("SELECT name, kind, attrs FROM resources")] == [
        ("sean-pixel", "field_tester")]
    other = logs_request("nas01", [log_record("boot.kernel_panic", TAKEN_S, "x")])
    r = env.send(other)
    assert rejected(r)[0] == 1 and "no plugin handles" in rejected(r)[1]
    assert env.rows("SELECT * FROM host_events") == []


def test_the_device_label_of_a_field_resource_comes_from_the_key(env):
    metrics = metrics_request(None, {"ph": [gauge("link_speed_mbps", [number(1000.0, TAKEN_S)])]},
                              observe__field__device="someone-else")
    assert env.client.post("/v1/metrics", json=metrics, headers={
        "Authorization": f"Bearer {env.key}"}).status_code == 200
    assert [r[0] for r in env.rows("SELECT name FROM resources")] == ["sean-pixel"]
    assert "someone-else" not in json.dumps(env.rows("SELECT attrs FROM resources"))


def test_an_oversize_report_is_refused_without_failing_the_request(env):
    big = env.report(notes="n" * 4096, warnings=["w" * 1000] * 32,
                     steps=[{"step": "s", "label": "l", "status": "ok",
                             "fields": [{"name": "n", "value": "v" * 1000}] * 64}] * 8)
    r = env.post(big)
    assert r.status_code == 200
    assert rejected(r) == (1, "report is too large (1)")
    assert env.rows("SELECT * FROM field_reports") == []


def test_invalid_reports_are_counted_in_the_partial_success(env):
    for body, reason in ((b"{not json", ""), (dump(env.report(surprise=1)), ""),
                         (dump(env.report(transcript="secret")), ""),
                         (b"[" * 40 + b"]" * 40, "nested too deeply")):
        r = env.post(body)
        assert r.status_code == 200
        count, message = rejected(r)
        assert count == 1 and message and reason in message
    assert env.rows("SELECT * FROM field_reports") == []


def test_a_record_that_is_not_a_string_report_is_refused(env):
    rec = log_record(EVENT, TAKEN_S)
    rec["body"] = {"kvlistValue": {"values": []}}
    r = env.send(logs_request(None, [rec], scope="pockethernet"))
    assert rejected(r)[0] == 1 and "string" in rejected(r)[1]


def test_a_good_and_a_bad_record_in_one_request_store_the_good_one(env):
    good = log_record(EVENT, TAKEN_S, dump(FIXTURE).decode())
    bad = log_record(EVENT, TAKEN_S, "{nope")
    r = env.send(logs_request(None, [bad, good], scope="pockethernet"))
    assert r.status_code == 200 and rejected(r)[0] == 1
    assert env.rows("SELECT report_id FROM field_reports") == [(FIXTURE["report_id"],)]


def test_rate_limit_is_429_and_uploads_stop(tmp_path):
    e = Env(tmp_path, rate=2)
    try:
        assert e.post(FIXTURE).status_code == 200
        assert e.post(FIXTURE).status_code == 200
        r = e.post(FIXTURE)
        assert r.status_code == 429 and r.headers["retry-after"] == "60"
        assert e.rows("SELECT status FROM audit WHERE kind='ingest_denied'") == [(429,)]
    finally:
        e.close()


def test_a_valid_key_is_limited_across_peers(tmp_path):
    e = Env(tmp_path, rate=2)
    try:
        assert [e.post(FIXTURE).status_code for _ in range(3)] == [200, 200, 429]
        other = TestClient(e.client.app, base_url="https://testserver",
                           client=("203.0.113.10", 5000))
        try:
            # The first key stays limited from a new peer; a new key from a new peer is fine.
            r = other.post(URL, content=dump(report_request(dump(FIXTURE))), headers={
                "Authorization": f"Bearer {e.key}", "Content-Type": "application/json"})
            assert r.status_code == 429
            second, _info = run(create_field_key(e.store, "other-phone"))
            r = other.post(URL, content=dump(report_request(dump(e.report(report_id="other-2")))),
                           headers={"Authorization": f"Bearer {second}",
                                    "Content-Type": "application/json"})
            assert r.status_code == 200
        finally:
            other.close()
    finally:
        e.close()


def test_audit_rows_for_accepted_refused_and_denied_pushes(env):
    env.post(FIXTURE)
    env.post(env.report(revision=2), headers={"X-Report-Sent-Ms": "1"})
    env.post(b"{bad")
    env.post(FIXTURE, key=None)
    rows = env.rows("SELECT actor, method, path, status, remote, detail FROM audit "
                    "WHERE kind='plugin_request' ORDER BY id")
    assert [r[3] for r in rows] == [200, 200, 200]
    for actor, method, path, _, remote, _ in rows:
        assert actor == env.info.prefix and method == "POST" and path == URL
        assert remote
    first, second, third = (json.loads(r[5]) for r in rows)
    handled = first["handled"][0]
    assert handled.pop("derived") == {"properties_added": 20, "properties_verified": 0,
                                      "jack_linked": True}
    assert handled == {"plugin": "pockethernet", "result": "accepted",
                       "report_id": FIXTURE["report_id"], "revision": 1, "stored_revision": 1,
                       "clock_corrected": False, "bytes": len(dump(FIXTURE)),
                       "taken_at_ms": FIXTURE["taken_at_ms"]}
    assert first["device"] == "sean-pixel" and first["rejected"] == 0
    assert second["handled"][0]["result"] == "replaced"
    assert second["handled"][0]["clock_corrected"] is True
    assert third["rejected"] == 1 and third["handled"] == []
    denied = env.rows("SELECT actor, status, path FROM audit WHERE kind='ingest_denied'")
    assert denied == [("", 401, URL)]
    # No key and no body content is ever written.
    text = json.dumps(env.rows("SELECT * FROM audit"))
    assert env.key not in text and FIXTURE["notes"] not in text


def test_rejected_report_audit_has_no_report_content(env):
    env.post(env.report(transcript="TOPSECRETVALUE"))
    text = json.dumps(env.rows("SELECT detail FROM audit"))
    assert "TOPSECRETVALUE" not in text


def test_a_failing_handler_is_audited_and_is_a_server_error(env, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr("observe_pockethernet.otlp.store_and_derive", boom)
    quiet = TestClient(env.client.app, base_url="https://testserver",
                       raise_server_exceptions=False)
    r = quiet.post(URL, content=dump(report_request(dump(FIXTURE))), headers={
        "Authorization": f"Bearer {env.key}", "Content-Type": "application/json"})
    assert r.status_code == 500
    rows = env.rows("SELECT actor, status, detail FROM audit WHERE kind='plugin_failed'")
    assert len(rows) == 1 and rows[0][0] == env.info.prefix and rows[0][1] == 500
    assert json.loads(rows[0][2]) == {"error": "RuntimeError", "plugin": "pockethernet"}
    quiet.close()


def test_retention_drops_old_bodies_but_keeps_the_summary(tmp_path):
    e = Env(tmp_path, settings={"evidence_retention_days": 30})
    try:
        e.post(FIXTURE)
        day = 86400
        assert run(prune_evidence(e.store, e.clock.now + 29 * day, 30)) == 0
        assert run(prune_evidence(e.store, e.clock.now + 31 * day, 30)) == 1
        (row,) = e.rows("SELECT revision, body, body_pruned_at FROM field_reports")
        assert row[0] == 1 and row[1] is None and row[2] is not None
        assert run(prune_evidence(e.store, e.clock.now + 40 * day, 30)) == 0
        assert e.post(FIXTURE).json()["result"] == "duplicate"  # a replay is still recognised
        assert e.post(e.report(revision=2)).json()["result"] == "replaced"
        (row,) = e.rows("SELECT body, body_pruned_at FROM field_reports")
        assert row[0] is not None and row[1] is None
    finally:
        e.close()


def test_plugin_prune_hook_uses_the_configured_retention(tmp_path):
    e = Env(tmp_path, settings={"evidence_retention_days": 2})
    try:
        e.post(FIXTURE)
        from observe_pockethernet import plugin
        assert plugin.settings.evidence_retention_days == 2
        assert run(plugin.prune(e.store, e.clock.now + 3 * 86400)) == 1
    finally:
        e.close()


def test_retention_setting_is_validated_without_echoing_the_value(tmp_path):
    for bad in ({"evidence_retention_days": 0}, {"unknown": 1}):
        cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                          plugins=["pockethernet"], plugin_settings={"pockethernet": bad})
        with pytest.raises(PluginError, match="plugin_settings.pockethernet"):
            load_plugins(cfg, lambda: [EntryPoint(
                "pockethernet", "observe_pockethernet:plugin", GROUP)])


# ---- the core's rules for key-authenticated plugin routers --------------------------------

class _Keyed(PluginBase):
    name = "keyed"
    core_versions = ">=2026.9,<2027"

    def __init__(self, router: PluginRouter, scopes: list[KeyScope]) -> None:
        self._router, self._scopes = router, scopes

    def routers(self):
        return [self._router]

    def key_scopes(self):
        return self._scopes


def _load(plugin: PluginBase) -> None:
    import sys
    import types
    mod = types.ModuleType("tests.fakes.keyed_tmp")
    mod.plugin = plugin  # type: ignore[attr-defined]
    sys.modules["tests.fakes.keyed_tmp"] = mod
    try:
        cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}], plugins=["keyed"])
        load_plugins(cfg, lambda: [EntryPoint("keyed", "tests.fakes.keyed_tmp:plugin", GROUP)])
    finally:
        sys.modules.pop("tests.fakes.keyed_tmp", None)


def test_core_refuses_a_router_with_a_scope_the_plugin_does_not_own():
    scopes = [KeyScope("kyd")]
    _load(_Keyed(PluginRouter(APIRouter(), key_scope="kyd", public_prefix="/api/v1"), scopes))
    for pr in (PluginRouter(APIRouter(), key_scope="wpi"),
               PluginRouter(APIRouter(), key_scope="other"),
               PluginRouter(APIRouter(), key_scope="kyd", admin=True),
               PluginRouter(APIRouter(), public_prefix="/api/v1"),
               PluginRouter(APIRouter(), key_scope="kyd", public_prefix="/api/v1x"),
               PluginRouter(APIRouter(), key_scope="kyd", public_prefix="/api/ingest"),
               PluginRouter(APIRouter(), key_scope="kyd", public_prefix="/internal/v1"),
               PluginRouter(APIRouter(), key_scope="kyd", public_prefix="/api/v1/../x")):
        with pytest.raises(PluginError):
            _load(_Keyed(pr, scopes))


def test_unlisted_plugin_handles_no_report(tmp_path):
    path = str(tmp_path / "w.db")
    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                      server={"db_path": path})
    store = Store(path)
    alerter = Alerter(cfg)
    app = create_app(cfg, store, Scheduler(cfg, store, alerter), alerter)
    key, _ = run(create_key(store, "sean-pixel", scope="wpf"))
    with TestClient(app, base_url="https://testserver") as client:
        r = client.post(URL, json=report_request(dump(FIXTURE)),
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        assert rejected(r)[0] == 1 and "no plugin handles" in rejected(r)[1]
    store.close()


def test_a_field_key_may_not_write_port_metrics(env):
    """A port belongs to the collectors that own its identity. A wpf key naming a switch and a
    port is refused and counted, and no port resource is created; the plugin derives port
    properties from a report through its own validated path."""
    metrics = metrics_request(None, {"ph": [gauge("link_speed_mbps", [number(1000.0, TAKEN_S)])]},
                              observe__switch="core-sw", observe__port="1")
    r = env.client.post("/v1/metrics", json=metrics,
                        headers={"Authorization": f"Bearer {env.key}"})
    assert r.status_code == 200
    assert r.json()["partialSuccess"]["rejectedDataPoints"] == "1"
    assert "may not write port metrics" in r.json()["partialSuccess"]["errorMessage"]
    assert env.rows("SELECT * FROM resources WHERE kind = 'port'") == []
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]
    own = metrics_request(None, {"ph": [gauge("link_speed_mbps", [number(1000.0, TAKEN_S)])]})
    assert env.client.post("/v1/metrics", json=own, headers={
        "Authorization": f"Bearer {env.key}"}).json() == {}
    assert env.rows("SELECT COUNT(*) FROM samples") == [(1,)]
