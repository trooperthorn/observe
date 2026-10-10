"""Readings an admin chose to ignore on one host (bug plan WP3).

A Super I/O input with nothing wired to it reads 99 degrees, and an empty fan header reads
0 RPM. Neither says anything about the machine, so an admin can ignore a reading by its
`hw.id` on one host. An ignored reading is still collected and still shown, greyed out, but it
no longer counts toward its section or the host's verdict.

The list lives in `app_settings` under `hosts.ignored` ({host: [hw.id, ...]}), and a change is
written in one write unit with the audit row that records the old and new lists, like the tier
rates (observe/tiers.py).
"""

from __future__ import annotations

import json
from typing import Any

from .storage.base import Conn

KEY = "hosts.ignored"
PATH = "/api/hosts/[host]/ignored"
MAX_IDS = 200
MAX_ID_LEN = 256


class IgnoreError(ValueError):
    """A refused change. The message is safe to show to the admin."""


def validate(body: Any) -> list[str]:
    """The new list of hw.id values from `{"ignored": [...]}`, sorted and without repeats."""
    if not isinstance(body, dict) or not isinstance(body.get("ignored"), list):
        raise IgnoreError("send {\"ignored\": [hw.id, ...]}")
    ids = body["ignored"]
    if len(ids) > MAX_IDS:
        raise IgnoreError(f"at most {MAX_IDS} readings can be ignored on one host")
    for i in ids:
        if not isinstance(i, str) or not i.strip() or len(i) > MAX_ID_LEN \
                or any(ord(c) < 32 for c in i):
            raise IgnoreError("each entry must be a hw.id of up to 256 printable characters")
    return sorted(set(ids))


def load(db: Conn) -> dict[str, frozenset[str]]:
    row = db.execute("SELECT value FROM app_settings WHERE key = ?", (KEY,)).fetchone()
    return from_value(row[0] if row else None)


def from_value(value: Any) -> dict[str, frozenset[str]]:
    """`load` over the stored value already read (None when there is none)."""
    try:
        data = json.loads(value) if value else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        return {}
    return {str(h): frozenset(str(i) for i in ids) for h, ids in data.items()
            if isinstance(ids, list) and ids}


def save(db: Conn, host: str, ids: list[str], *, now: float, actor: str,
         remote: str) -> dict[str, list[str]]:
    """Replace one host's list and append the audit row with the old and new lists."""
    current = {h: sorted(v) for h, v in load(db).items()}
    old = current.get(host, [])
    if ids:
        current[host] = ids
    else:
        current.pop(host, None)
    if current:
        db.execute(
            "INSERT INTO app_settings (key, value, updated) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (KEY, json.dumps(current, sort_keys=True), now))
    else:
        db.execute("DELETE FROM app_settings WHERE key = ?", (KEY,))
    db.execute(
        "INSERT INTO audit (ts, actor, kind, method, path, status, remote, detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (now, actor, "host_readings_ignored", "PUT", PATH, 200, remote,
         json.dumps({"host": host, "old": old, "new": ids,
                     "added": sorted(set(ids) - set(old)),
                     "removed": sorted(set(old) - set(ids))}, sort_keys=True)))
    return {"old": old, "new": ids}


async def load_ignored(store: Any) -> dict[str, frozenset[str]]:
    """The lists, cached on the store (observe/hostsettings.py)."""
    from . import hostsettings
    return (await hostsettings.load(store)).ignored
