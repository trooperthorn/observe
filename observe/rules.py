"""Threshold rules for statistics (docs/DATA-API-DESIGN.md section 10.4).

A rule is not one comparison of the last value. It has a condition (above, below, equal, not
equal or outside a range) against a warn value and a crit value, and one of four evaluations:

- `consecutive`: the condition holds for X polls in a row;
- `ratio`: the condition holds in X of the last Y polls;
- `window`: the min, max or average over a time window crosses the value;
- `missing`: no data for a gap in seconds, or fewer than X of the last Y polls returned data.

A missing value is treated as unknown (default, left out), as breaching or as not breaching. A
state clears only after the condition has been false for N polls (hysteresis), so a value at the
line does not flap.

`RuleEngine` keeps a `SeriesState` for every series: the latest value next to a bounded ring of
the last samples, so an evaluation reads memory and never scans history. The engine takes its
clock as a function, so a test drives time exactly. Rule configuration is validated by
`validate` and stored in `app_settings` through the storage interface by `save`, in the same
write unit as its audit row, and read back by `load`. Nothing here trusts a stored or submitted
value: each field is checked against fixed bounds.
"""

from __future__ import annotations

import json
import math
import re
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .otelnames import legacy_rule_advice
from .storage.base import Conn

SETTINGS_KEY = "rules.config"
PATH = "/api/admin/rules"
RING_CAPACITY = 100  # samples kept per series; no rule may look further back than this
MAX_RULES = 500
MAX_X = MAX_Y = RING_CAPACITY
MAX_SECONDS = 7 * 86400.0

KINDS = ("consecutive", "ratio", "window", "missing")
CONDITIONS = ("above", "below", "equal", "not_equal", "outside")
AGGREGATES = ("min", "max", "avg")
MISSING_POLICIES = ("unknown", "breaching", "not_breaching")
SEVERITIES = ("warning", "critical")
OK, WARNING, CRITICAL = 0, 1, 2
LEVEL_NAMES = {OK: "ok", WARNING: "warning", CRITICAL: "critical"}

_NAME = re.compile(r"^[A-Za-z0-9_.:/\-]{1,128}$")
_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_FIELDS = {"id", "kind", "metric", "host", "condition", "warn", "crit", "x", "y", "window",
           "agg", "clear", "missing", "gap", "severity", "enabled"}


class RuleError(ValueError):
    """A refused rule configuration. The message is safe to show to the admin."""


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    kind: str
    metric: str
    host: str = ""  # "" is global
    condition: str = "above"
    warn: float | tuple[float, float] | None = None
    crit: float | tuple[float, float] | None = None
    x: int = 1
    y: int = 1
    window: float = 0.0
    agg: str = "avg"
    clear: int = 1
    missing: str = "unknown"
    gap: float = 0.0
    severity: str = "warning"  # of a `missing` rule
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "metric": self.metric,
                               "host": self.host, "clear": self.clear, "enabled": self.enabled}
        if self.kind == "missing":
            out["severity"] = self.severity
            if self.gap:
                out["gap"] = self.gap
            else:
                out.update(x=self.x, y=self.y)
            return out
        out.update(condition=self.condition, warn=_out(self.warn), crit=_out(self.crit),
                   missing=self.missing)
        if self.kind == "consecutive":
            out["x"] = self.x
        elif self.kind == "ratio":
            out.update(x=self.x, y=self.y)
        else:
            out.update(window=self.window, agg=self.agg)
        return out


def _out(v: Any) -> Any:
    return list(v) if isinstance(v, tuple) else v


# ---- validation -----------------------------------------------------------------------------

def _number(raw: Any, what: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise RuleError(f"{what} must be a finite number")
    return float(raw)


def _whole(raw: Any, what: str, low: int, high: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not float(raw).is_integer():
        raise RuleError(f"{what} must be a whole number")
    if not low <= raw <= high:
        raise RuleError(f"{what} must be from {low} to {high}")
    return int(raw)


def _seconds(raw: Any, what: str) -> float:
    value = _number(raw, what)
    if not 0 < value <= MAX_SECONDS:
        raise RuleError(f"{what} must be more than 0 and at most {MAX_SECONDS:g} seconds")
    return value


def _choice(raw: Any, what: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(raw, str) or raw not in allowed:
        raise RuleError(f"{what} must be one of: {', '.join(allowed)}")
    return raw


def _level(raw: Any, condition: str, what: str) -> float | tuple[float, float] | None:
    if raw is None:
        return None
    if condition == "outside":
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise RuleError(f"{what} must be a pair [low, high] for an outside rule")
        low, high = _number(raw[0], what), _number(raw[1], what)
        if low >= high:
            raise RuleError(f"{what} low must be below high")
        return (low, high)
    if isinstance(raw, (list, tuple)):
        raise RuleError(f"{what} must be a number")
    return _number(raw, what)


def _check_order(rule: Rule) -> None:
    warn, crit = rule.warn, rule.crit
    if warn is None or crit is None:
        return
    if rule.condition == "above" and crit < warn:
        raise RuleError("crit must not be below warn for an above rule")
    if rule.condition == "below" and crit > warn:
        raise RuleError("crit must not be above warn for a below rule")
    if rule.condition == "outside" and not (crit[0] <= warn[0] and crit[1] >= warn[1]):
        raise RuleError("the crit range must contain the warn range for an outside rule")


def parse_rule(raw: Any, *, refuse_legacy: bool = False) -> Rule:
    if not isinstance(raw, dict):
        raise RuleError("each rule must be an object")
    unknown = sorted(str(k) for k in raw if k not in _FIELDS)
    if unknown:
        raise RuleError(f"a rule cannot set: {', '.join(unknown)[:80]}")
    rid = raw.get("id")
    if not isinstance(rid, str) or not _ID.match(rid):
        raise RuleError("a rule needs an id of letters, digits, dot, dash and underscore")
    where = f"rule {rid}"
    kind = _choice(raw.get("kind"), f"{where}: kind", KINDS)
    metric = raw.get("metric")
    if not isinstance(metric, str) or not _NAME.match(metric):
        raise RuleError(f"{where}: metric must be a metric name")
    if refuse_legacy and (advice := legacy_rule_advice(metric)):
        raise RuleError(f"{where}: {advice}")
    host = raw.get("host", "")
    if not isinstance(host, str) or (host and not _NAME.match(host)):
        raise RuleError(f"{where}: host must be empty or a host name")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RuleError(f"{where}: enabled must be true or false")
    values: dict[str, Any] = {"id": rid, "kind": kind, "metric": metric, "host": host,
                              "enabled": enabled}
    if kind == "missing":
        for bad in ("condition", "warn", "crit", "window", "agg", "missing"):
            if bad in raw:
                raise RuleError(f"{where}: a missing-data rule cannot set {bad}")
        values["severity"] = _choice(raw.get("severity", "warning"), f"{where}: severity",
                                     SEVERITIES)
        has_gap = raw.get("gap") is not None
        has_ratio = raw.get("x") is not None or raw.get("y") is not None
        if has_gap == has_ratio:
            raise RuleError(f"{where}: set either gap, or x and y, for a missing-data rule")
        if has_gap:
            values["gap"] = _seconds(raw["gap"], f"{where}: gap")
        else:
            values["y"] = _whole(raw.get("y"), f"{where}: y", 1, MAX_Y)
            values["x"] = _whole(raw.get("x"), f"{where}: x", 1, values["y"])
    else:
        if "gap" in raw or "severity" in raw:
            raise RuleError(f"{where}: only a missing-data rule sets gap or severity")
        condition = _choice(raw.get("condition"), f"{where}: condition", CONDITIONS)
        values["condition"] = condition
        values["warn"] = _level(raw.get("warn"), condition, f"{where}: warn")
        values["crit"] = _level(raw.get("crit"), condition, f"{where}: crit")
        if values["warn"] is None and values["crit"] is None:
            raise RuleError(f"{where}: set a warn value, a crit value or both")
        values["missing"] = _choice(raw.get("missing", "unknown"), f"{where}: missing",
                                    MISSING_POLICIES)
        if kind == "consecutive":
            values["x"] = _whole(raw.get("x"), f"{where}: x", 1, MAX_X)
            if "y" in raw or "window" in raw or "agg" in raw:
                raise RuleError(f"{where}: a consecutive rule sets only x")
        elif kind == "ratio":
            values["y"] = _whole(raw.get("y"), f"{where}: y", 1, MAX_Y)
            values["x"] = _whole(raw.get("x"), f"{where}: x", 1, values["y"])
            if "window" in raw or "agg" in raw:
                raise RuleError(f"{where}: a ratio rule sets only x and y")
        else:
            if "x" in raw or "y" in raw:
                raise RuleError(f"{where}: a window rule sets only window and agg")
            values["window"] = _seconds(raw.get("window"), f"{where}: window")
            values["agg"] = _choice(raw.get("agg"), f"{where}: agg", AGGREGATES)
    values["clear"] = _whole(raw.get("clear", values.get("x", 1)), f"{where}: clear", 1, MAX_X)
    rule = Rule(**values)
    _check_order(rule)
    return rule


def validate(body: Any, *, refuse_legacy: bool = False) -> tuple[Rule, ...]:
    """Parse the whole rule list. Ids are unique and the list is bounded. With `refuse_legacy`
    (a submitted list) a rule whose metric is a pre-OpenTelemetry name is refused with the
    replacement named; a stored list is read without it so `describe` can flag such a rule."""
    if isinstance(body, dict):
        body = body.get("rules")
    if not isinstance(body, list):
        raise RuleError("send a list of rules")
    if len(body) > MAX_RULES:
        raise RuleError(f"at most {MAX_RULES} rules are allowed")
    rules = tuple(parse_rule(item, refuse_legacy=refuse_legacy) for item in body)
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise RuleError(f"rule id {rule.id} is used twice")
        seen.add(rule.id)
    return rules


# ---- storage --------------------------------------------------------------------------------

def load(db: Conn) -> tuple[Rule, ...]:
    """The saved rules. A stored document that no longer validates yields no rules rather than
    a half-trusted set."""
    row = db.execute("SELECT value FROM app_settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    if row is None:
        return ()
    try:
        return validate(json.loads(row[0]))
    except (TypeError, ValueError):
        return ()


def invalid_reason(rule: Rule) -> str | None:
    """Why a stored rule can never match a series now, or None for a usable rule."""
    return legacy_rule_advice(rule.metric)


def describe(items: Iterable[Rule]) -> list[dict[str, Any]]:
    """The rules for the API and the console: each rule's fields plus `invalid`, a sentence for a
    rule that names an old metric (it matches nothing) or null."""
    return [{**r.as_dict(), "invalid": invalid_reason(r)} for r in items]


def save(db: Conn, rules: Iterable[Rule], *, now: float, actor: str, remote: str) -> dict:
    """Inside one write unit: replace the rule set and append the one audit row holding the old
    and new rules. Returns {"old": ..., "new": ...}."""
    old = [r.as_dict() for r in load(db)]
    new = [r.as_dict() for r in rules]
    db.execute(
        "INSERT INTO app_settings (key, value, updated) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
        (SETTINGS_KEY, json.dumps(new, sort_keys=True), now))
    db.execute(
        "INSERT INTO audit (ts, actor, kind, method, path, status, remote, detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (now, actor, "rules_changed", "PUT", PATH, 200, remote,
         json.dumps({"old": old, "new": new}, sort_keys=True)))
    return {"old": old, "new": new}


# ---- the per-series ring and the engine -----------------------------------------------------

class SeriesState:
    """The latest value of one series next to a bounded ring of its recent samples. A sample is
    (timestamp in seconds, value or None for a poll that returned no data)."""

    __slots__ = ("capacity", "ring", "latest_ts", "latest_value", "last_data_ts")

    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        self.capacity = capacity
        self.ring: deque[tuple[float, float | None]] = deque(maxlen=capacity)
        self.latest_ts: float | None = None
        self.latest_value: float | None = None
        self.last_data_ts: float | None = None

    def add(self, ts: float, value: float | None) -> None:
        self.ring.append((ts, value))  # a full ring drops its oldest sample
        self.latest_ts, self.latest_value = ts, value
        if value is not None:
            self.last_data_ts = ts


def _holds(condition: str, value: float, level: Any) -> bool:
    if condition == "above":
        return value > level
    if condition == "below":
        return value < level
    if condition == "equal":
        return value == level
    if condition == "not_equal":
        return value != level
    low, high = level
    return value < low or value > high


@dataclass(frozen=True, slots=True)
class Verdict:
    rule: str
    key: str
    level: int
    previous: int

    @property
    def changed(self) -> bool:
        return self.level != self.previous


class RuleEngine:
    """Evaluates rules on the latest values and the per-series rings. `observe` records one
    sample and returns the verdicts of the rules that apply to its series; `tick` re-evaluates
    the missing-data rules for the time that has passed with no sample."""

    def __init__(self, rules: Iterable[Rule] = (), *, clock: Callable[[], float],
                 capacity: int = RING_CAPACITY) -> None:
        self._clock = clock
        self._capacity = capacity
        self._rules: tuple[Rule, ...] = ()
        self._series: dict[str, SeriesState] = {}
        self._meta: dict[str, tuple[str, str]] = {}  # key -> (host, metric)
        self._state: dict[tuple[str, str], list[int]] = {}  # (rule, key) -> [level, clear streak]
        self.set_rules(rules)

    def set_rules(self, rules: Iterable[Rule]) -> None:
        self._rules = tuple(rules)
        live = {r.id for r in self._rules}
        for stale in [k for k in self._state if k[0] not in live]:
            del self._state[stale]

    def series(self, key: str) -> SeriesState | None:
        return self._series.get(key)

    def level(self, rule_id: str, key: str) -> int:
        return self._state.get((rule_id, key), [OK, 0])[0]

    def seed(self, key: str, host: str, metric: str,
             samples: Iterable[tuple[float, float | None]]) -> None:
        """Fill a ring from stored samples, oldest first, without evaluating anything."""
        st = self._get(key, host, metric)
        for ts, value in samples:
            st.add(ts, value)

    def _get(self, key: str, host: str, metric: str) -> SeriesState:
        st = self._series.get(key)
        if st is None:
            st = self._series[key] = SeriesState(self._capacity)
            self._meta[key] = (host, metric)
        return st

    def rules_for(self, host: str, metric: str) -> list[Rule]:
        """Rules for a series: the host's own rules for the metric if it has any, else the
        global ones."""
        mine = [r for r in self._rules if r.enabled and r.metric == metric]
        specific = [r for r in mine if r.host and r.host == host]
        return specific or [r for r in mine if not r.host]

    def observe(self, key: str, host: str, metric: str, value: float | None,
                ts: float | None = None) -> list[Verdict]:
        st = self._get(key, host, metric)
        now = self._clock()
        st.add(now if ts is None else ts, value)
        return [self._step(rule, key, st, now) for rule in self.rules_for(host, metric)]

    def worst(self, host: str) -> tuple[int, str]:
        """The highest level any rule holds on any series of `host`, with a short reason."""
        level, why = OK, ""
        for (rule_id, key), slot in self._state.items():
            if slot[0] > level and self._meta[key][0] == host:
                level = slot[0]
                why = (f"threshold rule {rule_id} is {LEVEL_NAMES[level]} "
                       f"on {self._meta[key][1]}")
        return level, why

    def tick(self) -> list[Verdict]:
        """Evaluate the missing-data rules of every known series against the clock."""
        now = self._clock()
        out = []
        for key, st in self._series.items():
            host, metric = self._meta[key]
            out.extend(self._step(r, key, st, now) for r in self.rules_for(host, metric)
                       if r.kind == "missing")
        return out

    # -- evaluation --

    def _step(self, rule: Rule, key: str, st: SeriesState, now: float) -> Verdict:
        raw = self._raw(rule, st, now)
        slot = self._state.setdefault((rule.id, key), [OK, 0])
        previous = slot[0]
        if raw >= slot[0]:
            slot[0], slot[1] = raw, 0
        elif self._latest_breaches(rule, st, slot[0]):
            slot[1] = 0  # a poll that breaches on its own restarts the count, even if X is unmet
        else:
            slot[1] += 1  # one more poll with the condition false at the current level
            if slot[1] >= rule.clear:
                slot[0], slot[1] = raw, 0
        return Verdict(rule.id, key, slot[0], previous)

    @staticmethod
    def _latest_breaches(rule: Rule, st: SeriesState, level: int) -> bool:
        """Whether the newest poll alone meets the condition at `level`. Only the consecutive
        and ratio kinds judge single polls; the others decide in `_raw`."""
        if rule.kind not in ("consecutive", "ratio"):
            return False
        threshold = rule.crit if level == CRITICAL else rule.warn
        if threshold is None:
            return False
        value = st.latest_value
        if value is None:
            return rule.missing == "breaching"
        return _holds(rule.condition, value, threshold)

    def _raw(self, rule: Rule, st: SeriesState, now: float) -> int:
        if rule.kind == "missing":
            return self._raw_missing(rule, st, now)
        for level, threshold in ((CRITICAL, rule.crit), (WARNING, rule.warn)):
            if threshold is not None and self._breach(rule, st, threshold, now):
                return level
        return OK

    def _raw_missing(self, rule: Rule, st: SeriesState, now: float) -> int:
        level = CRITICAL if rule.severity == "critical" else WARNING
        if rule.gap:
            since = st.last_data_ts
            return level if since is None or now - since > rule.gap else OK
        recent = list(st.ring)[-rule.y:]
        if len(recent) < rule.y:
            return OK  # not enough polls seen yet to judge a share
        got = sum(1 for _, v in recent if v is not None)
        return level if got < rule.x else OK

    @staticmethod
    def _flags(rule: Rule, threshold: Any, samples: list[tuple[float, float | None]]
               ) -> list[bool]:
        flags = []
        for _, value in samples:
            if value is None:
                if rule.missing == "unknown":
                    continue
                flags.append(rule.missing == "breaching")
            else:
                flags.append(_holds(rule.condition, value, threshold))
        return flags

    def _breach(self, rule: Rule, st: SeriesState, threshold: Any, now: float) -> bool:
        if rule.kind == "consecutive":
            flags = self._flags(rule, threshold, list(st.ring))[-rule.x:]
            return len(flags) == rule.x and all(flags)
        if rule.kind == "ratio":
            return sum(self._flags(rule, threshold, list(st.ring)[-rule.y:])) >= rule.x
        if len(st.ring) >= st.capacity and st.ring[0][0] > now - rule.window:
            return False  # a full ring that does not reach back over the window cannot judge it
        values = [v for ts, v in st.ring if v is not None and ts > now - rule.window]
        if not values:
            return False
        agg = {"min": min(values), "max": max(values), "avg": sum(values) / len(values)}[rule.agg]
        return _holds(rule.condition, agg, threshold)
