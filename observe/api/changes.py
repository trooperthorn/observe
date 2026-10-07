"""The change cursor (docs/DATA-API-DESIGN.md section 4.5).

`GET /api/v2/changes?since=<cursor>&wait=25` answers at once when any change domain has moved
since the cursor, and otherwise after `wait` seconds (at most 30) with the cursor unchanged. A
page uses it to refetch only the resources whose domains changed. The counters are read from
memory (Storage.change_seqs), so a waiting request costs no database access, and a cursor from a
database that was reset reads as changed, never as current.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Query

from ..storage.base import CHANGE_DOMAINS
from .cursor import decode, encode
from .models import ChangesOut
from .registry import ApiContext, ApiRegistry

POLL_S = 0.2  # how often a waiting request looks at the in-memory counters
MAX_WAIT_S = 30


def _counters(ctx: ApiContext) -> list[int]:
    seqs = ctx.store.storage.change_seqs()
    return [int(seqs.get(d, 0)) for d in CHANGE_DOMAINS]


def _previous(since: str) -> list[int] | None:
    values = decode(since)
    if len(values) != len(CHANGE_DOMAINS) or not all(isinstance(v, int) for v in values):
        return None  # written by another version: everything counts as changed
    return values


async def get_changes(ctx: ApiContext,
                      since: str | None = Query(None, max_length=512,
                                                description="The cursor of the previous answer"),
                      wait: int = Query(25, ge=0, le=MAX_WAIT_S,
                                        description="Seconds to wait for a change")
                      ) -> dict[str, Any]:
    """The change domains that moved since a cursor, waiting up to `wait` seconds for one."""
    current = _counters(ctx)
    if since is None:
        return {"cursor": encode(current), "changed": []}
    previous = _previous(since)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        if previous is None:
            changed = list(CHANGE_DOMAINS)
        else:
            changed = [d for d, a, b in zip(CHANGE_DOMAINS, current, previous) if a != b]
        left = deadline - loop.time()
        if changed or left <= 0:
            break
        await asyncio.sleep(min(POLL_S, left))
        current = _counters(ctx)
    return {"cursor": encode(current), "changed": changed}


def register(api: ApiRegistry) -> None:
    api.resource("/changes", get_changes, ChangesOut, tags=("changes",), etag=False,
                 summary="Change cursor, long poll")
