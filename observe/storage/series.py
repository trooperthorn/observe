"""Series storage: resources, scopes, series, samples, the latest table and the incremental
summary levels (docs/DATA-API-DESIGN.md sections 2.1 to 2.4 and 10.2).

Everything here is portable SQL run inside a write unit, so one module serves SQLite, plain
PostgreSQL and PostgreSQL with TimescaleDB. A point is stored in `samples` with
INSERT ... ON CONFLICT DO NOTHING, and only a point that was newly inserted changes anything else:
a replay or an out-of-order delivery of a point already stored leaves the latest row and every
summary level as it was. A point sent again with a different value replaces the stored value and
the summaries of its buckets are corrected. Where the summary levels are maintained at ingest
(`rollups=True`) each new non-null point is folded into its 5 minute, hourly and daily row in the
same transaction. On TimescaleDB the levels are continuous aggregates over `samples`, so the
writer only stores the point (`rollups=False`).

Timestamps are integer milliseconds. A series is identified by its resource, its scope (for a
pushed host, the source name), its metric and its point attributes. The design leaves the scope out
of the identity; it is part of it until the OpenTelemetry normalizer gives every producer one
metric namespace, because two hostwatch sources may send the same metric name for one host.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .base import Conn

MS_5M, MS_1H, MS_1D = 300_000, 3_600_000, 86_400_000
# (table, bucket width in milliseconds), finest first.
SUMMARY_LEVELS = (("rollup_5m", MS_5M), ("rollup_1h", MS_1H), ("rollup_1d", MS_1D))

# Section 2.1: at most this many series per resource and in all. A point for a series past either
# cap is dropped and counted.
MAX_SERIES_PER_RESOURCE = 2000
MAX_SERIES_TOTAL = 50_000

# The attributes that identify a resource of each kind. Descriptive attributes are stored on the
# row but never change which resource it is.
RESOURCE_IDENTITY: dict[str, tuple[str, ...]] = {
    "host": ("host.name", "host.id"),
    "network_device": ("observe.device",),
    "port": ("observe.switch", "observe.port"),
    "ha_instance": ("service.instance.id",),
    "unifi_client": ("observe.client.mac",),
    "monitor": ("observe.monitor",),
    "field_tester": ("observe.field.device",),
    "service": ("service.name", "host.name"),
}


def canonical(attrs: Mapping[str, Any]) -> str:
    """The canonical JSON of an attribute map: sorted keys, scalar values only."""
    for key, value in attrs.items():
        if not isinstance(key, str) or isinstance(value, (list, tuple, dict)) or value is None:
            raise ValueError(f"attribute {key!r} must be a string, number or boolean")
    return json.dumps(dict(attrs), separators=(",", ":"), sort_keys=True)


def _digest(*parts: str) -> bytes:
    h = hashlib.blake2b(digest_size=16)
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.digest()


def to_ms(ts: float) -> int:
    """A timestamp in seconds as integer milliseconds."""
    return int(round(ts * 1000))


@dataclass(frozen=True, slots=True)
class Point:
    scope: str
    metric: str
    unit: str
    attrs: str  # canonical JSON of the point attributes
    ts_ms: int
    value: float | None


@dataclass(frozen=True, slots=True)
class Recorded:
    new: int  # points stored for the first time
    replaced: int  # points that replaced a stored value
    duplicate: int  # points already stored with the same value
    dropped: int  # points refused by the cardinality guard


# ---- identity -------------------------------------------------------------------------------

def resource_id(db: Conn, kind: str, name: str, now: float,
                attrs: Mapping[str, Any] | None = None) -> int:
    """The id of a resource, created on first use. Only the identifying attributes of its kind
    decide which resource it is; the others are kept on the row."""
    identity = RESOURCE_IDENTITY.get(kind)
    if identity is None:
        raise ValueError(f"unknown resource kind {kind!r}")
    given = dict(attrs or {})
    given.setdefault(identity[0], name)
    key = _digest(kind, canonical({k: given[k] for k in identity if k in given}))
    row = db.execute("SELECT id FROM resources WHERE key_hash = ?", (key,)).fetchone()
    if row is not None:
        db.execute("UPDATE resources SET last_seen = MAX(last_seen, ?) WHERE id = ?", (now, row[0]))
        return int(row[0])
    db.execute(
        "INSERT INTO resources (key_hash, kind, name, attrs, first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT (key_hash) DO NOTHING",
        (key, kind, name, canonical(given), now, now))
    return int(db.execute("SELECT id FROM resources WHERE key_hash = ?", (key,)).fetchone()[0])


def find_resource(db: Conn, kind: str, name: str) -> int | None:
    row = db.execute("SELECT id FROM resources WHERE kind = ? AND name = ? ORDER BY id LIMIT 1",
                     (kind, name)).fetchone()
    return None if row is None else int(row[0])


def scope_id(db: Conn, name: str, version: str = "") -> int:
    row = db.execute("SELECT id FROM scopes WHERE name = ? AND version = ?",
                     (name, version)).fetchone()
    if row is not None:
        return int(row[0])
    db.execute("INSERT INTO scopes (name, version) VALUES (?, ?) "
               "ON CONFLICT (name, version) DO NOTHING", (name, version))
    return int(db.execute("SELECT id FROM scopes WHERE name = ? AND version = ?",
                          (name, version)).fetchone()[0])


class _Counts:
    """The series counts for the cardinality guard, read once per batch and kept up to date."""

    def __init__(self, db: Conn, rid: int) -> None:
        self.resource = int(db.execute("SELECT COUNT(*) FROM series WHERE resource_id = ?",
                                       (rid,)).fetchone()[0])
        self.total = int(db.execute("SELECT COUNT(*) FROM series").fetchone()[0])


def _series_ids(db: Conn, rid: int, points: Sequence[Point], now: float,
                max_per_resource: int, max_total: int) -> dict[tuple[str, str, str], int | None]:
    """The series id of each distinct (scope, metric, attributes) in the points, creating the
    ones that are new. None marks a series the cardinality guard refused."""
    ids: dict[tuple[str, str, str], int | None] = {}
    first: dict[tuple[str, str, str], Point] = {}
    for p in points:
        first.setdefault((p.scope, p.metric, p.attrs), p)
    counts: _Counts | None = None
    scopes: dict[str, int] = {}
    for key, p in first.items():
        scope, metric, attrs = key
        digest = _digest(str(rid), scope, metric, attrs)
        row = db.execute("SELECT id FROM series WHERE key_hash = ?", (digest,)).fetchone()
        if row is not None:
            ids[key] = int(row[0])
            continue
        counts = counts or _Counts(db, rid)
        if counts.resource >= max_per_resource or counts.total >= max_total:
            ids[key] = None
            continue
        sid = scopes.get(scope)
        if sid is None:
            sid = scopes[scope] = scope_id(db, scope)
        seen = p.ts_ms / 1000.0
        db.execute(
            "INSERT INTO series (key_hash, resource_id, scope_id, metric, unit, instrument, "
            "attrs, first_seen, last_seen) VALUES (?,?,?,?,?,'gauge',?,?,?) "
            "ON CONFLICT (key_hash) DO NOTHING", (digest, rid, sid, metric, p.unit, attrs, seen, seen))
        ids[key] = int(db.execute("SELECT id FROM series WHERE key_hash = ?",
                                  (digest,)).fetchone()[0])
        counts.resource += 1
        counts.total += 1
    return ids


# ---- writing points -------------------------------------------------------------------------

def _bump(db: Conn, sid: int, ts_ms: int, value: float) -> None:
    for table, width in SUMMARY_LEVELS:
        db.execute(
            f"INSERT INTO {table} (series_id, bucket, n, sum_v, min_v, max_v) "
            "VALUES (?,?,1,?,?,?) ON CONFLICT (series_id, bucket) DO UPDATE SET "
            f"n = {table}.n + 1, sum_v = {table}.sum_v + excluded.sum_v, "
            f"min_v = MIN({table}.min_v, excluded.min_v), "
            f"max_v = MAX({table}.max_v, excluded.max_v)",
            (sid, ts_ms // width * width, value, value, value))


def _latest(db: Conn, sid: int, ts_ms: int, value: float | None) -> None:
    cur = db.execute(
        "INSERT INTO latest (series_id, ts, value) VALUES (?,?,?) "
        "ON CONFLICT (series_id) DO UPDATE SET prev_ts = latest.ts, prev_value = latest.value, "
        "ts = excluded.ts, value = excluded.value WHERE excluded.ts >= latest.ts",
        (sid, ts_ms, value))
    if cur.rowcount == 0:
        # Older than the newest point: it can still be the one just before it.
        db.execute(
            "UPDATE latest SET prev_ts = ?, prev_value = ? WHERE series_id = ? AND ts > ? "
            "AND (prev_ts IS NULL OR prev_ts < ?)", (ts_ms, value, sid, ts_ms, ts_ms))


def _window(db: Conn, table: str, sid: int, lo: int, hi: int) -> tuple[int, float | None,
                                                                          float | None,
                                                                          float | None]:
    if table == "samples":
        row = db.execute("SELECT COUNT(value), SUM(value), MIN(value), MAX(value) FROM samples "
                         "WHERE series_id = ? AND ts >= ? AND ts < ?", (sid, lo, hi)).fetchone()
    else:
        row = db.execute(f"SELECT COALESCE(SUM(n), 0), SUM(sum_v), MIN(min_v), MAX(max_v) FROM "
                         f"{table} WHERE series_id = ? AND bucket >= ? AND bucket < ?",
                         (sid, lo, hi)).fetchone()
    return int(row[0] or 0), row[1], row[2], row[3]


def _store_bucket(db: Conn, table: str, sid: int, bucket: int,
                  agg: tuple[int, float | None, float | None, float | None]) -> None:
    n, total, low, high = agg
    if n == 0:
        db.execute(f"DELETE FROM {table} WHERE series_id = ? AND bucket = ?", (sid, bucket))
        return
    db.execute(
        f"INSERT INTO {table} (series_id, bucket, n, sum_v, min_v, max_v) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT (series_id, bucket) DO UPDATE SET n = excluded.n, sum_v = excluded.sum_v, "
        "min_v = excluded.min_v, max_v = excluded.max_v", (sid, bucket, n, total, low, high))


def _correct(db: Conn, sid: int, ts_ms: int, old: float | None, new: float | None) -> None:
    dn = (new is not None) - (old is not None)
    ds = (new or 0.0) - (old or 0.0)
    lower: str | None = None
    for table, width in SUMMARY_LEVELS:
        bucket = ts_ms // width * width
        current = db.execute(
            f"SELECT n, sum_v, min_v, max_v FROM {table} WHERE series_id = ? AND bucket = ?",
            (sid, bucket)).fetchone()
        if lower is None:
            # The 5 minute bucket: raw samples still hold every point of it.
            _store_bucket(db, table, sid, bucket, _window(db, "samples", sid, bucket,
                                                          bucket + width))
        else:
            fine = _window(db, lower, sid, bucket, bucket + width)
            if current is not None and int(current[0]) + dn == fine[0]:
                _store_bucket(db, table, sid, bucket, fine)  # the level below holds all of it
            elif current is not None:
                n = int(current[0]) + dn
                lows = [x for x in (current[2], new) if x is not None]
                highs = [x for x in (current[3], new) if x is not None]
                _store_bucket(db, table, sid, bucket,
                              (n, (current[1] or 0.0) + ds, min(lows) if lows else None,
                               max(highs) if highs else None))
        lower = table


def _replace(db: Conn, sid: int, ts_ms: int, old: float | None, new: float | None,
             rollups: bool) -> None:
    db.execute("UPDATE samples SET value = ? WHERE series_id = ? AND ts = ?", (new, sid, ts_ms))
    db.execute("UPDATE latest SET value = ? WHERE series_id = ? AND ts = ?", (new, sid, ts_ms))
    db.execute("UPDATE latest SET prev_value = ? WHERE series_id = ? AND prev_ts = ?",
               (new, sid, ts_ms))
    if rollups:
        _correct(db, sid, ts_ms, old, new)


def record_points(db: Conn, *, kind: str, name: str, points: Iterable[Point], now: float,
                  rollups: bool, attrs: Mapping[str, Any] | None = None,
                  max_per_resource: int = MAX_SERIES_PER_RESOURCE,
                  max_total: int = MAX_SERIES_TOTAL) -> Recorded:
    """Store the points of one resource inside a write unit. `rollups` says whether the summary
    levels are maintained here (False on TimescaleDB, where continuous aggregates build them)."""
    pts = list(points)
    rid = resource_id(db, kind, name, now, attrs)
    ids = _series_ids(db, rid, pts, now, max_per_resource, max_total)
    new = replaced = duplicate = dropped = 0
    for p in pts:
        sid = ids[(p.scope, p.metric, p.attrs)]
        if sid is None:
            dropped += 1
            continue
        cur = db.execute("INSERT INTO samples (series_id, ts, value) VALUES (?,?,?) "
                         "ON CONFLICT (series_id, ts) DO NOTHING", (sid, p.ts_ms, p.value))
        if cur.rowcount == 1:
            new += 1
            _latest(db, sid, p.ts_ms, p.value)
            if rollups and p.value is not None:
                _bump(db, sid, p.ts_ms, p.value)
            continue
        stored = db.execute("SELECT value FROM samples WHERE series_id = ? AND ts = ?",
                            (sid, p.ts_ms)).fetchone()
        old = None if stored is None else stored[0]
        if old == p.value:
            duplicate += 1  # a replay changes nothing
        else:
            replaced += 1
            _replace(db, sid, p.ts_ms, old, p.value, rollups)
    return Recorded(new, replaced, duplicate, dropped)


# ---- removing a resource --------------------------------------------------------------------

def remove_resource(db: Conn, kind: str, name: str, *, rollups: bool) -> int:
    """Delete a resource with its series, samples, latest rows and (where the writer maintains
    them) summary rows. Returns the number of samples removed. On TimescaleDB the continuous
    aggregates cannot be deleted from; their buckets for the removed series are left to
    retention, and the views join the series table, so they no longer appear."""
    rid = find_resource(db, kind, name)
    if rid is None:
        return 0
    mine = "series_id IN (SELECT id FROM series WHERE resource_id = ?)"
    removed = db.execute(f"DELETE FROM samples WHERE {mine}", (rid,)).rowcount
    db.execute(f"DELETE FROM latest WHERE {mine}", (rid,))
    if rollups:
        for table, _ in SUMMARY_LEVELS:
            db.execute(f"DELETE FROM {table} WHERE {mine}", (rid,))
    db.execute("DELETE FROM series WHERE resource_id = ?", (rid,))
    db.execute("DELETE FROM resources WHERE id = ?", (rid,))
    return max(removed, 0)
