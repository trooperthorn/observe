"""Database access for Observe. Everything outside this package talks to `Storage`."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from .base import (CHANGE_DOMAINS, Conn, IntegrityConflict, Storage, StorageBusy, StorageError,
                   StorageTimeout, not_retryable, savepoint)

__all__ = ["CHANGE_DOMAINS", "Conn", "DB_ERRORS", "IntegrityConflict", "Storage", "StorageBusy",
           "StorageError", "StorageTimeout", "not_retryable", "open_storage", "savepoint"]

# Every error a unit of work can meet from a driver or from the storage layer, so code that
# tolerates a failed statement can name it without importing a driver.
DB_ERRORS: tuple[type[Exception], ...] = (StorageError, sqlite3.Error)
try:
    import psycopg
    DB_ERRORS = (*DB_ERRORS, psycopg.Error)
except ImportError:  # the PostgreSQL driver is only needed when that backend is chosen
    pass


def open_storage(path: str, plugins: Mapping[str, Sequence[Any]] | None = None, *,
                 backend: str = "sqlite", dsn: str | None = None, password: str | None = None,
                 timescale: str = "auto") -> Storage:
    """Open the storage backend. `path` is the SQLite database file (or ":memory:"); `dsn`,
    `password` and `timescale` are used only by the PostgreSQL backend."""
    if backend == "sqlite":
        from .sqlite import SqliteStorage
        return SqliteStorage(path, plugins)
    if backend == "postgres":
        if not dsn:
            raise StorageError("the postgres backend needs storage.dsn")
        from .postgres import PgStorage
        return PgStorage(dsn, plugins, password=password, timescale=timescale)
    raise StorageError(f"storage backend {backend!r} is not available in this build")
