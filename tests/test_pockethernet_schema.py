"""The pockethernet.report v1 schema: round trip, caps, non-finite numbers, transcripts."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from watchpost_pockethernet import schema
from watchpost_pockethernet.schema import (MAX_REPORT_BYTES, Report, ReportError, parse_report)

FIXTURE = Path(__file__).parent / "fixtures" / "pockethernet" / "report_v1.json"


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def reject(raw, status: int = 422) -> str:
    body = raw if isinstance(raw, (bytes, str)) else json.dumps(raw)
    with pytest.raises(ReportError) as err:
        parse_report(body)
    assert err.value.status == status
    return err.value.reason


def test_valid_fixture_round_trips():
    report = parse_report(FIXTURE.read_bytes())
    assert report.report_id == "7f3c2a10-5b1e-4c52-9d1a-0a6e2f9b8c41"
    assert report.device.serial == 1234567
    assert report.properties.link_speed_mbps == 1000
    assert report.properties.pair_1_2_length_m == 42.5
    assert report.neighbors[0].lldp.port_id == "Gi1/0/5"
    # Location and Wi-Fi fields are accepted, as the owner decided.
    assert report.geo.latitude_deg == 52.370216
    assert report.wifi.ssid == "hq-staff"
    dumped = report.model_dump(by_alias=True, mode="json")
    again = parse_report(json.dumps(dumped))
    assert again == report
    assert Report.model_validate(dumped).model_dump(by_alias=True, mode="json") == dumped


def test_minimal_report_is_valid_and_properties_default_empty():
    raw = {"schema": "pockethernet.report", "version": 1, "report_id": "r1",
           "taken_at_ms": 1, "device": {"serial": 1, "mac": "02:00", "hw_version": 1,
                                        "sw_version": 1}}
    report = parse_report(json.dumps(raw))
    assert report.revision == 1 and report.steps == [] and report.geo is None
    assert report.properties.model_dump(exclude_none=True) == {}


@pytest.mark.parametrize("change", [
    {"schema": "pockethernet.other"}, {"version": 2}, {"version": 0}, {"version": "1"},
    {"report_id": ""}, {"report_id": "has space"}, {"revision": 0}, {"taken_at_ms": -1},
    {"status": "done"},
])
def test_wrong_envelope_is_rejected(change):
    raw = fixture() | change
    reject(raw)


def test_missing_schema_or_version_is_rejected():
    for key in ("schema", "version", "report_id", "device", "taken_at_ms"):
        raw = fixture()
        del raw[key]
        reject(raw)


def test_unknown_fields_are_rejected_at_every_level():
    for path in ([], ["device"], ["properties"], ["steps", 0], ["neighbors", 0, "lldp"],
                 ["geo"], ["wifi"], ["poe"], ["tool_results", 0]):
        raw = fixture()
        node = raw
        for part in path:
            node = node[part]
        node["surprise"] = 1
        assert "surprise" in reject(raw)


def test_property_names_are_an_allowlist():
    raw = fixture()
    raw["properties"]["custom.note"] = "x"
    reject(raw)
    raw = fixture()
    raw["properties"]["link_speed_mbps"] = "fast"
    reject(raw)
    raw = fixture()
    raw["properties"]["duplex"] = "both"
    reject(raw)


@pytest.mark.parametrize("where,key,value", [
    (["properties"], "jack_label", "x" * (schema.MAX_NAME + 1)),
    (["notes"], None, "x" * (schema.MAX_NOTES + 1)),
    (["warnings", 0], None, "x" * (schema.MAX_TEXT + 1)),
    (["steps", 0, "fields", 0], "value", "x" * (schema.MAX_TEXT + 1)),
    (["properties"], "vlan", 4095),
    (["properties"], "link_speed_mbps", 400_001),
    (["properties"], "pair_1_2_length_m", 1001),
    (["properties"], "pair_1_2_length_m", -0.5),
    (["geo"], "latitude_deg", 90.5),
    (["geo"], "longitude_deg", -181),
])
def test_value_caps_are_enforced(where, key, value):
    raw = fixture()
    node = raw
    for part in where[:-1] if key is None else where:
        node = node[part]
    if key is None:
        node[where[-1]] = value
    else:
        node[key] = value
    reject(raw)


def test_list_caps_are_enforced():
    cases = {
        "steps": (schema.MAX_STEPS + 1, fixture()["steps"][0]),
        "neighbors": (schema.MAX_NEIGHBORS + 1, fixture()["neighbors"][0]),
        "warnings": (schema.MAX_WARNINGS + 1, "w"),
        "tool_results": (schema.MAX_TOOL_RESULTS + 1, fixture()["tool_results"][0]),
    }
    for name, (count, item) in cases.items():
        raw = fixture()
        raw[name] = [copy.deepcopy(item) for _ in range(count)]
        assert name in reject(raw), name
        raw[name] = raw[name][:-1]
        parse_report(json.dumps(raw))  # exactly at the cap is accepted
    raw = fixture()
    raw["dhcp"]["dns_servers"] = ["10.0.0.1"] * (schema.MAX_ADDRESSES + 1)
    reject(raw)


def test_control_characters_are_rejected_but_notes_keep_newlines():
    raw = fixture()
    raw["properties"]["jack_label"] = "a\u0000b"
    reject(raw)
    raw = fixture()
    raw["properties"]["room"] = "line1\nline2"
    reject(raw)
    raw = fixture()
    raw["notes"] = "ok\nnew line\ttab"
    parse_report(json.dumps(raw))
    raw["notes"] = "bell\u0007"
    reject(raw)


def test_oversized_body_is_413_and_deep_body_is_400():
    pad = "x" * (MAX_REPORT_BYTES + 1)
    assert reject(pad.encode(), 413) == "report is too large"
    raw = fixture()
    raw["notes"] = "n" * 4096
    assert len(json.dumps(raw)) < MAX_REPORT_BYTES
    deep = b"[" * (schema.MAX_DEPTH + 1) + b"]" * (schema.MAX_DEPTH + 1)
    assert reject(deep, 400) == "report is nested too deeply"
    # Brackets inside a string do not count as nesting.
    raw = fixture()
    raw["notes"] = "[" * 200
    parse_report(json.dumps(raw))


def test_not_json_or_not_an_object_is_rejected():
    reject(b"not json")
    reject(b"[1, 2]")
    reject(b"\xff\xfe")


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_rejected(literal):
    marker = '"pair_1_2_length_m": 42.5'
    text = FIXTURE.read_text(encoding="utf-8")
    assert marker in text
    text = text.replace(marker, f'"pair_1_2_length_m": {literal}')
    assert reject(text) == "body is not valid JSON"


def test_non_finite_floats_are_rejected_when_already_parsed():
    for field in ("pair_1_2_length_m", "poe_voltage_v", "poe_load_w"):
        for bad in (float("nan"), float("inf")):
            raw = fixture()
            raw["properties"][field] = bad
            with pytest.raises(ValueError):
                Report.model_validate(raw)
    raw = fixture()
    raw["geo"]["accuracy_m"] = float("nan")
    with pytest.raises(ValueError):
        Report.model_validate(raw)


@pytest.mark.parametrize("name", ["transcript", "transcript_truncated", "script_runs",
                                  "scriptRuns", "ScriptRuns", "script-values", "scriptHosts"])
def test_ssh_transcript_and_script_fields_are_rejected_at_any_depth(name):
    for path in ([], ["device"], ["steps", 0], ["steps", 0, "fields", 0],
                 ["tool_results", 0], ["neighbors", 0, "lldp"]):
        raw = fixture()
        node = raw
        for part in path:
            node = node[part]
        node[name] = "ssh session text with password hunter2"
        reason = reject(raw)
        assert "ssh transcripts and script values are never accepted" in reason
        assert "hunter2" not in reason


def test_a_full_app_script_run_is_rejected_whole():
    raw = fixture()
    raw["script_runs"] = [{"scriptName": "show vlan", "hosts": [{
        "host": "sw1", "status": "PASSED", "transcript": "enable\nshow run", "values": []}]}]
    reject(raw)


def test_error_messages_never_echo_the_rejected_value():
    raw = fixture()
    raw["properties"]["vlan"] = 987654321
    assert "987654321" not in reject(raw)
