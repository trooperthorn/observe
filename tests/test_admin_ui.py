"""The admin screen routes: access, CSRF, key and user management, and audit rows."""

from __future__ import annotations

import asyncio

import pytest

from watchpost import audit
from watchpost.ingest.keys import verify_key

from .test_auth import BASIC, PASSWORD, Env


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def admin_login(env, name="root"):
    env.user(name, admin=True)
    r = env.login(name)
    assert r.status_code == 200
    return env.csrf(r)


def audit_rows(env, kind):
    return env.rows("SELECT actor, status, path, detail FROM audit WHERE kind=? ORDER BY id",
                    kind)


def user_id(env, name):
    return env.rows("SELECT id FROM users WHERE username=?", name)[0][0]


def test_page_is_static_and_holds_no_data(env):
    r = env.client.get("/admin")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert env.client.get("/static/admin.js").status_code == 200


def test_admin_can_list_and_non_admin_is_denied(env):
    hdr = admin_login(env)
    assert env.client.get("/api/admin/keys").json() == []
    assert env.client.post("/api/admin/keys", json={"host": "h1"}, headers=hdr).status_code == 200

    env.client.cookies.clear()
    env.user("bob")
    bob = env.csrf(env.login("bob"))
    assert env.client.get("/api/admin/keys").status_code == 403
    assert env.client.post("/api/admin/keys", json={"host": "h2"}, headers=bob).status_code == 403
    assert env.client.post("/api/admin/keys/abc/revoke", headers=bob).status_code == 403
    assert env.client.post("/api/admin/users/1/disabled", json={"value": True},
                           headers=bob).status_code == 403
    assert env.client.post("/api/admin/users/1/admin", json={"value": False},
                           headers=bob).status_code == 403
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(1,)]
    assert env.rows("SELECT disabled, is_admin FROM users WHERE username='root'") == [(0, 1)]


def test_no_session_and_basic_auth_are_refused(env):
    for method, path in [("GET", "/api/admin/keys"), ("POST", "/api/admin/keys"),
                         ("POST", "/api/admin/keys/abc/revoke"),
                         ("POST", "/api/admin/users/1/disabled"),
                         ("POST", "/api/admin/users/1/admin")]:
        assert env.client.request(method, path).status_code == 401
        assert env.client.request(method, path, headers=BASIC).status_code == 401


def test_every_admin_post_needs_csrf(env):
    admin_login(env)
    env.user("bob")
    for path, body in [("/api/admin/keys", {"host": "h1"}),
                       ("/api/admin/keys/abc/revoke", {}),
                       ("/api/admin/users", {"username": "x", "password": PASSWORD}),
                       ("/api/admin/users/2/disabled", {"value": True}),
                       ("/api/admin/users/2/admin", {"value": True})]:
        assert env.client.post(path, json=body).status_code == 403
        assert env.client.post(path, json=body,
                               headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]
    assert env.rows("SELECT disabled, is_admin FROM users WHERE username='bob'") == [(0, 0)]


def test_key_create_shows_secret_once_and_revoke(env):
    hdr = admin_login(env)
    r = env.client.post("/api/admin/keys", json={"host": "nas1"}, headers=hdr)
    assert r.status_code == 200
    made = r.json()
    assert made["key"].startswith("wpi_") and made["host"] == "nas1"
    assert r.headers["cache-control"] == "no-store"

    listing = env.client.get("/api/admin/keys")
    assert [k["id"] for k in listing.json()] == [made["id"]]
    assert listing.json()[0]["active"] is True
    assert made["key"] not in listing.text
    assert asyncio.run(verify_key(env.store, made["key"], "nas1")) is True

    rv = env.client.post(f"/api/admin/keys/{made['id']}/revoke", headers=hdr)
    assert rv.status_code == 200
    assert env.client.get("/api/admin/keys").json()[0]["active"] is False
    assert asyncio.run(verify_key(env.store, made["key"], "nas1")) is False
    again = env.client.post(f"/api/admin/keys/{made['id']}/revoke", headers=hdr)
    assert again.status_code == 404

    created = audit_rows(env, "key_created")
    assert len(created) == 1 and created[0][0] == "root" and created[0][1] == 200
    assert made["id"] in created[0][3] and made["host"] in created[0][3]
    assert [r[1] for r in audit_rows(env, "key_revoked")] == [200]
    assert [r[1] for r in audit_rows(env, "key_revoke_failed")] == [404]
    everything = " ".join(str(x) for row in env.rows("SELECT * FROM audit") for x in row)
    assert made["key"] not in everything and made["key"].split("_", 2)[2] not in everything


def test_bad_key_host_is_refused_and_audited(env):
    hdr = admin_login(env)
    assert env.client.post("/api/admin/keys", json={"host": "has space"},
                           headers=hdr).status_code == 422
    assert env.client.post("/api/admin/keys", json={}, headers=hdr).status_code == 422
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]
    assert [r[1] for r in audit_rows(env, "key_create_failed")] == [422]


def test_user_disable_enable_and_roles_are_audited(env):
    hdr = admin_login(env)
    env.user("bob")
    bob = user_id(env, "bob")
    for path, value in [("disabled", True), ("disabled", False), ("admin", True),
                        ("admin", False)]:
        assert env.client.post(f"/api/admin/users/{bob}/{path}", json={"value": value},
                               headers=hdr).status_code == 200
    assert env.client.post("/api/admin/users/999/disabled", json={"value": True},
                           headers=hdr).status_code == 404
    assert env.client.post(f"/api/admin/users/{bob}/disabled", json={"value": "yes"},
                           headers=hdr).status_code == 422
    for kind in ("user_disabled", "user_enabled", "user_promoted", "user_demoted"):
        rows = audit_rows(env, kind)
        assert len(rows) == 1 and rows[0][0] == "root", kind
    assert [r[1] for r in audit_rows(env, "user_change_failed")] == [404]


def test_disabled_user_session_stops_working(env):
    hdr = admin_login(env)
    env.user("bob")
    bob = user_id(env, "bob")
    other = type(env.client)(env.client.app, base_url="https://testserver")
    try:
        r = other.post("/api/login", json={"username": "bob", "password": PASSWORD})
        assert r.status_code == 200
        assert other.get("/api/session").status_code == 200
        env.client.post(f"/api/admin/users/{bob}/disabled", json={"value": True}, headers=hdr)
        assert other.get("/api/session").status_code == 401
    finally:
        other.close()


def test_last_active_admin_cannot_be_removed(env):
    hdr = admin_login(env)
    root = user_id(env, "root")
    assert env.client.post(f"/api/admin/users/{root}/disabled", json={"value": True},
                           headers=hdr).status_code == 409
    assert env.client.post(f"/api/admin/users/{root}/admin", json={"value": False},
                           headers=hdr).status_code == 409
    assert env.rows("SELECT disabled, is_admin FROM users WHERE id=?", root) == [(0, 1)]
    assert len(audit_rows(env, "user_change_failed")) == 2

    env.user("second", admin=True)
    assert env.client.post(f"/api/admin/users/{root}/admin", json={"value": False},
                           headers=hdr).status_code == 200


def test_user_create_through_admin_route_is_audited(env):
    hdr = admin_login(env)
    r = env.client.post("/api/admin/users", json={"username": "carol", "password": PASSWORD},
                        headers=hdr)
    assert r.status_code == 200
    assert [r[0] for r in audit_rows(env, "user_created")] == ["root"]
    rows = asyncio.run(audit.list_rows(env.store, 10, "user_created"))
    assert rows and PASSWORD not in str(rows)


def test_revoke_url_containing_the_full_key_leaves_no_key_text_in_the_audit_log(env):
    hdr = admin_login(env)
    made = env.client.post("/api/admin/keys", json={"host": "nas1"}, headers=hdr).json()
    secret = made["key"].split("_", 2)[2]
    for url_key in (made["key"], made["key"] + "?k=" + made["key"]):
        env.client.post(f"/api/admin/keys/{url_key}/revoke", headers=hdr)
    everything = " ".join(str(x) for row in env.rows("SELECT * FROM audit") for x in row)
    assert made["key"] not in everything and secret not in everything
    assert env.rows("SELECT COUNT(*) FROM audit WHERE kind LIKE 'key_revoke%'")[0][0] >= 1
