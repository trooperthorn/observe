"""Status rollup: dependency reachability and group state.

Two ideas, both long-established in network monitoring (see
docs/PRIOR-ART.md for sources):

1. Reachability. If a monitor's parent (the switch in front of a server, the
   hypervisor under a VM) is DOWN, the child's own failure tells you nothing
   new. The child is reported UNREACHABLE and its alerts are suppressed, so
   one outage produces one page. This is the host-dependency model Nagios
   documents as UP / DOWN / UNREACHABLE.

2. Group state is the worst member state ("worst-of"), the rule Big Brother
   used to colour its display. One refinement: a member marked
   `critical: false` can degrade its group to WARN but never to DOWN.

Effective states, worst first: down, unreachable, warn, pending, up.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .state import MonitorState, State

SEVERITY = {"down": 4, "unreachable": 3, "warn": 2, "pending": 1, "up": 0}


class Rollup:
    def __init__(self, config: Config, states: dict[str, MonitorState]) -> None:
        self.config = config
        self.states = states
        self.by_slug = {m.slug: m for m in config.monitors if m.enabled}

    def _parents(self, slug: str) -> list[Any]:
        return [p for p in self.config.parents(self.by_slug[slug]) if p.slug in self.by_slug]

    def blocking_parent(self, slug: str) -> str | None:
        """Name of the nearest ancestor that is DOWN or itself unreachable.

        A parent is followed up the chain: if the core switch is DOWN, the
        access switch behind it is UNREACHABLE, and the server behind that is
        UNREACHABLE too, attributed to the core switch.
        """
        for parent in self._parents(slug):
            if self.states[parent.slug].state is State.DOWN:
                upstream = self.blocking_parent(parent.slug)
                return upstream or parent.name
            upstream = self.blocking_parent(parent.slug)
            if upstream and self.states[parent.slug].state is not State.UP:
                return upstream
        return None

    def effective(self, slug: str) -> tuple[str, str | None]:
        """(effective state, name of the blocking ancestor or None)."""
        own = self.states[slug].state
        if own in (State.DOWN, State.WARN):
            blocker = self.blocking_parent(slug)
            if blocker:
                return "unreachable", blocker
        return own.value, None

    def group_states(self) -> dict[str, dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for slug, mon in self.by_slug.items():
            eff, _ = self.effective(slug)
            counted = eff
            if not mon.critical and eff in ("down", "unreachable"):
                counted = "warn"
            g = groups.setdefault(mon.group, {"state": "up", "counts": {}, "worst": []})
            g["counts"][eff] = g["counts"].get(eff, 0) + 1
            if SEVERITY[counted] > SEVERITY[g["state"]]:
                g["state"], g["worst"] = counted, [mon.name]
            elif SEVERITY[counted] == SEVERITY[g["state"]] and counted != "up":
                g["worst"].append(mon.name)
        return groups
