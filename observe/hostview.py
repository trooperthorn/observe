"""Per-host hardware views built from the newest pushed batch.

The grouping and the section names follow hostwatch/integrations/summary.py
(hostwatch, same owner), adapted: Observe reads its own store, and every
reading is graded Good, Warning or Critical here. Nothing is guessed:

- A reading with no value this cycle is a Warning, never zero.
- A reading, a source, or a whole host that has not reported within the stale
  window is marked stale and is at least a Warning. A host that has gone silent
  is Critical, the same as the pushed_host check.
- A source the agent says does not exist on the host (state "absent") and a
  source nobody ever reported (state "not_reported") claim nothing, so they do
  not make the host look worse. They are listed under `sources` so the page
  can say so.

Thresholds listed in the YAML for a pushed_host monitor override the built-in
defaults for that source and metric. The agent never sets thresholds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .checks.host import CRITICAL, GOOD, WARNING, grade
from .config import Thresholds
from .store import ABSENT_REASON

_RANK = {GOOD: 0, WARNING: 1, CRITICAL: 2}
ALERT_WINDOW_S = 86400.0

Grader = Callable[[float, dict[str, str]], tuple[str, str]]


def worst(levels: list[str]) -> str:
    return max(levels, key=_RANK.__getitem__, default=GOOD)


def _above(warn: float, crit: float) -> Grader:
    def fn(v: float, _labels: dict[str, str]) -> tuple[str, str]:
        if v >= crit:
            return CRITICAL, f"{v:g} is at or past {crit:g}"
        if v >= warn:
            return WARNING, f"{v:g} is at or past {warn:g}"
        return GOOD, ""
    return fn


def _below(warn: float, crit: float) -> Grader:
    def fn(v: float, _labels: dict[str, str]) -> tuple[str, str]:
        if v <= crit:
            return CRITICAL, f"{v:g} is at or under {crit:g}"
        if v <= warn:
            return WARNING, f"{v:g} is at or under {warn:g}"
        return GOOD, ""
    return fn


def _info(_v: float, _labels: dict[str, str]) -> tuple[str, str]:
    return GOOD, ""


def _fan(v: float, _labels: dict[str, str]) -> tuple[str, str]:
    return (CRITICAL, "fan reads 0 RPM") if v <= 0 else (GOOD, "")


def _nonzero(level: str, why: str) -> Grader:
    def fn(v: float, _labels: dict[str, str]) -> tuple[str, str]:
        return (level, why) if v else (GOOD, "")
    return fn


def _level_value(v: float, _labels: dict[str, str]) -> tuple[str, str]:
    """Storage health levels from the Windows agent: 0 healthy, 1 warning, 2 or more critical."""
    return (GOOD, "") if v <= 0 else (WARNING, "health warning") if v < 2 else \
        (CRITICAL, "health failed")


def _label_state(table: dict[str, str], key: str, default: str = WARNING) -> Grader:
    def fn(v: float, labels: dict[str, str]) -> tuple[str, str]:
        state = labels.get(key, "unknown")
        level = table.get(state.lower(), default)
        return level, "" if level == GOOD else f"{key} is {state}"
    return fn


def _flag(level: str, which: str, why: str) -> Grader:
    def fn(v: float, labels: dict[str, str]) -> tuple[str, str]:
        return (level, why) if labels.get("flag") == which and v >= 1 else (GOOD, "")
    return fn


def _ups_flag(v: float, labels: dict[str, str]) -> tuple[str, str]:
    flag = labels.get("flag", "")
    if flag == "LB" and v >= 1:
        return CRITICAL, "UPS battery is low"
    if flag == "OB" and v >= 1:
        return WARNING, "UPS is on battery"
    return GOOD, ""


_ARRAY_STATES = {"clean": GOOD, "active": GOOD, "active-idle": GOOD, "idle": GOOD,
                 "write-pending": GOOD, "readonly": WARNING, "broken": CRITICAL}
_SYNC_ACTIONS = {"idle": GOOD, "check": GOOD}
_POOL_STATES = {"online": GOOD, "degraded": CRITICAL, "faulted": CRITICAL,
                "unavail": CRITICAL, "suspended": CRITICAL}

# section -> {(source, metric): grader}. Sources are the hostwatch collector ids.
RULES: dict[str, dict[tuple[str, str], Grader]] = {
    "cpu": {("cpu", "utilization_pct"): _above(90, 98), ("cpu", "load"): _info,
            ("cpu", "freq_mhz"): _info, ("cpu", "idle_residency_pct"): _info,
            ("cpu", "core_throttle_count"): _info, ("cpu", "package_throttle_count"): _info},
    "memory": {("memory", "mem_total"): _info, ("memory", "mem_available"): _info,
               ("memory", "swap_total"): _info, ("memory", "swap_free"): _info},
    "power": {("rapl", "watts"): _info, ("hwmon", "power"): _info},
    "temperatures": {("hwmon", "temp"): _above(80, 90), ("thermalctl", "zone_temp"): _above(80, 90),
                     ("rpi", "soc_temp"): _above(70, 80)},
    "fans": {("hwmon", "fan"): _fan, ("thermalctl", "fan"): _fan,
             ("thermalctl", "fan_duty"): _info, ("thermalctl", "fan_target"): _info,
             ("thermalctl", "zone_load"): _info,
             ("thermalctl", "failsafe"): _nonzero(WARNING, "fan controller is in failsafe"),
             ("rpi", "throttle_flag"): _info},
    "raid": {("mdraid", "degraded"): _nonzero(CRITICAL, "array is missing members"),
             ("mdraid", "raid_disks"): _info, ("mdraid", "array_state"):
             _label_state(_ARRAY_STATES, "state"),
             ("mdraid", "sync_action"): _label_state(_SYNC_ACTIONS, "action"),
             ("mdraid", "sync_progress_pct"): _info,
             ("mdraid", "mismatch_cnt"): _nonzero(WARNING, "mismatched sectors found"),
             ("win_storage", "virtual_disk_health"): _level_value},
    "zfs": {("zfs", "pool_state"): _label_state(_POOL_STATES, "state"),
            ("win_storage", "pool_health"): _level_value,
            ("truenas", "pool_health"): _level_value,
            ("truenas", "pool_healthy"): lambda v, _l: (GOOD, "") if v else (CRITICAL, "pool unhealthy"),
            ("truenas", "pool_state"): _label_state(_POOL_STATES, "state"),
            ("truenas", "pool_scan_errors"): _nonzero(WARNING, "scan found errors"),
            ("truenas", "pool_warning"): _nonzero(WARNING, "pool has a warning"),
            ("zfs", "vdev_self_healed_bytes"): _info},
    "disks": {("scrutiny", "device_status"): _nonzero(CRITICAL, "SMART or Scrutiny reports failure"),
              ("scrutiny", "temp"): _above(50, 60), ("scrutiny", "power_on_hours"): _info,
              ("scrutiny", "api_up"): lambda v, _l: (GOOD, "") if v else (WARNING, "Scrutiny API is down"),
              ("win_storage", "disk_health"): _level_value, ("win_storage", "disk_temp_c"):
              _above(50, 60), ("win_storage", "wear_pct"): _above(80, 95),
              ("win_smartctl", "smart_passed"): lambda v, _l: (GOOD, "") if v else (CRITICAL, "SMART failed"),
              ("truenas", "disk_temp_c"): _above(50, 60)},
    "ups": {("nut", "ups_status_flag"): _ups_flag,
            ("nut", "battery_charge_pct"): _below(50, 20), ("nut", "battery_runtime_s"): _info,
            ("nut", "input_voltage_v"): _info, ("nut", "ups_load_pct"): _above(80, 95)},
}
SECTIONS = ("cpu", "memory", "power", "temperatures", "fans", "raid", "zfs", "disks", "ups")


def _sources_of(section: str) -> list[str]:
    return sorted({src for src, _ in RULES[section]})


def source_views(sources: dict[str, dict[str, Any]], now: float,
                 stale_after: float) -> list[dict[str, Any]]:
    out = []
    for name in sorted(sources):
        info = sources[name]
        absent = not info["available"] and info["reason"] == ABSENT_REASON
        age = max(0.0, now - info["updated"])
        out.append({"source": name, "available": info["available"], "present": not absent,
                    "reason": info["reason"], "updated": info["updated"], "age_seconds": age,
                    "stale": age > stale_after,
                    "status": GOOD if absent or (info["available"] and age <= stale_after)
                    else WARNING})
    return out


def _item(rule: Grader, override: Thresholds | None, s: dict[str, Any], now: float,
          stale_after: float, src_info: dict[str, Any] | None,
          host_stale: bool) -> dict[str, Any]:
    age = max(0.0, now - s["ts"])
    stale = host_stale or age > stale_after
    labels = s["labels"]
    reasons: list[str] = []
    if s["value"] is None:
        level = WARNING
        reasons.append("no value this cycle")
    else:
        level, why = rule(s["value"], labels)
        if override is not None:
            level = grade(s["value"], override)
            why = f"{s['value']:g} past the configured limit" if level != GOOD else ""
        if why:
            reasons.append(why)
    if stale:
        level = worst([level, WARNING])
        reasons.append(f"last reading {age:.0f}s ago")
    if src_info is not None and not src_info["available"] and src_info["reason"] != ABSENT_REASON:
        level = worst([level, WARNING])
        reasons.append(f"source unavailable: {src_info['reason'] or 'no reason given'}")
    return {"source": s["source"], "metric": s["metric"], "labels": labels, "value": s["value"],
            "unit": s["unit"], "ts": s["ts"], "age_seconds": age, "stale": stale,
            "status": level, "reason": "; ".join(reasons)}


def _section(name: str, samples: list[dict[str, Any]], sources: dict[str, dict[str, Any]],
             overrides: dict[tuple[str, str], Thresholds], now: float, stale_after: float,
             host_stale: bool) -> dict[str, Any]:
    rules = RULES[name]
    items = []
    for s in sorted(samples, key=lambda x: (x["source"], x["metric"],
                                             sorted(x["labels"].items()))):
        rule = rules.get((s["source"], s["metric"]))
        if rule is None:
            continue
        info = sources.get(s["source"])
        items.append(_item(rule, overrides.get((s["source"], s["metric"])), s, now,
                           stale_after, info, host_stale))
    names = _sources_of(name)
    infos = {n: sources[n] for n in names if n in sources}
    bad = {n: i for n, i in infos.items()
           if not i["available"] and i["reason"] != ABSENT_REASON}
    if host_stale:
        state, note = "stale", "no current data from the host"
    elif items and all(i["stale"] for i in items):
        state, note = "stale", "no reading inside the stale window"
    elif bad:
        state = "unavailable"
        note = "; ".join(f"{n}: {i['reason'] or 'no reason given'}" for n, i in sorted(bad.items()))
    elif items or any(i["available"] for i in infos.values()):
        state, note = "ok", ""
    elif infos:
        state, note = "absent", "the agent reports this hardware is not present"
    else:
        state, note = "not_reported", "no source for this section has reported"
    level = worst([i["status"] for i in items])
    if state in ("stale", "unavailable"):
        level = worst([level, WARNING])
    return {"status": level, "state": state, "note": note, "items": items}


def _memory_extra(sec: dict[str, Any], overrides: dict[tuple[str, str], Thresholds]) -> None:
    """Add used_pct, computed from the newest total and available, graded 90 and 97."""
    by = {i["metric"]: i for i in sec["items"] if i["source"] == "memory"}
    total, avail = by.get("mem_total"), by.get("mem_available")
    if not total or not avail or total["value"] is None or avail["value"] is None \
            or total["value"] <= 0:
        return
    used = round(100 * (1 - avail["value"] / total["value"]), 1)
    level, why = _above(90, 97)(used, {})
    th = overrides.get(("memory", "used_pct"))
    if th is not None:
        level = grade(used, th)
        why = f"{used:g} past the configured limit" if level != GOOD else ""
    stale = total["stale"] or avail["stale"]
    reasons = [why] if why else []
    if stale:
        level = worst([level, WARNING])
        reasons.append("memory reading is stale")
    sec["items"].append({"source": "memory", "metric": "used_pct", "labels": {}, "value": used,
                         "unit": "%", "ts": min(total["ts"], avail["ts"]),
                         "age_seconds": max(total["age_seconds"], avail["age_seconds"]),
                         "stale": stale, "status": level, "reason": "; ".join(reasons)})
    sec["status"] = worst([sec["status"], level])


def _alerts(events: list[dict[str, Any]], now: float) -> dict[str, Any]:
    recent = [e for e in events if e["severity"] in ("warning", "critical")
              and now - e["ts"] <= ALERT_WINDOW_S]
    level = worst([CRITICAL if e["severity"] == "critical" else WARNING for e in recent])
    return {"status": level, "state": "ok", "note": f"warning and critical events in the last "
            f"{int(ALERT_WINDOW_S // 3600)}h", "items": recent}


def build_host_view(row: dict[str, Any], data: dict[str, Any] | None,
                    sources: dict[str, dict[str, Any]], events: list[dict[str, Any]],
                    now: float, stale_after: float, monitor: Any | None,
                    overrides: dict[tuple[str, str], Thresholds],
                    monitor_state: dict[str, Any] | None) -> dict[str, Any]:
    """The JSON document for one host. `row` is the hosts table row (or a stub for a
    monitor that has never heard from its host), `data` the output of Store.latest_host
    with since=0."""
    heard = data is not None
    age = max(0.0, now - row["last_seen"]) if heard else None
    host_stale = (not heard) or (age or 0.0) > stale_after
    samples = data["samples"] if data else []
    out: dict[str, Any] = {
        "host": row["host"], "platform": row.get("platform", ""),
        "agent_version": row.get("agent_version", ""),
        "heard": heard, "last_seen": row["last_seen"] if heard else None,
        "age_seconds": age, "stale": host_stale, "stale_after": stale_after,
        "confirmed": bool(row.get("confirmed")), "monitored": monitor is not None,
        "monitor": monitor_state,
        "boot": {"boot_id": row.get("boot_id"), "boot_ts": row.get("boot_ts"),
                 "clean_shutdown": None if row.get("clean_shutdown") is None
                 else bool(row["clean_shutdown"])},
    }
    for name in SECTIONS:
        out[name] = _section(name, samples, sources, overrides, now, stale_after, host_stale)
    _memory_extra(out["memory"], overrides)
    out["alerts"] = _alerts(events, now)
    out["events"] = events
    out["sources"] = source_views(sources, now, stale_after)
    levels = [out[n]["status"] for n in (*SECTIONS, "alerts")]
    if host_stale:
        out["status"] = CRITICAL
        out["status_reason"] = "no batch received yet" if not heard else \
            f"no batch for {age:.0f}s (limit {stale_after:.0f}s)"
    else:
        out["status"] = worst(levels)
        out["status_reason"] = ""
    return out


def summarize(view: dict[str, Any]) -> dict[str, Any]:
    """The compact row shown in GET /api/hosts."""
    keys = ("host", "platform", "agent_version", "heard", "last_seen", "age_seconds", "stale",
            "confirmed", "monitored", "monitor", "status", "status_reason")
    out = {k: view[k] for k in keys}
    out["sections"] = {n: view[n]["status"] for n in (*SECTIONS, "alerts")}
    out["states"] = {n: view[n]["state"] for n in SECTIONS}
    return out
