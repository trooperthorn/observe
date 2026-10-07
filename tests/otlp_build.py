"""Hand-built OTLP requests for the tests.

The JSON builders write the OTLP JSON mapping (lowerCamelCase, 64-bit integers as strings). `proto`
turns the same structure into protobuf bytes with its own field table, written from the
opentelemetry-proto definitions and deliberately not shared with observe/otlp/wire.py, so a wrong
field number in the decoder is not mirrored here. `from_batch` turns a hostwatch-shaped batch (the
fixtures in tests/fixtures) into the OTLP requests a producer would send for it.
"""

from __future__ import annotations

import gzip
import json
import struct
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "hostwatch"


def fixture(name: str) -> dict[str, Any]:
    """A hostwatch-shaped batch from tests/fixtures/hostwatch."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def kv(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        av: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int):
        av = {"intValue": str(value)}
    elif isinstance(value, float):
        av = {"doubleValue": value}
    elif isinstance(value, dict) and "arrayValue" in value:
        av = value
    else:
        av = {"stringValue": str(value)}
    return {"key": key, "value": av}


def attrs(**values: Any) -> list[dict[str, Any]]:
    return [kv(k.replace("__", "."), v) for k, v in values.items()]


def resource(host: str | None, **extra: Any) -> dict[str, Any]:
    res = [] if host is None else [kv("host.name", host)]
    res += [kv(k.replace("__", "."), v) for k, v in extra.items()]
    return {"attributes": res}


def number(value: float | None, ts: float, labels: dict[str, str] | None = None,
           as_int: bool = False, flags: int = 0) -> dict[str, Any]:
    dp: dict[str, Any] = {"timeUnixNano": str(int(ts * 1_000_000_000)),
                          "attributes": [kv(k, v) for k, v in (labels or {}).items()]}
    if value is not None:
        if as_int:
            dp["asInt"] = str(int(value))
        else:
            dp["asDouble"] = value
    if flags:
        dp["flags"] = flags
    return dp


def gauge(name: str, points: list[dict[str, Any]], unit: str = "") -> dict[str, Any]:
    return {"name": name, "unit": unit, "gauge": {"dataPoints": points}}


def total(name: str, points: list[dict[str, Any]], unit: str = "") -> dict[str, Any]:
    return {"name": name, "unit": unit, "sum": {"dataPoints": points, "aggregationTemporality": 2,
                                                "isMonotonic": True}}


def histogram(name: str, count: int, total_: float, ts: float,
              labels: dict[str, str] | None = None) -> dict[str, Any]:
    return {"name": name, "histogram": {"aggregationTemporality": 1, "dataPoints": [{
        "timeUnixNano": str(int(ts * 1e9)), "count": str(count), "sum": total_,
        "bucketCounts": ["1", "2"], "explicitBounds": [1.0],
        "attributes": [kv(k, v) for k, v in (labels or {}).items()]}]}}


def scope_metrics(scope: str, metrics: list[dict[str, Any]]) -> dict[str, Any]:
    return {"scope": {"name": scope, "version": "1"}, "metrics": metrics}


def metrics_request(host: str | None, scopes: dict[str, list[dict[str, Any]]],
                    **res: Any) -> dict[str, Any]:
    return {"resourceMetrics": [{"resource": resource(host, **res), "scopeMetrics": [
        scope_metrics(name, metrics) for name, metrics in scopes.items()]}]}


def log_record(kind: str, ts: float, body: str | None = None, severity: str | None = None,
               number_: int | None = None, **attributes: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {"timeUnixNano": str(int(ts * 1e9)),
                           "attributes": [kv("event.name", kind)]
                           + [kv(k.replace("__", "."), v) for k, v in attributes.items()]}
    if body is not None:
        rec["body"] = {"stringValue": body}
    if severity is not None:
        rec["severityText"] = severity
    if number_ is not None:
        rec["severityNumber"] = number_
    return rec


def logs_request(host: str | None, records: list[dict[str, Any]], scope: str = "journal",
                 **res: Any) -> dict[str, Any]:
    return {"resourceLogs": [{"resource": resource(host, **res),
                              "scopeLogs": [{"scope": {"name": scope}, "logRecords": records}]}]}


def from_batch(batch: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The metrics request, and the logs request when there are events, for a hostwatch batch."""
    res = {"os__type": batch["platform"], "service__version": batch["agent_version"],
           "observe__agent__sent_at": batch["sent_at"]}
    by_scope: dict[str, list[dict[str, Any]]] = {}
    for s in batch["samples"]:
        by_scope.setdefault(s["source"], []).append(
            gauge(s["metric"], [number(s["value"], s["ts"], s["labels"],
                                       flags=1 if s["value"] is None else 0)], s.get("unit", "")))
    status = []
    for st in batch["sources"]:
        reason = {"observe.source.reason": st["reason"]} if st.get("reason") else {}
        status.append(gauge("observe.source.available", [number(
            1.0 if st["available"] else 0.0, batch["sent_at"],
            {"observe.source": st["source"], **reason})]))
        status.append(gauge("observe.source.present", [number(
            1.0 if st.get("present", True) else 0.0, batch["sent_at"],
            {"observe.source": st["source"]})]))
    by_scope.setdefault("observe", []).extend(status)
    metrics = metrics_request(batch["host"], by_scope, **res)
    events = batch.get("events") or []
    if not events:
        return metrics, None
    records = []
    for e in events:
        extra: dict[str, Any] = {"observe__source": e["source"],
                                 "observe__dedup_key": e["dedup_key"]}
        if e.get("boot_id"):
            extra["observe__boot_id"] = e["boot_id"]
        extra.update(e.get("detail", {}))
        records.append(log_record(e["kind"], e["ts"], e["title"], e["severity"], **extra))
    return metrics, logs_request(batch["host"], records, **res)


def post_batch(client: Any, batch: dict[str, Any], key: str | None, **kw: Any) -> Any:
    """Send a hostwatch-shaped batch as OTLP: the logs request first when there are events, then
    the metrics request, whose response is returned. batch_id becomes the Idempotency-Key."""
    metrics, logs = from_batch(batch)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if batch.get("batch_id"):
        headers["Idempotency-Key"] = batch["batch_id"]
    if logs is not None:
        client.post("/v1/logs", json=logs, headers=headers, **kw)
    return client.post("/v1/metrics", json=metrics, headers=headers, **kw)


def gz(data: bytes) -> bytes:
    return gzip.compress(data)


# ---- protobuf, from the opentelemetry-proto definitions --------------------------------------

_ANY = {"stringValue": (1, "str"), "boolValue": (2, "bool"), "intValue": (3, "int64"),
        "doubleValue": (4, "double"), "arrayValue": (5, "msg:Array"),
        "kvlistValue": (6, "msg:KvList"), "bytesValue": (7, "bytes")}
_SPEC: dict[str, dict[str, tuple[int, str]]] = {
    "Request": {"resourceMetrics": (1, "rep:ResourceMetrics"), "resourceLogs": (1, "rep:ResourceLogs")},
    "ResourceMetrics": {"resource": (1, "msg:Resource"), "scopeMetrics": (2, "rep:ScopeMetrics")},
    "ResourceLogs": {"resource": (1, "msg:Resource"), "scopeLogs": (2, "rep:ScopeLogs")},
    "Resource": {"attributes": (1, "rep:KeyValue")},
    "ScopeMetrics": {"scope": (1, "msg:Scope"), "metrics": (2, "rep:Metric")},
    "ScopeLogs": {"scope": (1, "msg:Scope"), "logRecords": (2, "rep:LogRecord")},
    "Scope": {"name": (1, "str"), "version": (2, "str"), "attributes": (3, "rep:KeyValue")},
    "Metric": {"name": (1, "str"), "description": (2, "str"), "unit": (3, "str"),
               "gauge": (5, "msg:Gauge"), "sum": (7, "msg:Sum"), "histogram": (9, "msg:Histogram"),
               "exponentialHistogram": (10, "msg:Gauge"), "summary": (11, "msg:Gauge")},
    "Gauge": {"dataPoints": (1, "rep:Number")},
    "Sum": {"dataPoints": (1, "rep:Number"), "aggregationTemporality": (2, "varint"),
            "isMonotonic": (3, "bool")},
    "Histogram": {"dataPoints": (1, "rep:HistPoint"), "aggregationTemporality": (2, "varint")},
    "Number": {"startTimeUnixNano": (2, "fixed64"), "timeUnixNano": (3, "fixed64"),
               "asDouble": (4, "double"), "asInt": (6, "sfixed64"),
               "attributes": (7, "rep:KeyValue"), "flags": (8, "varint")},
    "HistPoint": {"startTimeUnixNano": (2, "fixed64"), "timeUnixNano": (3, "fixed64"),
                  "count": (4, "fixed64"), "sum": (5, "double"), "bucketCounts": (6, "packed64"),
                  "explicitBounds": (7, "packeddouble"), "attributes": (9, "rep:KeyValue"),
                  "flags": (10, "varint")},
    "KeyValue": {"key": (1, "str"), "value": (2, "msg:Any")},
    "Any": _ANY,
    "Array": {"values": (1, "rep:Any")},
    "KvList": {"values": (1, "rep:KeyValue")},
    "LogRecord": {"timeUnixNano": (1, "fixed64"), "severityNumber": (2, "varint"),
                  "severityText": (3, "str"), "body": (5, "msg:Any"),
                  "attributes": (6, "rep:KeyValue"), "observedTimeUnixNano": (11, "fixed64")},
}


def varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def tag(number: int, wire: int) -> bytes:
    return varint(number << 3 | wire)


def ld(number: int, payload: bytes) -> bytes:
    return tag(number, 2) + varint(len(payload)) + payload


def _field(number: int, kind: str, value: Any) -> bytes:
    base, _, sub = kind.partition(":")
    if base == "str":
        return ld(number, value.encode("utf-8"))
    if base == "bytes":
        return ld(number, bytes.fromhex(value))
    if base == "bool":
        return tag(number, 0) + varint(1 if value else 0)
    if base == "varint":
        return tag(number, 0) + varint(int(value))
    if base == "int64":
        return tag(number, 0) + varint(int(value))
    if base == "double":
        return tag(number, 1) + struct.pack("<d", value)
    if base == "fixed64":
        return tag(number, 1) + int(value).to_bytes(8, "little")
    if base == "sfixed64":
        return tag(number, 1) + int(value).to_bytes(8, "little", signed=True)
    if base == "packed64":
        return ld(number, b"".join(int(v).to_bytes(8, "little") for v in value))
    if base == "packeddouble":
        return ld(number, b"".join(struct.pack("<d", v) for v in value))
    if base == "msg":
        return ld(number, message(sub, value))
    raise AssertionError(kind)


def message(name: str, obj: dict[str, Any]) -> bytes:
    out = bytearray()
    for key, value in obj.items():
        number, kind = _SPEC[name][key]
        if kind.startswith("rep:"):
            for item in value:
                out += _field(number, "msg:" + kind[4:], item)
        else:
            out += _field(number, kind, value)
    return bytes(out)


def proto(request: dict[str, Any]) -> bytes:
    """A metrics or logs request in the OTLP JSON shape as protobuf bytes."""
    return message("Request", request)


def body_of(request: dict[str, Any]) -> bytes:
    return json.dumps(request, separators=(",", ":")).encode("utf-8")
