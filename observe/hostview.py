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

A reading is matched by its OpenTelemetry scope name (`hostwatch.collector.<source>`) and metric
name, with the point attributes as its labels (docs/DATA-API-DESIGN.md section 3). Utilization,
charge and wear are ratios from 0 to 1, so their limits are too. The source status of a section is
read by the short collector id the agent reports in `observe.source`.

Thresholds listed in the YAML for a pushed_host monitor override the built-in
defaults for that scope and metric. The agent never sets thresholds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .checks.host import CRITICAL, GOOD, WARNING, grade
from .config import Thresholds
from .otelnames import collector_scope, short_source
from .store import ABSENT_REASON

_RANK = {GOOD: 0, WARNING: 1, CRITICAL: 2}
UTILIZATION = "system.memory.utilization"  # the computed memory ratio of the memory section
ALERT_WINDOW_S = 86400.0
HA_UNAVAILABLE_WARN = 25  # unavailable entities at or above this make the HA section Warning

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
    flag = labels.get("observe.ups.flag", "")
    if flag == "LB" and v >= 1:
        return CRITICAL, "UPS battery is low"
    if flag == "OB" and v >= 1:
        return WARNING, "UPS is on battery"
    return GOOD, ""


def _thermal_mode(v: float, labels: dict[str, str]) -> tuple[str, str]:
    """The controller mode gauge is 1 for the mode it is in; failsafe is a Warning."""
    if v and labels.get("observe.thermal.mode") == "failsafe":
        return WARNING, "fan controller is in failsafe"
    return GOOD, ""


class _Only:
    """A grader that claims a reading only when a point attribute has the given value, so one
    metric name can be shown in two sections (a Windows pool and a physical disk are both
    `hw.status`, told apart by `hw.type`)."""

    def __init__(self, key: str, value: str, grader: Grader) -> None:
        self.key, self.value, self.grader = key, value, grader

    def accepts(self, labels: dict[str, str]) -> bool:
        return labels.get(self.key) == self.value

    def __call__(self, v: float, labels: dict[str, str]) -> tuple[str, str]:
        return self.grader(v, labels)


_ARRAY_STATES = {"clean": GOOD, "active": GOOD, "active-idle": GOOD, "idle": GOOD,
                 "write-pending": GOOD, "readonly": WARNING, "broken": CRITICAL}
_SYNC_ACTIONS = {"idle": GOOD, "check": GOOD}
_POOL_STATES = {"online": GOOD, "degraded": CRITICAL, "faulted": CRITICAL,
                "unavail": CRITICAL, "suspended": CRITICAL}


def _state_gauge(table: dict[str, str]) -> Grader:
    """`hw.status` with one point per state: 1 says the object is in the state `hw.state` names,
    0 says it is not, which claims nothing."""
    def fn(v: float, labels: dict[str, str]) -> tuple[str, str]:
        if v <= 0:
            return GOOD, ""
        state = labels.get("hw.state", "unknown")
        level = table.get(state.lower(), WARNING)
        return level, "" if level == GOOD else f"state is {state}"
    return fn


def _pool_status(v: float, labels: dict[str, str]) -> tuple[str, str]:
    """`hw.status` of a ZFS pool: its state, and the `healthy` and `warning` flags TrueNAS adds."""
    state = labels.get("hw.state", "").lower()
    if state == "healthy":
        return (GOOD, "") if v else (CRITICAL, "pool unhealthy")
    if state == "warning":
        return (WARNING, "pool has a warning") if v else (GOOD, "")
    return _state_gauge(_POOL_STATES)(v, labels)


def _smart_status(v: float, labels: dict[str, str]) -> tuple[str, str]:
    """`hw.status{hw.state=smart_ok}`: 1 when the drive passes its SMART self assessment."""
    return (GOOD, "") if v else (CRITICAL, "SMART failed")


def _ok_flag(why: str, level: str = WARNING) -> Grader:
    return lambda v, _l: (GOOD, "") if v else (level, why)


def _count_by_label(table: dict[str, str], key: str) -> Grader:
    """A count labelled by class: zero is Good, otherwise the label picks the level."""
    def fn(v: float, labels: dict[str, str]) -> tuple[str, str]:
        if v <= 0:
            return GOOD, ""
        name = labels.get(key, "unknown")
        level = table.get(name.lower(), WARNING)
        return level, "" if level == GOOD else f"{v:g} with {key} {name}"
    return fn


_INTEGRATION_CATEGORIES = {"failing": CRITICAL, "credential": WARNING, "communication": WARNING,
                           "collection": WARNING, "errors": WARNING, "debug_logging": GOOD,
                           "disabled": GOOD}
_REPAIR_SEVERITIES = {"critical": CRITICAL, "error": WARNING, "warning": WARNING}


def _c(source: str, metric: str) -> tuple[str, str]:
    """The rule key of a hostwatch collector's point: its scope name and OpenTelemetry metric."""
    return collector_scope(source), metric


_LOADS = ("system.cpu.load_average.1m", "system.cpu.load_average.5m",
          "system.cpu.load_average.15m")
_UTIL = _above(0.90, 0.98)  # a ratio of 0 to 1, as the design sends it
_FAN_SOURCES = ("hwmon", "thermalctl", "win_thermalsuite")

# section -> {(scope, metric): grader}. The scope is the agent's `hostwatch.collector.<source>`
# and the metric the OpenTelemetry name of docs/DATA-API-DESIGN.md section 3.2; the point
# attributes are the labels a grader reads. A metric the agent could not map arrives as
# `observe.legacy.<source>.<metric>` and is graded like the reading it carried. The `snmp`,
# `homeassistant`, `hassio` and `ha_*` keys are the sources Observe's own pollers and ha_Int_soc
# write, which keep their names until those producers move to OpenTelemetry names.
RULES: dict[str, dict[tuple[str, str], Grader]] = {
    "cpu": {_c("cpu", "system.cpu.utilization"): _UTIL,
            _c("win_cpu", "system.cpu.utilization"): _UTIL,
            **{_c("cpu", m): _info for m in _LOADS},
            _c("cpu", "system.cpu.frequency"): _info,
            _c("cpu", "observe.cpu.idle_residency"): _info,
            ("snmp", "cpu_pct"): _above(90, 98), ("snmp", "cpu_core_pct"): _info},
    "memory": {_c("memory", "system.memory.limit"): _info,
               _c("memory", "system.memory.usage"): _info,
               _c("memory", "system.paging.usage"): _info,
               _c("memory", "observe.legacy.memory.commit_used"): _info,
               ("snmp", "mem_used_pct"): _above(90, 97), ("snmp", "mem_total_bytes"): _info,
               ("snmp", "mem_used_bytes"): _info},
    "power": {_c("rapl", "hw.power"): _info, _c("hwmon", "hw.power"): _info,
              _c("hwmon", "hw.voltage"): _info},
    "temperatures": {_c("hwmon", "hw.temperature"): _above(80, 90),
                     _c("thermalctl", "hw.temperature"): _above(80, 90),
                     _c("win_thermalsuite", "hw.temperature"): _above(80, 90),
                     _c("rpi", "hw.temperature"): _above(70, 80)},
    "fans": {**{_c(src, "hw.fan.speed"): _fan for src in _FAN_SOURCES},
             **{_c(src, m): _info for src in ("thermalctl", "win_thermalsuite")
                for m in ("observe.thermal.fan.duty", "observe.thermal.zone.load")},
             _c("win_thermalsuite", "observe.thermal.zone.duty"): _info,
             _c("win_thermalsuite", "observe.thermal.fan.target_duty"): _info,
             _c("thermalctl", "observe.thermal.mode"): _thermal_mode,
             _c("win_thermalsuite", "observe.thermal.mode"): _thermal_mode,
             _c("win_thermalsuite", "observe.thermal.failsafe"):
             _nonzero(WARNING, "fan controller is in failsafe"),
             _c("rpi", "observe.rpi.throttled"): _info,
             _c("rpi", "observe.rpi.throttled_raw"): _info},
    "raid": {_c("mdraid", "hw.status"): _state_gauge(_ARRAY_STATES),
             _c("mdraid", "observe.mdraid.sync_action"):
             _label_state(_SYNC_ACTIONS, "observe.mdraid.action"),
             _c("mdraid", "observe.mdraid.sync_progress"): _info,
             _c("mdraid", "observe.legacy.mdraid.degraded"):
             _nonzero(CRITICAL, "array is missing members"),
             _c("win_storage", "hw.status"): _Only("hw.type", "logical_disk", _level_value)},
    "zfs": {_c("zfs", "hw.status"): _pool_status,
            _c("truenas", "hw.status"): _pool_status,
            _c("truenas", "observe.zfs.pool.health"): _level_value,
            _c("truenas", "observe.zfs.pool.scan.errors"): _nonzero(WARNING, "scan found errors"),
            _c("truenas", "observe.zfs.pool.scan.state"): _info,
            _c("truenas", "hw.errors"): _nonzero(WARNING, "read, write or checksum errors counted"),
            _c("truenas", "observe.zfs.vdev.self_healed"): _info},
    "disks": {_c("scrutiny", "hw.status"):
              _nonzero(CRITICAL, "SMART or Scrutiny reports failure"),
              _c("scrutiny", "observe.legacy.scrutiny.temp"): _above(50, 60),
              _c("scrutiny", "observe.scrutiny.up"): _ok_flag("Scrutiny API is down"),
              _c("win_storage", "hw.status"): _Only("hw.type", "physical_disk", _level_value),
              _c("win_storage", "observe.legacy.win_storage.temp"): _above(50, 60),
              _c("win_smartctl", "hw.status"): _smart_status,
              _c("win_smartctl", "hw.errors"): _nonzero(WARNING, "media errors counted"),
              _c("win_smartctl", "hw.physical_disk.endurance_utilization"): _above(0.80, 0.95),
              _c("truenas", "hw.temperature"): _above(50, 60),
              ("hassio", "disk_used_pct"): _above(85, 95), ("hassio", "disk_free_gb"): _info,
              ("hassio", "disk_used_gb"): _info, ("hassio", "disk_total_gb"): _info,
              ("snmp", "disk_used_pct"): _above(85, 95), ("snmp", "disk_total_bytes"): _info,
              ("snmp", "disk_used_bytes"): _info},
    "ups": {_c("nut", "observe.ups.status"): _ups_flag,
            _c("nut", "hw.battery.charge"): _below(0.50, 0.20),
            _c("nut", "hw.battery.time_left"): _info,
            _c("nut", "hw.voltage"): _info,
            _c("nut", "observe.ups.load"): _above(0.80, 0.95)},
    "ha": {("homeassistant", "running"):
           lambda v, _l: (GOOD, "") if v else (CRITICAL, "Home Assistant is not running"),
           ("homeassistant", "safe_mode"): _nonzero(WARNING, "Home Assistant is in safe mode"),
           ("homeassistant", "recovery_mode"):
           _nonzero(WARNING, "Home Assistant is in recovery mode"),
           ("homeassistant", "update_pending"): _nonzero(WARNING, "an update is pending"),
           ("homeassistant", "updates_pending"): _info,
           ("homeassistant", "unavailable_entities"):
           _above(HA_UNAVAILABLE_WARN, float("inf")),
           ("homeassistant", "entities_total"): _info, ("homeassistant", "entities"): _info,
           ("homeassistant", "version"): _info,
           ("ha_supervisor", "healthy"):
           lambda v, _l: (GOOD, "") if v else (CRITICAL, "Supervisor reports an unhealthy system"),
           ("ha_supervisor", "supported"):
           lambda v, _l: (GOOD, "") if v else (WARNING, "Supervisor reports an unsupported system"),
           ("ha_supervisor", "unhealthy_reasons"): _info,
           ("ha_soc", "posture_score"): _info, ("ha_soc", "open_detections"): _info,
           ("ha_soc", "users_at_risk"): _info, ("ha_soc", "suspicious_activity"): _info},
    # ha_container, ha_watchdog, ha_integrations, ha_repairs, ha_backup and ha_supervisor are the
    # sources pushed by ha_Int_soc (docs/ARCHITECTURE.md, "Home Assistant push contract"). The
    # metric names and label values are shaped from the ha_Int_soc code and are unverified
    # against a live push.
    "containers": {("hassio", "cpu_percent"): _above(85, 95),
                   ("hassio", "memory_percent"): _above(85, 95),
                   ("ha_container", "cpu_percent"): _above(85, 95),
                   ("ha_container", "memory_percent"): _above(85, 95),
                   ("ha_container", "memory_usage_bytes"): _info,
                   ("ha_container", "memory_limit_bytes"): _info,
                   ("ha_container", "running"):
                   lambda v, _l: (GOOD, "") if v else (WARNING, "container is not running"),
                   ("ha_watchdog", "breach_count"):
                   _nonzero(WARNING, "sustained resource breach counted by the watchdog")},
    "integrations": {("ha_integrations", "issue"): _count_by_label(_INTEGRATION_CATEGORIES, "category"),
                     ("ha_integrations", "issues_total"): _info,
                     ("ha_integrations", "loaded_total"): _info},
    "repairs": {("ha_repairs", "open"): _count_by_label(_REPAIR_SEVERITIES, "severity"),
                ("ha_repairs", "open_total"): _info},
    "backups": {("ha_backup", "last_success_age_hours"): _above(36, 72),
                ("ha_backup", "last_backup_ok"):
                lambda v, _l: (GOOD, "") if v else (WARNING, "the last backup failed"),
                ("ha_backup", "backups_total"): _info},
    # Interfaces read over SNMP. An interface the admin chose to watch that is not up is a
    # Warning, never silently zero traffic.
    "network": {("snmp", "if_up"): lambda v, _l: (GOOD, "") if v else (WARNING, "interface is down"),
                ("snmp", "if_in_bps"): _info, ("snmp", "if_out_bps"): _info,
                ("snmp", "if_speed_mbps"): _info, ("snmp", "if_util_pct"): _above(70, 90)},
}
SECTIONS = ("cpu", "memory", "power", "temperatures", "fans", "raid", "zfs", "disks", "ups",
            "ha", "containers", "integrations", "repairs", "backups", "network")


def _sources_of(section: str) -> list[str]:
    return sorted({short_source(src) for src, _ in RULES[section]})


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
        if rule is None or not getattr(rule, "accepts", lambda _l: True)(s["labels"]):
            continue
        info = sources.get(short_source(s["source"]))
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
    """Add system.memory.utilization (a ratio), computed from the newest limit and used bytes
    (or the limit minus the free bytes), graded 0.90 and 0.97."""
    scope = collector_scope("memory")
    by = {(i["metric"], i["labels"].get("system.memory.state", "")): i
          for i in sec["items"] if i["source"] == scope}
    total = by.get(("system.memory.limit", ""))
    used_item, free_item = by.get(("system.memory.usage", "used")),         by.get(("system.memory.usage", "free"))
    if not total or total["value"] is None or total["value"] <= 0:
        return
    if used_item and used_item["value"] is not None:
        used, parts = used_item["value"], [total, used_item]
    elif free_item and free_item["value"] is not None:
        used, parts = total["value"] - free_item["value"], [total, free_item]
    else:
        return
    ratio = round(used / total["value"], 4)
    level, why = _above(0.90, 0.97)(ratio, {})
    key = (scope, UTILIZATION)
    th = overrides.get(key)
    if th is not None:
        level = grade(ratio, th)
        why = f"{ratio:g} past the configured limit" if level != GOOD else ""
    stale = any(p["stale"] for p in parts)
    reasons = [why] if why else []
    if stale:
        level = worst([level, WARNING])
        reasons.append("memory reading is stale")
    sec["items"].append({"source": scope, "metric": UTILIZATION, "labels": {}, "value": ratio,
                         "unit": "1", "ts": min(p["ts"] for p in parts),
                         "age_seconds": max(p["age_seconds"] for p in parts),
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
