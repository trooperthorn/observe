"""The data behind the report list, report detail and jack pages.

These are read-only routes for a logged-in user; the core mounts them behind its session check
(watchpost/web.py), so nothing here handles authentication. Every string in a report came from a
phone, so the pages write it with textContent only (static/pockethernet.js) and this module only
hands it over as JSON. A report body dropped by retention is reported as `body: null` with the
summary row still present.

A report is joined to the ports it produced through the port property rows that carry its
report id. A jack is joined to its history through its `jack_label` property rows, which is how
a jack that moved between ports shows up in the order it was seen.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from watchpost.store import Store

from .derive import SOURCE

MAX_LIMIT = 200
_SUMMARY = ("source, report_id, revision, taken_at_ms, reported_taken_at_ms, clock_corrected, "
            "received_at, updated_at, revisions_seen, tester_serial, status, site, port_id, "
            "body_pruned_at")


def _summary(r: tuple[Any, ...]) -> dict[str, Any]:
    keys = [k.strip() for k in _SUMMARY.split(",")]
    row = dict(zip(keys, r))
    row["clock_corrected"] = bool(row["clock_corrected"])
    row["body_pruned"] = row.pop("body_pruned_at") is not None
    row["taken_at"] = row["taken_at_ms"] / 1000.0
    return row


def _ports_of(db: Any, report_id: str) -> list[dict[str, str]]:
    return [{"switch_id": s, "port_key": p} for s, p in db.execute(
        "SELECT DISTINCT switch_id, port_key FROM port_properties "
        "WHERE source=? AND report_id=? ORDER BY switch_id, port_key", (SOURCE, report_id))]


def _list_sync(store: Store, limit: int, offset: int) -> dict[str, Any]:
    with store._lock:
        db = store._db
        total = db.execute("SELECT COUNT(*) FROM field_reports").fetchone()[0]
        rows = db.execute(
            f"SELECT {_SUMMARY} FROM field_reports "
            "ORDER BY updated_at DESC, source, report_id LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
        reports = [{**_summary(r), "ports": _ports_of(db, r[1])} for r in rows]
    return {"total": total, "reports": reports}


def _detail_sync(store: Store, source: str, report_id: str) -> dict[str, Any] | None:
    with store._lock:
        db = store._db
        row = db.execute(f"SELECT {_SUMMARY}, body FROM field_reports "
                         "WHERE source=? AND report_id=?", (source, report_id)).fetchone()
        if row is None:
            return None
        out = {**_summary(row[:-1]), "ports": _ports_of(db, report_id)}
    out["body"] = json.loads(bytes(row[-1])) if row[-1] is not None else None
    return out


def _jack_sync(store: Store, key: str) -> dict[str, Any] | None:
    with store._lock:
        db = store._db
        jack = db.execute("SELECT jack_key, room, site, switch_id, port_key, first_seen, "
                          "last_seen FROM infra_jacks WHERE jack_key=?", (key,)).fetchone()
        if jack is None:
            return None
        history = [
            {"switch_id": s, "port_key": p, "report_id": rid, "observed_at": obs,
             "recorded_at": rec, "recorded_by": by, "source": src}
            for s, p, rid, obs, rec, by, src in db.execute(
                "SELECT switch_id, port_key, report_id, observed_at, recorded_at, recorded_by, "
                "source FROM port_properties WHERE name='jack_label' AND value=? "
                "ORDER BY observed_at DESC, id DESC LIMIT 200", (json.dumps(key, sort_keys=True),))]
        ids = {h["report_id"] for h in history if h["report_id"]}
        marks = ",".join("?" * len(ids))
        reports = db.execute(
            f"SELECT {_SUMMARY} FROM field_reports WHERE port_id=? "
            + (f"OR report_id IN ({marks}) " if ids else "")
            + "ORDER BY updated_at DESC, source, report_id LIMIT 200",
            (key, *sorted(ids))).fetchall()
        links = db.execute(
            "SELECT b_ref, source, confidence, first_seen, last_seen, closed_at FROM infra_links "
            "WHERE a_kind='jack' AND a_ref=? ORDER BY id DESC", (key,)).fetchall()
    return {
        "jack_key": jack[0], "room": jack[1], "site": jack[2], "switch_id": jack[3],
        "port_key": jack[4], "first_seen": jack[5], "last_seen": jack[6],
        "history": history, "reports": [_summary(r) for r in reports],
        "links": [{"port": b, "source": s, "confidence": c, "first_seen": f, "last_seen": ls,
                   "closed_at": cl} for b, s, c, f, ls, cl in links],
    }


def build_pages_router() -> APIRouter:
    router = APIRouter()

    @router.get("/reports")
    async def report_list(request: Request, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, MAX_LIMIT))
        return await asyncio.to_thread(_list_sync, request.app.state.plugin_store, limit,
                                       max(0, offset))

    @router.get("/report")
    async def report_detail(request: Request, source: str, report_id: str) -> dict[str, Any]:
        got = await asyncio.to_thread(_detail_sync, request.app.state.plugin_store, source[:128],
                                      report_id[:128])
        if got is None:
            raise HTTPException(404, "unknown report")
        return got

    @router.get("/jack")
    async def jack_detail(request: Request, key: str) -> dict[str, Any]:
        got = await asyncio.to_thread(_jack_sync, request.app.state.plugin_store, key[:256])
        if got is None:
            raise HTTPException(404, "unknown jack")
        return got

    return router
