"""The Storage interface: the only way the rest of Observe reaches a database.

A backend implements `Storage`. Callers submit units of work: a write unit is a callable
that receives a transaction connection and runs on the backend's single writer inside one
transaction; a read unit receives a read-only connection and runs on the read pool. The
API, rules, plugins and pollers never learn which backend is in use (docs/DATA-API-DESIGN.md
section 12). The connection handed to a unit is a DB-API connection that takes `?`
placeholders; a backend whose driver differs adapts it.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from typing import Any, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")
Conn = Any

# Change domains (section 1.2). A write unit names the domains it changed and the writer bumps
# each counter inside the same transaction.
CHANGE_DOMAINS = ("metrics", "hosts", "monitors", "events", "map", "ports", "unifi", "ha",
                  "audit", "admin")


class StorageError(RuntimeError):
    """Base class for storage failures."""


class IntegrityConflict(StorageError):
    """A uniqueness, foreign key or check constraint refused a write."""


class StorageBusy(StorageError):
    """No read connection became free within the pool deadline, or the write queue is full (the
    API answers 503)."""


class StorageTimeout(StorageError):
    """A read ran past its deadline and was interrupted."""


# True inside the server's own background work (a plugin collector): its writes are critical, as
# a poller's already are, because a refused one would drop that cycle's reading.
SERVER_WORK: ContextVar[bool] = ContextVar("observe_server_work", default=False)

# Write units that may wait for the single writer at once (docs/DATA-API-DESIGN.md section 9).
WRITE_QUEUE_LIMIT = 256


class WriteGate:
    """Bounds the writer queue. A unit counts from the moment it is submitted until it has
    finished; a submission past `limit` raises StorageBusy instead of queueing, so a slow disk
    or a flood of pushes can not grow memory without bound. The API answers 503 with
    Retry-After and the producer keeps its batch."""

    def __init__(self, limit: int = WRITE_QUEUE_LIMIT) -> None:
        if limit < 1:
            raise ValueError("the write queue limit must be at least 1")
        self.limit = limit
        self._pending = 0
        self._lock = threading.Lock()

    @property
    def pending(self) -> int:
        return self._pending

    def submit(self, executor: Executor, fn: Callable[..., T], *args: Any,
               critical: bool = False) -> "Future[T]":
        """Queue `fn`. A `critical` unit is never refused: it is the server's own work that
        alerting depends on (a poll result, the open-alert mark, the outbox, the audit row of a
        denial), produced by a bounded number of pollers and by requests that are already being
        answered, so it can not grow without bound the way a flood of pushes can."""
        with self._lock:
            if not (critical or SERVER_WORK.get()) and self._pending >= self.limit:
                raise StorageBusy(f"the write queue is full ({self.limit} units waiting)")
            self._pending += 1
        try:
            future = executor.submit(fn, *args)
        except BaseException:
            self._done()
            raise
        future.add_done_callback(lambda _f: self._done())
        return future

    def _done(self) -> None:
        with self._lock:
            self._pending -= 1


@contextmanager
def savepoint(db: Conn, name: str = "item") -> Iterator[None]:
    """One item of a bigger write unit. An exception rolls back to the start of the block and
    is raised again, so the caller can count the item as skipped and carry on with the rest;
    the transaction around it is untouched. Works on every backend, because SAVEPOINT is the
    same statement on SQLite and PostgreSQL."""
    if not name.isidentifier():
        raise ValueError("a savepoint name must be an identifier")
    db.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        db.execute(f"ROLLBACK TO SAVEPOINT {name}")
        db.execute(f"RELEASE SAVEPOINT {name}")
        raise
    db.execute(f"RELEASE SAVEPOINT {name}")


@runtime_checkable
class Storage(Protocol):
    backend: str
    # Whether the writer folds each new point into the summary levels itself. False on
    # PostgreSQL with TimescaleDB, where continuous aggregates build them.
    incremental_rollups: bool

    async def write(self, unit: Callable[[Conn], T], *, touches: Sequence[str] = (),
                    critical: bool = False) -> T:
        """Run `unit` on the writer in one transaction and return its result. `touches`
        names the change domains to bump in that transaction. A `critical` unit is not refused
        when the write queue is full (see WriteGate.submit); ingest never sets it."""

    def write_sync(self, unit: Callable[[Conn], T], *, touches: Sequence[str] = (),
                   critical: bool = False) -> T:
        """Blocking form of `write`, for code already running on a worker thread. Called from
        inside a running write unit it joins that unit's transaction."""

    async def read(self, unit: Callable[[Conn], T]) -> T:
        """Run `unit` on a read-only pooled connection in one snapshot."""

    def read_sync(self, unit: Callable[[Conn], T]) -> T:
        """Blocking form of `read`."""

    async def fetchall(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """One read statement."""

    async def execute(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """One write statement in its own unit. Returns the rows it produced, if any."""

    def change_seq(self, domain: str) -> int:
        """The committed change counter of a domain, read from memory."""

    def change_seqs(self) -> dict[str, int]:
        """All committed change counters."""

    async def apply_retention(self, *, now: float, retention_days: int,
                              audit_retention_days: int) -> int:
        """One compaction pass (observe/storage/compaction.py): trim the raw samples and the
        summary levels past their retention after verifying coverage, in chunks, and drop the
        other history rows past theirs. Returns the number of poll rows removed."""

    async def save_retention_settings(self, changes: dict[str, str | None], *, now: float,
                                      actor: str, remote: str, path: str,
                                      fallback_raw_days: int | None = None) -> dict:
        """Write retention, compaction and rollup settings to app_settings with one audit row
        holding the old and new values, in one transaction. A None value resets a setting.
        Returns {"old": ..., "new": ...}. On TimescaleDB the policies are registered again."""

    def apply_plugin_migrations(self, plugins: Mapping[str, Sequence[Any]]) -> None:
        """Create or upgrade plugin tables from each plugin's migrations (portable DDL)."""

    def close(self) -> None:
        """Stop the writer, close every connection."""
