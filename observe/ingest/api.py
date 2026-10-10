"""The guards shared by the ingest routes: the per-key rate limit, the per-peer limit on failures, the aggregated denial audit
and the agent-config route.

The per-key limit is a token bucket (observe/api/ratelimit.py): `server.ingest_burst` requests at
once, refilled at `server.ingest_rate_per_minute`. The burst lets an agent replay the outbox it
built during an outage or a restart (truenas-svr queued 1,625 requests) without a 429, while the
refill rate still bounds what one key can send over time. Every 429 says in `Retry-After` when the
next request will be accepted, and an agent must wait that long before it sends again.

Data is pushed only as OTLP (observe/otlp/api.py, POST /v1/metrics and /v1/logs). This module holds
what both routes share. Denials are written to the audit log through DenialAggregator, adapted
from hostwatch/hub.py (hostwatch, same owner): at most one row per peer per window with a count of
the denials it covers, so a scanner can not grow the database.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import audit, tiers
from ..config import Config
from ..store import Store
from .keys import key_host


class DenialAggregator:
    """Bounds audit growth from denied requests. The first denial from a peer is
    written. Further denials inside the window are only counted; the first one
    after the window closes is written with the count since the previous row. At
    most MAX_PEERS peers are tracked (extra peers share one bucket)."""

    WINDOW_S = 60.0
    MAX_PEERS = 4096
    OVERFLOW = "overflow"

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._state: dict[str, list[float]] = {}  # peer -> [last row time, unwritten count]

    def note(self, peer: str) -> int | None:
        """Record one denial. None means write no row, 0 a first row, otherwise
        the number of denials the row covers (including this one)."""
        now = self._clock()
        if peer not in self._state and len(self._state) >= self.MAX_PEERS:
            self._state = {k: v for k, v in self._state.items() if now - v[0] < self.WINDOW_S}
            if len(self._state) >= self.MAX_PEERS:
                peer = self.OVERFLOW
        entry = self._state.get(peer)
        if entry is None:
            self._state[peer] = [now, 0]
            return 0
        if now - entry[0] < self.WINDOW_S:
            entry[1] += 1
            return None
        covered = int(entry[1]) + 1
        self._state[peer] = [now, 0]
        return covered


class RateLimiter:
    """Fixed-window request counter per peer, bounded in memory."""

    MAX_PEERS = 4096
    WINDOW_S = 60.0

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = per_minute
        self._clock = clock
        self._state: dict[str, list[float]] = {}  # peer -> [window start, count]

    def allow(self, peer: str) -> bool:
        now = self._clock()
        if peer not in self._state and len(self._state) >= self.MAX_PEERS:
            self._state = {k: v for k, v in self._state.items() if now - v[0] < self.WINDOW_S}
            if len(self._state) >= self.MAX_PEERS:
                peer = "overflow"
        entry = self._state.get(peer)
        if entry is None or now - entry[0] >= self.WINDOW_S:
            self._state[peer] = [now, 1]
            return True
        entry[1] += 1
        return entry[1] <= self.limit


    def charge(self, peer: str, extra: int) -> None:
        """Count `extra` more requests against a peer's current window, for a request that cost
        more than one (a body refused only after it was inflated or decoded)."""
        entry = self._state.get(peer)
        if entry is not None and self._clock() - entry[0] < self.WINDOW_S:
            entry[1] += extra


def bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header[:7].lower() != "bearer ":
        return None
    return header[7:].strip() or None


class Guard:
    """The rate limit and the denial audit of the ingest routes, one instance for all of them."""

    def __init__(self, config: Config, store: Store,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        # Failed or missing keys, per peer. A valid key is never counted against its peer, so
        # hosts behind one NAT or proxy do not throttle each other.
        self.fail_limiter = RateLimiter(config.server.ingest_rate_per_minute, clock)
        # A valid key, per key: a token bucket, so a backlog replay fits in its burst. Imported
        # here because the observe.api package imports this module.
        from ..api.ratelimit import TokenBucket
        rate = config.server.ingest_rate_per_minute
        self.key_limiter = TokenBucket(rate / 60.0, max(config.server.ingest_burst, rate), clock)
        self.denials = DenialAggregator(clock)
        self.clock = clock

    async def deny(self, request: Request, status: int, reason: str, actor: str = "",
                   extra: dict[str, Any] | None = None, retry_after: int = 60) -> JSONResponse:
        """The refusal, audited. A 429 always carries Retry-After (`retry_after` seconds, the
        fixed window of the per-peer limit unless the key's bucket says sooner)."""
        peer = request.client.host if request.client else "unknown"
        covered = self.denials.note(peer)
        if covered is not None:
            detail: dict[str, Any] = {"reason": reason, **(extra or {})}
            if covered:
                detail["denials_covered"] = covered
            await audit.record(self.store, "ingest_denied", actor=actor, method=request.method,
                               path=request.url.path, status=status, remote=peer, detail=detail)
        headers = None
        if status == 429:
            headers = {"Retry-After": str(max(1, int(retry_after)))}
        elif status == 401:
            headers = {"WWW-Authenticate": "Bearer"}
        return JSONResponse({"detail": reason}, status_code=status, headers=headers)


def build_router(config: Config, store: Store, guard: Guard) -> APIRouter:
    router = APIRouter()
    deny = guard.deny

    @router.get("/internal/v1/agent-config", include_in_schema=False)
    async def agent_config(request: Request) -> JSONResponse:
        """The polling rates for the host the ingest key is bound to (section 10.1). The host is
        taken from the key, never from the request, so an agent can read only its own rates. The
        same limits and denial audit as ingest apply, and a key of another scope is refused."""
        peer = request.client.host if request.client else "unknown"
        key = bearer(request)
        bound = await key_host(store, key) if key else None
        if key is None or bound is None:
            if not guard.fail_limiter.allow(peer):
                return await deny(request, 429, "rate limit exceeded")
            return await deny(request, 401, "missing or invalid ingest key")
        prefix, host = bound
        wait = guard.key_limiter.take(prefix)
        if wait:
            return await deny(request, 429, "rate limit exceeded", prefix, retry_after=wait)
        glob, hosts = await store.storage.read(tiers.load)
        return JSONResponse({"host": host, "intervals": tiers.effective(host, glob, hosts)},
                            headers={"Cache-Control": "no-store"})

    return router
