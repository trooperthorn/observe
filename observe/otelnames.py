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
