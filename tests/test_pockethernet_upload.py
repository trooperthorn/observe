"""The Pockethernet upload endpoint: auth, caps, gzip, revisions, clocks, retention and audit."""

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

from watchpost.alerts import Alerter
from watchpost.ingest.keys import create_key
from watchpost.plugins import (GROUP, KeyScope, PluginBase, PluginError, PluginRouter,
                               load_plugins)
from watchpost.scheduler import Scheduler
from watchpost.store import Store
from watchpost.web import create_app
from watchpost_pockethernet import schema
from watchpost_pockethernet.keys import create_field_key
from watchpost_pockethernet.reports import prune_evidence
from watchpost_pockethernet.upload import MAX_RATIO, correct_clock, inflate

from .conftest import make_config
from .test_auth import Clock

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "pockethernet" / "report_v1.json")
                     .read_text(encoding="utf-8"))
TAKEN_S = FIXTURE["taken_at_ms"] / 1000
URL = "/api/v1/field-reports"


def run(coro):
    return asyncio.run(coro)


def dump(body: dict[str, Any]) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


class Env:
    def __init__(self, tmp_path, rate: int = 300, settings: dict[str, Any] | None = None) -> None:
        self.path = str(tmp_path / "w.db")
        self.cfg = make_config(
            [{"name": "p", "type": "ping", "host": "127.0.0.1"}],
            plugins=["pockethernet"],
            plugin_settings={"pockethernet": settings or {}},
            server={"db_path": self.path, "plugin_rate_per_minute": rate})
        loaded = load_plugins(self.cfg, lambda: [EntryPoint(
            "pockethernet", "watchpost_pockethernet:plugin", GROUP)])
        self.store = Store(self.path, loaded)
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        self.clock = Clock()
        self.clock.now = TAKEN_S + 100  # the phone's report was taken 100 s ago
        self.client = TestClient(
            create_app(self.cfg, self.store, sched, alerter, plugins=loaded,
                       auth_clock=self.clock), base_url="https://testserver")
        self.key, self.info = run(create_field_key(self.store, "sean-pixel"))

    def post(self, body: bytes | dict[str, Any], key: str | None = "default",
             headers: dict[str, str] | None = None):
        raw = dump(body) if isinstance(body, dict) else body
        token = self.key if key == "default" else key
        h = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.post(URL, content=raw, headers={**h, **(headers or {})})

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


def test_ping_checks_the_key_and_reports_the_server_clock(env):
    r = env.client.get(URL + "/ping", headers={"Authorization": f"Bearer {env.key}"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["device"] == "sean-pixel"
    assert body["server_time_ms"] == int(env.clock.now * 1000)
    assert body["max_report_bytes"] == schema.MAX_REPORT_BYTES
    assert env.client.get(URL + "/ping").status_code == 401


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


def test_gzip_body_is_accepted_and_stored_inflated(env):
    raw = dump(FIXTURE)
    r = env.post(gzip.compress(raw), headers={"Content-Encoding": "gzip"})
    assert r.status_code == 200 and r.json()["result"] == "accepted"
    (row,) = env.rows("SELECT body FROM field_reports")
    assert bytes(row[0]) == raw


def test_gzip_bomb_is_refused_on_size(env):
    bomb = gzip.compress(b"0" * (4 * 1024 * 1024))
    assert len(bomb) < schema.MAX_REPORT_BYTES  # small on the wire
    r = env.post(bomb, headers={"Content-Encoding": "gzip"})
    assert r.status_code == 413 and "inflated" in r.json()["detail"]
    assert env.rows("SELECT * FROM field_reports") == []


def test_gzip_bomb_is_refused_on_ratio_under_the_size_cap(env):
    inside = gzip.compress(b"0" * 200_000)  # 200 KB inflated, under the 256 KiB cap
    assert 200_000 > MAX_RATIO * len(inside)
    r = env.post(inside, headers={"Content-Encoding": "gzip"})
    assert r.status_code == 413 and "ratio" in r.json()["detail"]


def test_inflate_never_allocates_beyond_the_cap_and_refuses_bad_framing():
    from watchpost_pockethernet.schema import ReportError
    with pytest.raises(ReportError) as err:
        inflate(gzip.compress(b"0" * (50 * 1024 * 1024)))
    assert err.value.status == 413
    for bad in (zlib.compress(b"{}"), b"not gzip", gzip.compress(b"{}")[:-4],
                gzip.compress(b"{}") + gzip.compress(b"{}")):
        with pytest.raises(ReportError) as err:
            inflate(bad)
        assert err.value.status == 400


def test_unknown_encoding_and_non_gzip_body_are_refused(env):
    assert env.post(dump(FIXTURE), headers={"Content-Encoding": "br"}).status_code == 415
    r = env.post(zlib.compress(dump(FIXTURE)), headers={"Content-Encoding": "gzip"})
    assert r.status_code == 400
    assert env.post(dump(FIXTURE), headers={"Content-Encoding": "identity"}).status_code == 200


def test_missing_wrong_revoked_and_host_keys_are_401(env):
    assert env.post(FIXTURE, key=None).status_code == 401
    assert env.post(FIXTURE, key="wpf_000000000000_nothing").status_code == 401
    host_key, _ = run(create_key(env.store, "somehost"))
    r = env.post(FIXTURE, key=host_key)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    from watchpost.ingest.keys import revoke_key
    run(revoke_key(env.store, env.info.prefix))
    assert env.post(FIXTURE).status_code == 401
    assert env.rows("SELECT * FROM field_reports") == []


def test_field_key_does_not_open_host_ingest(env):
    r = env.client.post("/api/ingest", json={}, headers={"Authorization": f"Bearer {env.key}"})
    assert r.status_code == 401


def test_oversize_body_is_413_before_parsing(env):
    r = env.post(b"x" * (schema.MAX_REPORT_BYTES + 1))
    assert r.status_code == 413
    pad = env.report(notes="n" * 4096, warnings=["w" * 1000] * 32,
                     steps=[{"step": "s", "label": "l", "status": "ok",
                             "fields": [{"name": "n", "value": "v" * 1000}] * 64}] * 8)
    assert env.post(pad).status_code == 413
    assert env.rows("SELECT * FROM field_reports") == []


def test_invalid_reports_use_the_schema_status(env):
    assert env.post(b"{not json").status_code == 422
    assert env.post(env.report(surprise=1)).status_code == 422
    assert env.post(env.report(transcript="secret")).status_code == 422
    assert env.post(b"[" * 40 + b"]" * 40).status_code == 400
    assert env.rows("SELECT * FROM field_reports") == []


def test_rate_limit_is_429_and_uploads_stop(tmp_path):
    e = Env(tmp_path, rate=2)
    try:
        assert e.post(FIXTURE).status_code == 200
        assert e.post(FIXTURE).status_code == 200
        r = e.post(FIXTURE)
        assert r.status_code == 429 and r.headers["retry-after"] == "60"
        denied = e.rows("SELECT status, detail FROM audit WHERE kind='plugin_denied'")
        assert denied and denied[0][0] == 429
    finally:
        e.close()


def test_bad_key_flood_is_limited_but_never_blocks_a_valid_key(tmp_path):
    e = Env(tmp_path, rate=3)
    try:
        codes = [e.post(FIXTURE, key="wpf_bad").status_code for _ in range(6)]
        assert codes == [401, 401, 401, 429, 429, 429]
        # The same peer's valid key is counted on its own and is still accepted.
        assert [e.post(FIXTURE).status_code for _ in range(3)] == [200, 200, 200]
        assert e.post(FIXTURE).status_code == 429  # the valid key's own limit still holds
        # A flood from another peer does not touch the first peer's counters either way.
        other = TestClient(e.client.app, base_url="https://testserver",
                           client=("203.0.113.9", 5000))
        try:
            for _ in range(5):
                other.post(URL, content=dump(FIXTURE), headers={"Authorization": "Bearer wpf_bad"})
            second, _info = run(create_field_key(e.store, "other-phone"))
            r = other.post(URL, content=dump(e.report(report_id="other-1")),
                           headers={"Authorization": f"Bearer {second}"})
            assert r.status_code == 200
        finally:
            other.close()
    finally:
        e.close()


def test_valid_key_limit_is_per_key_and_per_peer(tmp_path):
    e = Env(tmp_path, rate=2)
    try:
        assert [e.post(FIXTURE).status_code for _ in range(3)] == [200, 200, 429]
        other = TestClient(e.client.app, base_url="https://testserver",
                           client=("203.0.113.10", 5000))
        try:
            # The first key stays limited from a new peer; a new key from a new peer is fine.
            r = other.post(URL, content=dump(FIXTURE),
                           headers={"Authorization": f"Bearer {e.key}"})
            assert r.status_code == 429
            second, _info = run(create_field_key(e.store, "other-phone"))
            r = other.post(URL, content=dump(e.report(report_id="other-2")),
                           headers={"Authorization": f"Bearer {second}"})
            assert r.status_code == 200
        finally:
            other.close()
    finally:
        e.close()


def test_audit_rows_for_accepted_refused_and_denied_uploads(env):
    env.post(FIXTURE)
    env.post(env.report(revision=2), headers={"X-Report-Sent-Ms": "1"})
    env.post(b"{bad")
    env.post(FIXTURE, key=None)
    env.client.get(URL + "/ping", headers={"Authorization": f"Bearer {env.key}"})  # not audited
    rows = env.rows("SELECT actor, method, path, status, remote, detail FROM audit "
                    "WHERE kind='plugin_request' ORDER BY id")
    assert [r[3] for r in rows] == [200, 200, 422]
    for actor, method, path, _, remote, _ in rows:
        assert actor == env.info.prefix and method == "POST" and path == URL
        assert remote
    first, second, third = (json.loads(r[5]) for r in rows)
    assert first.pop("derived") == {"properties_added": 20, "properties_verified": 0,
                                    "jack_linked": True}
    assert first == {"plugin": "pockethernet", "device": "sean-pixel", "result": "accepted",
                     "report_id": FIXTURE["report_id"], "revision": 1, "stored_revision": 1,
                     "clock_corrected": False, "bytes": len(dump(FIXTURE))}
    assert second["result"] == "replaced" and second["clock_corrected"] is True
    assert third["reason"] and third["device"] == "sean-pixel"
    denied = env.rows("SELECT actor, status, path, detail FROM audit WHERE kind='plugin_denied'")
    assert denied == [("", 401, URL, json.dumps({"plugin": "pockethernet"}, sort_keys=True))]
    # No key and no body content is ever written.
    text = json.dumps(env.rows("SELECT * FROM audit"))
    assert env.key not in text and FIXTURE["notes"] not in text


def test_rejected_upload_audit_has_no_report_content(env):
    env.post(env.report(transcript="TOPSECRETVALUE"))
    text = json.dumps(env.rows("SELECT detail FROM audit"))
    assert "TOPSECRETVALUE" not in text


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
        from watchpost_pockethernet import plugin
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
                "pockethernet", "watchpost_pockethernet:plugin", GROUP)])


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


def test_unlisted_plugin_has_no_upload_route(tmp_path):
    path = str(tmp_path / "w.db")
    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                      server={"db_path": path})
    store = Store(path)
    alerter = Alerter(cfg)
    app = create_app(cfg, store, Scheduler(cfg, store, alerter), alerter)
    with TestClient(app, base_url="https://testserver") as client:
        assert client.post(URL, content=b"{}").status_code == 404
    store.close()
