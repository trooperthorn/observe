"""A minimal protobuf decoder for the OTLP messages Observe accepts, with no dependency.

The result has the shape of the OTLP JSON mapping (lowerCamelCase names, 64-bit integers as
decimal strings, bytes as hex), so one normalizer reads a protobuf request and a JSON request
alike. Only the messages and fields listed in SCHEMAS are decoded; any other field is skipped by
its wire type. The decoder enforces its limits while it works and never builds an object larger
than the caps: nesting depth, a total field budget, varints of at most 10 bytes, and every
length-delimited field must fit in the bytes that remain. Anything malformed raises WireError.
"""

from __future__ import annotations

import struct
from typing import Any

MAX_DEPTH = 16
MAX_FIELDS = 400_000  # decoded fields in one request, a bound on memory for a hostile body

VARINT, FIXED64, LEN, SGROUP, EGROUP, FIXED32 = 0, 1, 2, 3, 4, 5


class WireError(ValueError):
    """The body is not a valid protobuf message of the expected type."""


# kind: msg (sub message), str, bytes (as hex), u64 (fixed64 as a decimal string), i64 (sfixed64
# as a decimal string), f64 (double), vint (varint as an int), vint64 (signed varint as a decimal
# string), bool. A name ending in [] is repeated.
SCHEMAS: dict[str, dict[int, tuple[str, str, str | None]]] = {
    "MetricsRequest": {1: ("resourceMetrics[]", "msg", "ResourceMetrics")},
    "ResourceMetrics": {1: ("resource", "msg", "Resource"),
                        2: ("scopeMetrics[]", "msg", "ScopeMetrics")},
    "Resource": {1: ("attributes[]", "msg", "KeyValue")},
    "ScopeMetrics": {1: ("scope", "msg", "Scope"), 2: ("metrics[]", "msg", "Metric")},
    "Scope": {1: ("name", "str", None), 2: ("version", "str", None),
              3: ("attributes[]", "msg", "KeyValue")},
    "Metric": {1: ("name", "str", None), 2: ("description", "str", None), 3: ("unit", "str", None),
               5: ("gauge", "msg", "Gauge"), 7: ("sum", "msg", "Sum"),
               9: ("histogram", "msg", "Histogram"),
               10: ("exponentialHistogram", "msg", "Skipped"), 11: ("summary", "msg", "Skipped")},
    "Skipped": {},
    "Gauge": {1: ("dataPoints[]", "msg", "NumberPoint")},
    "Sum": {1: ("dataPoints[]", "msg", "NumberPoint"), 2: ("aggregationTemporality", "vint", None),
            3: ("isMonotonic", "bool", None)},
    "Histogram": {1: ("dataPoints[]", "msg", "HistogramPoint"),
                  2: ("aggregationTemporality", "vint", None)},
    "NumberPoint": {2: ("startTimeUnixNano", "u64", None), 3: ("timeUnixNano", "u64", None),
                    4: ("asDouble", "f64", None), 6: ("asInt", "i64", None),
                    7: ("attributes[]", "msg", "KeyValue"), 8: ("flags", "vint", None)},
    "HistogramPoint": {2: ("startTimeUnixNano", "u64", None), 3: ("timeUnixNano", "u64", None),
                       4: ("count", "u64", None), 5: ("sum", "f64", None),
                       9: ("attributes[]", "msg", "KeyValue"), 10: ("flags", "vint", None)},
    "KeyValue": {1: ("key", "str", None), 2: ("value", "msg", "AnyValue")},
    "AnyValue": {1: ("stringValue", "str", None), 2: ("boolValue", "bool", None),
                 3: ("intValue", "vint64", None), 4: ("doubleValue", "f64", None),
                 5: ("arrayValue", "msg", "ArrayValue"), 6: ("kvlistValue", "msg", "KvList"),
                 7: ("bytesValue", "bytes", None)},
    "ArrayValue": {1: ("values[]", "msg", "AnyValue")},
    "KvList": {1: ("values[]", "msg", "KeyValue")},
    "LogsRequest": {1: ("resourceLogs[]", "msg", "ResourceLogs")},
    "ResourceLogs": {1: ("resource", "msg", "Resource"), 2: ("scopeLogs[]", "msg", "ScopeLogs")},
    "ScopeLogs": {1: ("scope", "msg", "Scope"), 2: ("logRecords[]", "msg", "LogRecord")},
    "LogRecord": {1: ("timeUnixNano", "u64", None), 2: ("severityNumber", "vint", None),
                  3: ("severityText", "str", None), 5: ("body", "msg", "AnyValue"),
                  6: ("attributes[]", "msg", "KeyValue"),
                  11: ("observedTimeUnixNano", "u64", None)},
}
# The wire type each kind of field arrives as. A mismatch (a corrupt or hostile body) is an
# error rather than a guess.
_WIRE = {"msg": LEN, "str": LEN, "bytes": LEN, "u64": FIXED64, "i64": FIXED64, "f64": FIXED64,
         "vint": VARINT, "vint64": VARINT, "bool": VARINT}


class _Budget:
    __slots__ = ("left",)

    def __init__(self) -> None:
        self.left = MAX_FIELDS


def _varint(buf: bytes, pos: int, end: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if pos >= end:
            raise WireError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
    raise WireError("varint longer than 10 bytes")


def _decode(buf: bytes, start: int, end: int, name: str, depth: int,
            budget: _Budget) -> dict[str, Any]:
    if depth > MAX_DEPTH:
        raise WireError("messages are nested too deeply")
    schema = SCHEMAS[name]
    out: dict[str, Any] = {}
    pos = start
    while pos < end:
        tag, pos = _varint(buf, pos, end)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise WireError("field number 0")
        budget.left -= 1
        if budget.left < 0:
            raise WireError("too many fields")
        raw: Any
        if wire == VARINT:
            raw, pos = _varint(buf, pos, end)
        elif wire == FIXED64:
            if pos + 8 > end:
                raise WireError("truncated fixed64")
            raw = buf[pos:pos + 8]
            pos += 8
        elif wire == FIXED32:
            if pos + 4 > end:
                raise WireError("truncated fixed32")
            pos += 4
            raw = None
        elif wire == LEN:
            size, pos = _varint(buf, pos, end)
            if size > end - pos:
                raise WireError("length runs past the end of the message")
            raw = (pos, pos + size)
            pos += size
        else:
            raise WireError("unsupported wire type")  # groups are not used by OTLP
        spec = schema.get(number)
        if spec is None:
            continue  # an unknown field is skipped by its wire type
        field, kind, sub = spec
        if _WIRE[kind] != wire:
            raise WireError(f"field {field} has the wrong wire type")
        value: Any
        if kind == "msg":
            value = _decode(buf, raw[0], raw[1], sub or "Skipped", depth + 1, budget)
        elif kind == "str":
            try:
                value = buf[raw[0]:raw[1]].decode("utf-8")
            except UnicodeDecodeError as err:
                raise WireError("a string is not valid UTF-8") from err
        elif kind == "bytes":
            value = buf[raw[0]:raw[1]].hex()
        elif kind == "u64":
            value = str(int.from_bytes(raw, "little"))
        elif kind == "i64":
            value = str(int.from_bytes(raw, "little", signed=True))
        elif kind == "f64":
            value = struct.unpack("<d", raw)[0]
        elif kind == "bool":
            value = bool(raw)
        elif kind == "vint64":
            value = str(raw - (1 << 64) if raw >= 1 << 63 else raw)
        else:  # vint
            value = raw
        if field.endswith("[]"):
            out.setdefault(field[:-2], []).append(value)
        else:
            out[field] = value  # a repeated singular field: the last one wins, as in protobuf
    return out


def decode(body: bytes, message: str) -> dict[str, Any]:
    """Decode a MetricsRequest or LogsRequest into the OTLP JSON shape, or raise WireError."""
    if message not in ("MetricsRequest", "LogsRequest"):
        raise ValueError(message)
    return _decode(body, 0, len(body), message, 1, _Budget())


# ---- responses ------------------------------------------------------------------------------

def _enc_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def partial_success_body(rejected: int, message: str) -> bytes:
    """ExportMetricsServiceResponse or ExportLogsServiceResponse as protobuf (both carry the
    same two fields, rejected count and message, in field 1). Empty when nothing was rejected."""
    if not rejected and not message:
        return b""
    text = message.encode("utf-8")
    inner = b""
    if rejected:
        inner += b"\x08" + _enc_varint(rejected)
    if text:
        inner += b"\x12" + _enc_varint(len(text)) + text
    return b"\x0a" + _enc_varint(len(inner)) + inner
