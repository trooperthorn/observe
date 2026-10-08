"""Metrics: the catalogue, the latest values and the time series query (docs/DATA-API-DESIGN.md
sections 4.2, 4.3 and 10.2).

The query chooses the level of data from the range and the step, so the cost of a chart does
not grow with how long the data has been kept:

* raw samples for a step under 300 seconds,
* the 5 minute level for a step under an hour,
* the hourly level for a step under a day,
* the daily level beyond,

moving to a coarser level when the finer one no longer holds the start of the range (the
admin's retention settings decide, including a metric's own override: the shortest retention of
any selected metric counts). When even the level used no longer holds the start for a selected
metric, `complete` is false and the note says the earliest part is missing. A summarised level
always answers with min, max, avg and count, so a week or a month shows peaks and not only averages. A response holds at most 1,000
points per series (the step is raised to fit) and at most 50 series.

Not done yet: a query does not mix levels inside one response (it uses the one level that covers
the whole range), and `rate` needs sum series, which no producer writes until the
OpenTelemetry normalizer exists.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from fastapi import Query, Request

from ..storage import rollups
from .cursor import PageParams, encode
from .models import (LatestPage, MetricCatalogPage, MetricQuery, QueryOut)
from .problems import ApiProblem
from .registry import ApiContext, ApiRegistry
from .timeparse import parse_time

MAX_POINTS = 1000
AUTO_POINTS = 500
MAX_SERIES = 50
MAX_SCAN = 20_000  # series rows read while filtering by attribute
MAX_ATTR_ROWS = 5000
MAX_GLOB = 64  # characters in a ~ pattern
MAX_VALUE = 256  # characters of an attribute value that a ~ pattern reads
AGGS = ("avg", "min", "max", "sum", "count", "last")
DEFAULT_AGGS = ("avg", "min", "max")
# step below, level, table, bucket width in seconds, retention field
TIERS = (
    (300, "raw", None, 1, "raw_days"),
    (3600, "rollup_5m", "rollup_5m", rollups.WIDTH_5M, "rollup_5m_days"),
    (86400, "rollup_1h", "rollup_1h", rollups.WIDTH_1H, "hourly_days"),
    (math.inf, "rollup_1d", "rollup_1d", rollups.WIDTH_1D, "daily_days"),
)


def finite(v: Any) -> Any:
    """The number, or None when it is infinite or not a number."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---- the selector ---------------------------------------------------------------------------

class Selector:
    def __init__(self, metric: str | None, scope: str | None, resource: str | None,
                 kind: str | None, series_id: list[int] | None,
                 match: dict[str, str] | None) -> None:
        self.metric, self.scope, self.resource, self.kind = metric, scope, resource, kind
        self.series_id = series_id
        self.tests = [_attr_test(k, v) for k, v in (match or {}).items()]
        if len(self.tests) > 16:
            raise ApiProblem(400, "at most 16 match terms")

    def sql(self) -> tuple[str, list[Any]]:
        where, args = ["1=1"], []
        if self.metric:
            if self.metric.endswith("*"):
                where.append("s.metric LIKE ? ESCAPE '\\'")
                args.append(_like(self.metric[:-1]) + "%")
            else:
                where.append("s.metric = ?")
                args.append(self.metric)
        for column, value in (("sc.name", self.scope), ("r.name", self.resource),
                              ("r.kind", self.kind)):
            if value:
                where.append(f"{column} = ?")
                args.append(value)
        if self.series_id:
            where.append(f"s.id IN ({','.join('?' * len(self.series_id))})")
            args += self.series_id
        return " AND ".join(where), args

    def keeps(self, attrs: dict[str, Any]) -> bool:
        return all(test(attrs) for test in self.tests)


def glob_match(pattern: str, text: str) -> bool:
    """Whole-value match where `*` is any run of characters and `?` is one character. It keeps
    one restart point (the last `*`), so the cost is at most len(pattern) * len(text) steps and
    there is no exponential case."""
    p = t = 0
    star = mark = -1
    while t < len(text):
        if p < len(pattern) and pattern[p] != "*" and (pattern[p] == "?" or pattern[p] == text[t]):
            p += 1
            t += 1
        elif p < len(pattern) and pattern[p] == "*":
            star, mark = p, t
            p += 1
        elif star != -1:
            mark += 1
            p, t = star + 1, mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def _attr_test(key: str, value: str) -> Any:
    """One `match` term. A key ending in ! is not-equal, a value starting with ~ is a pattern
    (`*` any run of characters, `?` one character, matched against the whole value, so `abc*` is
    a prefix match; at most 64 characters, run by a linear-time matcher over the first 256
    characters of the value), anything else is equal."""
    negate = key.endswith("!")
    key = key[:-1] if negate else key
    if not key or len(key) > 128 or len(value) > 1024:
        raise ApiProblem(400, "a match term is too long")
    if value.startswith("~"):
        pattern = value[1:]
        if len(pattern) > MAX_GLOB:
            raise ApiProblem(400, f"the pattern for {key[:40]!r} is not allowed: at most "
                                  f"{MAX_GLOB} characters, with * and ? as the only wildcards")

        def test(attrs: dict[str, Any]) -> bool:
            have = attrs.get(key)
            hit = have is not None and glob_match(pattern, str(have)[:MAX_VALUE])
            return not hit if negate else hit
        return test

    def equal(attrs: dict[str, Any]) -> bool:
        have = attrs.get(key)
        same = have is not None and str(have) == value
        return not same if negate else (same if have is not None else False)
    return equal


def _selection(db: Any, sel: Selector, want: int, after: int | None = None,
               ) -> tuple[list[tuple[Any, ...]], int | None]:
    """Up to `want` series rows that match, in id order, starting after series id `after`:
    (id, scope, metric, unit, attrs, resource id, kind, name). The second value is None when
    the scan reached the end or filled `want`; when the scan limit stopped it early it is the
    last series id read, so the caller can offer it as the next cursor."""
    where, args = sel.sql()
    out: list[tuple[Any, ...]] = []
    scanned = 0
    last = after if after is not None else 0
    while len(out) < want:
        if scanned >= MAX_SCAN:
            return out, last
        rows = db.execute(
            "SELECT s.id, sc.name, s.metric, s.unit, s.attrs, r.id, r.kind, r.name FROM series s "
            "JOIN resources r ON r.id = s.resource_id JOIN scopes sc ON sc.id = s.scope_id "
            f"WHERE {where} AND s.id > ? ORDER BY s.id LIMIT ?", (*args, last, 500)).fetchall()
        if not rows:
            break
        for row in rows:
            scanned += 1
            last = row[0]
            if not sel.tests or sel.keeps(json.loads(row[4])):
                out.append(row)
                if len(out) >= want:
                    break
    return out, None


def _series_info(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"id": row[0], "scope": row[1], "metric": row[2], "unit": row[3],
            "resource": {"id": row[5], "kind": row[6], "name": row[7]},
            "attrs": json.loads(row[4])}


def _need_selector(sel: Selector) -> None:
    if not (sel.metric or sel.series_id):
        raise ApiProblem(400, "name a metric or a series_id")


# ---- catalogue ------------------------------------------------------------------------------

def list_metrics(db: Any, page: PageParams,
                 q: str | None = Query(None, max_length=128, description="Text in the metric name"),
                 scope: str | None = Query(None, max_length=128),
                 kind: str | None = Query(None, max_length=32, description="Resource kind"),
                 ) -> dict[str, Any]:
    """The metrics that have data: name, unit, how many series and resources, and the attribute
    keys their series carry."""
    where, args = ["1=1"], []
    if q:
        where.append("s.metric LIKE ? ESCAPE '\\'")
        args.append("%" + _like(q) + "%")
    if scope:
        where.append("sc.name = ?")
        args.append(scope)
    if kind:
        where.append("r.kind = ?")
        args.append(kind)
    after = page.after(str, str)
    if after is not None:
        where.append("(sc.name > ? OR (sc.name = ? AND s.metric > ?))")
        args += [after[0], after[0], after[1]]
    rows = db.execute(
        "SELECT sc.name, s.metric, MIN(s.unit), COUNT(*), COUNT(DISTINCT s.resource_id) "
        "FROM series s JOIN scopes sc ON sc.id = s.scope_id JOIN resources r ON r.id = s.resource_id "
        f"WHERE {' AND '.join(where)} GROUP BY sc.name, s.metric ORDER BY sc.name, s.metric "
        "LIMIT ?", (*args, page.limit + 1)).fetchall()
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    keys: dict[tuple[str, str], set[str]] = {(r[0], r[1]): set() for r in rows}
    if rows:
        names = sorted({r[1] for r in rows})
        for sname, metric, attrs in db.execute(
                "SELECT DISTINCT sc.name, s.metric, s.attrs FROM series s JOIN scopes sc "
                f"ON sc.id = s.scope_id WHERE s.metric IN ({','.join('?' * len(names))}) LIMIT ?",
                (*names, MAX_ATTR_ROWS)).fetchall():
            if (sname, metric) in keys:
                keys[(sname, metric)].update(json.loads(attrs))
    items = [{"scope": r[0], "metric": r[1], "unit": r[2], "series_count": int(r[3]),
              "resource_count": int(r[4]), "attribute_keys": sorted(keys[(r[0], r[1])])}
             for r in rows]
    return {"items": items,
            "next_cursor": encode([rows[-1][0], rows[-1][1]]) if more else None}


# ---- latest ---------------------------------------------------------------------------------

def _match_params(request: Request) -> dict[str, str]:
    """The `match[key]=value` terms of a query string. `match[key]!=value` arrives as the name
    `match[key]!`, and `match[key]=~pattern` as the value `~pattern`."""
    out: dict[str, str] = {}
    for name, value in request.query_params.multi_items():
        if not name.startswith("match[") or not (name.endswith("]") or name.endswith("]!")):
            continue
        inner = name[6:]
        if inner.endswith("]!"):
            out[inner[:-2] + "!"] = value
        else:
            out[inner[:-1]] = value
    return out


def latest_metrics(db: Any, request: Request, page: PageParams,
                   metric: str | None = Query(None, max_length=128,
                                              description="Exact, or a prefix ending in *"),
                   scope: str | None = Query(None, max_length=128),
                   resource: str | None = Query(None, max_length=128,
                                                description="Resource name"),
                   kind: str | None = Query(None, max_length=32, description="Resource kind"),
                   series_id: list[int] | None = Query(None, max_length=MAX_SERIES)
                   ) -> dict[str, Any]:
    """The newest point of each series that matches, with the one before it, so a counter's rate
    can be worked out. Attribute terms are written match[key]=value, match[key]!=value and
    match[key]=~pattern."""
    sel = Selector(metric, scope, resource, kind, series_id, _match_params(request))
    _need_selector(sel)
    after = page.after(int)
    rows, resume = _selection(db, sel, page.limit + 1, after[0] if after else None)
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    next_id = rows[-1][0] if more and rows else resume
    latest = {}
    if rows:
        ids = [r[0] for r in rows]
        for sid, ts, value, pts, pval in db.execute(
                "SELECT series_id, ts, value, prev_ts, prev_value FROM latest "
                f"WHERE series_id IN ({','.join('?' * len(ids))})", tuple(ids)).fetchall():
            latest[sid] = (ts, value, pts, pval)
    items = []
    for row in rows:
        if row[0] not in latest:
            continue
        ts, value, pts, pval = latest[row[0]]
        items.append({"series_id": row[0], "scope": row[1], "metric": row[2], "unit": row[3],
                      "resource": {"id": row[5], "kind": row[6], "name": row[7]},
                      "attrs": json.loads(row[4]), "ts": ts / 1000.0, "value": finite(value),
                      "previous_ts": None if pts is None else pts / 1000.0,
                      "previous_value": finite(pval)})
    return {"items": items, "next_cursor": encode([next_id]) if next_id is not None else None}


# ---- query ----------------------------------------------------------------------------------

def _kept_days(levels: rollups.RetentionLevels, names: Sequence[str], field: str) -> int:
    """The days a level keeps for a query: the shortest any selected metric keeps it (its own
    override, else the global level), so no selected series is served from a level that has
    already trimmed the start of the range. With no metric named it is the global level."""
    return min([getattr(levels, field), *(rollups.days_for(levels, m, field) for m in names)])


def _plan(levels: rollups.RetentionLevels, now: float, start: float, end: float,
          step: int | None, names: Sequence[str] = ()
          ) -> tuple[str, str | None, int, int, str | None]:
    """(level name, table, step in seconds, bucket width in seconds, note). `names` are the
    metrics of the selected series, whose per-metric retention overrides are honoured."""
    span = end - start
    if span <= 0:
        raise ApiProblem(400, "to must be after from")
    note = None
    least = max(1, math.ceil(span / (MAX_POINTS - 1)))
    wanted = step if step is not None else max(1, math.ceil(span / AUTO_POINTS))
    chosen = max(wanted, least)
    if step is not None and chosen > step:
        note = f"the step was raised from {step} to {chosen} seconds to keep at most " \
               f"{MAX_POINTS} points"
    index = next(i for i, t in enumerate(TIERS) if chosen < t[0])
    first = index
    # Move to a coarser level while the chosen one no longer holds the start of the range.
    while (index < len(TIERS) - 1
           and now - start > _kept_days(levels, names, TIERS[index][4]) * 86400):
        index += 1
    if index != first:
        chosen = max(chosen, TIERS[index][3])
        note = (note + "; " if note else "") + (
            f"the {TIERS[first][1]} level no longer holds the start of the range, so "
            f"{TIERS[index][1]} was used")
    _, name, table, width, _ = TIERS[index]
    if width > 1 and chosen % width:
        chosen = math.ceil(chosen / width) * width
        note = (note + "; " if note else "") + f"the step was rounded up to {chosen} seconds, a " \
                                               f"whole number of {name} buckets"
    return name, table, chosen, width, note


def _complete(levels: rollups.RetentionLevels, now: float, start: float,
              names: Sequence[str], level: str) -> bool:
    """Whether the level the query uses still holds the start of the range for every selected
    metric. A finer level that does is enough, even if the daily level keeps less."""
    field = next(t[4] for t in TIERS if t[1] == level)
    return now - start <= _kept_days(levels, names, field) * 86400


def _pack(n: int, total: Any, lo: Any, hi: Any) -> dict[str, Any]:
    """The aggregates of one bucket. A sum of large values can overflow: SQLite may return
    infinity, and PostgreSQL or another SQLite build returns NULL. JSON has no such number, so
    the sum and the average are null whenever the total is NULL or not finite."""
    total = finite(total)
    avg = finite(total / n) if n and total is not None else None
    return {"avg": avg, "min": finite(lo), "max": finite(hi), "sum": total, "count": n}


def _aggregate(db: Any, name: str, table: str | None, ids: list[int], step: int,
               start_ms: int, end_ms: int, aggs: list[str]) -> dict[int, list[list[Any]]]:
    marks = ",".join("?" * len(ids))
    width = step * 1000
    out: dict[int, list[list[Any]]] = {i: [] for i in ids}

    if "last" in aggs:
        if name != "raw":
            raise ApiProblem(400, "last needs the raw level: use a step under 300 seconds "
                                  "inside raw retention")
        rows = db.execute(
            f"SELECT series_id, (ts / ?) * ?, ts, value FROM samples WHERE series_id IN ({marks}) "
            "AND ts >= ? AND ts <= ? AND value IS NOT NULL ORDER BY series_id, ts",
            (width, width, *ids, start_ms, end_ms)).fetchall()
        by: dict[tuple[int, int], dict[str, Any]] = {}
        for sid, b, _ts, value in rows:
            cur = by.get((sid, b))
            if cur is None:
                by[(sid, b)] = {"n": 1, "sum": value, "min": value, "max": value, "last": value}
            else:
                cur["n"] += 1
                cur["sum"] += value
                cur["min"] = min(cur["min"], value)
                cur["max"] = max(cur["max"], value)
                cur["last"] = value
        for (sid, b), c in sorted(by.items()):
            full = _pack(c["n"], c["sum"], c["min"], c["max"])
            full["last"] = finite(c["last"])
            out[sid].append([b // 1000, *[full[a] for a in aggs]])
        return out
    if name == "raw":
        rows = db.execute(
            "SELECT series_id, (ts / ?) * ? AS b, COUNT(value), "
            "CAST(SUM(value) AS DOUBLE PRECISION), MIN(value), MAX(value) FROM samples "
            f"WHERE series_id IN ({marks}) AND ts >= ? AND ts <= ? AND value IS NOT NULL "
            "GROUP BY 1, 2 ORDER BY 1, 2", (width, width, *ids, start_ms, end_ms)).fetchall()
    else:
        rows = db.execute(
            "SELECT series_id, (bucket / ?) * ? AS b, CAST(SUM(n) AS BIGINT), "
            f"CAST(SUM(sum_v) AS DOUBLE PRECISION), MIN(min_v), MAX(max_v) FROM {table} "
            f"WHERE series_id IN ({marks}) AND bucket >= ? AND bucket <= ? "
            "GROUP BY 1, 2 ORDER BY 1, 2", (width, width, *ids, start_ms, end_ms)).fetchall()
    for sid, b, n, total, lo, hi in rows:
        if not n:
            continue
        full = _pack(int(n), total, lo, hi)
        out[sid].append([b // 1000, *[full[a] for a in aggs]])
    return out


def run_query(db: Any, ctx: ApiContext, q: MetricQuery) -> dict[str, Any]:
    sel = Selector(q.metric, q.scope, q.resource, q.kind, q.series_id, q.match)
    _need_selector(sel)
    aggs = list(q.agg or DEFAULT_AGGS)
    for a in aggs:
        if a == "rate":
            raise ApiProblem(400, "rate needs sum series, and none are stored yet")
        if a not in AGGS:
            raise ApiProblem(400, f"agg must be from: {', '.join(AGGS)}")
    if len(set(aggs)) != len(aggs):
        raise ApiProblem(400, "agg names must not repeat")
    now = ctx.now
    start = parse_time(q.from_, now, "from")
    end = parse_time(q.to, now, "to")
    levels = rollups.load_levels(db, ctx.config.server.retention_days)
    rows, resume = _selection(db, sel, q.limit_series + 1)
    truncated = len(rows) > q.limit_series or resume is not None
    rows = rows[:q.limit_series]
    names = sorted({r[2] for r in rows})
    name, table, step, _width, note = _plan(levels, now, start, end, q.step, names)
    complete = _complete(levels, now, start, names, name)
    if not complete:
        note = (note + "; " if note else "") + (
            "the range starts before the oldest data kept for a selected metric, so the "
            "earliest part is missing")
    start_s = int(start // step * step)
    data = _aggregate(db, name, table, [r[0] for r in rows], step, start_s * 1000,
                      int(end * 1000), aggs) if rows else {}
    series = []
    for row in rows:
        info = _series_info(row)
        info["points"] = data.get(row[0], [])[:MAX_POINTS]
        series.append(info)
    return {"tier": name, "step": step, "start": start, "end": end, "aggs": aggs,
            "series": series, "series_truncated": truncated, "complete": complete, "note": note}


def query_metrics(db: Any, ctx: ApiContext, request: Request,
                  metric: str | None = Query(None, max_length=128,
                                             description="Exact, or a prefix ending in *"),
                  scope: str | None = Query(None, max_length=128),
                  resource: str | None = Query(None, max_length=128, description="Resource name"),
                  kind: str | None = Query(None, max_length=32, description="Resource kind"),
                  series_id: list[int] | None = Query(None, max_length=MAX_SERIES),
                  from_: str = Query("-24h", alias="from",
                                     description="RFC 3339, unix seconds or an offset like -24h"),
                  to: str = Query("now", description="RFC 3339, unix seconds, now or an offset"),
                  step: int | None = Query(None, ge=1, description="Seconds per point"),
                  agg: str | None = Query(None, max_length=64,
                                          description="Comma separated: " + ", ".join(AGGS)),
                  limit_series: int = Query(MAX_SERIES, ge=1, le=MAX_SERIES)) -> dict[str, Any]:
    """Time series for the series that match, as columnar points [time, agg...]. Attribute terms
    are written match[key]=value, match[key]!=value and match[key]=~pattern."""
    q = MetricQuery.model_validate({
        "metric": metric, "scope": scope, "resource": resource, "kind": kind,
        "series_id": series_id, "match": _match_params(request) or None, "from": from_,
        "to": to, "step": step, "agg": [a.strip() for a in agg.split(",")] if agg else None,
        "limit_series": limit_series})
    return run_query(db, ctx, q)


def query_metrics_post(db: Any, ctx: ApiContext, body: MetricQuery) -> dict[str, Any]:
    """The same query with the selector in a JSON body, for a long list of series ids."""
    return run_query(db, ctx, body)


def register(api: ApiRegistry) -> None:
    runtime = api.runtime
    api.resource("/metrics", list_metrics, MetricCatalogPage, domains=("metrics",),
                 tags=("metrics",), paginate=True, summary="The metric catalogue")
    api.resource("/metrics/latest", latest_metrics, LatestPage, domains=("metrics",),
                 tags=("metrics",), paginate=True, summary="Latest values")
    # A range such as -24h slides with the clock, so the ETag changes at least once a minute.
    api.resource("/metrics/query", query_metrics, QueryOut, domains=("metrics",),
                 tags=("metrics",), cost=5, memory=lambda: int(runtime.wall() // 60),
                 summary="Time series query")
    api.resource("/metrics/query", query_metrics_post, QueryOut, methods=("POST",), roles=("viewer",),
                 tags=("metrics",), cost=5, summary="Time series query with a body")
