"""Names of the OpenTelemetry scopes a host agent sends (docs/DATA-API-DESIGN.md section 3.1).

The instrumentation scope of a point is `hostwatch.collector.<source>`, where `<source>` is the
collector id the agent reports in its source status (`observe.source`). Observe stores the scope
name as the source of a series, so the host view, the pushed host check and the threshold rules
all key on the full scope name and the OpenTelemetry metric name, and use these helpers only to
go between a scope and the short collector id.
"""

from __future__ import annotations

COLLECTOR_PREFIX = "hostwatch.collector."


def collector_scope(source: str) -> str:
    """The scope name of a collector id."""
    return COLLECTOR_PREFIX + source


def short_source(scope: str) -> str:
    """The collector id of a scope name. A scope that is not a collector scope (a poller or a
    Home Assistant source) is its own id."""
    return scope[len(COLLECTOR_PREFIX):] if scope.startswith(COLLECTOR_PREFIX) else scope


def rule_metric(scope: str, metric: str) -> str:
    """The metric name a saved threshold rule names for a pushed series. An OpenTelemetry metric
    name is meaningful by itself (`hw.temperature`), so a rule on it applies to every sensor of
    the host; the series of two collectors stay apart because the engine key carries the scope.
    A source outside the collector scopes keeps the older `<source>.<metric>` form."""
    return metric if scope.startswith(COLLECTOR_PREFIX) else f"{scope}.{metric}"


# The collector ids of the hostwatch agent. Before the OpenTelemetry names, a series was keyed by
# one of these as its source (`hwmon`) and a short metric (`temp`); a saved configuration or rule
# that still does so matches nothing now, and its limits were in other units (percent, not ratio).
LEGACY_COLLECTORS = frozenset({
    "cpu", "memory", "rapl", "hwmon", "thermalctl", "linux_thermal", "rpi", "mdraid", "zfs",
    "truenas", "scrutiny", "nut", "win_cpu", "win_memory", "win_storage", "win_smartctl",
    "win_thermalsuite", "snmp"})

# The Home Assistant sources, before they moved to the section 3.4 names: the three sources of
# Observe's Home Assistant monitor and the sources ha_Int_soc pushed, each with the scope that
# replaced it. A saved component or rule that names one matches nothing now.
HA_SOC_PREFIX = "ha_soc.collector."
LEGACY_HA_SOURCES: dict[str, str] = {
    "homeassistant": "observe.check.homeassistant",
    "hassio": "observe.check.hassio",
    "ha_soc": "observe.check.ha_soc",
    "ha_container": HA_SOC_PREFIX + "containers",
    "ha_backup": HA_SOC_PREFIX + "backup",
    "ha_watchdog": HA_SOC_PREFIX + "watchdog",
    "ha_integrations": HA_SOC_PREFIX + "integrations",
    "ha_repairs": HA_SOC_PREFIX + "repairs",
    "ha_supervisor": HA_SOC_PREFIX + "supervisor",
}

# The SNMP poller of Observe was the source `snmp` before it wrote the scope `observe.check.snmp`
# (docs/DATA-API-DESIGN.md section 3.5), so its replacement scope is not a collector scope.
SNMP_SCOPE = "observe.check.snmp"


def replacement_scope(source: str) -> str:
    """The scope that replaced an old bare source id."""
    if source in LEGACY_HA_SOURCES:
        return LEGACY_HA_SOURCES[source]
    return SNMP_SCOPE if source == "snmp" else collector_scope(source)


def legacy_source(source: str) -> bool:
    """True for a bare hostwatch collector id or an old Home Assistant source id used where a
    scope name is now required."""
    return source in LEGACY_COLLECTORS or source in LEGACY_HA_SOURCES


def legacy_rule_metric(metric: str) -> bool:
    """True for a saved rule metric in the old `<collector>.<metric>` form."""
    if metric.startswith(HA_SOC_PREFIX):
        return False  # a current rule on a pushed Home Assistant scope, not the old `ha_soc` id
    head, dot, _ = metric.partition(".")
    return bool(dot) and (head in LEGACY_COLLECTORS or head in LEGACY_HA_SOURCES)


# The OpenTelemetry metric that replaced an old `<collector>.<metric>` rule metric, with what
# changes for the limits (docs/DATA-API-DESIGN.md section 3.2). A ratio is 0 to 1, not percent.
_RATIO = "now a ratio from 0 to 1, so divide percent limits by 100"
_UNCHANGED = "unit is unchanged"
_LEGACY_RULE_REPLACEMENTS: dict[str, tuple[str, str]] = {
    "homeassistant.running": ("observe.check.homeassistant / observe.ha.running", _UNCHANGED),
    "homeassistant.safe_mode": ("observe.check.homeassistant / observe.ha.safe_mode", _UNCHANGED),
    "homeassistant.recovery_mode": ("observe.check.homeassistant / observe.ha.recovery_mode",
                                    _UNCHANGED),
    "homeassistant.update_pending": ("observe.check.homeassistant / observe.ha.update.pending",
                                     _UNCHANGED),
    "homeassistant.updates_pending": ("observe.check.homeassistant / observe.ha.update.count",
                                      _UNCHANGED),
    "homeassistant.unavailable_entities": ("observe.check.homeassistant / "
                                           "observe.ha.entity.unavailable", _UNCHANGED),
    "homeassistant.entities_total": ("observe.check.homeassistant / observe.ha.entity.count",
                                     _UNCHANGED),
    "hassio.cpu_percent": ("observe.check.hassio / container.cpu.utilization", _RATIO),
    "hassio.memory_percent": ("observe.check.hassio / container.memory.utilization", _RATIO),
    "hassio.disk_used_pct": ("observe.check.hassio / system.filesystem.utilization", _RATIO),
    "hassio.disk_used_gb": ("observe.check.hassio / system.filesystem.usage",
                            "unit is now bytes, not GB, so multiply limits by 1000000000"),
    "hassio.disk_free_gb": ("observe.check.hassio / system.filesystem.usage",
                            "unit is now bytes, not GB, so multiply limits by 1000000000"),
    "hassio.disk_total_gb": ("observe.check.hassio / system.filesystem.limit",
                             "unit is now bytes, not GB, so multiply limits by 1000000000"),
    "ha_container.cpu_percent": ("ha_soc.collector.containers / container.cpu.utilization",
                                 _RATIO),
    "ha_container.memory_percent": ("ha_soc.collector.containers / "
                                    "container.memory.utilization", _RATIO),
    "ha_container.memory_usage_bytes": ("ha_soc.collector.containers / container.memory.usage",
                                        "unit is bytes, unchanged"),
    "ha_container.running": ("ha_soc.collector.containers / observe.ha.container.running",
                             _UNCHANGED),
    "ha_watchdog.breach_count": ("ha_soc.collector.watchdog / observe.ha.watchdog.breaches",
                                 _UNCHANGED),
    "ha_backup.last_success_age_hours": ("ha_soc.collector.backup / "
                                         "observe.ha.backup.last_success_age",
                                         "unit is now seconds, not hours, so multiply limits "
                                         "by 3600"),
    "ha_backup.last_backup_ok": ("ha_soc.collector.backup / observe.ha.backup.last_ok",
                                 _UNCHANGED),
    "ha_backup.backups_total": ("ha_soc.collector.backup / observe.ha.backup.count", _UNCHANGED),
    "ha_repairs.open": ("ha_soc.collector.repairs / observe.ha.repair.issues", _UNCHANGED),
    "ha_integrations.issue": ("ha_soc.collector.integrations / observe.ha.integration.errors",
                              _UNCHANGED),
    "ha_supervisor.healthy": ("ha_soc.collector.supervisor / observe.ha.supervisor.healthy",
                              _UNCHANGED),
    "cpu.utilization_pct": ("system.cpu.utilization", _RATIO),
    "win_cpu.utilization_pct": ("system.cpu.utilization", _RATIO),
    "memory.mem_total": ("system.memory.limit", "unit is bytes, unchanged"),
    "win_memory.mem_total": ("system.memory.limit", "unit is bytes, unchanged"),
    "memory.mem_available": ("system.memory.usage", "unit is bytes, unchanged; the free share is "
                             "the point with system.memory.state=free"),
    "hwmon.temp": ("hw.temperature", "unit is degrees Celsius, unchanged"),
    "hwmon.fan": ("hw.fan.speed", "unit is rpm, unchanged"),
    "hwmon.voltage": ("hw.voltage", "unit is volts, unchanged"),
    "hwmon.power": ("hw.power", "unit is watts, unchanged"),
    "rapl.watts": ("hw.power", "unit is watts, unchanged"),
    "rpi.soc_temp": ("hw.temperature", "unit is degrees Celsius, unchanged"),
    "truenas.disk_temp_c": ("hw.temperature", "unit is degrees Celsius, unchanged"),
    "thermalctl.zone_temp": ("hw.temperature", "unit is degrees Celsius, unchanged"),
    "win_thermalsuite.zone_temp": ("hw.temperature", "unit is degrees Celsius, unchanged"),
    "mdraid.sync_progress_pct": ("observe.mdraid.sync_progress", _RATIO),
    "nut.battery_charge_pct": ("hw.battery.charge", _RATIO),
    "nut.battery_runtime_s": ("hw.battery.time_left", "unit is seconds, unchanged"),
    "nut.input_voltage_v": ("hw.voltage", "unit is volts, unchanged"),
    "nut.ups_load_pct": ("observe.ups.load", _RATIO),
    "win_storage.wear_pct": ("hw.physical_disk.endurance_utilization", _RATIO),
    "snmp.cpu_pct": ("system.cpu.utilization", _RATIO),
    "snmp.cpu_core_pct": ("system.cpu.utilization", _RATIO + "; one core has the attribute "
                          "cpu.logical_number"),
    "snmp.mem_used_pct": ("system.memory.utilization", _RATIO),
    "snmp.mem_total_bytes": ("system.memory.limit", "unit is bytes, unchanged"),
    "snmp.mem_used_bytes": ("system.memory.usage", "unit is bytes, unchanged"),
    "snmp.disk_used_pct": ("system.filesystem.utilization", _RATIO),
    "snmp.disk_total_bytes": ("system.filesystem.usage", "unit is bytes, unchanged; the total is "
                              "the sum of the points with system.filesystem.state used and free"),
    "snmp.disk_used_bytes": ("system.filesystem.usage", "unit is bytes, unchanged"),
    "snmp.if_up": ("observe.network.interface.up", "1 for up, 0 otherwise, unchanged"),
    "snmp.if_in_bps": ("observe.network.interface.rate", "unit is bit/s, unchanged; the receive "
                       "direction has the attribute network.io.direction"),
    "snmp.if_out_bps": ("observe.network.interface.rate", "unit is bit/s, unchanged; the transmit "
                        "direction has the attribute network.io.direction"),
    "snmp.if_speed_mbps": ("observe.network.interface.speed", "unit is now bit/s, not Mbit/s, so "
                           "multiply limits by 1000000"),
    "snmp.if_util_pct": ("observe.network.interface.utilization", _RATIO),
}


# Source rows an old version wrote that nothing writes now. They stay in the store, but would
# only read stale, so the host view hides them and `require_sources` refuses them.
RETIRED_SOURCES = frozenset({"snmp"} | set(LEGACY_HA_SOURCES))


def legacy_rule_advice(metric: str) -> str | None:
    """None for a current rule metric. For an old `<collector>.<metric>` name, a sentence naming
    the OpenTelemetry replacement and the unit change, safe to show to the admin."""
    if not legacy_rule_metric(metric):
        return None
    found = _LEGACY_RULE_REPLACEMENTS.get(metric)
    if found is not None:
        new, unit = found
        return f"metric {metric} is a pre-OpenTelemetry name that agents no longer send; use {new} ({unit})"
    note = "; percent values are now ratios from 0 to 1" if metric.endswith("_pct") else ""
    head = metric.partition(".")[0]
    if head in LEGACY_HA_SOURCES:
        return (f"metric {metric} is a pre-OpenTelemetry name that agents no longer send; use "
                f"the metric of the scope {LEGACY_HA_SOURCES[head]} listed in section 3.4 of "
                f"docs/DATA-API-DESIGN.md{note}")
    return (f"metric {metric} is a pre-OpenTelemetry name that agents no longer send; use the "
            f"OpenTelemetry metric listed for it in section 3.2 of docs/DATA-API-DESIGN.md{note}")
