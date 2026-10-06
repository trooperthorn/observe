"""Threshold rules (docs/DATA-API-DESIGN.md section 10.4): each rule kind firing and clearing,
hysteresis, missing data, the per-series ring and the validation and storage of rule
configuration. Time comes from a faked clock, never from the wall."""

from __future__ import annotations

import json

import pytest

from observe import rules
from observe.rules import CRITICAL, OK, WARNING, RuleEngine, RuleError, SeriesState

from .test_recheck import storage  # noqa: F401  (storage is a fixture)

KEY, HOST, METRIC = "s1", "h1", "cpu"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def rule(**kw):
    base = {"id": "r", "kind": "consecutive", "metric": METRIC, "condition": "above",
            "warn": 90, "x": 3}
    base.update(kw)
    return rules.parse_rule(base)


def window_rule(agg, window=60, **kw):
    base = {"id": "r", "kind": "window", "metric": METRIC, "condition": "above", "warn": 90,
            "window": window, "agg": agg, "clear": 1}
    base.update(kw)
    return rules.parse_rule(base)


def missing_rule(**kw):
    return rules.parse_rule({"id": "m", "kind": "missing", "metric": METRIC, **kw})


def engine(*rs, capacity=rules.RING_CAPACITY):
    clock = Clock()
    return RuleEngine(rs, clock=clock, capacity=capacity), clock


def feed(eng, clock, values, step=10.0):
    """Record each value one poll apart; returns the level of the first rule after each."""
    levels = []
    for v in values:
        clock.now += step
        out = eng.observe(KEY, HOST, METRIC, v)
        levels.append(out[0].level if out else None)
    return levels


# ---- consecutive ----------------------------------------------------------------------------

def test_consecutive_fires_after_x_in_a_row_and_clears_after_x_good():
    eng, clock = engine(rule())
    assert feed(eng, clock, [95, 95, 95]) == [OK, OK, WARNING]
    assert feed(eng, clock, [10, 10, 10]) == [WARNING, WARNING, OK]


def test_consecutive_is_broken_by_one_good_poll():
    eng, clock = engine(rule())
    assert feed(eng, clock, [95, 95, 10, 95, 95]) == [OK] * 5


def test_critical_beats_warning():
    eng, clock = engine(rule(warn=80, crit=95, x=2))
    assert feed(eng, clock, [85, 85, 99, 99]) == [OK, WARNING, WARNING, CRITICAL]


@pytest.mark.parametrize("cond,warn,bad,good", [
    ("below", 10, 5, 50), ("equal", 7, 7, 8), ("not_equal", 7, 8, 7),
    ("outside", [10, 20], 25, 15)])
def test_each_condition_fires_and_clears(cond, warn, bad, good):
    eng, clock = engine(rule(condition=cond, warn=warn, x=1, clear=1))
    assert feed(eng, clock, [good, bad, good]) == [OK, WARNING, OK]


# ---- hysteresis -----------------------------------------------------------------------------

def test_hysteresis_stops_a_value_at_the_line_from_flapping():
    eng, clock = engine(rule(x=2, clear=4))
    levels = feed(eng, clock, [95, 95, 10, 95, 10, 10, 95, 10, 10, 10])
    # Fires once and stays raised through every dip shorter than 4 polls in a row.
    assert levels == [OK] + [WARNING] * 9
    assert feed(eng, clock, [10]) == [OK]


def test_a_clear_count_of_one_flaps_without_hysteresis():
    eng, clock = engine(rule(x=1, clear=1))
    assert feed(eng, clock, [95, 10, 95, 10]) == [WARNING, OK, WARNING, OK]


def test_default_clear_equals_x():
    assert rule(x=3).clear == 3


def test_escalation_is_immediate_and_decay_waits_for_the_clear_count():
    eng, clock = engine(rule(warn=80, crit=95, x=1, clear=2))
    assert feed(eng, clock, [85, 99, 85, 85]) == [WARNING, CRITICAL, CRITICAL, WARNING]


def test_a_changed_verdict_is_flagged_once():
    eng, clock = engine(rule(x=1))
    clock.now += 1
    first = eng.observe(KEY, HOST, METRIC, 95)[0]
    clock.now += 1
    second = eng.observe(KEY, HOST, METRIC, 96)[0]
    assert first.changed and not second.changed


# ---- X of Y ---------------------------------------------------------------------------------

def test_ratio_fires_at_x_of_the_last_y_and_clears_when_it_falls_below():
    eng, clock = engine(rule(kind="ratio", x=3, y=4, clear=2, warn=55))
    assert feed(eng, clock, [60, 10, 60, 60]) == [OK, OK, OK, WARNING]
    assert feed(eng, clock, [10, 10, 10]) == [WARNING, OK, OK]


def test_ratio_counts_only_the_last_y_polls():
    eng, clock = engine(rule(kind="ratio", x=2, y=3, warn=50, clear=1))
    assert feed(eng, clock, [60, 60, 10, 10, 10]) == [OK, WARNING, WARNING, OK, OK]


# ---- window ---------------------------------------------------------------------------------

@pytest.mark.parametrize("agg,values,fires", [
    ("avg", [80, 100, 100], True), ("avg", [80, 90, 100], False),
    ("max", [10, 99, 10], True), ("max", [10, 80, 10], False),
    ("min", [95, 91, 92], True), ("min", [95, 10, 92], False)])
def test_window_min_max_and_average_cross_the_value(agg, values, fires):
    eng, clock = engine(window_rule(agg))
    assert feed(eng, clock, values)[-1] == (WARNING if fires else OK)


def test_window_forgets_samples_older_than_the_window():
    eng, clock = engine(window_rule("max", window=30))
    assert feed(eng, clock, [99])[-1] == WARNING
    assert feed(eng, clock, [10], step=31)[-1] == OK  # the 99 is 31 s old, outside the window


# ---- missing data ---------------------------------------------------------------------------

def test_a_missing_value_is_unknown_by_default():
    eng, clock = engine(rule(x=2, clear=1))
    assert feed(eng, clock, [95, None, 95]) == [OK, OK, WARNING]  # the gap is left out


def test_a_missing_value_can_count_as_breaching_or_not_breaching():
    breaching, c1 = engine(rule(x=2, missing="breaching"))
    assert feed(breaching, c1, [95, None]) == [OK, WARNING]
    fine, c2 = engine(rule(x=2, missing="not_breaching"))
    assert feed(fine, c2, [95, None, 95]) == [OK, OK, OK]


def test_missing_data_fires_after_the_gap_and_clears_on_data():
    eng, clock = engine(missing_rule(gap=60, severity="critical"))
    clock.now += 1
    assert eng.observe(KEY, HOST, METRIC, 5.0)[0].level == OK
    clock.now += 60
    assert [v.level for v in eng.tick()] == [OK]  # exactly the gap is not yet past it
    clock.now += 1
    assert [v.level for v in eng.tick()] == [CRITICAL]
    clock.now += 600
    assert [v.level for v in eng.tick()] == [CRITICAL]
    clock.now += 1
    assert eng.observe(KEY, HOST, METRIC, 5.0)[0].level == OK


def test_an_empty_poll_does_not_reset_the_gap():
    eng, clock = engine(missing_rule(gap=30))
    clock.now += 1
    eng.observe(KEY, HOST, METRIC, 1.0)
    clock.now += 40
    assert eng.observe(KEY, HOST, METRIC, None)[0].level == WARNING


def test_fewer_than_x_of_the_last_y_polls_returned_data():
    eng, clock = engine(missing_rule(x=8, y=10, clear=2))
    assert feed(eng, clock, [1.0] * 9)[-1] == OK
    assert feed(eng, clock, [None, None])[-1] == OK  # 8 of 10 is still enough
    assert feed(eng, clock, [None])[-1] == WARNING  # 7 of 10
    assert feed(eng, clock, [1.0] * 10)[-1] == OK  # the three gaps leave the last 10


def test_the_share_of_polls_waits_until_y_polls_have_been_seen():
    eng, clock = engine(missing_rule(x=3, y=4))
    assert feed(eng, clock, [None, None, None]) == [OK, OK, OK]
    assert feed(eng, clock, [None]) == [WARNING]


def test_a_series_that_never_returned_data_is_missing_once_a_tick_runs():
    eng, clock = engine(missing_rule(gap=30))
    clock.now += 1
    eng.observe(KEY, HOST, METRIC, None)
    assert [v.level for v in eng.tick()] == [WARNING]


# ---- the ring -------------------------------------------------------------------------------

def test_the_ring_keeps_the_latest_value_and_evicts_the_oldest_at_capacity():
    st = SeriesState(capacity=3)
    for i in range(5):
        st.add(float(i), float(i * 10))
    assert [v for _, v in st.ring] == [20.0, 30.0, 40.0]
    assert (st.latest_ts, st.latest_value) == (4.0, 40.0)


def test_an_engine_ring_is_bounded_and_no_rule_can_look_past_it():
    eng, clock = engine(rule(x=2), capacity=4)
    feed(eng, clock, range(10))
    assert len(eng.series(KEY).ring) == 4
    assert eng.series(KEY).latest_value == 9
    assert rules.MAX_X == rules.MAX_Y == rules.RING_CAPACITY


def test_seed_fills_a_ring_without_evaluating():
    eng, _ = engine(rule(x=1))
    eng.seed(KEY, HOST, METRIC, [(1.0, 99.0), (2.0, 99.0)])
    assert eng.level("r", KEY) == OK
    assert len(eng.series(KEY).ring) == 2


def test_rules_are_chosen_by_host_then_global_and_disabled_rules_are_skipped():
    eng, _ = engine(rule(id="g"), rule(id="h", host=HOST, warn=50),
                    rule(id="off", enabled=False))
    assert [r.id for r in eng.rules_for(HOST, METRIC)] == ["h"]
    assert [r.id for r in eng.rules_for("other", METRIC)] == ["g"]
    assert eng.rules_for(HOST, "mem") == []


def test_replacing_the_rules_drops_state_of_rules_that_are_gone():
    eng, clock = engine(rule(x=1))
    feed(eng, clock, [95])
    assert eng.level("r", KEY) == WARNING
    eng.set_rules([])
    assert eng.level("r", KEY) == OK


# ---- validation -----------------------------------------------------------------------------

# (kind, extra fields to set, field to remove or None, text the refusal must contain)
BAD = [
    ({"id": None}, "id"),
    ({"id": "bad id!"}, "id"),
    ({"kind": "sum"}, "kind"),
    ({"metric": ""}, "metric"),
    ({"metric": "a b"}, "metric"),
    ({"host": "no spaces"}, "host"),
    ({"condition": "near"}, "condition"),
    ({"warn": None}, "warn value"),
    ({"warn": "90"}, "warn"),
    ({"warn": True}, "warn"),
    ({"warn": float("nan")}, "warn"),
    ({"warn": float("inf")}, "warn"),
    ({"warn": [1, 2]}, "warn"),
    ({"warn": 90, "crit": 80}, "crit"),
    ({"x": 0}, "x"),
    ({"x": 101}, "x"),
    ({"x": 1.5}, "x"),
    ({"x": True}, "x"),
    ({"x": None}, "x"),
    ({"y": 5}, "only x"),
    ({"clear": 0}, "clear"),
    ({"clear": 1000}, "clear"),
    ({"missing": "maybe"}, "missing"),
    ({"enabled": "yes"}, "enabled"),
    ({"bogus": 1}, "cannot set"),
    ({"gap": 5}, "missing-data"),
    ({"kind": "ratio", "y": 3, "x": 4}, "x"),
    ({"kind": "ratio", "y": 1000, "x": 2}, "y"),
    ({"kind": "ratio", "y": 3, "x": 2, "window": 5}, "only x and y"),
    ({"kind": "window", "window": 0, "agg": "max", "x": None}, "window"),
    ({"kind": "window", "window": 1e9, "agg": "max", "x": None}, "window"),
    ({"kind": "window", "window": 5, "agg": "median", "x": None}, "agg"),
    ({"kind": "window", "window": 5, "agg": "max"}, "only window and agg"),
    ({"condition": "outside", "warn": 5}, "pair"),
    ({"condition": "outside", "warn": [9, 3]}, "low"),
    ({"condition": "outside", "warn": [3, 9], "crit": [4, 8]}, "contain"),
    ({"condition": "below", "warn": 10, "crit": 20}, "crit"),
]


@pytest.mark.parametrize("change,text", BAD)
def test_invalid_rule_configuration_is_refused(change, text):
    raw = {"id": "r", "kind": "consecutive", "metric": METRIC, "condition": "above",
           "warn": 90, "x": 3}
    for key, value in change.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    with pytest.raises(RuleError) as err:
        rules.parse_rule(raw)
    assert text in str(err.value)


@pytest.mark.parametrize("raw", [
    {},
    {"gap": 5, "x": 1, "y": 2},
    {"gap": 0},
    {"gap": 5, "warn": 1},
    {"x": 5, "y": 2},
    {"gap": 5, "severity": "fatal"},
])
def test_invalid_missing_data_rules_are_refused(raw):
    with pytest.raises(RuleError):
        missing_rule(**raw)


def test_a_rule_list_must_be_a_bounded_list_of_objects_with_unique_ids():
    ok = {"id": "a", "kind": "consecutive", "metric": "cpu", "condition": "above", "warn": 1,
          "x": 1}
    too_many = [dict(ok, id=f"r{i}") for i in range(rules.MAX_RULES + 1)]
    for bad in (None, "x", {"rules": "x"}, [1], [ok, ok], too_many):
        with pytest.raises(RuleError):
            rules.validate(bad)
    assert [r.id for r in rules.validate({"rules": [ok]})] == ["a"]


# ---- storage --------------------------------------------------------------------------------

DOC = [
    {"id": "cpu-hot", "kind": "consecutive", "metric": "cpu", "condition": "above", "warn": 90,
     "x": 5},
    {"id": "disk-temp", "kind": "ratio", "metric": "temp", "condition": "above", "crit": 55,
     "x": 3, "y": 4, "host": "nas"},
    {"id": "cpu-avg", "kind": "window", "metric": "cpu", "condition": "outside",
     "warn": [10, 90], "window": 300, "agg": "avg"},
    {"id": "gone", "kind": "missing", "metric": "cpu", "x": 8, "y": 10},
]


async def test_rules_are_saved_through_storage_with_one_audit_row_per_change(storage):  # noqa: F811
    new = rules.validate(DOC)
    result = await storage.write(
        lambda db: rules.save(db, new, now=5.0, actor="root", remote="10.0.0.1"),
        touches=("admin",))
    assert result["old"] == [] and len(result["new"]) == 4
    assert await storage.read(rules.load) == new
    again = await storage.write(
        lambda db: rules.save(db, new[:1], now=6.0, actor="root", remote="10.0.0.1"))
    assert [r["id"] for r in again["old"]] == [r.id for r in new]
    assert [r.id for r in await storage.read(rules.load)] == ["cpu-hot"]
    audit = await storage.fetchall(
        "SELECT actor, kind, detail FROM audit WHERE kind = 'rules_changed' ORDER BY ts")
    assert [tuple(a[:2]) for a in audit] == [("root", "rules_changed")] * 2
    assert len(json.loads(audit[1][2])["old"]) == 4


async def test_a_corrupt_stored_document_loads_as_no_rules(storage):  # noqa: F811
    for text in ("not json", json.dumps([{"id": "x", "kind": "nope"}]), json.dumps({"a": 1})):
        await storage.execute(
            "INSERT INTO app_settings (key, value, updated) VALUES (?, ?, 1) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (rules.SETTINGS_KEY, text))
        assert await storage.read(rules.load) == ()


@pytest.mark.parametrize("raw", DOC)
def test_a_rule_survives_a_round_trip_through_its_dict(raw):
    parsed = rules.parse_rule(raw)
    assert rules.parse_rule(parsed.as_dict()) == parsed
