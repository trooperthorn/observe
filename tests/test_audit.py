"""The audit log: one row per audited action, no secrets, admin-only read, failures recorded."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from observe import audit
from observe.__main__ import main
from observe.ingest.keys import create_key

from .otlp_build import fixture, post_batch
from .test_auth import BASIC, PASSWORD, Env


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def kinds(env, kind: str) -> list[tuple]:
    return env.rows("SELECT actor, method, path, status FROM audit WHERE kind=?", kind)


def test_login_logout_and_failed_login_each_write_one_row(env):
    env.user("alice")
    assert env.login("alice", "wrong password here").status_code == 401
    r = env.login("alice")
    assert r.status_code == 200
    assert env.client.post("/api/logout", headers=env.csrf(r)).status_code == 200
    assert kinds(env, "login_failed") == [("alice", "POST", "/api/login", 401)]
    assert kinds(env, "login_ok") == [("alice", "POST", "/api/login", 200)]
    assert kinds(env, "logout") == [("alice", "POST", "/api/logout", 200)]
    assert env.rows("SELECT COUNT(*) FROM audit") == [(3,)]


def test_user_creation_writes_one_row_and_a_refusal_writes_a_failure_row(env):
    env.user("root", admin=True)
    r = env.login("root")
    ok = env.client.post("/api/admin/users", json={"username": "bob", "password": PASSWORD},
                         headers=env.csrf(r))
    assert ok.status_code == 200
    assert kinds(env, "user_created") == [("root", "POST", "/api/admin/users", 200)]
    short = env.client.post("/api/admin/users", json={"username": "carol", "password": "tiny"},
                            headers=env.csrf(r))
    assert short.status_code == 422
    assert kinds(env, "user_create_failed") == [("root", "POST", "/api/admin/users", 422)]
    detail = json.loads(env.rows("SELECT detail FROM audit WHERE kind='user_create_failed'")[0][0])
    assert detail["username"] == "carol" and "password" in detail["reason"]
    assert env.rows("SELECT COUNT(*) FROM audit WHERE kind='user_created'") == [(1,)]


def test_user_creation_that_errors_partway_is_recorded(env, monkeypatch):
    env.user("root", admin=True)
    r = env.login("root")

    async def boom(*a, **kw):
        raise RuntimeError(f"disk full while saving {PASSWORD}")

    monkeypatch.setattr("observe.auth.create_user", boom)
    quiet = TestClient(env.client.app, base_url="https://testserver",
                       raise_server_exceptions=False)
    quiet.cookies.update(env.client.cookies)
    res = quiet.post("/api/admin/users", json={"username": "dave", "password": PASSWORD},
                     headers=env.csrf(r))
    assert res.status_code == 500
    assert kinds(env, "user_create_error") == [("root", "POST", "/api/admin/users", 500)]
    row = env.rows("SELECT detail FROM audit WHERE kind='user_create_error'")[0][0]
    assert json.loads(row)["error"] == "RuntimeError" and PASSWORD not in row
    assert env.rows("SELECT COUNT(*) FROM audit WHERE kind='user_created'") == [(0,)]


def test_login_that_fails_after_the_password_check_is_recorded(env, monkeypatch):
    env.user("alice")

    async def boom(*a, **kw):
        raise RuntimeError("no session")

    monkeypatch.setattr("observe.auth.create_session", boom)
    quiet = TestClient(env.client.app, base_url="https://testserver",
                       raise_server_exceptions=False)
    assert quiet.post("/api/login", json={"username": "alice",
                                          "password": PASSWORD}).status_code == 500
    assert kinds(env, "login_error") == [("alice", "POST", "/api/login", 500)]
    assert env.rows("SELECT COUNT(*) FROM audit WHERE kind='login_ok'") == [(0,)]


def test_ingest_that_fails_while_storing_is_recorded(tmp_path, monkeypatch):
    e = Env(tmp_path)
    try:
        key, info = asyncio.run(create_key(e.store, "nas01"))

        def boom(*a, **kw):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(e.store, "ingest_batch", boom)
        quiet = TestClient(e.client.app, raise_server_exceptions=False)
        res = post_batch(quiet, fixture("batch_minimal"), key)
        assert res.status_code == 500
        rows = e.rows("SELECT actor, status, detail FROM audit WHERE kind='ingest_failed'")
        assert len(rows) == 1 and rows[0][0] == info.prefix and rows[0][1] == 500
        assert json.loads(rows[0][2]) == {"error": "RuntimeError", "host": "nas01"}
        assert key.split("_", 2)[2] not in json.dumps(e.rows("SELECT * FROM audit"))
    finally:
        e.client.close()
        e.store.close()


def _cli(tmp_path, monkeypatch, *argv: str) -> tuple[int, str]:
    cfg = tmp_path / "c.yaml"
    db = tmp_path / "cli.db"
    cfg.write_text(
        "server:\n"
        f"  db_path: {db.as_posix()}\n"
        "  argon2_time_cost: 1\n  argon2_memory_kib: 8\n  argon2_parallelism: 1\n"
        "monitors:\n  - {name: p, type: ping, host: 127.0.0.1}\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["observe", "--config", str(cfg), *argv])
    return main(), str(db)


def test_key_creation_and_revocation_each_write_one_row(tmp_path, monkeypatch, capsys):
    code, db = _cli(tmp_path, monkeypatch, "--ingest-key-create", "nas01")
    assert code == 0
    plaintext = capsys.readouterr().out.strip()
    prefix = plaintext.split("_")[1]
    assert _cli(tmp_path, monkeypatch, "--ingest-key-revoke", prefix)[0] == 0
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT kind, actor, detail FROM audit ORDER BY id").fetchall()
        assert [(k, a) for k, a, _ in rows] == [("key_created", "cli"), ("key_revoked", "cli")]
        assert json.loads(rows[0][2]) == {"host": "nas01", "key_id": prefix,
                                       "scope": "wpi"}
        assert plaintext.split("_", 2)[2] not in json.dumps(rows)
    finally:
        conn.close()


def test_failed_key_actions_are_recorded(tmp_path, monkeypatch):
    assert _cli(tmp_path, monkeypatch, "--ingest-key-create", "bad host")[0] == 2
    assert _cli(tmp_path, monkeypatch, "--ingest-key-revoke", "nosuchid")[0] == 2
    conn = sqlite3.connect(tmp_path / "cli.db")
    try:
        assert conn.execute("SELECT kind FROM audit ORDER BY id").fetchall() == [
            ("key_create_failed",), ("key_revoke_failed",)]
    finally:
        conn.close()


def test_no_secret_reaches_the_log(env):
    env.user("root", admin=True)
    bad = env.login("root", "not the password")
    r = env.login("root")
    token = env.client.cookies.get("observe_session")
    key, _ = asyncio.run(create_key(env.store, "nas01"))
    env.client.post("/api/admin/users", json={"username": "bob", "password": "Sup3r secret pw!"},
                    headers=env.csrf(r))
    env.client.post("/api/admin/users", json={"username": "eve", "password": "short"},
                    headers=env.csrf(r))
    post_batch(env.client, fixture("batch_minimal"), key + "x")
    assert bad.status_code == 401
    dump = json.dumps(env.rows("SELECT * FROM audit"))
    shown = json.dumps(env.client.get("/api/v2/audit").json())
    for secret in (PASSWORD, "not the password", "Sup3r secret pw!", token,
                   r.json()["csrf"], key, key.split("_", 2)[2]):
        assert secret not in dump and secret not in shown


@pytest.mark.parametrize("kind, status, detail, want", [
    # The rows of the /audit review: no HTTP status is not a failure, and the detail decides.
    ("control_requested", 0, {"host": "nas01"}, "ok"),
    ("infra_switch_linked", 0, {"basis": "manual"}, "ok"),
    ("plugin_request", 200, {"outcome": "refused"}, "refused"),
    ("plugin_request", 200, {"outcome": "failed"}, "failed"),
    ("plugin_request", 200, {"outcome": "done"}, "ok"),
    ("enrol_install_problem", 200, {"status": "failed"}, "failed"),
    ("enrol_install_problem", 200, {"status": "refused"}, "refused"),
    ("plugin_request", 200, {"result": "Error"}, "failed"),
    ("plugin_request", 200, {"result": "rejected"}, "refused"),
    # Then the kind, the HTTP status, and a failure kind last.
    ("api_denied", 0, {}, "refused"),
    ("control_request_refused", 404, {}, "refused"),
    ("infra_depends_rejected", 0, {}, "refused"),
    ("login_failed", 401, {}, "refused"),
    ("key_create_failed", 400, {}, "refused"),
    ("plugin_failed", 500, {}, "failed"),
    ("infra_switch_link_failed", 0, {"reason": "unknown switch"}, "failed"),
    ("user_create_error", 0, {}, "failed"),
    ("ingest_failed", 200, {}, "failed"),
    ("plugin_request", 503, {}, "failed"),
    ("enrol_fetched", 200, {"host": "newbox", "status": 3}, "ok"),
    ("probe", 0, None, "ok"),
])
def test_outcome_is_derived_from_the_detail_the_kind_and_the_status(kind, status, detail, want):
    assert audit.outcome(kind, status, detail) == want


def test_the_audit_api_filters_on_the_outcome_and_names_the_key_host(env):
    env.user("root", admin=True)
    env.login("root")
    plaintext, info = asyncio.run(create_key(env.store, "nas01"))
    asyncio.run(audit.record(env.store, "enrol_install_problem", actor="nas01", status=200,
                             detail={"host": "nas01", "status": "failed"}))
    asyncio.run(audit.record(env.store, "control_pull", actor=info.prefix, status=200))
    asyncio.run(audit.record(env.store, "enrol_fetched", status=200, remote="10.0.0.7",
                             detail={"host": "newbox"}))
    failed = env.client.get("/api/v2/audit", params={"outcome": "failed"}).json()["items"]
    assert [r["kind"] for r in failed] == ["enrol_install_problem"]
    ok = {r["kind"]: r for r in env.client.get("/api/v2/audit",
                                                 params={"outcome": "ok"}).json()["items"]}
    assert ok["control_pull"]["actor_host"] == "nas01" and plaintext not in json.dumps(ok)
    assert ok["enrol_fetched"]["actor"] == "" and ok["enrol_fetched"]["actor_host"] is None
    assert ok["login_ok"]["actor_host"] is None and "enrol_install_problem" not in ok


def test_the_outcome_filter_fills_every_page_and_carries_on_past_the_scan_cap(env, monkeypatch):
    from observe.api import admin
    env.user("root", admin=True)
    env.login("root")
    for n in range(40):
        status = 403 if n % 5 == 0 else 200
        asyncio.run(audit.record(env.store, "probe", actor="x", status=status, detail={"n": n}))
    monkeypatch.setattr(admin, "SCAN_BATCH", 7)
    seen, cursor, pages = [], None, 0
    while True:
        params = {"kind": "probe", "outcome": "refused", "limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = env.client.get("/api/v2/audit", params=params).json()
        pages += 1
        seen += [r["detail"]["n"] for r in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
        assert len(page["items"]) == 3
    assert seen == [35, 30, 25, 20, 15, 10, 5, 0] and pages == 3
    # A scan cut short hands back a cursor from the last row it looked at, even with no item.
    # (Two rows a batch and a cap of three: rows 39 to 36, then 35 to 32. Limit 2 is a new query,
    # so no cached answer from above is served.)
    monkeypatch.setattr(admin, "SCAN_BATCH", 2)
    monkeypatch.setattr(admin, "MAX_SCAN", 3)
    page = env.client.get("/api/v2/audit", params={"kind": "probe", "outcome": "refused",
                                                   "limit": 2}).json()
    assert page["items"] == [] and page["next_cursor"]
    nxt = env.client.get("/api/v2/audit", params={"kind": "probe", "outcome": "refused",
                                                  "limit": 2, "cursor": page["next_cursor"]})
    assert [r["detail"]["n"] for r in nxt.json()["items"]] == [35]


def test_record_redacts_secret_named_fields_and_sanitizes_the_path(env):
    asyncio.run(audit.record(env.store, "probe", actor="x", method="GET",
                             path="/a\nforged line\x00/" + "z/" * 200,
                             detail={"password": "p1", "Session_Token": "t1", "ok": "fine",
                                     "ingest_key": "k1"}))
    path, detail = env.rows("SELECT path, detail FROM audit WHERE kind='probe'")[0]
    assert "\n" not in path and "\x00" not in path and len(path) == audit.AUDIT_PATH_MAX
    assert path.startswith("/a?forged line?/")
    assert json.loads(detail) == {"password": audit.REDACTED, "Session_Token": audit.REDACTED,
                                  "ok": "fine", "ingest_key": audit.REDACTED}
    assert audit.sanitize_audit_path("/ok/path") == "/ok/path"


def test_admin_reads_the_log_newest_first_with_filters(env):
    env.user("root", admin=True)
    env.login("root")
    for _ in range(3):
        asyncio.run(audit.record(env.store, "probe", actor="x"))
    rows = env.client.get("/api/v2/audit").json()["items"]
    assert rows[0]["id"] > rows[-1]["id"] and rows[-1]["kind"] == "login_ok"
    assert set(rows[0]) == {"id", "ts", "actor", "actor_host", "kind", "method", "path",
                            "status", "outcome", "remote", "detail"}
    assert rows[0]["ts"].endswith("Z")
    first = env.client.get("/api/v2/audit", params={"kind": "probe", "limit": 2}).json()
    assert [r["kind"] for r in first["items"]] == ["probe", "probe"] and first["next_cursor"]
    older = env.client.get("/api/v2/audit", params={"kind": "probe", "limit": 2,
                                                    "cursor": first["next_cursor"]}).json()
    assert len(older["items"]) == 1 and older["items"][0]["id"] < first["items"][-1]["id"]
    assert older["next_cursor"] is None
    assert env.client.get("/api/v2/audit", params={"limit": 0}).status_code == 400
    assert env.client.get("/api/v2/audit", params={"limit": 100000}).status_code == 400
    assert env.client.get("/api/v2/audit", params={"actor": "x"}).json()["items"]


def test_non_admin_anonymous_and_basic_auth_are_denied(env):
    env.user("alice")
    assert env.client.get("/api/v2/audit").status_code == 401
    assert env.client.get("/api/v2/audit", headers=BASIC).status_code == 401
    env.login("alice")
    denied = env.client.get("/api/v2/audit")
    assert denied.status_code == 403 and "kind" not in denied.text
    assert env.client.get("/api/v2/audit", headers=BASIC).status_code == 403



def test_sanitizer_redacts_keys_and_session_tokens_anywhere(env):
    token = "A" * 43
    out = audit.sanitize_audit_path(f"/x/wpi_abc123_{token}/y?t={token}&k=wpi_zz")
    assert token not in out and "wpi_" not in out
    assert audit.sanitize_audit_path("/api/hosts/nas01") == "/api/hosts/nas01"
