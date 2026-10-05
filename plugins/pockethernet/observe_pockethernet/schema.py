"""The Pockethernet field report wire schema: `pockethernet.report`, version 1.

This is the body the phone uploads (docs/FIELD-DATA.md). It is a new versioned
schema, not the hostwatch Batch. It is shaped from the Pockethernet app's
saved Report model but is deliberately narrower, and every field name that
holds a measurement carries its unit (`speed_mbps`, `length_m`, `poe_load_w`).

The rules, all enforced here and tested:

- Unknown fields are rejected, not ignored. The phone and Observe ship
  together, so a field this schema does not name is a mistake or an attack.
- SSH transcripts and script values are never accepted, at any depth. They are
  refused by name before validation, so a future model change cannot let one in.
- Port properties are an allowlist with a fixed type per name.
- Strings, lists and nesting are capped, and the whole body is capped at
  MAX_REPORT_BYTES. Non-finite numbers are rejected in every float field.
- Control characters are refused in every string. Free text may keep a newline.
- Location and Wi-Fi fields are accepted: the owner decided they are sent by
  default, and the app can turn each off. They are stored with the report and
  never become a port property.

`parse_report` is the single entry point for raw bytes. It checks the size, the
nesting depth and the JSON before the model sees anything, and raises
ReportError with a status that an HTTP layer can use.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, StringConstraints, ValidationError,
                      model_validator)

SCHEMA_NAME = "pockethernet.report"
SCHEMA_VERSION = 1

MAX_REPORT_BYTES = 262_144  # 256 KiB, as in docs/FIELD-DATA.md
MAX_DEPTH = 16  # a real report nests about 7 deep
MAX_NAME = 128
MAX_TEXT = 1024
MAX_NOTES = 4096
MAX_STEPS = 64
MAX_FIELDS = 64
MAX_NEIGHBORS = 8
MAX_ADDRESSES = 16
MAX_TOOL_RESULTS = 32
MAX_DETAILS = 50
MAX_WARNINGS = 32

# Printable text. Newline and tab are refused here and allowed only in Notes.
_NO_CONTROL = r"^[^\x00-\x1f\x7f]*$"
_NO_CONTROL_NL = r"^[^\x00-\x08\x0b-\x1f\x7f]*$"
Name = Annotated[str, StringConstraints(max_length=MAX_NAME, pattern=_NO_CONTROL)]
Text = Annotated[str, StringConstraints(max_length=MAX_TEXT, pattern=_NO_CONTROL)]
Notes = Annotated[str, StringConstraints(max_length=MAX_NOTES, pattern=_NO_CONTROL_NL)]
ReportId = Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME,
                                            pattern=r"^[A-Za-z0-9._:-]+$")]
Real = Annotated[float, Field(allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Vlan = Annotated[int, Field(ge=0, le=4094)]
EpochMs = Annotated[int, Field(ge=0, le=253_402_300_799_999)]  # up to the year 9999

# Names that carry SSH transcripts or script values in the app's Report model, compared with
# case and underscores ignored so scriptRuns, script_runs and ScriptRuns all match.
_FORBIDDEN = frozenset({"transcript", "transcripts", "transcripttruncated", "scriptruns",
                        "scriptrun", "scripthosts", "scriptvalues", "scriptsteps"})


class ReportError(ValueError):
    """A body that must not be stored. `status` is the HTTP status that fits the reason."""

    def __init__(self, reason: str, status: int = 422) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Device(_Strict):
    """The tester. `serial` is the always-sent tester serial."""

    serial: Annotated[int, Field(ge=0, le=2**63 - 1)]
    mac: Name
    hw_version: Annotated[int, Field(ge=0, le=2**31 - 1)]
    sw_version: Annotated[int, Field(ge=0, le=2**31 - 1)]


class Geo(_Strict):
    latitude_deg: Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
    longitude_deg: Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
    accuracy_m: NonNegative | None = None
    fix_time_ms: EpochMs | None = None


class Wifi(_Strict):
    ssid: Name | None = None
    bssid: Name | None = None
    rssi_dbm: Annotated[int, Field(ge=-200, le=0)] | None = None
    channel: Annotated[int, Field(ge=0, le=500)] | None = None


class Site(_Strict):
    """The app's site model. `port_id` is the site port id, `building.room.panel.NN`."""

    site: Name = ""
    building: Name = ""
    room: Name = ""
    panel: Name = ""
    port_id: Name = ""


class Measure(_Strict):
    """One measured value. `unit` is null for a value without one."""

    name: Name
    value: Text
    unit: Name | None = None


class Step(_Strict):
    step: Name
    label: Name
    status: Literal["ok", "failed", "warn"]
    error: Text | None = None
    fields: Annotated[list[Measure], Field(max_length=MAX_FIELDS)] = []


class Lldp(_Strict):
    chassis_id_subtype: Annotated[int, Field(ge=0, le=255)] | None = None
    chassis_id: Name | None = None
    port_id_subtype: Annotated[int, Field(ge=0, le=255)] | None = None
    port_id: Name | None = None
    ttl_s: Annotated[int, Field(ge=0, le=65535)] | None = None
    port_description: Text | None = None
    system_name: Name | None = None
    system_description: Text | None = None
    management_address: Name | None = None
    port_vlan_id: Vlan | None = None


class Cdp(_Strict):
    version: Annotated[int, Field(ge=0, le=255)]
    ttl_s: Annotated[int, Field(ge=0, le=65535)]
    device_id: Name | None = None
    addresses: Annotated[list[Name], Field(max_length=MAX_ADDRESSES)] = []
    port_id: Name | None = None
    platform: Text | None = None
    software_version: Text | None = None
    native_vlan: Vlan | None = None
    voice_vlan: Vlan | None = None
    full_duplex: bool | None = None
    power_consumption_mw: Annotated[int, Field(ge=0, le=2**31 - 1)] | None = None


class Neighbor(_Strict):
    protocol: Literal["lldp", "cdp"]
    device_id: Name | None = None
    port_id: Name | None = None
    system_name: Name | None = None
    description: Text | None = None
    management_addresses: Annotated[list[Name], Field(max_length=MAX_ADDRESSES)] = []
    vlan_id: Vlan | None = None
    voice_vlan_id: Vlan | None = None
    source_mac: Name | None = None
    vendor: Name | None = None
    lldp: Lldp | None = None
    cdp: Cdp | None = None


class Dhcp(_Strict):
    source: Name
    address: Name | None = None
    subnet_mask: Name | None = None
    routers: Annotated[list[Name], Field(max_length=MAX_ADDRESSES)] = []
    dns_servers: Annotated[list[Name], Field(max_length=MAX_ADDRESSES)] = []
    server_id: Name | None = None
    domain_name: Name | None = None
    lease_s: Annotated[int, Field(ge=0, le=2**40)] | None = None


class Link(_Strict):
    link_up: bool
    speed_mbps: Annotated[int, Field(ge=0, le=400_000)]
    full_duplex: bool
    mdix: bool


class Poe(_Strict):
    """What the PoE check found. `inferred` is true when the class was not measured."""

    poe_class: Annotated[int, Field(ge=0, le=8)] | None = None
    voltage_v: NonNegative | None = None
    load_w: NonNegative | None = None
    inferred: bool = False


class ToolResult(_Strict):
    """A Network toolkit run saved into the report. Plain strings and numbers only."""

    tool: Name
    title: Name
    target: Name = ""
    started_ms: EpochMs = 0
    verdict: Literal["ok", "warn", "fail", "info"]
    headline: Text
    fields: Annotated[list[Measure], Field(max_length=MAX_FIELDS)] = []
    details: Annotated[list[Text], Field(max_length=MAX_DETAILS)] = []


class Properties(_Strict):
    """The allowlisted port properties, one typed field per name (docs/FIELD-DATA.md).

    A name that is not here is rejected. Core `custom.<name>` properties are added by an
    admin by hand and are never accepted from a phone.
    """

    jack_label: Name | None = None
    panel: Name | None = None
    room: Name | None = None
    site: Name | None = None
    cable_verdict: Literal["pass", "warn", "fail", "unknown"] | None = None
    pair_1_2_length_m: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] | None = None
    pair_3_6_length_m: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] | None = None
    pair_4_5_length_m: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] | None = None
    pair_7_8_length_m: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] | None = None
    pair_fault: Name | None = None
    link_speed_mbps: Annotated[int, Field(ge=0, le=400_000)] | None = None
    duplex: Literal["full", "half"] | None = None
    poe_class: Annotated[int, Field(ge=0, le=8)] | None = None
    poe_voltage_v: NonNegative | None = None
    poe_load_w: NonNegative | None = None
    vlan: Vlan | None = None
    voice_vlan: Vlan | None = None
    dhcp_ok: bool | None = None
    dns_ok: bool | None = None
    last_tested_at_ms: EpochMs | None = None
    tester_serial: Annotated[int, Field(ge=0, le=2**63 - 1)] | None = None


def _normal(key: str) -> str:
    return key.replace("_", "").replace("-", "").lower()


def _scan(raw: Any) -> None:
    """Refuse a forbidden key or too deep a nesting anywhere in the raw body.

    Iterative, so a hostile body cannot recurse. Only the reason is reported, never a value.
    """
    stack: list[tuple[Any, int]] = [(raw, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise ValueError("report is nested too deeply")
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and _normal(key) in _FORBIDDEN:
                    raise ValueError("ssh transcripts and script values are never accepted")
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)


class Report(_Strict):
    schema_name: Literal["pockethernet.report"] = Field(alias="schema")
    version: Literal[1]
    report_id: ReportId
    revision: Annotated[int, Field(ge=1, le=1_000_000)] = 1
    taken_at_ms: EpochMs
    device: Device
    preset_name: Name = ""
    status: Literal["complete", "cancelled", "aborted"] = "complete"
    notes: Notes = ""
    location_label: Name | None = None
    warnings: Annotated[list[Text], Field(max_length=MAX_WARNINGS)] = []
    site: Site | None = None
    geo: Geo | None = None
    wifi: Wifi | None = None
    steps: Annotated[list[Step], Field(max_length=MAX_STEPS)] = []
    neighbors: Annotated[list[Neighbor], Field(max_length=MAX_NEIGHBORS)] = []
    dhcp: Dhcp | None = None
    link: Link | None = None
    poe: Poe | None = None
    properties: Properties = Properties()
    tool_results: Annotated[list[ToolResult], Field(max_length=MAX_TOOL_RESULTS)] = []

    @model_validator(mode="before")
    @classmethod
    def _refuse_transcripts_and_depth(cls, raw: Any) -> Any:
        _scan(raw)
        return raw


def _too_deep(body: bytes) -> bool:
    """True when JSON nesting exceeds MAX_DEPTH. A linear scan that ignores brackets in strings,
    run before json.loads so a deeply nested body cannot recurse in the parser."""
    depth = 0
    in_string = False
    escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_DEPTH:
                return True
        elif byte in (0x5D, 0x7D):
            depth -= 1
    return False


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a valid JSON number")


def parse_report(body: bytes | str) -> Report:
    """Validate a raw upload body. Raises ReportError; nothing is returned for a bad body."""
    data = body.encode("utf-8") if isinstance(body, str) else body
    if len(data) > MAX_REPORT_BYTES:
        raise ReportError("report is too large", 413)
    if _too_deep(data):
        raise ReportError("report is nested too deeply", 400)
    try:
        raw = json.loads(data, parse_constant=_no_constant)
    except (ValueError, RecursionError) as err:
        raise ReportError("body is not valid JSON") from err
    if not isinstance(raw, dict):
        raise ReportError("body must be a JSON object")
    try:
        return Report.model_validate(raw)
    except ValidationError as err:
        # Where and why, never the rejected value.
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '(report)'}: {e['msg']}"
                             for e in err.errors()[:5])
        raise ReportError(f"schema validation failed: {problems}") from err
