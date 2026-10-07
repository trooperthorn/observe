"""The reads the admin pages of the console draw (docs/DATA-API-DESIGN.md section 5.4): the
dependency edges and the unlinked switches of the infrastructure admin page, and the enrolment and
settings documents of one host.

Every one needs the admin role, except the dependency edges, which any signed-in caller reads like
the map. None sends an ETag: an enrolment moves as the host fetches the script and reports, and
no change domain counts that. A document never holds a token or a key. The services behind them
(the dependency mapper, the switch matcher, the enrolment store) are the ones the console already
had, reached through `runtime.infra` and `runtime.console`, which `observe/web.py` sets.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from .problems import ApiProblem
from .registry import ApiContext, ApiRegistry


class Document(BaseModel):
    """A document whose named fields are always present; others may follow."""

    model_config = ConfigDict(extra="allow")


class UnlinkedList(BaseModel):
    items: list[dict[str, Any]]


def _service(ctx: ApiContext, name: str) -> Any:
    found = getattr(ctx.runtime, name, None)
    if found is None:
        raise ApiProblem(503, "the console services are not running")
    return found


async def dependencies(ctx: ApiContext) -> dict[str, Any]:
    """Applied, pending, refused and rejected dependency edges, computed now."""
    return (await _service(ctx, "infra").mapper.refresh()).as_dict()


async def unlinked(ctx: ApiContext) -> dict[str, Any]:
    """Switches seen in the field that match no monitor, waiting for an admin to link."""
    return {"items": await _service(ctx, "infra").matcher.unlinked()}


async def host_enrolment(host: str, ctx: ApiContext, request: Request) -> dict[str, Any]:
    """Progress of one enrolment: script fetched, first data, control first pull, ready or
    expired. It never returns the token or a key."""
    remote = request.client.host if request.client else ""
    state = await _service(ctx, "console").enrolment(host, ctx.principal.name, remote)
    if state is None:
        raise ApiProblem(404, "no enrolment for this host")
    return state


async def host_settings(host: str, ctx: ApiContext) -> dict[str, Any]:
    """What the host settings page shows: identity, the saved allowlist, whether the host has
    picked it up, and the newest update or cleanup task."""
    got = await _service(ctx, "console").host_settings(host)
    if got is None:
        raise ApiProblem(404, "unknown host")
    return got


def register(api: ApiRegistry) -> None:
    api.resource("/infra/dependencies", dependencies, Document, etag=False, tags=("network",),
                 anonymous=False, summary="Dependency edges by decision")
    api.resource("/admin/infra/unlinked", unlinked, UnlinkedList, etag=False, tags=("admin",),
                 roles=("admin",), anonymous=False, summary="Switches that match no monitor")
    api.resource("/hosts/{host}/enrolment", host_enrolment, Document, etag=False,
                 tags=("admin",), roles=("admin",), anonymous=False,
                 summary="Enrolment progress of one host")
    api.resource("/hosts/{host}/settings", host_settings, Document, etag=False,
                 tags=("admin",), roles=("admin",), anonymous=False,
                 summary="Settings document of one host")
