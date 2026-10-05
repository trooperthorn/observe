"""The field report store: one row per report, the raw body kept as evidence.

A report is keyed by `(source, report_id)`. The source is the device label the upload key is
bound to, so a key for one phone can never replace or hide a report of another phone, even
with a guessed report id. Port properties and links are derived from these rows (derive.py),
so the body is the evidence they can be rebuilt from.

Revisions decide what an upload does: a higher revision replaces the stored report, an equal
revision is a duplicate and changes nothing, and a lower revision is ignored. The decision and
the write happen in one transaction under the store's lock, so two uploads of the same report
cannot both win.

Retention applies to the body only. After `evidence_retention_days` without an update the body
is dropped and the summary row stays, so a late replay of an old report is still recognised as
a duplicate instead of being stored again.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Literal

from watchpost.plugins import Migration
from watchpost.store import Store

Result = Literal["accepted", "replaced", "duplicate", "ignored"]

MIGRATIONS = (
    Migration(1, (
        """CREATE TABLE IF NOT EXISTS field_reports (
  source TEXT NOT NULL,
  report_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  taken_at_ms INTEGER NOT NULL,
  reported_taken_at_ms INTEGER NOT NULL,
  clock_corrected INTEGER NOT NULL DEFAULT 0,
  received_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  revisions_seen INTEGER NOT NULL DEFAULT 1,
  tester_serial INTEGER,
  status TEXT NOT NULL,
  site TEXT NOT NULL DEFAULT '',
  port_id TEXT NOT NULL DEFAULT '',
  body_sha256 TEXT NOT NULL,
  body BLOB,
  body_pruned_at REAL,
  PRIMARY KEY (source, report_id)
)""",
        "CREATE INDEX IF NOT EXISTS field_reports_updated ON field_reports (updated_at)",
    )),
    # The key prefix is kept so a rebuild can credit the same key as the live accept did.
    Migration(2, ("ALTER TABLE field_reports ADD COLUMN key_prefix TEXT NOT NULL DEFAULT ''",)),
)


@dataclass(frozen=True)
class NewReport:
    source: str
    report_id: str
    revision: int
    taken_at_ms: int
    reported_taken_at_ms: int
    clock_corrected: bool
    tester_serial: int | None
    status: str
    site: str
    port_id: str
    body: bytes
    received_at: float
    key_prefix: str = ""


@dataclass(frozen=True)
class Outcome:
    result: Result
    revision: int  # the revision now stored


def _store_sync(store: Store, r: NewReport) -> Outcome:
    digest = hashlib.sha256(r.body).hexdigest()
    with store._lock, store._db:
        row = store._db.execute(
            "SELECT revision FROM field_reports WHERE source=? AND report_id=?",
            (r.source, r.report_id)).fetchone()
        values = (r.revision, r.taken_at_ms, r.reported_taken_at_ms, int(r.clock_corrected),
                  r.tester_serial, r.status, r.site, r.port_id, digest, r.body, r.key_prefix)
        if row is None:
            store._db.execute(
                "INSERT INTO field_reports (revision, taken_at_ms, reported_taken_at_ms, "
                "clock_corrected, tester_serial, status, site, port_id, body_sha256, body, "
                "key_prefix, source, report_id, received_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*values, r.source, r.report_id, r.received_at, r.received_at))
            return Outcome("accepted", r.revision)
        stored = int(row[0])
        if r.revision > stored:
            store._db.execute(
                "UPDATE field_reports SET revision=?, taken_at_ms=?, reported_taken_at_ms=?, "
                "clock_corrected=?, tester_serial=?, status=?, site=?, port_id=?, "
                "body_sha256=?, body=?, key_prefix=?, body_pruned_at=NULL, updated_at=?, "
                "revisions_seen=revisions_seen+1 WHERE source=? AND report_id=?",
                (*values, r.received_at, r.source, r.report_id))
            return Outcome("replaced", r.revision)
        return Outcome("duplicate" if r.revision == stored else "ignored", stored)


async def store_report(store: Store, report: NewReport) -> Outcome:
    return await asyncio.to_thread(_store_sync, store, report)


async def prune_evidence(store: Store, now: float, retention_days: int) -> int:
    """Drop bodies not updated for retention_days. Summary rows stay. Returns bodies dropped."""
    cutoff = now - retention_days * 86400
    return await asyncio.to_thread(
        store._delete,
        "UPDATE field_reports SET body=NULL, body_pruned_at=? "
        "WHERE body IS NOT NULL AND updated_at < ?", (now, cutoff))
