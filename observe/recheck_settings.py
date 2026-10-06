"""Admin settings for the fast re-check (docs/DATA-API-DESIGN.md section 10.3): the re-check
window, the re-check interval and the number of good replies in a row that return a monitor to
Up, as one global value and as an override per monitor.

The values live in `app_settings` on both backends and are written inside one write unit that
also appends the audit row, so a change and its record are never apart. Nothing here trusts the
request: each value must be a number inside the bounds of its setting, and an override must name
a monitor that exists. The scheduler reads the values through `load` and uses `resolve`: a saved
per-monitor override beats the value in the monitor's own config entry, which beats the saved
global value, which beats the config default.
"""

from __future__ import annotations

import json
from typing import Any

from .config import MIN_RECHECK_INTERVAL
from .storage.base import Conn

PATH = "/api/admin/recheck"
OVERRIDES_KEY = "recheck.overrides"
# field -> (settings key, config field, lowest, highest, whole number)
FIELDS = {
    "window": ("recheck.window", "recheck_window", 0.0, 86400.0, False),
    "interval": ("recheck.interval", "recheck_interval", MIN_RECHECK_INTERVAL, 3600.0, False),
    "good": ("recheck.good", "recheck_good", 1, 20, True),
}
LABELS = {"window": "Re-check window (seconds)", "interval": "Re-check interval (seconds)",
          "good": "Good replies in a row"}
CONFIG_FIELD = {name: spec[1] for name, spec in FIELDS.items()}


class RecheckError(ValueError):
    """A refused change. The message is safe to show to the admin."""


def _value(name: str, raw: Any) -> float | int:
    _, _, low, high, whole = FIELDS[name]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise RecheckError(f"{name} must be a number")
    if whole and not float(raw).is_integer():
        raise RecheckError(f"{name} must be a whole number")
    value = int(raw) if whole else float(raw)
    if not low <= value <= high:
        raise RecheckError(f"{name} must be from {low:g} to {high:g}")
    return value


def _fields_of(raw: dict[str, Any], what: str, *, null_resets: bool) -> dict[str, Any]:
    unknown = sorted(str(k) for k in raw if k not in FIELDS)
    if unknown:
        raise RecheckError(f"{what} cannot set: {', '.join(unknown)[:80]}")
    return {k: (None if raw[k] is None and null_resets else _value(k, raw[k])) for k in raw}


def validate(body: Any, slugs: set[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]] | None]:
    """(global changes, replacement overrides or None). A global value of null resets it to the
    config default. `overrides` replaces the whole per-monitor map; {} or null clears it."""
    if not isinstance(body, dict) or not body:
        raise RecheckError("send a JSON object with the settings to change")
    unknown = sorted(str(k) for k in body if k not in (*FIELDS, "overrides"))
    if unknown:
        raise RecheckError(f"unknown setting: {', '.join(unknown)[:80]}")
    changes = _fields_of({k: v for k, v in body.items() if k != "overrides"}, "settings",
                         null_resets=True)
    overrides = None
    if "overrides" in body:
        raw = {} if body["overrides"] is None else body["overrides"]
        if not isinstance(raw, dict):
            raise RecheckError("overrides must be an object of monitor slug to settings")
        overrides = {}
        for slug, values in raw.items():
            if slug not in slugs:
                raise RecheckError(f"no monitor named {str(slug)[:64]}")
            if not isinstance(values, dict) or not values:
                raise RecheckError(f"the override for {slug} must set at least one value")
            overrides[slug] = _fields_of(values, slug, null_resets=False)
    return changes, overrides


def _stored(value: Any, name: str) -> float | int | None:
    try:
        return _value(name, float(value))
    except (TypeError, ValueError, RecheckError):
        return None


def load(db: Conn) -> tuple[dict[str, float | int], dict[str, dict[str, float | int]]]:
    """The saved global values and per-monitor overrides; a malformed or out-of-range stored
    value is left out."""
    glob: dict[str, float | int] = {}
    overrides: dict[str, dict[str, float | int]] = {}
    keys = {spec[0]: name for name, spec in FIELDS.items()}
    for key, value in db.execute("SELECT key, value FROM app_settings WHERE key LIKE 'recheck.%'"):
        if key == OVERRIDES_KEY:
            try:
                data = json.loads(value)
            except (TypeError, ValueError):
                continue
            for slug, values in (data.items() if isinstance(data, dict) else ()):
                if not isinstance(values, dict):
                    continue
                kept = {n: v for n, raw in values.items()
                        if n in FIELDS and (v := _stored(raw, n)) is not None}
                if kept:
                    overrides[str(slug)] = kept
        elif key in keys:
            v = _stored(value, keys[key])
            if v is not None:
                glob[keys[key]] = v
    return glob, overrides


def resolve(config: Any, monitor: Any, name: str, glob: dict[str, Any],
            overrides: dict[str, dict[str, Any]]) -> float | int:
    """The value the engine uses for one monitor: its saved override, then its own config value,
    then the saved global value, then the config default."""
    saved = overrides.get(monitor.slug, {}).get(name)
    if saved is not None:
        return saved
    own = getattr(monitor, CONFIG_FIELD[name])
    if own is not None:
        return own
    if name in glob:
        return glob[name]
    return getattr(config.defaults, CONFIG_FIELD[name])


def describe(config: Any, glob: dict[str, Any], overrides: dict[str, dict[str, Any]]) -> dict:
    """The saved values with the bounds, defaults and monitors an admin form needs."""
    return {
        "settings": {n: glob.get(n, getattr(config.defaults, CONFIG_FIELD[n])) for n in FIELDS},
        "saved": {n: glob.get(n) for n in FIELDS},
        "overrides": {s: dict(sorted(v.items())) for s, v in sorted(overrides.items())},
        "bounds": {n: {"min": spec[2], "max": spec[3],
                       "default": getattr(config.defaults, spec[1])} for n, spec in FIELDS.items()},
        "monitors": [{"slug": m.slug, "name": m.name} for m in config.monitors],
    }


def save(db: Conn, changes: dict[str, Any], overrides: dict[str, dict[str, Any]] | None, *,
         now: float, actor: str, remote: str) -> dict:
    """Inside one write unit: write the changed keys, read the values before and after and
    append the one audit row that records both. Returns {"old": ..., "new": ...}."""
    def view() -> dict:
        glob, ov = load(db)
        return {"settings": dict(sorted(glob.items())),
                "overrides": {s: dict(sorted(v.items())) for s, v in sorted(ov.items())}}

    old = view()
    writes: dict[str, str | None] = {FIELDS[n][0]: None if v is None else str(v)
                                     for n, v in changes.items()}
    if overrides is not None:
        writes[OVERRIDES_KEY] = json.dumps(overrides, sort_keys=True) if overrides else None
    for key, value in writes.items():
        if value is None:
            db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            db.execute(
                "INSERT INTO app_settings (key, value, updated) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (key, value, now))
    new = view()
    db.execute(
        "INSERT INTO audit (ts, actor, kind, method, path, status, remote, detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (now, actor, "recheck_settings_changed", "PUT", PATH, 200, remote,
         json.dumps({"old": old, "new": new}, sort_keys=True)))
    return {"old": old, "new": new}
