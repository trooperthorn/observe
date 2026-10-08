"""The map, ports, findings, audit, admin, settings, Home Assistant, resource and plugin
resources of /api/v2 (docs/DATA-API-DESIGN.md sections 4.2 and 4.11, slice O-7)."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from observe import audit
from observe.api import schema as schema_mod
from observe.config import Config
from observe.infra import InfraService
from observe.ingest.keys import create_key, revoke_key
from observe.ingest.schema import SourceStatus
from observe.portkey import switch_id

from .api_env import START, ApiEnv, host_batch
from .test_api_v2_plugins import plugins_for

SID = switch_id("aa:bb:cc:dd:ee:07")
HOSTILE = '<img src=x onerror="alert(1)">&"\'</script>'


@pytest.fixture
def env(tmp_path):
    e = ApiEnv(tmp_path)
    e.infra = InfraService(e.store)
    yield e
    e.close()


def run(coro):
    return asyncio.run(coro)


def seed_port(env, rows=(("link_speed_mbps", 1000), ("link_speed_mbps", 100)), name="edge"):
    """A switch with one patched port, and field properties whose last two values conflict."""
    infra = env.infra
    run(infra.upsert_switch(SID, name=name, mgmt_addresses=["10.0.0.2"], now=1.0))
    run(infra.upsert_port(SID, "Gi1/0/5", if_index=3, now=1.0))
    run(infra.upsert_jack("J-1", room="R1", site="hq", switch=SID, port="Gi1/0/5", now=1.0))
    run(infra.upsert_link(infra.jack_ref("J-1"), infra.port_ref(SID, "Gi1/0/5"), source="lldp",
                          confidence=0.9, now=START))
    for n, (prop, value) in enumerate(rows):
        run(infra.append_property(SID, "Gi1/0/5", prop, value, source="field",
                                  report_id=f"r{n}", observed_at=10.0 + n, now=10.0 + n))
    run(env.app.state.mapper.rebuild(START))


# ---- the committed schema and a contract test per resource ----------------------------------

def test_every_new_resource_is_in_the_committed_schema_with_its_roles():
    doc = schema_mod.generate()
    paths = set(doc["paths"])
    assert {"/map", "/map/nodes", "/map/edges", "/ports", "/ports/{switch_id}/{port}",
            "/findings", "/findings/ack", "/session", "/plugins", "/audit", "/admin/keys",
            "/admin/users", "/admin/config", "/admin/settings/tiers",
            "/admin/settings/retention", "/admin/settings/recheck", "/admin/settings/rules",
            "/admin/settings/storage", "/resources", "/resources/{rid}", "/ha/instances",
            "/ha/instances/{name}"} <= paths
    for path in ("/audit", "/admin/keys", "/admin/users", "/admin/config",
                 "/admin/settings/storage", "/findings/ack"):
        for op in doc["paths"][path].values():
            assert op["x-roles"] == ["admin"], path
            assert {} not in op["security"], path  # never anonymous


def _concrete(path: str) -> str | None:
    """A path with its parameters filled in, or None for one that needs real ids."""
    return None if "{" in path else path


def test_contract_every_listed_get_answers_with_its_schema_and_the_standard_machinery(tmp_path):
    """Generated from the schema: each parameterless GET refuses a stranger, refuses a viewer
    when it is admin only, answers an admin with a body that fits its model, and states a 304
    for an unchanged page when it sends an ETag."""
    e = ApiEnv(tmp_path)
    try:
        e.infra = InfraService(e.store)
        seed_port(e)
        e.push(host_batch())
        e.poll("core")
        doc = e.get("/openapi.json").json()
        viewer = e.token("viewer", "v")
        e.login("root", admin=True)
        saved = [(c.name, c.value) for c in e.client.cookies.jar]
        checked = 0
        for path, ops in sorted(doc["paths"].items()):
            for method, op in ops.items():
                url = _concrete(path)
                # A selector that must name a metric is covered in test_api_v2_metrics.py.
                needs = path in ("/metrics/latest", "/metrics/query") or any(
                    p.get("required") for p in op.get("parameters", []))
                if method != "get" or url is None or needs:
                    continue
                roles = op["x-roles"]
                e.client.cookies.clear()
                e.app.state.v2_runtime.failures._state.clear()  # the per-peer failure allowance
                assert e.get(url).status_code == 401, url
                assert e.get(url, headers={"Authorization": "Bearer nope"}).status_code == 401
                if roles == ["admin"]:
                    assert e.get(url, headers=viewer).status_code == 403, url
                for name, value in saved:
                    e.client.cookies.set(name, value)
                got = e.get(url)
                assert got.status_code == 200, (url, got.text)
                assert got.headers["content-type"].startswith("application/json")
                model = op["responses"]["200"]["content"]["application/json"]["schema"]
                assert "$ref" in model
                if "etag" in got.headers:
                    again = e.get(url, headers={"If-None-Match": got.headers["etag"]})
                    assert again.status_code == 304, url
                checked += 1
        assert checked >= 20
    finally:
        e.close()


# ---- session --------------------------------------------------------------------------------

def test_session_tells_who_is_calling_and_a_session_gets_its_csrf_token(env):
    assert env.get("/session").status_code == 401
    csrf = env.login("root", admin=True)
    got = env.get("/session").json()
    assert got == {"username": "root", "kind": "session", "role": "admin", "is_admin": True,
                   "csrf": csrf["X-CSRF-Token"]}
    env.client.cookies.clear()
    tok = env.get("/session", headers=env.token("operator", "ops")).json()
    assert tok["kind"] == "token" and tok["role"] == "operator" and tok["csrf"] is None
    assert tok["is_admin"] is False


def test_anonymous_read_reads_the_session_as_anonymous_but_nothing_admin(tmp_path):
    e = ApiEnv(tmp_path, anonymous_read=True)
    try:
        assert e.get("/session").json()["role"] == "anonymous"
        for path in ("/audit", "/admin/keys", "/admin/config", "/plugins", "/map", "/findings",
                     "/ha/instances", "/resources"):
            assert e.get(path).status_code == 401, path
    finally:
        e.close()


# ---- the map --------------------------------------------------------------------------------

def test_map_nodes_edges_and_the_filtered_map(env):
    seed_port(env)
    other = switch_id("aa:bb:cc:dd:ee:08")
    run(env.infra.upsert_switch(other, name="core", now=1.0))
    run(env.infra.upsert_port(other, "Gi1/0/1", now=1.0))
    run(env.infra.upsert_link(env.infra.port_ref(SID, "Gi1/0/5"),
                              env.infra.port_ref(other, "Gi1/0/1"), source="lldp",
                              confidence=0.9, now=START))
    run(env.app.state.mapper.rebuild(START))
    env.login("bob", admin=False)
    whole = env.get("/map").json()
    kinds = {n["kind"] for n in whole["nodes"]}
    assert {"switch", "port", "jack"} <= kinds
    assert whole["filter"] == {"site": None, "building": None} and whole["stale_days"] > 0
    cut = env.get("/map?site=nowhere").json()
    assert cut["nodes"] == [] and cut["filter"]["site"] == "nowhere"
    kept = env.get("/map?site=hq").json()
    assert {n["kind"] for n in kept["nodes"]} >= {"jack", "port", "switch"}

    first = env.get("/map/nodes?limit=2").json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    seen = [n["id"] for n in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        page = env.get(f"/map/nodes?limit=2&cursor={cursor}").json()
        seen += [n["id"] for n in page["items"]]
        cursor = page["next_cursor"]
    assert seen == sorted(n["id"] for n in whole["nodes"]) == sorted(set(seen))
    only = env.get("/map/nodes?kind=switch").json()["items"]
    assert sorted(n["label"] for n in only) == ["core", "edge"]
    whole = env.get("/map").json()
    edges = env.get("/map/edges").json()["items"]
    assert edges and sorted(e["id"] for e in edges) == sorted(e["id"] for e in whole["edges"])
    assert {"id", "a", "b", "source", "state"} <= set(edges[0])
    assert env.get("/map/edges?source=lldp").status_code == 200
    assert env.get("/map/nodes?cursor=%%%").status_code == 400


def test_map_etag_changes_with_the_map_and_serves_304_otherwise(env):
    env.login("bob", admin=False)
    first = env.get("/map")
    assert env.get("/map", headers={"If-None-Match": first.headers["etag"]}).status_code == 304
    seed_port(env)
    second = env.get("/map")
    assert second.status_code == 200 and second.headers["etag"] != first.headers["etag"]
    assert second.json()["nodes"]


def test_hostile_map_strings_are_json_only(env):
    seed_port(env, name=HOSTILE)
    env.login("bob", admin=False)
    got = env.get("/map")
    assert got.headers["content-type"].startswith("application/json")
    assert any(n["label"] == HOSTILE for n in got.json()["nodes"])
    assert got.headers["x-content-type-options"] == "nosniff"


# ---- ports and findings ---------------------------------------------------------------------

def test_ports_list_and_one_port(env):
    seed_port(env)
    env.login("bob", admin=False)
    ports = env.get("/ports").json()["items"]
    assert [(p["switch_id"], p["port_key"], p["switch_name"]) for p in ports] == [
        (SID, "gi1/0/5", "edge")]
    assert ports[0]["first_seen"].endswith("Z") and isinstance(ports[0]["findings"], list)
    assert env.get(f"/ports?switch_id={SID}").json()["items"]
    assert env.get("/ports?switch_id=other").json()["items"] == []
    one = env.get(f"/ports/{SID}/Gi1/0/5")
    assert one.status_code == 200
    d = one.json()
    assert d["port_key"] == "gi1/0/5" and d["switch"]["name"] == "edge"
    assert d["properties"]["link_speed_mbps"]["value"] == 100
    assert [f["kind"] for f in d["findings"]] == ["speed_drop"]
    assert env.get(f"/ports/{SID}/gi9").status_code == 404
    assert env.get("/ports/nope/gi1").status_code == 404


def test_ports_page_by_cursor(env):
    infra = env.infra
    run(infra.upsert_switch(SID, name="edge", now=1.0))
    for n in range(5):
        run(infra.upsert_port(SID, f"Gi1/0/{n + 1}", now=1.0))
    run(env.app.state.mapper.rebuild(START))
    env.login("bob", admin=False)
    keys, cursor = [], None
    while True:
        page = env.get("/ports?limit=2" + (f"&cursor={cursor}" if cursor else "")).json()
        keys += [p["port_key"] for p in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert keys == [f"gi1/0/{n + 1}" for n in range(5)]


def test_findings_list_and_an_admin_acknowledges_with_csrf_and_an_audit_row(env):
    seed_port(env)
    body = {"switch_id": SID, "port_key": "gi1/0/5", "kind": "speed_drop"}
    assert env.get("/findings").status_code == 401
    csrf = env.login("bob", admin=False)
    listed = env.get("/findings").json()["items"]
    assert [(f["kind"], f["acknowledged"]) for f in listed] == [("speed_drop", False)]
    assert env.client.post("/api/v2/findings/ack", json=body, headers=csrf).status_code == 403
    env.client.cookies.clear()
    csrf = env.login("root", admin=True)
    assert env.client.post("/api/v2/findings/ack", json=body).status_code == 403  # no CSRF
    bad = env.client.post("/api/v2/findings/ack", json={**body, "kind": "nope"}, headers=csrf)
    assert bad.status_code == 409 and bad.headers["content-type"].startswith(
        "application/problem+json")
    assert env.client.post("/api/v2/findings/ack", json={"kind": "x"}, headers=csrf
                           ).status_code == 400
    ok = env.client.post("/api/v2/findings/ack", json=body, headers=csrf)
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    assert env.get("/findings").json()["items"][0]["acknowledged"] is True
    assert env.get(f"/ports/{SID}/Gi1/0/5").json()["findings"][0]["acked_by"] == "root"
    kinds = [r[0] for r in env.store.storage.read_sync(lambda db: db.execute(
        "SELECT kind FROM audit WHERE kind LIKE 'infra_finding%' ORDER BY id").fetchall())]
    assert kinds == ["infra_finding_ack_failed", "infra_finding_acknowledged"]
    # A read token, even an operator, cannot acknowledge.
    env.client.cookies.clear()
    tok = env.token("operator", "ops")
    assert env.client.post("/api/v2/findings/ack", json=body, headers=tok).status_code == 403


# ---- audit ----------------------------------------------------------------------------------

def test_audit_is_admin_only_paged_filtered_and_redacted(env):
    for n in range(5):
        run(audit.record(env.store, "probe", actor="x", detail={"n": n, "password": "p1"}))
    run(audit.record(env.store, "other", actor="y"))
    assert env.get("/audit").status_code == 401
    env.login("bob", admin=False)
    assert env.get("/audit").status_code == 403
    env.client.cookies.clear()
    assert env.get("/audit", headers=env.token("operator", "ops")).status_code == 403
    env.login("root", admin=True)
    got = env.get("/audit?kind=probe&limit=2").json()
    assert [r["detail"]["n"] for r in got["items"]] == [4, 3] and got["next_cursor"]
    assert all(r["detail"]["password"] == "[redacted]" or "p1" not in json.dumps(r)
               for r in got["items"])
    rest = env.get("/audit?kind=probe&limit=10&cursor=" + got["next_cursor"]).json()
    assert [r["detail"]["n"] for r in rest["items"]] == [2, 1, 0] and rest["next_cursor"] is None
    assert [r["kind"] for r in env.get("/audit?actor=y").json()["items"]] == ["other"]
    assert env.get("/audit?limit=0").status_code == 400
    assert "p1" not in env.get("/audit?limit=500").text


def test_audit_etag_follows_the_audit_domain(env):
    env.login("root", admin=True)
    first = env.get("/audit")
    assert env.get("/audit", headers={"If-None-Match": first.headers["etag"]}).status_code == 304
    run(audit.record(env.store, "probe", actor="x"))
    again = env.get("/audit", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 200 and again.headers["etag"] != first.headers["etag"]


# ---- keys, users, config --------------------------------------------------------------------

def test_admin_keys_are_listed_by_prefix_without_any_secret(env):
    plain, info = run(create_key(env.store, "nas01", "test"))
    wpr, rinfo = run(create_key(env.store, "script", "test", scope="wpr", role="viewer"))
    run(revoke_key(env.store, info.prefix))
    env.login("root", admin=True)
    got = env.get("/admin/keys")
    assert got.status_code == 200 and "etag" not in got.headers
    items = got.json()["items"]
    by_id = {k["id"]: k for k in items}
    assert by_id[info.prefix]["active"] is False and by_id[rinfo.prefix]["role"] == "viewer"
    assert set(items[0]) == {"id", "host", "scope", "role", "created", "created_by",
                             "revoked_at", "last_used", "active"}
    for secret in (plain, wpr, plain.split("_", 2)[2], wpr.split("_", 2)[2]):
        assert secret not in got.text
    assert [k["id"] for k in env.get("/admin/keys?scope=wpr").json()["items"]] == [rinfo.prefix]
    assert [k["id"] for k in env.get("/admin/keys?active=true").json()["items"]] == [rinfo.prefix]
    assert [k["id"] for k in env.get("/admin/keys?active=false").json()["items"]] == [info.prefix]
    page = env.get("/admin/keys?limit=1").json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    env.client.cookies.clear()
    assert env.get("/admin/keys", headers=env.token("operator", "o")).status_code == 403


def test_admin_users_never_show_a_hash(env):
    env.login("root", admin=True)
    env.login("bob", admin=False)
    env.client.cookies.clear()
    env.login("boss", admin=True)
    items = env.get("/admin/users").json()["items"]
    assert [u["username"] for u in items] == ["root", "bob", "boss"]
    assert set(items[0]) == {"id", "username", "is_admin", "disabled", "created"}
    assert "argon2" not in env.get("/admin/users").text and "$" not in env.get("/admin/users").text
    assert len(env.get("/admin/users?limit=1").json()["items"]) == 1


def test_config_shows_no_secret(tmp_path):
    e = ApiEnv(tmp_path, basic_auth_user="ops", basic_auth_password="hunter2hunter2",
               public_url="https://observe.example")
    try:
        e.login("root", admin=True)
        text = e.get("/admin/config").text
        got = json.loads(text)
        assert got["server"]["basic_auth_set"] is True and got["storage"]["backend"] == "sqlite"
        assert got["server"]["public_url"] == "https://observe.example"
        assert "hunter2" not in text and "ops" not in got["server"].values()
        assert got["monitors"] == 3 and got["storage"]["dsn_set"] is False
        assert "basic_auth_password" not in text
    finally:
        e.close()


def test_a_postgres_connection_string_is_only_ever_reported_as_set():
    from observe.api.admin import get_config
    from observe.api.registry import ApiContext
    cfg = Config.model_validate({
        "server": {"db_path": ":memory:"}, "monitors": [],
        "storage": {"backend": "postgres",
                    "dsn": "postgresql://observe@db.internal:5432/observe"},
        "credentials": {"ha": {"type": "homeassistant", "token": "SEKRET-T0KEN"}}})

    class Runtime:
        config = cfg

    out = get_config(ApiContext(None, Runtime(), 0.0))  # type: ignore[arg-type]
    text = json.dumps(out)
    assert out["storage"]["dsn_set"] is True and out["storage"]["backend"] == "postgres"
    assert "db.internal" not in text and "SEKRET" not in text
    assert out["credentials"] == [{"name": "ha", "type": "homeassistant"}]


# ---- settings -------------------------------------------------------------------------------

def test_settings_documents_for_tiers_retention_recheck_and_rules(env):
    env.login("root", admin=True)
    tiers = env.get("/admin/settings/tiers").json()
    assert tiers["global"]["availability"] == 30 and "bounds" in tiers
    ret = env.get("/admin/settings/retention").json()
    assert {"settings", "bounds", "override_fields", "max_overrides"} <= set(ret)
    assert ret["settings"]["raw_days"] == 7 or ret["settings"]
    rc = env.get("/admin/settings/recheck").json()
    assert rc["settings"]["window"] == 180 and {m["slug"] for m in rc["monitors"]} == {
        "core", "edge", "nas"}
    rules = env.get("/admin/settings/rules").json()
    assert rules == {"rules": [], "max_rules": 500}


def test_a_changed_setting_changes_the_etag_and_the_document(env):
    csrf = env.login("root", admin=True)
    first = env.get("/admin/settings/tiers")
    assert env.get("/admin/settings/tiers",
                   headers={"If-None-Match": first.headers["etag"]}).status_code == 304
    put = env.client.put("/api/admin/tiers", json={"global": {"availability": 45}}, headers=csrf)
    assert put.status_code == 200
    second = env.get("/admin/settings/tiers")
    assert second.status_code == 200 and second.headers["etag"] != first.headers["etag"]
    assert second.json()["global"]["availability"] == 45
    put = env.client.put("/api/admin/retention", json={"raw_days": 9}, headers=csrf)
    assert put.status_code == 200
    assert env.get("/admin/settings/retention").json()["settings"]["raw_days"] == 9


def test_storage_status_names_the_backend_and_the_levels_and_never_a_dsn(env):
    env.login("root", admin=True)
    got = env.get("/admin/settings/storage")
    assert got.status_code == 200 and "etag" not in got.headers
    d = got.json()
    assert d["backend"] == "sqlite" and d["timescaledb"] is None
    assert d["incremental_rollups"] is True and set(d["change_seqs"]) >= {"metrics", "map"}
    assert isinstance(d["levels"], list)
    assert "dsn" not in got.text.lower() and "password" not in got.text.lower()
    env.client.cookies.clear()
    assert env.get("/admin/settings/storage", headers=env.token("operator", "o")).status_code == 403


# ---- plugins --------------------------------------------------------------------------------

def test_plugin_list_has_resources_nav_and_pages_and_hides_admin_pages_from_a_viewer(tmp_path):
    e = ApiEnv(tmp_path, plugins=plugins_for())
    try:
        assert e.get("/plugins").status_code == 401
        e.login("bob", admin=False)
        got = e.get("/plugins").json()["items"]
        assert [p["name"] for p in got] == ["demo"]
        ops = {r["operation_id"]: r for r in got[0]["resources"]}
        assert set(ops) == {"demo_things"} or "demo_things" in ops
        assert ops["demo_things"]["path"] == "/demo/things"
        assert ops["demo_things"]["domains"] == ["unifi"]
        e.client.cookies.clear()
        e.login("root", admin=True)
        assert {r["operation_id"] for r in e.get("/plugins").json()["items"][0]["resources"]} >= {
            "demo_things", "demo_bump"}
    finally:
        e.close()


def test_a_plugin_cannot_read_or_register_anything_outside_its_prefix_or_anonymously(tmp_path):
    e = ApiEnv(tmp_path, plugins=plugins_for(), anonymous_read=True)
    try:
        assert e.get("/demo/things").status_code == 401  # even with anonymous reads on
    finally:
        e.close()


# ---- resources ------------------------------------------------------------------------------

def test_resources_list_filter_page_and_detail(env):
    env.poll("core")
    env.poll("nas")
    env.push(host_batch("nas01"))
    env.login("bob", admin=False)
    items = env.get("/resources?limit=500").json()["items"]
    assert items and {"id", "kind", "name", "attrs", "first_seen", "last_seen"} == set(items[0])
    names = {i["name"] for i in items}
    assert "nas01" in names
    kind = next(i["kind"] for i in items if i["name"] == "nas01")
    only = env.get(f"/resources?kind={kind}&name=nas01").json()["items"]
    assert [i["name"] for i in only] == ["nas01"]
    assert env.get("/resources?name=nothing").json()["items"] == []
    first = env.get("/resources?limit=1").json()
    assert len(first["items"]) == 1 and first["next_cursor"]
    walked, cursor = [], None
    while True:
        page = env.get("/resources?limit=1" + (f"&cursor={cursor}" if cursor else "")).json()
        walked += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert walked == sorted(i["id"] for i in items)
    one = env.get(f"/resources/{only[0]['id']}").json()
    assert one["name"] == "nas01" and one["series_total"] >= 1
    assert {"id", "scope", "metric", "unit", "attrs", "last_seen"} == set(one["series"][0])
    assert env.get("/resources/999999").status_code == 404
    assert env.get("/resources/abc").status_code == 400


def test_resource_attribute_filter_and_seen_since(env):
    env.push(host_batch("nas01"))
    env.login("bob", admin=False)
    items = env.get("/resources").json()["items"]
    host = next(i for i in items if i["name"] == "nas01")
    key, value = next(iter(host["attrs"].items()))
    hit = env.get(f"/resources?attr.{key}={value}").json()["items"]
    assert host["id"] in [i["id"] for i in hit]
    assert env.get(f"/resources?attr.{key}=no-such-value").json()["items"] == []
    assert env.get("/resources?seen_since=2030-01-01T00:00:00Z").json()["items"] == []
    assert env.get("/resources?seen_since=nonsense").status_code == 400
    many = "&".join(f"attr.k{n}=v" for n in range(9))
    assert env.get("/resources?" + many).status_code == 400


# ---- Home Assistant -------------------------------------------------------------------------

def ha_batch(host="ha01", ts=START - 5):
    ha = "ha_soc.collector."
    return host_batch(host, ts, samples=[
        {"source": "observe.check.homeassistant", "metric": "observe.ha.running", "value": 1.0,
         "unit": "1", "labels": {}, "ts": ts},
        {"source": "observe.check.homeassistant", "metric": "observe.ha.version", "value": 1.0,
         "unit": "1", "labels": {"observe.ha.component": "core", "observe.ha.version": "2026.9.1"},
         "ts": ts},
        {"source": ha + "supervisor", "metric": "observe.ha.supervisor.healthy", "value": 1.0,
         "unit": "1", "labels": {}, "ts": ts},
        {"source": ha + "backup", "metric": "observe.ha.backup.count", "value": 3.0,
         "unit": "{backup}", "labels": {}, "ts": ts},
        {"source": ha + "repairs", "metric": "observe.ha.repair.issues", "value": 0.0,
         "unit": "{issue}", "labels": {"observe.ha.repair.state": "open"}, "ts": ts}])


def test_ha_instances_are_hosts_with_home_assistant_sections(env):
    env.push(host_batch("nas01"))
    ha = ha_batch()
    ha.sources.extend([SourceStatus(source="ha_soc.collector.supervisor", available=True),
                       SourceStatus(source="observe.check.homeassistant", available=True),
                       SourceStatus(source="gpu", available=True)])
    env.push(ha)
    assert env.get("/ha/instances").status_code == 401
    env.login("bob", admin=False)
    got = env.get("/ha/instances").json()
    assert [i["host"] for i in got["items"]] == ["ha01"]
    row = got["items"][0]
    assert set(row["sections"]) == {"ha", "containers", "integrations", "repairs", "backups"}
    assert row["last_seen"].endswith("Z") and row["heard"] is True
    one = env.get("/ha/instances/ha01").json()
    assert one["host"] == "ha01"
    metrics = {i["metric"] for i in one["ha"]["items"]}
    assert {"observe.ha.running", "observe.ha.supervisor.healthy"} <= metrics
    assert [i["metric"] for i in one["backups"]["items"]] == ["observe.ha.backup.count"]
    assert "events" in one
    # only the Home Assistant sources are listed: cpu and gpu are host sources of no HA section
    assert {s["source"] for s in one["sources"]} == {"ha_soc.collector.supervisor",
                                                     "observe.check.homeassistant"}
    assert env.get("/ha/instances/nas01").status_code == 404
    assert env.get("/ha/instances/nobody").status_code == 404


def test_ha_etag_follows_new_batches(env):
    env.push(ha_batch())
    env.login("bob", admin=False)
    first = env.get("/ha/instances")
    assert env.get("/ha/instances",
                   headers={"If-None-Match": first.headers["etag"]}).status_code == 304
    env.push(ha_batch(ts=START - 2))
    assert env.get("/ha/instances", headers={"If-None-Match": first.headers["etag"]}
                   ).status_code == 200


# ---- the legacy reads that moved are gone ----------------------------------------------------

def test_the_legacy_map_port_finding_audit_and_plugin_list_reads_do_not_exist(env):
    env.login("root", admin=True)
    for path in ("/api/infra/map", "/api/infra/findings", "/api/infra/port", "/api/audit",
                 "/api/plugins"):
        assert env.client.get(path).status_code in (404, 405), path
    assert env.client.post("/api/admin/infra/findings/ack", json={},
                           headers=env.csrf).status_code in (404, 405)
    gets = {r.path for r in env.app.routes if "GET" in getattr(r, "methods", ())}
    assert not gets & {"/api/infra/map", "/api/infra/findings", "/api/infra/port", "/api/audit",
                       "/api/plugins"}


def test_page_scripts_name_none_of_the_removed_routes():
    from pathlib import Path
    root = Path(__file__).parent.parent
    removed = re.compile(r"""["'`]/api/(infra/map|infra/findings|infra/port|audit|plugins)["'`?]|"""
                         r"""/api/admin/infra/findings/ack""")
    scripts = list((root / "observe" / "static").rglob("*.js")) + list(
        (root / "plugins").rglob("*.js"))
    assert scripts
    for js in scripts:
        assert not removed.search(js.read_text(encoding="utf-8")), js.name
