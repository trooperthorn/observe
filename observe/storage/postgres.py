"""The PostgreSQL backend, with TimescaleDB when it is available.

It mirrors the SQLite backend's shape so nothing above it can tell them apart. Writes run on one
dedicated thread over one connection, each unit in its own transaction and in submission order.
Reads borrow a read-only, repeatable-read connection from a psycopg-pool pool, so one response
sees one snapshot; an empty pool past the deadline raises StorageBusy and a statement past the
deadline is cancelled by the server and raises StorageTimeout. The portable SQL that units
carry (`?` placeholders and the other forms in pg_dialect) is rewritten at the connection.

The connection string never leaves this module. Its password comes from a secret file and is
passed on its own, and every error, log record and repr that could carry either is scrubbed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool, PoolTimeout

from . import compaction, pg_timescale, rollups
from .base import (CHANGE_DOMAINS, Conn, IntegrityConflict, StorageBusy, StorageError,
                   StorageTimeout, T, WriteGate)
from .pg_dialect import table_info_query, translate_sql
from .schema import (MIGRATIONS, PLUGIN_TABLES, ROLLUP_STEP, PluginSchemaTooNewError,
                     SchemaTooNewError, refuse_legacy)

log = logging.getLogger("observe.storage.postgres")

READ_POOL_SIZE = 3
READ_DEADLINE_S = 2.0
CONNECT_TIMEOUT_S = 10.0
MIGRATION_LOCK = 7_424_001  # advisory lock key, so two starting processes migrate one at a time
TIMESCALE_MODES = ("auto", "on", "off")

_URI_OR_PASSWORD = re.compile(r"(?:postgres(?:ql)?://\S+|password\s*=\s*\S+)", re.IGNORECASE)


class Scrubber:
    """Replaces the connection string, its password and anything shaped like either with a
    fixed marker. Built from the secrets, held only here."""

    def __init__(self, *secrets: str | None) -> None:
        self._secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[withheld]")
        return _URI_OR_PASSWORD.sub("[withheld]", text)


class _ScrubFilter(logging.Filter):
    def __init__(self, scrub: Scrubber) -> None:
        super().__init__()
        self._scrub = scrub

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_info = None  # the traceback text is not scrubbed, so it is not kept
            record.exc_text = None
        return True


class _Cursor:
    """The slice of the DB-API cursor that units use, safe on statements that return no rows."""

    def __init__(self, cur: psycopg.Cursor[Any]) -> None:
        self._cur = cur

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self) -> Any:
        return self._cur.description

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._cur.fetchall()) if self._cur.description else []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._cur.fetchone() if self._cur.description else None

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(self.fetchall())


def _args(args: Sequence[Any]) -> tuple[Any, ...]:
    # A flag stored in an INTEGER column arrives as bool; PostgreSQL does not cast it.
    return tuple(int(a) if isinstance(a, bool) else a for a in args)


class PgConn:
    """A psycopg connection that takes the portable SQL and the `?` placeholders."""

    def __init__(self, raw: psycopg.Connection[Any]) -> None:
        self.raw = raw

    def execute(self, sql: str, args: Sequence[Any] = ()) -> _Cursor:
        info = table_info_query(sql)
        if info is not None:
            return _Cursor(self.raw.execute(*info))
        if args:
            return _Cursor(self.raw.execute(translate_sql(sql), _args(args)))
        return _Cursor(self.raw.execute(translate_sql(sql, has_args=False)))

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> _Cursor:
        cur = self.raw.cursor()
        cur.executemany(translate_sql(sql), [_args(r) for r in rows])
        return _Cursor(cur)


class PgStorage:
    backend = "postgres"

    def __init__(self, dsn: str, plugins: Mapping[str, Sequence[Any]] | None = None, *,
                 password: str | None = None, timescale: str = "auto",
                 read_pool_size: int = READ_POOL_SIZE, read_deadline_s: float = READ_DEADLINE_S,
                 connect_timeout_s: float = CONNECT_TIMEOUT_S) -> None:
        if not 1 <= read_pool_size <= 6:
            raise ValueError("read_pool_size must be 1 to 6")
        if timescale not in TIMESCALE_MODES:
            raise ValueError(f"timescale must be one of {', '.join(TIMESCALE_MODES)}")
        self._closed = True
        self._scrub = Scrubber(dsn, password)
        self._filter = _ScrubFilter(self._scrub)
        self._pool_log = logging.getLogger("psycopg.pool")
        self._pool_log.addFilter(self._filter)
        self._deadline_s = read_deadline_s
        self._writer_tid = 0
        self._touched: set[str] = set()
        self._wraw: psycopg.Connection[Any] | None = None
        self._admin: PgConn | None = None
        self._pool: ConnectionPool | None = None
        self._read_exec: ThreadPoolExecutor | None = None
        self._gate = WriteGate()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer",
                                          initializer=self._mark_writer)
        try:
            self._conninfo = make_conninfo(dsn, password=password, application_name="observe",
                                           connect_timeout=int(max(1, connect_timeout_s)))
            self._wraw = psycopg.connect(self._conninfo, autocommit=False)
            self._admin = PgConn(psycopg.connect(self._conninfo, autocommit=True))
            self._wconn = PgConn(self._wraw)
            self.timescale = self._start(plugins or {}, timescale)
            self.incremental_rollups = not self.timescale
            self._seqs = {d: int(s) for d, s in self._wconn.execute(
                "SELECT domain, seq FROM change_seq")}
            self._wraw.commit()
            self._pool = ConnectionPool(
                self._conninfo, min_size=read_pool_size, max_size=read_pool_size, open=False,
                timeout=read_deadline_s, configure=self._configure_reader, name="observe-read",
                kwargs={"options": self._reader_options(read_deadline_s)})
            self._pool.open(wait=True, timeout=connect_timeout_s)
            self._read_exec = ThreadPoolExecutor(max_workers=read_pool_size,
                                                 thread_name_prefix="db-read")
        except BaseException as err:
            self._closed = False
            self.close()
            if isinstance(err, (StorageError, SchemaTooNewError)):
                raise
            if isinstance(err, (psycopg.Error, PoolTimeout)):
                raise StorageError("could not open the PostgreSQL database: "
                                   f"{self._scrub(str(err)) or type(err).__name__}") from None
            raise
        self._closed = False

    def __repr__(self) -> str:
        return "PgStorage(postgres)"

    # ---- start-up -------------------------------------------------------------------------

    def _reader_options(self, deadline_s: float) -> str:
        """The server options for a pooled reader: those already in the connection string (a
        search_path, for one) followed by the statement timeout. A bare `options` keyword would
        replace the ones in the string, so the pool would lose the schema the writer uses."""
        have = conninfo_to_dict(self._conninfo).get("options")
        timeout = f"-c statement_timeout={int(deadline_s * 1000)}"
        return f"{have} {timeout}" if have else timeout

    @staticmethod
    def _configure_reader(conn: psycopg.Connection[Any]) -> None:
        conn.read_only = True
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ

    def _mark_writer(self) -> None:
        self._writer_tid = threading.get_ident()

    def _start(self, plugins: Mapping[str, Sequence[Any]], mode: str) -> bool:
        """Migrate, then settle on TimescaleDB or not. Returns whether it is in use."""
        assert self._admin is not None
        wanted = False
        if mode != "off":
            try:
                self._admin.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                wanted = True
            except psycopg.Error as err:
                if mode == "on":
                    raise StorageError("storage.timescaledb is 'on' but the TimescaleDB extension "
                                       f"is not available: {self._scrub(str(err))}") from None
                log.warning("TimescaleDB is not available; using plain PostgreSQL with the "
                            "shared incremental rollups")
        self._migrate(wanted)
        self._migrate_plugins(plugins)
        in_use = self._is_hypertable()
        if wanted and not in_use:
            raise StorageError("this database was created without TimescaleDB; destroy it and "
                               "redeploy, or set storage.timescaledb to 'off'")
        if in_use and not wanted:
            raise StorageError("this database uses TimescaleDB, but the extension is off or "
                               "unavailable; enable it or destroy and redeploy")
        if in_use:
            self._apply_policies(rollups.load_levels(self._wconn))
        return in_use

    def _is_hypertable(self) -> bool:
        assert self._admin is not None
        have = self._admin.execute("SELECT to_regclass('timescaledb_information.hypertables')"
                                   ).fetchone()
        if not have or have[0] is None:
            return False
        return bool(self._admin.execute(pg_timescale.IS_HYPERTABLE).fetchall())

    def _migrate(self, timescale: bool) -> None:
        db, raw = self._wconn, self._wraw
        assert raw is not None
        db.execute("SELECT pg_advisory_lock(%s)" % MIGRATION_LOCK)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            raw.commit()
            current = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
            raw.commit()
            latest = max(MIGRATIONS)
            legacy = db.execute("SELECT to_regclass('host_samples')").fetchone()
            raw.commit()
            refuse_legacy(current, bool(legacy and legacy[0] is not None))
            if current > latest:
                raise SchemaTooNewError(
                    f"database schema version {current} is newer than this Observe "
                    f"supports ({latest}); upgrade Observe or restore an older database")
            for version in range(current + 1, latest + 1):
                try:
                    if version == ROLLUP_STEP and timescale:
                        self._timescale_step()
                    else:
                        for stmt in MIGRATIONS[version]:
                            if callable(stmt):
                                stmt(db)
                            else:
                                db.execute(stmt)
                    db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                    raw.commit()
                except BaseException:
                    raw.rollback()
                    raise
        finally:
            raw.rollback()
            db.execute("SELECT pg_advisory_unlock(%s)" % MIGRATION_LOCK)
            raw.commit()

    def _timescale_step(self) -> None:
        """The last core step on TimescaleDB: the hypertable and continuous aggregates replace
        the summary tables, then the shared state table and views. Not transactional (a
        continuous aggregate cannot be created in a transaction), and every statement is
        repeatable, so a failed start is run again."""
        assert self._admin is not None and self._wraw is not None
        self._wraw.commit()
        pg_timescale.apply_setup(self._admin.raw)
        for stmt in (rollups.ROLLUP_TABLES[-1], *rollups.METRIC_VIEWS):
            self._admin.execute(stmt)

    def _migrate_plugins(self, plugins: Mapping[str, Sequence[Any]]) -> None:
        db, raw = self._wconn, self._wraw
        assert raw is not None
        db.execute(PLUGIN_TABLES[0])
        raw.commit()
        current: dict[str, int] = {}
        for name, migrations in plugins.items():
            row = db.execute("SELECT version FROM plugin_schema WHERE plugin=?", (name,)).fetchone()
            current[name] = row[0] if row else 0
            if current[name] > len(migrations):
                raise PluginSchemaTooNewError(
                    f"plugin {name!r} database schema version {current[name]} is newer than "
                    f"this plugin release supports ({len(migrations)}); upgrade the plugin "
                    "or restore an older database")
        raw.commit()
        for name, migrations in plugins.items():
            for migration in migrations[current[name]:]:
                try:
                    for stmt in migration.statements:
                        db.execute(stmt)
                    db.execute(
                        "INSERT INTO plugin_schema (plugin, version) VALUES (?, ?) "
                        "ON CONFLICT (plugin) DO UPDATE SET version = excluded.version",
                        (name, migration.version))
                    raw.commit()
                except BaseException:
                    raw.rollback()
                    raise

    # ---- writer ---------------------------------------------------------------------------

    @staticmethod
    def _check_domains(touches: Sequence[str]) -> None:
        for domain in touches:
            if domain not in CHANGE_DOMAINS:
                raise ValueError(f"unknown change domain {domain!r}")

    def _translate(self, err: psycopg.Error) -> Exception:
        if isinstance(err, pg_errors.IntegrityError):
            return IntegrityConflict(self._scrub(str(err)))
        if isinstance(err, psycopg.OperationalError):
            return StorageError(self._scrub(str(err)) or "database connection failed")
        return err

    def _reconnect_if_broken(self) -> None:
        raw = self._wraw
        assert raw is not None
        if raw.closed or raw.broken:
            try:
                raw.close()
            finally:
                self._wraw = psycopg.connect(self._conninfo, autocommit=False)
                self._wconn = PgConn(self._wraw)

    def _run_unit(self, unit: Callable[[Conn], T], touches: tuple[str, ...]) -> T:
        try:
            self._reconnect_if_broken()
        except psycopg.Error as err:
            raise self._translate(err) from None
        raw, conn = self._wraw, self._wconn
        assert raw is not None
        self._touched = set(touches)
        try:
            result = unit(conn)
            bumped = sorted(self._touched)
            fresh = {d: int(conn.execute(
                "UPDATE change_seq SET seq = seq + 1 WHERE domain = ? RETURNING seq",
                (d,)).fetchone()[0]) for d in bumped}
            raw.commit()
        except psycopg.Error as err:
            self._safe_rollback(raw)
            raise self._translate(err) from None
        except BaseException:
            self._safe_rollback(raw)
            raise
        finally:
            self._touched = set()
        if fresh:
            self._seqs = {**self._seqs, **fresh}
        return result

    @staticmethod
    def _safe_rollback(raw: psycopg.Connection[Any]) -> None:
        try:
            raw.rollback()
        except psycopg.Error:
            pass  # a broken connection is replaced at the next unit

    def _on_writer(self) -> bool:
        return threading.get_ident() == self._writer_tid

    def write_sync(self, unit: Callable[[Conn], T], *, touches: Sequence[str] = ()) -> T:
        self._check_domains(touches)
        if self._on_writer():
            self._touched.update(touches)
            return unit(self._wconn)
        return self._gate.submit(self._writer, self._run_unit, unit, tuple(touches)).result()

    async def write(self, unit: Callable[[Conn], T], *, touches: Sequence[str] = ()) -> T:
        self._check_domains(touches)
        return await asyncio.wrap_future(
            self._gate.submit(self._writer, self._run_unit, unit, tuple(touches)))

    # ---- readers --------------------------------------------------------------------------

    def _read_on_writer(self, unit: Callable[[Conn], T]) -> T:
        try:
            return unit(self._wconn)
        except psycopg.Error as err:
            raise self._translate(err) from None

    def _read_unit(self, unit: Callable[[Conn], T]) -> T:
        assert self._pool is not None
        try:
            with self._pool.connection(timeout=self._deadline_s) as raw:
                try:
                    return unit(PgConn(raw))
                finally:
                    raw.rollback()
        except PoolTimeout:
            raise StorageBusy("no read connection was free within the deadline") from None
        except pg_errors.QueryCanceled:
            raise StorageTimeout(f"read ran past {self._deadline_s} s") from None
        except psycopg.Error as err:
            raise self._translate(err) from None

    def read_sync(self, unit: Callable[[Conn], T]) -> T:
        if self._on_writer():
            return self._read_on_writer(unit)  # a unit reads its own uncommitted writes
        return self._read_unit(unit)

    async def read(self, unit: Callable[[Conn], T]) -> T:
        assert self._read_exec is not None
        return await asyncio.wrap_future(self._read_exec.submit(self._read_unit, unit))

    async def fetchall(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return await self.read(lambda db: db.execute(sql, args).fetchall())

    async def execute(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return await self.write(lambda db: db.execute(sql, args).fetchall())

    # ---- change sequences -----------------------------------------------------------------

    def change_seq(self, domain: str) -> int:
        return self._seqs[domain]

    def change_seqs(self) -> dict[str, int]:
        return dict(self._seqs)

    # ---- rollups, retention, plugin tables ------------------------------------------------

    def _admin_run(self, statements: list[str]) -> None:
        assert self._admin is not None
        try:
            for stmt in statements:
                self._admin.raw.execute(stmt)
        except psycopg.Error as err:
            raise self._translate(err) from None

    def _apply_policies(self, levels: rollups.RetentionLevels) -> None:
        self._admin_run(pg_timescale.policy_statements(levels))

    async def apply_retention(self, *, now: float, retention_days: int,
                              audit_retention_days: int) -> int:
        """One compaction pass. Plain PostgreSQL runs the shared per-series, chunked, verified
        compaction. On TimescaleDB the summary levels are trimmed by their policies, and raw
        samples by `drop_raw`, which refreshes the aggregates and checks coverage first."""
        if not self.timescale:
            return await compaction.run(self, now, retention_days, audit_retention_days)
        return await compaction.run(self, now, retention_days, audit_retention_days,
                                    summaries_by_policy=True, raw=self.drop_raw)

    async def drop_raw(self, now: float, levels: rollups.RetentionLevels) -> int:
        """TimescaleDB: refresh the aggregates over the recent window, verify that they cover the
        raw chunks about to go, and drop those chunks. A metric with a shorter override is trimmed
        by row first, because a chunk can only be dropped once every metric in it is past
        retention. Returns the raw rows older than the drop bound."""
        def refresh() -> None:
            self._apply_policies(levels)
            self._admin_run(pg_timescale.refresh_statements(now, levels))
        await asyncio.wrap_future(self._writer.submit(refresh))
        if levels.overrides:
            await compaction.compact_level(self, "raw", now, levels)
        holds = compaction.whole_table_cuts(levels, now)
        bound = holds.raw

        def check(db: Conn) -> tuple[bool, int]:
            ok = compaction.coverage_ok(db, "raw", bound, holds)
            n = db.execute("SELECT COUNT(*) FROM samples WHERE ts < ?", (bound,)).fetchone()[0]
            return ok, int(n)

        def check_and_drop() -> tuple[bool, int]:
            # One writer-thread unit: ingest runs on this thread too, so no late sample can
            # reach an old chunk between the check and the drop.
            assert self._wraw is not None
            try:
                ok, n = self._read_on_writer(check)
            finally:
                self._wraw.rollback()
            if ok:
                self._admin_run([pg_timescale.drop_raw_statement(bound)])
            return ok, n

        ok, rows = await asyncio.wrap_future(self._writer.submit(check_and_drop))
        error = ""
        if not ok:
            error = "raw chunks kept: the aggregates do not cover them yet"
            log.warning(error)
        await self.write(lambda db: compaction.note_level(
            db, "raw", bound // 1000, now, rows if ok else 0, error))
        return rows if ok else 0

    async def save_retention_settings(self, changes: dict[str, str | None], *, now: float,
                                      actor: str, remote: str, path: str,
                                      fallback_raw_days: int | None = None) -> dict:
        """Write the retention settings and their one audit row in one transaction, then, on
        TimescaleDB, register the refresh, retention and compression policies again so the new
        values apply now and not at the next compaction."""
        result = await self.write(lambda db: rollups.save_settings(
            db, changes, now=now, actor=actor, remote=remote, path=path,
            fallback_raw_days=fallback_raw_days), touches=("admin", "audit"))
        if self.timescale:
            await self.refresh_policies(fallback_raw_days)
        return result

    async def refresh_policies(self, fallback_raw_days: int | None = None) -> None:
        levels = await self.write(lambda db: rollups.load_levels(db, fallback_raw_days))
        await asyncio.wrap_future(self._writer.submit(self._apply_policies, levels))

    def apply_plugin_migrations(self, plugins: Mapping[str, Sequence[Any]]) -> None:
        if self._on_writer():
            self._migrate_plugins(plugins)
        else:
            self._writer.submit(self._migrate_plugins, plugins).result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.shutdown(wait=True)
        if self._read_exec is not None:
            self._read_exec.shutdown(wait=True)
        for closer in (self._pool and self._pool.close,
                       self._wraw and self._wraw.close,
                       self._admin and self._admin.raw.close):
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    log.debug("closing a connection failed")
        self._pool_log.removeFilter(self._filter)
