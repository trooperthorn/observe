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
