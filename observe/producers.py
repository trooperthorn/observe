"""How a host's data reaches Observe: pushed by an agent with an ingest key, or polled by Observe.

A Home Assistant monitor in `mode: host` and an SNMP monitor with `host_name` are polled by
Observe: the check reads the device with the monitor's own credential (a Home Assistant
long-lived token, an SNMP community or user) and stores the readings in process through
`Store.ingest_batch`. Nothing is pushed, so such a host has no ingest key, and "Active keys: none"
on its settings page is correct. The batch names its producer in `agent_version`
("observe-ha-host", "observe-snmp-host"), which `store.PULL_PRODUCER_RANK` ranks; that marker is
not an agent version, so the pages show `agent_label` instead.

Every pushed batch goes through POST /v1/metrics or /v1/logs (observe/otlp/api.py), which accept
only a listed, unrevoked ingest key.
"""

from __future__ import annotations

from typing import Any

# The producer markers of observe/checks/ha_host.py and observe/checks/snmp.py.
POLLED_LABELS = {
    "observe-ha-host": "polled by Observe (Home Assistant monitor)",
    "observe-snmp-host": "polled by Observe (SNMP monitor)",
}


def agent_label(agent_version: str | None) -> str:
    """What the pages show for a polled host's agent, or "" for a pushed agent's version."""
    return POLLED_LABELS.get(agent_version or "", "")


def pollers(monitors: list[Any], host: str) -> list[dict[str, str]]:
    """The enabled monitors that poll `host` and store their readings under it, with the name
    of the credential each one reads the device with (never the credential itself)."""
    out = []
    for m in monitors:
        if not getattr(m, "enabled", True):
            continue
        if m.type == "homeassistant" and m.mode == "host" and m.host_name == host:
            kind = "Home Assistant"
        elif m.type == "snmp" and m.host_name == host:
            kind = "SNMP"
        else:
            continue
        out.append({"monitor": m.name, "slug": m.slug, "type": m.type, "kind": kind,
                    "target": f"{m.host}", "credential": m.credential})
    return out
