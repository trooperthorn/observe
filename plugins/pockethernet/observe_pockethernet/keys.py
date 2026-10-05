"""The wpf key scope: upload keys for Pockethernet field reports, bound to a device label.

A wpf key looks like `wpf_<12 hex prefix>_<secret>` and is stored in the core's
ingest_keys table with scope `wpf`. It is bound to a device label, for example
"sean-pixel", instead of a host name. The label is what the UI shows as the
source of each report, so a stolen phone is revoked by its label's key.

The rules live in the core (observe/ingest/keys.py): the key's marker and its
stored scope must both be wpf, and a wpf key is never valid for host ingest
because that surface asks for scope wpi. These helpers only fix the scope so
the upload endpoint cannot ask for the wrong one.
"""

from __future__ import annotations

from observe.ingest.keys import KeyInfo, create_key, key_host, verify_key
from observe.store import Store

SCOPE = "wpf"


async def create_field_key(store: Store, device: str, created_by: str = "") -> tuple[str, KeyInfo]:
    """Create an upload key for one device label. The plaintext is returned once."""
    return await create_key(store, device, created_by=created_by, scope=SCOPE)


async def field_key_device(store: Store, key: str) -> tuple[str, str] | None:
    """(prefix, device label) for a live wpf key, else None. A wpi key is always None."""
    return await key_host(store, key, scope=SCOPE)


async def verify_field_key(store: Store, key: str, device: str, now: float | None = None) -> bool:
    """True only for an unrevoked wpf key bound to exactly this device label."""
    return await verify_key(store, key, device, now=now, scope=SCOPE)
