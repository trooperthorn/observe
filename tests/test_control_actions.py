"""Admin action requests, confirmation, cancel and history (docs/CONTROL.md): the admin-only,
CSRF-protected request routes, parameter checks against reported capabilities, the typed host
name for a reboot, cancel while scheduled, and the Control section of the host page."""

from __future__ import annotations

import json
import re

import pytest

from observe import auth
from observe_control.actions import capabilities, validate
from observe_control.queue import QueueError

from .dbq import put_samples
from .test_auth import PASSWORD
from .test_control_queue import FLOOR, Env, bearer, run
from .test_field_docs import ROOT, stored_bytes

BASE = "/api/plugins/control"
HOST = "nas01"
HOSTILE = "<script>alert(1)</script>"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    now = e.clock.now
    def seed(db):
        for host in (HOST, "nas02", HOSTILE):
            db.execute("INSERT INTO hosts (host, agent_version, first_seen, last_seen) "
                       "VALUES (?,?,?,?)", (host, "0.2.0", now, now))
        for fan in ("pwm1", "pwm2"):
            put_samples(db, [(now, HOST, "thermalctl", "fan_duty", json.dumps({"fan": fan}),
                              40, "%")])

    e.store.storage.write_sync(seed)
    run(auth.create_user(e.store, e.cfg, "root", PASSWORD, True, now=now))
    run(auth.create_user(e.store, e.cfg, "viewer", PASSWORD, False, now=now))
    yield e
    e.close()


def login(env, user="root") -> str:
    env.client.cookies.clear()
    assert env.client.post("/api/login", json={"username": user,
                                               "password": PASSWORD}).status_code == 200
    return env.client.get("/api/v2/session").json()["csrf"]


def post(env, csrf, body, path="/request"):
    return env.client.post(BASE + path, json=body, headers={"X-CSRF-Token": csrf})


def req(action="fan.set_floor", params=None, host=HOST, **extra):
    return {"host": host, "action": action, "params": FLOOR if params is None else params,
            "confirmed": True, **extra}


def commands(env, host=HOST):
    return env.client.get("/api/v2/control/commands", params={"host": host}).json()["items"]


# ---- access ----------------------------------------------------------------------------

def test_request_needs_a_login(env):
    assert env.client.post(BASE + "/request", json=req()).status_code == 401
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]


def test_non_admin_gets_403_on_every_route(env):
    csrf = login(env, "viewer")
    assert post(env, csrf, req()).status_code == 403
    assert post(env, csrf, {}, "/commands/x/cancel").status_code == 403
    assert env.client.get("/api/v2/control/commands").status_code == 403
    assert env.client.get(f"/api/v2/control/capabilities?host={HOST}").status_code == 403
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]


def test_missing_or_wrong_csrf_gets_403(env):
    csrf = login(env)
    assert env.client.post(BASE + "/request", json=req()).status_code == 403
    assert post(env, "x" * len(csrf), req()).status_code == 403
    assert env.client.post(BASE + "/commands/x/cancel").status_code == 403
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]


def test_admin_request_is_queued_signed_and_audited(env):
    csrf = login(env)
    r = post(env, csrf, req())
    assert r.status_code == 200 and r.json()["state"] == "requested" and r.json()["seq"] == 1
    (cmd,) = commands(env)
    assert cmd["requested_by"] == "root" and cmd["params"] == FLOOR and cmd["state"] == "requested"
    (row,) = env.audit("control_requested")
    assert row["command_id"] == cmd["id"] and row["params"] == FLOOR
    pulled = env.pull(env.key()).json()["commands"]
    assert pulled[0]["command"]["id"] == cmd["id"]


# ---- confirmation -----------------------------------------------------------------------

def test_unconfirmed_request_is_refused(env):
    csrf = login(env)
    body = req()
    del body["confirmed"]
    assert post(env, csrf, body).status_code == 400
    assert post(env, csrf, {**body, "confirmed": False}).status_code == 400
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]
    assert len(env.audit("control_request_refused")) == 2


@pytest.mark.parametrize("value", ["true", 1, "yes", "True", [True], None])
def test_confirmed_must_be_the_json_boolean_true(env, value):
    csrf = login(env)
    assert post(env, csrf, {**req(), "confirmed": value}).status_code == 400
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]


@pytest.mark.parametrize("typed", [None, "", "NAS01", "nas01 ", " nas01", "nas02", "nas0"])
def test_reboot_needs_the_exact_host_name(env, typed):
    csrf = login(env)
    body = req("host.reboot", {})
    if typed is not None:
        body["confirm_host"] = typed
    r = post(env, csrf, body)
    assert r.status_code == 400 and "host name" in r.json()["detail"]
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]
    assert "host name" in env.audit("control_request_refused")[-1]["reason"]


def test_reboot_with_the_exact_name_is_queued(env):
    csrf = login(env)
    r = post(env, csrf, req("host.reboot", {}, confirm_host=HOST))
    assert r.status_code == 200
    assert commands(env)[0]["action"] == "host.reboot"


def test_unknown_host_is_refused(env):
    csrf = login(env)
    assert post(env, csrf, req(host="ghost")).status_code == 404
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]


def test_rate_limit_still_applies_to_requests(env):
    csrf = login(env)
    assert post(env, csrf, req()).status_code == 200
    assert post(env, csrf, req()).status_code == 429


# ---- parameter validation --------------------------------------------------------------

@pytest.mark.parametrize("action,params", [
    ("fan.set_floor", {**FLOOR, "min_duty": 101}),
    ("fan.set_floor", {**FLOOR, "min_duty": -1}),
    ("fan.set_floor", {**FLOOR, "min_duty": 20.5}),
    ("fan.set_floor", {**FLOOR, "min_duty": True}),
    ("fan.set_floor", {**FLOOR, "min_duty": "20"}),
    ("fan.set_floor", {**FLOOR, "header": "pwm9"}),          # not reported by the host
    ("fan.set_floor", {**FLOOR, "header": "pwm1; rm -rf"}),
    ("fan.set_floor", {**FLOOR, "controller": "other"}),
    ("fan.set_floor", {**FLOOR, "extra": 1}),
    ("fan.set_floor", {"controller": "thermalctl", "header": "pwm1"}),
    ("fan.set_mode", {"controller": "thermalctl", "mode": "off"}),
    ("service.restart", {"name": "a b"}),
    ("service.restart", {"name": HOSTILE}),
    ("service.restart", {"name": ""}),
    ("service.restart", {"name": "nginx\n"}),
    ("service.restart", {"name": "nginx "}),
    ("fan.set_floor", {**FLOOR, "header": "pwm1\n"}),
    ("fan.set_floor", {**FLOOR, "header": "pwm1 "}),
    ("service.restart", {"name": "x", "extra": 1}),
    ("host.reboot", {"x": 1}),
    ("agent.update", {}),
    ("agent.update", {"component": "kernel"}),
    ("agent.update", {"component": "agent", "extra": 1}),
    ("agent.update", {"component": ["agent"]}),
    ("agent.update", {"component": "agent\n"}),
    ("rm.everything", {}),
])
def test_bad_parameters_are_refused(env, action, params):
    csrf = login(env)
    r = post(env, csrf, req(action, params, confirm_host=HOST))
    assert r.status_code == 422, r.text
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]
    assert env.audit("control_request_refused")


def test_headers_are_checked_only_when_the_host_reported_them(env):
    csrf = login(env)
    # nas02 reported no thermalctl, so the header is not checked here; its allowlist decides.
    assert post(env, csrf, req(host="nas02", params={**FLOOR, "header": "pwm9"})).status_code == 200
    # thermalctl mode needs a reported thermalctl controller.
    r = post(env, csrf, req("fan.set_mode", {"controller": "thermalctl", "mode": "active"},
                            host="nas02"))
    assert r.status_code == 422
    ok = post(env, csrf, req("fan.set_mode", {"controller": "thermalctl", "mode": "dry_run"}))
    assert ok.status_code == 200


def test_valid_actions_are_all_queued(env):
    csrf = login(env)
    for action, params in (("fan.set_mode", {"controller": "thermal-control-suite",
                                             "mode": "active"}),
                           ("service.restart", {"name": "docker:scrutiny"})):
        assert post(env, csrf, req(action, params)).status_code == 200


def test_capabilities_route_reports_headers(env):
    login(env)
    data = env.client.get("/api/v2/control/capabilities", params={"host": HOST}).json()
    assert data["known"] and data["capabilities"] == {"thermalctl": True,
                                                       "headers": ["pwm1", "pwm2"]}
    assert "host.reboot" in data["actions"] and "agent.update" in data["actions"]
    assert data["components"] == ["agent", "control", "all"]
    other = env.client.get("/api/v2/control/capabilities", params={"host": "ghost"}).json()
    assert other["known"] is False


def test_capabilities_leave_out_agent_update_for_an_agent_too_old_for_it(env):
    env.store.storage.write_sync(lambda db: db.execute(
        "UPDATE hosts SET agent_version='0.1.0' WHERE host=?", (HOST,)))
    login(env)
    data = env.client.get("/api/v2/control/capabilities", params={"host": HOST}).json()
    assert "agent.update" not in data["actions"] and "host.reboot" in data["actions"]


def test_validate_and_capabilities_helpers():
    caps = capabilities([{"source": "thermalctl", "metric": "fan", "labels": {"header": "a"}},
                         {"source": "hwmon", "metric": "fan", "labels": {"fan": "z"}}])
    assert caps == {"thermalctl": True, "headers": ["a"]}
    assert validate("fan.set_floor", {"controller": "thermalctl", "header": "a", "min_duty": 0},
                    caps)["min_duty"] == 0
    with pytest.raises(QueueError):
        validate("fan.set_floor", "nope", caps)


# ---- agent.update -----------------------------------------------------------------------------

@pytest.mark.parametrize("component", ["agent", "control", "all"])
def test_agent_update_is_validated_signed_and_queued_like_any_action(env, component):
    from observe_control.signing import verify_command

    csrf = login(env)
    r = post(env, csrf, req("agent.update", {"component": component}))
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    (params, sig) = env.rows("SELECT params, signature FROM control_commands WHERE id=?", cid)[0]
    assert json.loads(params) == {"component": component}
    pulled = env.pull(env.key()).json()["commands"]
    (item,) = [c for c in pulled if c["command"]["id"] == cid]
    assert item["command"]["action"] == "agent.update"
    assert item["command"]["params"] == {"component": component} and item["signature"] == sig
    assert verify_command(env.public, item["command"], sig)
    (row,) = [a for a in env.audit("control_requested") if a["command_id"] == cid]
    assert row["action"] == "agent.update" and row["params"] == {"component": component}


def test_agent_update_result_carries_the_versions_the_daemon_reports(env):
    csrf = login(env)
    cid = post(env, csrf, req("agent.update", {"component": "agent"})).json()["id"]
    key = env.key()
    env.pull(key)
    output = json.dumps({"old_image_id": "sha256:aa", "new_image_id": "sha256:bb",
                         "old_version": "1.4.0", "new_version": "1.5.0"})
    assert env.result(key, {"id": cid, "state": "done", "output": output}).status_code == 200
    (item,) = [c for c in commands(env) if c["id"] == cid]
    assert item["state"] == "done" and json.loads(item["result"]["output"])["new_version"] == "1.5.0"


def eligible(env, host, pulled=True, platform="linux"):
    env.store.storage.write_sync(
        lambda db: db.execute("UPDATE hosts SET platform=? WHERE host=?", (platform, host)))
    key = env.key(host)
    if pulled:
        env.pull(key, host)


def update_all(env, csrf, body=None):
    return env.client.post(BASE + "/update-agents",
                           json={"component": "agent", "confirmed": True} if body is None else body,
                           headers={"X-CSRF-Token": csrf})


def test_update_all_queues_one_command_per_eligible_host_and_lists_the_rest(env):
    csrf = login(env)
    eligible(env, HOST)
    eligible(env, "nas02", pulled=False)
    r = update_all(env, csrf)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["component"] == "agent"
    assert [q["host"] for q in got["queued"]] == [HOST]
    refused = {x["host"]: x["reason"] for x in got["refused"]}
    assert refused["nas02"] == "the control daemon has never pulled"
    assert refused[HOSTILE] == "unknown platform"
    (cid,) = [q["id"] for q in got["queued"]]
    assert env.rows("SELECT host, action, params FROM control_commands WHERE id=?", cid) == \
        [(HOST, "agent.update", '{"component":"agent"}')]
    # The second run is refused by max_pending_per_action for that host, and says so.
    again = update_all(env, csrf).json()
    assert again["queued"] == [] and {x["host"]: x["reason"] for x in again["refused"]}[HOST] == \
        "agent.update is already pending for this host"
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(1,)]
    assert env.audit("control_request_refused")


def test_update_all_honours_the_hourly_limit_per_host(tmp_path):
    e = Env(tmp_path, max_commands_per_host_per_hour=2, max_pending_per_action=5)
    now = e.clock.now

    def seed(db):
        db.execute("INSERT INTO hosts (host, platform, agent_version, first_seen, last_seen) "
                   "VALUES (?,?,?,?,?)", (HOST, "linux", "0.2.0", now, now))
    e.store.storage.write_sync(seed)
    run(auth.create_user(e.store, e.cfg, "root", PASSWORD, True, now=now))
    try:
        csrf = login(e)
        eligible(e, HOST)
        assert post(e, csrf, req("service.restart", {"name": "x"})).status_code == 200
        assert update_all(e, csrf).json()["queued"] == [{"host": HOST, "id": e.rows(
            "SELECT id FROM control_commands WHERE action='agent.update'")[0][0]}]
        third = update_all(e, csrf, {"component": "all", "confirmed": True}).json()
        assert third["queued"] == [] and third["refused"][0]["reason"] == \
            "too many commands for this host in the last hour"
    finally:
        e.close()


@pytest.mark.parametrize("body, status", [
    ({"component": "agent"}, 400), ({"component": "agent", "confirmed": "true"}, 400),
    ({"component": "kernel", "confirmed": True}, 422), ({"confirmed": True, "extra": 1}, 422),
])
def test_update_all_needs_confirmation_and_a_known_component(env, body, status):
    csrf = login(env)
    eligible(env, HOST)
    r = update_all(env, csrf, body)
    assert r.status_code == status, r.text
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]


def test_update_all_is_admin_only_with_csrf(env):
    eligible(env, HOST)
    env.client.cookies.clear()
    assert env.client.post(BASE + "/update-agents", json={"confirmed": True}).status_code == 401
    csrf = login(env, "viewer")
    assert update_all(env, csrf).status_code == 403
    login(env)
    assert env.client.post(BASE + "/update-agents", json={"confirmed": True}).status_code == 403
    assert update_all(env, "wrong").status_code == 403
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]


def test_the_host_page_offers_the_update_and_reads_its_result():
    js = (ROOT / "observe" / "static" / "host-control.js").read_text(encoding="utf-8")
    assert '"agent.update": "Update the hostwatch agent"' in js
    assert "ctlCaps.components" in js and "updateResultText" in js
    assert 'from "/static/js/updates-logic.js"' in js
    logic = (ROOT / "observe" / "static" / "js" / "updates-logic.js").read_text(encoding="utf-8")
    assert "export function updateResultText" in logic and "JSON.parse" in logic
    assert "innerHTML" not in logic


# ---- cancel ---------------------------------------------------------------------------------

def schedule(env, csrf):
    cid = post(env, csrf, req("host.reboot", {}, confirm_host=HOST)).json()["id"]
    return cid


def test_cancel_works_only_while_scheduled(env):
    csrf = login(env)
    cid = schedule(env, csrf)
    assert env.answer(env.key(), {"id": cid, "state": "scheduled"}).status_code == 200
    r = post(env, csrf, {}, f"/commands/{cid}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert env.state(cid) == "cancelled"
    # A second cancel and a refused result are rejected.
    assert post(env, csrf, {}, f"/commands/{cid}/cancel").status_code == 409
    assert env.result(env.key(), {"id": cid, "state": "refused"}).status_code == 409
    assert env.state(cid) == "cancelled"
    # The daemon is told through the cancel list until it acknowledges, even days later.
    env.clock.now += 3 * 86400
    got = env.pull(env.key()).json()
    assert got["commands"] == [] and got["cancel"] == [cid]
    assert env.result(env.key(), {"id": cid, "state": "cancelled"}).status_code == 200
    assert env.state(cid) == "cancelled"
    assert env.pull(env.key()).json()["cancel"] == []
    assert env.audit("control_cancelled") == [{"command_id": cid, "host": HOST}]
    assert len(env.audit("control_cancel_refused")) == 1


def test_a_late_cancel_still_records_what_the_host_did(env):
    # The host may have rebooted before it saw the cancel; it reports the truth.
    csrf = login(env)
    cid = schedule(env, csrf)
    assert env.answer(env.key(), {"id": cid, "state": "scheduled"}).status_code == 200
    assert post(env, csrf, {}, f"/commands/{cid}/cancel").status_code == 200
    assert env.result(env.key(), {"id": cid, "state": "done"}).status_code == 200
    assert env.state(cid) == "done"
    assert env.pull(env.key()).json()["cancel"] == []


def test_only_a_reboot_cancelled_while_scheduled_can_be_reported_cancelled(env):
    csrf = login(env)
    other = env.enqueue(HOST)["command"]["id"]
    assert env.answer(env.key(), {"id": other, "state": "cancelled"}).status_code == 409


def test_cancel_is_refused_after_done_and_for_other_actions(env):
    csrf = login(env)
    cid = schedule(env, csrf)
    env.answer(env.key(), {"id": cid, "state": "scheduled"})
    env.result(env.key(), {"id": cid, "state": "done"})
    assert post(env, csrf, {}, f"/commands/{cid}/cancel").status_code == 409
    assert env.state(cid) == "done"
    other = post(env, csrf, req()).json()["id"]
    env.pull(env.key())  # pulled commands can no longer be cancelled
    assert post(env, csrf, {}, f"/commands/{other}/cancel").status_code == 409
    assert post(env, csrf, {}, "/commands/nope/cancel").status_code == 404


def test_cancel_of_an_expired_command_is_refused(env):
    csrf = login(env)
    cid = schedule(env, csrf)
    env.clock.now += 600
    csrf = login(env)
    assert post(env, csrf, {}, f"/commands/{cid}/cancel").status_code == 409
    assert env.state(cid) == "unknown"


def test_cancel_of_a_requested_command_of_any_action(env):
    csrf = login(env)
    cid = post(env, csrf, req()).json()["id"]
    r = post(env, csrf, {}, f"/commands/{cid}/cancel")
    assert r.status_code == 200 and env.state(cid) == "cancelled"
    got = env.pull(env.key()).json()
    assert got["commands"] == [] and got["cancel"] == []  # never pulled, nothing to tell
    assert env.result(env.key(), {"id": cid, "state": "done"}).status_code == 409


def test_scheduled_reboot_is_not_served_again_and_blocks_no_other_action(env):
    csrf = login(env)
    cid = schedule(env, csrf)
    env.answer(env.key(), {"id": cid, "state": "scheduled"})
    fan = post(env, csrf, req()).json()["id"]
    got = env.pull(env.key()).json()
    assert [c["command"]["id"] for c in got["commands"]] == [fan] and got["cancel"] == []
    assert env.state(cid) == "scheduled"
    assert env.result(env.key(), {"id": cid, "state": "done"}).status_code == 200


def test_result_for_an_unpulled_command_is_refused(env):
    csrf = login(env)
    cid = post(env, csrf, req()).json()["id"]
    r = env.result(env.key(), {"id": cid, "state": "done"})
    assert r.status_code == 409 and env.state(cid) == "requested"


def test_scheduled_is_refused_for_other_actions_than_reboot(env):
    csrf = login(env)
    cid = post(env, csrf, req()).json()["id"]
    env.pull(env.key())
    r = env.result(env.key(), {"id": cid, "state": "scheduled"})
    assert r.status_code == 422 and env.state(cid) == "pulled"


def test_signed_params_of_a_reboot_carry_no_confirm_host(env):
    csrf = login(env)
    cid = post(env, csrf, req("host.reboot", {}, confirm_host=HOST)).json()["id"]
    (item,) = env.pull(env.key()).json()["commands"]
    assert item["command"]["id"] == cid and item["command"]["params"] == {}
    assert "confirm_host" not in json.dumps(item["command"])
    other = post(env, csrf, req("host.reboot", {"confirm_host": "nas02"}, host="nas02",
                                      confirm_host="nas02"))
    assert other.status_code == 422


# ---- history --------------------------------------------------------------------------------

def test_history_shows_states_for_one_host(env):
    csrf = login(env)
    a = post(env, csrf, req()).json()["id"]
    b = post(env, csrf, req("service.restart", {"name": "x"})).json()["id"]
    c = post(env, csrf, req("host.reboot", {}, confirm_host=HOST)).json()["id"]
    d = post(env, csrf, req(host="nas02")).json()["id"]
    key = env.key()
    env.pull(key)
    env.result(key, {"id": b, "state": "failed", "output": "unit not found"})
    env.result(key, {"id": c, "state": "scheduled"})  # pulled above
    states = {x["id"]: x["state"] for x in commands(env)}
    assert states == {a: "pulled", b: "failed", c: "scheduled"}
    failed = next(x for x in commands(env) if x["id"] == b)
    assert failed["result"]["output"] == "unit not found"
    assert [x["id"] for x in commands(env, "nas02")] == [d]
    env.clock.now += 600
    login(env)
    assert {x["id"]: x["state"] for x in commands(env)}[a] == "unknown"
    assert {x["id"]: x["state"] for x in commands(env)}[c] == "scheduled"


def test_hostile_strings_come_back_as_json_data(env):
    csrf = login(env)
    cid = post(env, csrf, req("service.restart", {"name": "ok"}, host=HOSTILE)).json()["id"]
    env.answer(env.key(HOSTILE), {"id": cid, "state": "failed", "output": HOSTILE + "<img>"}, HOSTILE)
    r = env.client.get("/api/v2/control/commands", params={"host": HOSTILE})
    assert r.headers["content-type"].startswith("application/json")
    (item,) = r.json()["items"]
    assert item["host"] == HOSTILE and HOSTILE in item["result"]["output"]
    refused = post(env, csrf, req("service.restart", {"name": HOSTILE}))
    assert refused.status_code == 422


# ---- audit outcome ---------------------------------------------------------------------------

def test_refused_commands_are_found_by_the_refused_audit_filter_and_not_by_ok(env):
    csrf = login(env)
    assert post(env, csrf, req()).status_code == 200
    unconfirmed = req()
    del unconfirmed["confirmed"]
    assert post(env, csrf, unconfirmed).status_code == 400
    (cmd,) = commands(env)
    key = env.key()
    r = env.answer(key, {"id": cmd["id"], "state": "refused", "output": "not allowed here"})
    assert r.status_code == 200

    def audit_page(outcome):
        got = env.client.get("/api/v2/audit", params={"outcome": outcome}).json()["items"]
        return {(row["kind"], row["status"]): row for row in got}

    refused, ok = audit_page("refused"), audit_page("ok")
    # The admin's refused request, and the agent's refused result, which was answered 200.
    assert refused[("control_request_refused", 400)]["actor"] == "root"
    agent = refused[("plugin_request", 200)]
    assert agent["detail"]["outcome"] == "refused" and agent["outcome"] == "refused"
    # The actor is the key prefix, and the host the key is bound to comes with it.
    assert agent["actor"] == key.split("_")[1] and agent["actor_host"] == HOST
    # The queued command has no HTTP status of its own (0), and it worked.
    assert ok[("control_requested", 0)]["outcome"] == "ok"
    assert ("control_request_refused", 400) not in ok
    assert {row["id"] for row in ok.values()}.isdisjoint(row["id"] for row in refused.values())
    assert all(row["detail"].get("outcome") != "refused" for row in ok.values())
    assert env.client.get("/api/v2/audit", params={"outcome": "maybe"}).status_code == 400


# ---- the page --------------------------------------------------------------------------------

def test_host_page_loads_the_control_section_and_writes_only_text(env):
    page = env.client.get("/host").text
    assert 'id="control"' in page and "host-control.js" in page
    js = env.client.get("/static/host-control.js").text
    assert "textContent" in js and "X-CSRF-Token" in js
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert banned not in js
    # The reboot dialog compares the typed name with the host name exactly.
    # The reboot goes through the shared typed-name dialog, and the page holds a native dialog
    # only through that module. Control sits inside main as the last card.
    assert "typedConfirm" in js and "confirm_host" in js and "toast(" in js
    assert 'from "/static/js/dialog.js"' in js and "createElement" in js
    assert "ctlHost" in js and "typed.value" not in js
    assert "showModal" in env.client.get("/static/js/dialog.js").text
    assert page.index("<main") < page.index('id="control"') < page.index("</main>")
    assert '<script type="module" src="/static/host-control.js"></script>' in page
    assert 'href="/static/css/host.css"' in page and re.search(r"hidden", page)


def test_new_files_use_lf_and_no_em_dashes_or_model_names():
    for rel in ("plugins/control/observe_control/actions.py", "tests/test_control_actions.py",
                "observe/static/host-control.js", "observe/static/host.html",
                "observe/static/js/updates-logic.js",
                "observe/static/host.js", "observe/static/css/host.css",
                "plugins/control/observe_control/__init__.py", "docs/CONTROL.md"):
        raw = stored_bytes(ROOT / rel)
        text = raw.decode("utf-8")
        assert b"\r" not in raw and chr(0x2014) not in text, rel
        names = ("cla" + "ude", "op" + "us", "son" + "net", "hai" + "ku")
        assert not any(w in text.lower() for w in names), rel
