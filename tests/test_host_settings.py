"""Host settings (docs/GUI-DESIGN.md section 3.11, slice S13): the settings API, the allowlist
save with its update command, applied or pending status, reissue of the install command, the
cleanup command and the danger zone."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from observe import hosttasks
from observe.ingest.keys import verify_key

from .dbq import SAMPLE_ROWS
from .test_auth import BASIC
from .test_enrol_api import GOOD, admin, audit_kinds, create, ingest_for, run_install, token_of
from .test_install_script import STEP_KEY, Served, check_syntax, env  # noqa: F401

PUT_BODY = {"fans": [{"header": "fan1", "min_duty_limit": 20}, "fan9"], "services": ["smbd"],
            "reboot": False, "confirmed": True}


def settings(env, host="nas01"):
    return env.client.get(f"/api/v2/hosts/{host}/settings")


def put(env, hdr, host="nas01", **over):
    body = {**PUT_BODY, **over}
    return env.client.put(f"/api/hosts/{host}/allowlist", json=body, headers=hdr)


def enrol_host(env, hdr, name="nas01", platform="linux", fetch=True, **over):
    """Create an enrolment and, by default, run its install script. Returns the script text, which
    holds the keys, the way a real host sees it."""
    resp = create(env, hdr, name=name, platform=platform, **over)
    assert resp.status_code == 200, resp.text
    token = token_of(resp)
    return run_install(env, token).text if fetch else token


def secrets_of(script):
    found = {k: re.search(rf"{k}='([^']+)'", script) for k in ("STEP_KEY", "AGENT_KEY", "CONTROL_KEY")}
    return {k: m.group(1) for k, m in found.items() if m}


def task_token(resp):
    return re.search(r"/t/(wpt_[A-Za-z0-9_-]+)", resp.json()["command"]).group(1)


def pull(env, script, at, host="nas01"):
    """What the control daemon's first authenticated pull does to its key."""
    key = secrets_of(script)["CONTROL_KEY"]
    assert asyncio.run(verify_key(env.store, key, host, now=at, scope="wpc"))


def step(env, key, name, status="ok", note="x"):
    return env.client.post("/api/enrol/step", json={"step": name, "status": status, "note": note},
                           headers={"Authorization": f"Bearer {key}"})


# ---------------------------------------------------------------- reading the settings


def test_settings_are_admin_only_session_only_and_hold_no_secret(env):
    hdr = admin(env)
    script = enrol_host(env, hdr)
    body = settings(env).json()
    assert body["host"] == "nas01" and body["enrolled"] and body["installed"]
    assert body["platform"] == "linux" and body["platform_label"] == "Linux server"
    assert body["control"] and body["can_update"] and body["can_cleanup"]
    assert body["allowlist"]["fans"] == [{"header": "fan1"}, {"header": "fan2", "min_duty_limit": 20}]
    assert body["active_keys"] == 2 and body["ttl_s"] == 1800
    dump = json.dumps(body)
    for secret in secrets_of(script).values():
        assert secret not in dump and secret.split("_", 1)[1] not in dump
    env.client.post("/api/logout", headers=hdr)
    assert settings(env).status_code in (401, 403)
    env.user("bob")
    env.login("bob")
    assert settings(env).status_code == 403
    env.client.cookies.clear()
    assert env.client.get("/api/v2/hosts/nas01/settings", headers=BASIC).status_code in (401, 403)


def test_an_unknown_host_is_404_and_a_host_outside_the_console_is_readable(env):
    admin(env)
    assert settings(env, "ghost").status_code == 404
    asyncio.run(env.store.execute(
        "INSERT INTO hosts (host, platform, agent_version, first_seen, last_seen) "
        "VALUES ('legacy', 'Linux', '1.0', 1.0, 2.0)"))
    body = settings(env, "legacy").json()
    assert body["enrolled"] is False and body["reporting"] and body["allowlist"] is None
    assert body["can_update"] is False and body["task"] is None


# ---------------------------------------------------------------- the allowlist


def test_save_after_the_install_makes_a_headed_update_command(env):
    hdr = admin(env)
    enrol_host(env, hdr)
    r = put(env, hdr)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    out = r.json()
    assert out["saved"] and out["rev"] == 1 and out["kind"] == "update"
    head, line = out["command"].split("\n")
    assert head.startswith("# Observe settings update for nas01 (Linux server). Run this on nas01 only.")
    assert "30 minutes" in head
    assert line.startswith("curl -fsSL 'https://192.0.2.50:8443/t/wpt_") and line.endswith("| sudo sh")
    assert out["allowlist"] == {"fans": [{"header": "fan1", "min_duty_limit": 20}, {"header": "fan9"}],
                                "services": ["smbd"], "reboot": False}
    assert out["allowlist_status"]["state"] == "pending"
    stored = json.loads(env.rows("SELECT allowlist FROM enrolments")[0][0])
    assert stored == out["allowlist"]
    token = task_token(r)
    assert token not in repr(env.rows("SELECT * FROM host_tasks"))
    assert token not in repr(env.rows("SELECT * FROM audit"))
    assert {"host_allowlist_saved", "host_task_created"} <= set(audit_kinds(env))


def test_save_before_the_install_is_run_makes_no_command_and_lands_in_the_install(env):
    hdr = admin(env)
    token = enrol_host(env, hdr, fetch=False)
    env.clock.now += 10
    out = put(env, hdr).json()
    assert out["saved"] and out["command"] is None and "command_error" not in out
    assert out["allowlist_status"]["state"] == "pending"
    assert env.rows("SELECT COUNT(*) FROM host_tasks") == [(0,)]
    env.clock.now += 10
    script = run_install(env, token).text
    assert "'headers = [\"fan1\", \"fan9\"]'" in script and "min_duty_floor = 20" in script
    assert "fan2" not in script and "docker:scrutiny" not in script
    # The install wrote the saved list, so the status moves on once the host has pulled.
    assert settings(env).json()["allowlist_status"]["state"] == "written"


@pytest.mark.parametrize("over", [
    {"fans": ["fan1; rm -rf /"]}, {"fans": ["$(id)"]}, {"fans": ["a b"]}, {"fans": [""]},
    {"fans": [{"header": "f", "min_duty_limit": 101}]}, {"fans": [{"header": "f", "extra": 1}]},
    {"services": ["smbd`id`"]}, {"services": ["a|b"]}, {"services": ["a\nb"]},
    {"services": ["smbd\n"]}, {"services": ["a..b"]}, {"services": "smbd"},
    {"fans": [f"f{i}" for i in range(33)]}, {"reboot": "true"}, {"other": []}])
def test_invalid_allowlists_are_refused_and_change_nothing(env, over):
    hdr = admin(env)
    enrol_host(env, hdr)
    before = env.rows("SELECT allowlist, allowlist_rev FROM enrolments")
    r = put(env, hdr, **over)
    assert r.status_code == 422
    assert env.rows("SELECT allowlist, allowlist_rev FROM enrolments") == before
    assert env.rows("SELECT COUNT(*) FROM host_tasks") == [(0,)]
    assert audit_kinds(env).count("host_allowlist_failed") == 1


def test_save_needs_confirmation_a_change_control_and_an_enrolment(env):
    hdr = admin(env)
    enrol_host(env, hdr)
    for bad in (None, "true", 1, "yes", False):
        body = {**PUT_BODY, "confirmed": bad}
        assert env.client.put("/api/hosts/nas01/allowlist", json=body, headers=hdr).status_code == 400
    same = {"fans": ["fan1", {"header": "fan2", "min_duty_limit": 20}],
            "services": ["smbd", "docker:scrutiny"], "reboot": True, "confirmed": True}
    r = env.client.put("/api/hosts/nas01/allowlist", json=same, headers=hdr)
    assert r.status_code == 409 and "unchanged" in r.json()["detail"]
    assert env.rows("SELECT allowlist_rev FROM enrolments") == [(0,)]
    enrol_host(env, hdr, name="agent01", control=False, allowlist=None)
    r = put(env, hdr, host="agent01")
    assert r.status_code == 409 and "control was not chosen" in r.json()["detail"]
    assert put(env, hdr, host="ghost").status_code == 404
    assert env.client.put("/api/hosts/nas01/allowlist", content=b"{", headers=hdr).status_code == 400


def test_save_is_admin_only_with_csrf(env):
    hdr = admin(env)
    enrol_host(env, hdr)
    assert env.client.put("/api/hosts/nas01/allowlist", json=PUT_BODY).status_code in (401, 403)
    env.client.post("/api/logout", headers=hdr)
    env.user("bob")
    bob = env.csrf(env.login("bob"))
    assert put(env, bob).status_code == 403
    assert env.client.put("/api/hosts/nas01/allowlist", json=PUT_BODY, headers={
        "X-CSRF-Token": "wrong"}).status_code == 403
    assert env.rows("SELECT allowlist_rev FROM enrolments") == [(0,)]


def test_a_newer_update_command_kills_the_older_unfetched_one(env):
    hdr = admin(env)
    enrol_host(env, hdr)
    first = task_token(put(env, hdr))
    second = task_token(put(env, hdr, services=["smbd", "nfs-server"]))
    assert env.client.get(f"/t/{first}").status_code == 410
    assert env.client.get(f"/t/{second}").status_code == 200
    assert env.rows("SELECT COUNT(*) FROM host_tasks") == [(1,)]


def test_without_a_control_plugin_the_allowlist_is_saved_but_no_command_is_made(tmp_path):
    from observe.plugins import LoadedPlugins
    e = Served(tmp_path, plugins=LoadedPlugins())
    try:
        hdr = admin(e)
        e.client.post("/api/hosts", json={**GOOD, "platform": "linux"}, headers=hdr)
        asyncio.run(e.store.execute("UPDATE enrolments SET fetched_at=1.0"))
        out = put(e, hdr).json()
        assert out["saved"] and out["command"] is None
        assert "control is not set up" in out["command_error"]
        assert e.rows("SELECT COUNT(*) FROM host_tasks") == [(0,)]
    finally:
        e.client.close()
        e.store.close()


# ---------------------------------------------------------------- the update command


def test_fetching_the_update_command_serves_a_guarded_script_once(env, tmp_path):
    hdr = admin(env)
    enrol_host(env, hdr)
    token = task_token(put(env, hdr))
    r = env.client.get(f"/t/{token}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"].startswith("text/x-shellscript")
    text = r.text
    assert text.startswith("#!/bin/sh\n# Observe settings update for nas01 (Linux server)")
    check_syntax(text, tmp_path)
    assert "HOST_NAME='nas01'" in text and "OBSERVE_MACHINE_ID='0123456789abcdef0123456789abcdef'" in text
    assert "OBSERVE_ADDRS='192.0.2.50'" in text and "'headers = [\"fan1\", \"fan9\"]'" in text
    assert "wpi_" not in text and "wpc_" not in text
    again = env.client.get(f"/t/{token}")
    assert again.status_code == 410 and "wps_" not in again.text
    assert audit_kinds(env).count("host_task_fetched") == 1
    assert audit_kinds(env).count("host_task_fetch_failed") == 1
    assert token not in repr(env.rows("SELECT * FROM audit"))


def test_expired_unknown_and_wrong_kind_tokens_are_410(env):
    hdr = admin(env)
    script = enrol_host(env, hdr)
    token = task_token(put(env, hdr))
    asyncio.run(env.store.execute("UPDATE host_tasks SET expires_at=?", (env.clock() - 1,)))
    install_token = secrets_of(script)["STEP_KEY"]
    for t in (token, "wpt_nonsense", "garbage", install_token):
        assert env.client.get(f"/t/{t}").status_code == 410
    # An install token is not a task token, and a task token is not an install token.
    fresh = task_token(put(env, hdr, services=["smbd", "nfs-server"]))
    assert env.client.get(f"/i/{fresh}").status_code == 410


def test_a_hostile_host_header_never_reaches_a_task_command_or_script(env):
    hdr = admin(env)
    enrol_host(env, hdr)
    hostile = {"Host": "bad_host.lan:1;id"}
    made = put(env, {**hdr, **hostile})
    assert made.status_code == 200 and "bad_host" not in made.json()["command"]
    assert "'https://192.0.2.50:8443/t/wpt_" in made.json()["command"]
    token = task_token(made)
    got = env.client.get(f"/t/{token}", headers=hostile)
    assert got.status_code == 200 and "bad_host" not in got.text


def test_task_step_reports_need_the_task_step_key_and_set_the_task_state(env):
    hdr = admin(env)
    enrol_host(env, hdr)
    token = task_token(put(env, hdr))
    assert settings(env).json()["task"]["state"] == "waiting"
    key = secrets_of(env.client.get(f"/t/{token}").text)["STEP_KEY"]
    assert settings(env).json()["task"]["state"] == "fetched"
    assert step(env, "wps_wrong", "done").status_code == 401
    assert step(env, key, "control_config", note=f"leaked {key}").status_code == 204
    assert settings(env).json()["task"]["state"] == "fetched"
    assert step(env, key, "done").status_code == 204
    task = settings(env).json()["task"]
    assert task["state"] == "done" and task["kind"] == "update"
    assert [r["step"] for r in task["install"]] == ["control_config", "done"]
    dump = json.dumps(task) + repr(env.rows("SELECT * FROM host_tasks")) + repr(
        env.rows("SELECT * FROM audit"))
    assert key not in dump and key.split("_", 1)[1] not in dump
    # A failed or refused step marks the task failed.
    second = task_token(put(env, hdr, services=["nfs-server"]))
    key2 = secrets_of(env.client.get(f"/t/{second}").text)["STEP_KEY"]
    assert step(env, key2, "hostname", "refused", "hostname does not match").status_code == 204
    assert settings(env).json()["task"]["state"] == "failed"
    assert "enrol_install_problem" in audit_kinds(env)


def test_an_unfetched_task_expires_and_the_expiry_is_audited_once(env):
    hdr = admin(env)
    enrol_host(env, hdr)
    put(env, hdr)
    asyncio.run(env.store.execute("UPDATE host_tasks SET expires_at=?", (env.clock() - 1,)))
    assert settings(env).json()["task"]["state"] == "expired"
    assert settings(env).json()["task"]["state"] == "expired"
    assert audit_kinds(env).count("host_task_expired") == 1


# ---------------------------------------------------------------- applied or pending


def test_status_is_pending_then_written_then_applied_after_a_control_pull(env):
    hdr = admin(env)
    script = enrol_host(env, hdr)
    # The first install: applied once the control daemon has pulled.
    assert settings(env).json()["allowlist_status"]["state"] == "written"
    env.clock.now += 10
    pull(env, script, env.clock())
    first = settings(env).json()["allowlist_status"]
    assert first["state"] == "applied" and first["applied_at"] == env.clock()
    env.clock.now += 10
    token = task_token(put(env, hdr))
    status = settings(env).json()["allowlist_status"]
    assert status["state"] == "pending" and status["rev"] == 1 and status["applied_at"] is None
    # Fetching the script is not enough. Only the script's own report that control.toml was
    # written moves it on.
    env.clock.now += 10
    key = secrets_of(env.client.get(f"/t/{token}").text)["STEP_KEY"]
    assert settings(env).json()["allowlist_status"]["state"] == "pending"
    env.clock.now += 5
    assert step(env, key, "control_config").status_code == 204
    assert settings(env).json()["allowlist_status"]["state"] == "pending"
    env.clock.now += 5
    assert step(env, key, "control_unit").status_code == 204
    written = settings(env).json()["allowlist_status"]
    assert written["state"] == "written" and written["written_at"] == env.clock()
    # A pull from before the write does not count. The next pull does.
    env.clock.now += 5
    pull(env, script, env.clock())
    done = settings(env).json()["allowlist_status"]
    assert done["state"] == "applied" and done["applied_at"] == env.clock()


def test_a_pull_older_than_the_write_leaves_the_status_at_written(env):
    hdr = admin(env)
    script = enrol_host(env, hdr)
    env.clock.now += 10
    pull(env, script, env.clock())
    env.clock.now += 10
    token = task_token(put(env, hdr))
    key = secrets_of(env.client.get(f"/t/{token}").text)["STEP_KEY"]
    env.clock.now += 10
    step(env, key, "control_config")
    step(env, key, "control_unit")
    assert settings(env).json()["allowlist_status"]["state"] == "written"


def test_status_has_no_control_state_for_an_agent_only_host(env):
    hdr = admin(env)
    enrol_host(env, hdr, name="agent01", control=False, allowlist=None)
    assert settings(env, "agent01").json()["allowlist_status"]["state"] == "none"


# ---------------------------------------------------------------- reissue


def test_reissue_revokes_the_old_token_and_keys_and_makes_a_new_command(env):
    hdr = admin(env)
    old = enrol_host(env, hdr)
    keys = secrets_of(old)
    assert asyncio.run(verify_key(env.store, keys["AGENT_KEY"], "nas01"))
    assert ingest_for(env, "nas01", keys["AGENT_KEY"]).status_code == 200
    put(env, hdr)
    r = env.client.post("/api/hosts/nas01/enrolment/reissue", json={"confirmed": True}, headers=hdr)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    out = r.json()
    assert out["keys_revoked"] == 2 and out["host"] == "nas01"
    head, line = out["command"].split("\n")
    assert head.startswith("# Observe install for nas01 (Linux server). Run this on nas01 only.")
    assert ingest_for(env, "nas01", keys["AGENT_KEY"]).status_code == 401
    assert not asyncio.run(verify_key(env.store, keys["CONTROL_KEY"], "nas01", scope="wpc"))
    assert env.rows("SELECT COUNT(*) FROM ingest_keys WHERE revoked_at IS NULL") == [(0,)]
    # The unfetched update command made for the old install is dead as well.
    assert env.rows("SELECT COUNT(*) FROM host_tasks") == [(0,)]
    token = re.search(r"/i/(wpe_[A-Za-z0-9_-]+)", line).group(1)
    fresh = run_install(env, token)
    assert fresh.status_code == 200
    new_keys = secrets_of(fresh.text)
    assert new_keys["AGENT_KEY"] != keys["AGENT_KEY"]
    assert "'headers = [\"fan1\", \"fan9\"]'" in fresh.text  # the saved allowlist, not the first
    assert asyncio.run(verify_key(env.store, new_keys["AGENT_KEY"], "nas01"))
    assert env.client.get("/i/" + token).status_code == 410
    assert "enrol_reissued" in audit_kinds(env)
    assert token not in repr(env.rows("SELECT * FROM audit"))
    assert keys["STEP_KEY"] not in repr(env.rows("SELECT * FROM enrolments"))
    # The old install's step key stopped working with the reissue.
    assert step(env, keys["STEP_KEY"], "done").status_code == 401


def test_reissue_resets_the_progress_so_old_data_does_not_count(env):
    hdr = admin(env)
    old = enrol_host(env, hdr)
    assert ingest_for(env, "nas01", secrets_of(old)["AGENT_KEY"]).status_code == 200
    assert env.client.get("/api/v2/hosts/nas01/enrolment").json()["ready"] is False
    env.clock.now += 100
    env.client.post("/api/hosts/nas01/enrolment/reissue", json={"confirmed": True}, headers=hdr)
    state = env.client.get("/api/v2/hosts/nas01/enrolment").json()
    assert [s["status"] for s in state["steps"]] == ["waiting"] * 4 and not state["ready"]


def test_reissue_needs_confirmation_an_enrolment_and_admin_csrf(env):
    hdr = admin(env)
    enrol_host(env, hdr)
    url = "/api/hosts/nas01/enrolment/reissue"
    for bad in ({}, {"confirmed": "true"}, {"confirmed": 1}):
        assert env.client.post(url, json=bad, headers=hdr).status_code == 400
    assert env.client.post(url, json={"confirmed": True}).status_code in (401, 403)
    assert env.client.post("/api/hosts/ghost/enrolment/reissue", json={"confirmed": True},
                           headers=hdr).status_code == 404
    assert env.rows("SELECT COUNT(*) FROM ingest_keys WHERE revoked_at IS NULL") == [(2,)]
    env.client.post("/api/logout", headers=hdr)
    env.user("bob")
    assert env.client.post(url, json={"confirmed": True}, headers=env.csrf(env.login("bob"))
                           ).status_code == 403


def test_regenerate_before_the_script_is_run_still_works_and_after_it_is_refused(env):
    hdr = admin(env)
    token = enrol_host(env, hdr, fetch=False)
    r = env.client.post("/api/hosts/nas01/enrolment/regenerate", json={}, headers=hdr)
    assert r.status_code == 200
    assert env.client.get(f"/i/{token}").status_code == 410
    new = re.search(r"/i/(wpe_[A-Za-z0-9_-]+)", r.json()["command"]).group(1)
    assert run_install(env, new).status_code == 200
    assert env.client.post("/api/hosts/nas01/enrolment/regenerate", json={},
                           headers=hdr).status_code == 404


# ---------------------------------------------------------------- tasks and cleanup


def test_cleanup_command_is_headed_and_needs_an_install_and_confirmation(env, tmp_path):
    hdr = admin(env)
    enrol_host(env, hdr, fetch=False)
    url = "/api/hosts/nas01/tasks"
    r = env.client.post(url, json={"kind": "cleanup", "confirmed": True}, headers=hdr)
    assert r.status_code == 409 and "has not been run" in r.json()["detail"]
    asyncio.run(env.store.execute("UPDATE enrolments SET fetched_at=1.0"))
    for bad in ({"kind": "cleanup"}, {"kind": "cleanup", "confirmed": "true"},
                {"kind": "reformat", "confirmed": True}, {"confirmed": True}):
        assert env.client.post(url, json=bad, headers=hdr).status_code in (400, 422)
    r = env.client.post(url, json={"kind": "cleanup", "confirmed": True}, headers=hdr)
    assert r.status_code == 200 and r.json()["kind"] == "cleanup"
    head, line = r.json()["command"].split("\n")
    assert head.startswith("# Observe cleanup for nas01 (Linux server). Run this only on the machine")
    assert "refuses any other machine" in head
    script = env.client.get(f"/t/{task_token(r)}")
    assert script.status_code == 200
    check_syntax(script.text, tmp_path)
    assert script.text.startswith("#!/bin/sh\n# Observe cleanup for nas01 (Linux server).")
    assert "host_task_created" in audit_kinds(env)


def test_cleanup_does_not_revoke_keys_and_leaves_the_enrolment_alone(env):
    hdr = admin(env)
    script = enrol_host(env, hdr)
    r = env.client.post("/api/hosts/nas01/tasks", json={"kind": "cleanup", "confirmed": True},
                        headers=hdr)
    env.client.get(f"/t/{task_token(r)}")
    assert asyncio.run(verify_key(env.store, secrets_of(script)["AGENT_KEY"], "nas01"))
    assert env.rows("SELECT COUNT(*) FROM enrolments") == [(1,)]


@pytest.mark.parametrize("platform", ["truenas", "windows"])
def test_cleanup_exists_for_the_agent_only_platforms_and_update_does_not(env, platform):
    hdr = admin(env)
    enrol_host(env, hdr, name="box01", platform=platform, control=False, allowlist=None)
    body = settings(env, "box01").json()
    assert body["can_cleanup"] is True and body["can_update"] is False
    r = env.client.post("/api/hosts/box01/tasks", json={"kind": "cleanup", "confirmed": True},
                        headers=hdr)
    assert r.status_code == 200 and r.json()["platform"] == platform
    if platform == "windows":
        assert r.json()["command"].split("\n")[1].startswith("irm 'https://192.0.2.50:8443/t/wpt_")
    got = env.client.get(f"/t/{task_token(r)}")
    assert got.status_code == 200
    assert "Observe cleanup for box01" in got.text
    nope = env.client.post("/api/hosts/box01/tasks", json={"kind": "update", "confirmed": True},
                           headers=hdr)
    assert nope.status_code == 409


def test_a_task_with_an_old_host_progress_is_shown_in_the_settings(env):
    hdr = admin(env)
    enrol_host(env, hdr)
    r = env.client.post("/api/hosts/nas01/tasks", json={"kind": "update", "confirmed": True},
                        headers=hdr)
    assert r.status_code == 200
    task = settings(env).json()["task"]
    assert task["kind"] == "update" and task["state"] == "waiting"
    assert STEP_KEY not in json.dumps(task)


# ---------------------------------------------------------------- the danger zone


def test_revoke_keys_needs_the_typed_name_and_only_touches_this_host(env):
    hdr = admin(env)
    mine = enrol_host(env, hdr)
    other = enrol_host(env, hdr, name="nas02")
    url = "/api/hosts/nas01/keys/revoke"
    for bad in ({}, {"confirm_host": "nas02"}, {"confirm_host": "NAS01"}, {"confirm_host": "nas01 "},
                {"confirm_host": 5}):
        assert env.client.post(url, json=bad, headers=hdr).status_code == 400
    assert env.rows("SELECT COUNT(*) FROM ingest_keys WHERE revoked_at IS NULL") == [(4,)]
    r = env.client.post(url, json={"confirm_host": "nas01"}, headers=hdr)
    assert r.status_code == 200 and r.json() == {"host": "nas01", "keys_revoked": 2}
    assert ingest_for(env, "nas01", secrets_of(mine)["AGENT_KEY"]).status_code == 401
    assert ingest_for(env, "nas02", secrets_of(other)["AGENT_KEY"]).status_code == 200
    assert env.client.post(url, json={"confirm_host": "nas01"}, headers=hdr).json()["keys_revoked"] == 0
    assert audit_kinds(env).count("host_keys_revoked") == 2
    assert audit_kinds(env).count("host_keys_revoke_failed") == 5
    env.client.post("/api/logout", headers=hdr)
    env.user("bob")
    assert env.client.post(url, json={"confirm_host": "nas01"}, headers=env.csrf(env.login("bob"))
                           ).status_code == 403


def test_a_wpf_key_with_the_same_label_is_not_touched_by_a_revoke(env):
    from observe.ingest.keys import create_key
    hdr = admin(env)
    enrol_host(env, hdr)
    asyncio.run(create_key(env.store, "nas01", scope="wpf"))
    env.client.post("/api/hosts/nas01/keys/revoke", json={"confirm_host": "nas01"}, headers=hdr)
    assert env.rows("SELECT scope FROM ingest_keys WHERE revoked_at IS NULL") == [("wpf",)]


def test_remove_deletes_the_host_and_its_data_but_keeps_the_audit_log(env):
    hdr = admin(env)
    script = enrol_host(env, hdr)
    other = enrol_host(env, hdr, name="nas02")
    assert ingest_for(env, "nas01", secrets_of(script)["AGENT_KEY"]).status_code == 200
    assert ingest_for(env, "nas02", secrets_of(other)["AGENT_KEY"]).status_code == 200
    put(env, hdr)
    assert env.rows("SELECT COUNT(*) FROM hosts WHERE host='nas01'") == [(1,)]
    url = "/api/hosts/nas01/remove"
    for bad in ({}, {"confirm_host": "nas02"}, {"confirm_host": "nas01\n"}):
        assert env.client.post(url, json=bad, headers=hdr).status_code == 400
    assert env.rows("SELECT COUNT(*) FROM enrolments WHERE host='nas01'") == [(1,)]
    stored = env.rows(f"SELECT COUNT(*) FROM {SAMPLE_ROWS} WHERE host='nas01'")[0][0]
    assert stored > 0
    r = env.client.post(url, json={"confirm_host": "nas01"}, headers=hdr)
    assert r.status_code == 200 and r.json()["removed"]["keys"] == 2
    assert r.json()["removed"]["samples"] == stored
    assert env.rows(f"SELECT COUNT(*) FROM {SAMPLE_ROWS} WHERE host='nas01'") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM resources WHERE name='nas01'") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM latest l WHERE NOT EXISTS "
                    "(SELECT 1 FROM series s WHERE s.id = l.series_id)") == [(0,)]
    assert env.rows(f"SELECT COUNT(*) FROM {SAMPLE_ROWS} WHERE host='nas02'")[0][0] > 0
    for table in ("enrolments", "hosts", "host_sources", "host_events",
                  "ingest_batches", "host_tasks"):
        assert env.rows(f"SELECT COUNT(*) FROM {table} WHERE host='nas01'") == [(0,)], table
    assert env.rows("SELECT COUNT(*) FROM ingest_keys WHERE host='nas01' AND revoked_at IS NULL"
                    ) == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM hosts WHERE host='nas02'") == [(1,)]
    assert env.rows("SELECT COUNT(*) FROM ingest_keys WHERE host='nas02' AND revoked_at IS NULL"
                    ) == [(2,)]
    kinds = audit_kinds(env)
    assert "host_removed" in kinds and "enrol_created" in kinds and "host_allowlist_saved" in kinds
    assert settings(env).status_code == 404
    assert env.client.post(url, json={"confirm_host": "nas01"}, headers=hdr).status_code == 404
    # The name is free again.
    assert create(env, hdr, platform="linux").status_code == 200


def test_a_host_listed_in_the_config_cannot_be_removed(tmp_path, monkeypatch):
    from tests import test_auth
    from tests.conftest import make_config
    from tests.test_auth import Env
    pushed = {"name": "nas01", "type": "pushed_host", "host": "nas01", "group": "storage",
              "components": [{"source": "hwmon", "metric": "cpu_temp_c", "direction": "above",
                              "warn": 70, "crit": 90}], "stale_after": 120}
    monkeypatch.setattr(test_auth, "make_config",
                        lambda monitors, **kw: make_config([*monitors, pushed], **kw))
    e = Env(tmp_path)
    try:
        hdr = admin(e)
        r = e.client.post("/api/hosts/nas01/remove", json={"confirm_host": "nas01"}, headers=hdr)
        assert r.status_code == 409 and "Observe config" in r.text
        assert e.client.post("/api/hosts/nas01/remove", json={"confirm_host": "x"},
                             headers=hdr).status_code == 400
        assert e.client.post("/api/hosts/p/remove", json={"confirm_host": "p"}, headers=hdr
                             ).status_code == 404
    finally:
        e.client.close()
        e.store.close()


def test_tasks_helper_remove_host_reports_none_for_an_unknown_host(env):
    assert asyncio.run(hosttasks.remove_host(env.store, "ghost", 1.0)) is None
    assert asyncio.run(hosttasks.revoke_host_keys(env.store, "ghost", 1.0)) == 0


# ---------------------------------------------------------------- schema 12 from schema 11


def test_a_version_11_database_with_enrolments_migrates_to_14(tmp_path):
    import sqlite3
    from observe.storage.schema import MIGRATIONS, SCHEMA_VERSION, migrate
    from observe.store import Store
    path = str(tmp_path / "w.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.commit()
    newer = {v: m for v, m in MIGRATIONS.items() if v > 11}
    assert newer and SCHEMA_VERSION == 19
    for v in newer:
        del MIGRATIONS[v]
    try:
        migrate(db)
    finally:
        MIGRATIONS.update(newer)
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 11
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='host_tasks'").fetchall()
    assert "allowlist_rev" not in [r[1] for r in db.execute("PRAGMA table_info(enrolments)")]
    db.execute("INSERT INTO enrolments (host, platform, agent, control, allowlist, token_hash, "
               "created, created_by, expires_at) VALUES ('nas01','linux',1,1,'{}','h',1.0,'a',2.0)")
    db.commit()
    db.close()
    Store(path).close()
    db = sqlite3.connect(path)
    try:
        assert db.execute("SELECT allowlist_rev, allowlist_saved_at, reissued_at FROM enrolments "
                          "WHERE host='nas01'").fetchone() == (0, None, None)
        assert db.execute("SELECT COUNT(*) FROM host_tasks").fetchone() == (0,)
        assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        db.close()


def test_a_version_18_database_keeps_its_ingest_keys_with_an_empty_role(tmp_path):
    import sqlite3
    from observe.storage.schema import MIGRATIONS, SCHEMA_VERSION, migrate
    from observe.store import Store
    path = str(tmp_path / "w.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.commit()
    newer = {v: m for v, m in MIGRATIONS.items() if v > 18}
    assert newer and SCHEMA_VERSION == 19
    for v in newer:
        del MIGRATIONS[v]
    try:
        migrate(db)
    finally:
        MIGRATIONS.update(newer)
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 18
    assert "role" not in [r[1] for r in db.execute("PRAGMA table_info(ingest_keys)")]
    db.execute("INSERT INTO ingest_keys (prefix, hash, host, created, scope) "
               "VALUES ('wpr_old', 'h', 'nas01', 1.0, 'wpr')")
    db.commit()
    db.close()
    Store(path).close()
    db = sqlite3.connect(path)
    try:
        assert db.execute("SELECT prefix, host, scope, role FROM ingest_keys").fetchall() == [
            ("wpr_old", "nas01", "wpr", "")]
        assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        db.close()


def test_reissue_refuses_a_bad_pool_and_changes_nothing(env):
    hdr = admin(env)
    old = enrol_host(env, hdr)
    url = "/api/hosts/nas01/enrolment/reissue"
    for pool in ("Apps", "../x"):
        r = env.client.post(url, json={"confirmed": True, "pool": pool}, headers=hdr)
        assert r.status_code == 400, pool
    assert ingest_for(env, "nas01", secrets_of(old)["AGENT_KEY"]).status_code == 200
    assert env.rows("SELECT COUNT(*) FROM ingest_keys WHERE host='nas01' AND revoked_at IS NULL"
                    ) == [(2,)]


def test_reissue_for_truenas_uses_the_pool_asked_for_or_refuses_it(env):
    hdr = admin(env)
    enrol_host(env, hdr, name="tn01", platform="truenas", control=False, allowlist=None)
    url = "/api/hosts/tn01/enrolment/reissue"
    bad = env.client.post(url, json={"confirmed": True, "pool": "a b;"}, headers=hdr)
    assert bad.status_code == 400
    assert env.rows("SELECT COUNT(*) FROM ingest_keys WHERE host='tn01' AND revoked_at IS NULL"
                    ) == [(1,)]
    ok = env.client.post(url, json={"confirmed": True, "pool": "tank"}, headers=hdr)
    assert ok.status_code == 200 and "tank" in ok.json()["command"]


def test_a_failed_update_never_reads_as_applied_even_if_the_old_daemon_pulls(env):
    hdr = admin(env)
    script = enrol_host(env, hdr)
    env.clock.now += 10
    pull(env, script, env.clock())
    env.clock.now += 10
    token = task_token(put(env, hdr))
    key = secrets_of(env.client.get(f"/t/{token}").text)["STEP_KEY"]
    env.clock.now += 5
    step(env, key, "control_config")
    env.clock.now += 5
    pull(env, script, env.clock())
    assert settings(env).json()["allowlist_status"]["state"] == "pending"
    env.clock.now += 5
    step(env, key, "control_unit", status="failed")
    env.clock.now += 5
    pull(env, script, env.clock())
    status = settings(env).json()["allowlist_status"]
    assert status["state"] == "pending" and status["applied_at"] is None
    assert settings(env).json()["task"]["state"] == "failed"


def test_a_pull_before_the_restart_report_does_not_count_as_applied(env):
    hdr = admin(env)
    script = enrol_host(env, hdr)
    env.clock.now += 10
    token = task_token(put(env, hdr))
    key = secrets_of(env.client.get(f"/t/{token}").text)["STEP_KEY"]
    env.clock.now += 5
    step(env, key, "control_config")
    env.clock.now += 5
    pull(env, script, env.clock())
    env.clock.now += 5
    step(env, key, "control_unit")
    assert settings(env).json()["allowlist_status"]["state"] == "written"
