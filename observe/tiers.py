"""Polling tiers (docs/DATA-API-DESIGN.md section 10.1): the default rate of each tier, the
global rates an admin can change and one optional override per host, and the effective rates an
agent is told.

The values live in `app_settings` on both backends (`tiers.global`, `tiers.hosts`) and are
written inside one write unit that also appends the audit row, so a change and its record are
never apart. Each rate must be a number inside the bounds of its tier, so a host cannot be asked
to poll harder than it can bear. A host override must name a host that has an ingest key. For one
host the effective rate is its override, then the saved global value, then the default.
"""

from __future__ import annotations

import json
from typing import Any

from .storage.base import Conn

PATH = "/api/admin/tiers"
GLOBAL_KEY = "tiers.global"
HOSTS_KEY = "tiers.hosts"
# tier -> (label, default seconds, lowest, highest)
TIERS: dict[str, tuple[str, float, float, float]] = {
    "availability": ("Availability", 30.0, 5.0, 3600.0),
    "device_metrics": ("Device metrics", 60.0, 10.0, 3600.0),
    "storage_health": ("Storage health", 900.0, 60.0, 86400.0),
    "smart": ("SMART", 3600.0, 300.0, 86400.0),
    "inventory": ("Inventory", 3600.0, 600.0, 86400.0),
}
DEFAULTS = {name: spec[1] for name, spec in TIERS.items()}


class TierError(ValueError):
    """A refused change. The message is safe to show to the admin."""


def _rate(name: str, raw: Any) -> float:
    _, _, low, high = TIERS[name]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TierError(f"{name} must be a number of seconds")
    value = float(raw)
    if not low <= value <= high:
        raise TierError(f"{name} must be from {low:g} to {high:g} seconds")
    return value


def _rates(raw: Any, what: str, *, null_resets: bool) -> dict[str, float | None]:
    if not isinstance(raw, dict):
        raise TierError(f"{what} must be an object of tier to seconds")
    unknown = sorted(str(k) for k in raw if k not in TIERS)
    if unknown:
        raise TierError(f"{what} has no tier: {', '.join(unknown)[:80]}")
    return {k: (None if raw[k] is None and null_resets else _rate(k, raw[k])) for k in raw}


def validate(body: Any, hosts: set[str]) -> tuple[dict[str, float | None], dict | None]:
    """(global changes, replacement host overrides or None). A global value of null resets it to
    the default. `hosts` replaces the whole per-host map; {} or null clears it."""
    if not isinstance(body, dict) or not body:
        raise TierError("send a JSON object with the rates to change")
    unknown = sorted(str(k) for k in body if k not in ("global", "hosts"))
    if unknown:
        raise TierError(f"unknown setting: {', '.join(unknown)[:80]}")
    changes: dict[str, float | None] = {}
    if "global" in body:
        changes = _rates(body["global"], "global", null_resets=True)
    overrides = None
    if "hosts" in body:
        raw = {} if body["hosts"] is None else body["hosts"]
        if not isinstance(raw, dict):
            raise TierError("hosts must be an object of host name to rates")
        overrides = {}
        for host, values in raw.items():
            if host not in hosts:
                raise TierError(f"no host named {str(host)[:64]}")
            if not isinstance(values, dict) or not values:
                raise TierError(f"the override for {str(host)[:64]} must set at least one rate")
            overrides[host] = _rates(values, str(host)[:64], null_resets=False)
    return changes, overrides


def _stored(raw: Any, name: str) -> float | None:
    try:
        return _rate(name, float(raw))
    except (TypeError, ValueError, TierError):
        return None


def _json(value: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def load(db: Conn) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """The saved global rates and per-host overrides; a malformed or out-of-range stored value is
    left out."""
    glob: dict[str, float] = {}
    hosts: dict[str, dict[str, float]] = {}
    for key, value in db.execute("SELECT key, value FROM app_settings WHERE key LIKE 'tiers.%'"):
        data = _json(value)
        if not isinstance(data, dict):
            continue
        if key == GLOBAL_KEY:
            glob = {n: v for n, raw in data.items()
                    if n in TIERS and (v := _stored(raw, n)) is not None}
        elif key == HOSTS_KEY:
            for host, values in data.items():
                if not isinstance(values, dict):
                    continue
                kept = {n: v for n, raw in values.items()
                        if n in TIERS and (v := _stored(raw, n)) is not None}
                if kept:
                    hosts[str(host)] = kept
    return glob, hosts


def effective(host: str, glob: dict[str, float],
              hosts: dict[str, dict[str, float]]) -> dict[str, float]:
    """The rates one host is told: its override, then the saved global value, then the default."""
    own = hosts.get(host, {})
    return {n: own.get(n, glob.get(n, DEFAULTS[n])) for n in TIERS}


def describe(glob: dict[str, float], hosts: dict[str, dict[str, float]],
             known: list[str]) -> dict:
    """The saved rates with the bounds and hosts an admin form needs."""
    return {
        "global": {n: glob.get(n, DEFAULTS[n]) for n in TIERS},
        "saved": {n: glob.get(n) for n in TIERS},
        "hosts": {h: dict(sorted(v.items())) for h, v in sorted(hosts.items())},
        "bounds": {n: {"label": s[0], "default": s[1], "min": s[2], "max": s[3]}
                   for n, s in TIERS.items()},
        "known_hosts": sorted(known),
    }


def known_hosts(db: Conn) -> list[str]:
    """Hosts that have an unrevoked host ingest key."""
    return [r[0] for r in db.execute(
        "SELECT DISTINCT host FROM ingest_keys WHERE scope = 'wpi' AND revoked_at IS NULL")]


def save(db: Conn, changes: dict[str, float | None], overrides: dict | None, *,
         now: float, actor: str, remote: str) -> dict:
    """Inside one write unit: write the changed keys, read the values before and after and
    append the one audit row that records both. Returns {"old": ..., "new": ...}."""
    def view() -> dict:
        glob, hosts = load(db)
        return {"global": dict(sorted(glob.items())),
                "hosts": {h: dict(sorted(v.items())) for h, v in sorted(hosts.items())}}

    old = view()
    writes: dict[str, str | None] = {}
    if changes:
        merged = dict(old["global"])
        for n, v in changes.items():
            if v is None:
                merged.pop(n, None)
            else:
                merged[n] = v
        writes[GLOBAL_KEY] = json.dumps(merged, sort_keys=True) if merged else None
    if overrides is not None:
        writes[HOSTS_KEY] = json.dumps(overrides, sort_keys=True) if overrides else None
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
        (now, actor, "tier_rates_changed", "PUT", PATH, 200, remote,
         json.dumps({"old": old, "new": new}, sort_keys=True)))
    return {"old": old, "new": new}
