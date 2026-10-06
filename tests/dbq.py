"""Test helper: run one SQL statement through the storage writer and return its rows."""

from __future__ import annotations

from typing import Any

from observe.store import Store


def run_sql(store: Store, query: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    return store.storage.write_sync(lambda db: db.execute(query, args).fetchall())
