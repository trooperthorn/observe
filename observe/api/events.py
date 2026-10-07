"""Events (docs/DATA-API-DESIGN.md sections 3.9 and 4.2): the monitor transitions and the events
pushed by hosts, as one feed of log records, newest first.

The design stores both in one `logs` table. Until that table exists they are two tables, and
this module merges them. A page is stable while rows are inserted: the cursor holds the time,
the source and the sort key of the last item, so a newer row never moves an older one onto
another page. Two monitor transitions of one monitor at the very same instant share a key; a
page boundary can fall between them, which a transition rate of one per poll makes unlikely.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Query

from .cursor import PageParams, encode
from .models import EventPage
from .problems import ApiProblem
from .registry import ApiContext, ApiRegistry
from .timeparse import parse_time

MONITOR_EVENT = "observe.monitor.transition"
HOST_PREFIX = "hostwatch."
# OpenTelemetry severity numbers (section 3.9).
MONITOR_SEVERITY = {"up": (9, "INFO"), "pending": (9, "INFO"), "warn": (13, "WARN"),
                    "down": (17, "ERROR")}
HOST_SEVERITY = {"info": (9, "INFO"), "warning": (13, "WARN"), "critical": (17, "ERROR")}
RANK_HOST, RANK_MONITOR = 0, 1  # the order of the sources when two events share an instant
MAX_DETAIL_KEYS = 32


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _after(rank: int, column: str, cursor: list[Any]) -> tuple[str, list[Any]]:
    ts, crank, key = cursor
    if rank < crank:
        return "ts <= ?", [ts]
    if rank > crank:
        return "ts < ?", [ts]
    return f"(ts < ? OR (ts = ? AND {column} < ?))", [ts, ts, key]


def _flatten(detail: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(detail, dict):
        for k, v in detail.items():
            if len(out) >= MAX_DETAIL_KEYS:
                break
            if isinstance(v, (str, int, float, bool)):
                out[f"observe.detail.{str(k)[:64]}"] = v
    return out


def list_events(db: Any, ctx: ApiContext, page: PageParams,
                resource: str | None = Query(None, max_length=128,
                                             description="A monitor slug or a host name"),
                kind: str | None = Query(None, pattern="^(monitor|host)$",
                                         description="monitor or host"),
                event_name: str | None = Query(None, max_length=128,
                                               description="Exact, or a prefix ending in *"),
                severity_min: int | None = Query(None, ge=1, le=24),
                since: str | None = Query(None, max_length=64),
                until: str | None = Query(None, max_length=64)) -> dict[str, Any]:
    """Monitor transitions and host events as log records, newest first."""
    after = page.after(float, int, (str, int))
    if after is not None and not isinstance(after[2], str if after[1] == RANK_MONITOR else int):
        raise ApiProblem(400, "the cursor is not valid")
    lo = parse_time(since, ctx.now, "since") if since else None
    hi = parse_time(until, ctx.now, "until") if until else None
    floor = severity_min or 0
    want = page.limit + 1
    items: list[tuple[float, int, Any, dict[str, Any]]] = []

    prefix = event_name[:-1] if event_name and event_name.endswith("*") else None

    def named(full: str) -> bool:
        if event_name is None:
            return True
        return full.startswith(prefix) if prefix is not None else full == event_name

    if kind != "host" and named(MONITOR_EVENT):
        states = [s for s, (n, _) in MONITOR_SEVERITY.items() if n >= floor]
        where, args = ["1=1"], []
        if resource:
            where.append("monitor = ?")
            args.append(resource)
        if lo is not None:
            where.append("ts >= ?")
            args.append(lo)
        if hi is not None:
            where.append("ts <= ?")
            args.append(hi)
        where.append(f"current IN ({','.join('?' * len(states))})" if states else "1=0")
        args += states
        if after is not None:
            clause, extra = _after(RANK_MONITOR, "monitor", after)
            where.append(clause)
            args += extra
        for monitor, ts, previous, current, message in db.execute(
                "SELECT monitor, ts, previous, current, message FROM events WHERE "
                + " AND ".join(where) + " ORDER BY ts DESC, monitor DESC LIMIT ?",
                (*args, want)).fetchall():
            number, text = MONITOR_SEVERITY.get(current, (9, "INFO"))
            items.append((ts, RANK_MONITOR, monitor, {
                "id": f"m:{monitor}:{int(ts * 1000)}", "ts": ts,
                "resource": {"kind": "monitor", "name": monitor},
                "event_name": MONITOR_EVENT, "severity_number": number, "severity_text": text,
                "body": message or "",
                "attributes": {"observe.monitor.id": monitor,
                               "observe.monitor.state.previous": previous,
                               "observe.monitor.state": current}}))

    host_filter: tuple[str, list[Any]] | None = ("1=1", [])
    if kind == "monitor":
        host_filter = None
    elif event_name is not None:
        if prefix is None:
            host_filter = (("kind = ?", [event_name[len(HOST_PREFIX):]])
                           if event_name.startswith(HOST_PREFIX) else None)
        elif prefix.startswith(HOST_PREFIX):
            host_filter = ("kind LIKE ? ESCAPE '\\'", [_like(prefix[len(HOST_PREFIX):]) + "%"])
        elif not HOST_PREFIX.startswith(prefix):
            host_filter = None
    if host_filter is not None:
        severities = [s for s, (n, _) in HOST_SEVERITY.items() if n >= floor]
        where, args = [host_filter[0]], list(host_filter[1])
        if resource:
            where.append("host = ?")
            args.append(resource)
        if lo is not None:
            where.append("ts >= ?")
            args.append(lo)
        if hi is not None:
            where.append("ts <= ?")
            args.append(hi)
        where.append(f"severity IN ({','.join('?' * len(severities))})" if severities else "1=0")
        args += severities
        if after is not None:
            clause, extra = _after(RANK_HOST, "id", after)
            where.append(clause)
            args += extra
        for eid, host, ts, ekind, severity, source, title, detail, boot in db.execute(
                "SELECT id, host, ts, kind, severity, source, title, detail, boot_id "
                "FROM host_events WHERE " + " AND ".join(where)
                + " ORDER BY ts DESC, id DESC LIMIT ?", (*args, want)).fetchall():
            number, text = HOST_SEVERITY.get(severity, (9, "INFO"))
            try:
                flat = _flatten(json.loads(detail))
            except ValueError:
                flat = {}
            attrs: dict[str, Any] = {"observe.source": source, "observe.severity": severity}
            if boot:
                attrs["observe.boot_id"] = boot
            items.append((ts, RANK_HOST, int(eid), {
                "id": f"h:{eid}", "ts": ts, "resource": {"kind": "host", "name": host},
                "event_name": HOST_PREFIX + ekind, "severity_number": number,
                "severity_text": text, "body": title, "attributes": {**attrs, **flat}}))

    items.sort(key=lambda i: (i[0], i[1], i[2]), reverse=True)
    more = len(items) > page.limit
    items = items[:page.limit]
    nxt = encode([items[-1][0], items[-1][1], items[-1][2]]) if more else None
    return {"items": [i[3] for i in items], "next_cursor": nxt}


def register(api: ApiRegistry) -> None:
    runtime = api.runtime
    # since= and until= may be relative (-1h), so the answer can change with the clock alone.
    api.resource("/events", list_events, EventPage, domains=("events",), tags=("events",),
                 paginate=True, memory=lambda: int(runtime.wall() // 60), summary="List events")
