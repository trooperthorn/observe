"""Map Home Assistant /api/config and /api/states into a hostwatch-schema Batch.

Only the data a non-admin long-lived token can read is used. Observe never holds
an HA admin token; richer detail comes from ha_Int_soc pushing its own batches.

Shapes, from the Home Assistant REST API and the ha_Int_soc code:

- GET /api/config: version, state ("RUNNING" when started), safe_mode, recovery_mode,
  location_name. Taken from the HA REST documentation.
- GET /api/states: a list of {entity_id, state, attributes, last_changed}. Taken from the
  HA REST documentation, and already read by the existing unavailable and updates modes.
- update.* entities: state "on" means an update is pending; attributes installed_version
  and latest_version. The entity ids of the Supervisor updates
  (update.home_assistant_core_update, update.home_assistant_supervisor_update,
  update.home_assistant_operating_system_update) are UNVERIFIED against a live install.
- hassio sensors: sensor.<slug>_cpu_percent and sensor.<slug>_memory_percent for Core,
  Supervisor and each add-on, and sensor.home_assistant_host_disk_free, _disk_used and
  _disk_total in GB. UNVERIFIED: the entity ids come from knowledge of the HA hassio
  integration and no live install was read. They are disabled by default in HA, so a
  missing sensor is reported as absent, never as zero. Only Core, Supervisor and add-ons count
  (see HASSIO_FIXED and the add-on rule below); a *_cpu_percent sensor from any other
  integration is ignored.
- HA SOC sensors: sensor.ha_soc_posture_score, sensor.ha_soc_open_detections,
  sensor.ha_soc_users_at_risk and binary_sensor.ha_soc_suspicious_activity. The unique ids
  are in ha_Int_soc sensor.py; the entity ids derive from translated names and are
  UNVERIFIED.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from ..ingest.schema import MAX_ABS_VALUE, MAX_SAMPLES, MAX_TEXT, Batch

AGENT_VERSION = "observe-ha-host"
MAX_PENDING = 100
MAX_UPDATES = 300
MAX_HASSIO = 400
_STATS = re.compile(r"^sensor\.([a-z0-9_]+?)_(cpu_percent|memory_percent)$")
# The hassio integration names its sensors after the Core and Supervisor entities and after each
# add-on. An add-on slug is not fixed, so a stats sensor counts for an add-on only when the same
# slug also has its hassio "running" binary sensor or its update entity. UNVERIFIED against a live
# install, like the other entity ids here.
HASSIO_FIXED = ("home_assistant_core", "home_assistant_supervisor")
_ADDON_MARK = re.compile(r"^(?:binary_sensor\.([a-z0-9_]+)_running|update\.([a-z0-9_]+)_update)$")
_DISK = {"sensor.home_assistant_host_disk_free": "disk_free_gb",
         "sensor.home_assistant_host_disk_used": "disk_used_gb",
         "sensor.home_assistant_host_disk_total": "disk_total_gb"}
_SOC = {"sensor.ha_soc_posture_score": "posture_score",
        "sensor.ha_soc_open_detections": "open_detections",
        "sensor.ha_soc_users_at_risk": "users_at_risk",
        "binary_sensor.ha_soc_suspicious_activity": "suspicious_activity"}
_SUPERVISOR_UPDATES = {
    "update.home_assistant_supervisor_update": "supervisor",
    "update.home_assistant_operating_system_update": "os"}


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    # A value too large to store is dropped here, not left to fail the whole batch.
    return v if math.isfinite(v) and abs(v) <= MAX_ABS_VALUE else None


def update_is_pending(st: dict[str, Any]) -> bool:
    """An update entity is pending when its state is on and the installed version is not the
    latest one. An installed update clears it even if the entity state lags behind."""
    if st.get("state") != "on":
        return False
    attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
    installed, latest = attrs.get("installed_version"), attrs.get("latest_version")
    return not (installed and latest and str(installed) == str(latest))


def _text(raw: Any) -> str:
    return str(raw if raw is not None else "")[:MAX_TEXT]


def build_batch(host: str, config: dict[str, Any], states: list[dict[str, Any]],
                now: float) -> Batch:
    samples: list[dict[str, Any]] = []

    def add(source: str, metric: str, value: float | None, unit: str = "", **labels: str) -> None:
        samples.append({"source": source, "metric": metric, "value": value, "unit": unit,
                        "labels": {k: _text(v) for k, v in labels.items()}, "ts": now})

    core_version = _text(config.get("version"))
    add("homeassistant", "running", 1.0 if config.get("state") == "RUNNING" else 0.0, "",
        state=_text(config.get("state")))
    add("homeassistant", "safe_mode", 1.0 if config.get("safe_mode") else 0.0)
    add("homeassistant", "recovery_mode", 1.0 if config.get("recovery_mode") else 0.0)
    if core_version:
        add("homeassistant", "version", 1.0, "", component="core", version=core_version)

    domains: Counter[str] = Counter()
    unavailable = 0
    pending: list[dict[str, Any]] = []
    update_entities: list[str] = []
    pending_ids: set[str] = set()
    addon_slugs: set[str] = set()
    hassio: list[tuple[str, str, float | None]] = []
    stats: list[tuple[str, str, float | None]] = []
    disk: dict[str, float | None] = {}
    soc: dict[str, float | None] = {}
    versions: dict[str, str] = {}
    for st in states:
        if not isinstance(st, dict):
            continue
        eid = st.get("entity_id")
        if not isinstance(eid, str) or "." not in eid:
            continue
        state = st.get("state")
        attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
        domains[eid.split(".", 1)[0]] += 1
        if state == "unavailable":
            unavailable += 1
        if eid.startswith("update."):
            if len(update_entities) < MAX_UPDATES:
                update_entities.append(eid)
            if update_is_pending(st) and len(pending) < MAX_PENDING:
                pending_ids.add(eid)
                pending.append({"entity_id": eid, "installed": attrs.get("installed_version"),
                                "latest": attrs.get("latest_version")})
            comp = _SUPERVISOR_UPDATES.get(eid)
            if comp and attrs.get("installed_version"):
                versions[comp] = _text(attrs["installed_version"])
        mark = _ADDON_MARK.match(eid)
        if mark:
            addon_slugs.add(mark.group(1) or mark.group(2))
        m = _STATS.match(eid)
        if m:
            stats.append((m.group(1), m.group(2), _number(state)))
        if eid in _DISK:
            disk[_DISK[eid]] = _number(state)
        if eid in _SOC:
            soc[_SOC[eid]] = _number(1.0 if state == "on" else 0.0 if state == "off" else state)

    for slug, kind, v in stats:
        if (slug in HASSIO_FIXED or slug in addon_slugs) and len(hassio) < MAX_HASSIO:
            hassio.append((slug, kind, v))

    for comp in ("supervisor", "os"):
        if comp in versions:
            add("homeassistant", "version", 1.0, "", component=comp, version=versions[comp])
    add("homeassistant", "entities_total", float(sum(domains.values())), "count")
    add("homeassistant", "unavailable_entities", float(unavailable), "count")
    for domain, n in sorted(domains.items()):
        add("homeassistant", "entities", float(n), "count", domain=domain)
    add("homeassistant", "updates_pending", float(len(pending)), "count")
    for p in pending:
        add("homeassistant", "update_pending", 1.0, "", entity_id=p["entity_id"])
    # An explicit 0 for every update entity that is not pending, with the same labels, so an
    # installed update supersedes the earlier pending reading instead of lingering as the latest
    # one. The versions are not labels, because a label change would start a new series.
    for eid in update_entities:
        if eid not in pending_ids:
            add("homeassistant", "update_pending", 0.0, "", entity_id=eid)

    for name, kind, v in hassio:
        add("hassio", kind, v, "%", name=name)
    for metric, v in sorted(disk.items()):
        add("hassio", metric, v, "GB")
    used, total = disk.get("disk_used_gb"), disk.get("disk_total_gb")
    if used is not None and total:
        add("hassio", "disk_used_pct", round(100 * used / total, 1), "%")
    for metric, v in sorted(soc.items()):
        add("ha_soc", metric, v)

    have_hassio = bool(hassio or disk)
    sources = [
        {"source": "homeassistant", "available": True},
        {"source": "hassio", "available": have_hassio, "present": have_hassio,
         "reason": ""},
        {"source": "ha_soc", "available": bool(soc), "present": bool(soc),
         "reason": ""},
    ]
    return Batch.model_validate({
        "schema_version": 1, "agent_version": AGENT_VERSION, "host": host,
        "platform": "homeassistant", "sent_at": now, "sources": sources,
        "samples": samples[:MAX_SAMPLES]})
