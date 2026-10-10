"""The action catalogue: which parameters each action takes, and the checks made before a
request is queued (docs/CONTROL.md).

Observe checks the shape of every parameter and, when the host has reported a thermalctl
controller, that the header exists there. `control_form` adds what Observe knows of the host's
daemon: whether there is one, the controller it drives and, for a host enrolled with control,
the saved allowlist with the rules control.toml is written from. The console offers only the
choices it allows, and `validate` refuses the same requests the console would not make. The
host's own allowlist still has the final say.
"""

from __future__ import annotations

import re
from typing import Any

from observe.enrol import controller_id
from observe.updates import UPDATABLE_PLATFORMS, supports_update

from .queue import ACTIONS, UPDATE, QueueError

CONTROLLERS = ("thermalctl", "thermal-control-suite")
MODES = ("dry_run", "active")
# What agent.update may update: the agent container, the control daemon, or both. The
# daemon's own [update] allowlist (docs/CONTROL.md) has the final say on each.
COMPONENTS = ("agent", "control", "all")
_HEADER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,31}")
_SERVICE = re.compile(r"[A-Za-z0-9_.@:-]{1,128}")
_FAN_METRICS = ("fan", "fan_duty", "fan_target")


def capabilities(sample_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What the host last reported: whether thermalctl reports and the headers it names, as
    controller ids. thermalctl reports `observe.thermal.fan.*` with hw.id `fan:pwm1`; the older
    `fan_duty` rows carry the header as a label.

    `headers` is None when thermalctl has reported no fan, so nothing is known about it.
    """
    headers: set[str] = set()
    seen = False
    for s in sample_rows:
        if s.get("source") != "thermalctl":
            continue
        seen = True
        labels = s.get("labels") or {}
        metric = str(s.get("metric") or "")
        name: Any = None
        if metric in _FAN_METRICS:
            name = labels.get("header") or labels.get("fan")
        elif metric.startswith("observe.thermal.fan."):
            hw_id = labels.get("hw.id")
            if isinstance(hw_id, str) and hw_id.startswith("fan:"):
                name = hw_id[len("fan:"):]
        if isinstance(name, str) and name:
            headers.add(controller_id(name))
    return {"thermalctl": seen, "headers": sorted(headers) if headers else None}


def controller_for(platform: str) -> str:
    """The fan controller a host's daemon drives. The control.toml Observe writes names
    thermalctl (scripts.control_toml); Thermal Control Suite is the Windows controller."""
    return "thermal-control-suite" if platform == "windows" else "thermalctl"


def allowlist_view(allow: dict[str, Any]) -> dict[str, Any]:
    """The saved allowlist as the control form needs it. Each fan header carries its controller
    id and its floor. control.toml holds one `min_duty_floor` for the whole fan table, the
    largest `min_duty_limit` of any header (scripts.control_toml), and the daemon refuses a
    request below it, so that is the floor of every header."""
    fans: list[dict[str, Any]] = []
    for fan in allow.get("fans") or []:
        header = fan["header"] if isinstance(fan, dict) else fan
        limit = fan.get("min_duty_limit") if isinstance(fan, dict) else None
        fans.append({"header": header, "controller_id": controller_id(header),
                     "min_duty_limit": limit})
    limits = [f["min_duty_limit"] for f in fans if f["min_duty_limit"] is not None]
    floor = max(limits) if limits else 0
    for f in fans:
        f["floor"] = floor
    return {"fans": fans, "services": list(allow.get("services") or []),
            "reboot": allow.get("reboot") is True, "update": allow.get("update") is True,
            "min_duty_floor": floor}


def _offered(action: str, allow: dict[str, Any] | None, platform: str,
             agent_version: str) -> bool:
    if action == "fan.set_floor":
        return allow is None or bool(allow["fans"])
    if action == "fan.set_mode":
        # The control.toml Observe writes says allow_mode_change = false.
        return allow is None
    if action == "service.restart":
        return allow is None or bool(allow["services"])
    if action == "host.reboot":
        return allow is None or bool(allow["reboot"])
    if action == UPDATE:
        # Only an agent Observe can update (observe/updates.py agent_row).
        return (platform in UPDATABLE_PLATFORMS and supports_update(agent_version)
                and (allow is None or bool(allow["update"])))
    return False


def control_form(*, platform: str, agent_version: str, has_key: bool, pulled: bool,
                 enrolled: bool, control_chosen: bool, allowlist: dict[str, Any] | None,
                 reported: dict[str, Any]) -> dict[str, Any]:
    """Whether a host can be sent control requests, and the choices a request may make.

    Control is available when the host has an unrevoked wpc key that its daemon has pulled
    with. That is the only proof a daemon is there: an enrolment with control chosen whose
    install never ran has none, and a key made by hand in the key screen has no enrolment.
    For a host enrolled with control, `allowlist` is the saved allowlist and every choice is
    limited to it; otherwise it is None and the host's own file decides. `fan_headers` is the
    list the Header choice is made from (None when nothing is known), and `headers` stays what
    thermalctl reported.
    """
    reason = ""
    if not has_key:
        reason = ("control was not chosen for this host" if enrolled and not control_chosen
                  else "this host has no control daemon")
    elif not pulled:
        reason = "the control daemon has never pulled"
    allow = allowlist_view(allowlist or {}) if enrolled and control_chosen else None
    actions = [] if reason else [a for a in ACTIONS
                                 if _offered(a, allow, platform, agent_version)]
    fan_headers: list[dict[str, Any]] | None = None
    if allow is not None:
        fan_headers = allow["fans"]
    elif reported.get("headers"):
        fan_headers = [{"header": h, "controller_id": h, "min_duty_limit": None, "floor": 0}
                       for h in reported["headers"]]
    return {**reported, "available": not reason, "reason": reason, "actions": actions,
            "controller": controller_for(platform), "allowlist": allow,
            "fan_headers": fan_headers,
            # The control.toml Observe writes has [update] control = false.
            "components": list(COMPONENTS) if allow is None else ["agent"]}


def _only(params: dict[str, Any], keys: tuple[str, ...]) -> None:
    if set(params) != set(keys):
        raise QueueError(f"parameters must be exactly: {', '.join(keys) or 'none'}", 422)


def _controller(params: dict[str, Any], caps: dict[str, Any]) -> str:
    value = params["controller"]
    if value not in CONTROLLERS:
        raise QueueError(f"controller must be one of {', '.join(CONTROLLERS)}", 422)
    chosen = caps.get("controller")
    if chosen and value != chosen:
        raise QueueError(f"this host's fan controller is {chosen}", 422)
    return str(value)


def _allowed_fan(header: str, duty: int, allow: dict[str, Any]) -> None:
    fan = next((f for f in allow["fans"] if f["controller_id"] == header), None)
    if fan is None:
        names = [f["header"] if f["header"] == f["controller_id"]
                 else f"{f['header']} ({f['controller_id']})" for f in allow["fans"]]
        raise QueueError("this host's allowlist does not have that header; it allows: "
                         + (", ".join(names) or "none"), 422)
    if duty < fan["floor"]:
        raise QueueError(f"min_duty must be at least {fan['floor']}, the floor this host's "
                         f"allowlist sets for {fan['header']}", 422)


def validate(action: str, params: Any, caps: dict[str, Any]) -> dict[str, Any]:
    """Return the params to queue, or raise QueueError (422).

    `caps` is what the host reported (`capabilities`), or a `control_form` answer, which also
    holds the request to the host's daemon, its controller and its saved allowlist: the same
    choices the control form offers.
    """
    if not isinstance(params, dict):
        raise QueueError("params must be an object", 422)
    if caps.get("available") is False:
        raise QueueError(caps.get("reason") or "this host has no control daemon", 422)
    if action in ACTIONS and "actions" in caps and action not in caps["actions"]:
        raise QueueError(f"this host does not allow {action}", 422)
    allow = caps.get("allowlist")
    if action == "fan.set_floor":
        _only(params, ("controller", "header", "min_duty"))
        controller = _controller(params, caps)
        header, duty = params["header"], params["min_duty"]
        if not isinstance(header, str) or not _HEADER.fullmatch(header):
            raise QueueError("header must be 1 to 32 letters, digits, dashes or "
                             "underscores and must not start with a dash", 422)
        if isinstance(duty, bool) or not isinstance(duty, int) or not 0 <= duty <= 100:
            raise QueueError("min_duty must be a whole number from 0 to 100", 422)
        # The signed command names the controller's id, which control.toml lists.
        header = controller_id(header)
        known = caps.get("headers")
        if controller == "thermalctl" and known is not None and header not in known:
            raise QueueError("this host has not reported that header; it reported: "
                             + ", ".join(known), 422)
        if allow is not None:
            _allowed_fan(header, duty, allow)
        return {"controller": controller, "header": header, "min_duty": duty}
    if action == "fan.set_mode":
        _only(params, ("controller", "mode"))
        controller = _controller(params, caps)
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
        if allow is not None and name not in allow["services"]:
            raise QueueError("this host's allowlist does not have that service; it allows: "
                             + (", ".join(allow["services"]) or "none"), 422)
        return {"name": name}
    if action == "host.reboot":
        _only(params, ())
        return {}
    if action == "agent.update":
        _only(params, ("component",))
        if params["component"] not in COMPONENTS:
            raise QueueError(f"component must be one of {', '.join(COMPONENTS)}", 422)
        components = caps.get("components")
        if components is not None and params["component"] not in components:
            raise QueueError(f"this host can update only: {', '.join(components)}", 422)
        return {"component": params["component"]}
    raise QueueError(f"unknown action {action!r}", 422)
