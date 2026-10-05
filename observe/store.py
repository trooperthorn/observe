"""SQLite history: one row per poll, one row per state transition.

SQLite in WAL mode is plenty for a homelab: at 200 monitors on a 60 s
interval that is about 290k rows per day, and retention pruning keeps the
file bounded. All access goes through one connection guarded by a lock and
run in a worker thread, so the event loop never blocks on disk I/O.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .checks.base import CheckResult
from .ingest.schema import Batch, normalize_severity
from .state import Transition

if TYPE_CHECKING:
    from .plugins import LoadedPlugins

# Schema versioning uses a schema_version table. Databases created before it
# existed hold only the results and events tables; they are treated as version
# 1 once the baseline step has run, which changes nothing for them because every
# statement is guarded. Each later step is additive: it only creates objects,
# guarded with IF NOT EXISTS, so rerunning a step changes nothing. The layout
# is adapted from hostwatch (hostwatch/store.py).
BASELINE = (
    """CREATE TABLE IF NOT EXISTS results (
    monitor TEXT NOT NULL,
    ts REAL NOT NULL,
    result TEXT NOT NULL,
    value REAL,
    latency_ms REAL,
    message TEXT
)""",
    "CREATE INDEX IF NOT EXISTS results_monitor_ts ON results(monitor, ts)",
    """CREATE TABLE IF NOT EXISTS events (
    monitor TEXT NOT NULL,
    ts REAL NOT NULL,
    previous TEXT NOT NULL,
    current TEXT NOT NULL,
    message TEXT
)""",
    "CREATE INDEX IF NOT EXISTS events_ts ON events(ts)",
)

HOST_TABLES = (
    """CREATE TABLE IF NOT EXISTS hosts (
  host TEXT PRIMARY KEY, platform TEXT NOT NULL DEFAULT '', agent_version TEXT NOT NULL DEFAULT '',
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, boot_id TEXT, boot_ts REAL,
  heartbeat_ts REAL, clean_shutdown INTEGER, confirmed INTEGER NOT NULL DEFAULT 0, confirmed_at REAL
)""",
    """CREATE TABLE IF NOT EXISTS host_samples (
  ts REAL NOT NULL, host TEXT NOT NULL, source TEXT NOT NULL, metric TEXT NOT NULL,
  labels TEXT NOT NULL DEFAULT '{}', value REAL, unit TEXT NOT NULL DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS host_samples_lookup ON host_samples(host, source, metric, ts)",
    "CREATE INDEX IF NOT EXISTS host_samples_ts ON host_samples(ts)",
    """CREATE TABLE IF NOT EXISTS host_sources (
  host TEXT NOT NULL, source TEXT NOT NULL, available INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '', updated REAL NOT NULL, PRIMARY KEY (host, source)
)""",
    """CREATE TABLE IF NOT EXISTS host_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, ts REAL NOT NULL,
  kind TEXT NOT NULL, severity TEXT NOT NULL, source TEXT NOT NULL, title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}', dedup_key TEXT NOT NULL, boot_id TEXT,
  UNIQUE (host, dedup_key)
)""",
    "CREATE INDEX IF NOT EXISTS host_events_host_ts ON host_events(host, ts)",
    "CREATE INDEX IF NOT EXISTS host_events_ts ON host_events(ts)",
)

ACCESS_TABLES = (
    """CREATE TABLE IF NOT EXISTS ingest_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT, prefix TEXT NOT NULL UNIQUE, hash TEXT NOT NULL,
  host TEXT NOT NULL, created REAL NOT NULL, created_by TEXT NOT NULL DEFAULT '',
  revoked_at REAL, last_used REAL
)""",
    """CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0, locked_until REAL, created REAL NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS sessions (
  id_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), csrf_hash TEXT NOT NULL,
  created REAL NOT NULL, expires REAL NOT NULL, last_seen REAL NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0
)""",
    "CREATE INDEX IF NOT EXISTS sessions_expires ON sessions(expires)",
    """CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
  method TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '', status INTEGER NOT NULL DEFAULT 0,
  remote TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}'
)""",
    "CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)",
)

BATCH_TABLES = (
    """CREATE TABLE IF NOT EXISTS ingest_batches (
  host TEXT NOT NULL, batch_id TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY (host, batch_id)
)""",
    "CREATE INDEX IF NOT EXISTS ingest_batches_ts ON ingest_batches(ts)",
)

# One row per plugin holds the highest migration version applied for it. Plugin tables
# live beside the core's and are never dropped, so disabling a plugin leaves them as they are.
PLUGIN_TABLES = (
    """CREATE TABLE IF NOT EXISTS plugin_schema (
  plugin TEXT PRIMARY KEY, version INTEGER NOT NULL
)""",
)

# The infrastructure map: switches, their ports, wall jacks, the links between them, endpoints
# seen on ports, and the append-only port property history (docs/FIELD-DATA.md). Keys are the
# normalised forms made by observe/portkey.py. A link names its two ends as (kind, ref) pairs,
# stored in sorted order so an edge has one row whichever way it was reported. All steps are
# additive; no existing table changes.
INFRA_TABLES = (
    """CREATE TABLE IF NOT EXISTS infra_switches (
  switch_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', mgmt_addresses TEXT NOT NULL DEFAULT '[]',
  vendor TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT '', matched_monitor TEXT,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS infra_ports (
  switch_id TEXT NOT NULL REFERENCES infra_switches(switch_id), port_key TEXT NOT NULL,
  raw_port_id TEXT NOT NULL DEFAULT '', if_index INTEGER, unifi_index INTEGER,
  role TEXT NOT NULL DEFAULT 'unknown', first_seen REAL NOT NULL, last_seen REAL NOT NULL,
  PRIMARY KEY (switch_id, port_key)
)""",
    """CREATE TABLE IF NOT EXISTS infra_jacks (
  jack_key TEXT PRIMARY KEY, room TEXT NOT NULL DEFAULT '', site TEXT NOT NULL DEFAULT '',
  switch_id TEXT, port_key TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS infra_endpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref TEXT NOT NULL,
  mac TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, UNIQUE (kind, ref)
)""",
    """CREATE TABLE IF NOT EXISTS infra_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT, a_kind TEXT NOT NULL, a_ref TEXT NOT NULL,
  b_kind TEXT NOT NULL, b_ref TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, closed_at REAL,
  UNIQUE (a_kind, a_ref, b_kind, b_ref, source)
)""",
    """CREATE TABLE IF NOT EXISTS port_properties (
  id INTEGER PRIMARY KEY AUTOINCREMENT, switch_id TEXT NOT NULL, port_key TEXT NOT NULL,
  name TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL, report_id TEXT NOT NULL DEFAULT '', observed_at REAL NOT NULL,
  recorded_at REAL NOT NULL, recorded_by TEXT NOT NULL DEFAULT '', last_verified REAL NOT NULL,
  FOREIGN KEY (switch_id, port_key) REFERENCES infra_ports(switch_id, port_key)
)""",
    "CREATE INDEX IF NOT EXISTS port_properties_lookup ON port_properties(switch_id, port_key, name, id)",
)

# Admin decisions about inferred dependencies between monitors (docs/FIELD-DATA.md). The
# proposals themselves are computed from the links on each read; only the decision is stored.
MAP_DEPENDENCY_TABLES = (
    """CREATE TABLE IF NOT EXISTS infra_dependencies (
  child TEXT NOT NULL, parent TEXT NOT NULL, decision TEXT NOT NULL,
  decided_by TEXT NOT NULL, decided_at REAL NOT NULL, PRIMARY KEY (child, parent)
)""",
)

# An admin's acknowledgement of a field finding. Findings are computed on each read and never
# stored; an acknowledgement names the finding by kind and port and keeps the message it was
# given for, so a finding whose facts changed is shown as new again.
FINDING_ACK_TABLES = (
    """CREATE TABLE IF NOT EXISTS infra_finding_acks (
  kind TEXT NOT NULL, switch_id TEXT NOT NULL, port_key TEXT NOT NULL, message TEXT NOT NULL,
  acked_by TEXT NOT NULL, acked_at REAL NOT NULL, PRIMARY KEY (kind, switch_id, port_key)
)""",
)

# Stored as the reason when an agent says a source does not exist on the host.
ABSENT_REASON = "not present on this host"

# Agent clocks may run a little ahead. A timestamp further ahead of receive time
# than this is clamped to receive time, so it can not mask later readings or
# freeze boot state.
MAX_FUTURE_SKEW_S = 300.0

# Keys gain a scope: wpi (host ingest, the default for every existing key) or a marker that a
# plugin registers, such as wpf. For a plugin scope the host column holds the device label.
def _add_key_scope(db: sqlite3.Connection) -> None:
    # ALTER TABLE has no IF NOT EXISTS, so check first; every step must be safe to run again.
    columns = {row[1] for row in db.execute("PRAGMA table_info(ingest_keys)")}
    if "scope" not in columns:
        db.execute("ALTER TABLE ingest_keys ADD COLUMN scope TEXT NOT NULL DEFAULT 'wpi'")


KEY_SCOPE_TABLES = (_add_key_scope,)

# Host enrolment (observe/enrol.py): one row per host being added through the console. Only a
# digest of the single-use token is stored, and the keys are minted when it is redeemed.
ENROLMENT_TABLES = (
    """CREATE TABLE IF NOT EXISTS enrolments (
  host TEXT PRIMARY KEY, platform TEXT NOT NULL, agent INTEGER NOT NULL, control INTEGER NOT NULL,
  allowlist TEXT NOT NULL DEFAULT '{}', token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL,
  created_by TEXT NOT NULL DEFAULT '', expires_at REAL NOT NULL, fetched_at REAL,
  expiry_audited INTEGER NOT NULL DEFAULT 0, agent_prefix TEXT, control_prefix TEXT
)""",
)

MIGRATIONS: dict[int, tuple[str | Callable[[sqlite3.Connection], None], ...]] = {
    1: BASELINE,
    2: HOST_TABLES,
    3: ACCESS_TABLES,
    4: BATCH_TABLES,
    5: PLUGIN_TABLES,
    6: INFRA_TABLES,
    7: MAP_DEPENDENCY_TABLES,
    8: FINDING_ACK_TABLES,
    9: KEY_SCOPE_TABLES,
    10: ENROLMENT_TABLES,
}
SCHEMA_VERSION = max(MIGRATIONS)


class SchemaTooNewError(RuntimeError):
    """Raised when the database was written by a newer version of Observe."""


def migrate(db: sqlite3.Connection) -> None:
    """Bring an open database up to SCHEMA_VERSION, one transaction per step."""
    old = db.isolation_level
    db.isolation_level = None  # manual BEGIN and COMMIT so DDL is transactional
    try:
        db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        current = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        latest = max(MIGRATIONS)
        if current > latest:
            raise SchemaTooNewError(
                f"database schema version {current} is newer than this Observe "
                f"supports ({latest}); upgrade Observe or restore an older database")
        for version in range(current + 1, latest + 1):
            db.execute("BEGIN")
            try:
                for stmt in MIGRATIONS[version]:
                    if callable(stmt):
                        stmt(db)
                    else:
                        db.execute(stmt)
                db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
    finally:
        db.isolation_level = old


class PluginSchemaTooNewError(SchemaTooNewError):
    """Raised when a plugin's tables were written by a newer release of that plugin."""


def migrate_plugins(db: sqlite3.Connection,
                    plugins: Mapping[str, Sequence[Any]]) -> None:
    """Apply each listed plugin's migrations after the core's, one transaction per step.

    `plugins` maps a plugin name to its Migration objects (version, statements), numbered
    from 1. Every plugin is checked before any is changed, so a database that is too new
    for one plugin is refused whole. A plugin that is not listed is not touched at all.
    """
    old = db.isolation_level
    db.isolation_level = None
    try:
        db.execute(PLUGIN_TABLES[0])
        current: dict[str, int] = {}
        for name, migrations in plugins.items():
            row = db.execute("SELECT version FROM plugin_schema WHERE plugin=?",
                             (name,)).fetchone()
            current[name] = row[0] if row else 0
            if current[name] > len(migrations):
                raise PluginSchemaTooNewError(
                    f"plugin {name!r} database schema version {current[name]} is newer than "
                    f"this plugin release supports ({len(migrations)}); upgrade the plugin "
                    "or restore an older database")
        for name, migrations in plugins.items():
            for migration in migrations[current[name]:]:
                db.execute("BEGIN")
                try:
                    for stmt in migration.statements:
                        db.execute(stmt)
                    db.execute("INSERT OR REPLACE INTO plugin_schema (plugin, version) "
                               "VALUES (?, ?)", (name, migration.version))
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")
    finally:
        db.isolation_level = old


def content_key(batch: Batch) -> str:
    """Stable identity of a batch that carries no batch_id: a SHA-256 of its
    validated content. The prefix keeps it apart from any agent-chosen id."""
    digest = hashlib.sha256(batch.model_dump_json(exclude={"batch_id"}).encode("utf-8")).hexdigest()
    return f"content:{digest}"


class Store:
    def __init__(self, path: str, plugins: LoadedPlugins | None = None) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        try:
            migrate(self._db)
            migrate_plugins(self._db, {p.name: p.migrations
                                       for p in plugins.plugins} if plugins else {})
        except BaseException:
            self._db.close()
            raise
        self._lock = threading.Lock()

    def _exec(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchall()
            self._db.commit()
            return rows

    async def _run(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return await asyncio.to_thread(self._exec, sql, args)

    async def record(self, monitor: str, ts: float, res: CheckResult) -> None:
        await self._run(
            "INSERT INTO results VALUES (?,?,?,?,?,?)",
            (monitor, ts, res.result.value, res.value, res.latency_ms, res.message),
        )

    async def record_event(self, monitor: str, tr: Transition) -> None:
        await self._run(
            "INSERT INTO events VALUES (?,?,?,?,?)",
            (monitor, tr.at, tr.previous.value, tr.current.value, tr.message),
        )

    async def history(self, monitor: str, hours: float) -> list[dict[str, Any]]:
        since = time.time() - hours * 3600
        rows = await self._run(
            "SELECT ts, result, value, latency_ms, message FROM results "
            "WHERE monitor=? AND ts>=? ORDER BY ts LIMIT 20000",
            (monitor, since),
        )
        return [dict(zip(("ts", "result", "value", "latency_ms", "message"), r)) for r in rows]

    async def events(self, limit: int = 200, monitor: str | None = None) -> list[dict[str, Any]]:
        if monitor:
            rows = await self._run(
                "SELECT monitor, ts, previous, current, message FROM events "
                "WHERE monitor=? ORDER BY ts DESC LIMIT ?", (monitor, limit))
        else:
            rows = await self._run(
                "SELECT monitor, ts, previous, current, message FROM events "
                "ORDER BY ts DESC LIMIT ?", (limit,))
        keys = ("monitor", "ts", "previous", "current", "message")
        return [dict(zip(keys, r)) for r in rows]

    async def hourly_series(self, monitor: str, days: float) -> list[tuple[float, float]]:
        """Hourly means of non-null values: [(bucket midpoint ts, mean)]."""
        since = time.time() - days * 86400
        rows = await self._run(
            "SELECT CAST(ts / 3600 AS INTEGER) AS h, AVG(value) FROM results "
            "WHERE monitor=? AND ts>=? AND value IS NOT NULL AND result != 'fail' "
            "GROUP BY h ORDER BY h",
            (monitor, since),
        )
        return [(h * 3600 + 1800.0, float(v)) for h, v in rows]

    async def availability(self, monitor: str, hours: float) -> float | None:
        """Percent of polls in the window that were not FAIL."""
        since = time.time() - hours * 3600
        rows = await self._run(
            "SELECT COUNT(*), SUM(result != 'fail') FROM results WHERE monitor=? AND ts>=?",
            (monitor, since),
        )
        total, ok = rows[0]
        return None if not total else round(ok / total * 100, 3)

    def _ingest_sync(self, batch: Batch, boots: dict[int, tuple[str, int | None]],
                     now: float) -> tuple[int, int, bool]:
        # Adapted from hostwatch's Store.ingest_batch (hostwatch, same owner): one
        # transaction for the batch id, host row, sources, samples and events.
        with self._lock, self._db:
            # A batch without batch_id is identified by a hash of its content, so a
            # resend of the same batch is not stored twice.
            def clamp(ts: float) -> float:
                return now if ts > now + MAX_FUTURE_SKEW_S else ts

            sent = clamp(batch.sent_at)
            batch_key = batch.batch_id if batch.batch_id is not None else content_key(batch)
            cur = self._db.execute(
                "INSERT INTO ingest_batches (host, batch_id, ts) VALUES (?,?,?) "
                "ON CONFLICT(host, batch_id) DO NOTHING", (batch.host, batch_key, now))
            if cur.rowcount == 0:
                return 0, 0, True
            self._db.execute(
                "INSERT INTO hosts (host, platform, agent_version, first_seen, last_seen, heartbeat_ts) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET "
                "last_seen=MAX(hosts.last_seen, excluded.last_seen), "
                "platform=CASE WHEN excluded.heartbeat_ts >= COALESCE(hosts.heartbeat_ts, 0) "
                "THEN excluded.platform ELSE hosts.platform END, "
                "agent_version=CASE WHEN excluded.heartbeat_ts >= COALESCE(hosts.heartbeat_ts, 0) "
                "THEN excluded.agent_version ELSE hosts.agent_version END, "
                "heartbeat_ts=MAX(COALESCE(hosts.heartbeat_ts, 0), excluded.heartbeat_ts)",
                (batch.host, batch.platform, batch.agent_version, now, now, sent))
            self._db.executemany(
                "INSERT INTO host_samples (ts, host, source, metric, labels, value, unit) "
                "VALUES (?,?,?,?,?,?,?)",
                [(clamp(s.ts), batch.host, s.source, s.metric, json.dumps(s.labels, sort_keys=True),
                  s.value, s.unit) for s in batch.samples])
            self._db.executemany(
                "INSERT INTO host_sources (host, source, available, reason, updated) VALUES (?,?,?,?,?) "
                "ON CONFLICT(host, source) DO UPDATE SET available=excluded.available, "
                "reason=excluded.reason, updated=excluded.updated "
                "WHERE excluded.updated >= host_sources.updated",
                [(batch.host, st.source, int(st.available and st.present),
                  st.reason or ("" if st.present else ABSENT_REASON), sent)
                 for st in batch.sources])
            stored = 0
            for i, ev in enumerate(batch.events):
                ev_ts = clamp(ev.ts)
                detail = dict(ev.detail)
                if i in boots:
                    detail["classification"] = boots[i][0]
                severity = normalize_severity(ev.severity)
                if severity != ev.severity:
                    detail["severity_raw"] = ev.severity
                cur = self._db.execute(
                    "INSERT INTO host_events (host, ts, kind, severity, source, title, detail, "
                    "dedup_key, boot_id) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(host, dedup_key) DO NOTHING",
                    (batch.host, ev_ts, ev.kind, severity, ev.source, ev.title,
                     json.dumps(detail, sort_keys=True), ev.dedup_key, ev.boot_id))
                if cur.rowcount == 0:
                    continue
                stored += 1
                if i in boots:
                    clean = boots[i][1]
                    self._db.execute(
                        "UPDATE hosts SET boot_id=?, boot_ts=?, clean_shutdown=? "
                        "WHERE host=? AND (boot_ts IS NULL OR boot_ts <= ?)",
                        (ev.boot_id, ev_ts, clean, batch.host, ev_ts))
            return len(batch.samples), stored, False

    async def ingest_batch(self, batch: Batch, boots: dict[int, tuple[str, int | None]],
                           now: float | None = None) -> tuple[int, int, bool]:
        """Store a pushed batch. boots maps an event index to (classification,
        clean_shutdown flag). Returns (samples stored, events stored, duplicate).
        A batch_id already recorded for the host is acknowledged and nothing is
        stored again."""
        return await asyncio.to_thread(
            self._ingest_sync, batch, boots, time.time() if now is None else now)

    def _latest_host_sync(self, host: str, since: float) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT last_seen, platform, agent_version, clean_shutdown, boot_ts FROM hosts WHERE host=?",
                (host,)).fetchone()
            if row is None:
                return None
            # Newest row per series; rowid breaks a tie between equal timestamps, so the
            # row inserted last wins instead of an arbitrary one.
            samples = self._db.execute(
                "SELECT source, metric, labels, value, unit, ts FROM ("
                "SELECT source, metric, labels, value, unit, ts, ROW_NUMBER() OVER ("
                "PARTITION BY source, metric, labels ORDER BY ts DESC, rowid DESC) AS n "
                "FROM host_samples WHERE host=? AND ts>=?) WHERE n=1", (host, since)).fetchall()
            sources = self._db.execute(
                "SELECT source, available, reason FROM host_sources WHERE host=?",
                (host,)).fetchall()
        return {
            "last_seen": row[0], "platform": row[1], "agent_version": row[2],
            "clean_shutdown": row[3], "boot_ts": row[4],
            "samples": [{"source": r[0], "metric": r[1], "labels": json.loads(r[2]),
                         "value": r[3], "unit": r[4], "ts": r[5]} for r in samples],
            "sources": {r[0]: {"available": bool(r[1]), "reason": r[2]} for r in sources},
        }

    async def latest_host(self, host: str, since: float = 0.0) -> dict[str, Any] | None:
        """The newest reading per source, metric and label set for a pushed host
        (samples older than `since` are left out), its source availability, and
        when a batch last arrived. None when the host has never pushed."""
        return await asyncio.to_thread(self._latest_host_sync, host, since)

    def _host_rows_sync(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT host, platform, agent_version, first_seen, last_seen, boot_id, boot_ts, "
                "clean_shutdown, confirmed FROM hosts ORDER BY host").fetchall()
        keys = ("host", "platform", "agent_version", "first_seen", "last_seen", "boot_id",
                "boot_ts", "clean_shutdown", "confirmed")
        return [dict(zip(keys, r)) for r in rows]

    async def host_rows(self) -> list[dict[str, Any]]:
        """One row per host that has ever pushed, ordered by name."""
        return await asyncio.to_thread(self._host_rows_sync)

    def _host_sources_sync(self, host: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT source, available, reason, updated FROM host_sources WHERE host=?",
                (host,)).fetchall()
        return {r[0]: {"available": bool(r[1]), "reason": r[2], "updated": r[3]} for r in rows}

    async def host_sources(self, host: str) -> dict[str, dict[str, Any]]:
        """Source status for a host, with when each source was last reported."""
        return await asyncio.to_thread(self._host_sources_sync, host)

    def _host_events_sync(self, host: str, since: float, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, kind, severity, source, title, detail, boot_id FROM host_events "
                "WHERE host=? AND ts>=? ORDER BY ts DESC, id DESC LIMIT ?",
                (host, since, limit)).fetchall()
        return [{"ts": r[0], "kind": r[1], "severity": r[2], "source": r[3], "title": r[4],
                 "detail": json.loads(r[5]), "boot_id": r[6]} for r in rows]

    async def host_events(self, host: str, since: float = 0.0,
                          limit: int = 50) -> list[dict[str, Any]]:
        """Newest pushed events for a host (boot classifications, journal matches)."""
        return await asyncio.to_thread(self._host_events_sync, host, since, limit)

    def _audit_sync(self, row: tuple[Any, ...]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO audit (ts, actor, kind, method, path, status, remote, detail) "
                "VALUES (?,?,?,?,?,?,?,?)", row)

    async def write_audit(self, kind: str, actor: str = "", method: str = "", path: str = "",
                          status: int = 0, remote: str = "",
                          detail: dict[str, Any] | None = None, ts: float | None = None) -> None:
        """Append one audit row. Callers must never put secrets in detail."""
        row = (time.time() if ts is None else ts, actor, kind, method, path, status, remote,
               json.dumps(detail or {}, sort_keys=True))
        await asyncio.to_thread(self._audit_sync, row)

    def _delete(self, sql: str, args: tuple[Any, ...]) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.rowcount

    async def prune(self, retention_days: int, audit_retention_days: int = 365) -> int:
        """Drop poll rows and host samples past retention. Transitions and host
        events are kept for at least a year because they are small and are the
        record you want in a review. The audit log has its own retention.
        Expired sessions are removed. Returns the number of result rows removed."""
        now = time.time()
        cutoff = now - retention_days * 86400
        keep = now - max(retention_days, 365) * 86400
        removed = await asyncio.to_thread(self._delete, "DELETE FROM results WHERE ts < ?", (cutoff,))
        await asyncio.to_thread(self._delete, "DELETE FROM host_samples WHERE ts < ?", (cutoff,))
        await asyncio.to_thread(self._delete, "DELETE FROM ingest_batches WHERE ts < ?", (cutoff,))
        await asyncio.to_thread(self._delete, "DELETE FROM events WHERE ts < ?", (keep,))
        await asyncio.to_thread(self._delete, "DELETE FROM host_events WHERE ts < ?", (keep,))
        await asyncio.to_thread(
            self._delete, "DELETE FROM audit WHERE ts < ?", (now - audit_retention_days * 86400,))
        await asyncio.to_thread(self._delete, "DELETE FROM sessions WHERE expires < ?", (now,))
        return removed

    def close(self) -> None:
        with self._lock:
            self._db.close()
