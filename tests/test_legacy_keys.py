"""Configuration written before the OpenTelemetry names must fail loudly, never read Good."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from observe.config import ComponentThresholds
from observe.otelnames import collector_scope, legacy_rule_metric

from .test_recheck import storage  # noqa: F401


def test_a_component_with_an_old_source_is_refused_with_the_new_scope():
    with pytest.raises(ValidationError) as err:
        ComponentThresholds(source="linux_thermal", metric="temp", warn=80, crit=90)
    assert collector_scope("linux_thermal") in str(err.value)


def test_a_component_with_the_otel_scope_is_accepted():
    c = ComponentThresholds(source=collector_scope("hwmon"), metric="hw.temperature",
                            warn=80, crit=90)
    assert c.metric == "hw.temperature"


@pytest.mark.parametrize("metric,old", [("hwmon.temp", True), ("cpu.utilization_pct", True),
                                        ("hw.temperature", False), ("system.cpu.utilization", False),
                                        ("monitor.value", False)])
def test_an_old_rule_metric_is_recognised(metric, old):
    assert legacy_rule_metric(metric) is old


async def test_loading_an_old_rule_warns(storage, caplog):  # noqa: F811
    from observe import rules
    from .test_recheck import Env, T0
    env = Env(storage, [])
    parsed = rules.validate([{"id": "r1", "kind": "consecutive", "metric": "hwmon.temp",
                              "condition": "above", "crit": 90, "x": 1}])
    await storage.write(lambda db: rules.save(db, parsed, now=T0, actor="t", remote=""))
    with caplog.at_level(logging.WARNING):
        await env.sched.load_rules()
    assert "hwmon.temp" in caplog.text


async def test_loading_an_old_home_assistant_rule_warns(storage, caplog):  # noqa: F811
    from observe import rules
    from .test_recheck import Env, T0
    env = Env(storage, [])
    parsed = rules.validate([{"id": "r2", "kind": "consecutive", "metric": "hassio.disk_used_pct",
                              "condition": "above", "crit": 95, "x": 1}])
    await storage.write(lambda db: rules.save(db, parsed, now=T0, actor="t", remote=""))
    with caplog.at_level(logging.WARNING):
        await env.sched.load_rules()
    assert "hassio.disk_used_pct" in caplog.text


OLD_HA = {"homeassistant": "observe.check.homeassistant", "hassio": "observe.check.hassio",
          "ha_soc": "observe.check.ha_soc", "ha_container": "ha_soc.collector.containers",
          "ha_backup": "ha_soc.collector.backup", "ha_watchdog": "ha_soc.collector.watchdog",
          "ha_integrations": "ha_soc.collector.integrations",
          "ha_repairs": "ha_soc.collector.repairs", "ha_supervisor": "ha_soc.collector.supervisor"}


@pytest.mark.parametrize("old,new", sorted(OLD_HA.items()))
def test_an_old_home_assistant_component_is_refused_with_its_replacement(old, new):
    with pytest.raises(ValueError, match=new.replace(".", r"\.")):
        ComponentThresholds(source=old, metric="disk_used_pct", warn=85, crit=95)
    ok = ComponentThresholds(source=new, metric="system.filesystem.utilization", warn=0.85,
                             crit=0.95)
    assert ok.source == new


@pytest.mark.parametrize("old", sorted(OLD_HA))
def test_requiring_an_old_home_assistant_source_is_refused(old):
    from observe.config import PushedHostMonitor
    with pytest.raises(ValueError, match="old Home Assistant id"):
        PushedHostMonitor(name="ha", type="pushed_host", host="ha", require_sources=[old])
    PushedHostMonitor(name="ha", type="pushed_host", host="ha", require_sources=["watchdog"])


@pytest.mark.parametrize("metric,new,unit", [
    ("hassio.disk_used_pct", "system.filesystem.utilization", "ratio"),
    ("hassio.disk_free_gb", "system.filesystem.usage", "1000000000"),
    ("ha_backup.last_success_age_hours", "observe.ha.backup.last_success_age", "by 3600"),
    ("ha_container.cpu_percent", "container.cpu.utilization", "ratio"),
    ("ha_repairs.open_total", "ha_soc.collector.repairs", "")])
def test_an_old_home_assistant_rule_metric_names_its_replacement(metric, new, unit):
    from observe.otelnames import legacy_rule_advice
    assert legacy_rule_metric(metric) is True
    advice = legacy_rule_advice(metric)
    assert new in advice and unit in advice


def test_a_current_rule_on_a_pushed_home_assistant_scope_is_not_legacy():
    from observe.otelnames import rule_metric
    name = rule_metric("ha_soc.collector.backup", "observe.ha.backup.last_ok")
    assert legacy_rule_metric(name) is False
    assert legacy_rule_metric(rule_metric("observe.check.hassio", "container.cpu.utilization")) is False


def test_old_home_assistant_source_rows_are_hidden_from_the_host_view():
    from observe.hostview import source_views
    src = {n: {"available": True, "reason": "", "updated": 0.0} for n in
           ("hassio", "ha_backup", "snmp", "ha_soc.collector.backup", "watchdog")}
    shown = {s["source"] for s in source_views(src, 10.0, 100.0)}
    assert shown == {"ha_soc.collector.backup", "watchdog"}
