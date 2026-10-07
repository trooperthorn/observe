"""The field report handler: a field report arrives as an OTLP log record.

The phone sends one OTLP/HTTP request to `POST /v1/logs` with a `wpf` key (docs/DATA-API-DESIGN.md
section 7.4). The core (observe/otlp/api.py) does everything that is not about reports: the rate
limit, the key check, the size and gzip caps, the decoding, the audit row, and the response with
its partial success. For each log record whose `event.name` is `observe.pockethernet.report` it
calls `handle_report`, and this module adds the rest:

1. The record body is a string holding the report JSON, exactly as the phone built it. Any other
   body is refused.
2. The schema (observe_pockethernet/schema.py) validates the report.
3. The clock is checked and corrected (see correct_clock).
4. The report is stored by `(source, report_id)` and revision (reports.py).
5. A new or replaced report is derived into port properties and map edges (derive.py) in the
   same transaction as step 4, so a report is one commit. A failure there never loses the
   evidence: the derivation is rolled back to a savepoint, the report is stored with
   derive_status `failed`, the audit row says so, and an admin retry or rebuild derives it
   again.

A record the handler refuses is counted in the partial success of the response and is not
retried by the phone, because the same bytes would be refused again. The stored body is the exact
JSON the phone sent, never the corrected version. The corrected time is a column beside it. A
report is identified by its own id and revision, so a resend of the same report is a duplicate
whether or not the phone sent an Idempotency-Key.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from observe.plugins import LogContext, LogRejected

from .reports import NewReport, store_and_derive
from .schema import MAX_REPORT_BYTES, ReportError, parse_report

log = logging.getLogger(__name__)

EVENT = "observe.pockethernet.report"
MAX_SKEW_MS = 300_000  # a phone clock off by more than this is corrected


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


async def handle_report(store: Any, ctx: LogContext, record: Any) -> Mapping[str, Any]:
    """Store and derive one report record. Raises LogRejected for a record that is not valid."""
    if not isinstance(record.body, str):
        raise LogRejected("the report record body must be a string holding the report JSON")
    body = record.body.encode("utf-8")
    if len(body) > MAX_REPORT_BYTES:
        raise LogRejected("report is too large")
    try:
        report = parse_report(body)
    except ReportError as err:
        raise LogRejected(err.reason) from err
    taken_ms, corrected = correct_clock(report.taken_at_ms, ctx.now, ctx.sent_ms)
    site = report.site
    ingested = await store_and_derive(store, NewReport(
        source=ctx.device, report_id=report.report_id, revision=report.revision,
        taken_at_ms=taken_ms, reported_taken_at_ms=report.taken_at_ms,
        clock_corrected=corrected, tester_serial=report.device.serial,
        status=report.status, site=site.site if site else "",
        port_id=site.port_id if site else "", body=body, received_at=ctx.now,
        key_prefix=ctx.key_prefix), report, now=ctx.now)
    outcome = ingested.outcome
    detail: dict[str, Any] = {
        "result": outcome.result, "report_id": report.report_id,
        "revision": report.revision, "stored_revision": outcome.revision,
        "clock_corrected": corrected, "bytes": len(body), "taken_at_ms": taken_ms}
    if ingested.derived is not None:
        detail["derived"] = ingested.derived.as_detail()
    elif ingested.derive_error:
        detail["derive_status"] = "failed"
        detail["derive_failed"] = ingested.derive_error
    return detail
