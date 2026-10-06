"""Store: the monitor history, host data and access tables, behind the Storage interface.

Every method here builds a unit of work and hands it to `Storage` (observe/storage). Nothing in
this module opens a database, takes a lock or names a backend: the writer thread, the read pool
and the change counters belong to the storage layer (docs/DATA-API-DESIGN.md sections 1.2, 2.9
to 2.11 and 12).
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from .checks.base import CheckResult
from .ingest.schema import Batch, normalize_severity
from .state import Transition
from .storage import Conn, Storage, open_storage, rollups, series

if TYPE_CHECKING:
    from .plugins import LoadedPlugins

# Stored as the reason when an agent says a source does not exist on the host.
ABSENT_REASON = "not present on this host"

# Agent clocks may run a little ahead. A timestamp further ahead of receive time
# than this is clamped to receive time, so it can not mask later readings or
# freeze boot state.
MAX_FUTURE_SKEW_S = 300.0


def content_key(batch: Batch) -> str:
    """Stable identity of a batch that carries no batch_id: a SHA-256 of its
    validated content. The prefix keeps it apart from any agent-chosen id."""
    digest = hashlib.sha256(batch.model_dump_json(exclude={"batch_id"}).encode("utf-8")).hexdigest()
    return f"content:{digest}"


# The producer that names a host's platform and agent version. Observe's own pull checks
# (Home Assistant host mode and SNMP) write batches for a host that an installed agent, or
# ha_Int_soc, also pushes for. The ranking is declared here: a pushed agent outranks the Home
# Assistant pull, which outranks SNMP. A lower rank never replaces a higher one, and within one
# rank the newest heartbeat wins. This keeps the host page stable however the writers interleave.
PULL_PRODUCER_RANK = {"observe-snmp-host": 0, "observe-ha-host": 1}
PUSHED_PRODUCER_RANK = 2


def _rank_sql(column: str) -> str:
    whens = " ".join(f"WHEN '{name}' THEN {rank}" for name, rank in PULL_PRODUCER_RANK.items())
    return f"CASE {column} {whens} ELSE {PUSHED_PRODUCER_RANK} END"


_TAKES_OVER = (f"({_rank_sql('excluded.agent_version')} > {_rank_sql('hosts.agent_version')} OR "
               f"({_rank_sql('excluded.agent_version')} = {_rank_sql('hosts.agent_version')} AND "
               "excluded.heartbeat_ts >= COALESCE(hosts.heartbeat_ts, 0)))")



# The newest point of each series of a host, with its source (scope), metric, labels and unit.
_LATEST_SQL = ("SELECT sc.name, s.metric, s.attrs, l.value, s.unit, l.ts FROM latest l "
               "JOIN series s ON s.id = l.series_id JOIN scopes sc ON sc.id = s.scope_id ")


class Store:
    def __init__(self, path: str, plugins: LoadedPlugins | None = None, *,
                 backend: str = "sqlite", dsn: str | None = None, password: str | None = None,
                 timescale: str = "auto") -> None:
        self.storage: Storage = open_storage(
            path, {p.name: p.migrations for p in plugins.plugins} if plugins else None,
            backend=backend, dsn=dsn, password=password, timescale=timescale)

    @classmethod
    def from_config(cls, config: Any, plugins: LoadedPlugins | None = None) -> "Store":
        """Open the database the config names: SQLite at server.db_path, or PostgreSQL."""
        s = config.storage
        return cls(config.server.db_path, plugins, backend=s.backend,
                   dsn=s.dsn.get_secret_value() if s.dsn else None, password=s.password(),
                   timescale=s.timescaledb)

    async def fetch(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """One read statement on the read pool."""
        return await self.storage.fetchall(sql, args)

    async def execute(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """One write statement in its own unit; returns the rows it produced, if any."""
        return await self.storage.execute(sql, args)

    async def record(self, monitor: str, ts: float, res: CheckResult) -> None:
        row = (monitor, ts, res.result.value, res.value, res.latency_ms, res.message)
        await self.storage.write(
            lambda db: db.execute("INSERT INTO results VALUES (?,?,?,?,?,?)", row),
            touches=("monitors",))

    async def record_event(self, monitor: str, tr: Transition) -> None:
        row = (monitor, tr.at, tr.previous.value, tr.current.value, tr.message)
        await self.storage.write(
            lambda db: db.execute("INSERT INTO events VALUES (?,?,?,?,?)", row),
            touches=("events",))

    async def history(self, monitor: str, hours: float,
                      limit: int = 20000) -> list[dict[str, Any]]:
        """Rows in the window, oldest first. At the row limit the newest rows are kept."""
        since = time.time() - hours * 3600
        rows = await self.fetch(
            "SELECT ts, result, value, latency_ms, message FROM results "
            "WHERE monitor=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (monitor, since, limit),
        )
        rows.reverse()
        return [dict(zip(("ts", "result", "value", "latency_ms", "message"), r)) for r in rows]

    async def events(self, limit: int = 200, monitor: str | None = None) -> list[dict[str, Any]]:
        if monitor:
            rows = await self.fetch(
                "SELECT monitor, ts, previous, current, message FROM events "
                "WHERE monitor=? ORDER BY ts DESC LIMIT ?", (monitor, limit))
        else:
            rows = await self.fetch(
                "SELECT monitor, ts, previous, current, message FROM events "
                "ORDER BY ts DESC LIMIT ?", (limit,))
        keys = ("monitor", "ts", "previous", "current", "message")
        return [dict(zip(keys, r)) for r in rows]

    async def hourly_series(self, monitor: str, days: float) -> list[tuple[float, float]]:
        """Hourly means of non-null values: [(bucket midpoint ts, mean)]."""
        since = time.time() - days * 86400
        rows = await self.fetch(
            "SELECT CAST(FLOOR(ts / 3600) AS BIGINT) AS h, CAST(AVG(value) AS DOUBLE PRECISION) "
            "FROM results WHERE monitor=? AND ts>=? AND value IS NOT NULL AND result != 'fail' "
            "GROUP BY CAST(FLOOR(ts / 3600) AS BIGINT) ORDER BY h",
            (monitor, since),
        )
        return [(int(h) * 3600 + 1800.0, float(v)) for h, v in rows]

    async def availability(self, monitor: str, hours: float) -> float | None:
        """Percent of polls in the window that were not FAIL."""
        since = time.time() - hours * 3600
        rows = await self.fetch(
            "SELECT CAST(COUNT(*) AS BIGINT), "
            "CAST(COALESCE(SUM(CASE WHEN result != 'fail' THEN 1 ELSE 0 END), 0) AS BIGINT) "
            "FROM results WHERE monitor=? AND ts>=?",
            (monitor, since),
        )
        total, ok = rows[0]
        return None if not total else round(int(ok) / int(total) * 100, 3)

    def _ingest_unit(self, db: Conn, batch: Batch, boots: dict[int, tuple[str, int | None]],
                     now: float) -> tuple[int, int, bool]:
        # Adapted from hostwatch's Store.ingest_batch (hostwatch, same owner): one
        # transaction for the batch id, host row, sources, samples and events.
        # A batch without batch_id is identified by a hash of its content, so a
        # resend of the same batch is not stored twice.
        def clamp(ts: float) -> float:
            return now if ts > now + MAX_FUTURE_SKEW_S else ts

        sent = clamp(batch.sent_at)
        batch_key = batch.batch_id if batch.batch_id is not None else content_key(batch)
        cur = db.execute(
            "INSERT INTO ingest_batches (host, batch_id, ts) VALUES (?,?,?) "
            "ON CONFLICT(host, batch_id) DO NOTHING", (batch.host, batch_key, now))
        if cur.rowcount == 0:
            return 0, 0, True
        # A resend changes nothing, so only a stored batch bumps the change counters.
        self.storage.write_sync(lambda _db: None, touches=("metrics", "hosts", "events"))
        db.execute(
            "INSERT INTO hosts (host, platform, agent_version, first_seen, last_seen, heartbeat_ts) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET "
            "last_seen=MAX(hosts.last_seen, excluded.last_seen), "
            f"platform=CASE WHEN {_TAKES_OVER} THEN excluded.platform "
            "ELSE hosts.platform END, "
            f"agent_version=CASE WHEN {_TAKES_OVER} THEN excluded.agent_version "
            "ELSE hosts.agent_version END, "
            "heartbeat_ts=MAX(COALESCE(hosts.heartbeat_ts, 0), excluded.heartbeat_ts)",
            (batch.host, batch.platform, batch.agent_version, now, now, sent))
        # A pushed host is a resource of kind host; each source is the scope its points came
        # from and its labels are the point attributes (docs/DATA-API-DESIGN.md section 3.2).
        recorded = series.record_points(
            db, kind="host", name=batch.host, now=now, rollups=self.storage.incremental_rollups,
            points=[series.Point(s.source, s.metric, s.unit, series.canonical(s.labels),
                                 series.to_ms(clamp(s.ts)), s.value) for s in batch.samples])
        db.executemany(
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
            cur = db.execute(
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
                db.execute(
                    "UPDATE hosts SET boot_id=?, boot_ts=?, clean_shutdown=? "
                    "WHERE host=? AND (boot_ts IS NULL OR boot_ts <= ?)",
                    (ev.boot_id, ev_ts, clean, batch.host, ev_ts))
        return len(batch.samples) - recorded.dropped, stored, False

    async def ingest_batch(self, batch: Batch, boots: dict[int, tuple[str, int | None]],
                           now: float | None = None) -> tuple[int, int, bool]:
        """Store a pushed batch. boots maps an event index to (classification,
        clean_shutdown flag). Returns (samples stored, events stored, duplicate).
        A batch_id already recorded for the host is acknowledged and nothing is
        stored again."""
        at = time.time() if now is None else now
        return await self.storage.write(lambda db: self._ingest_unit(db, batch, boots, at))

    @staticmethod
    def _latest_host_unit(db: Conn, host: str, since: float, newest_fallback: bool,
                          series_wanted: tuple[tuple[str, str], ...]) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT last_seen, platform, agent_version, clean_shutdown, boot_ts FROM hosts WHERE host=?",
            (host,)).fetchone()
        if row is None:
            return None
        since_ms = series.to_ms(since)
        rid = series.find_resource(db, "host", host)
        samples = [] if rid is None else db.execute(
            _LATEST_SQL + "WHERE s.resource_id=? AND l.ts>=?", (rid, since_ms)).fetchall()
        if rid is not None and not samples and newest_fallback:
            # Nothing inside the window: return the single newest row so a host that has gone
            # quiet still reads as stale, not empty.
            samples = db.execute(_LATEST_SQL + "WHERE s.resource_id=? ORDER BY l.ts DESC LIMIT 1",
                                 (rid,)).fetchall()
        if series_wanted and rid is not None:
            # A named series with nothing inside the window is silent, not absent: read its own
            # latest row so it still grades as stale.
            have = {(r[0], r[1]) for r in samples}
            for src, metric in series_wanted:
                if (src, metric) in have:
                    continue
                samples = list(samples) + db.execute(
                    _LATEST_SQL + "WHERE s.resource_id=? AND sc.name=? AND s.metric=? AND l.ts<?",
                    (rid, src, metric, since_ms)).fetchall()
        sources = db.execute(
            "SELECT source, available, reason FROM host_sources WHERE host=?",
            (host,)).fetchall()
        return {
            "last_seen": row[0], "platform": row[1], "agent_version": row[2],
            "clean_shutdown": row[3], "boot_ts": row[4],
            "samples": [{"source": r[0], "metric": r[1], "labels": json.loads(r[2]),
                         "value": r[3], "unit": r[4], "ts": r[5] / 1000.0} for r in samples],
            "sources": {r[0]: {"available": bool(r[1]), "reason": r[2]} for r in sources},
        }

    async def latest_host(self, host: str, since: float = 0.0, *, window: float | None = None,
                          now: float | None = None,
                          series: tuple[tuple[str, str], ...] = ()) -> dict[str, Any] | None:
        """The newest reading per source, metric and label set for a pushed host
        (samples older than `since` are left out), its source availability, and
        when a batch last arrived. None when the host has never pushed. With `window`
        only samples from the last `window` seconds before `now` are read, so the cost
        does not grow with retention; a series silent for longer is absent, except that when
        nothing at all is in the window the single newest sample is returned. Each
        (source, metric) in `series` that has nothing in the window is still returned with its
        own newest older reading, so a silent configured component is graded stale."""
        if window is not None:
            since = max(since, (time.time() if now is None else now) - window)
        fallback = window is not None
        want = tuple(series)
        return await self.storage.read(
            lambda db: self._latest_host_unit(db, host, since, fallback, want))

    async def host_rows(self) -> list[dict[str, Any]]:
        """One row per host that has ever pushed, ordered by name."""
        rows = await self.fetch(
            "SELECT host, platform, agent_version, first_seen, last_seen, boot_id, boot_ts, "
            "clean_shutdown, confirmed FROM hosts ORDER BY host")
        keys = ("host", "platform", "agent_version", "first_seen", "last_seen", "boot_id",
                "boot_ts", "clean_shutdown", "confirmed")
        return [dict(zip(keys, r)) for r in rows]

    async def host_sources(self, host: str) -> dict[str, dict[str, Any]]:
        """Source status for a host, with when each source was last reported."""
        rows = await self.fetch(
            "SELECT source, available, reason, updated FROM host_sources WHERE host=?",
            (host,))
        return {r[0]: {"available": bool(r[1]), "reason": r[2], "updated": r[3]} for r in rows}

    async def host_events(self, host: str, since: float = 0.0,
                          limit: int = 50) -> list[dict[str, Any]]:
        """Newest pushed events for a host (boot classifications, journal matches)."""
        rows = await self.fetch(
            "SELECT ts, kind, severity, source, title, detail, boot_id FROM host_events "
            "WHERE host=? AND ts>=? ORDER BY ts DESC, id DESC LIMIT ?",
            (host, since, limit))
        return [{"ts": r[0], "kind": r[1], "severity": r[2], "source": r[3], "title": r[4],
                 "detail": json.loads(r[5]), "boot_id": r[6]} for r in rows]

    async def write_audit(self, kind: str, actor: str = "", method: str = "", path: str = "",
                          status: int = 0, remote: str = "",
                          detail: dict[str, Any] | None = None, ts: float | None = None) -> None:
        """Append one audit row. Callers must never put secrets in detail."""
        row = (time.time() if ts is None else ts, actor, kind, method, path, status, remote,
               json.dumps(detail or {}, sort_keys=True))
        await self.storage.write(
            lambda db: db.execute(
                "INSERT INTO audit (ts, actor, kind, method, path, status, remote, detail) "
                "VALUES (?,?,?,?,?,?,?,?)", row),
            touches=("audit",))

    async def note_maintenance(self, rows: int, error: str = "") -> None:
        """Record the outcome of a compaction pass for the admin retention page."""
        now = time.time()
        await self.storage.write(lambda db: rollups.note_maintenance(db, now, rows, error))

    async def prune(self, retention_days: int, audit_retention_days: int = 365) -> int:
        """Drop poll rows and host samples past retention. Transitions and host
        events are kept for at least a year because they are small and are the
        record you want in a review. The audit log has its own retention.
        Expired sessions are removed. Returns the number of result rows removed."""
        return await self.storage.apply_retention(
            now=time.time(), retention_days=retention_days,
            audit_retention_days=audit_retention_days)

    def close(self) -> None:
        self.storage.close()
