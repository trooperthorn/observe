"""Store: the monitor history, host data and access tables, behind the Storage interface.

Every method here builds a unit of work and hands it to `Storage` (observe/storage). Nothing in
this module opens a database, takes a lock or names a backend: the writer thread, the read pool
and the change counters belong to the storage layer (docs/DATA-API-DESIGN.md sections 1.2, 2.9
to 2.11 and 12).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .checks.base import CheckResult, Result
from .ingest.schema import Batch, normalize_severity
from .state import Transition
from .storage import Conn, Storage, compaction, open_storage, rollups, series

if TYPE_CHECKING:
    from .plugins import LoadedPlugins

# Stored as the reason when an agent says a source does not exist on the host.
ABSENT_REASON = "not present on this host"

# Agent clocks may run a little ahead. A timestamp further ahead of receive time
# than this is clamped to receive time, so it can not mask later readings, freeze the latest
# value of a series or freeze boot state. The allowance is small because the latest row only
# moves forward in time: a point stamped further ahead would hold it for that long.
MAX_FUTURE_SKEW_S = 5.0


@dataclass
class IngestOutcome:
    """Why points of a batch were not stored, filled in by Store.ingest_batch for the caller
    that has to say so to the sender."""

    capped: int = 0  # points refused by the series cardinality guard
    late: int = 0  # points older than the raw retention boundary


class IdempotencyConflict(Exception):
    """An idempotency record exists for the batch id but was made from a different body."""


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
# What a request that names no os.type or service.version is given (observe/otlp/normalize.py).
# Such a request carries no claim about the platform or agent version, so it never replaces the
# value the host already has; only a first insert stores the placeholder.
UNNAMED_PLATFORM = "unknown"
UNNAMED_VERSION = "otlp"


def _rank_sql(column: str) -> str:
    whens = " ".join(f"WHEN '{name}' THEN {rank}" for name, rank in PULL_PRODUCER_RANK.items())
    return f"CASE {column} {whens} ELSE {PUSHED_PRODUCER_RANK} END"


_TAKES_OVER = (f"({_rank_sql('excluded.agent_version')} > {_rank_sql('hosts.agent_version')} OR "
               f"({_rank_sql('excluded.agent_version')} = {_rank_sql('hosts.agent_version')} AND "
               "excluded.heartbeat_ts >= COALESCE(hosts.heartbeat_ts, 0)))")



# The newest point of each series of a host, with its source (scope), metric, labels and unit.
_LATEST_SQL = ("SELECT sc.name, s.metric, s.attrs, l.value, s.unit, l.ts, s.resource_id "
               "FROM latest l "
               "JOIN series s ON s.id = l.series_id JOIN scopes sc ON sc.id = s.scope_id ")


# A monitor is a resource of kind "monitor"; each poll writes these series under one scope. State
# is read back from the summary views and the latest table, never from the raw poll rows.
MONITOR_SCOPE = "observe-monitor"
RESULT_CODE = {Result.OK: 0.0, Result.WARN: 1.0, Result.FAIL: 2.0}
CODE_RESULT = {0: Result.OK, 1: Result.WARN, 2: Result.FAIL}
# A window up to this long is read from the 5 minute level, a longer one from the hourly level.
FINE_WINDOW_H = 48.0


log = logging.getLogger(__name__)
BACKFILL_KEY = "monitor_series_backfill_done"

class Store:
    # server.retention_days, the raw level when no retention setting exists; it decides which
    # points are too old to record (see raw_cut). Set by from_config.
    retention_fallback: int | None = None

    def __init__(self, path: str, plugins: LoadedPlugins | None = None, *,
                 backend: str = "sqlite", dsn: str | None = None, password: str | None = None,
                 timescale: str = "auto") -> None:
        self.storage: Storage = open_storage(
            path, {p.name: p.migrations for p in plugins.plugins} if plugins else None,
            backend=backend, dsn=dsn, password=password, timescale=timescale)
        # How long an unconfirmed link stays drawn (config `map.stale_days`). The map tables are
        # rebuilt inside write units that have no config, so the value lives here.
        self.map_stale_days = 90
        self.exporter: Any = None  # the OTLP exporter when it is on (observe/otlp/export.py)

    @classmethod
    def from_config(cls, config: Any, plugins: LoadedPlugins | None = None) -> "Store":
        """Open the database the config names: SQLite at server.db_path, or PostgreSQL."""
        s = config.storage
        store = cls(config.server.db_path, plugins, backend=s.backend,
                    dsn=s.dsn.get_secret_value() if s.dsn else None, password=s.password(),
                    timescale=s.timescaledb)
        store.map_stale_days = config.map.stale_days
        store.retention_fallback = config.server.retention_days
        return store

    async def fetch(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """One read statement on the read pool."""
        return await self.storage.fetchall(sql, args)

    async def execute(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """One write statement in its own unit; returns the rows it produced, if any."""
        return await self.storage.execute(sql, args)

    async def record(self, monitor: str, ts: float, res: CheckResult) -> None:
        # Only a check that was answered has a latency: the time a failed check took is its
        # timeout or a refusal, not how fast the monitor responds.
        latency = None if res.result is Result.FAIL else res.latency_ms
        row = (monitor, ts, res.result.value, res.value, latency, res.message)
        ms = series.to_ms(ts)

        def point(metric: str, unit: str, value: float) -> series.Point:
            return series.Point(MONITOR_SCOPE, metric, unit, "{}", ms, value)

        points = [point("monitor.up", "1", 0.0 if res.result is Result.FAIL else 1.0),
                  point("monitor.result", "1", RESULT_CODE[res.result])]
        if res.value is not None and res.result is not Result.FAIL:
            points.append(point("monitor.value", res.unit.strip(), res.value))
        if latency is not None:
            points.append(point("monitor.latency", "ms", latency))

        def unit(db: Conn) -> None:
            db.execute("INSERT INTO results VALUES (?,?,?,?,?,?)", row)
            series.record_points(db, kind="monitor", name=monitor, points=points, now=ts,
                                 rollups=self.storage.incremental_rollups)

        # A poll result is the server's own work: a full ingest queue must not discard it, or
        # the Down transition that depends on it.
        await self.storage.write(unit, touches=("monitors", "metrics"), critical=True)

    async def backfill_monitor_series(self, batch: int = 2000) -> int:
        """Copy poll rows older than a monitor's first series point into the series. Before the
        series existed every poll was only a row in results, and availability and the forecast now
        read the series, so an upgraded install would otherwise show no history. Newest rows go
        first, so an interrupted run leaves no gap and the next start continues below the oldest
        point already copied. Returns the number of rows copied."""
        done = await self.fetch("SELECT 1 FROM app_settings WHERE key = ?", (BACKFILL_KEY,))
        if done:
            return 0  # the copy ran to the end once; pruned raw samples must not be copied again
        monitors = [r[0] for r in await self.fetch("SELECT DISTINCT monitor FROM results")]
        copied = 0
        for n, monitor in enumerate(monitors, 1):
            log.info("Copying poll history into the series: monitor %d of %d (%s)",
                     n, len(monitors), monitor)
            while True:
                first = await self.fetch(
                    "SELECT MIN(sm.ts) FROM samples sm JOIN series s ON s.id = sm.series_id "
                    "JOIN resources r ON r.id = s.resource_id JOIN scopes sc ON sc.id = s.scope_id "
                    "WHERE r.kind = 'monitor' AND r.name = ? AND sc.name = ? AND s.metric = ?",
                    (monitor, MONITOR_SCOPE, "monitor.up"))
                cutoff = first[0][0] if first and first[0][0] is not None else None
                if cutoff is None:
                    rows = await self.fetch(
                        "SELECT ts, result, value, latency_ms FROM results WHERE monitor = ? "
                        "ORDER BY ts DESC LIMIT ?", (monitor, batch))
                else:
                    rows = await self.fetch(
                        "SELECT ts, result, value, latency_ms FROM results WHERE monitor = ? "
                        "AND ts < ? ORDER BY ts DESC LIMIT ?", (monitor, cutoff / 1000.0, batch))
                if not rows:
                    break
                await self.storage.write(
                    lambda db, rows=rows, monitor=monitor: self._backfill_unit(db, monitor, rows),
                    touches=("monitors", "metrics"))
                copied += len(rows)
        await self.execute(
            "INSERT INTO app_settings (key, value, updated) VALUES (?, '1', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (BACKFILL_KEY, time.time()))
        log.info("Copied %d poll rows into the series", copied)
        return copied

    def _backfill_unit(self, db: Conn, monitor: str, rows: list[tuple[Any, ...]]) -> None:
        points: list[series.Point] = []
        newest = 0.0
        for ts, result, value, latency in rows:
            res = Result(result)
            ms = series.to_ms(ts)
            newest = max(newest, ts)

            def point(metric: str, unit: str, v: float, ms: int = ms) -> series.Point:
                return series.Point(MONITOR_SCOPE, metric, unit, "{}", ms, v)

            points.append(point("monitor.up", "1", 0.0 if res is Result.FAIL else 1.0))
            points.append(point("monitor.result", "1", RESULT_CODE[res]))
            if value is not None and res is not Result.FAIL:
                points.append(point("monitor.value", "", value))
            if latency is not None and res is not Result.FAIL:
                points.append(point("monitor.latency", "ms", latency))
        series.record_points(db, kind="monitor", name=monitor, points=points, now=newest,
                             rollups=self.storage.incremental_rollups)

    async def last_result(self, monitor: str) -> tuple[float, Result] | None:
        """The newest poll result of a monitor and when it was taken, from the latest table."""
        rows = await self.fetch(
            "SELECT l.ts, l.value FROM latest l JOIN series s ON s.id = l.series_id "
            "JOIN resources r ON r.id = s.resource_id JOIN scopes sc ON sc.id = s.scope_id "
            "WHERE r.kind = 'monitor' AND r.name = ? AND sc.name = ? AND s.metric = ?",
            (monitor, MONITOR_SCOPE, "monitor.result"))
        if not rows or rows[0][1] is None:
            return None
        return rows[0][0] / 1000.0, CODE_RESULT[int(rows[0][1])]

    # ---- durable alerts (observe/alerts.py) -------------------------------------------------

    async def set_alert_open(self, monitor: str, state: str | None, at: float) -> None:
        """Remember that a problem alert for `monitor` was sent while it was in `state`
        (down or warn), or forget it when `state` is None (the monitor recovered)."""
        def unit(db: Conn) -> None:
            if state is None:
                db.execute("DELETE FROM alert_open WHERE monitor=?", (monitor,))
            else:
                db.execute("DELETE FROM alert_open WHERE monitor=?", (monitor,))
                db.execute("INSERT INTO alert_open (monitor, state, since) VALUES (?,?,?)",
                           (monitor, state, at))
        await self.storage.write(unit, critical=True)

    async def prune_alert_open(self, keep: set[str]) -> int:
        """Forget the open-alert mark of every monitor not in `keep` (a monitor that was removed
        from the configuration), so a stale row neither lingers nor revives under a reused name.
        Returns the number of rows removed."""
        def unit(db: Conn) -> int:
            rows = db.execute("SELECT monitor FROM alert_open").fetchall()
            gone = [m for (m,) in rows if m not in keep]
            for m in gone:
                db.execute("DELETE FROM alert_open WHERE monitor=?", (m,))
            return len(gone)
        return await self.storage.write(unit, critical=True)

    async def open_alerts(self) -> dict[str, tuple[str, float]]:
        """The monitors with an open problem alert: monitor -> (state, since)."""
        rows = await self.fetch("SELECT monitor, state, since FROM alert_open")
        return {m: (st, float(since)) for m, st, since in rows}

    async def outbox_add(self, rows: list[tuple[str, str]], now: float) -> None:
        """Queue alerts as (target name, JSON body), all due now, in one transaction."""
        def unit(db: Conn) -> None:
            for target, body in rows:
                db.execute("INSERT INTO alert_outbox (target, body, created, attempts, next_at) "
                           "VALUES (?,?,?,0,?)", (target, body, now, now))
        await self.storage.write(unit, critical=True)

    async def outbox_due(self, now: float, limit: int = 100) -> list[tuple[Any, ...]]:
        """Queued alerts that are due, oldest first: (id, target, body, created, attempts). An
        alert is due only when no older alert of its target is still waiting for its next try,
        so the alerts of a target are delivered in the order they were queued. `limit` applies
        to each target, so a large backlog for one target never delays another's alerts."""
        return await self.fetch(
            "SELECT id, target, body, created, attempts FROM ("
            "SELECT id, target, body, created, attempts, "
            "ROW_NUMBER() OVER (PARTITION BY target ORDER BY id) AS rn FROM alert_outbox o "
            "WHERE next_at<=? AND NOT EXISTS (SELECT 1 FROM alert_outbox p WHERE p.target=o.target "
            "AND p.id<o.id AND p.next_at>?)) AS due WHERE rn<=? ORDER BY id", (now, now, limit))

    async def outbox_done(self, ids: list[int]) -> None:
        """Remove alerts that were delivered, or that are no longer wanted."""
        def unit(db: Conn) -> None:
            for i in ids:
                db.execute("DELETE FROM alert_outbox WHERE id=?", (i,))
        await self.storage.write(unit, critical=True)

    async def outbox_retry(self, alert_id: int, attempts: int, next_at: float, error: str) -> None:
        await self.storage.write(lambda db: db.execute(
            "UPDATE alert_outbox SET attempts=?, next_at=?, last_error=? WHERE id=?",
            (attempts, next_at, error[:500], alert_id)), critical=True)

    async def outbox_depth(self) -> int:
        return int((await self.fetch("SELECT COUNT(*) FROM alert_outbox"))[0][0])

    @staticmethod
    def _summary_level(hours: float) -> tuple[str, int]:
        return ("metric_5m", 300) if hours <= FINE_WINDOW_H else ("metric_hourly", 3600)

    async def record_event(self, monitor: str, tr: Transition) -> None:
        row = (monitor, tr.at, tr.previous.value, tr.current.value, tr.message)
        await self.storage.write(
            lambda db: db.execute("INSERT INTO events VALUES (?,?,?,?,?)", row),
            touches=("events",), critical=True)

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

    async def hourly_series(self, monitor: str, days: float,
                            now: float | None = None) -> list[tuple[float, float]]:
        """Hourly means of the non-failing values: [(bucket midpoint ts, mean)]. Read from the
        hourly summary view."""
        since = (time.time() if now is None else now) - days * 86400
        rows = await self.fetch(
            "SELECT bucket, avg_v FROM metric_hourly WHERE resource = ? AND scope = ? "
            "AND metric = ? AND bucket >= ? AND avg_v IS NOT NULL ORDER BY bucket",
            (monitor, MONITOR_SCOPE, "monitor.value", int(since // 3600 * 3600)))
        return [(float(b) + 1800.0, float(v)) for b, v in rows]

    async def availability(self, monitor: str, hours: float,
                           now: float | None = None) -> float | None:
        """Percent of polls in the window that were not FAIL, from the summary view (the 5 minute
        level up to 48 hours, the hourly level beyond), so the cost does not grow with the
        number of polls."""
        view, width = self._summary_level(hours)
        since = (time.time() if now is None else now) - hours * 3600
        rows = await self.fetch(
            f"SELECT CAST(COALESCE(SUM(n), 0) AS BIGINT), "
            f"CAST(COALESCE(SUM(sum_v), 0) AS DOUBLE PRECISION) FROM {view} "
            "WHERE resource = ? AND scope = ? AND metric = ? AND bucket >= ?",
            (monitor, MONITOR_SCOPE, "monitor.up", int(since // width * width)))
        total, ok = rows[0]
        return None if not total else round(float(ok) / int(total) * 100, 3)

    def raw_cut(self, db: Conn, now: float) -> Callable[[str], int]:
        """For a metric name, the millisecond time before which its raw rows are trimmed now."""
        levels = rollups.load_levels(db, self.retention_fallback)
        return lambda metric: compaction.cuts_for(levels, metric, now).raw

    def _ingest_unit(self, db: Conn, batch: Batch, boots: dict[int, tuple[str, int | None]],
                     now: float, body_hash: str = "",
                     outcome: IngestOutcome | None = None) -> tuple[int, int, bool]:
        # Adapted from hostwatch's Store.ingest_batch (hostwatch, same owner): one
        # transaction for the batch id, host row, sources, samples and events.
        # A batch without batch_id is identified by a hash of its content, so a
        # resend of the same batch is not stored twice.
        def clamp(ts: float) -> float:
            return now if ts > now + MAX_FUTURE_SKEW_S else ts

        sent = clamp(batch.sent_at)
        batch_key = batch.batch_id if batch.batch_id is not None else content_key(batch)
        cur = db.execute(
            "INSERT INTO ingest_batches (host, batch_id, ts, body_hash) VALUES (?,?,?,?) "
            "ON CONFLICT(host, batch_id) DO NOTHING", (batch.host, batch_key, now, body_hash))
        if cur.rowcount == 0:
            if body_hash:
                row = db.execute("SELECT body_hash FROM ingest_batches WHERE host = ? "
                                 "AND batch_id = ?", (batch.host, batch_key)).fetchone()
                if row is not None and row[0] and row[0] != body_hash:
                    raise IdempotencyConflict(batch_key)
            return 0, 0, True
        # A resend changes nothing, so only a stored batch bumps the change counters.
        self.storage.write_sync(lambda _db: None, touches=("metrics", "hosts", "events"))
        db.execute(
            "INSERT INTO hosts (host, platform, agent_version, first_seen, last_seen, heartbeat_ts) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET "
            "last_seen=MAX(hosts.last_seen, excluded.last_seen), "
            f"platform=CASE WHEN {_TAKES_OVER} AND excluded.platform <> '{UNNAMED_PLATFORM}' "
            "THEN excluded.platform ELSE hosts.platform END, "
            f"agent_version=CASE WHEN {_TAKES_OVER} "
            f"AND excluded.agent_version <> '{UNNAMED_VERSION}' "
            "THEN excluded.agent_version ELSE hosts.agent_version END, "
            "heartbeat_ts=MAX(COALESCE(hosts.heartbeat_ts, 0), excluded.heartbeat_ts)",
            (batch.host, batch.platform, batch.agent_version, now, now, sent))
        # A pushed host is a resource of kind host; each source is the scope its points came
        # from and its labels are the point attributes (docs/DATA-API-DESIGN.md section 3.2).
        recorded = series.record_points(
            db, kind="host", name=batch.host, now=now, rollups=self.storage.incremental_rollups,
            raw_cut=self.raw_cut(db, now),
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
        if outcome is not None:
            outcome.capped, outcome.late = recorded.dropped, recorded.late
        return len(batch.samples) - recorded.dropped - recorded.late, stored, False

    async def ingest_batch(self, batch: Batch, boots: dict[int, tuple[str, int | None]],
                           now: float | None = None, body_hash: str = "",
                           outcome: IngestOutcome | None = None, critical: bool = False
                           ) -> tuple[int, int, bool]:
        """Store a pushed batch. boots maps an event index to (classification,
        clean_shutdown flag). Returns (samples stored, events stored, duplicate).
        A batch_id already recorded for the host is acknowledged and nothing is
        stored again, unless both records carry a body hash and the hashes differ: the id was
        reused for another body and IdempotencyConflict is raised (nothing is stored). `critical` is
        for the server's own pollers (SNMP, apps), whose readings are not refused when the write
        queue is full; a push never sets it."""
        at = time.time() if now is None else now
        return await self.storage.write(
            lambda db: self._ingest_unit(db, batch, boots, at, body_hash, outcome),
            critical=critical)

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

    @staticmethod
    def _host_list_unit(db: Conn, wanted: dict[str, tuple[float, tuple[tuple[str, str], ...]]],
                        alert_since: float) -> dict[str, dict[str, Any]]:
        """Four statements whatever the number of hosts: the host resources, the latest rows
        inside the oldest wanted window, every source status and the recent warning and critical
        events. A host whose window is empty, or that has a configured series silent for longer,
        costs one more statement for all such hosts together, which reads only their own rows."""
        rids: dict[str, int] = {}
        for rid, name in db.execute("SELECT id, name FROM resources WHERE kind='host' "
                                    "ORDER BY id").fetchall():
            rids.setdefault(name, int(rid))
        by_rid = {rid: host for host, rid in rids.items() if host in wanted}
        min_ms = min((series.to_ms(w[0]) for w in wanted.values()), default=0)
        # The window is pushed into SQL, as _latest_host_unit does, so the rows read follow the
        # live series and not the retention.
        latest: dict[int, list[Any]] = {}
        for r in db.execute(_LATEST_SQL + "WHERE s.resource_id IN "
                            "(SELECT id FROM resources WHERE kind='host') AND l.ts>=? "
                            "ORDER BY s.resource_id, l.ts, l.series_id", (min_ms,)).fetchall():
            latest.setdefault(int(r[6]), []).append(r[:6])
        incomplete = []
        for rid, host in by_rid.items():
            since_ms = series.to_ms(wanted[host][0])
            have = {(r[0], r[1]) for r in latest.get(rid, []) if r[5] >= since_ms}
            if not have or any(k not in have for k in wanted[host][1]):
                incomplete.append(rid)
        full: dict[int, list[Any]] = {}
        if incomplete:
            marks = ",".join(str(i) for i in incomplete)  # integers read from the database
            for r in db.execute(_LATEST_SQL + f"WHERE s.resource_id IN ({marks}) "
                                "ORDER BY s.resource_id, l.ts, l.series_id").fetchall():
                full.setdefault(int(r[6]), []).append(r[:6])
        out: dict[str, dict[str, Any]] = {h: {"samples": [], "sources": {}, "events": []}
                                           for h in wanted}
        for rid, host in by_rid.items():
            since_ms, series_wanted = series.to_ms(wanted[host][0]), wanted[host][1]
            rows = full[rid] if rid in full else latest.get(rid, [])
            # The same three steps as _latest_host_unit: the window, the single newest row when
            # the window is empty, and each configured series' own newest older row.
            samples = [r for r in rows if r[5] >= since_ms]
            if not samples and rows:
                samples = [max(rows, key=lambda r: r[5])]
            have = {(r[0], r[1]) for r in samples}
            for src, metric in series_wanted:
                if (src, metric) not in have:
                    samples = samples + [r for r in rows
                                         if r[0] == src and r[1] == metric and r[5] < since_ms]
            out[host]["samples"] = [
                {"source": r[0], "metric": r[1], "labels": json.loads(r[2]), "value": r[3],
                 "unit": r[4], "ts": r[5] / 1000.0} for r in samples]
        for host, source, available, reason, updated in db.execute(
                "SELECT host, source, available, reason, updated FROM host_sources").fetchall():
            if host in out:
                out[host]["sources"][source] = {"available": bool(available), "reason": reason,
                                                "updated": updated}
        # Of the 50 newest events of a host, only the warnings and criticals inside the alert
        # window reach the list (the summary shows the alert section, not the event rows).
        for host, ts, kind, severity, src, title, detail, boot_id in db.execute(
                "SELECT host, ts, kind, severity, source, title, detail, boot_id FROM ("
                "SELECT host, ts, kind, severity, source, title, detail, boot_id, id, "
                "ROW_NUMBER() OVER (PARTITION BY host ORDER BY ts DESC, id DESC) AS rn "
                "FROM host_events WHERE ts >= ?) e WHERE rn <= 50 "
                "AND severity IN ('warning','critical') ORDER BY host, ts DESC, id DESC",
                (alert_since,)).fetchall():
            if host in out:
                out[host]["events"].append(
                    {"ts": ts, "kind": kind, "severity": severity, "source": src, "title": title,
                     "detail": json.loads(detail), "boot_id": boot_id})
        return out

    async def host_list_inputs(self, wanted: dict[str, tuple[float, tuple[tuple[str, str], ...]]],
                               alert_since: float) -> dict[str, dict[str, Any]]:
        """What the host list needs for the hosts in `wanted` (host -> oldest sample time and the
        configured series), in a constant number of statements. Per host the result has the
        `samples` latest_host would return, the `sources` of host_sources and the `events`
        host_events would return that can raise an alert."""
        return await self.storage.read(lambda db: self._host_list_unit(db, wanted, alert_since))

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
            touches=("audit",), critical=True)  # a denial must stay a 401/403/429 under load

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
