"""The field report store: one row per report, the raw body kept as evidence.

A report is keyed by `(source, report_id)`. The source is the device label the upload key is
bound to, so a key for one phone can never replace or hide a report of another phone, even
with a guessed report id. Port properties and links are derived from these rows (derive.py),
so the body is the evidence they can be rebuilt from.

Revisions decide what an upload does: a higher revision replaces the stored report, an equal
revision is a duplicate and changes nothing, and a lower revision is ignored. The decision and
the write happen in one transaction on the storage writer, so two uploads of the same report
cannot both win. A new or replaced report is derived in that same transaction, inside a
SAVEPOINT: a derivation that fails is rolled back to the savepoint and the evidence is kept
with `derive_status` failed, and the map tables are updated before the single commit
(`store_and_derive`).

Retention applies to the body only. After `evidence_retention_days` without an update the body
is dropped and the summary row stays, so a late replay of an old report is still recognised as
a duplicate instead of being stored again.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Literal

from observe.infra import InfraTx
from observe.map_tables import rebuild_and_touch
from observe.plugins import Migration
from observe.storage import Conn, savepoint
from observe.store import Store

from .derive import (Derived, Footprint, derive_report_tx, footprint, recorded_by_for,
                     replay_siblings_tx, retract_rows)
from .schema import Report

log = logging.getLogger(__name__)

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
    # `ok` once the report is derived, `pending` between storing and deriving, `failed` when the
    # derivation raised. Existing rows were derived by the version that stored them.
    Migration(3, ("ALTER TABLE field_reports ADD COLUMN derive_status TEXT NOT NULL "
                  "DEFAULT 'ok'",)),
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
    retracted: Footprint | None = None  # what a replaced revision's retraction removed


def _store(db: Conn, r: NewReport) -> Outcome:
    digest = hashlib.sha256(r.body).hexdigest()
    row = db.execute(
        "SELECT revision, key_prefix, body FROM field_reports WHERE source=? AND report_id=?",
        (r.source, r.report_id)).fetchone()
    values = (r.revision, r.taken_at_ms, r.reported_taken_at_ms, int(r.clock_corrected),
              r.tester_serial, r.status, r.site, r.port_id, digest, r.body, r.key_prefix)
    if row is None:
        db.execute(
            "INSERT INTO field_reports (revision, taken_at_ms, reported_taken_at_ms, "
            "clock_corrected, tester_serial, status, site, port_id, body_sha256, body, "
            "key_prefix, source, report_id, received_at, updated_at, derive_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
            (*values, r.source, r.report_id, r.received_at, r.received_at))
        return Outcome("accepted", r.revision)
    stored = int(row[0])
    if r.revision > stored:
        # The earlier revision's derived rows go in the same transaction as its replacement,
        # so live state never mixes two revisions and equals what a rebuild would produce.
        fp = footprint(row[2])
        retract_rows(db, r.report_id, recorded_by_for(row[1], r.source), fp)
        db.execute(
            "UPDATE field_reports SET revision=?, taken_at_ms=?, reported_taken_at_ms=?, "
            "clock_corrected=?, tester_serial=?, status=?, site=?, port_id=?, "
            "body_sha256=?, body=?, key_prefix=?, body_pruned_at=NULL, updated_at=?, "
            "derive_status='pending', "
            "revisions_seen=revisions_seen+1 WHERE source=? AND report_id=?",
            (*values, r.received_at, r.source, r.report_id))
        return Outcome("replaced", r.revision, fp)
    return Outcome("duplicate" if r.revision == stored else "ignored", stored)


def set_derive_status(db: Conn, source: str, report_id: str, revision: int,
                      status: str) -> None:
    """Record how deriving a stored revision went, on an open unit; a newer revision is
    never touched."""
    db.execute(
        "UPDATE field_reports SET derive_status=? WHERE source=? AND report_id=? "
        "AND revision=?", (status, source, report_id, revision))


async def mark_derive_status(store: Store, source: str, report_id: str, revision: int,
                             status: str) -> None:
    """Record how deriving a stored revision went; a newer revision is never touched."""
    await store.storage.write(lambda db: set_derive_status(db, source, report_id, revision,
                                                           status))


@dataclass(frozen=True)
class Ingested:
    outcome: Outcome
    derived: Derived | None = None  # None for a duplicate, an ignored report or a failed derivation
    derive_error: str = ""  # the exception class when the derivation failed, never its message


async def store_and_derive(store: Store, new: NewReport, report: Report, *, now: float) -> Ingested:
    """One upload in one transaction: store the report, derive it into the map and mark it.

    The derivation runs inside a SAVEPOINT. When it raises, its rows are rolled back, the report
    stays stored with `derive_status` failed (an admin retry or rebuild derives it again), and
    the transaction still commits, so the evidence is never lost. A derivation that works updates
    the map tables in the same unit, so one upload is one commit."""
    taken_ms = new.taken_at_ms

    def unit(db: Conn) -> Ingested:
        outcome = _store(db, new)
        if outcome.result not in ("accepted", "replaced"):
            return Ingested(outcome)
        try:
            with savepoint(db, "derive"):
                derived: Derived | None = None
                if outcome.retracted is not None:
                    derived = replay_siblings_tx(db, outcome.retracted,
                                                 (new.source, new.report_id))
                if derived is None:
                    derived = derive_report_tx(
                        InfraTx(db), report, key_prefix=new.key_prefix, device=new.source,
                        taken_ms=taken_ms, now=now)
                set_derive_status(db, new.source, new.report_id, outcome.revision, "ok")
        except Exception as err:  # the evidence is stored; only the derivation is lost
            log.exception("derivation failed for field report %s", new.report_id)
            set_derive_status(db, new.source, new.report_id, outcome.revision, "failed")
            return Ingested(outcome, None, type(err).__name__)
        rebuild_and_touch(db, store.storage, now, store.map_stale_days)
        return Ingested(outcome, derived)
    return await store.storage.write(unit)  # type: ignore[no-any-return]


async def prune_evidence(store: Store, now: float, retention_days: int) -> int:
    """Drop bodies not updated for retention_days. Summary rows stay. Returns bodies dropped."""
    cutoff = now - retention_days * 86400
    return await store.storage.write(lambda db: db.execute(
        "UPDATE field_reports SET body=NULL, body_pruned_at=? "
        "WHERE body IS NOT NULL AND updated_at < ?", (now, cutoff)).rowcount)
