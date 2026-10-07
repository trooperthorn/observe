"""From a decoded OTLP request to what Observe stores (docs/DATA-API-DESIGN.md sections 6.4, 6.5).

The input is the OTLP JSON shape, whether it arrived as JSON or was decoded from protobuf
(observe/otlp/wire.py). It is untrusted and may have any structure, so every access checks its
type; an item that is not well formed is rejected on its own and counted, never raised. The
output of a host request is a `Batch` (the internal normalized form, observe/ingest/schema.py)
that Store.ingest_batch writes in one transaction, so a producer's data lands in the same series
as the data Observe pulls itself.

What a host producer sends:
- Resource attribute `host.name` names the host and must equal the host the key is bound to
  (ignoring case and surrounding space). A resource for another host is rejected with its count.
  `os.type` names the platform, `service.version` the agent version, and `observe.agent.sent_at`
  (unix seconds) the time the batch was sent.
- The instrumentation scope name is the source of a point (the collector), the metric name is
  the metric and the point attributes are its labels. Gauge and sum points are stored with the
  value they carry, a histogram as `<name>.count` and `<name>.sum`, and a point with the
  no-recorded-value flag as an unavailable sample. Exponential histograms and summaries are
  rejected.
- Gauges `observe.source.available` and `observe.source.present` with the point attribute
  `observe.source` (and `observe.source.reason` on the first) say whether a source works.
- A log record is an event. `event.name` is its kind, the body its title, `observe.source` its
  source, `observe.dedup_key` its dedup key (else a hash of the record), `observe.boot_id` its
  boot id and severityNumber or severityText its severity. The other attributes are its detail.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..ingest.schema import (MAX_DETAIL_BYTES, MAX_DETAIL_KEYS, MAX_EVENTS, MAX_LABELS, MAX_NAME,
                             MAX_SAMPLES, MAX_TEXT, Batch, Event, Sample, SourceStatus,
                             normalize_severity)

MAX_RESOURCE_ATTRS = 64
MAX_POINT_ATTRS = 32
MAX_ATTR_KEY = 128
MAX_ATTR_VALUE = 1024
MAX_UNIT = 32
MAX_NS = (1 << 63) - 1
_METRIC = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")
_UNIT = re.compile(r"^[A-Za-z0-9%/.{}_\[\]^*()' -]{0,32}$")
SOURCE_AVAILABLE = "observe.source.available"
SOURCE_PRESENT = "observe.source.present"
FLAG_NO_VALUE = 1
_COMPLEX = object()  # an array, map or bytes attribute value


def host_key(name: str) -> str:
    """The form of a host name two names are compared in."""
    return name.strip().casefold()


@dataclass
class Rejects:
    """What was refused, counted per reason so the message stays short."""

    count: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def add(self, reason: str, n: int = 1) -> None:
        self.count += n
        self.reasons[reason] = self.reasons.get(reason, 0) + n

    def message(self) -> str:
        text = "; ".join(f"{reason} ({n})" for reason, n in list(self.reasons.items())[:8])
        return text[:512]


@dataclass
class Normalized:
    batch: Batch | None
    accepted: int
    rejects: Rejects


@dataclass(frozen=True)
class FieldPoint:
    """A point of a field key's request: the resource it belongs to and the point itself."""

    kind: str
    name: str
    attrs: dict[str, Any]
    scope: str
    metric: str
    unit: str
    labels: str
    ts_ms: int
    value: float | None


@dataclass(frozen=True)
class LogRecord:
    """A log record as a plugin handler sees it."""

    event: str
    time_ms: int
    body: str | None
    attributes: dict[str, Any]


# ---- reading untrusted values ---------------------------------------------------------------

def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in _list(value) if isinstance(v, dict)]


def _scalar(av: Any) -> Any:
    """A python scalar from an AnyValue, or _COMPLEX for an array, map or bytes, or None."""
    if not isinstance(av, dict):
        return None
    if "stringValue" in av:
        v = av["stringValue"]
        return v if isinstance(v, str) else None
    if "boolValue" in av:
        v = av["boolValue"]
        return v if isinstance(v, bool) else None
    if "intValue" in av:
        v = av["intValue"]
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str) and re.fullmatch(r"-?[0-9]{1,19}", v):
            return int(v)
        return None
    if "doubleValue" in av:
        v = av["doubleValue"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return float(v) if math.isfinite(v) else None
    if "arrayValue" in av or "kvlistValue" in av or "bytesValue" in av:
        return _COMPLEX
    return None


def _attrs(raw: Any, limit: int, what: str) -> dict[str, Any] | str:
    """The attributes of a resource, point or record as a map, or the reason they were refused."""
    items = _dicts(raw)
    if len(items) > limit:
        return f"more than {limit} {what} attributes"
    out: dict[str, Any] = {}
    for kv in items:
        key = kv.get("key")
        if not isinstance(key, str) or not key or len(key) > MAX_ATTR_KEY:
            return f"{what} attribute key is empty or longer than {MAX_ATTR_KEY}"
        value = _scalar(kv.get("value"))
        if isinstance(value, str) and len(value) > MAX_ATTR_VALUE:
            return f"{what} attribute value is longer than {MAX_ATTR_VALUE}"
        out[key] = value
    return out


def _label(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else repr(value)


def _ns(raw: Any) -> int | None:
    """A time in nanoseconds from an OTLP JSON value (a decimal string or a number)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        n = raw
    elif isinstance(raw, str) and re.fullmatch(r"[0-9]{1,19}", raw):
        n = int(raw)
    else:
        return None
    return n if 0 <= n <= MAX_NS else None


def _point_time(raw: dict[str, Any], now: float) -> float | None:
    """The time of a point in seconds; the receive time when the point has none."""
    given = raw.get("timeUnixNano")
    if given in (None, "", "0", 0):
        return now
    ns = _ns(given)
    return None if ns is None else ns / 1e9


def _number(raw: dict[str, Any]) -> tuple[float | None, str]:
    """(value, problem). A problem is empty when the point has a usable value or says it has none."""
    flags = raw.get("flags")
    if isinstance(flags, int) and not isinstance(flags, bool) and flags & FLAG_NO_VALUE:
        return None, ""
    if "asDouble" in raw:
        v = raw["asDouble"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None, "a value is not a number"
        return (float(v), "") if math.isfinite(v) else (None, "a value is not finite")
    if "asInt" in raw:
        v = raw["asInt"]
        if isinstance(v, bool):
            return None, "a value is not a number"
        if isinstance(v, int):
            return float(v), ""
        if isinstance(v, str) and re.fullmatch(r"-?[0-9]{1,19}", v):
            return float(int(v)), ""
        return None, "a value is not a number"
    return None, "a point has no value"


def _resource(rm: dict[str, Any]) -> dict[str, Any] | str:
    res = rm.get("resource")
    if res is not None and not isinstance(res, dict):
        return "resource is not an object"
    return _attrs((res or {}).get("attributes"), MAX_RESOURCE_ATTRS, "resource")


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value < 253_402_300_800 else None


def _count_points(rm: dict[str, Any]) -> int:
    n = 0
    for sm in _dicts(rm.get("scopeMetrics")):
        for metric in _dicts(sm.get("metrics")):
            for kind in ("gauge", "sum", "histogram"):
                body = metric.get(kind)
                if isinstance(body, dict):
                    n += len(_list(body.get("dataPoints")))
    return n


def _count_records(rl: dict[str, Any]) -> int:
    return sum(len(_list(sl.get("logRecords"))) for sl in _dicts(rl.get("scopeLogs")))


def _scope_name(sm: dict[str, Any]) -> str | None:
    scope = sm.get("scope")
    if scope is None:
        return "otlp"
    name = scope.get("name") if isinstance(scope, dict) else None
    if name in (None, ""):
        return "otlp"
    return name if isinstance(name, str) and len(name) <= MAX_NAME else None


# ---- metrics --------------------------------------------------------------------------------

def _walk_metrics(req: Any, rejects: Rejects, now: float, want: Any):
    """Yield (resource attributes, scope name, metric name, unit, kind, point, labels, ts) for
    every point of the resources `want` accepts, counting what it refuses."""
    if not isinstance(req, dict):
        rejects.add("request is not an object")
        return
    seen = 0
    for rm in _dicts(req.get("resourceMetrics")):
        res = _resource(rm)
        verdict = res if isinstance(res, str) else want(res)
        if verdict:
            rejects.add(verdict, max(_count_points(rm), 1))
            continue
        for sm in _dicts(rm.get("scopeMetrics")):
            scope = _scope_name(sm)
            for metric in _dicts(sm.get("metrics")):
                kinds = [k for k in ("gauge", "sum", "histogram", "exponentialHistogram",
                                     "summary") if isinstance(metric.get(k), dict)]
                n_points = sum(len(_list(metric[k].get("dataPoints"))) for k in kinds)
                name, unit = metric.get("name"), metric.get("unit", "")
                if scope is None:
                    rejects.add("scope name is longer than 128", max(n_points, 1))
                elif not isinstance(name, str) or not _METRIC.match(name):
                    rejects.add("metric name is not valid", max(n_points, 1))
                elif not isinstance(unit, str) or not _UNIT.match(unit):
                    rejects.add("metric unit is not valid", max(n_points, 1))
                elif "exponentialHistogram" in kinds or "summary" in kinds:
                    rejects.add("exponential histograms and summaries are not accepted",
                                max(n_points, 1))
                elif not kinds:
                    rejects.add("metric has no data")
                else:
                    kind = kinds[0]
                    for dp in _list(metric[kind].get("dataPoints")):
                        seen += 1
                        got = _point(dp, now, seen)
                        if isinstance(got, str):
                            rejects.add(got)
                        else:
                            yield res, scope, name, unit, kind, dp, got[0], got[1]


def _point(dp: Any, now: float, seen: int) -> str | tuple[dict[str, Any], float]:
    """(attributes, time) of a data point, or the reason it was refused."""
    if seen > MAX_SAMPLES:
        return f"more than {MAX_SAMPLES} points in one request"
    if not isinstance(dp, dict):
        return "a data point is not an object"
    attrs = _attrs(dp.get("attributes"), MAX_POINT_ATTRS, "point")
    if isinstance(attrs, str):
        return attrs
    if any(v is _COMPLEX or v is None for v in attrs.values()):
        return "a point attribute is not a string, number or boolean"
    ts = _point_time(dp, now)
    if ts is None:
        return "a point time is not valid"
    return attrs, ts


def normalize_metrics(req: Any, bound_host: str, now: float) -> Normalized:
    """A host key's metrics request as a Batch, with the points that were refused counted."""
    rejects = Rejects()
    wanted = host_key(bound_host)

    def want(res: dict[str, Any]) -> str:
        name = res.get("host.name")
        if not isinstance(name, str) or host_key(name) != wanted:
            return "resource host.name is not the host the key is bound to"
        return ""

    samples: list[Sample] = []
    status: dict[str, dict[str, Any]] = {}
    platform, version, sent = "", "", None
    for res, scope, name, unit, kind, dp, attrs, ts in _walk_metrics(req, rejects, now, want):
        if not platform and isinstance(res.get("os.type"), str):
            platform = res["os.type"][:MAX_NAME]
        if not version:
            for key in ("observe.agent.version", "service.version"):
                if isinstance(res.get(key), str) and res[key]:
                    version = res[key][:MAX_NAME]
                    break
        if sent is None:
            sent = _seconds(res.get("observe.agent.sent_at"))
        value, problem = _number(dp) if kind != "histogram" else (None, "")
        if name in (SOURCE_AVAILABLE, SOURCE_PRESENT):
            source = attrs.get("observe.source")
            if not isinstance(source, str) or not source or len(source) > MAX_NAME or problem:
                rejects.add("a source status point has no valid source or value")
                continue
            entry = status.setdefault(source, {})
            entry["available" if name == SOURCE_AVAILABLE else "present"] = bool(value)
            reason = attrs.get("observe.source.reason")
            if name == SOURCE_AVAILABLE and isinstance(reason, str):
                entry["reason"] = reason[:MAX_TEXT]
            continue
        labels = {k: _label(v) for k, v in attrs.items()}
        if len(labels) > MAX_LABELS:
            rejects.add("too many point attributes")
            continue
        if kind == "histogram":
            pairs: list[tuple[str, float | None]] = []
            count = dp.get("count")
            total = dp.get("sum")
            c = _ns(count) if count is not None else None
            if c is not None:
                pairs.append((name + ".count", float(c)))
            if isinstance(total, (int, float)) and not isinstance(total, bool) \
                    and math.isfinite(total):
                pairs.append((name + ".sum", float(total)))
            if not pairs:
                rejects.add("a histogram point has no count or sum")
            built = [(n, v) for n, v in pairs if len(n) <= MAX_NAME]
        else:
            if problem:
                rejects.add(problem)
                continue
            built = [(name, value)]
        for metric_name, v in built:
            try:
                samples.append(Sample(source=scope, metric=metric_name, value=v, unit=unit,
                                      labels=labels, ts=ts))
            except ValidationError:
                rejects.add("a point does not fit the size limits")
    sources = [SourceStatus(source=s, available=bool(e.get("available", True)),
                            present=bool(e.get("present", True)), reason=e.get("reason", ""))
               for s, e in status.items()][:256]
    accepted = len(samples) + len(status)
    if not accepted:
        return Normalized(None, 0, rejects)
    batch = Batch(agent_version=version or "otlp", host=bound_host, platform=platform or "unknown",
                  sent_at=now if sent is None else sent, sources=sources, samples=samples,
                  events=[])
    return Normalized(batch, accepted, rejects)


def normalize_field_metrics(req: Any, device: str, now: float) -> tuple[list[FieldPoint], Rejects]:
    """A field key's metrics request. Every resource is the field tester named by the key's
    device label, and the `observe.field.device` attribute is set to that label whatever the
    client sent. A resource that names a switch or a port is refused and counted."""
    rejects = Rejects()

    def want(res: dict[str, Any]) -> str:
        return ""

    out: list[FieldPoint] = []
    for res, scope, name, unit, kind, dp, attrs, ts in _walk_metrics(req, rejects, now, want):
        value, problem = _number(dp) if kind != "histogram" else (None, "no histograms")
        if problem:
            rejects.add(problem)
            continue
        # A field key owns one resource, the tester named by its device label. A port is shared
        # with the SNMP and UniFi collectors that own its identity, so a field key never writes
        # port metrics: the Pockethernet plugin derives port properties from a report through
        # its own validated path (observe_pockethernet/derive.py).
        keep = {k: v for k, v in res.items() if isinstance(v, (str, int, float, bool))
                and not k.startswith("observe.field.")}
        if "observe.switch" in res or "observe.port" in res:
            rejects.add("a field key may not write port metrics")
            continue
        rkind, rname = "field_tester", device
        keep["observe.field.device"] = device
        labels = json.dumps({k: _label(v) for k, v in sorted(attrs.items())},
                            separators=(",", ":"), sort_keys=True)
        out.append(FieldPoint(rkind, rname, keep, scope, name, unit, labels,
                              int(round(ts * 1000)), value))
    return out, rejects


# ---- logs -----------------------------------------------------------------------------------

def _severity(number: Any, text: Any) -> str:
    if isinstance(text, str) and text.strip():
        return normalize_severity(text)
    if isinstance(number, int) and not isinstance(number, bool):
        if number <= 12:
            return "info"
        return "warning" if number <= 16 else "critical"
    return "info"


def _body_text(body: Any) -> str | None:
    if isinstance(body, dict) and isinstance(body.get("stringValue"), str):
        return body["stringValue"]
    return None


def _walk_logs(req: Any, rejects: Rejects, want: Any):
    """Yield (resource attributes, scope name, record) for the records of accepted resources."""
    if not isinstance(req, dict):
        rejects.add("request is not an object")
        return
    seen = 0
    for rl in _dicts(req.get("resourceLogs")):
        res = _resource(rl)
        verdict = res if isinstance(res, str) else want(res)
        if verdict:
            rejects.add(verdict, max(_count_records(rl), 1))
            continue
        for sl in _dicts(rl.get("scopeLogs")):
            scope = _scope_name(sl)
            records = _list(sl.get("logRecords"))
            if scope is None:
                rejects.add("scope name is longer than 128", max(len(records), 1))
                continue
            for rec in records:
                seen += 1
                if not isinstance(rec, dict):
                    rejects.add("a log record is not an object")
                elif seen > MAX_EVENTS:
                    rejects.add(f"more than {MAX_EVENTS} log records in one request")
                else:
                    yield res, scope, rec


def _record_time(rec: dict[str, Any], now: float) -> float | None:
    for key in ("timeUnixNano", "observedTimeUnixNano"):
        raw = rec.get(key)
        if raw in (None, "", "0", 0):
            continue
        ns = _ns(raw)
        return None if ns is None else ns / 1e9
    return now


def normalize_logs(req: Any, bound_host: str, now: float) -> Normalized:
    """A host key's logs request as a Batch whose events are the log records."""
    rejects = Rejects()
    wanted = host_key(bound_host)

    def want(res: dict[str, Any]) -> str:
        name = res.get("host.name")
        if not isinstance(name, str) or host_key(name) != wanted:
            return "resource host.name is not the host the key is bound to"
        return ""

    events: list[Event] = []
    platform, version, sent = "", "", None
    for res, scope, rec in _walk_logs(req, rejects, want):
        if not platform and isinstance(res.get("os.type"), str):
            platform = res["os.type"][:MAX_NAME]
        if not version:
            for key in ("observe.agent.version", "service.version"):
                if isinstance(res.get(key), str) and res[key]:
                    version = res[key][:MAX_NAME]
                    break
        if sent is None:
            sent = _seconds(res.get("observe.agent.sent_at"))
        attrs = _attrs(rec.get("attributes"), MAX_POINT_ATTRS, "log")
        if isinstance(attrs, str):
            rejects.add(attrs)
            continue
        kind = attrs.pop("event.name", None)
        if not isinstance(kind, str) or not kind or len(kind) > MAX_NAME:
            rejects.add("a log record has no valid event.name")
            continue
        ts = _record_time(rec, now)
        if ts is None:
            rejects.add("a log record time is not valid")
            continue
        body = _body_text(rec.get("body"))
        source = attrs.pop("observe.source", None)
        dedup = attrs.pop("observe.dedup_key", None)
        boot = attrs.pop("observe.boot_id", None)
        detail = {k: v for k, v in attrs.items() if v is not _COMPLEX and v is not None}
        if len(detail) > MAX_DETAIL_KEYS or len(json.dumps(detail)) > MAX_DETAIL_BYTES:
            rejects.add("a log record has too much detail")
            continue
        if not isinstance(dedup, str) or not dedup or len(dedup) > MAX_TEXT:
            digest = hashlib.sha256(json.dumps(
                [ts, kind, body, sorted((k, repr(v)) for k, v in detail.items())],
                sort_keys=True).encode("utf-8")).hexdigest()
            dedup = f"otlp:{digest}"
        try:
            events.append(Event(
                kind=kind, severity=_severity(rec.get("severityNumber"), rec.get("severityText")),
                source=source if isinstance(source, str) and source and len(source) <= MAX_NAME
                else scope, ts=ts, title=(body if body else kind)[:MAX_TEXT], detail=detail,
                dedup_key=dedup, boot_id=boot[:MAX_NAME] if isinstance(boot, str) and boot
                else None))
        except ValidationError:
            rejects.add("a log record does not fit the size limits")
    if not events:
        return Normalized(None, 0, rejects)
    batch = Batch(agent_version=version or "otlp", host=bound_host, platform=platform or "unknown",
                  sent_at=now if sent is None else sent, sources=[], samples=[], events=events)
    return Normalized(batch, len(events), rejects)


def field_records(req: Any, now: float) -> tuple[list[LogRecord], Rejects]:
    """The log records of a field key's request, for the plugin handler of each event name."""
    rejects = Rejects()
    out: list[LogRecord] = []
    for _res, _scope, rec in _walk_logs(req, rejects, lambda res: ""):
        attrs = _attrs(rec.get("attributes"), MAX_POINT_ATTRS, "log")
        if isinstance(attrs, str):
            rejects.add(attrs)
            continue
        event = attrs.get("event.name")
        ts = _record_time(rec, now)
        if not isinstance(event, str) or not event or ts is None:
            rejects.add("a log record has no valid event.name or time")
            continue
        out.append(LogRecord(event, int(round(ts * 1000)), _body_text(rec.get("body")),
                             {k: v for k, v in attrs.items() if v is not _COMPLEX}))
    return out, rejects
