"""The ApiRegistry: how a resource joins /api/v2 (docs/DATA-API-DESIGN.md section 4.10).

A resource is a path, a handler and a response model. The registry mounts the route and does
everything around it, so no handler repeats it and a plugin cannot leave any of it out:

* authentication (session or read token, observe/api/principals.py) and the role check;
* the CSRF check for a session on an unsafe method;
* the rate limit, with a cost per resource;
* the ETag, built from the change counters of the domains the resource names, and a 304 or a
  cached body for an unchanged page, both answered before any read connection is opened;
* the read connection: a handler that names a `db` parameter runs on the read pool and receives
  a read-only connection, never the writer;
* cursor pagination and sparse fields;
* problem details for every failure, and a 503 with Retry-After when the read pool is busy.

A handler may take these parameters by name: `ctx` (the ApiContext), `db` (a read connection;
the handler must then be a plain function), `page` (PageParams, when `paginate=True`), `filters`
(the validated query model, when `filters=` is given) and `request`. Every other parameter is an
ordinary FastAPI path, query or body parameter and appears in the OpenAPI schema.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import logging
import threading
import time
import typing
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError

from .. import audit
from ..config import Config
from ..ingest.api import DenialAggregator
from ..storage.base import CHANGE_DOMAINS, StorageBusy, StorageTimeout
from ..store import Store
from . import cursor as cursor_mod
from .principals import RANK, Authenticator, Principal, csrf_ok
from .problems import ApiProblem, new_request_id
from .ratelimit import TokenBucket

log = logging.getLogger("observe.api")

CACHE_BYTES = 4 * 1024 * 1024
SPECIAL = ("ctx", "db", "page", "filters", "request")
SAFE_METHODS = ("GET", "HEAD")
JSON_TYPE = "application/json; charset=utf-8"
# The failed-credential allowance of one peer: a guessing client is stopped after a short burst.
FAIL_BURST = 20.0
FAIL_RATE = 0.2


class NotModified(Exception):
    def __init__(self, etag: str) -> None:
        super().__init__(etag)
        self.etag = etag


class CachedBody(Exception):
    def __init__(self, etag: str, body: bytes) -> None:
        super().__init__(etag)
        self.etag = etag
        self.body = body


async def on_not_modified(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, NotModified)
    return Response(status_code=304, headers={"ETag": exc.etag})


async def on_cached(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, CachedBody)
    return Response(exc.body, media_type=JSON_TYPE, headers={"ETag": exc.etag})


class ResponseCache:
    """The last rendered body per (route, query, role) with its ETag, at most CACHE_BYTES in all,
    least recently used first out. Touched only from the event loop, and guarded anyway."""

    def __init__(self, limit: int = CACHE_BYTES) -> None:
        self._limit = limit
        self._items: OrderedDict[tuple[str, str, str], tuple[str, bytes]] = OrderedDict()
        self._size = 0
        self._guard = threading.Lock()

    def get(self, key: tuple[str, str, str], etag: str) -> bytes | None:
        with self._guard:
            hit = self._items.get(key)
            if hit is None or hit[0] != etag:
                return None
            self._items.move_to_end(key)
            return hit[1]

    def put(self, key: tuple[str, str, str], etag: str, body: bytes) -> None:
        if len(body) > self._limit // 4:
            return
        with self._guard:
            old = self._items.pop(key, None)
            if old is not None:
                self._size -= len(old[1])
            self._items[key] = (etag, body)
            self._size += len(body)
            while self._size > self._limit and self._items:
                _, (_, dropped) = self._items.popitem(last=False)
                self._size -= len(dropped)

    @property
    def size(self) -> int:
        return self._size


class ApiRuntime:
    """What every v2 resource shares: the store, the clocks, the limiters and the caches."""

    def __init__(self, config: Config, store: Store, *, scheduler: Any = None,
                 alerter: Any = None, plugins: Any = None,
                 auth_clock: Callable[[], float] = time.time,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.store = store
        self.scheduler = scheduler
        self.alerter = alerter
        self.plugins = plugins
        self.wall = auth_clock
        self.clock = clock
        s = config.server
        self.limiter = TokenBucket(s.api_rate_per_second, s.api_burst, clock)
        self.failures = TokenBucket(FAIL_RATE, FAIL_BURST, clock)
        self.cache = ResponseCache()
        self.auth = Authenticator(config, store, auth_clock, clock)
        self.denials = DenialAggregator(clock)
        # Set by observe.api.build and observe.web once they exist: the resources registered so
        # far, and the infrastructure services the map and port resources read through.
        self.resources: list[Any] = []
        self.infra: Any = None

    async def denied(self, request: Request, status: int, reason: str,
                     principal: Principal | None = None) -> None:
        """Audit a refused request. Bounded like every other denial: one row per peer per window,
        with a count of the rest."""
        peer = request.client.host if request.client else "unknown"
        covered = self.denials.note(peer)
        if covered is None:
            return
        detail: dict[str, Any] = {"reason": reason}
        if covered:
            detail["denials_covered"] = covered
        await audit.record(self.store, "api_denied", actor=principal.name if principal else "",
                           method=request.method, path=request.url.path, status=status,
                           remote=peer, detail=detail)


@dataclass(frozen=True)
class ApiContext:
    """The `ctx` a handler receives."""

    principal: Principal
    runtime: ApiRuntime
    now: float

    @property
    def store(self) -> Store:
        return self.runtime.store

    @property
    def config(self) -> Config:
        return self.runtime.config

    @property
    def scheduler(self) -> Any:
        return self.runtime.scheduler

    @property
    def alerter(self) -> Any:
        return self.runtime.alerter


@dataclass
class Gate:
    principal: Principal
    etag: str | None
    cache_key: tuple[str, str, str] | None


@dataclass(frozen=True)
class Resource:
    method: str
    path: str
    operation_id: str
    owner: str | None
    roles: tuple[str, ...]
    domains: tuple[str, ...]
    anonymous: bool


def _hash(*parts: str, size: int = 6) -> str:
    h = hashlib.blake2b(digest_size=size)
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _etag_matches(header: str, etag: str) -> bool:
    if header.strip() == "*":
        return True
    mine = etag[2:] if etag.startswith("W/") else etag
    for part in header.split(","):
        theirs = part.strip()
        if theirs.startswith("W/"):
            theirs = theirs[2:]
        if theirs == mine:
            return True
    return False


class ApiRegistry:
    def __init__(self, app: FastAPI, runtime: ApiRuntime, *, owner: str | None = None,
                 resources: list[Resource] | None = None) -> None:
        self.app = app
        self.runtime = runtime
        self.owner = owner
        self.resources: list[Resource] = resources if resources is not None else []
        self._declared: set[str] = set()

    # ---- plugins ---------------------------------------------------------------------------

    def for_plugin(self, name: str) -> "ApiRegistry":
        """A registry that mounts only under /<name> and never lets a resource be read
        anonymously. Resources and operation ids are shared with the core's, so a clash fails."""
        return ApiRegistry(self.app, self.runtime, owner=name, resources=self.resources)

    async def submit_write(self, unit: Callable[[Any], Any], *, touches: Sequence[str] = ()) -> Any:
        """Run a write unit on the writer. A plugin may bump only the domains its resources
        declared, so it cannot make another plugin's pages look changed."""
        extra = [d for d in touches if d not in self._declared] if self.owner else []
        if extra:
            raise ValueError(f"domain {extra[0]!r} was not declared by a resource of "
                             f"{self.owner!r}")
        return await self.runtime.store.storage.write(unit, touches=tuple(touches))

    # ---- registration ----------------------------------------------------------------------

    def _check(self, path: str, method: str, roles: Sequence[str], domains: Sequence[str],
               operation_id: str) -> None:
        if not path.startswith("/") or path == "/" or "//" in path or ".." in path.split("/"):
            raise ValueError(f"resource path {path!r} must start with one /")
        if self.owner is not None and not (path == f"/{self.owner}"
                                           or path.startswith(f"/{self.owner}/")):
            raise ValueError(f"plugin {self.owner!r} may mount only under /{self.owner}, "
                             f"not {path!r}")
        if not roles or any(r not in ("viewer", "operator", "admin") for r in roles):
            raise ValueError("roles must name viewer, operator or admin")
        for d in domains:
            if d not in CHANGE_DOMAINS:
                raise ValueError(f"unknown change domain {d!r}")
        for r in self.resources:
            if r.operation_id == operation_id:
                raise ValueError(f"operation id {operation_id!r} is already used")
            if r.method == method and r.path == path:
                raise ValueError(f"{method} {path} is already registered")

    def resource(self, path: str, handler: Callable[..., Any], model: type[BaseModel], *,
                 domains: Sequence[str] = (), roles: Sequence[str] = ("viewer",),
                 tags: Sequence[str] = (), methods: Sequence[str] = ("GET",),
                 paginate: bool = False, filters: type[BaseModel] | None = None,
                 sparse: Sequence[str] | None = None, anonymous: bool = True, cost: float = 1.0,
                 memory: Callable[[], Any] | None = None, etag: bool = True,
                 counters: bool = True, summary: str | None = None, operation_id: str | None = None,
                 status_code: int = 200) -> None:
        """Mount `handler` at /api/v2<path>. `domains` are the change counters the response
        depends on (they are declared for clients, and also make the ETag unless `counters` is False,
        for a resource whose `memory` fingerprint already covers everything it shows), `memory` a
        fingerprint of the state it also depends on (a plain or an async callable), `roles`
        the roles allowed (the lowest listed is the least role that may read), `sparse` the item
        fields a client may select with `fields=`, `cost` the rate limit tokens one call takes."""
        for method in methods:
            name = operation_id or handler.__name__
            if self.owner:
                name = f"{self.owner}_{name}"
            self._check(path, method, roles, domains, name)
            self._mount(path, method, handler, model, name, tuple(domains), tuple(roles),
                        tuple(tags), paginate, filters, tuple(sparse) if sparse else None,
                        anonymous and self.owner is None, cost, memory,
                        etag and method in SAFE_METHODS, summary, status_code, counters)

    def _mount(self, path: str, method: str, handler: Callable[..., Any],
               model: type[BaseModel], operation_id: str, domains: tuple[str, ...],
               roles: tuple[str, ...], tags: tuple[str, ...], paginate: bool,
               filters: type[BaseModel] | None, sparse: tuple[str, ...] | None, anonymous: bool,
               cost: float, memory: Callable[[], Any] | None, use_etag: bool,
               summary: str | None, status_code: int, counters: bool = True) -> None:
        runtime = self.runtime
        min_rank = min(RANK[r] for r in roles)
        route_key = _hash(method, path, size=4)
        sig = inspect.signature(handler)
        hints = typing.get_type_hints(handler, include_extras=True)
        wants = {name for name in SPECIAL if name in sig.parameters}
        if "db" in wants and inspect.iscoroutinefunction(handler):
            raise ValueError(f"{operation_id}: a handler that takes db must be a plain function")
        if "page" in wants and not paginate:
            raise ValueError(f"{operation_id}: page needs paginate=True")
        if "filters" in wants and filters is None:
            raise ValueError(f"{operation_id}: filters needs a filters model")
        kind = inspect.Parameter.KEYWORD_ONLY
        params = [inspect.Parameter("request", kind, annotation=Request)]
        for name, p in sig.parameters.items():
            if name in SPECIAL:
                continue
            params.append(p.replace(kind=kind, annotation=hints.get(name, p.annotation)))
        if paginate:
            params.append(inspect.Parameter(
                "limit", kind, default=Query(
                    cursor_mod.DEFAULT_LIMIT, ge=1, le=cursor_mod.MAX_LIMIT,
                    description="The most items to return."), annotation=int))
            params.append(inspect.Parameter(
                "cursor", kind, default=Query(
                    None, max_length=cursor_mod.MAX_CURSOR_CHARS,
                    description="The next_cursor of the previous page."),
                annotation=str | None))
        if sparse:
            params.append(inspect.Parameter(
                "fields", kind, default=Query(
                    None, max_length=256,
                    description="Comma separated item fields to return: " + ", ".join(sparse)),
                annotation=str | None))
        filter_names: list[str] = []
        if filters is not None:
            # FastAPI flattens a query model only when it is the sole query parameter, so each
            # field of the model becomes a query parameter of its own and the model is built
            # from them for the handler.
            for fname, info in filters.model_fields.items():
                filter_names.append(fname)
                typed = (Annotated[(info.annotation, *info.metadata)] if info.metadata
                         else info.annotation)
                params.append(inspect.Parameter(
                    fname, kind, annotation=typed,
                    default=Query(... if info.is_required() else info.default,
                                  description=info.description)))
        signature = inspect.Signature(params)

        async def gate(request: Request) -> None:
            request.state.request_id = new_request_id()
            peer = request.client.host if request.client else "unknown"
            if runtime.failures.exhausted(peer):
                raise ApiProblem(429, "too many failed requests from this address",
                                 headers={"Retry-After": "5"})
            try:
                principal = await runtime.auth.authenticate(request)
            except ApiProblem as err:
                runtime.failures.take(peer)
                await runtime.denied(request, err.status, "bad token")
                raise
            if principal is None or (principal.kind == "anonymous" and not anonymous):
                runtime.failures.take(peer)
                await runtime.denied(request, 401, "no credentials", principal)
                raise ApiProblem(401, "sign in or send a read token",
                                 headers={"WWW-Authenticate": "Bearer"})
            # An anonymous reader of an anonymous-readable route reads at the viewer level.
            rank = RANK["viewer"] if principal.kind == "anonymous" else principal.rank
            if rank < min_rank:
                await runtime.denied(request, 403, "role", principal)
                raise ApiProblem(403, f"this needs the {roles[0]} role or above")
            if request.method not in SAFE_METHODS and not csrf_ok(request, principal):
                await runtime.denied(request, 403, "csrf", principal)
                raise ApiProblem(403, "missing or invalid CSRF token")
            wait = runtime.limiter.take(principal.key or f"a:{peer}", cost)
            if wait:
                await runtime.denied(request, 429, "rate limit", principal)
                raise ApiProblem(429, "rate limit exceeded", headers={"Retry-After": str(wait)})
            tag: str | None = None
            key: tuple[str, str, str] | None = None
            if use_etag:
                seqs = runtime.store.storage.change_seqs()
                seen = "-".join(str(seqs.get(d, 0)) for d in domains) or "0" if counters else "0"
                try:
                    fingerprint = memory() if memory is not None else ""
                    if inspect.isawaitable(fingerprint):
                        fingerprint = await fingerprint
                except (StorageBusy, StorageTimeout) as err:
                    raise ApiProblem(503, "the database is busy, try again shortly",
                                     headers={"Retry-After": "2"}) from err
                except Exception:
                    log.exception("v2 %s fingerprint failed (request %s)", path,
                                  getattr(request.state, "request_id", "?"))
                    raise ApiProblem(500, "the request could not be completed") from None
                # The path is part of the identity: /hosts/{name} is one route and many pages.
                query = request.url.path + "?" + "&".join(
                    f"{k}={v}" for k, v in sorted(request.query_params.multi_items()))
                tag = (f'W/"{route_key}-{seen}-{_hash(repr(fingerprint), size=4)}-'
                       f'{_hash(query, principal.role, principal.kind)}"')
                key = (route_key, query, principal.role)
                theirs = request.headers.get("if-none-match")
                if theirs and _etag_matches(theirs, tag):
                    raise NotModified(tag)
                body = runtime.cache.get(key, tag)
                if body is not None:
                    raise CachedBody(tag, body)
            request.state.gate = Gate(principal, tag, key)

        async def endpoint(**kw: Any) -> Response:
            request: Request = kw.pop("request")
            state: Gate = request.state.gate
            call: dict[str, Any] = {k: v for k, v in kw.items()
                                    if k in sig.parameters and k not in SPECIAL}
            if "request" in wants:
                call["request"] = request
            if "ctx" in wants:
                call["ctx"] = ApiContext(state.principal, runtime, runtime.wall())
            if paginate:
                page = cursor_mod.PageParams(kw["limit"], kw.get("cursor"))
                if "page" in wants:
                    call["page"] = page
            if "filters" in wants:
                try:
                    call["filters"] = filters(**{n: kw[n] for n in filter_names})
                except ValidationError as err:
                    raise ApiProblem(400, "the filters are not valid", errors=[
                        {"loc": ["query", *map(str, e["loc"])], "msg": str(e["msg"])}
                        for e in err.errors()]) from None
            wanted: list[str] | None = None
            if sparse:
                raw = kw.get("fields")
                if raw:
                    wanted = [f.strip() for f in raw.split(",") if f.strip()]
                    bad = [f for f in wanted if f not in sparse]
                    if bad:
                        raise ApiProblem(400, f"unknown field {bad[0][:40]!r}; "
                                              f"use {', '.join(sparse)}")
            try:
                if "db" in wants:
                    result = await runtime.store.storage.read(
                        lambda db: handler(db=db, **call))
                else:
                    result = handler(**call)
                    if inspect.isawaitable(result):
                        result = await result
                # The answer must fit the declared model, so the schema never lies.
                out = result if isinstance(result, model) else model.model_validate(result)
                data = out.model_dump(mode="json", by_alias=True)
            except ApiProblem:
                raise
            except (StorageBusy, StorageTimeout) as err:
                raise ApiProblem(503, "the database is busy, try again shortly",
                                 headers={"Retry-After": "2"}) from err
            except Exception:
                log.exception("v2 %s %s failed (request %s)", request.method, path,
                              getattr(request.state, "request_id", "?"))
                raise ApiProblem(500, "the request could not be completed") from None
            if wanted:
                data["items"] = [{k: item[k] for k in wanted if k in item}
                                 for item in data["items"]]
            body = _dump(data)
            headers: dict[str, str] = {"X-Request-Id": request.state.request_id}
            if state.etag:
                headers["ETag"] = state.etag
                if state.cache_key is not None:
                    runtime.cache.put(state.cache_key, state.etag, body)
            return Response(body, status_code=status_code, media_type=JSON_TYPE, headers=headers)

        endpoint.__signature__ = signature  # type: ignore[attr-defined]
        endpoint.__name__ = operation_id
        endpoint.__doc__ = handler.__doc__
        security: list[dict[str, list[str]]] = [{"bearerAuth": []}, {"sessionCookie": []}]
        if anonymous:
            security.append({})
        self.app.add_api_route(
            path, endpoint, methods=[method], response_model=model, status_code=status_code,
            operation_id=operation_id, tags=list(tags) or None, summary=summary,
            dependencies=[Depends(gate)], responses=ERROR_RESPONSES,
            openapi_extra={"security": security, "x-roles": list(roles),
                           "x-change-domains": list(domains), "x-rate-cost": cost})
        self.resources.append(Resource(method, path, operation_id, self.owner, roles, domains,
                                       anonymous))
        self._declared.update(domains)


def _scrub(v: Any) -> Any:
    """The same data with every NaN or infinite float replaced by None, because JSON has no such
    number."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _scrub(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub(x) for x in v]
    return v


def _dump(data: Any) -> bytes:
    data = _scrub(data)
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


class Problem(BaseModel):
    """RFC 9457 problem details."""

    type: str
    title: str
    status: int
    detail: str
    instance: str
    request_id: str
    errors: list[dict[str, Any]] | None = None


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": Problem, "description": text} for code, text in (
        (400, "The request is invalid."), (401, "No valid session or read token."),
        (403, "The role is too low, or the CSRF token is missing."),
        (429, "Rate limit exceeded. Retry-After says when to retry."),
        (500, "An internal error."), (503, "The database is busy. Retry-After says when."))
}
ERROR_RESPONSES[304] = {"description": "Unchanged since the ETag in If-None-Match."}
