"""Plugin resources on /api/v2 (docs/DATA-API-DESIGN.md section 4.10), the read token routes of
the admin console, and the removal of the legacy read routes."""

from __future__ import annotations

import asyncio
from importlib.metadata import EntryPoint

import pytest

from observe.plugins import GROUP, PluginError, load_plugins

from .api_env import ApiEnv, host_batch
from .conftest import make_config


def plugins_for(mode: str = "good"):
    from tests.fakes import api_plugin
    api_plugin.plugin.mode = mode
    cfg = make_config([], plugins=["demo"])
    ep = EntryPoint(name="demo", value="tests.fakes.api_plugin:plugin", group=GROUP)
    return load_plugins(cfg, lambda: [ep])


@pytest.fixture
def env(tmp_path):
    e = ApiEnv(tmp_path, plugins=plugins_for())
    e.headers = e.token("operator", "op")
    asyncio.run(e.store.execute("INSERT INTO demo_things (name) VALUES ('alpha')"))
    asyncio.run(e.store.execute("INSERT INTO demo_things (name) VALUES ('beta')"))
    asyncio.run(e.store.execute("INSERT INTO demo_things (name) VALUES ('alphabet')"))
    yield e
    e.close()


def test_plugin_resource_is_served_with_the_core_machinery(env):
    first = env.get("/demo/things?limit=2", headers=env.headers)
    assert first.status_code == 200 and first.headers["etag"]
    body = first.json()
    assert [t["name"] for t in body["items"]] == ["alpha", "beta"] and body["next_cursor"]
    rest = env.get(f"/demo/things?limit=2&cursor={body['next_cursor']}", headers=env.headers).json()
    assert [t["name"] for t in rest["items"]] == ["alphabet"] and rest["next_cursor"] is None
    assert [t["name"] for t in env.get("/demo/things?name_contains=alpha",
                                       headers=env.headers).json()["items"]] == ["alpha", "alphabet"]
    again = env.get("/demo/things?limit=2", headers={**env.headers, "If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert env.get("/demo/things?limit=9999", headers=env.headers).status_code == 400
    assert env.get("/demo/things").status_code == 401  # a plugin resource is never anonymous


def test_a_plugin_resource_is_in_the_schema_under_its_tag(env):
    doc = env.get("/openapi.json").json()
    op = doc["paths"]["/demo/things"]["get"]
    assert op["tags"] == ["demo"] and op["operationId"] == "demo_things"
    names = {p["name"] for p in op["parameters"]}
    assert {"limit", "cursor", "name_contains"} <= names


def test_a_plugin_write_bumps_only_its_declared_domain(env):
    before = env.store.storage.change_seqs()
    assert env.get("/demo/bump?n=2", headers=env.headers).status_code == 200
    after = env.store.storage.change_seqs()
    assert after["unifi"] == before["unifi"] + 2
    assert {d for d in after if after[d] != before[d]} == {"unifi"}
    viewer = env.token("viewer", "v")
    assert env.get("/demo/bump", headers=viewer).status_code == 403
    changed = env.get("/changes?wait=0", headers=env.headers).json()
    assert changed["changed"] == []


def test_a_plugin_cannot_mount_outside_its_prefix_and_startup_says_why(tmp_path):
    with pytest.raises(PluginError, match="may mount only under /demo"):
        ApiEnv(tmp_path, plugins=plugins_for("outside"))


# ---- read tokens through the admin console --------------------------------------------------

def test_an_admin_creates_lists_and_revokes_read_tokens(tmp_path):
    e = ApiEnv(tmp_path)
    try:
        hdr = e.login("root", admin=True)
        r = e.client.post("/api/admin/keys", json={"host": "kiosk", "scope": "wpr",
                                                   "role": "viewer"}, headers=hdr)
        assert r.status_code == 200, r.text
        made = r.json()
        assert made["role"] == "viewer" and made["key"].startswith("wpr_")
        token = {"Authorization": f"Bearer {made['key']}"}
        e.client.cookies.clear()  # the token alone, without the admin cookie
        assert e.get("/monitors", headers=token).status_code == 200
        assert e.get("/hosts", headers=token).status_code == 200
        e.login("root2", admin=True)
        listed = {k["id"]: k for k in e.client.get("/api/admin/keys").json()}
        assert listed[made["id"]]["role"] == "viewer" and listed[made["id"]]["scope"] == "wpr"
        assert listed[made["id"]]["host"] == "kiosk"
        rows = asyncio.run(e.store.fetch(
            "SELECT detail FROM audit WHERE kind='key_created' ORDER BY id DESC LIMIT 1"))
        assert '"role": "viewer"' in rows[0][0] and made["key"] not in rows[0][0]
        assert e.client.post(f"/api/admin/keys/{made['id']}/revoke", headers=e.csrf).status_code == 200
        e.mono.advance(10)
        assert e.get("/monitors", headers=token).status_code == 401
    finally:
        e.close()


def test_the_admin_console_refuses_a_bad_read_token_request(tmp_path):
    e = ApiEnv(tmp_path)
    try:
        hdr = e.login("root", admin=True)
        for body in ({"host": "k", "scope": "wpr"}, {"host": "k", "scope": "wpr", "role": "admin"},
                     {"host": "k", "scope": "wpr", "role": 3}, {"host": "k", "scope": "wpi",
                                                               "role": "viewer"}):
            r = e.client.post("/api/admin/keys", json=body, headers=hdr)
            assert r.status_code == 422, body
        assert asyncio.run(e.store.fetch("SELECT COUNT(*) FROM ingest_keys")) == [(0,)]
        assert e.client.post("/api/admin/keys", json={"host": "k", "scope": "wpr", "role": "viewer"}
                             ).status_code in (401, 403)  # no CSRF header
    finally:
        e.close()


def test_read_tokens_never_appear_in_the_audit_log(tmp_path):
    from observe import audit
    text = audit.sanitize_audit_path("/api/v2/monitors?token=wpr_abcdef123456_" + "S" * 43)
    assert "wpr_" not in text and "SSSS" not in text


def test_last_use_of_a_token_is_recorded_at_most_once_a_minute(tmp_path):
    e = ApiEnv(tmp_path, api_auth_cache_s=0)  # no memory, so every request checks the token
    try:
        headers = e.token()
        for _ in range(3):
            assert e.get("/groups", headers=headers).status_code == 200
        first = asyncio.run(e.store.fetch("SELECT last_used FROM ingest_keys"))[0][0]
        assert first is not None
        for _ in range(3):
            e.get("/groups", headers=headers)
        assert asyncio.run(e.store.fetch("SELECT last_used FROM ingest_keys"))[0][0] == first
    finally:
        e.close()


# ---- the legacy read routes are gone --------------------------------------------------------

LEGACY = ("/api/monitors", "/api/monitors/core/history", "/api/events", "/api/groups",
          "/api/forecasts", "/api/hosts", "/api/hosts/nas01")


def test_the_legacy_read_routes_do_not_exist(tmp_path):
    e = ApiEnv(tmp_path)
    try:
        e.login("root", admin=True)
        e.push(host_batch())
        for path in LEGACY:
            r = e.client.get(path)
            assert r.status_code in (404, 405), path
        gets = {(r.path) for r in e.app.routes if "GET" in getattr(r, "methods", ())}
        assert not gets & {"/api/monitors", "/api/events", "/api/groups", "/api/forecasts",
                           "/api/hosts", "/api/hosts/{host:path}",
                           "/api/monitors/{slug}/history"}
        # The routes that write, and the pages, are untouched.
        assert "/api/hosts" in {r.path for r in e.app.routes if "POST" in getattr(r, "methods", ())}
        assert e.client.get("/").status_code in (200, 401)
    finally:
        e.close()


def test_every_html_page_script_calls_only_routes_that_exist():
    """The scripts of the console name no removed route."""
    import re
    from pathlib import Path
    root = Path(__file__).parent.parent / "observe" / "static"
    # The monitor, event, group and forecast reads are gone, and so are the host list and the
    # one-host read. Other /api/hosts routes (enrolment, settings, create) are other routes.
    removed = re.compile(r"""["'`]/api/(monitors|events|groups|forecasts)|"""
                         r"""(fetch\(|"GET",\s*)["'`]/api/hosts(["'`?]|/\$\{[^}]*\}["'`])""")
    for js in root.rglob("*.js"):
        text = js.read_text(encoding="utf-8")
        assert not removed.search(text), js.name
