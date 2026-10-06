"""Fan header names follow exactly the rule hostwatch-control uses (hostwatch/control/actions_linux.py,
HEADER_ID, and its test_bad_header_names_are_rejected_before_any_call).

Observe checks a header in the wizard form, in the enrolment API, in the install script builder, in
the control queue and in the browser. All of them must accept and refuse the same names, or a header
that Observe lets through would be refused later by the host and a fan would silently stay out of
the allowlist. The rejected cases below are hostwatch's own test cases, plus the names the old
Observe rule let through: dots, which hostwatch does not allow."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from observe import enrol, scripts
from observe_control import actions

# hostwatch: HEADER_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,31}$"), matched with fullmatch.
HOSTWATCH_RULE = r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,31}$"

# Copied from hostwatch tests/test_control_actions_linux.py (the non-string ones are not names).
HOSTWATCH_REJECTED = ["", "pwm 1", "../x", "a;b", "$(id)", "-pwm1", "pw@m", "a" * 40]
OLD_OBSERVE_ONLY = ["pwm.1", ".x", "a.b", "pwm1\n", "a" * 33]
ACCEPTED = ["pwm1", "pwm2", "fan1", "pwm-fan", "_x", "a", "A_b-9", "a" * 32]

STATIC = Path(__file__).parent.parent / "observe" / "static"


def test_the_server_patterns_are_hostwatchs_pattern():
    assert enrol._HEADER.pattern == HOSTWATCH_RULE
    assert scripts._HEADER.pattern == HOSTWATCH_RULE
    assert actions._HEADER.pattern in (HOSTWATCH_RULE, HOSTWATCH_RULE[1:-1])


def test_the_browser_pattern_is_hostwatchs_pattern():
    logic = (STATIC / "js" / "wizard-logic.js").read_text(encoding="utf-8")
    m = re.search(r"export const HEADER_RE = /(.+)/;", logic)
    assert m and m.group(1) == HOSTWATCH_RULE


@pytest.mark.parametrize("name", ACCEPTED)
def test_accepted_names_pass_everywhere(name):
    assert enrol._HEADER.fullmatch(name) and scripts._HEADER.fullmatch(name)
    assert actions._HEADER.fullmatch(name)
    assert enrol._fans([name]) == [(name, None)]


@pytest.mark.parametrize("name", HOSTWATCH_REJECTED + OLD_OBSERVE_ONLY)
def test_rejected_names_fail_everywhere(name):
    assert not enrol._HEADER.fullmatch(name) and not scripts._HEADER.fullmatch(name)
    assert not actions._HEADER.fullmatch(name)
    with pytest.raises(enrol.EnrolError):
        enrol._fans([name])
    with pytest.raises(enrol.EnrolError):
        enrol._fans([{"header": name}])
    with pytest.raises(scripts.ScriptError):
        scripts._need(scripts._HEADER, name, "a fan header")


@pytest.mark.parametrize("value", [5, None, ["pwm1"]])
def test_non_string_names_are_refused(value):
    with pytest.raises(enrol.EnrolError):
        enrol._fans([value])


def test_a_header_stored_under_the_old_rule_gives_a_message_that_says_how_to_recover():
    allow = {"fans": [{"header": "pwm.1"}], "services": []}
    with pytest.raises(scripts.ScriptError) as err:
        scripts.control_toml("host1", allow, "")
    assert "a fan header has characters that are not allowed" in str(err.value)
    assert "remove or rename that header" in str(err.value)
