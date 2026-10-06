"""POST /internal/v1/ingest (alias /api/ingest): authenticated, size-capped, rate-limited push endpoint.

This is the one write path that does not use a login, so it is the most
constrained. Order of checks, cheapest and least informative first:

1. Per-peer rate limit (429).
2. A bearer ingest key is required and must be valid and unrevoked (401). The
   body is not read before this passes.
3. The body is read with a hard cap of MAX_BODY_BYTES (413) and a nesting limit of
   MAX_JSON_DEPTH checked before parsing (400).
4. The host in the body must equal the host the key is bound to (403).
5. The body must validate against the wire schema (422). Unknown fields are ignored. Nothing is
   stored from a request that fails any check.

Denials are written to the audit log through DenialAggregator, adapted from
hostwatch/hub.py (hostwatch, same owner): at most one row per peer per window
with a count of the denials it covers, so a scanner can not grow the database.
A repeated batch_id is acknowledged without storing it again, which makes the
agent's outbox replay safe.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .. import audit, tiers
from ..config import Config
from ..store import Store
from .boot import classify_events
from .keys import key_host, verify_key
from .schema import MAX_BODY_BYTES, MAX_JSON_DEPTH, Batch

log = logging.getLogger("observe.ingest")


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


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header[:7].lower() != "bearer ":
        return None
    return header[7:].strip() or None


async def _read_capped(request: Request) -> bytes | None:
    """The request body, or None when it exceeds MAX_BODY_BYTES."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _too_deep(body: bytes) -> bool:
    """True when the JSON nesting depth exceeds MAX_JSON_DEPTH. A linear scan that ignores
    brackets inside strings, run before json.loads so a deeply nested body can not recurse."""
    depth = 0
    in_string = False
    escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return True
        elif byte in (0x5D, 0x7D):
            depth -= 1
    return False


def build_router(config: Config, store: Store,
                 clock: Callable[[], float] = time.monotonic) -> APIRouter:
    router = APIRouter()
    limiter = RateLimiter(config.server.ingest_rate_per_minute, clock)
    denials = DenialAggregator(clock)

    async def deny(request: Request, status: int, reason: str, actor: str = "",
                   extra: dict[str, Any] | None = None) -> JSONResponse:
        peer = request.client.host if request.client else "unknown"
        covered = denials.note(peer)
        if covered is not None:
            detail: dict[str, Any] = {"reason": reason, **(extra or {})}
            if covered:
                detail["denials_covered"] = covered
            await audit.record(store, "ingest_denied", actor=actor, method=request.method,
                               path=request.url.path, status=status, remote=peer,
                               detail=detail)
        headers = None
        if status == 429:
            headers = {"Retry-After": "60"}
        elif status == 401:
            headers = {"WWW-Authenticate": "Bearer"}
        return JSONResponse({"detail": reason}, status_code=status, headers=headers)

    # /internal/v1/ingest is the path unmodified hostwatch agents post to;
    # /api/ingest is kept as an alias.
    @router.post("/internal/v1/ingest", include_in_schema=False)
    @router.post("/api/ingest", include_in_schema=False)
    async def ingest(request: Request) -> JSONResponse:
        peer = request.client.host if request.client else "unknown"
        if not limiter.allow(peer):
            return await deny(request, 429, "rate limit exceeded")
        key = _bearer(request)
        bound = await key_host(store, key) if key else None
        if key is None or bound is None:
            return await deny(request, 401, "missing or invalid ingest key")
        prefix, bound_host = bound
        body = await _read_capped(request)
        if body is None:
            return await deny(request, 413, "body too large", prefix)
        if _too_deep(body):
            return await deny(request, 400, "body is nested too deeply", prefix)
        try:
            raw = json.loads(body)
        except (ValueError, RecursionError):
            return await deny(request, 422, "body is not valid JSON", prefix)
        claimed = raw.get("host") if isinstance(raw, dict) else None
        if not isinstance(claimed, str):
            return await deny(request, 422, "body has no host", prefix)
        if claimed != bound_host:
            return await deny(request, 403, "key is bound to another host", prefix,
                              {"claimed_host": claimed[:128], "bound_host": bound_host})
        try:
            batch = Batch.model_validate(raw)
        except ValidationError as exc:
            where = [".".join(str(p) for p in e["loc"]) for e in exc.errors()[:5]]
            # 422 is dead-lettered by hostwatch agents, so say why in the log.
            log.warning("ingest batch from %s rejected as invalid (422): %s", claimed[:128], where)
            return await deny(request, 422, "schema validation failed", prefix, {"fields": where})
        if not await verify_key(store, key, batch.host):
            return await deny(request, 401, "missing or invalid ingest key")
        try:
            n, e, duplicate = await store.ingest_batch(batch, classify_events(batch.events))
        except Exception as err:
            # Nothing was stored, so record that the batch failed partway.
            await audit.record(store, "ingest_failed", actor=prefix, method=request.method,
                               path=request.url.path, status=500, remote=peer,
                               detail={"host": batch.host, "error": type(err).__name__})
            raise
        out: dict[str, Any] = {"stored": n, "events_stored": e}
        if duplicate:
            out["duplicate"] = True
        return JSONResponse(out)

    @router.get("/internal/v1/agent-config", include_in_schema=False)
    async def agent_config(request: Request) -> JSONResponse:
        """The polling rates for the host the ingest key is bound to (section 10.1). The host is
        taken from the key, never from the request, so an agent can read only its own rates. The
        same limits and denial audit as ingest apply, and a key of another scope is refused."""
        peer = request.client.host if request.client else "unknown"
        if not limiter.allow(peer):
            return await deny(request, 429, "rate limit exceeded")
        key = _bearer(request)
        bound = await key_host(store, key) if key else None
        if key is None or bound is None:
            return await deny(request, 401, "missing or invalid ingest key")
        _, host = bound
        glob, hosts = await store.storage.read(tiers.load)
        return JSONResponse({"host": host, "intervals": tiers.effective(host, glob, hosts)},
                            headers={"Cache-Control": "no-store"})

    return router
