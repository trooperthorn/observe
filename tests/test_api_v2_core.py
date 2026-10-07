"""The /api/v2 core: credentials and roles, problem details, ETags and the 304 path, rate limits,
the 503 path, the change cursor and the committed schema (docs/DATA-API-DESIGN.md section 4)."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from observe.api import schema as schema_mod
from observe.checks.base import Result
from observe.ingest.keys import create_key, revoke_key
from observe.state import State, Transition
from observe.storage.base import StorageBusy

from .api_env import START, ApiEnv, host_batch


@pytest.fixture
def env(tmp_path):
    e = ApiEnv(tmp_path)
    yield e
    e.close()


# ---- the committed schema -------------------------------------------------------------------

def test_committed_schema_matches_the_code():
    text = schema_mod.render(schema_mod.generate())
    on_disk = schema_mod.PATH.read_text(encoding="utf-8")
    assert on_disk == text, "docs/openapi-v2.json is out of date; run: python -m observe.api.schema"


def test_schema_describes_every_route_completely():
    doc = schema_mod.generate()
    seen = set()
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            assert op["operationId"] not in seen
            seen.add(op["operationId"])
            assert op["tags"] and op["summary"], (method, path)
            ok = op["responses"]["200"]["content"]["application/json"]["schema"]
            assert "$ref" in ok, (method, path)
            assert {"bearerAuth": []} in op["security"] and {"sessionCookie": []} in op["security"]
            assert "application/problem+json" in op["responses"]["401"]["content"]
    assert {"/hosts", "/hosts/{name}", "/monitors", "/monitors/{slug}", "/groups", "/events",
            "/metrics", "/metrics/latest", "/metrics/query", "/changes",
            "/status", "/waiting-hosts"} <= set(doc["paths"])
    assert doc["servers"] == [{"url": "/api/v2"}]


def test_schema_is_served_without_a_login(env):
    r = env.get("/openapi.json")
    assert r.status_code == 200 and r.json()["info"]["title"] == "Observe API"
    assert r.json() == schema_mod.generate()


# ---- credentials and roles ------------------------------------------------------------------

def test_no_credentials_is_a_401_problem(env):
    r = env.get("/monitors")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"].endswith("/unauthenticated") and body["status"] == 401
    assert body["instance"] == "/api/v2/monitors" and body["request_id"] == r.headers["x-request-id"]


def test_session_and_token_both_read(env):
    env.login("alice", admin=False)
    assert env.get("/monitors").status_code == 200
    env.client.cookies.clear()
    assert env.get("/monitors").status_code == 401
    assert env.get("/monitors", headers=env.token()).status_code == 200


def test_basic_auth_and_other_key_scopes_are_refused(env):
    import base64
    asyncio.run(env.store.execute("SELECT 1"))
    basic = {"Authorization": "Basic " + base64.b64encode(b"ops:pw").decode()}
    assert env.get("/monitors", headers=basic).status_code == 401
    for scope in ("wpi", "wpf"):
        plain, _ = asyncio.run(create_key(env.store, "h", "t", scope=scope))
        assert env.get("/monitors", headers={"Authorization": f"Bearer {plain}"}).status_code == 401
    bad = env.get("/monitors", headers={"Authorization": "Bearer wpr_nope_nope"})
    assert bad.status_code == 401 and bad.headers["www-authenticate"] == "Bearer"


def test_a_read_token_is_never_an_ingest_key(env):
    plain, _ = asyncio.run(create_key(env.store, "script", "t", scope="wpr", role="viewer"))
    batch = host_batch().model_dump(mode="json")
    r = env.client.post("/internal/v1/ingest", json=batch,
                        headers={"Authorization": f"Bearer {plain}"})
    assert r.status_code in (401, 403)


def test_token_roles_are_viewer_or_operator_only(env):
    with pytest.raises(Exception):
        asyncio.run(create_key(env.store, "x", "t", scope="wpr", role="admin"))
    with pytest.raises(Exception):
        asyncio.run(create_key(env.store, "x", "t", scope="wpr", role=""))
    with pytest.raises(Exception):
        asyncio.run(create_key(env.store, "x", "t", scope="wpi", role="viewer"))


def test_revoked_token_stops_within_the_cache_window(env):
    headers = env.token()
    assert env.get("/monitors", headers=headers).status_code == 200
    asyncio.run(revoke_key(env.store, headers["Authorization"].split("_")[1]))
    assert env.get("/monitors", headers=headers).status_code == 200  # remembered, at most 5 s
    env.mono.advance(6)
    assert env.get("/monitors", headers=headers).status_code == 401


def test_logout_ends_the_session_for_v2_at_once(env):
    env.login("alice", admin=False)
    assert env.get("/monitors").status_code == 200
    assert env.client.post("/api/logout", headers=env.csrf).status_code == 200
    assert env.get("/monitors").status_code == 401


def test_anonymous_read_is_off_by_default_and_never_opens_hosts(tmp_path):
    e = ApiEnv(tmp_path, anonymous_read=True)
    try:
        assert e.get("/monitors").status_code == 200
        assert e.get("/groups").status_code == 200
        assert e.get("/hosts").status_code == 401  # hardware inventory needs a session or token
        assert e.get("/hosts", headers=e.token()).status_code == 200
        assert e.get("/monitors", headers={"Authorization": "Bearer wpr_a_b"}).status_code == 401
    finally:
        e.close()


def test_denials_are_audited_once_per_peer_window(env):
    for _ in range(5):
        env.get("/monitors")
    rows = asyncio.run(env.store.fetch("SELECT kind, status FROM audit WHERE kind='api_denied'"))
    assert len(rows) == 1 and rows[0][1] == 401


# ---- the role matrix ------------------------------------------------------------------------

def _matrix_app(env, anonymous: bool = True):
    """Extra resources at every role, mounted through the same registry plugins use."""
    from fastapi import FastAPI

    from observe.api import ApiRegistry, ApiRuntime
    from observe.api import problems
    from observe.api.registry import CachedBody, NotModified, on_cached, on_not_modified
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    class Ok(BaseModel):
        ok: bool

    app = FastAPI()
    problems.install(app)
    app.add_exception_handler(NotModified, on_not_modified)
    app.add_exception_handler(CachedBody, on_cached)
    runtime = ApiRuntime(env.cfg, env.store, scheduler=env.sched, auth_clock=env.wall,
                         clock=env.mono)
    api = ApiRegistry(app, runtime)
    def ok():
        return {"ok": True}

    api.resource("/viewer-thing", ok, Ok, roles=("viewer",), operation_id="viewer_thing")
    api.resource("/operator-thing", ok, Ok, roles=("operator",), operation_id="operator_thing")
    api.resource("/admin-thing", ok, Ok, roles=("admin",), operation_id="admin_thing")
    api.resource("/admin-post", ok, Ok, roles=("admin",), methods=("POST",),
                 operation_id="admin_post")
    return TestClient(app, base_url="https://testserver"), runtime


def test_role_matrix(env):
    client, _ = _matrix_app(env)
    viewer, operator = env.token("viewer", "v"), env.token("operator", "o")
    admin_csrf = env.login("root", admin=True)
    admin_cookies = dict(env.client.cookies)
    expect = {  # path: (viewer token, operator token, admin session)
        "/viewer-thing": (200, 200, 200),
        "/operator-thing": (403, 200, 200),
        "/admin-thing": (403, 403, 200),
    }
    for path, (v, o, a) in expect.items():
        assert client.get(path, headers=viewer).status_code == v, path
        assert client.get(path, headers=operator).status_code == o, path
        assert client.get(path, cookies=admin_cookies).status_code == a, path
        assert client.get(path).status_code == 401, path
    # A session needs the CSRF header on an unsafe method; a token does not.
    assert client.post("/admin-post", cookies=admin_cookies).status_code == 403
    assert client.post("/admin-post", cookies=admin_cookies, headers=admin_csrf).status_code == 200
    assert client.post("/admin-post", headers=operator).status_code == 403
    # A non-admin session reads as viewer.
    env.client.cookies.clear()
    env.login("bob", admin=False)
    assert client.get("/viewer-thing", cookies=dict(env.client.cookies)).status_code == 200
    assert client.get("/operator-thing", cookies=dict(env.client.cookies)).status_code == 403


def runtime_resources(client):
    return []


def test_registry_refuses_bad_registrations(env):
    client, runtime = _matrix_app(env)
    from observe.api import ApiRegistry
    from pydantic import BaseModel

    class Ok(BaseModel):
        ok: bool

    def ok():
        return {"ok": True}

    api = ApiRegistry(client.app, runtime, resources=runtime_resources(client))
    api.resource("/thing", ok, Ok, operation_id="thing_one")
    for kwargs in ({"roles": ("root",)}, {"domains": ("nope",)}, {"operation_id": "thing_one"}):
        with pytest.raises(ValueError):
            api.resource("/other", ok, Ok, **{"operation_id": "x", **kwargs})
    with pytest.raises(ValueError):
        api.resource("/thing", ok, Ok, operation_id="thing_two")
    with pytest.raises(ValueError):
        api.resource("things", ok, Ok)


def test_a_plugin_registry_stays_under_its_own_prefix(env):
    client, runtime = _matrix_app(env)
    from observe.api import ApiRegistry
    from pydantic import BaseModel

    class Ok(BaseModel):
        ok: bool

    def ok():
        return {"ok": True}

    api = ApiRegistry(client.app, runtime).for_plugin("unifi")
    api.resource("/unifi/devices", ok, Ok, domains=("unifi", "metrics"), operation_id="devices")
    for path in ("/ha/instances", "/monitors", "/unifi2/x", "/"):
        with pytest.raises(ValueError):
            api.resource(path, ok, Ok, operation_id="x")
    # A plugin resource is never anonymous, and a plugin writes only to domains it declared.
    assert client.get("/unifi/devices").status_code == 401
    assert client.get("/unifi/devices", headers=env.token()).status_code == 200
    with pytest.raises(ValueError):
        asyncio.run(api.submit_write(lambda db: None, touches=("ha",)))
    asyncio.run(api.submit_write(lambda db: None, touches=("unifi",)))


# ---- ETag, 304 and the cache ----------------------------------------------------------------

class CountingStorage:
    """Wraps a Storage and counts the reads that reach the database."""

    def __init__(self, inner):
        self._inner = inner
        self.reads = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def read(self, unit):
        self.reads += 1
        return await self._inner.read(unit)

    def read_sync(self, unit):
        self.reads += 1
        return self._inner.read_sync(unit)

    async def fetchall(self, sql, args=()):
        self.reads += 1
        return await self._inner.fetchall(sql, args)


def test_unchanged_page_is_a_304_that_opens_no_read_connection(env):
    env.poll("core", Result.OK)
    env.push(host_batch())
    headers = env.token()
    first = env.get("/hosts", headers=headers)
    assert first.status_code == 200 and first.headers["etag"].startswith('W/"')
    counter = CountingStorage(env.store.storage)
    env.store.storage = counter
    try:
        again = env.get("/hosts", headers={**headers, "If-None-Match": first.headers["etag"]})
        assert again.status_code == 304 and again.content == b""
        assert counter.reads == 0
        # The same request without If-None-Match is served from the cache, also with no read.
        cached = env.get("/hosts", headers=headers)
        assert cached.status_code == 200 and cached.content == first.content
        assert counter.reads == 0
        # A change in a domain the page depends on makes a new ETag and a real read.
        env.push(host_batch(ts=START - 4))
        fresh = env.get("/hosts", headers={**headers, "If-None-Match": first.headers["etag"]})
        assert fresh.status_code == 200 and fresh.headers["etag"] != first.headers["etag"]
        assert counter.reads > 0
    finally:
        env.store.storage = counter._inner


def test_a_memory_resource_follows_scheduler_state_without_a_counter(env):
    headers = env.token()
    first = env.get("/monitors", headers=headers)
    env.sched.states["core"].state = State.DOWN  # no write, so no change counter moves
    second = env.get("/monitors", headers={**headers, "If-None-Match": first.headers["etag"]})
    assert second.status_code == 200 and second.headers["etag"] != first.headers["etag"]
    by = {m["slug"]: m for m in second.json()["items"]}
    assert by["core"]["state"] == "down" and by["edge"]["effective_state"] == "pending"


def test_etag_differs_by_query_and_role(env):
    a = env.get("/monitors?limit=1", headers=env.token())
    b = env.get("/monitors?limit=2", headers=env.token())
    assert a.headers["etag"] != b.headers["etag"]
    c = env.get("/monitors?limit=1", headers=env.token("operator", "o2"))
    assert c.headers["etag"] != a.headers["etag"]
    # A matching ETag for another query does not apply.
    d = env.get("/monitors?limit=2", headers={**env.token(), "If-None-Match": a.headers["etag"]})
    assert d.status_code == 200


def test_the_cache_is_bounded(env):
    from observe.api.registry import ResponseCache
    cache = ResponseCache(limit=1000)
    for i in range(20):
        cache.put(("r", str(i), "viewer"), "e", b"x" * 200)
    assert cache.size <= 1000
    assert cache.get(("r", "19", "viewer"), "e") == b"x" * 200
    assert cache.get(("r", "0", "viewer"), "e") is None
    cache.put(("r", "big", "viewer"), "e", b"x" * 900)  # too large to keep
    assert cache.get(("r", "big", "viewer"), "e") is None


# ---- rate limits and the 503 path -----------------------------------------------------------

def test_rate_limit_is_a_429_with_retry_after(tmp_path):
    e = ApiEnv(tmp_path, api_rate_per_second=1, api_burst=3)
    try:
        headers = e.token()
        codes = [e.get("/groups", headers=headers).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        r = e.get("/groups", headers=headers)
        assert r.headers["retry-after"] == "1" and r.json()["type"].endswith("/rate-limited")
        e.mono.advance(2)
        assert e.get("/groups", headers=headers).status_code == 200
        # Another principal has its own bucket.
        assert e.get("/groups", headers=e.token(label="other")).status_code == 200
    finally:
        e.close()


def test_a_query_costs_five_tokens(tmp_path):
    e = ApiEnv(tmp_path, api_rate_per_second=1, api_burst=10)
    try:
        headers = e.token()
        codes = [e.get("/metrics/query?metric=m", headers=headers).status_code
                 for _ in range(3)]
        assert codes == [200, 200, 429]
    finally:
        e.close()


def test_failed_credentials_are_limited_per_peer(env):
    codes = [env.get("/monitors", headers={"Authorization": "Bearer wpr_a_b"}).status_code
             for _ in range(25)]
    assert codes[0] == 401 and codes[-1] == 429


def test_a_busy_database_is_a_503_with_retry_after(env):
    class Busy:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def read(self, unit):
            raise StorageBusy("no free read connection")

    headers = env.token()
    inner = env.store.storage
    env.store.storage = Busy(inner)
    try:
        r = env.get("/events", headers=headers)
    finally:
        env.store.storage = inner
    assert r.status_code == 503 and r.headers["retry-after"] == "2"
    assert r.json()["type"].endswith("/busy")


def test_an_internal_error_hides_its_cause(env, monkeypatch):
    from observe.api import hosts

    async def boom(*a, **k):
        raise RuntimeError("secret detail /etc/passwd")

    headers = env.token()
    monkeypatch.setattr(hosts.HostViews, "view", boom)
    env.push(host_batch())
    r = env.get("/hosts", headers=headers)
    assert r.status_code == 500
    assert "secret" not in r.text and "Traceback" not in r.text
    assert r.json()["type"].endswith("/internal")


def test_validation_errors_are_400_problems_without_the_input(env):
    r = env.get("/monitors?limit=100000&sort=bogus", headers=env.token())
    assert r.status_code == 400 and r.json()["type"].endswith("/validation")
    assert "100000" not in json.dumps(r.json().get("errors"))
    assert env.get("/monitors?sort=bogus", headers=env.token()).status_code == 400
    assert env.get("/nope", headers=env.token()).status_code == 404
    assert env.get("/nope").json()["type"].endswith("/not-found")


# ---- the change cursor ----------------------------------------------------------------------

def test_changes_returns_a_cursor_then_the_domains_that_moved(env):
    headers = env.token()
    first = env.get("/changes", headers=headers).json()
    assert first["changed"] == []
    same = env.get(f"/changes?since={first['cursor']}&wait=0", headers=headers).json()
    assert same == {"cursor": first["cursor"], "changed": []}
    env.poll("core")
    moved = env.get(f"/changes?since={first['cursor']}&wait=0", headers=headers).json()
    assert set(moved["changed"]) == {"monitors", "metrics"} and moved["cursor"] != first["cursor"]
    assert env.get("/changes?since=!!!", headers=headers).status_code == 400
    assert env.get("/changes?wait=31", headers=headers).status_code == 400


def test_changes_waits_and_wakes_on_a_write(tmp_path):
    from observe.api import changes
    changes.POLL_S = 0.02
    e = ApiEnv(tmp_path)
    try:
        headers = e.token()
        cursor = e.get("/changes", headers=headers).json()["cursor"]
        timer = threading.Timer(0.3, lambda: asyncio.run(
            e.store.record_event("core", Transition(State.UP, State.DOWN, 1.0, "m"))))
        timer.start()
        r = e.get(f"/changes?since={cursor}&wait=10", headers=headers).json()
        timer.join()
        assert r["changed"] == ["events"]
        # With nothing happening the answer comes after the wait, with the cursor unchanged.
        quiet = e.get(f"/changes?since={r['cursor']}&wait=1", headers=headers).json()
        assert quiet == {"cursor": r["cursor"], "changed": []}
    finally:
        changes.POLL_S = 0.2
        e.close()


def test_changes_treats_a_foreign_cursor_as_everything_changed(env):
    from observe.api.cursor import encode
    r = env.get(f"/changes?since={encode([1, 2])}&wait=0", headers=env.token()).json()
    assert len(r["changed"]) == 10


def test_v2_never_writes_session_last_seen_on_the_304_path(env):
    env.login("alice", admin=False)
    first = env.get("/monitors")
    before = asyncio.run(env.store.fetch("SELECT last_seen FROM sessions"))
    env.wall.advance(30)
    again = env.get("/monitors", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert asyncio.run(env.store.fetch("SELECT last_seen FROM sessions")) == before
    env.wall.advance(60)  # past the minute: the next hit records it, once
    env.get("/monitors", headers={"If-None-Match": first.headers["etag"]})
    assert asyncio.run(env.store.fetch("SELECT last_seen FROM sessions")) != before


def test_schema_file_uses_lf_endings():
    assert b"\r" not in Path(schema_mod.PATH).read_bytes()


def test_a_host_that_goes_quiet_changes_its_page_with_no_write(env):
    env.push(host_batch())
    headers = env.token()
    first = env.get("/hosts/nas01", headers=headers)
    assert first.json()["stale"] is False
    env.wall.advance(10_000)  # no batch, so no change counter moves; only the clock does
    second = env.get("/hosts/nas01", headers={**headers, "If-None-Match": first.headers["etag"]})
    assert second.status_code == 200 and second.json()["stale"] is True
    assert second.headers["etag"] != first.headers["etag"]


def test_pages_that_differ_only_by_path_do_not_share_an_etag_or_a_cached_body(env):
    headers = env.token()
    a = env.get("/monitors/core", headers=headers)
    b = env.get("/monitors/edge", headers=headers)
    assert a.headers["etag"] != b.headers["etag"]
    assert a.json()["slug"] == "core" and b.json()["slug"] == "edge"
    assert env.get("/monitors/core", headers=headers).json()["slug"] == "core"
    wrong = env.get("/monitors/edge", headers={**headers, "If-None-Match": a.headers["etag"]})
    assert wrong.status_code == 200 and wrong.json()["slug"] == "edge"


def test_an_answer_that_does_not_fit_its_model_is_a_500_problem(env):
    client, runtime = _matrix_app(env)
    from observe.api import ApiRegistry
    from pydantic import BaseModel

    class Ok(BaseModel):
        ok: bool

    def wrong():
        return {"ok": "not a bool"}

    api = ApiRegistry(client.app, runtime)
    api.resource("/wrong", wrong, Ok, operation_id="wrong_one")
    r = client.get("/wrong", headers=env.token())
    assert r.status_code == 500 and r.json()["type"].endswith("/internal")
    assert "bool" not in r.text
