"""The SQLite backend: one writer thread and a small read-only WAL connection pool.

Writes. One read-write connection lives on a single dedicated thread. Each write unit runs in
its own BEGIN IMMEDIATE ... COMMIT, in submission order, so nothing else ever holds the WAL
write lock and the default thread pool (and so asyncio's getaddrinfo) never waits for the
database. A unit may bump change counters; the in-memory mirror is updated after the commit.

Reads. Three connections opened with a `mode=ro` URI and query_only=ON sit in a queue. A read
borrows one, runs inside one deferred transaction (one snapshot per response), and returns it.
An empty pool for longer than the deadline raises StorageBusy, and a progress handler
interrupts any read that runs past the same deadline, so a long read cannot hold back WAL
checkpoints. An in-memory database (tests only) has no second connection to open, so its reads
run on the writer connection.
"""

from __future__ import annotations

import asyncio
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .base import CHANGE_DOMAINS, Conn, IntegrityConflict, StorageBusy, StorageTimeout, T
from .schema import migrate, migrate_plugins

READ_POOL_SIZE = 3
READ_DEADLINE_S = 2.0
PROGRESS_STEPS = 10_000

WRITER_PRAGMAS = (
    "PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000", "PRAGMA cache_size=-16000", "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=67108864", "PRAGMA wal_autocheckpoint=1000",
    "PRAGMA journal_size_limit=67108864",
)
READER_PRAGMAS = (
    "PRAGMA query_only=ON", "PRAGMA cache_size=-8000", "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=67108864",
)


class _Reader:
    """A read connection and the deadline its progress handler enforces."""

    def __init__(self, path: str) -> None:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        self.deadline = 0.0
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
        for pragma in READER_PRAGMAS:
            self.conn.execute(pragma)
        self.conn.set_progress_handler(self._expired, PROGRESS_STEPS)

    def _expired(self) -> int:
        return 1 if self.deadline and time.monotonic() > self.deadline else 0


def _translate(err: sqlite3.Error) -> Exception:
    if isinstance(err, sqlite3.IntegrityError):
        return IntegrityConflict(str(err))
    return err


class SqliteStorage:
    backend = "sqlite"

    def __init__(self, path: str, plugins: Mapping[str, Sequence[Any]] | None = None, *,
                 read_pool_size: int = READ_POOL_SIZE,
                 read_deadline_s: float = READ_DEADLINE_S) -> None:
        if not 1 <= read_pool_size <= 6:
            raise ValueError("read_pool_size must be 1 to 6")
        self._closed = True
        self._path = path
        self._memory = path == ":memory:"
        self._deadline_s = read_deadline_s
        if not self._memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            for pragma in WRITER_PRAGMAS:
                self._conn.execute(pragma)
            migrate(self._conn)
            migrate_plugins(self._conn, plugins or {})
            self._seqs = self._load_seqs(self._conn)
        except BaseException:
            self._conn.close()
            raise
        self._writer_tid = 0
        self._touched: set[str] = set()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer",
                                          initializer=self._mark_writer)
        self._readers: queue.Queue[_Reader] = queue.Queue()
        self._all_readers: list[_Reader] = []
        self._read_exec: ThreadPoolExecutor | None = None
        self._closed = False
        if not self._memory:
            try:
                for _ in range(read_pool_size):
                    reader = _Reader(path)
                    self._all_readers.append(reader)
                    self._readers.put(reader)
                self._read_exec = ThreadPoolExecutor(max_workers=read_pool_size,
                                                     thread_name_prefix="db-read")
            except BaseException:
                self.close()
                raise

    # ---- writer ---------------------------------------------------------------------------

    def _mark_writer(self) -> None:
        self._writer_tid = threading.get_ident()

    @staticmethod
    def _load_seqs(conn: sqlite3.Connection) -> dict[str, int]:
        return {d: int(s) for d, s in conn.execute("SELECT domain, seq FROM change_seq")}

    @staticmethod
    def _check_domains(touches: Sequence[str]) -> None:
        for domain in touches:
            if domain not in CHANGE_DOMAINS:
                raise ValueError(f"unknown change domain {domain!r}")

    def _run_unit(self, unit: Callable[[Conn], T], touches: tuple[str, ...]) -> T:
        conn = self._conn
        self._touched = set(touches)
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = unit(conn)
            bumped = sorted(self._touched)
            for domain in bumped:
                conn.execute("UPDATE change_seq SET seq=seq+1 WHERE domain=?", (domain,))
            fresh = {d: int(conn.execute("SELECT seq FROM change_seq WHERE domain=?",
                                         (d,)).fetchone()[0]) for d in bumped}
            conn.execute("COMMIT")
        except sqlite3.Error as err:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise _translate(err) from err
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            self._touched = set()
        if fresh:
            self._seqs = {**self._seqs, **fresh}
        return result

    def _on_writer(self) -> bool:
        return threading.get_ident() == self._writer_tid

    def write_sync(self, unit: Callable[[Conn], T], *, touches: Sequence[str] = ()) -> T:
        self._check_domains(touches)
        if self._on_writer():
            # Called from inside a running unit: join its transaction instead of waiting on
            # ourselves.
            self._touched.update(touches)
            return unit(self._conn)
        return self._writer.submit(self._run_unit, unit, tuple(touches)).result()

    async def write(self, unit: Callable[[Conn], T], *, touches: Sequence[str] = ()) -> T:
        self._check_domains(touches)
        return await asyncio.wrap_future(
            self._writer.submit(self._run_unit, unit, tuple(touches)))

    # ---- readers --------------------------------------------------------------------------

    def _read_on_writer(self, unit: Callable[[Conn], T]) -> T:
        try:
            return unit(self._conn)
        except sqlite3.Error as err:
            raise _translate(err) from err

    def _read_unit(self, unit: Callable[[Conn], T], borrow_by: float) -> T:
        try:
            reader = self._readers.get(timeout=max(0.0, borrow_by - time.monotonic()))
        except queue.Empty:
            raise StorageBusy("no read connection was free within the deadline") from None
        try:
            reader.deadline = time.monotonic() + self._deadline_s
            conn = reader.conn
            conn.execute("BEGIN")
            try:
                return unit(conn)
            finally:
                reader.deadline = 0.0
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        except sqlite3.OperationalError as err:
            if "interrupted" in str(err):
                raise StorageTimeout(f"read ran past {self._deadline_s} s") from err
            raise
        finally:
            self._readers.put(reader)

    def read_sync(self, unit: Callable[[Conn], T]) -> T:
        if self._on_writer():
            return self._read_on_writer(unit)  # a unit reads its own uncommitted writes
        if self._memory:
            return self._writer.submit(self._read_on_writer, unit).result()
        return self._read_unit(unit, time.monotonic() + self._deadline_s)

    async def read(self, unit: Callable[[Conn], T]) -> T:
        if self._read_exec is None:
            return await asyncio.wrap_future(self._writer.submit(self._read_on_writer, unit))
        borrow_by = time.monotonic() + self._deadline_s
        return await asyncio.wrap_future(self._read_exec.submit(self._read_unit, unit, borrow_by))

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

    async def rollup(self, now: float) -> int:
        # The rollup tables arrive with the samples schema (slice O-2); until then there is
        # nothing to fold.
        return 0

    async def apply_retention(self, *, now: float, retention_days: int,
                              audit_retention_days: int) -> int:
        """Poll rows and host samples past retention go; transitions and host events are kept
        at least a year; the audit log has its own retention; expired sessions go. Each table
        is its own unit so ingest is never held back behind one long delete."""
        cutoff = now - retention_days * 86400
        keep = now - max(retention_days, 365) * 86400

        async def delete(sql: str, bound: float) -> int:
            return await self.write(lambda db: db.execute(sql, (bound,)).rowcount)

        removed = await delete("DELETE FROM results WHERE ts < ?", cutoff)
        await delete("DELETE FROM host_samples WHERE ts < ?", cutoff)
        await delete("DELETE FROM ingest_batches WHERE ts < ?", cutoff)
        await delete("DELETE FROM events WHERE ts < ?", keep)
        await delete("DELETE FROM host_events WHERE ts < ?", keep)
        await delete("DELETE FROM audit WHERE ts < ?", now - audit_retention_days * 86400)
        await delete("DELETE FROM sessions WHERE expires < ?", now)
        return removed

    def apply_plugin_migrations(self, plugins: Mapping[str, Sequence[Any]]) -> None:
        # migrate_plugins runs its own transaction per step, so it is not wrapped in a unit.
        if self._on_writer():
            migrate_plugins(self._conn, plugins)
        else:
            self._writer.submit(migrate_plugins, self._conn, plugins).result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.shutdown(wait=True)
        if self._read_exec is not None:
            self._read_exec.shutdown(wait=True)
        for reader in self._all_readers:
            reader.conn.close()
        self._conn.close()
