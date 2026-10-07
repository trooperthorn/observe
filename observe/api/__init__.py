"""The /api/v2 read API (docs/DATA-API-DESIGN.md section 4).

`build` makes the sub-application the web app mounts at /api/v2. It has its own exception
handlers, so the problem details of this surface never change the answers of any other route,
and its own OpenAPI schema, which is committed as docs/openapi-v2.json and checked by a test.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from . import changes, events, hosts, metrics, monitors, problems
from .registry import (ApiContext, ApiRegistry, ApiRuntime, CachedBody, NotModified,
                       on_cached, on_not_modified)

PREFIX = "/api/v2"
VERSION = "2.0.0"
DESCRIPTION = (
    "The read API of Observe. Sign in with the console (a session cookie) or send a read "
    "token as `Authorization: Bearer wpr_...`. Responses carry an ETag; send it back in "
    "If-None-Match to get a 304. Lists are `{items, next_cursor}`. Errors are RFC 9457 "
    "problem details. Changes are additive within /api/v2.")
__all__ = ["ApiContext", "ApiRegistry", "ApiRuntime", "PREFIX", "build", "build_openapi"]


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """The schema, with the security schemes and the problem media type of every error."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description,
                         routes=app.routes, servers=app.servers, openapi_version="3.1.0")
    schema.setdefault("components", {})["securitySchemes"] = {
        "bearerAuth": {"type": "http", "scheme": "bearer",
                       "description": "A read token (wpr_...) created by an admin."},
        "sessionCookie": {"type": "apiKey", "in": "cookie", "name": "observe_session",
                          "description": "The console login session. An unsafe method also "
                                         "needs the X-CSRF-Token header."},
    }
    for path in schema["paths"].values():
        for op in path.values():
            for code, response in op.get("responses", {}).items():
                content = response.get("content")
                if code.startswith(("4", "5")) and content and "application/json" in content:
                    content["application/problem+json"] = content.pop("application/json")
    app.openapi_schema = schema
    return schema


def build(runtime: ApiRuntime) -> tuple[FastAPI, ApiRegistry]:
    app = FastAPI(title="Observe API", version=VERSION, description=DESCRIPTION, docs_url=None,
                  redoc_url=None, openapi_url=None, servers=[{"url": PREFIX}])
    problems.install(app)
    app.add_exception_handler(NotModified, on_not_modified)
    app.add_exception_handler(CachedBody, on_cached)
    app.openapi = lambda: build_openapi(app)  # type: ignore[method-assign]
    registry = ApiRegistry(app, runtime)
    if runtime.scheduler is not None:
        monitors.register(registry)
        hosts.register(registry)
    events.register(registry)
    metrics.register(registry)
    changes.register(registry)

    @app.get("/openapi.json", include_in_schema=False)
    async def schema() -> JSONResponse:
        # The schema describes routes that are public in the repository, so it needs no login.
        return JSONResponse(build_openapi(app))

    return app, registry
