"""Threshold rules that name a metric agents no longer send (audit failure
old-rule-saved-silently): a submitted one is refused with the replacement named, and a stored one
is listed as invalid instead of only logged."""

from __future__ import annotations

import asyncio

import pytest

from observe import rules
from observe.otelnames import legacy_rule_advice

from .test_auth import Env

LIST = "/api/v2/admin/settings/rules"


def _rule(metric, rid="r1", crit=0.95):
    return {"id": rid, "kind": "consecutive", "metric": metric, "condition": "above",
            "crit": crit, "x": 1}


def _admin(tmp_path):
    env = Env(tmp_path)
    env.user("root", admin=True)
    return env, env.csrf(env.login("root"))


def test_put_with_a_pre_otel_metric_is_refused_naming_the_replacement(tmp_path):
    env, hdr = _admin(tmp_path)
    try:
        r = env.client.put("/api/admin/rules", headers=hdr,
                           json={"rules": [_rule("cpu.utilization_pct", crit=95)]})
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "system.cpu.utilization" in detail and "divide percent limits by 100" in detail
        assert env.client.get(LIST).json()["rules"] == []  # nothing was saved
    finally:
        env.client.close()
        env.store.close()


def test_a_valid_otel_rule_is_saved_and_listed_without_a_problem(tmp_path):
    env, hdr = _admin(tmp_path)
    try:
        r = env.client.put("/api/admin/rules", headers=hdr,
                           json={"rules": [_rule("system.cpu.utilization")]})
        assert r.status_code == 200
        assert r.json()["rules"][0]["invalid"] is None
        listed = env.client.get(LIST).json()["rules"]
        assert [(x["id"], x["metric"], x["invalid"]) for x in listed] == [
            ("r1", "system.cpu.utilization", None)]
    finally:
        env.client.close()
        env.store.close()


def test_a_stored_old_rule_is_listed_as_invalid(tmp_path):
    env, _ = _admin(tmp_path)
    try:
        stored = rules.validate([_rule("hwmon.temp", "old", 90), _rule("hw.temperature", "new", 90)])
        asyncio.run(env.store.storage.write(
            lambda db: rules.save(db, stored, now=1.0, actor="t", remote="")))
        listed = {x["id"]: x for x in env.client.get(LIST).json()["rules"]}
        assert "hw.temperature" in listed["old"]["invalid"]
        assert listed["new"]["invalid"] is None
    finally:
        env.client.close()
        env.store.close()


@pytest.mark.parametrize("metric,new", [
    ("memory.mem_available", "system.memory.usage"),
    ("nut.ups_load_pct", "observe.ups.load"),
    ("win_storage.wear_pct", "hw.physical_disk.endurance_utilization")])
def test_known_old_names_name_their_replacement(metric, new):
    assert new in legacy_rule_advice(metric)


def test_an_unlisted_old_name_still_gets_advice_and_a_current_name_none():
    assert "percent values are now ratios" in legacy_rule_advice("hwmon.unknown_pct")
    assert legacy_rule_advice("hw.temperature") is None
    assert legacy_rule_advice("monitor.value") is None


def test_validate_without_the_flag_still_reads_stored_old_rules():
    assert rules.validate([_rule("hwmon.temp")])[0].metric == "hwmon.temp"
    with pytest.raises(rules.RuleError):
        rules.validate([_rule("hwmon.temp")], refuse_legacy=True)
