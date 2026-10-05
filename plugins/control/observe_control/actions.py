"""The action catalogue: which parameters each action takes, and the checks made before a
request is queued (docs/CONTROL.md).

Observe checks the shape of every parameter and, when the host has reported a thermalctl
controller, that the header exists there. The host's own allowlist still has the final say.
"""

from __future__ import annotations

import re
from typing import Any

from .queue import QueueError

CONTROLLERS = ("thermalctl", "thermal-control-suite")
MODES = ("dry_run", "active")
_HEADER = re.compile(r"[A-Za-z0-9_.-]{1,32}")
_SERVICE = re.compile(r"[A-Za-z0-9_.@:-]{1,128}")
_FAN_METRICS = ("fan", "fan_duty", "fan_target")


def capabilities(sample_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What the host last reported: whether thermalctl reports and the headers it names.

    `headers` is None when thermalctl has reported no fan, so nothing is known about it.
    """
    headers: set[str] = set()
    seen = False
    for s in sample_rows:
        if s.get("source") != "thermalctl":
            continue
        seen = True
        if s.get("metric") in _FAN_METRICS:
            labels = s.get("labels") or {}
            name = labels.get("header") or labels.get("fan")
            if isinstance(name, str) and name:
                headers.add(name)
    return {"thermalctl": seen, "headers": sorted(headers) if headers else None}


def _only(params: dict[str, Any], keys: tuple[str, ...]) -> None:
    if set(params) != set(keys):
        raise QueueError(f"parameters must be exactly: {', '.join(keys) or 'none'}", 422)


def _controller(params: dict[str, Any]) -> str:
    value = params["controller"]
    if value not in CONTROLLERS:
        raise QueueError(f"controller must be one of {', '.join(CONTROLLERS)}", 422)
    return str(value)


def validate(action: str, params: Any, caps: dict[str, Any]) -> dict[str, Any]:
    """Return the params to queue, or raise QueueError (422)."""
    if not isinstance(params, dict):
        raise QueueError("params must be an object", 422)
    if action == "fan.set_floor":
        _only(params, ("controller", "header", "min_duty"))
        controller = _controller(params)
        header, duty = params["header"], params["min_duty"]
        if not isinstance(header, str) or not _HEADER.fullmatch(header):
            raise QueueError("header must be 1 to 32 letters, digits, dots, dashes or "
                             "underscores", 422)
        if isinstance(duty, bool) or not isinstance(duty, int) or not 0 <= duty <= 100:
            raise QueueError("min_duty must be a whole number from 0 to 100", 422)
        known = caps.get("headers")
        if controller == "thermalctl" and known is not None and header not in known:
            raise QueueError("this host has not reported that header; it reported: "
                             + ", ".join(known), 422)
        return {"controller": controller, "header": header, "min_duty": duty}
    if action == "fan.set_mode":
        _only(params, ("controller", "mode"))
        controller = _controller(params)
        if params["mode"] not in MODES:
            raise QueueError(f"mode must be one of {', '.join(MODES)}", 422)
        if controller == "thermalctl" and caps.get("thermalctl") is False:
            raise QueueError("this host has not reported a thermalctl controller", 422)
        return {"controller": controller, "mode": params["mode"]}
    if action == "service.restart":
        _only(params, ("name",))
        name = params["name"]
        if not isinstance(name, str) or not _SERVICE.fullmatch(name):
            raise QueueError("name must be 1 to 128 letters, digits or . _ @ : -", 422)
        return {"name": name}
    if action == "host.reboot":
        _only(params, ())
        return {}
    raise QueueError(f"unknown action {action!r}", 422)
