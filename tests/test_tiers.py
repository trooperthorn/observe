"""Polling tiers (docs/DATA-API-DESIGN.md section 10.1): the default rates, per-host overrides,
what an agent is told, the key scope check and the audit row of a change."""

from __future__ import annotations

import asyncio
import json

from observe import tiers
from observe.ingest.keys import create_key

from .test_auth import Env

URL = "/api/admin/tiers"
AGENT = "/internal/v1/agent-config"


def _admin(tmp_path):
    env = Env(tmp_path)
    env.user("root", admin=True)
    return env, env.csrf(env.login("root"))


def _key(env, host, scope="wpi"):
    return asyncio.run(create_key(env.store, host, scope=scope))[0]


def _agent(env, key):
    return env.client.get(AGENT, headers={"Authorization": f"Bearer {key}"})


def _close(env):
    env.client.close()
    env.store.close()


def test_default_tier_intervals_are_served(tmp_path):
    env, _ = _admin(tmp_path)
    try:
        r = _agent(env, _key(env, "nas01"))
        assert r.status_code == 200
        assert r.json() == {"host": "nas01", "intervals": {
            "availability": 30.0, "device_metrics": 60.0, "storage_health": 900.0,
            "smart": 3600.0, "inventory": 3600.0}}
        assert r.headers["cache-control"] == "no-store"
        assert env.client.get(URL).json()["global"] == tiers.DEFAULTS
    finally:
        _close(env)


def test_a_per_host_override_applies_to_that_host_only(tmp_path):
    env, hdr = _admin(tmp_path)
    try:
        k1, k2 = _key(env, "nas01"), _key(env, "nas02")
        r = env.client.put(URL, headers=hdr, json={
            "global": {"device_metrics": 120}, "hosts": {"nas01": {"availability": 10}}})
        assert r.status_code == 200, r.text
        one = _agent(env, k1).json()["intervals"]
        two = _agent(env, k2).json()["intervals"]
        assert one["availability"] == 10.0 and one["device_metrics"] == 120.0
        assert two["availability"] == 30.0 and two["device_metrics"] == 120.0
        assert env.client.get(URL).json()["hosts"] == {"nas01": {"availability": 10.0}}
    finally:
        _close(env)


def test_an_agent_receives_only_its_own_config(tmp_path):
    env, hdr = _admin(tmp_path)
    try:
        k1 = _key(env, "nas01")
        _key(env, "nas02")
        env.client.put(URL, headers=hdr, json={"hosts": {"nas02": {"smart": 600}}})
        r = _agent(env, k1)
        assert r.json()["host"] == "nas01" and r.json()["intervals"]["smart"] == 3600.0
        assert "nas02" not in r.text
        # A host named in the query is ignored: the host comes from the key.
        q = env.client.get(AGENT + "?host=nas02", headers={"Authorization": f"Bearer {k1}"})
        assert q.json()["host"] == "nas01"
    finally:
        _close(env)


def test_a_wrong_key_scope_or_no_key_is_refused(tmp_path):
    env, _ = _admin(tmp_path)
    try:
        assert _agent(env, _key(env, "jack1", scope="wpf")).status_code == 401
        assert env.client.get(AGENT).status_code == 401
        assert _agent(env, "wpi_nosuchprefix_secret").status_code == 401
        denied = env.rows("SELECT kind FROM audit WHERE kind='ingest_denied'")
        assert len(denied) >= 1
        # An ingest key is not an admin session.
        env.client.cookies.clear()
        assert env.client.get(URL, headers={"Authorization": f"Bearer {_key(env, 'h')}"}
                              ).status_code in (401, 403)
    finally:
        _close(env)


def test_changing_an_override_writes_an_audit_entry(tmp_path):
    env, hdr = _admin(tmp_path)
    try:
        _key(env, "nas01")
        assert env.client.put(URL, headers=hdr, json={
            "hosts": {"nas01": {"storage_health": 300}}}).status_code == 200
        rows = env.rows("SELECT actor, detail FROM audit WHERE kind='tier_rates_changed'")
        assert len(rows) == 1 and rows[0][0] == "root"
        detail = json.loads(rows[0][1])
        assert detail["old"]["hosts"] == {}
        assert detail["new"]["hosts"] == {"nas01": {"storage_health": 300.0}}
    finally:
        _close(env)


def test_refused_values_are_422_audited_and_store_nothing(tmp_path):
    env, hdr = _admin(tmp_path)
    try:
        _key(env, "nas01")
        for body in ({"global": {"availability": 1}}, {"global": {"nope": 60}},
                     {"hosts": {"ghost": {"smart": 600}}}, {"global": {"smart": "x"}},
                     {"hosts": {"nas01": {}}}, {}):
            assert env.client.put(URL, headers=hdr, json=body).status_code == 422, body
        assert env.client.put(URL, json={"global": {"smart": 600}}).status_code in (401, 403)
        assert len(env.rows("SELECT 1 FROM audit WHERE kind='tier_rates_failed'")) == 6
        assert env.rows("SELECT 1 FROM audit WHERE kind='tier_rates_changed'") == []
        assert env.client.get(URL).json()["global"] == tiers.DEFAULTS
    finally:
        _close(env)


def test_null_resets_a_global_rate(tmp_path):
    env, hdr = _admin(tmp_path)
    try:
        env.client.put(URL, headers=hdr, json={"global": {"smart": 600}})
        assert env.client.get(URL).json()["global"]["smart"] == 600.0
        env.client.put(URL, headers=hdr, json={"global": {"smart": None}})
        assert env.client.get(URL).json()["global"]["smart"] == 3600.0
    finally:
        _close(env)
