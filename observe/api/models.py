"""Response models of the core v2 resources (docs/DATA-API-DESIGN.md section 4).

Timestamps are held as unix seconds and written as RFC 3339 strings in UTC, so a client never
guesses a unit. Every list is `{"items": [...], "next_cursor": "..." | null}`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, WithJsonSchema


def rfc3339(ts: float) -> str:
    """Unix seconds as an RFC 3339 string with millisecond precision, in UTC."""
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


Ts = Annotated[float, PlainSerializer(rfc3339, return_type=str, when_used="json"),
               WithJsonSchema({"type": "string", "format": "date-time"})]


# A timestamp that was already written as an RFC 3339 string (the host documents are built
# elsewhere and converted whole).
IsoTs = Annotated[str, WithJsonSchema({"type": "string", "format": "date-time"})]


class Page(BaseModel):
    next_cursor: str | None = None


# ---- monitors, groups, status ---------------------------------------------------------------

class MonitorOut(BaseModel):
    slug: str
    name: str
    group: str
    type: str
    mode: str | None = None
    target: str
    state: str
    degraded: bool
    effective_state: str
    blocked_by: str | None = None
    held_by: str | None = None
    depends_on: list[str]
    critical: bool
    forecast: dict[str, Any] | None = None
    since: Ts
    last_at: Ts | None = None
    result: str | None = None
    message: str
    value: float | None = None
    unit: str
    latency_ms: float | None = None
    detail: dict[str, Any] | None = None


class MonitorPage(Page):
    items: list[MonitorOut]


class MonitorDetail(MonitorOut):
    availability_24h: float | None = None
    availability_7d: float | None = None


class MonitorCheckDetail(BaseModel):
    slug: str
    detail: dict[str, Any]


class GroupOut(BaseModel):
    name: str
    state: str
    counts: dict[str, int]
    worst: list[str]


class GroupPage(Page):
    items: list[GroupOut]


class AlertTarget(BaseModel):
    name: str
    type: str
    sent: int
    last_error: str | None = None


class StatusOut(BaseModel):
    version: str
    alerts: list[AlertTarget]


# ---- hosts ----------------------------------------------------------------------------------

class HostView(BaseModel):
    """One host. The hardware sections (cpu, memory, power, temperatures, fans, raid, zfs,
    disks, ups and the Home Assistant sections) are present too, each as an object with a
    status; they are not listed here because they vary by platform."""

    model_config = ConfigDict(extra="allow")
    host: str
    platform: str
    agent_version: str
    heard: bool
    last_seen: IsoTs | None = None
    age_seconds: float | None = None
    stale: bool
    status: str
    status_reason: str
    install_problem: dict[str, Any] | None = Field(
        None, description="The newest failed console install step not cleared since.")


class HostSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    host: str
    platform: str
    agent_version: str
    heard: bool
    last_seen: IsoTs | None = None
    age_seconds: float | None = None
    stale: bool
    confirmed: bool
    monitored: bool
    monitor: dict[str, Any] | None = None
    status: str
    status_reason: str
    install_problem: dict[str, Any] | None = None
    sections: dict[str, str]
    states: dict[str, str]


class HostPage(Page):
    items: list[HostSummary]


class WaitingHost(BaseModel):
    host: str
    platform: str
    control: bool
    created: Ts
    state: str
    note: str
    enrolment_url: str


class WaitingPage(BaseModel):
    items: list[WaitingHost]


# ---- events ---------------------------------------------------------------------------------

class EventResource(BaseModel):
    kind: str
    name: str


class EventOut(BaseModel):
    id: str
    ts: Ts
    resource: EventResource
    event_name: str
    severity_number: int
    severity_text: str
    body: str
    attributes: dict[str, Any]


class EventPage(Page):
    items: list[EventOut]


# ---- metrics --------------------------------------------------------------------------------

class MetricCatalogEntry(BaseModel):
    scope: str
    metric: str
    unit: str
    series_count: int
    resource_count: int
    attribute_keys: list[str]


class MetricCatalogPage(Page):
    items: list[MetricCatalogEntry]


class SeriesResource(BaseModel):
    id: int
    kind: str
    name: str


class LatestItem(BaseModel):
    series_id: int
    scope: str
    metric: str
    unit: str
    resource: SeriesResource
    attrs: dict[str, Any]
    ts: Ts
    value: float | None = None
    previous_ts: Ts | None = None
    previous_value: float | None = None


class LatestPage(Page):
    items: list[LatestItem]


class MetricQuery(BaseModel):
    """A metrics query. The same fields are the query parameters of GET /metrics/query; `match`
    is written there as `match[key]=value`, `match[key]!=value` or `match[key]=~pattern`."""

    model_config = ConfigDict(populate_by_name=True)
    metric: str | None = None
    scope: str | None = None
    resource: str | None = None
    kind: str | None = None
    series_id: list[int] | None = None
    match: dict[str, str] | None = None
    from_: str = Field("-24h", alias="from", description="Start: RFC 3339, unix seconds or -24h")
    to: str = Field("now", description="End: RFC 3339, unix seconds, now or -1h")
    step: int | None = Field(None, ge=1, description="Seconds per point")
    agg: list[str] | None = Field(None, description="avg, min, max, sum, count or last")
    limit_series: int = Field(50, ge=1, le=50)


class QuerySeries(BaseModel):
    id: int
    scope: str
    metric: str
    unit: str
    resource: SeriesResource
    attrs: dict[str, Any]
    points: list[list[int | float | None]]


class QueryOut(BaseModel):
    tier: str
    step: int
    start: Ts
    end: Ts
    aggs: list[str]
    series: list[QuerySeries]
    series_truncated: bool
    complete: bool = True
    note: str | None = None


# ---- changes --------------------------------------------------------------------------------

class ChangesOut(BaseModel):
    cursor: str
    changed: list[str]
