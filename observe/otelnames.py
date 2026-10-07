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
    "win_thermalsuite"})


def legacy_source(source: str) -> bool:
    """True for a bare hostwatch collector id used where a scope name is now required."""
    return source in LEGACY_COLLECTORS


def legacy_rule_metric(metric: str) -> bool:
    """True for a saved rule metric in the old `<collector>.<metric>` form."""
    head, dot, _ = metric.partition(".")
    return bool(dot) and head in LEGACY_COLLECTORS


# The OpenTelemetry metric that replaced an old `<collector>.<metric>` rule metric, with what
# changes for the limits (docs/DATA-API-DESIGN.md section 3.2). A ratio is 0 to 1, not percent.
_RATIO = "now a ratio from 0 to 1, so divide percent limits by 100"
_LEGACY_RULE_REPLACEMENTS: dict[str, tuple[str, str]] = {
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
}


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
    return (f"metric {metric} is a pre-OpenTelemetry name that agents no longer send; use the "
            f"OpenTelemetry metric listed for it in section 3.2 of docs/DATA-API-DESIGN.md{note}")
