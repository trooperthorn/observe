"""The field report upload routes: POST /api/v1/field-reports and GET .../ping.

The core mounts this router (watchpost/web.py) with its own dependencies: the per-peer rate
limit (429) and a `wpf` bearer key (401), both before the body is read, and an audit row for
every upload, accepted or refused. This module adds the rest, cheapest check first:

1. The body is read with a hard cap of MAX_REPORT_BYTES (413). That cap applies to the bytes on
   the wire, so a compressed body is also capped.
2. `Content-Encoding` must be absent, `identity` or `gzip` (415).
3. A gzip body is inflated with a cap of MAX_REPORT_BYTES (413) and a compression ratio limit
   (413), so a small body cannot expand into memory. Truncated data, trailing data and a second
   gzip member are refused (400). The inflater never produces more than the cap plus one byte.
4. The schema (watchpost_pockethernet/schema.py) validates the report (400, 413 or 422).
5. The clock is checked and corrected (see correct_clock).
6. The report is stored by `(source, report_id)` and revision (reports.py).
7. A new or replaced report is derived into port properties and map edges (derive.py). A
   failure there never fails the upload: the evidence is stored, the audit row says the
   derivation failed, and a rebuild can repeat it.

The stored body is the exact JSON the phone sent, after inflating, never the corrected
version. The corrected time is a column beside it.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from watchpost.infra import InfraService

from .derive import derive_report
from .reports import NewReport, store_report
from .schema import MAX_REPORT_BYTES, ReportError, parse_report

MAX_RATIO = 50  # inflated bytes per compressed byte; real reports compress about 5 to 15 times
RATIO_FLOOR = 16_384  # below this size the ratio is not checked, because it cannot hurt
MAX_SKEW_MS = 300_000  # a phone clock off by more than this is corrected
SENT_HEADER = "x-report-sent-ms"
MAX_EPOCH_MS = 253_402_300_799_999


def inflate(data: bytes) -> bytes:
    """Inflate one gzip member under the size and ratio caps, or raise ReportError."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)  # gzip framing only, not raw zlib or deflate
    try:
        out = d.decompress(data, MAX_REPORT_BYTES + 1)
    except zlib.error as err:
        raise ReportError("body is not valid gzip", 400) from err
    if len(out) > MAX_REPORT_BYTES:
        raise ReportError("report is too large once inflated", 413)
    if len(out) > RATIO_FLOOR and len(out) > MAX_RATIO * len(data):
        raise ReportError("compression ratio is too high", 413)
    if not d.eof:
        raise ReportError("gzip body is truncated", 400)
    if d.unused_data:
        raise ReportError("gzip body has data after the compressed stream", 400)
    return out


def correct_clock(taken_ms: int, now_s: float, sent_ms: int | None) -> tuple[int, bool]:
    """The report time to use and whether it was corrected.

    When the phone says what time its clock showed as it sent (`X-Report-Sent-Ms`) and that
    differs from ours by more than MAX_SKEW_MS, the whole difference is added to the report
    time, so a phone that is hours slow or fast still lands on the right moment. Without the
    header only a time in the future can be recognised: past `now` plus MAX_SKEW_MS it is set
    to now. A past time without the header is kept, because a queued report is legitimately old.
    """
    now_ms = int(now_s * 1000)
    corrected = False
    if sent_ms is not None and abs(now_ms - sent_ms) > MAX_SKEW_MS:
        taken_ms = max(0, taken_ms + (now_ms - sent_ms))
        corrected = True
    if taken_ms > now_ms + MAX_SKEW_MS:
        taken_ms, corrected = now_ms, True
    return taken_ms, corrected


async def _read_capped(request: Request) -> bytes | None:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_REPORT_BYTES:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REPORT_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _refuse(request: Request, status: int, reason: str) -> JSONResponse:
    request.state.audit_detail = {"reason": reason[:200]}
    return JSONResponse({"detail": reason}, status_code=status)


def build_router() -> APIRouter:
    router = APIRouter()

    def clock(request: Request) -> Callable[[], float]:
        return request.app.state.plugin_clock  # type: ignore[no-any-return]

    @router.get("/field-reports/ping")
    async def ping(request: Request) -> dict[str, Any]:
        """Checks the key and tells the phone our clock, so it can show a skew warning."""
        _, device = request.state.plugin_key
        return {"ok": True, "device": device, "server_time_ms": int(clock(request)() * 1000),
                "max_report_bytes": MAX_REPORT_BYTES, "encodings": ["identity", "gzip"]}

    @router.post("/field-reports")
    async def upload(request: Request) -> JSONResponse:
        prefix, device = request.state.plugin_key
        body = await _read_capped(request)
        if body is None:
            return _refuse(request, 413, "body too large")
        encoding = request.headers.get("content-encoding", "").strip().lower()
        sent_ms: int | None = None
        sent = request.headers.get(SENT_HEADER)
        if sent is not None:
            if not sent.isascii() or not sent.isdigit() or int(sent) > MAX_EPOCH_MS:
                return _refuse(request, 400, "X-Report-Sent-Ms is not a time in milliseconds")
            sent_ms = int(sent)
        try:
            if encoding == "gzip":
                body = inflate(body)
            elif encoding not in ("", "identity"):
                raise ReportError("only gzip or no content encoding is accepted", 415)
            report = parse_report(body)
        except ReportError as err:
            return _refuse(request, err.status, err.reason)
        now = clock(request)()
        taken_ms, corrected = correct_clock(report.taken_at_ms, now, sent_ms)
        site = report.site
        outcome = await store_report(request.app.state.plugin_store, NewReport(
            source=device, report_id=report.report_id, revision=report.revision,
            taken_at_ms=taken_ms, reported_taken_at_ms=report.taken_at_ms,
            clock_corrected=corrected, tester_serial=report.device.serial,
            status=report.status, site=site.site if site else "",
            port_id=site.port_id if site else "", body=body, received_at=now,
            key_prefix=prefix))
        request.state.audit_detail = {
            "result": outcome.result, "report_id": report.report_id,
            "revision": report.revision, "stored_revision": outcome.revision,
            "clock_corrected": corrected, "bytes": len(body)}
        if outcome.result in ("accepted", "replaced"):
            try:
                derived = await derive_report(
                    InfraService(request.app.state.plugin_store), report, key_prefix=prefix,
                    device=device, taken_ms=taken_ms, now=now)
                request.state.audit_detail["derived"] = derived.as_detail()
            except Exception as err:  # the evidence is stored; only the derivation is lost
                request.state.audit_detail["derive_failed"] = type(err).__name__
        return JSONResponse({
            "result": outcome.result, "report_id": report.report_id,
            "revision": outcome.revision, "clock_corrected": corrected,
            "taken_at_ms": taken_ms})

    return router
