"""The Control card's choices (docs/CONTROL.md "Capabilities"): a host without a control daemon
is offered nothing, an install-only platform is never offered agent.update, an enrolled host is
offered only what its saved allowlist allows, and the request route refuses what the form would
not offer. The allowlist's board header names (fan1) and the controller's ids (pwm1) are mapped
in one place, enrol.controller_id."""

from __future__ import annotations

import json

import pytest

from observe import auth, scripts
from observe.enrol import controller_id
from observe_control.actions import capabilities, control_form, validate
from observe_control.queue import QueueError

from .dbq import put_samples
from .test_auth import PASSWORD
from .test_control_actions import BASE, login
from .test_control_queue import Env, run

ALLOW = {"fans": [{"header": "fan1", "min_duty_limit": 30}, {"header": "fan2",
                                                               "min_duty_limit": 30}],
         "services": ["smbd", "nfs-server"], "reboot": True, "update": True}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    now = e.clock.now

    def seed(db):
        for host, platform in (("mediain-svr", "linux"), ("homeassistant", "linux"),
                               ("truenas-svr", "truenas"), ("x15-wk", "windows"),
                               ("pending-svr", "linux"), ("hand-svr", "windows")):
            db.execute("INSERT INTO hosts (host, platform, agent_version, first_seen, last_seen) "
                       "VALUES (?,?,?,?,?)", (host, platform, "0.2.0", now, now))
        # thermalctl's own readings name the controller ids.
        put_samples(db, [(now, "mediain-svr", "thermalctl", "observe.thermal.fan.duty",
                          json.dumps({"hw.id": f"fan:pwm{i}"}), 0.4, "1") for i in (1, 2, 3, 4)])
        enrolments = (("mediain-svr", "linux", 1, ALLOW), ("x15-wk", "windows", 0, {}),
                      ("truenas-svr", "truenas", 0, {}), ("pending-svr", "linux", 1, ALLOW))
        for host, platform, control, allow in enrolments:
            db.execute("INSERT INTO enrolments (host, platform, agent, control, allowlist, "
                       "token_hash, created, expires_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (host, platform, 1, control, json.dumps(allow), "t-" + host, now,
                        now + 3600, now))

    e.store.storage.write_sync(seed)
    # mediain-svr and hand-svr (a key made in the key screen) have a daemon that has pulled;
    # pending-svr has a key whose daemon has never pulled.
    for host in ("mediain-svr", "hand-svr", "pending-svr"):
        e.key(host)
    e.store.storage.write_sync(lambda db: db.execute(
        "UPDATE ingest_keys SET last_used=? WHERE host IN ('mediain-svr', 'hand-svr')", (now,)))
    run(auth.create_user(e.store, e.cfg, "root", PASSWORD, True, now=now))
    yield e
    e.close()


def caps(env, host):
    r = env.client.get("/api/v2/control/capabilities", params={"host": host})
    assert r.status_code == 200, r.text
    return r.json()


def request(env, csrf, host, action, params, **extra):
    return env.client.post(BASE + "/request", json={"host": host, "action": action,
                                                     "params": params, "confirmed": True,
                                                     **extra},
                           headers={"X-CSRF-Token": csrf})


# ---- the header name mapping -------------------------------------------------------------------

@pytest.mark.parametrize("name, cid", [("fan1", "pwm1"), ("fan2", "pwm2"), ("fan3", "pwm3"),
                                       ("fan10", "pwm10"), ("pwm1", "pwm1"),
                                       ("pwm-fan", "pwm-fan"), ("fan", "fan"),
                                       ("fan1a", "fan1a"), ("cpu_fan", "cpu_fan")])
def test_board_header_names_map_to_controller_ids(name, cid):
    assert controller_id(name) == cid


def test_control_toml_lists_the_controller_ids_the_daemon_and_thermalctl_share():
    body = scripts.control_toml("mediain-svr", {**ALLOW, "fans": ALLOW["fans"] + ["pwm1"]},
                                "ed25519:" + "A" * 43 + "=")
    assert 'headers = ["pwm1", "pwm2"]' in body and "fan1" not in body
    assert "min_duty_floor = 30" in body


def test_thermalctl_readings_name_their_headers_by_hw_id():
    rows = [{"source": "thermalctl", "metric": "observe.thermal.fan.duty",
             "labels": {"hw.id": f"fan:pwm{i}"}} for i in (2, 1)]
    rows.append({"source": "thermalctl", "metric": "observe.thermal.mode", "labels": {}})
    rows.append({"source": "hwmon", "metric": "hw.fan.speed", "labels": {"hw.id": "nct:fan1"}})
    assert capabilities(rows) == {"thermalctl": True, "headers": ["pwm1", "pwm2"]}


def test_a_floor_on_fan1_is_signed_for_pwm1_and_checked_against_its_floor():
    form = control_form(platform="linux", agent_version="0.2.0", has_key=True, pulled=True,
                        enrolled=True, control_chosen=True, allowlist=ALLOW,
                        reported={"thermalctl": True, "headers": ["pwm1", "pwm2", "pwm3"]})
    got = validate("fan.set_floor", {"controller": "thermalctl", "header": "fan1",
                                     "min_duty": 30}, form)
    assert got == {"controller": "thermalctl", "header": "pwm1", "min_duty": 30}
    assert validate("fan.set_floor", {"controller": "thermalctl", "header": "pwm2",
                                      "min_duty": 45}, form)["header"] == "pwm2"
    for header, duty in (("fan1", 29), ("fan3", 50), ("pwm3", 50)):
        with pytest.raises(QueueError):
            validate("fan.set_floor", {"controller": "thermalctl", "header": header,
                                       "min_duty": duty}, form)


# ---- the capabilities answer -------------------------------------------------------------------

@pytest.mark.parametrize("host, reason", [
    ("homeassistant", "this host has no control daemon"),
    ("truenas-svr", "control was not chosen for this host"),
    ("x15-wk", "control was not chosen for this host"),
    ("pending-svr", "the control daemon has never pulled"),
    ("nobody", "this host has never reported"),
])
def test_a_host_without_a_control_daemon_is_offered_nothing(env, host, reason):
    login(env)
    got = caps(env, host)
    assert got["available"] is False and got["reason"] == reason and got["actions"] == []


def test_an_enrolled_host_is_offered_its_allowlist_only(env):
    login(env)
    got = caps(env, "mediain-svr")
    assert got["available"] is True and got["reason"] == ""
    assert got["controller"] == "thermalctl" and got["controllers"] == ["thermalctl"]
    # control.toml says allow_mode_change = false, so the mode is not offered.
    assert got["actions"] == ["fan.set_floor", "service.restart", "host.reboot", "agent.update"]
    assert got["components"] == ["agent"]
    assert got["allowlist"]["services"] == ["smbd", "nfs-server"]
    assert got["allowlist"]["min_duty_floor"] == 30
    assert got["fan_headers"] == [
        {"header": "fan1", "controller_id": "pwm1", "min_duty_limit": 30, "floor": 30},
        {"header": "fan2", "controller_id": "pwm2", "min_duty_limit": 30, "floor": 30}]
    assert got["capabilities"]["headers"] == ["pwm1", "pwm2", "pwm3", "pwm4"]


def test_the_floor_is_the_strictest_limit_as_control_toml_writes_it():
    form = control_form(platform="linux", agent_version="0.2.0", has_key=True, pulled=True,
                        enrolled=True, control_chosen=True,
                        allowlist={"fans": [{"header": "fan1", "min_duty_limit": 25}, "fan2"]},
                        reported={"thermalctl": False, "headers": None})
    assert [f["floor"] for f in form["fan_headers"]] == [25, 25]
    assert form["actions"] == ["fan.set_floor"]


def test_a_windows_host_is_never_offered_agent_update(env):
    login(env)
    got = caps(env, "hand-svr")
    assert got["available"] is True and "agent.update" not in got["actions"]
    assert got["controllers"] == ["thermal-control-suite"]


# ---- the request route refuses what the form would not offer ---------------------------------

@pytest.mark.parametrize("host, action, params", [
    ("mediain-svr", "fan.set_floor", {"controller": "thermalctl", "header": "fan1",
                                      "min_duty": 20}),
    ("mediain-svr", "fan.set_floor", {"controller": "thermalctl", "header": "fan3",
                                      "min_duty": 50}),
    ("mediain-svr", "fan.set_floor", {"controller": "thermal-control-suite", "header": "fan1",
                                      "min_duty": 50}),
    ("mediain-svr", "fan.set_mode", {"controller": "thermalctl", "mode": "active"}),
    ("mediain-svr", "service.restart", {"name": "sshd"}),
    ("mediain-svr", "agent.update", {"component": "all"}),
    ("homeassistant", "service.restart", {"name": "smbd"}),
    ("pending-svr", "service.restart", {"name": "smbd"}),
    ("hand-svr", "agent.update", {"component": "agent"}),
])
def test_requests_outside_the_form_are_refused(env, host, action, params):
    csrf = login(env)
    r = request(env, csrf, host, action, params)
    assert r.status_code == 422, r.text
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]


def test_requests_the_form_offers_are_queued_with_the_controller_id(env):
    csrf = login(env)
    r = request(env, csrf, "mediain-svr", "fan.set_floor",
                {"controller": "thermalctl", "header": "fan1", "min_duty": 30})
    assert r.status_code == 200, r.text
    assert json.loads(env.rows("SELECT params FROM control_commands")[0][0]) == \
        {"controller": "thermalctl", "header": "pwm1", "min_duty": 30}
    assert request(env, csrf, "mediain-svr", "service.restart",
                   {"name": "nfs-server"}).status_code == 200
    assert request(env, csrf, "mediain-svr", "agent.update",
                   {"component": "agent"}).status_code == 200
