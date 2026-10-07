"""The Pockethernet resources of /api/v2 (docs/DATA-API-DESIGN.md sections 4.2 and 4.10).

Reports are read here and uploaded through the key-authenticated route of upload.py, which is not
part of v2. The core mounts these under /api/v2/pockethernet and gives them its sign-in, role
check, rate limit, ETag and read connection. Every string in a report came from a phone and is
only handed over as JSON. A report body dropped by retention is `body: null` with its summary.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from observe.api import ApiRegistry
from observe.api.cursor import PageParams, encode
from observe.api.models import Page, Ts
from observe.api.problems import ApiProblem

from .pages import _SUMMARY, _detail, _jack, _ports_of, _summary, _verdict_of

# An upload that is stored but not derived (or whose derivation failed) changes no change domain,
# so these resources send no ETag and a poll always reads the table.


class PortRef(BaseModel):
    switch_id: str
    port_key: str


class ReportSummary(BaseModel):
    source: str
    report_id: str
    revision: int
    taken_at: Ts
    taken_at_ms: int
    reported_taken_at_ms: int
    clock_corrected: bool
    received_at: Ts
    updated_at: Ts
    revisions_seen: int
    tester_serial: int | None = None
    status: str
    site: str
    port_id: str
    body_pruned: bool
    ports: list[PortRef]
    verdict: Any = None


class ReportPage(Page):
    items: list[ReportSummary]


class ReportDetail(ReportSummary):
    body: dict[str, Any] | None = Field(
        None, description="The report as the phone sent it, or null once retention dropped it.")


class ReportFilters(BaseModel):
    site: str | None = Field(None, max_length=128)
    status: str | None = Field(None, max_length=32)


def list_reports(db: Any, page: PageParams, filters: ReportFilters) -> dict[str, Any]:
    """Reports, most recently updated first."""
    after = page.after(float, str, str)
    where, args = ["1=1"], []
    if after:
        where.append("(updated_at < ? OR (updated_at = ? AND (source > ? OR "
                     "(source = ? AND report_id > ?))))")
        args += [after[0], after[0], after[1], after[1], after[2]]
    for column, value in (("site", filters.site), ("status", filters.status)):
        if value:
            where.append(f"{column} = ?")
            args.append(value)
    rows = db.execute(
        f"SELECT {_SUMMARY} FROM field_reports WHERE " + " AND ".join(where)
        + " ORDER BY updated_at DESC, source, report_id LIMIT ?",
        (*args, page.limit + 1)).fetchall()
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    items = [{**_summary(r), "ports": _ports_of(db, r[1]), "verdict": _verdict_of(db, r[1])}
             for r in rows]
    last = rows[-1] if rows else None
    return {"items": items,
            "next_cursor": encode([last[7], last[0], last[1]]) if more and last else None}


def get_report(db: Any, source: str, report_id: str) -> dict[str, Any]:
    """One report with its body and the ports it produced."""
    got = _detail(db, source[:128], report_id[:128])
    if got is None:
        raise ApiProblem(404, "unknown report")
    got["verdict"] = _verdict_of(db, report_id[:128])
    return got


class JackDetail(BaseModel):
    jack_key: str
    room: str
    site: str
    switch_id: str | None = None
    port_key: str | None = None
    first_seen: Ts
    last_seen: Ts
    history: list[dict[str, Any]]
    reports: list[dict[str, Any]]
    links: list[dict[str, Any]]


def get_jack(db: Any, key: str) -> dict[str, Any]:
    """A jack with the ports its label was seen on, in the order seen, and its reports."""
    got = _jack(db, key[:256])
    if got is None:
        raise ApiProblem(404, "unknown jack")
    return got


def register(api: ApiRegistry) -> None:
    api.resource("/pockethernet/reports", list_reports, ReportPage, etag=False,
                 tags=("pockethernet",), paginate=True, filters=ReportFilters,
                 operation_id="reports", summary="List field reports")
    api.resource("/pockethernet/reports/{source}/{report_id}", get_report, ReportDetail,
                 etag=False, tags=("pockethernet",), operation_id="report",
                 summary="One field report")
    api.resource("/pockethernet/jacks/{key:path}", get_jack, JackDetail, etag=False,
                 tags=("pockethernet",), operation_id="jack", summary="One jack")
