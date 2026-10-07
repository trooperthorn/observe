"""The optional OTLP/HTTP exporter (docs/DATA-API-DESIGN.md section 6.6).

The database is the source of truth, not a memory buffer. For each signal the exporter keeps a
cursor in `export_cursor` and reads committed rows after it: raw samples in insertion order (the
number each new point takes from the `ingest_seq` counter, not its timestamp), host events and,
when asked, audit rows by id. A point that arrives late, such as a replay after an outage, takes a
new number and is exported like any other. A batch moves the cursor only once the collector has
answered, so a restart, an outage or a crash repeats at most one batch and loses nothing that raw
retention still holds.

A batch is sent as protobuf or JSON, gzip compressed, to {endpoint}/v1/metrics or /v1/logs.
Network errors and every answer that is not final are retried after an exponential backoff with
full jitter that honours Retry-After, so a collector restart (500, 502, 503, 504), throttling
(429) or a wrong or expired credential (401, 403) holds the data instead of losing it. The
answers 413 and 400 halve the batch size and send the same points again in smaller batches. Only
a defined set of answers is final (PERMANENT): 400 for a single point, 404 (a wrong endpoint,
raised as an alert, as is a redirect, which is never followed), 405, 410, 415, 422 and 501. A
final answer drops the batch and every drop is recorded: a log line, an audit row `export_gap` and, when logs are exported, the log record
`observe.export.gap`. A partial success moves the cursor on and counts the rejects.

If the cursor falls behind raw retention (the collector was away for longer than raw samples are
kept) the exporter resumes from the oldest retained sample and reports the gap in the same way.

Secrets: header values come from the configuration as secret strings and are placed only in the
request. They are never logged, never part of an error text and never returned by the status.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import random
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .. import __version__
from ..config import OtlpExportConfig
from ..httpclient import http_client
from . import encode

log = logging.getLogger("observe.export")

BACKOFF_START_S, BACKOFF_MAX_S, RETRY_AFTER_MAX_S = 1.0, 300.0, 3600.0
# Answers that end a batch. Everything else that is not a success is retried.
PERMANENT = frozenset({400, 404, 405, 410, 415, 422, 501})
SPLIT = frozenset({400, 413})  # tried again in smaller batches first
NOT_FOUND = 404
ALERT_404 = ("the collector answered 404 or a redirect: check the export endpoint; records are "
             "being dropped until it is right")
ID_CHUNK = 500
SAMPLES_INDEX = "CREATE INDEX IF NOT EXISTS samples_seq ON samples(seq)"
SEVERITY = {"debug": 5, "info": 9, "notice": 10, "warning": 13, "warn": 13, "error": 17,
            "critical": 21, "fatal": 21}


def retry_after(value: str | None, now: float) -> float | None:
    """Seconds named by a Retry-After header, as a number of seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return min(max(0.0, float(int(value))), RETRY_AFTER_MAX_S)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None
    return min(max(0.0, when - now), RETRY_AFTER_MAX_S)


def _value(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def kvs(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": str(k), "value": _value(v)} for k, v in sorted(attrs.items())]


def _span_s(rows: list[Any], col: int) -> tuple[int, int]:
    """First and last time of rows whose column `col` is a time in seconds, as milliseconds."""
    times = [float(r[col]) for r in rows]
    return (int(min(times) * 1000), int(max(times) * 1000)) if times else (0, 0)


def _loads(text: str) -> dict[str, Any]:
    try:
        doc = json.loads(text)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


@dataclass
class Outcome:
    ok: bool
    retry: bool = False
    wait: float | None = None  # Retry-After
    rejected: int = 0
    reason: str = ""
    split: bool = False  # a 400 or 413: try again in smaller batches before giving up
    status: int = 0


@dataclass
class Stats:
    sent: int = 0
    failed: int = 0
    dropped: int = 0
    rejected: int = 0
    requests: int = 0
    lag_s: float = 0.0
    consecutive_failures: int = 0
    last_success: float | None = None
    last_error: str = ""
    gaps: int = 0
    alert: str = ""


@dataclass
class _Batch:
    kind: str  # metrics or logs, which decides the path and the message type
    request: dict[str, Any]
    count: int
    cursor: tuple[int, int]  # the (ts, seq or id) the cursor moves to once the batch is delivered
    span: tuple[int, int] = (0, 0)  # first and last point time in ms, for a drop record
    gaps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Exporter:
    cfg: OtlpExportConfig
    store: Any
    client: httpx.AsyncClient | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    rng: Callable[[], float] = random.random
    stats: Stats = field(default_factory=Stats)
    _gaps: list[dict[str, Any]] = field(default_factory=list)
    _own_client: bool = False
    _ready: bool = False
    _fail_kind: str = ""  # the request path (metrics or logs) of the last failure
    _limit: int = 0  # the batch size after a 413 or 400, 0 while the configured size works

    # ---- lifecycle ---------------------------------------------------------------------

    def _tls(self) -> ssl.SSLContext | bool:
        if self.cfg.ca_file is None and self.cfg.client_cert_file is None:
            return True
        ctx = ssl.create_default_context(cafile=self.cfg.ca_file)
        if self.cfg.client_cert_file is not None:
            ctx.load_cert_chain(self.cfg.client_cert_file, self.cfg.client_key_file)
        return ctx

    def open_client(self) -> httpx.AsyncClient:
        """The one long-lived client, so the exporter keeps its TLS session. Redirects are not
        followed: a redirect would carry the headers to another host."""
        if self.client is None:
            self.client = http_client(self._tls(), self.cfg.timeout_s)
            self._own_client = True
        return self.client

    async def close(self) -> None:
        if self.client is not None and self._own_client:
            await self.client.aclose()
            self.client = None

    async def run(self) -> None:
        """Export until cancelled. An unexpected error is logged and the loop carries on."""
        try:
            while True:
                try:
                    wait = await self.cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # the exporter must never take the service down
                    wait = self.cfg.interval
                    self.stats.last_error = f"internal error: {type(err).__name__}"
                    log.error("OTLP export cycle failed: %s", type(err).__name__)
                await self.sleep(wait)
        finally:
            await self.close()

    # ---- cursors -----------------------------------------------------------------------

    @property
    def batch_limit(self) -> int:
        return self._limit or self.cfg.max_batch_points

    @property
    def signals(self) -> list[str]:
        out = list(self.cfg.signals)
        if self.cfg.include_audit:
            out.append("audit")
        return out

    async def _prepare(self) -> None:
        if self._ready:
            return
        if "metrics" in self.cfg.signals:
            # On a large table this holds the write lock until it is built, so say so.
            log.info("OTLP export: making sure the samples_seq index exists; the first start on a "
                     "large database can take a while and ingest waits meanwhile")
            started = time.monotonic()
            await self.store.storage.execute(SAMPLES_INDEX)
            log.info("OTLP export: the samples_seq index is ready after %.1f s",
                     time.monotonic() - started)
        self._ready = True

    async def _cursor(self, signal: str) -> tuple[int, int]:
        rows = await self.store.fetch("SELECT ts, last_id FROM export_cursor WHERE signal = ?",
                                      (signal,))
        if rows:
            return int(rows[0][0]), int(rows[0][1])
        # First start: export what arrives from now on, not the whole history.
        if signal == "metrics":
            top = await self.store.fetch("SELECT seq FROM ingest_seq WHERE name = 'samples'")
            start = (int(self.clock() * 1000), int(top[0][0]) if top else 0)
        else:
            table = "audit" if signal == "audit" else "host_events"
            top = await self.store.fetch(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
            start = (0, int(top[0][0]))
        await self._save(signal, start)
        return start

    async def _save(self, signal: str, pos: tuple[int, int]) -> None:
        now = self.clock()
        await self.store.storage.execute(
            "INSERT INTO export_cursor (signal, ts, last_id, updated) VALUES (?,?,?,?) "
            "ON CONFLICT (signal) DO UPDATE SET ts = excluded.ts, last_id = excluded.last_id, "
            "updated = excluded.updated", (signal, pos[0], pos[1], now))

    # ---- one pass ----------------------------------------------------------------------

    async def cycle(self) -> float:
        """Send everything that is pending. Returns the seconds to wait before the next pass:
        the interval after a clean pass, the backoff after a failure."""
        await self._prepare()
        for signal in self.signals:
            while True:
                batch = await self._next_batch(signal)
                if batch is None:
                    break
                outcome = await self._post(batch)
                if outcome.ok:
                    self.stats.sent += batch.count - outcome.rejected
                    self.stats.rejected += outcome.rejected
                    self.stats.requests += 1
                    self.stats.consecutive_failures = 0
                    self.stats.last_success = self.clock()
                    if batch.kind == self._fail_kind:  # the path that failed works again
                        self.stats.last_error = ""
                        self.stats.alert = ""
                    await self._save(signal, batch.cursor)
                    if outcome.rejected:
                        log.warning("OTLP collector rejected %d of %s", outcome.rejected, signal)
                    continue
                self.stats.failed += 1
                self.stats.last_error = outcome.reason
                self._fail_kind = batch.kind
                if outcome.retry:
                    self._gaps = batch.gaps + self._gaps  # the notice goes with the next try
                    self.stats.consecutive_failures += 1
                    await self._measure_lag()
                    return self._backoff(outcome.wait)
                if outcome.split and batch.count > 1:
                    self._limit = max(1, batch.count // 2)
                    self.stats.consecutive_failures = 0
                    log.warning("OTLP collector refused a request of %d (%d); sending %d at a "
                                "time", batch.count, outcome.status, self._limit)
                    self._gaps = batch.gaps + self._gaps
                    continue
                self._gaps = batch.gaps + self._gaps  # a gap notice rides on with the next records
                # A final answer: skip the batch so one bad request cannot block the stream, and
                # say so, so the loss is never silent.
                self.stats.dropped += batch.count
                self.stats.consecutive_failures = 0
                log.error("OTLP export of %d %s dropped: %s", batch.count, signal, outcome.reason)
                if outcome.status == NOT_FOUND or 300 <= outcome.status < 400:
                    self.stats.alert = ALERT_404
                    log.error("OTLP export misconfigured: %s", ALERT_404)
                await self._record_gap(batch.span[0], batch.span[1], signal=signal,
                                       count=batch.count, reason=outcome.reason)
                await self._save(signal, batch.cursor)
        await self._measure_lag()
        return self.cfg.interval

    def _backoff(self, wait: float | None) -> float:
        n = max(1, self.stats.consecutive_failures)
        ceiling = min(BACKOFF_MAX_S, BACKOFF_START_S * 2 ** (n - 1))
        delay = self.rng() * ceiling  # full jitter
        return max(delay, wait) if wait is not None else delay

    async def _measure_lag(self) -> None:
        lag = 0.0
        if "metrics" in self.cfg.signals:
            cur = await self._cursor("metrics")
            row = await self.store.fetch(
                "SELECT ts FROM samples WHERE seq > ? ORDER BY seq LIMIT 1", (cur[1],))
            if row:  # the age of the oldest point not yet sent
                lag = self.clock() - int(row[0][0]) / 1000.0
        for signal in self.signals:
            if signal == "metrics":
                continue
            table = "audit" if signal == "audit" else "host_events"
            cur = await self._cursor(signal)
            row = await self.store.fetch(f"SELECT MIN(ts) FROM {table} WHERE id > ?", (cur[1],))
            if row and row[0][0] is not None:
                lag = max(lag, self.clock() - float(row[0][0]))
        self.stats.lag_s = max(0.0, lag)

    # ---- reading batches ---------------------------------------------------------------

    async def _next_batch(self, signal: str) -> _Batch | None:
        if signal == "metrics":
            return await self._metrics_batch()
        if signal == "logs":
            return await self._events_batch()
        return await self._audit_batch()

    async def _metrics_batch(self) -> _Batch | None:
        while True:
            ts, last = await self._cursor("metrics")
            rows = await self.store.fetch(
                "SELECT series_id, ts, value, seq FROM samples WHERE seq > ? ORDER BY seq LIMIT ?",
                (last, self.batch_limit))
            if not rows:
                top = await self.store.fetch("SELECT seq FROM ingest_seq WHERE name = 'samples'")
                if top and int(top[0][0]) > last:
                    # Numbers were issued past the cursor and no row holds any: raw retention
                    # removed them all before they were sent.
                    now_ms = int(self.clock() * 1000)
                    await self._record_gap(ts, now_ms, signal="metrics",
                                           count=int(top[0][0]) - last, reason="raw retention")
                    await self._save("metrics", (now_ms, int(top[0][0])))
                return None
            first = int(rows[0][3])
            if first > last + 1:
                # The numbers between the cursor and the oldest retained point are gone.
                await self._record_gap(ts, int(rows[0][1]), signal="metrics",
                                       count=first - last - 1, reason="raw retention")
                await self._save("metrics", (int(rows[0][1]), first - 1))
            cursor = (int(rows[-1][1]), int(rows[-1][3]))
            request, count = await self._metrics_request(rows)
            if count:
                return _Batch("metrics", request, count, cursor,
                              (min(int(r[1]) for r in rows), max(int(r[1]) for r in rows)))
            await self._save("metrics", cursor)  # every row was filtered out: no request needed

    async def _metrics_request(self, rows: list[Any]) -> tuple[dict[str, Any], int]:
        meta = await self._series_meta({int(r[0]) for r in rows})
        wanted = set(self.cfg.resource_filter)
        grouped: dict[Any, dict[Any, dict[Any, list[dict[str, Any]]]]] = {}
        count = 0
        for sid, t, v, _seq in rows:
            m = meta.get(int(sid))
            if m is None or v is None or (wanted and m["kind"] not in wanted):
                continue
            point = {"timeUnixNano": str(int(t) * 1_000_000), "asDouble": float(v),
                     "attributes": kvs(m["point_attrs"])}
            key = (m["metric"], m["unit"], m["instrument"], m["monotonic"], m["temporality"])
            grouped.setdefault((m["resource"], m["rattrs"]), {}).setdefault(
                (m["scope"], m["version"]), {}).setdefault(key, []).append(point)
            count += 1
        resources = []
        for (_rid, rattrs), scopes in grouped.items():
            scope_list = []
            for (scope, version), metrics in scopes.items():
                items = []
                for (name, unit, instrument, mono, temporality), points in metrics.items():
                    metric: dict[str, Any] = {"name": name}
                    if unit:
                        metric["unit"] = unit
                    if instrument == "sum":
                        metric["sum"] = {"dataPoints": points, "isMonotonic": mono,
                                         "aggregationTemporality": 1 if temporality == "delta"
                                         else 2}
                    else:
                        metric["gauge"] = {"dataPoints": points}
                    items.append(metric)
                sc: dict[str, Any] = {"name": scope}
                if version:
                    sc["version"] = version
                scope_list.append({"scope": sc, "metrics": items})
            resources.append({"resource": {"attributes": kvs(_loads(rattrs))},
                              "scopeMetrics": scope_list})
        return {"resourceMetrics": resources}, count

    async def _series_meta(self, ids: set[int]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        listed = sorted(ids)
        for i in range(0, len(listed), ID_CHUNK):
            chunk = listed[i:i + ID_CHUNK]
            marks = ",".join("?" * len(chunk))
            for r in await self.store.fetch(
                    "SELECT s.id, s.metric, s.unit, s.instrument, s.monotonic, s.temporality, "
                    "s.attrs, r.id, r.kind, r.attrs, c.name, c.version FROM series s "
                    "JOIN resources r ON r.id = s.resource_id JOIN scopes c ON c.id = s.scope_id "
                    f"WHERE s.id IN ({marks})", tuple(chunk)):
                out[int(r[0])] = {
                    "metric": r[1], "unit": r[2], "instrument": r[3], "monotonic": bool(r[4]),
                    "temporality": r[5], "point_attrs": _loads(r[6]), "resource": int(r[7]),
                    "kind": r[8], "rattrs": r[9], "scope": r[10], "version": r[11]}
        return out

    async def _events_batch(self) -> _Batch | None:
        _, last = await self._cursor("logs")
        rows = await self.store.fetch(
            "SELECT id, host, ts, kind, severity, source, title, detail FROM host_events "
            "WHERE id > ? ORDER BY id LIMIT ?", (last, self.batch_limit))
        gaps, self._gaps = self._gaps, []
        if not rows and not gaps:
            return None
        by_host: dict[str, list[dict[str, Any]]] = {}
        for _id, host, ts, kind, sev, source, title, detail in rows:
            by_host.setdefault(host, []).append({
                "timeUnixNano": str(int(float(ts) * 1e9)),
                "severityNumber": SEVERITY.get(str(sev).lower(), 9), "severityText": str(sev),
                "body": {"stringValue": str(title)},
                "attributes": kvs({"event.name": kind, "observe.source": source,
                                   "observe.detail": detail})})
        resources = [{"resource": {"attributes": kvs({"host.name": h})},
                      "scopeLogs": [{"scope": {"name": "observe.events"}, "logRecords": recs}]}
                     for h, recs in by_host.items()]
        if gaps:
            resources.append({"resource": {"attributes": kvs({"service.name": "observe"})},
                              "scopeLogs": [{"scope": {"name": "observe.export"},
                                             "logRecords": gaps}]})
        return _Batch("logs", {"resourceLogs": resources}, len(rows),
                      (0, int(rows[-1][0]) if rows else last), _span_s(rows, 2), gaps)

    async def _audit_batch(self) -> _Batch | None:
        _, last = await self._cursor("audit")
        rows = await self.store.fetch(
            "SELECT id, ts, actor, kind, method, path, status, remote, detail FROM audit "
            "WHERE id > ? ORDER BY id LIMIT ?", (last, self.batch_limit))
        if not rows:
            return None
        recs = [{"timeUnixNano": str(int(float(ts) * 1e9)), "severityNumber": 9,
                 "severityText": "INFO", "body": {"stringValue": str(kind)},
                 "attributes": kvs({"event.name": f"observe.audit.{kind}",
                                    "observe.audit.actor": actor, "http.request.method": method,
                                    "url.path": path, "http.response.status_code": int(status),
                                    "client.address": remote, "observe.audit.detail": detail})}
                for _id, ts, actor, kind, method, path, status, remote, detail in rows]
        request = {"resourceLogs": [{"resource": {"attributes": kvs({"service.name": "observe"})},
                                     "scopeLogs": [{"scope": {"name": "observe.audit"},
                                                    "logRecords": recs}]}]}
        return _Batch("logs", request, len(recs), (0, int(rows[-1][0])), _span_s(rows, 1))

    async def _record_gap(self, start_ms: int, end_ms: int, *, signal: str = "metrics",
                          count: int = 0, reason: str = "raw retention") -> None:
        """Say that records could not be exported: a log line, an audit row and, when logs are
        exported, a log record in the next request. Used for retention loss and for every drop."""
        self.stats.gaps += 1
        text = (f"Observe could not export {count} {signal} records between {start_ms} and "
                f"{end_ms} (milliseconds since the epoch): {reason}.")
        log.warning("OTLP export gap: %s", text)
        try:
            await self.store.write_audit("export_gap", actor="exporter",
                                         detail={"start_ms": start_ms, "end_ms": end_ms,
                                                 "signal": signal, "count": count,
                                                 "reason": reason})
        except Exception as err:  # a failed audit row must not stop the export
            log.error("could not record the export gap in the audit log: %s", type(err).__name__)
        if "logs" not in self.cfg.signals:
            return
        self._gaps.append({"timeUnixNano": str(int(self.clock() * 1e9)), "severityNumber": 13,
                           "severityText": "WARN", "body": {"stringValue": text},
                           "attributes": kvs({"event.name": "observe.export.gap",
                                              "observe.export.gap.start_ms": start_ms,
                                              "observe.export.gap.end_ms": end_ms,
                                              "observe.export.gap.signal": signal,
                                              "observe.export.gap.count": count})})

    # ---- sending -----------------------------------------------------------------------

    def _headers(self, content_type: str) -> dict[str, str]:
        headers = {name: secret.get_secret_value() for name, secret in self.cfg.headers.items()}
        headers.update({"Content-Type": content_type, "Content-Encoding": "gzip",
                        "User-Agent": f"observe/{__version__}"})
        return headers

    async def _post(self, batch: _Batch) -> Outcome:
        message = "MetricsRequest" if batch.kind == "metrics" else "LogsRequest"
        if self.cfg.protocol == "http/json":
            body, ctype = encode.to_json(batch.request), "application/json"
        else:
            body, ctype = encode.to_protobuf(batch.request, message), "application/x-protobuf"
        data = gzip.compress(body, mtime=0)
        url = f"{self.cfg.endpoint}/v1/{batch.kind}"
        try:
            resp = await self.open_client().post(url, content=data, headers=self._headers(ctype))
        except httpx.HTTPError as err:
            # The class name only: a message can quote the URL or a header.
            return Outcome(False, retry=True, reason=f"network error: {type(err).__name__}")
        status = resp.status_code
        if status == 200:
            rejected, _ = encode.read_response(resp.content,
                                               resp.headers.get("content-type", ""))
            return Outcome(True, rejected=min(rejected, batch.count))
        if status in PERMANENT or 300 <= status < 400:  # a redirect is never followed
            return Outcome(False, reason=f"collector answered {status}", status=status,
                           split=status in SPLIT)
        if status == 413:
            return Outcome(False, reason=f"collector answered {status}", status=status,
                           split=True)
        # Any other answer (500, 502, 503, 504, 429, 401, 403, 408, a stray 3xx) is not a verdict
        # on the data: hold the batch and try again later.
        return Outcome(False, retry=True, reason=f"collector answered {status}", status=status,
                       wait=retry_after(resp.headers.get("retry-after"), self.clock()))

    # ---- status ------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        s = self.stats
        return {"enabled": True, "endpoint": self.cfg.endpoint, "protocol": self.cfg.protocol,
                "signals": self.signals, "interval": self.cfg.interval,
                "max_batch_points": self.cfg.max_batch_points, "sent": s.sent,
                "failed": s.failed, "dropped": s.dropped, "rejected": s.rejected,
                "lag_seconds": round(s.lag_s, 3), "last_success": s.last_success,
                "last_error": s.last_error, "consecutive_failures": s.consecutive_failures,
                "gaps": s.gaps, "alert": s.alert}
