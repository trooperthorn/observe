"""The clear band of the built-in thresholds: a value hovering at a threshold does not flap."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from observe.checks.base import (Check, CheckResult, Result, apply_thresholds, threshold_level,
                                 unit_suffix)
from observe.config import Thresholds

from .conftest import make_config

SERVFAIL = Thresholds(direction="above", warn=2, crit=10)  # the reported dns1-servfail-rate


def levels(th: Thresholds, values: list[float]) -> list[Result]:
    held, out = Result.OK, []
    for v in values:
        held = threshold_level(v, th, held)
        out.append(held)
    return out


def test_a_value_hovering_at_the_threshold_does_not_flap():
    assert levels(SERVFAIL, [1.9, 2.1, 1.9, 2.0, 1.95, 2.1, 1.9]) == [
        Result.OK] + [Result.WARN] * 6


def test_the_warning_clears_once_the_value_is_below_the_band():
    # 5% of 2 is 0.1: the warning holds down to 1.9 and clears below it.
    assert levels(SERVFAIL, [2.1, 1.91, 1.89]) == [Result.WARN, Result.WARN, Result.OK]
    assert levels(SERVFAIL, [2.1, 1.89, 1.95]) == [Result.WARN, Result.OK, Result.OK]


def test_critical_steps_down_through_its_own_band():
    assert levels(SERVFAIL, [10.5, 9.6, 9.4, 1.0]) == [
        Result.FAIL, Result.FAIL, Result.WARN, Result.OK]


def test_below_thresholds_clear_upwards():
    th = Thresholds(direction="below", warn=20, crit=10)
    assert levels(th, [19, 20.5, 21.5]) == [Result.WARN, Result.WARN, Result.OK]


def test_an_explicit_band_and_zero_turn_the_default_off():
    assert levels(Thresholds(warn=2, hysteresis=0.5), [2.1, 1.6, 1.4]) == [
        Result.WARN, Result.WARN, Result.OK]
    assert levels(Thresholds(warn=2, hysteresis=0), [2.1, 1.99]) == [Result.WARN, Result.OK]
    with pytest.raises(ValidationError):
        Thresholds(warn=2, hysteresis=-1)


def test_existing_configs_stay_valid():
    cfg = make_config([{"name": "a", "type": "tcp", "host": "192.0.2.1", "port": 1,
                        "thresholds": {"direction": "above", "warn": 2, "crit": 10}}])
    assert cfg.monitors[0].thresholds.hysteresis is None


def test_without_a_held_level_the_comparison_is_unchanged():
    res = apply_thresholds(CheckResult(Result.OK, "x", value=1.95, unit="%"), SERVFAIL)
    assert res.result is Result.OK
    res = apply_thresholds(CheckResult(Result.OK, "x", value=1.95, unit="%"), SERVFAIL,
                           Result.WARN)
    assert res.result is Result.WARN and res.message == "x (warning threshold 2% crossed)"


def test_a_count_unit_is_left_out_of_the_message():
    assert unit_suffix("{entity}") == "" and unit_suffix("%") == "%"
    res = apply_thresholds(CheckResult(Result.OK, "3 alerts", value=3, unit="{alert}"),
                           Thresholds(warn=1))
    assert res.message == "3 alerts (warning threshold 1 crossed)"


class Scripted(Check):
    def __init__(self, values: list[float]) -> None:
        cfg = make_config([{"name": "a", "type": "tcp", "host": "192.0.2.1", "port": 1,
                            "thresholds": {"direction": "above", "warn": 2, "crit": 10}}])
        super().__init__(cfg.monitors[0], cfg)
        self.values = list(values)

    async def probe(self) -> CheckResult:
        v = self.values.pop(0)
        return CheckResult(Result.OK, "rate", value=v, unit="%") if v >= 0 else \
            CheckResult.fail("no reply")


def test_a_check_keeps_its_level_between_polls():
    check = Scripted([1.9, 2.1, 1.9, 2.0, -1, 1.95, 1.8])

    async def run():
        return [(await check.run()).result for _ in range(7)]

    assert asyncio.run(run()) == [Result.OK, Result.WARN, Result.WARN, Result.WARN,
                                  Result.FAIL, Result.WARN, Result.OK]
