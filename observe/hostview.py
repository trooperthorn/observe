"""Per-host hardware views built from the newest pushed batch.

The grouping and the section names follow hostwatch/integrations/summary.py
(hostwatch, same owner), adapted: Observe reads its own store, and every
reading is graded Good, Warning or Critical here. Nothing is guessed:

- A reading with no value this cycle is "no data", never zero and never a
  breach: it claims nothing, so it does not make the host look worse.
- A reading, a source, or a whole host that has not reported within the stale
  window is marked stale and is at least a Warning. A reading's window comes from
  the polling tier its collector runs in (observe/tiers.py), so a reading polled
  every 300 seconds is not stale at 286 seconds. A host that has gone silent
  is Critical, the same as the pushed_host check.
- A source the agent says does not exist on the host (state "absent") and a
  source nobody ever reported (state "not_reported") claim nothing, so they do
  not make the host look worse. They are listed under `sources` so the page
  can say so. A source the agent reported present before and reports absent now
  (state "gone") is different: it disappeared, so it is Critical, with when it was
  last seen, until the agent reports it again or an admin accepts that it is gone
  (POST /api/hosts/{host}/sources/{source}/forget, audited).
- An unavailable source with no reason of its own takes the title of the newest
  event the agent sent about it.

A reading is matched by its OpenTelemetry scope name (`hostwatch.collector.<source>`) and metric
name, with the point attributes as its labels (docs/DATA-API-DESIGN.md section 3). Utilization,
charge and wear are ratios from 0 to 1, so their limits are too. The source status of a section is
read by the short collector id the agent reports in `observe.source`.

Thresholds listed in the YAML for a pushed_host monitor override the built-in
defaults for that scope and metric. The agent never sets thresholds.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .agentdrops import agent_drops, alert_item
from .producers import agent_label
from .checks.host import CRITICAL, GOOD, STALE, WARNING, grade, grade_components
from .config import Thresholds
from .enrol import install_problem
from .tiers import Staleness
from .otelnames import HA_SOC_PREFIX, RETIRED_SOURCES, collector_scope, short_source
from .store import ABSENT_REASON
from .units import value_text

NO_DATA = "no_data"  # a reading or section with nothing to grade; ranks below Good
_RANK = {NO_DATA: -1, GOOD: 0, WARNING: 1, CRITICAL: 2}
UTILIZATION = "system.memory.utilization"  # the computed memory ratio of the memory section
ALERT_WINDOW_S = 86400.0
HA_UNAVAILABLE_WARN = 25  # unavailable entities at or above this make the HA section Warning

Grader = Callable[[float, dict[str, str]], tuple[str, str]]


def worst(levels: list[str]) -> str:
    return max(levels, key=_RANK.__getitem__, default=GOOD)


def _unit_aware(fn: Any) -> Any:
    """Mark a grader that takes the reading's unit as a third argument, so its reason writes the
    value and the limit the way the Value column does ("92.5 %", not "0.9248")."""
    fn.unit_aware = True
    return fn


def _run(rule: Grader, v: float, labels: dict[str, str], unit: str) -> tuple[str, str]:
    if getattr(rule, "unit_aware", False):
        return rule(v, labels, unit)  # type: ignore[call-arg]
    return rule(v, labels)


def _above(warn: float, crit: float) -> Grader:
    def fn(v: float, _labels: dict[str, str], unit: str = "") -> tuple[str, str]:
        if v >= crit:
            return CRITICAL, f"{value_text(v, unit)} is at or past {value_text(crit, unit)}"
        if v >= warn:
            return WARNING, f"{value_text(v, unit)} is at or past {value_text(warn, unit)}"
        return GOOD, ""
    return _unit_aware(fn)


def _below(warn: float, crit: float) -> Grader:
    def fn(v: float, _labels: dict[str, str], unit: str = "") -> tuple[str, str]:
        if v <= crit:
            return CRITICAL, f"{value_text(v, unit)} is at or under {value_text(crit, unit)}"
        if v <= warn:
            return WARNING, f"{value_text(v, unit)} is at or under {value_text(warn, unit)}"
        return GOOD, ""
    return _unit_aware(fn)


def _past_limit(v: float, unit: str, th: Thresholds) -> tuple[str, str]:
    """A configured limit from the YAML, with the same wording as the built-in ones."""
    level = grade(v, th)
    if level == GOOD:
        return GOOD, ""
    limit = th.crit if level == CRITICAL else th.warn
    word = "past" if th.direction == "above" else "under"
    shown = value_text(limit, unit) if limit is not None else "limit"
    return level, f"{value_text(v, unit)} is at or {word} the configured {shown}"


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

    @property
    def unit_aware(self) -> bool:
        return bool(getattr(self.grader, "unit_aware", False))

    def __call__(self, v: float, labels: dict[str, str], unit: str = "") -> tuple[str, str]:
        return _run(self.grader, v, labels, unit)


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


def _integration_count(v: float, labels: dict[str, str]) -> tuple[str, str]:
    """An integration count or error count: a point with a `category` is graded by it, the total
    (no category) is shown."""
    if "observe.ha.integration.category" not in labels:
        return _info(v, labels)
    return _count_by_label(_INTEGRATION_CATEGORIES, "observe.ha.integration.category")(v, labels)


def _repair_issues(v: float, labels: dict[str, str]) -> tuple[str, str]:
    """Open repair issues are a Warning, or the level of the severity attribute when the producer
    sends one; the `all` state is a count that claims nothing."""
    if labels.get("observe.ha.repair.state") == "all":
        return _info(v, labels)
    return _count_by_label(_REPAIR_SEVERITIES, "observe.ha.repair.severity")(v, labels)


def _ha(name: str, metric: str) -> tuple[str, str]:
    """The rule key of a point ha_Int_soc pushes: its scope `ha_soc.collector.<name>`."""
    return HA_SOC_PREFIX + name, metric


def _c(source: str, metric: str) -> tuple[str, str]:
    """The rule key of a hostwatch collector's point: its scope name and OpenTelemetry metric."""
    return collector_scope(source), metric


_LOADS = ("system.cpu.load_average.1m", "system.cpu.load_average.5m",
          "system.cpu.load_average.15m")
_UTIL = _above(0.90, 0.98)  # a ratio of 0 to 1, as the design sends it
SNMP = "observe.check.snmp"  # scope of the readings Observe's SNMP poller stores
HA_POLL = "observe.check.homeassistant"  # scopes of the Home Assistant monitor in host mode
HASSIO_POLL = "observe.check.hassio"
SOC_POLL = "observe.check.ha_soc"


@_unit_aware
def _snmp_cpu(v: float, labels: dict[str, str], unit: str = "1") -> tuple[str, str]:
    """The whole-host load is graded; the load of one core (`cpu.logical_number`) is shown."""
    return _info(v, labels) if "cpu.logical_number" in labels else _run(_UTIL, v, labels, unit)

_FAN_SOURCES = ("hwmon", "thermalctl", "win_thermalsuite")

# section -> {(scope, metric): grader}. The scope is the agent's `hostwatch.collector.<source>`
# and the metric the OpenTelemetry name of docs/DATA-API-DESIGN.md section 3.2; the point
# attributes are the labels a grader reads. A metric the agent could not map arrives as
# `observe.legacy.<source>.<metric>` and is graded like the reading it carried. The SNMP poller
# writes the scope `observe.check.snmp` with the section 3.5 names. The Home Assistant sections read
# the section 3.4 names, from ha_Int_soc (`ha_soc.collector.<name>`) and from Observe's own Home
# Assistant monitor (`observe.check.homeassistant`, `.hassio`, `.ha_soc`).
RULES: dict[str, dict[tuple[str, str], Grader]] = {
    "cpu": {_c("cpu", "system.cpu.utilization"): _UTIL,
            _c("win_cpu", "system.cpu.utilization"): _UTIL,
            **{_c("cpu", m): _info for m in _LOADS},
            _c("cpu", "system.cpu.frequency"): _info,
            _c("cpu", "observe.cpu.idle_residency"): _info,
            (SNMP, "system.cpu.utilization"): _snmp_cpu},
    "memory": {_c("memory", "system.memory.limit"): _info,
               _c("memory", "system.memory.usage"): _info,
               _c("memory", "system.paging.usage"): _info,
               _c("memory", "observe.legacy.memory.commit_used"): _info,
               (SNMP, "system.memory.utilization"): _above(0.90, 0.97),
               (SNMP, "system.memory.limit"): _info, (SNMP, "system.memory.usage"): _info},
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
              (HASSIO_POLL, "system.filesystem.utilization"): _above(0.85, 0.95),
              (HASSIO_POLL, "system.filesystem.usage"): _info,
              (HASSIO_POLL, "system.filesystem.limit"): _info,
              (SNMP, "system.filesystem.utilization"): _above(0.85, 0.95),
              (SNMP, "system.filesystem.usage"): _info},
    "ups": {_c("nut", "observe.ups.status"): _ups_flag,
            _c("nut", "hw.battery.charge"): _below(0.50, 0.20),
            _c("nut", "hw.battery.time_left"): _info,
            _c("nut", "hw.voltage"): _info,
            _c("nut", "observe.ups.load"): _above(0.80, 0.95)},
    "ha": {(HA_POLL, "observe.ha.running"):
           lambda v, _l: (GOOD, "") if v else (CRITICAL, "Home Assistant is not running"),
           (HA_POLL, "observe.ha.safe_mode"): _nonzero(WARNING, "Home Assistant is in safe mode"),
           (HA_POLL, "observe.ha.recovery_mode"):
           _nonzero(WARNING, "Home Assistant is in recovery mode"),
           (HA_POLL, "observe.ha.update.pending"): _nonzero(WARNING, "an update is pending"),
           (HA_POLL, "observe.ha.update.count"): _info,
           (HA_POLL, "observe.ha.entity.unavailable"):
           _above(HA_UNAVAILABLE_WARN, float("inf")),
           (HA_POLL, "observe.ha.entity.count"): _info,
           (HA_POLL, "observe.ha.version"): _info,
           _ha("supervisor", "observe.ha.supervisor.healthy"):
           lambda v, _l: (GOOD, "") if v else (CRITICAL, "Supervisor reports an unhealthy system"),
           _ha("supervisor", "observe.ha.supervisor.supported"):
           lambda v, _l: (GOOD, "") if v else (WARNING, "Supervisor reports an unsupported system"),
           _ha("supervisor", "observe.ha.supervisor.unhealthy_reasons"): _info,
           (SOC_POLL, "observe.ha.soc.posture_score"): _info,
           (SOC_POLL, "observe.ha.soc.open_detections"): _info,
           (SOC_POLL, "observe.ha.soc.users_at_risk"): _info,
           (SOC_POLL, "observe.ha.soc.suspicious_activity"): _info},
    # The scopes `ha_soc.collector.<name>` are what ha_Int_soc pushes (docs/DATA-API-DESIGN.md
    # section 3.4, docs/ARCHITECTURE.md "Home Assistant push contract"). The scopes
    # `observe.check.homeassistant`, `.hassio` and `.ha_soc` are what Observe's own Home
    # Assistant monitor in host mode writes.
    "containers": {(HASSIO_POLL, "container.cpu.utilization"): _above(0.85, 0.95),
                   (HASSIO_POLL, "container.memory.utilization"): _above(0.85, 0.95),
                   _ha("containers", "container.cpu.utilization"): _above(0.85, 0.95),
                   _ha("containers", "container.memory.utilization"): _above(0.85, 0.95),
                   _ha("containers", "container.memory.usage"): _info,
                   _ha("containers", "observe.ha.container.running"):
                   lambda v, _l: (GOOD, "") if v else (WARNING, "container is not running"),
                   _ha("watchdog", "observe.ha.watchdog.breaches"):
                   _nonzero(WARNING, "sustained resource breach counted by the watchdog")},
    "integrations": {_ha("integrations", "observe.ha.integration.count"): _integration_count,
                     _ha("integrations", "observe.ha.integration.errors"): _integration_count},
    "repairs": {_ha("repairs", "observe.ha.repair.issues"): _repair_issues},
    "backups": {_ha("backup", "observe.ha.backup.last_success_age"): _above(36 * 3600, 72 * 3600),
                _ha("backup", "observe.ha.backup.last_ok"):
                lambda v, _l: (GOOD, "") if v else (WARNING, "the last backup failed"),
                _ha("backup", "observe.ha.backup.count"): _info,
                _ha("backup", "observe.ha.backup.unprotected"):
                _nonzero(WARNING, "backups are not password protected")},
    # Interfaces read over SNMP. An interface the admin chose to watch that is not up is a
    # Warning, never silently zero traffic.
    "network": {(SNMP, "observe.network.interface.up"):
                lambda v, _l: (GOOD, "") if v else (WARNING, "interface is down"),
                (SNMP, "observe.network.interface.rate"): _info,
                (SNMP, "observe.network.interface.speed"): _info,
                (SNMP, "observe.network.interface.utilization"): _above(0.70, 0.90)},
}
SECTIONS = ("cpu", "memory", "power", "temperatures", "fans", "raid", "zfs", "disks", "ups",
            "ha", "containers", "integrations", "repairs", "backups", "network")
# Everything a host's one verdict is the worst of. The pushed_host check, the Hosts list, the host
# page and its header all read that verdict (`status`, `status_reason`), never their own.
VERDICT = (*SECTIONS, "components", "alerts")


def _sources_of(section: str) -> list[str]:
    return sorted({short_source(src) for src, _ in RULES[section]})


# The source row Observe's SNMP poller wrote before it used the scope `observe.check.snmp`.
RETIRED_SNMP_SOURCE = "snmp"  # with the old Home Assistant ids, otelnames.RETIRED_SOURCES


def _windows(stale_after: float | Staleness) -> Staleness:
    return stale_after if isinstance(stale_after, Staleness) else Staleness(stale_after)


# The event the agent sends when a source it had seen is no longer there. While the source stays
# missing the host is held Critical by the source itself (`_gone`), so the event does not also
# count for 24 hours, and the host clears as soon as the source is back.
SOURCE_GONE = "source.disappeared"
_GONE_TITLE = re.compile(r"\bsource (\S+) disappeared", re.IGNORECASE)


def _ago(seconds: float) -> str:
    """'13h ago', the way the host page writes an age."""
    s = max(0.0, seconds)
    if s < 90:
        return f"{s:.0f}s ago"
    if s < 5400:
        return f"{s / 60:.0f}m ago"
    if s < 129600:
        return f"{s / 3600:.0f}h ago"
    return f"{s / 86400:.0f}d ago"


def _absent(info: dict[str, Any]) -> bool:
    """The agent reports the source as not present on the host."""
    return not info["available"] and info["reason"] == ABSENT_REASON


def _gone(info: dict[str, Any]) -> bool:
    """A source the agent reported present before and reports not present now: it disappeared,
    which is not hardware the host never had (that one has no `present_at`)."""
    return _absent(info) and info.get("present_at") is not None


def _gone_text(name: str, info: dict[str, Any], now: float) -> str:
    return (f"{name} disappeared, last seen {_ago(now - info['present_at'])}; this clears when "
            f"the agent reports {name} again or an admin accepts that it is gone")


def _event_source(e: dict[str, Any]) -> str:
    """The collector id an event is about: the source the agent named (`observe.source`), or for
    a disappeared source the one its title names."""
    if e.get("kind") == SOURCE_GONE:
        m = _GONE_TITLE.search(str(e.get("title", "")))
        if m:
            return m.group(1)
    return short_source(str(e.get("source") or ""))


def _reason(name: str, info: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """Why a source is unavailable: the reason its status carries, else the title of the newest
    event the agent sent about it (the rapl collector says how to grant access there), else
    "no reason given"."""
    if info["reason"]:
        return info["reason"]
    for e in sorted(events, key=lambda x: x.get("ts", 0.0), reverse=True):
        title = str(e.get("title", ""))
        if e.get("kind") == SOURCE_GONE or _event_source(e) != name \
                or title == f"{name} available":
            continue
        detail = e.get("detail") or {}
        why = detail.get("reason") or detail.get("observe.source.reason") or title
        if why:
            return str(why)
    return "no reason given"


def _source_problem(name: str, info: dict[str, Any] | None, now: float,
                    events: list[dict[str, Any]]) -> str:
    """What is wrong with the source of a reading, or "" when nothing is."""
    if info is None:
        return ""
    if _gone(info):
        return _gone_text(name, info, now)
    if not info["available"] and info["reason"] != ABSENT_REASON:
        return f"source unavailable: {_reason(name, info, events)}"
    return ""


def source_views(sources: dict[str, dict[str, Any]], now: float,
                 stale_after: float | Staleness,
                 events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One row per source. `state` is ok, stale, unavailable, gone (it was present and has
    disappeared: Critical until it returns or an admin accepts it) or absent (the host never had
    it: claims nothing)."""
    windows = _windows(stale_after)
    out = []
    for name in sorted(sources):
        if name in RETIRED_SOURCES:
            continue  # kept in the store, but nothing writes it now, so it would only read stale
        info = sources[name]
        absent, gone = _absent(info), _gone(info)
        age = max(0.0, now - info["updated"])
        limit = windows.for_source(name)
        stale = age > limit
        state = "gone" if gone else "absent" if absent else \
            "unavailable" if not info["available"] else "stale" if stale else "ok"
        reason = _gone_text(name, info, now) if gone else "" if absent else \
            _reason(name, info, events or []) if not info["available"] else info["reason"]
        out.append({"source": name, "available": info["available"], "present": not absent,
                    "gone": gone, "present_at": info.get("present_at"), "state": state,
                    "reason": reason, "updated": info["updated"], "age_seconds": age,
                    "stale": stale,
                    "status": CRITICAL if gone else NO_DATA if absent
                    else GOOD if info["available"] and not stale else WARNING})
    return out


def _item(rule: Grader, override: Thresholds | None, s: dict[str, Any], now: float,
          windows: Staleness, src_problem: str,
          host_stale: bool, ignored: frozenset[str] = frozenset()) -> dict[str, Any]:
    age = max(0.0, now - s["ts"])
    stale = host_stale or age > windows.for_scope(s["source"])
    labels = s["labels"]
    reasons: list[str] = []
    if s["value"] is None:
        level = NO_DATA
        reasons.append("no value this cycle")
    elif stale:
        # An old value is shown but never graded: a sensor that read 99 an hour ago says nothing
        # about now, so staleness alone decides the state.
        level = GOOD
    else:
        level, why = _run(rule, s["value"], labels, s["unit"])
        if override is not None:
            level, why = _past_limit(s["value"], s["unit"], override)
        if why:
            reasons.append(why)
    if stale:
        level = worst([level, WARNING])
        reasons.append(f"last reading {age:.0f}s ago")
    if src_problem:
        level = worst([level, WARNING])
        reasons.append(src_problem)
    hw_id = labels.get("hw.id")
    return {"source": s["source"], "metric": s["metric"], "labels": labels, "value": s["value"],
            "unit": s["unit"], "ts": s["ts"], "age_seconds": age, "stale": stale,
            "status": level, "reason": "; ".join(reasons),
            "ignored": bool(hw_id) and hw_id in ignored}


def _section(name: str, samples: list[dict[str, Any]], sources: dict[str, dict[str, Any]],
             overrides: dict[tuple[str, str], Thresholds], now: float, windows: Staleness,
             host_stale: bool, heard: bool = True,
             ignored: frozenset[str] = frozenset(),
             events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    events = events or []
    rules = RULES[name]
    items = []
    for s in sorted(samples, key=lambda x: (x["source"], x["metric"],
                                             sorted(x["labels"].items()))):
        rule = rules.get((s["source"], s["metric"]))
        if rule is None or not getattr(rule, "accepts", lambda _l: True)(s["labels"]):
            continue
        src = short_source(s["source"])
        items.append(_item(rule, overrides.get((s["source"], s["metric"])), s, now, windows,
                           _source_problem(src, sources.get(src), now, events), host_stale,
                           ignored))
    counted = [i for i in items if not i["ignored"]]
    names = _sources_of(name)
    infos = {n: sources[n] for n in names if n in sources}
    bad = {n: i for n, i in infos.items()
           if not i["available"] and i["reason"] != ABSENT_REASON}
    gone = {n: i for n, i in infos.items() if _gone(i)}
    if not heard:
        state, note = "not_reported", "the host has never reported"
    elif host_stale:
        state, note = "stale", "no current data from the host"
    elif gone:
        # A source that fed this section has disappeared: say which, since when, and how it
        # clears, instead of calling the hardware "not present".
        state = "gone"
        note = "; ".join([_gone_text(n, i, now) for n, i in sorted(gone.items())]
                         + [f"{n}: {_reason(n, i, events)}" for n, i in sorted(bad.items())])
    elif counted and all(i["stale"] for i in counted):
        state, note = "stale", "no reading inside the stale window"
    elif bad:
        state = "unavailable"
        note = "; ".join(f"{n}: {_reason(n, i, events)}" for n, i in sorted(bad.items()))
    elif items or any(i["available"] for i in infos.values()):
        state, note = "ok", ""
        if not items:
            working = ", ".join(sorted(n for n, i in infos.items() if i["available"]))
            note = f"{working} reports but sends no reading for this section"
        elif not counted:
            note = "every reading here is ignored on this host"
        elif all(i["status"] == NO_DATA for i in counted):
            note = "no reading here has a value this cycle"
    elif infos:
        state, note = "absent", "the agent reports this hardware is not present"
    else:
        state, note = "not_reported", "no source for this section has reported"
    level = worst([i["status"] for i in counted])
    if state == "gone":
        level = worst([level, CRITICAL])
    elif state in ("stale", "unavailable"):
        level = worst([level, WARNING])
    elif state in ("absent", "not_reported") or not counted:
        level = NO_DATA  # nothing to grade: neither Up nor Warning
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
    level, why = _run(_above(0.90, 0.97), ratio, {}, "1")
    key = (scope, UTILIZATION)
    th = overrides.get(key)
    if th is not None:
        level, why = _past_limit(ratio, "1", th)
    stale = any(p["stale"] for p in parts)
    reasons = [why] if why else []
    if stale:
        level = worst([level, WARNING])
        reasons.append("memory reading is stale")
    sec["items"].append({"source": scope, "metric": UTILIZATION, "labels": {}, "value": ratio,
                         "unit": "1", "ts": min(p["ts"] for p in parts),
                         "age_seconds": max(p["age_seconds"] for p in parts),
                         "stale": stale, "status": level, "reason": "; ".join(reasons),
                         "ignored": False})
    sec["status"] = worst([sec["status"], level])


def _components(monitor: Any | None, row: dict[str, Any], samples: list[dict[str, Any]],
                sources: dict[str, dict[str, Any]], windows: Staleness, now: float
                ) -> dict[str, Any]:
    """What the pushed_host monitor's YAML adds: its configured components, required sources and
    crash hold, graded by the same function the check uses. Empty for a host with no monitor."""
    if getattr(monitor, "type", None) != "pushed_host":
        return {"status": GOOD, "state": "ok", "note": "", "items": [], "levels": {}}
    levels, reasons = grade_components(monitor, samples, sources, windows, now,
                                       row.get("boot_ts"), row.get("clean_shutdown"))
    items = [{"name": n, "status": CRITICAL if lvl == STALE else lvl, "stale": lvl == STALE,
              "reason": reasons.get(n, "")} for n, lvl in sorted(levels.items())]
    return {"status": worst([i["status"] for i in items]), "state": "ok",
            "note": "the components the monitor lists", "items": items, "levels": levels}


def _alerts(events: list[dict[str, Any]], now: float, monitor: Any | None = None,
            sources: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Warning and critical events of the last day. For a host listed as a pushed_host monitor a
    boot classification is left to the monitor's crash hold (`crash_hold_s`, `crash_result`,
    graded under `components`), so a crash counts for as long, and as badly, as the YAML says.
    A disappeared source the store knows is graded by its current state instead of its event:
    each source still missing is a critical item for as long as it stays missing, and the
    agent's `source.disappeared` event stops counting (so the host clears when the source is
    back, and an old event alone never keeps it Down)."""
    crash_policy = getattr(monitor, "type", None) == "pushed_host"
    known = sources or {}
    recent = [e for e in events if e["severity"] in ("warning", "critical")
              and now - e["ts"] <= ALERT_WINDOW_S
              and not (crash_policy and str(e.get("kind", "")).startswith("boot."))
              and not (e.get("kind") == SOURCE_GONE and _event_source(e) in known)]
    recent += [{"ts": i["present_at"], "kind": "source.missing", "severity": "critical",
                "source": n, "title": _gone_text(n, i, now), "detail": {}, "boot_id": None}
               for n, i in sorted(known.items()) if _gone(i) and n not in RETIRED_SOURCES]
    level = worst([CRITICAL if e["severity"] == "critical" else WARNING for e in recent])
    return {"status": level, "state": "ok", "note": f"warning and critical events in the last "
            f"{int(ALERT_WINDOW_S // 3600)}h", "items": recent}


def build_host_view(row: dict[str, Any], data: dict[str, Any] | None,
                    sources: dict[str, dict[str, Any]], events: list[dict[str, Any]],
                    now: float, stale_after: float | Staleness, monitor: Any | None,
                    overrides: dict[tuple[str, str], Thresholds],
                    monitor_state: dict[str, Any] | None,
                    ignored: frozenset[str] = frozenset()) -> dict[str, Any]:
    """The JSON document for one host. `row` is the hosts table row (or a stub for a
    monitor that has never heard from its host), `data` the output of Store.latest_host
    with since=0. `stale_after` is the host's silence limit, or its per-tier windows."""
    windows = _windows(stale_after)
    stale_after = windows.batch
    heard = data is not None
    age = max(0.0, now - row["last_seen"]) if heard else None
    host_stale = (not heard) or (age or 0.0) > stale_after
    samples = data["samples"] if data else []
    out: dict[str, Any] = {
        "host": row["host"], "platform": row.get("platform", ""),
        "agent_version": row.get("agent_version", ""),
        "agent_label": agent_label(row.get("agent_version", "")),  # a polled host's producer
        "heard": heard, "last_seen": row["last_seen"] if heard else None,
        "age_seconds": age, "stale": host_stale, "stale_after": stale_after,
        "confirmed": bool(row.get("confirmed")), "monitored": monitor is not None,
        "monitor": monitor_state,
        "install_problem": install_problem(row.get("install_reports")),
        "boot": {"boot_id": row.get("boot_id"), "boot_ts": row.get("boot_ts"),
                 "clean_shutdown": None if row.get("clean_shutdown") is None
                 else bool(row["clean_shutdown"])},
    }
    for name in SECTIONS:
        out[name] = _section(name, samples, sources, overrides, now, windows, host_stale, heard,
                             ignored, events)
    _memory_extra(out["memory"], overrides)
    out["components"] = _components(monitor, row, samples, sources, windows, now)
    out["alerts"] = _alerts(events, now, monitor, sources)
    # The agent's outbox is dropping data: a warning in the verdict until the count stops growing.
    out["agent_drops"] = agent_drops(events, now)
    if out["agent_drops"]:
        out["alerts"]["items"] = [alert_item(out["agent_drops"]), *out["alerts"]["items"]]
        out["alerts"]["status"] = worst([out["alerts"]["status"], WARNING])
    if not heard and not out["alerts"]["items"]:
        out["alerts"]["status"] = NO_DATA  # a host that never reported has no "Up" anywhere
    out["events"] = events
    out["sources"] = source_views(sources, now, windows, events)
    levels = [out[n]["status"] for n in VERDICT]
    if host_stale:
        out["status"] = CRITICAL
        out["status_reason"] = "no batch received yet" if not heard else \
            f"no batch for {age:.0f}s (limit {stale_after:.0f}s)"
    else:
        out["status"] = worst(levels)
        out["status_reason"] = "" if out["status"] == GOOD else _cause(out)
    return out


def _cause(view: dict[str, Any]) -> str:
    """Why a host that is not stale is not good: the section and the first item that carry the
    worst level, so a warning or critical summary always names its cause."""
    top = worst([view[n]["status"] for n in VERDICT])
    for name in VERDICT:
        sec = view[name]
        if sec["status"] != top:
            continue
        for item in sec["items"]:
            if name == "alerts":
                if item.get("severity") == ("critical" if top == CRITICAL else "warning"):
                    return f"alerts: {item.get('title', '')}"[:240]
            elif name == "components":
                if item["status"] == top and item["reason"]:
                    return f"{item['name']}: {item['reason']}"[:240]
            elif item["status"] == top and item["reason"] and not item.get("ignored"):
                label = f"{item['source']} {item['metric']}"
                return f"{name}: {label}: {item['reason']}"[:240]
        if sec["note"]:
            return f"{name}: {sec['note']}"[:240]
        return f"{name} is {top}"
    return f"status is {top}"


def summarize(view: dict[str, Any]) -> dict[str, Any]:
    """The compact row shown in GET /api/hosts."""
    keys = ("host", "platform", "agent_version", "heard", "last_seen", "age_seconds", "stale",
            "confirmed", "monitored", "monitor", "status", "status_reason", "install_problem",
            "agent_drops", "agent_label")
    out = {k: view[k] for k in keys}
    out["sections"] = {n: view[n]["status"] for n in (*SECTIONS, "alerts")}
    if view["components"]["items"]:
        out["sections"]["components"] = view["components"]["status"]
    out["states"] = {n: view[n]["state"] for n in SECTIONS}
    return out
