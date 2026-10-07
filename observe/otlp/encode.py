"""The OTLP request encoder and response reader the exporter uses.

The encoder is the reverse of `wire.decode`: it takes a request in the OTLP JSON shape (the same
shape the decoder returns) and writes protobuf from the same field table, so the two cannot drift
apart. The response reader pulls the partial success counts out of a collector's reply in either
encoding. Nothing here talks to the network.
"""

from __future__ import annotations

import json
import struct
from typing import Any

from .wire import FIXED64, LEN, SCHEMAS, VARINT, WireError, _enc_varint

_WIRE_OF = {"msg": LEN, "str": LEN, "bytes": LEN, "u64": FIXED64, "i64": FIXED64, "f64": FIXED64,
            "vint": VARINT, "vint64": VARINT, "bool": VARINT}


def _field(number: int, kind: str, value: Any, sub: str | None) -> bytes:
    tag = _enc_varint(number << 3 | _WIRE_OF[kind])
    if kind == "msg":
        body = _message(sub or "Skipped", value)
        return tag + _enc_varint(len(body)) + body
    if kind == "str":
        data = str(value).encode("utf-8")
        return tag + _enc_varint(len(data)) + data
    if kind == "bytes":
        data = bytes.fromhex(value)
        return tag + _enc_varint(len(data)) + data
    if kind == "u64":
        return tag + int(value).to_bytes(8, "little")
    if kind == "i64":
        return tag + int(value).to_bytes(8, "little", signed=True)
    if kind == "f64":
        return tag + struct.pack("<d", float(value))
    if kind == "bool":
        return tag + _enc_varint(1 if value else 0)
    if kind == "vint64":
        return tag + _enc_varint(int(value) & (1 << 64) - 1)
    return tag + _enc_varint(int(value))


def _message(name: str, obj: dict[str, Any]) -> bytes:
    out = bytearray()
    # Field numbers in ascending order, as a protobuf writer does.
    for number, (field, kind, sub) in sorted(SCHEMAS[name].items()):
        if field.endswith("[]"):
            for item in obj.get(field[:-2], ()):
                out += _field(number, kind, item, sub)
        elif field in obj and obj[field] is not None:
            out += _field(number, kind, obj[field], sub)
    return bytes(out)


def to_protobuf(request: dict[str, Any], message: str) -> bytes:
    """A MetricsRequest or LogsRequest in the OTLP JSON shape as protobuf bytes."""
    if message not in ("MetricsRequest", "LogsRequest"):
        raise ValueError(message)
    return _message(message, request)


def to_json(request: dict[str, Any]) -> bytes:
    """The same request as OTLP JSON: the shape already follows the mapping, with 64-bit integers
    as decimal strings."""
    return json.dumps(request, separators=(",", ":"), allow_nan=False).encode("utf-8")


# ---- responses ------------------------------------------------------------------------------

def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if pos >= len(buf):
            raise WireError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
    raise WireError("varint longer than 10 bytes")


def _partial_protobuf(body: bytes) -> tuple[int, str]:
    """Field 1 of the response is the partial success message: rejected count (1) and text (2)."""
    pos = 0
    while pos < len(body):
        tag, pos = _read_varint(body, pos)
        number, wire = tag >> 3, tag & 7
        if wire == VARINT:
            _, pos = _read_varint(body, pos)
        elif wire == FIXED64:
            pos += 8
        elif wire == LEN:
            size, pos = _read_varint(body, pos)
            chunk = body[pos:pos + size]
            if len(chunk) != size:
                raise WireError("truncated message")
            pos += size
            if number == 1:
                return _partial_fields(chunk)
        elif wire == 5:
            pos += 4
        else:
            raise WireError("unsupported wire type")
    return 0, ""


def _partial_fields(chunk: bytes) -> tuple[int, str]:
    rejected, text, pos = 0, "", 0
    while pos < len(chunk):
        tag, pos = _read_varint(chunk, pos)
        number, wire = tag >> 3, tag & 7
        if wire == VARINT:
            value, pos = _read_varint(chunk, pos)
            if number == 1:
                rejected = value
        elif wire == LEN:
            size, pos = _read_varint(chunk, pos)
            data = chunk[pos:pos + size]
            if len(data) != size:
                raise WireError("truncated message")
            pos += size
            if number == 2:
                text = data.decode("utf-8", "replace")
        elif wire == FIXED64:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise WireError("unsupported wire type")
    return rejected, text


def read_response(body: bytes, content_type: str) -> tuple[int, str]:
    """The (rejected count, error message) of a 200 response, or (0, "") when it has none or is
    not readable. A reply that cannot be read does not undo a delivery the collector accepted."""
    if not body:
        return 0, ""
    try:
        if "json" in content_type.lower():
            doc = json.loads(body)
            part = doc.get("partialSuccess") or {}
            rejected = part.get("rejectedDataPoints", part.get("rejectedLogRecords", 0))
            return max(0, int(rejected or 0)), str(part.get("errorMessage", ""))[:200]
        rejected, text = _partial_protobuf(body)
        return rejected, text[:200]
    except (ValueError, WireError, AttributeError, TypeError):
        return 0, ""
