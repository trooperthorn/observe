"""Logins, sessions, CSRF, admin role, and the basic auth boundary."""

from __future__ import annotations

import asyncio
import base64
import sqlite3

import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

PASSWORD = "correct horse battery"
BASIC = {"Authorization": "Basic " + base64.b64encode(b"ui:uipass").decode()}
ADMIN_ROUTES = [("GET", "/api/admin/users"), ("POST", "/api/admin/users")]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class Env:
    def __init__(self, tmp_path, basic: bool = True, **server) -> None:
        self.path = str(tmp_path / "w.db")
        self.store = Store(self.path)
        # Install commands carry a configured address, never the Host header. Pass
        # public_url=None to test an Observe that has none.
        srv = {"db_path": self.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
               "argon2_parallelism": 1, "public_url": "https://testserver", **server}
        if basic:
            srv.update(basic_auth_user="ui", basic_auth_password="uipass")
        self.cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}], server=srv)
        self.clock = Clock()
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        app = create_app(self.cfg, self.store, sched, alerter, auth_clock=self.clock)
        self.client = TestClient(app, base_url="https://testserver")

    def user(self, name: str, admin: bool = False, password: str = PASSWORD) -> None:
        asyncio.run(auth.create_user(self.store, self.cfg, name, password, admin,
                                     now=self.clock()))

    def login(self, name: str, password: str = PASSWORD):
        return self.client.post("/api/login", json={"username": name, "password": password})

    def csrf(self, resp) -> dict[str, str]:
        return {"X-CSRF-Token": resp.json()["csrf"]}

    def rows(self, sql: str, *args):
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


def test_good_login_sets_session_and_reads(env):
    env.user("alice")
    r = env.login("alice")
    assert r.status_code == 200
    assert r.json()["username"] == "alice" and r.json()["is_admin"] is False
    # A session alone opens the read API even though basic auth is configured.
    assert env.client.get("/api/monitors").status_code == 200
    assert env.client.get("/api/session").json()["username"] == "alice"
    assert env.rows("SELECT kind, actor FROM audit WHERE kind='login_ok'") == [("login_ok", "alice")]


def test_session_and_password_stored_hashed(env):
    env.user("alice")
    r = env.login("alice")
    token = env.client.cookies.get(auth.COOKIE)
    assert token
    (stored,) = env.rows("SELECT hash FROM users")[0]
    assert stored.startswith("$argon2id$") and PASSWORD not in stored
    dump = repr(env.rows("SELECT * FROM sessions"))
    assert token not in dump and r.json()["csrf"] not in dump


def test_bad_password_and_unknown_user_look_identical(env):
    env.user("alice")
    bad = env.login("alice", "wrong password here")
    unknown = env.login("nobody")
    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json()
    assert auth.COOKIE not in env.client.cookies
    audit = env.rows("SELECT actor, detail FROM audit WHERE kind='login_failed'")
    assert PASSWORD not in repr(audit) and "wrong password here" not in repr(audit)


def test_lockout_refuses_even_the_right_password(tmp_path):
    e = Env(tmp_path, login_max_failures=2, login_lock_s=60)
    e.user("alice")
    assert e.login("alice", "x" * 14).status_code == 401
    assert e.login("alice", "y" * 14).status_code == 401
    assert e.login("alice").status_code == 401  # locked
    e.clock.now += 61
    assert e.login("alice").status_code == 200
    e.client.close()
    e.store.close()


def test_disabled_user_cannot_log_in_or_keep_session(env):
    env.user("alice")
    assert env.login("alice").status_code == 200
    asyncio.run(env.store.execute("UPDATE users SET disabled=1"))
    assert env.client.get("/api/session").status_code == 401
    assert env.login("alice").status_code == 401


def test_idle_session_expires(env):
    env.user("alice")
    env.login("alice")
    env.clock.now += env.cfg.server.session_idle_s + 1
    assert env.client.get("/api/session").status_code == 401
    # Revoked for good once seen expired, even if the clock were wound back.
    env.clock.now -= env.cfg.server.session_idle_s
    assert env.client.get("/api/session").status_code == 401


def test_absolute_expiry_applies_despite_activity(env):
    env.user("alice")
    env.login("alice")
    step = env.cfg.server.session_idle_s - 1
    while env.clock.now < 1_000_000.0 + env.cfg.server.session_absolute_s:
        env.clock.now += step
        status = env.client.get("/api/session").status_code
        if status == 401:
            break
    assert status == 401
    assert env.clock.now - 1_000_000.0 <= env.cfg.server.session_absolute_s + step


def test_post_without_csrf_is_403_and_with_csrf_works(env):
    env.user("root", admin=True)
    r = env.login("root")
    body = {"username": "bob", "password": PASSWORD}
    assert env.client.post("/api/admin/users", json=body).status_code == 403
    assert env.client.post("/api/admin/users", json=body,
                           headers={"X-CSRF-Token": "0" * 64}).status_code == 403
    assert env.rows("SELECT COUNT(*) FROM users WHERE username='bob'") == [(0,)]
    ok = env.client.post("/api/admin/users", json=body, headers=env.csrf(r))
    assert ok.status_code == 200
    assert env.rows("SELECT kind, actor FROM audit WHERE kind='user_created'") == [
        ("user_created", "root")]
    # Logout is state changing too.
    assert env.client.post("/api/logout").status_code == 403
    assert env.client.post("/api/logout", headers=env.csrf(r)).status_code == 200
    assert env.client.get("/api/session").status_code == 401


def test_csrf_token_of_another_session_is_rejected(env):
    env.user("root", admin=True)
    first = env.login("root")
    with TestClient(env.client.app, base_url="https://testserver") as other:
        r2 = other.post("/api/login", json={"username": "root", "password": PASSWORD})
        assert first.json()["csrf"] != r2.json()["csrf"]
        body = {"username": "bob", "password": PASSWORD}
        assert other.post("/api/admin/users", json=body,
                          headers=env.csrf(first)).status_code == 403


def test_non_admin_gets_403_on_admin_routes(env):
    env.user("alice")
    r = env.login("alice")
    assert env.client.get("/api/admin/users").status_code == 403
    assert env.client.post("/api/admin/users", headers=env.csrf(r),
                           json={"username": "x", "password": PASSWORD}).status_code == 403
    assert env.rows("SELECT COUNT(*) FROM users") == [(1,)]


def test_admin_can_list_users(env):
    env.user("root", admin=True)
    env.login("root")
    rows = env.client.get("/api/admin/users").json()
    assert [(u["username"], u["is_admin"]) for u in rows] == [("root", True)]
    assert "hash" not in rows[0]


def test_cookie_flags(env):
    env.user("alice")
    r = env.login("alice")
    cookie = r.headers["set-cookie"].lower()
    assert f"{auth.COOKIE}=" in cookie
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert "path=/" in cookie and "max-age=" in cookie


def test_logout_clears_cookie_with_same_flags(env):
    env.user("alice")
    r = env.login("alice")
    out = env.client.post("/api/logout", headers=env.csrf(r)).headers["set-cookie"].lower()
    assert "max-age=0" in out and "httponly" in out and "secure" in out and "samesite=strict" in out


def test_basic_auth_reads_but_never_reaches_admin_routes(env):
    env.user("root", admin=True)
    assert env.client.get("/api/monitors", headers=BASIC).status_code == 200
    assert env.client.get("/metrics", headers=BASIC).status_code == 200
    assert env.client.get("/api/monitors").status_code == 401
    for method, path in ADMIN_ROUTES:
        for headers in (BASIC, {**BASIC, "X-CSRF-Token": "0" * 64}):
            r = env.client.request(method, path, headers=headers,
                                   json={"username": "x", "password": PASSWORD})
            assert r.status_code in (401, 403), (method, path)
            assert "www-authenticate" not in r.headers  # basic is not offered here
    for path in ("/api/session",):
        assert env.client.get(path, headers=BASIC).status_code == 401
    assert env.client.post("/api/logout", headers=BASIC).status_code in (401, 403)
    assert env.rows("SELECT COUNT(*) FROM users") == [(1,)]


def test_admin_user_with_basic_credentials_header_still_needs_session(env):
    env.user("ui", admin=True, password="uipass-but-longer")
    bad = {"Authorization": "Basic " + base64.b64encode(b"ui:uipass-but-longer").decode()}
    assert env.client.get("/api/admin/users", headers=bad).status_code == 401


def test_without_basic_auth_configured_reads_stay_open(tmp_path):
    e = Env(tmp_path, basic=False)
    assert e.client.get("/api/monitors").status_code == 200
    assert e.client.get("/api/admin/users").status_code == 401
    e.client.close()
    e.store.close()


def test_login_rate_limit(tmp_path):
    e = Env(tmp_path, login_rate_per_minute=3, login_max_failures=100)
    e.user("alice")
    codes = [e.login("alice", "z" * 14).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    assert e.login("alice", "z" * 14).headers["retry-after"] == "60"
    e.client.close()
    e.store.close()


def test_password_policy_and_duplicate_user(env):
    with pytest.raises(auth.AuthError):
        asyncio.run(auth.create_user(env.store, env.cfg, "bob", "short"))
    with pytest.raises(auth.AuthError):
        asyncio.run(auth.create_user(env.store, env.cfg, "bad name", PASSWORD))
    env.user("bob")
    with pytest.raises(auth.AuthError):
        env.user("bob")


def test_login_rejects_malformed_bodies(env):
    assert env.client.post("/api/login", content=b"not json").status_code == 422
    assert env.client.post("/api/login", json={"username": 1, "password": 2}).status_code == 422
    assert env.client.post("/api/login", json={"username": "a" * 5000,
                                               "password": "p"}).status_code == 413
    assert env.client.get("/login").status_code == 200


def test_create_admin_cli(tmp_path, monkeypatch):
    from observe.__main__ import main

    cfg = tmp_path / "c.yaml"
    db = tmp_path / "cli.db"
    cfg.write_text(
        "server:\n"
        f"  db_path: {db.as_posix()}\n"
        "  argon2_time_cost: 1\n  argon2_memory_kib: 8\n  argon2_parallelism: 1\n"
        "monitors:\n  - {name: p, type: ping, host: 127.0.0.1}\n", encoding="utf-8")
    monkeypatch.setenv("OBSERVE_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr("sys.argv", ["observe", "--config", str(cfg), "--create-admin", "root"])
    assert main() == 0
    assert main() == 2  # the name is taken
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT username, is_admin FROM users").fetchall() == [("root", 1)]
        assert conn.execute("SELECT kind, actor FROM audit").fetchall() == [
            ("user_created", "cli"), ("user_create_failed", "cli")]
    finally:
        conn.close()
