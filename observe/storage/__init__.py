"""Database access for Observe. Everything outside this package talks to `Storage`."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from .base import (CHANGE_DOMAINS, Conn, IntegrityConflict, Storage, StorageBusy, StorageError,
                   StorageTimeout)

__all__ = ["CHANGE_DOMAINS", "Conn", "DB_ERRORS", "IntegrityConflict", "Storage", "StorageBusy",
           "StorageError", "StorageTimeout", "open_storage"]

# Every error a unit of work can meet from a driver or from the storage layer, so code that
# tolerates a failed statement can name it without importing a driver.
DB_ERRORS: tuple[type[Exception], ...] = (StorageError, sqlite3.Error)


def open_storage(path: str, plugins: Mapping[str, Sequence[Any]] | None = None, *,
                 backend: str = "sqlite") -> Storage:
    """Open the storage backend. `path` is the SQLite database file (or ":memory:")."""
    if backend == "sqlite":
        from .sqlite import SqliteStorage
        return SqliteStorage(path, plugins)
    raise StorageError(f"storage backend {backend!r} is not available in this build")
