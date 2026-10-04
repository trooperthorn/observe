"""Agent-to-watchpost wire schema (version 1).

Copied from hostwatch/schema.py (hostwatch, same owner) and adapted. The
field names, types and defaults are unchanged, so a batch that a hostwatch
agent sends today parses here without modification. Differences, all
tightening and none visible to a well-formed agent. Unknown fields are ignored,
as hostwatch ignores them, so a newer agent is not dead-lettered:

- Strings, lists and mappings have size and count limits.
- An unknown schema_version is a validation error (not a bare ValueError).
- Non-finite numbers are rejected in every float field.

A sample with value None means the source exists but could not produce a
value this cycle. It is stored as unavailable, never as zero.

Events (boot classifications, journal matches, threshold crossings) travel in
the optional Batch.events list, which defaults to empty.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import SCHEMA_VERSION

MAX_BODY_BYTES = 1_048_576
MAX_SOURCES = 256
MAX_SAMPLES = 5000
MAX_EVENTS = 500
MAX_LABELS = 32
MAX_DETAIL_KEYS = 64
MAX_DETAIL_BYTES = 8192
MAX_NAME = 128
MAX_TEXT = 1024


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Sample(_Wire):
    source: str = Field(min_length=1, max_length=MAX_NAME,
                        description="collector id, e.g. rapl, mdraid")
    metric: str = Field(min_length=1, max_length=MAX_NAME,
                        description="metric name within the source, e.g. package_watts")
    value: float | None = Field(allow_inf_nan=False)
    unit: str = Field(default="", max_length=32)
    labels: dict[str, str] = Field(default_factory=dict, max_length=MAX_LABELS)
    ts: float = Field(allow_inf_nan=False, description="unix epoch seconds when the value was read")

    @field_validator("labels")
    @classmethod
    def _label_sizes(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if len(k) > MAX_NAME or len(val) > MAX_TEXT:
                raise ValueError("label key or value too long")
        return v


class SourceStatus(_Wire):
    source: str = Field(min_length=1, max_length=MAX_NAME)
    available: bool
    reason: str = Field(default="", max_length=MAX_TEXT)
    present: bool = Field(
        default=True,
        description="False only when the collector positively established that this host has no such "
                    "source. An unreadable source stays present and unavailable. Agents that never "
                    "send the field are read as present.")


class Event(_Wire):
    kind: str = Field(min_length=1, max_length=MAX_NAME,
                      description="event kind, e.g. boot.clean_shutdown or md.degraded")
    severity: str = Field(min_length=1, max_length=16, description="info, warning, or critical")
    source: str = Field(min_length=1, max_length=MAX_NAME,
                        description="event source id, e.g. journal, pstore, rasdaemon")
    ts: float = Field(allow_inf_nan=False, description="unix epoch seconds when the event happened")
    title: str = Field(max_length=MAX_TEXT)
    detail: dict[str, Any] = Field(default_factory=dict, max_length=MAX_DETAIL_KEYS)
    dedup_key: str = Field(min_length=1, max_length=MAX_TEXT,
                           description="stable key; one row is kept per host and key")
    boot_id: str | None = Field(default=None, max_length=MAX_NAME,
                                description="kernel boot_id the event belongs to, when known")

    @field_validator("detail")
    @classmethod
    def _detail_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        try:
            size = len(json.dumps(v, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("detail must be finite JSON") from exc
        if size > MAX_DETAIL_BYTES:
            raise ValueError("detail too large")
        return v


class Batch(_Wire):
    schema_version: int = SCHEMA_VERSION
    agent_version: str = Field(max_length=MAX_NAME)
    host: str = Field(min_length=1, max_length=MAX_NAME)
    platform: str = Field(max_length=MAX_NAME)
    sent_at: float = Field(allow_inf_nan=False)
    sources: list[SourceStatus] = Field(max_length=MAX_SOURCES)
    samples: list[Sample] = Field(max_length=MAX_SAMPLES)
    events: list[Event] = Field(default_factory=list, max_length=MAX_EVENTS)
    batch_id: str | None = Field(
        default=None, min_length=1, max_length=64,
        description="optional uuid string, set once per batch and reused on resend so a repeat can be acknowledged")

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {v}")
        return v
