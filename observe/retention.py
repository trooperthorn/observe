"""Admin retention, compaction and rollup settings (docs/DATA-API-DESIGN.md section 10.2).

The settings are validated here and written by `Storage.save_retention_settings`, which records
the old and new values in the audit log in the same transaction. Nothing here trusts the
request: every value must be a whole number inside the bounds of its setting.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .storage import rollups
from .store import Store

PATH = "/api/admin/retention"
METRIC_NAME = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
GLOBAL_FIELDS = tuple(rollups.FIELD_BOUNDS)


class RetentionError(ValueError):
    """A refused change. The message is safe to show to the admin."""


def _days(name: str, value: Any) -> int:
    low, high = rollups.FIELD_BOUNDS[name]
    if type(value) is not int:
        raise RetentionError(f"{name} must be a whole number")
    if not low <= value <= high:
        raise RetentionError(f"{name} must be from {low} to {high}")
    return value


def validate(body: Any) -> dict[str, str | None]:
    """The app_settings changes a request asks for (None resets a setting to its default).
    Raises RetentionError for anything unknown, mistyped or out of bounds."""
    if not isinstance(body, dict) or not body:
        raise RetentionError("send a JSON object with the settings to change")
    unknown = sorted(str(k) for k in body if k not in (*GLOBAL_FIELDS, "overrides"))
    if unknown:
        raise RetentionError(f"unknown setting: {', '.join(unknown)[:80]}")
    changes: dict[str, str | None] = {}
    for name in GLOBAL_FIELDS:
        if name not in body:
            continue
        key = rollups.FIELD_KEYS[name]
        changes[key] = None if body[name] is None else str(_days(name, body[name]))
    if "overrides" in body:
        changes[rollups.OVERRIDES_KEY] = _overrides(body["overrides"])
    return changes


def _overrides(raw: Any) -> str | None:
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise RetentionError("overrides must be an object of metric name to levels")
    if len(raw) > rollups.MAX_OVERRIDES:
        raise RetentionError(f"at most {rollups.MAX_OVERRIDES} metrics can have an override")
    out: dict[str, dict[str, int]] = {}
    for metric, levels in raw.items():
        if not isinstance(metric, str) or not METRIC_NAME.match(metric):
            raise RetentionError("a metric name is 1 to 64 letters, digits or _ . : / -")
        if not isinstance(levels, dict) or not levels:
            raise RetentionError(f"the override for {metric} must name at least one level")
        bad = sorted(str(k) for k in levels if k not in rollups.OVERRIDE_FIELDS)
        if bad:
            raise RetentionError(f"{metric} cannot override: {', '.join(bad)[:80]}")
        out[metric] = {name: _days(name, days) for name, days in levels.items()}
    return json.dumps(out, sort_keys=True)


def describe(levels: rollups.RetentionLevels) -> dict[str, Any]:
    """The saved settings (`load_levels(..., as_saved=True)`) with the bounds and defaults an
    admin form needs, the order the levels must keep and any place the saved values break it.
    Compaction uses the levels lifted into order (`rollups.ordered`) until they are fixed."""
    default = rollups.RetentionLevels()
    return {
        "settings": rollups.settings_view(levels),
        "order": list(rollups.ORDERED_FIELDS),
        "problems": rollups.order_problems(levels),
        "bounds": {n: {"min": lo, "max": hi, "default": getattr(default, n)}
                   for n, (lo, hi) in rollups.FIELD_BOUNDS.items()},
        "override_fields": list(rollups.OVERRIDE_FIELDS),
        "max_overrides": rollups.MAX_OVERRIDES,
    }


async def read_settings(store: Store, fallback_raw_days: int) -> dict[str, Any]:
    levels = await store.storage.read(
        lambda db: rollups.load_levels(db, fallback_raw_days, as_saved=True))
    return describe(levels)


async def update_settings(store: Store, body: Any, *, actor: str, remote: str, now: float,
                          fallback_raw_days: int) -> dict[str, Any]:
    """Validate, write and audit. Raises RetentionError before anything is written, also when
    the saved levels would be out of order (checked in the write unit, against the stored values
    the change does not touch)."""
    changes = validate(body)
    try:
        await store.storage.save_retention_settings(
            changes, now=now, actor=actor, remote=remote, path=PATH,
            fallback_raw_days=fallback_raw_days)
    except rollups.RetentionOrderError as err:
        raise RetentionError(f"{err}; each level must keep at least as long as the one before "
                             "it (raw, 5 minute, hourly, daily)") from None
    return await read_settings(store, fallback_raw_days)
