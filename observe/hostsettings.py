"""The per-host settings a host view and the pushed_host check read on every request: the polling
tier rates (observe/tiers.py) and the ignored readings (observe/ignored.py).

Both are read in one statement batch and kept on the store until the `admin` change counter
moves, which every save of either bumps, so a change applies on the next check without a
restart and an unchanged page reads nothing. A store without a storage backend (a test double)
has the defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import ignored, tiers
from .storage.base import Conn


@dataclass(frozen=True)
class HostSettings:
    glob: dict[str, float] = field(default_factory=dict)
    hosts: dict[str, dict[str, float]] = field(default_factory=dict)
    ignored: dict[str, frozenset[str]] = field(default_factory=dict)


def _read(db: Conn) -> HostSettings:
    rows = db.execute("SELECT key, value FROM app_settings WHERE key LIKE 'tiers.%' OR key = ?",
                      (ignored.KEY,)).fetchall()
    glob, hosts = tiers.from_rows(r for r in rows if r[0] != ignored.KEY)
    value = next((r[1] for r in rows if r[0] == ignored.KEY), None)
    return HostSettings(glob, hosts, ignored.from_value(value))


async def load(store: Any) -> HostSettings:
    storage = getattr(store, "storage", None)
    if storage is None:
        return HostSettings()
    try:
        seq = storage.change_seqs().get("admin")
    except (AttributeError, NotImplementedError):
        seq = None
    kept = getattr(store, "_host_settings", None)
    if seq is not None and kept is not None and kept[0] == seq:
        return kept[1]
    got = await storage.read(_read)
    if seq is not None:
        store._host_settings = (seq, got)
    return got
