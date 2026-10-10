"""POST /v1/metrics and POST /v1/logs: the OTLP/HTTP ingest (docs/DATA-API-DESIGN.md section 6).

This is the one write path that does not use a login, so it is the most constrained. Order of
checks, cheapest and least informative first:

1. Rate limit: per key for a valid key (a token bucket with a burst for an outbox replay), per
   peer for a missing or wrong key (429, always with Retry-After, which the agent must honour).
2. A bearer key is required and must be valid and unrevoked (401). A wpi key writes host data, a
   wpf key writes field data, and a valid key of any other scope is refused (403). A per-key rate
   limit follows (429). The body is not read before this passes.
3. The content type is application/x-protobuf or application/json (415) and the content
   encoding is identity or gzip (415).
4. The body is read with a hard cap of MAX_BODY_BYTES (413) and inflated with a cap of
   MAX_INFLATED_BYTES (413). A malformed gzip stream, protobuf or JSON body, or JSON nested more
   than MAX_JSON_DEPTH deep, is 400, which a client must not retry.
5. A repeat of a batch already stored is answered without decoding it (Idempotency-Key, or the
   hash of the body).
6. Decoding and validation run on a worker thread. A resource whose host.name is not the host the
   key is bound to, and every point or record that is not well formed, is rejected and counted in
   the partial success of a 200 response. A request in which nothing at all was accepted because
   of a host mismatch is a 403 instead.
7. The accepted data is written in one transaction.

Nothing is stored from a request that fails a check before step 6. Denials are written to the
audit log through the shared Guard, at most one row per peer per window.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
import zlib
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import audit
from ..ingest.api import Guard, bearer
from ..ingest.boot import classify_events
from ..ingest.keys import key_host, verify_key
from ..ingest.schema import MAX_BODY_BYTES, MAX_JSON_DEPTH
from ..plugins import LoadedPlugins, LogContext, LogRejected
from ..storage import StorageBusy, series
from ..store import IdempotencyConflict, IngestOutcome, MAX_FUTURE_SKEW_S, Store
from . import normalize, wire

log = logging.getLogger("observe.otlp")

MAX_INFLATED_BYTES = 4 * 1024 * 1024
# A body may inflate to at most this many times its compressed size (always up to
# MIN_INFLATED_ALLOWANCE). Telemetry compresses about ten to one; a body that expands a hundred
# times or more is a decompression bomb, refused before any decoding work is spent on it.
MAX_INFLATE_RATIO = 100
MIN_INFLATED_ALLOWANCE = 64 * 1024
# Extra requests charged to the key's rate limit for a body refused only after it was inflated
# or decoded, so a hostile key can not repeat expensive refusals at the full request rate.
REFUSAL_COST = 4
HOST_SCOPE, FIELD_SCOPE = "wpi", "wpf"
REFUSED_SCOPES = ("wpc", "wpr")  # control and read keys never write data
LAST_USED_EVERY_S = 60.0
MAX_IDEMPOTENCY_KEY = 128
SENT_HEADER = "x-report-sent-ms"
MAX_EPOCH_MS = 253_402_300_799_999
PROTOBUF, JSON = "application/x-protobuf", "application/json"


class BadRequest(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


async def _read_capped(request: Request) -> bytes | None:
    """The request body, or None when it exceeds MAX_BODY_BYTES."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def inflate(data: bytes) -> bytes:
    """Inflate one gzip member under MAX_INFLATED_BYTES, or raise BadRequest. The inflater never
    produces more than the cap plus one byte, so a small body can not expand into memory."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)  # gzip framing only, not raw zlib or deflate
    try:
        out = d.decompress(data, MAX_INFLATED_BYTES + 1)
    except zlib.error as err:
        raise BadRequest(400, "body is not valid gzip") from err
    if len(out) > MAX_INFLATED_BYTES:
        raise BadRequest(413, "body is too large once inflated")
    if len(out) > max(MIN_INFLATED_ALLOWANCE, MAX_INFLATE_RATIO * len(data)):
        raise BadRequest(400, "body expands too much when inflated")
    if not d.eof:
        raise BadRequest(400, "gzip body is truncated")
    if d.unused_data:
        raise BadRequest(400, "gzip body has data after the compressed stream")
    return out


def too_deep(body: bytes) -> bool:
    """True when the JSON nesting depth exceeds MAX_JSON_DEPTH. A linear scan that ignores
    brackets inside strings, run before json.loads so a deeply nested body can not recurse."""
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
            if depth > MAX_JSON_DEPTH:
                return True
        elif byte in (0x5D, 0x7D):
            depth -= 1
    return False


def decode_body(body: bytes, content_type: str, signal: str) -> Any:
    """The OTLP JSON shape of a request body of either content type. Raises BadRequest."""
    if content_type == PROTOBUF:
        try:
            return wire.decode(body, "MetricsRequest" if signal == "metrics" else "LogsRequest")
        except wire.WireError as err:
            raise BadRequest(400, f"body is not a valid OTLP protobuf message: {err}") from err
    if too_deep(body):
        raise BadRequest(400, "body is nested too deeply")
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as err:
        raise BadRequest(400, "body is not valid JSON") from err


def _batch_id(signal: str, idem: str | None) -> str:
    """The id a batch is remembered by: the Idempotency-Key when the producer sent one. The
    signal is part of it, because one key may legitimately be used for the metrics and the logs
    request of the same batch. A request without a key is not recognised by its body, because a
    body without timestamps repeats legitimately (every point takes the receive time); its points
    are deduplicated by series and timestamp and its log records by their dedup key instead, so
    it gets a fresh id."""
    if idem is not None:
        return "idem:" + hashlib.sha256(f"{signal}\0{idem}".encode()).hexdigest()[:40]
    return "otlp:" + uuid.uuid4().hex


def _body_hash(signal: str, body: bytes) -> str:
    """The hash of the (inflated) body an idempotency record is made from."""
    return hashlib.sha256(signal.encode() + b"\0" + body).hexdigest()[:40]


def drop_reason(capped: int, late: int) -> str:
    """The partial success text for points that were valid but not stored."""
    parts = []
    if capped:
        parts.append(f"{capped} points dropped: series limit reached (at most "
                     f"{series.MAX_SERIES_PER_RESOURCE} series per resource and "
                     f"{series.MAX_SERIES_TOTAL} in all)")
    if late:
        parts.append(f"{late} points ignored: older than the raw retention")
    return "; ".join(parts)


def _join(first: str, second: str) -> str:
    return "; ".join(t for t in (first, second) if t)[:512]


def _reply(content_type: str, signal: str, rejected: int, message: str) -> Response:
    if content_type == PROTOBUF:
        return Response(wire.partial_success_body(rejected, message), media_type=PROTOBUF)
    if not rejected and not message:
        return JSONResponse({})
    field = "rejectedDataPoints" if signal == "metrics" else "rejectedLogRecords"
    return JSONResponse({"partialSuccess": {field: str(rejected), "errorMessage": message}})


def problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse({"type": "about:blank", "title": title, "status": status,
                         "detail": detail}, status_code=status,
                        media_type="application/problem+json")


def build_router(store: Store, guard: Guard, plugins: LoadedPlugins,
                 wall: Callable[[], float] = time.time,
                 on_samples: Callable[[str, Any, float], Awaitable[None]] | None = None
                 ) -> APIRouter:
    router = APIRouter()
    last_used: dict[str, float] = {}  # key prefix -> monotonic time its last use was recorded

    async def authenticate(request: Request) -> tuple[str, str, str, str] | JSONResponse:
        """(scope, key, prefix, bound host or device) for a valid host or field key, or the
        refusal to send."""
        peer = request.client.host if request.client else "unknown"
        key = bearer(request)
        for scope in (HOST_SCOPE, FIELD_SCOPE):
            bound = await key_host(store, key, scope) if key else None
            if bound is not None:
                break
        else:
            if not guard.fail_limiter.allow(peer):
                return await guard.deny(request, 429, "rate limit exceeded")
            if key and any([await key_host(store, key, s) for s in REFUSED_SCOPES]):
                return await guard.deny(request, 403, "this key may not push data")
            return await guard.deny(request, 401, "missing or invalid ingest key")
        prefix, bound_to = bound
        wait = guard.key_limiter.take(prefix)
        if wait:
            return await guard.deny(request, 429, "rate limit exceeded", prefix, retry_after=wait)
        now = guard.clock()
        if prefix not in last_used or now - last_used[prefix] >= LAST_USED_EVERY_S:
            if not await verify_key(store, key or "", bound_to, scope=scope):
                return await guard.deny(request, 401, "missing or invalid ingest key")
            if len(last_used) > 4096:
                last_used.clear()
            last_used[prefix] = now
        return scope, key or "", prefix, bound_to

    async def read_request(request: Request, signal: str, prefix: str,
                           scope: str) -> tuple[bytes, str] | JSONResponse:
        """The inflated body and its content type, or the refusal to send."""
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in (PROTOBUF, JSON):
            return await guard.deny(request, 415, "content type must be application/x-protobuf "
                                    "or application/json", prefix)
        encoding = request.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity", "gzip"):
            return await guard.deny(request, 415, "only gzip or no content encoding is accepted",
                                    prefix)
        body = await _read_capped(request)
        if body is None:
            return await guard.deny(request, 413, "body too large", prefix)
        if encoding == "gzip":
            try:
                body = inflate(body)
            except BadRequest as err:
                guard.key_limiter.charge(prefix, REFUSAL_COST)
                return await guard.deny(request, err.status, err.reason, prefix)
        return body, ctype

    async def push(request: Request, signal: str) -> Response:
        try:
            return await handle(request, signal)
        except StorageBusy:
            # No database connection was free in time: the producer keeps its batch and retries.
            return Response(status_code=503, headers={"Retry-After": "5"})

    async def handle(request: Request, signal: str) -> Response:
        auth = await authenticate(request)
        if isinstance(auth, JSONResponse):
            return auth
        scope, _key, prefix, bound = auth
        got = await read_request(request, signal, prefix, scope)
        if isinstance(got, JSONResponse):
            return got
        body, ctype = got
        peer = request.client.host if request.client else "unknown"
        now = wall()
        idem = request.headers.get("idempotency-key")
        if idem is not None and not (0 < len(idem) <= MAX_IDEMPOTENCY_KEY and idem.isascii()
                                     and idem.isprintable()):
            return await guard.deny(request, 400, "Idempotency-Key is not valid", prefix)
        batch_id = _batch_id(signal, idem)
        body_hash = _body_hash(signal, body)
        if scope == HOST_SCOPE and idem is not None:
            seen = await store.fetch("SELECT body_hash FROM ingest_batches WHERE host = ? "
                                     "AND batch_id = ?", (bound, batch_id))
            if seen:
                if seen[0][0] and seen[0][0] != body_hash:
                    return await conflict(request, prefix, bound)
                return _reply(ctype, signal, 0, "")  # a resend: stored, nothing to do
        try:
            req = await asyncio.to_thread(decode_body, body, ctype, signal)
        except BadRequest as err:
            guard.key_limiter.charge(prefix, REFUSAL_COST)
            return await guard.deny(request, err.status, err.reason, prefix)
        if scope == FIELD_SCOPE:
            return await field_push(request, signal, req, ctype, prefix, bound, now)
        return await host_push(request, signal, req, ctype, prefix, bound, batch_id, body_hash,
                               now, peer)

    async def conflict(request: Request, prefix: str, bound: str) -> JSONResponse:
        return await guard.deny(request, 409, "Idempotency-Key was already used for a different "
                                "body", prefix, {"host": bound})

    async def host_push(request: Request, signal: str, req: Any, ctype: str, prefix: str,
                        bound: str, batch_id: str, body_hash: str, now: float,
                        peer: str) -> Response:
        fn = normalize.normalize_metrics if signal == "metrics" else normalize.normalize_logs
        result = await asyncio.to_thread(fn, req, bound, now)
        rejects = result.rejects
        if result.batch is None and rejects.count and not result.accepted and any(
                "host.name" in r for r in rejects.reasons):
            return await guard.deny(request, 403, "key is bound to another host", prefix,
                                    {"bound_host": bound, "rejected": rejects.count})
        dropped = 0
        drop_reasons = ""
        if result.batch is not None:
            batch = result.batch
            batch.batch_id = batch_id
            outcome = IngestOutcome()
            try:
                stored, _events, duplicate = await store.ingest_batch(
                    batch, classify_events(batch.events), now=now, body_hash=body_hash,
                    outcome=outcome)
            except IdempotencyConflict:
                return await conflict(request, prefix, bound)
            except StorageBusy:
                raise  # a refusal, not a failure: no audit row, push() answers 503 at once
            except Exception as err:
                # Nothing was stored, so record that the batch failed partway.
                await audit.record(store, "ingest_failed", actor=prefix, method=request.method,
                                   path=request.url.path, status=500, remote=peer,
                                   detail={"host": bound, "error": type(err).__name__})
                raise
            if not duplicate:
                dropped = len(batch.samples) - stored
                drop_reasons = drop_reason(outcome.capped, outcome.late)
                if on_samples is not None and batch.samples:
                    await on_samples(bound, batch.samples, now)  # the threshold rules
        return _reply(ctype, signal, rejects.count + dropped,
                      _join(drop_reasons, rejects.message()))

    async def field_push(request: Request, signal: str, req: Any, ctype: str, prefix: str,
                         device: str, now: float) -> Response:
        """A field key's request: metrics for field testers and ports, and log records for the
        plugin that handles each event name."""
        detail: dict[str, Any] = {"device": device, "signal": signal}
        if signal == "metrics":
            points, rejects = await asyncio.to_thread(normalize.normalize_field_metrics,
                                                      req, device, now)
            capped, late = await store.storage.write(
                lambda db: _write_field_points(db, points, now, store.storage.incremental_rollups,
                                               store.raw_cut(db, now)),
                touches=("metrics",)) if points else (0, 0)
            dropped = capped + late
            detail.update(points=len(points) - dropped)
            rejected = rejects.count + dropped
            message = _join(drop_reason(capped, late), rejects.message())
        else:
            records, rejects = await asyncio.to_thread(normalize.field_records, req, now)
            sent = request.headers.get(SENT_HEADER)
            sent_ms = None
            if sent is not None:
                if not sent.isascii() or not sent.isdigit() or int(sent) > MAX_EPOCH_MS:
                    return await guard.deny(request, 400, "X-Report-Sent-Ms is not a time in "
                                            "milliseconds", prefix)
                sent_ms = int(sent)
            ctx = LogContext(device, prefix, now, sent_ms)
            handled: list[dict[str, Any]] = []
            for rec in records:
                found = plugins.log_handler(rec.event)
                if found is None:
                    rejects.add("no plugin handles this event name")
                    continue
                name, handler = found
                try:
                    out = await handler(store, ctx, rec)
                except LogRejected as err:
                    rejects.add(str(err)[:200] or "the record was refused")
                    continue
                except StorageBusy:
                    raise  # a refusal, not a plugin failure: answered 503 without an audit row
                except Exception as err:
                    await audit.record(store, "plugin_failed", actor=prefix, method=request.method,
                                       path=request.url.path, status=500,
                                       remote=request.client.host if request.client else "",
                                       detail={"plugin": name, "error": type(err).__name__})
                    raise
                handled.append({"plugin": name, **dict(out)})
            detail.update(records=len(handled), handled=handled[:20])
            rejected, message = rejects.count, rejects.message()
        detail["rejected"] = rejected
        await audit.record(store, "plugin_request", actor=prefix, method=request.method,
                           path=request.url.path, status=200,
                           remote=request.client.host if request.client else "", detail=detail)
        return _reply(ctype, signal, rejected, message)

    @router.post("/v1/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        return await push(request, "metrics")

    @router.post("/v1/logs", include_in_schema=False)
    async def logs(request: Request) -> Response:
        return await push(request, "logs")

    @router.post("/v1/traces", include_in_schema=False)
    async def traces() -> JSONResponse:
        return problem(404, "Not Found", "Observe does not accept traces; send metrics to "
                       "/v1/metrics and logs to /v1/logs.")

    return router


def _write_field_points(db: Any, points: list[normalize.FieldPoint], now: float,
                        rollups: bool, raw_cut: Callable[[str], int] | None = None
                        ) -> tuple[int, int]:
    """Store the points of a field key's request, one resource at a time. Returns the number
    refused by the series cardinality guard and the number ignored as older than the raw
    retention."""
    groups: dict[tuple[str, str], tuple[dict[str, Any], list[series.Point]]] = {}
    now_ms = series.to_ms(now)
    ahead = now_ms + series.to_ms(MAX_FUTURE_SKEW_S)
    for p in points:
        attrs, pts = groups.setdefault((p.kind, p.name), (p.attrs, []))
        ts_ms = now_ms if p.ts_ms > ahead else p.ts_ms  # a clock far ahead is stored at receipt
        pts.append(series.Point(p.scope, p.metric, p.unit, p.labels, ts_ms, p.value))
    capped = late = 0
    for (kind, name), (attrs, pts) in groups.items():
        rec = series.record_points(db, kind=kind, name=name, points=pts, now=now,
                                   rollups=rollups, attrs=attrs, raw_cut=raw_cut)
        capped += rec.dropped
        late += rec.late
    return capped, late
