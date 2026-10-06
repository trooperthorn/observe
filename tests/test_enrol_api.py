"""POST /api/hosts and GET /api/hosts/{name}/enrolment: validation, access, tokens, progress."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from observe import enrol
from observe.ingest.keys import verify_key

from .test_auth import BASIC, Env

FIXTURES = Path(__file__).parent / "fixtures" / "hostwatch"
GOOD = {"name": "nas01", "platform": "truenas", "agent": True, "control": True,
        "allowlist": {"fans": ["fan1", {"header": "fan2", "min_duty_limit": 20}],
                      "services": ["smbd", "docker:scrutiny"], "reboot": True}}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def admin(env, name="root"):
    env.user(name, admin=True)
    return env.csrf(env.login(name))


def create(env, hdr, **over):
    return env.client.post("/api/hosts", json={**GOOD, **over}, headers=hdr)


def token_of(resp) -> str:
    return re.search(r"/i/(wpe_[A-Za-z0-9_-]+)", resp.json()["command"]).group(1)


def audit_kinds(env):
    return [r[0] for r in env.rows("SELECT kind FROM audit ORDER BY id")]


def redeem(env, token, at=None):
    return asyncio.run(enrol.redeem(env.store, token, env.clock() if at is None else at,
                                    "127.0.0.1"))


def run_install(env, token, params=None):
    """What a host does once its script's guards pass: fetch the script, then redeem the token.

    The response carries the script text followed by the keys the redeem reply gave, written the
    way the script holds them (KEY='value'), so tests read them as they always did. A fetch
    that is not a 200 is returned as it is, and nothing is redeemed."""
    got = env.client.get(f"/i/{token}", params=params)
    if got.status_code != 200:
        return got
    done = env.client.post("/api/enrol/redeem", json={"token": token})
    assert done.status_code == 200, done.text
    reply = done.json()
    lines = "".join(f"{name}='{reply[field]}'\n" for name, field in (
        ("STEP_KEY", "step_key"), ("AGENT_KEY", "agent_key"), ("CONTROL_KEY", "control_key"))
        if reply.get(field))
    return SimpleNamespace(status_code=200, headers=got.headers, text=got.text + "\n" + lines)


def ingest_for(env, host, key):
    body = json.loads((FIXTURES / "batch_minimal.json").read_text(encoding="utf-8"))
    body["host"] = host
    return env.client.post("/api/ingest", json=body, headers={"Authorization": f"Bearer {key}"})


def test_create_returns_headed_command_and_stores_only_a_digest(env):
    hdr = admin(env)
    r = create(env, hdr)
    assert r.status_code == 200
    out = r.json()
    assert r.headers["cache-control"] == "no-store"
    assert out["host"] == "nas01" and out["platform"] == "truenas" and out["ttl_s"] == 1800
    first, second = out["command"].split("\n")
    assert first.startswith("# Observe install for nas01 (TrueNAS)")
    assert "30 minutes" in first
    assert second.startswith("curl -fsSL 'https://testserver/i/wpe_") and second.endswith("| sudo sh")
    token = token_of(r)
    stored = repr(env.rows("SELECT * FROM enrolments"))
    assert token not in stored and token.split("_", 1)[1] not in stored
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]  # keys wait for redemption


def test_windows_gets_a_powershell_command_and_control_is_refused(env):
    hdr = admin(env)
    ok = create(env, hdr, name="win01", platform="windows", control=False, allowlist=None)
    assert ok.status_code == 200
    head, line = ok.json()["command"].split("\n")
    assert "win01 (Windows)" in head and line.startswith("irm 'https://testserver/i/wpe_")
    no = create(env, hdr, name="win02", platform="windows", control=True, allowlist=None)
    assert no.status_code == 422 and "Windows" in no.json()["detail"]
    assert env.rows("SELECT host FROM enrolments") == [("win01",)]


@pytest.mark.parametrize("over", [
    {"name": ""}, {"name": "nas01\n"}, {"name": "NAS01"}, {"name": "-nas"}, {"name": "nas_01"}, {"name": "a b"},
    {"name": "x" * 64}, {"name": "nas01/../x"}, {"name": 5},
    {"platform": "freebsd"}, {"platform": None},
    {"agent": "yes"}, {"control": 1}, {"agent": False, "control": False},
    {"allowlist": {"fans": ["fan1; rm -rf /"]}}, {"allowlist": {"fans": ["$(id)"]}},
    {"allowlist": {"fans": ["a b"]}}, {"allowlist": {"fans": [""]}},
    {"allowlist": {"fans": [{"header": "f", "min_duty_limit": 101}]}},
    {"allowlist": {"fans": [{"header": "f", "min_duty_limit": "5"}]}},
    {"allowlist": {"fans": [{"header": "f", "extra": 1}]}},
    {"allowlist": {"services": ["smbd`id`"]}}, {"allowlist": {"services": ["a|b"]}},
    {"allowlist": {"services": ["a\nb"]}}, {"allowlist": {"services": ["a'b"]}},
    {"allowlist": {"services": ["smbd\n"]}}, {"allowlist": {"fans": ["fan\n"]}},
    {"allowlist": {"fans": [{"header": "fan\n"}]}},
    {"allowlist": {"services": ["x" * 129]}}, {"allowlist": {"services": "smbd"}},
    {"allowlist": {"fans": [f"f{i}" for i in range(33)]}},
    {"allowlist": {"reboot": "true"}}, {"allowlist": {"other": []}}, {"allowlist": []},
    {"control": False},  # an allowlist without control
])
def test_invalid_requests_are_refused_and_audited(env, over):
    hdr = admin(env)
    r = create(env, hdr, **over)
    assert r.status_code == 422
    assert env.rows("SELECT COUNT(*) FROM enrolments") == [(0,)]
    assert audit_kinds(env).count("enrol_create_failed") == 1
    assert "enrol_created" not in audit_kinds(env)


def test_non_object_and_bad_json_bodies_are_refused(env):
    hdr = admin(env)
    assert env.client.post("/api/hosts", json=[1], headers=hdr).status_code == 422
    assert env.client.post("/api/hosts", content=b"{", headers=hdr).status_code == 422


def test_duplicates_are_refused(env):
    hdr = admin(env)
    assert create(env, hdr).status_code == 200
    again = create(env, hdr)
    assert again.status_code == 409
    assert env.rows("SELECT COUNT(*) FROM enrolments") == [(1,)]
    # A host that already reports is refused too, even with no enrolment.
    token = token_of(create(env, hdr, name="seen", control=False, allowlist=None))
    keys = redeem(env, token)
    assert ingest_for(env, "seen", keys.agent_key).status_code == 200
    db = sqlite3.connect(env.path)
    db.execute("DELETE FROM enrolments WHERE host='seen'")
    db.commit()
    db.close()
    assert create(env, hdr, name="seen", control=False, allowlist=None).status_code == 409


def test_admin_only_and_basic_auth_refused(env):
    for method, path in [("POST", "/api/hosts"), ("GET", "/api/hosts/nas01/enrolment")]:
        assert env.client.request(method, path).status_code == 401
        assert env.client.request(method, path, headers=BASIC).status_code == 401
    env.user("bob")
    bob = env.csrf(env.login("bob"))
    assert env.client.post("/api/hosts", json=GOOD, headers=bob).status_code == 403
    assert env.client.get("/api/hosts/nas01/enrolment").status_code == 403
    assert env.rows("SELECT COUNT(*) FROM enrolments") == [(0,)]


def test_create_needs_csrf(env):
    admin(env)
    assert env.client.post("/api/hosts", json=GOOD).status_code == 403
    assert env.client.post("/api/hosts", json=GOOD,
                           headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert env.rows("SELECT COUNT(*) FROM enrolments") == [(0,)]


def test_enrolment_route_is_not_shadowed_by_the_host_page_route(env):
    hdr = admin(env)
    create(env, hdr)
    r = env.client.get("/api/hosts/nas01/enrolment")
    assert r.status_code == 200 and r.json()["host"] == "nas01"
    assert env.client.get("/api/hosts/ghost/enrolment").status_code == 404


def test_token_is_single_use_and_mints_bound_keys(env):
    hdr = admin(env)
    token = token_of(create(env, hdr))
    got = redeem(env, token)
    assert got.host == "nas01" and got.agent_key.startswith("wpi_")
    assert got.control_key.startswith("wpc_")
    assert got.allowlist["services"] == ["smbd", "docker:scrutiny"]
    assert got.allowlist["fans"][1] == {"header": "fan2", "min_duty_limit": 20}
    assert asyncio.run(verify_key(env.store, got.agent_key, "nas01")) is True
    assert asyncio.run(verify_key(env.store, got.agent_key, "other")) is False
    assert asyncio.run(verify_key(env.store, got.control_key, "nas01", scope="wpc")) is True
    assert asyncio.run(verify_key(env.store, got.control_key, "nas01")) is False
    assert redeem(env, token) is None  # second fetch
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(2,)]
    assert redeem(env, "wpe_nonsense") is None and redeem(env, "garbage") is None


def test_agent_only_mints_no_control_key(env):
    hdr = admin(env)
    token = token_of(create(env, hdr, control=False, allowlist=None))
    got = redeem(env, token)
    assert got.control_key is None
    assert env.rows("SELECT scope FROM ingest_keys") == [("wpi",)]


def test_token_expires_after_thirty_minutes(env):
    hdr = admin(env)
    token = token_of(create(env, hdr))
    t0 = env.clock()
    assert redeem(env, token, t0 + 1800) is None  # exactly at expiry
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]
    token2 = token_of(create(env, hdr, name="nas02"))
    assert redeem(env, token2, t0 + 1799) is not None


def test_progress_follows_fake_ingest_and_control_pull(env):
    hdr = admin(env)
    token = token_of(create(env, hdr))

    def steps():
        r = env.client.get("/api/hosts/nas01/enrolment").json()
        return r["state"], {s["id"]: s["status"] for s in r["steps"]}

    assert steps() == ("waiting", {"script": "waiting", "data": "waiting",
                                   "control": "waiting", "ready": "waiting"})
    got = redeem(env, token)
    assert steps()[0] == "script_fetched"
    assert ingest_for(env, "nas01", got.agent_key).status_code == 200
    state, st = steps()
    assert state == "first_data" and st["data"] == "done" and st["control"] == "waiting"
    assert asyncio.run(verify_key(env.store, got.control_key, "nas01", scope="wpc"))
    state, st = steps()
    assert state == "ready" and set(st.values()) == {"done"}
    final = env.client.get("/api/hosts/nas01/enrolment").json()
    assert final["ready"] is True and final["expired"] is False
    assert all(s["at"] is not None for s in final["steps"])


def test_progress_for_agent_only_skips_control(env):
    hdr = admin(env)
    token = token_of(create(env, hdr, control=False, allowlist=None))
    got = redeem(env, token)
    ingest_for(env, "nas01", got.agent_key)
    r = env.client.get("/api/hosts/nas01/enrolment").json()
    assert r["state"] == "ready"
    assert {s["id"]: s["status"] for s in r["steps"]}["control"] == "skipped"


def test_control_pull_before_data_reports_control_pulled(env):
    hdr = admin(env)
    got = redeem(env, token_of(create(env, hdr)))
    asyncio.run(verify_key(env.store, got.control_key, "nas01", scope="wpc"))
    assert env.client.get("/api/hosts/nas01/enrolment").json()["state"] == "control_pulled"


def test_expiry_is_reported_and_audited_once(env):
    hdr = admin(env)
    create(env, hdr)
    env.clock.now += 1800
    for _ in range(3):
        r = env.client.get("/api/hosts/nas01/enrolment").json()
        assert r["state"] == "expired" and r["expired"] is True and r["ready"] is False
        assert r["steps"][0]["status"] == "expired"
    assert audit_kinds(env).count("enrol_expired") == 1


def test_a_fetched_enrolment_never_reports_expired(env):
    hdr = admin(env)
    redeem(env, token_of(create(env, hdr)))
    env.clock.now += 5000
    env.login("root")  # the idle session timed out; the enrolment has not changed
    r = env.client.get("/api/hosts/nas01/enrolment").json()
    assert r["state"] == "script_fetched" and r["expired"] is False
    assert "enrol_expired" not in audit_kinds(env)


def test_audit_rows_for_create_fetch_and_failed_fetch(env):
    hdr = admin(env)
    token = token_of(create(env, hdr))
    redeem(env, token)
    redeem(env, token)
    assert audit_kinds(env) == ["login_ok", "enrol_created", "enrol_fetched",
                                "enrol_fetch_failed"]
    created = env.rows("SELECT actor, status, detail FROM audit WHERE kind='enrol_created'")[0]
    assert created[0] == "root" and created[1] == 200
    assert json.loads(created[2]) == {"host": "nas01", "platform": "truenas", "agent": True,
                                      "control": True, "reboot": True, "fans": 2, "services": 2}


def test_token_and_keys_never_reach_logs_or_audit(env, caplog):
    caplog.set_level(logging.DEBUG)
    hdr = admin(env)
    resp = create(env, hdr)
    token = token_of(resp)
    secret = token.split("_", 1)[1]
    got = redeem(env, token)
    redeem(env, token)
    ingest_for(env, "nas01", got.agent_key)
    progress = env.client.get("/api/hosts/nas01/enrolment")
    env.clock.now += 1800
    env.client.get("/api/hosts/nas01/enrolment")
    audit_dump = " ".join(str(x) for row in env.rows("SELECT * FROM audit") for x in row)
    enrol_dump = repr(env.rows("SELECT * FROM enrolments"))
    secrets_ = [token, secret, got.agent_key, got.agent_key.split("_", 2)[2],
                got.control_key, got.control_key.split("_", 2)[2]]
    for s in secrets_:
        assert s not in audit_dump and s not in caplog.text and s not in enrol_dump
        assert s not in progress.text
    # The token is also not in any later API response.
    for path in ("/api/audit", "/api/admin/keys", "/api/hosts"):
        assert token not in env.client.get(path).text


def test_audit_redaction_covers_token_shapes():
    from observe.audit import redact_secrets
    assert "wpe_abc" not in redact_secrets("GET /i/wpe_abc")
    assert "wpc_abc" not in redact_secrets("key wpc_abc_def")
