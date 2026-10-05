"""The wpc key scope: control keys for the hostwatch-control daemon, bound to one host.

A wpc key looks like `wpc_<12 hex prefix>_<secret>` and is stored in the core's ingest_keys
table with scope `wpc`, bound to a host name. It can pull that host's commands and post that
host's results, and nothing else. The core refuses it on host ingest (which asks for wpi) and
on field reports (which ask for wpf), and refuses wpi and wpf keys on the control routes,
because the marker and the stored scope must both equal the scope the route asks for.

Admins create and revoke these keys through the existing key screen and the CLI with scope
wpc; the helpers here fix the scope so a control route cannot ask for the wrong one.
"""

from __future__ import annotations

from observe.ingest.keys import KeyInfo, create_key, key_host, verify_key
from observe.store import Store

SCOPE = "wpc"


async def create_control_key(store: Store, host: str,
                             created_by: str = "") -> tuple[str, KeyInfo]:
    """Create a control key for one host. The plaintext is returned once."""
    return await create_key(store, host, created_by=created_by, scope=SCOPE)


async def control_key_host(store: Store, key: str) -> tuple[str, str] | None:
    """(prefix, host) for a live wpc key, else None. A wpi or wpf key is always None."""
    return await key_host(store, key, scope=SCOPE)


async def verify_control_key(store: Store, key: str, host: str,
                             now: float | None = None) -> bool:
    """True only for an unrevoked wpc key bound to exactly this host."""
    return await verify_key(store, key, host, now=now, scope=SCOPE)
