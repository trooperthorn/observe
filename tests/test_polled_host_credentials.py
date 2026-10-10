"""Which credential a polled host's data arrives with (review R7).

/hosts/homeassistant/settings said "Active keys: none" while the host reported minutes earlier,
and its Agent field read "hostwatch observe-ha-host". The data is not pushed: the Home Assistant
monitor in mode host polls Home Assistant with its long-lived token and stores the readings in
process (observe/checks/apps.py, Store.ingest_batch). "No keys" is correct, so the page now says how
the data arrives, and the producer marker is no longer shown as an agent version."""

from __future__ import annotations

import asyncio

from observe import auth
from observe.checks import ha_host
from observe.producers import agent_label, pollers

from . import test_host_verdict
from . import test_host_views as hv


class Env(test_host_verdict.Env):
    """The verdict environment with an SNMP credential beside the Home Assistant one."""

    def __init__(self, tmp_path, monitors) -> None:
        real = test_host_verdict.make_config

        def with_snmp(mons, **extra):
            extra["credentials"] = {**extra["credentials"],
                                    "snmp_ro": {"type": "snmpv2c", "community": "c"}}
            return real(mons, **extra)

        test_host_verdict.make_config = with_snmp
        try:
            super().__init__(tmp_path, monitors)
        finally:
            test_host_verdict.make_config = real


HA_MONITORS = [
    {"name": "HA host", "type": "homeassistant", "host": "ha.lan", "credential": "ha",
     "mode": "host", "host_name": "homeassistant"},
    {"name": "HA api", "type": "homeassistant", "host": "ha.lan", "credential": "ha"},
    {"name": "Router CPU", "type": "snmp", "host": "10.0.0.1", "credential": "snmp_ro",
     "mode": "cpu", "host_name": "router"},
]


def admin_login(env: Env) -> None:
    asyncio.run(auth.create_user(env.store, env.cfg, "root", hv.PASSWORD, True,
                                 now=env.clock()))
    r = env.client.post("/api/login", json={"username": "root", "password": hv.PASSWORD})
    assert r.status_code == 200


def poll_batch(env: Env) -> None:
    """What the check stores after one poll: the batch ha_host builds, written in process."""
    b = ha_host.build_batch("homeassistant", {"version": "2026.10.0", "state": "RUNNING"},
                            [], env.clock.now - 5)
    asyncio.run(env.store.ingest_batch(b, {}, now=env.clock.now - 5, critical=True))


def test_the_producer_marker_is_not_an_agent_version():
    assert agent_label(ha_host.AGENT_VERSION) == "polled by Observe (Home Assistant monitor)"
    assert agent_label("observe-snmp-host") == "polled by Observe (SNMP monitor)"
    assert agent_label("0.9.0") == "" and agent_label(None) == ""


def test_a_polled_host_settings_page_names_the_monitor_and_credential(tmp_path):
    env = Env(tmp_path, HA_MONITORS)
    try:
        poll_batch(env)
        admin_login(env)
        body = env.client.get("/api/v2/hosts/homeassistant/settings").json()
        assert body["reporting"] and body["active_keys"] == 0  # nothing is pushed
        assert body["agent_version"] == "observe-ha-host"
        assert body["agent_label"] == "polled by Observe (Home Assistant monitor)"
        assert body["polled_by"] == [{"monitor": "HA host", "slug": "ha-host",
                                      "type": "homeassistant", "kind": "Home Assistant",
                                      "target": "ha.lan", "credential": "ha"}]
        assert "token" not in str(body).lower().replace("ttl_s", "")  # the name, never the token
        page = env.client.get("/api/v2/hosts/homeassistant").json()
        assert page["agent_label"] == "polled by Observe (Home Assistant monitor)"
        row = env.client.get("/api/v2/hosts").json()["items"]
        assert [r["agent_label"] for r in row if r["host"] == "homeassistant"] == [
            "polled by Observe (Home Assistant monitor)"]
        # An SNMP host that has not been polled yet still has a settings page that says how.
        snmp = env.client.get("/api/v2/hosts/router/settings").json()
        assert [p["credential"] for p in snmp["polled_by"]] == ["snmp_ro"]
        assert snmp["agent_label"] == ""
    finally:
        env.close()


def test_pollers_skip_other_modes_hosts_and_disabled_monitors(tmp_path):
    env = Env(tmp_path, [*HA_MONITORS, {
        "name": "HA off", "type": "homeassistant", "host": "ha2.lan", "credential": "ha",
        "mode": "host", "host_name": "homeassistant", "enabled": False}])
    try:
        got = pollers(env.sched.monitors, "homeassistant")
        assert [p["monitor"] for p in got] == ["HA host"]
        assert pollers(env.sched.monitors, "nas01") == []
    finally:
        env.close()
