"""Problem details for /api/v2 (RFC 9457, docs/DATA-API-DESIGN.md section 4.6).

Every error on the v2 surface is `application/problem+json` with a `type`, `title`, `status`,
`detail`, `instance` (the request path) and a `request_id` that is also returned in the
`X-Request-Id` header and written to the log, so a report from a user can be found. A 500 never
carries a stack trace or an exception message.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

BASE = "https://observe.local/problems/"
MEDIA_TYPE = "application/problem+json"

# status -> (type, title)
KINDS: dict[int, tuple[str, str]] = {
    400: ("validation", "Invalid request"),
    401: ("unauthenticated", "Authentication required"),
    403: ("forbidden", "Not allowed"),
    404: ("not-found", "Not found"),
    405: ("method-not-allowed", "Method not allowed"),
    409: ("conflict", "Conflict"),
    412: ("precondition-failed", "Precondition failed"),
    413: ("payload-too-large", "Payload too large"),
    429: ("rate-limited", "Too many requests"),
    500: ("internal", "Internal error"),
    503: ("busy", "Temporarily unavailable"),
}


class ApiProblem(Exception):
    """Raised anywhere on the v2 surface to answer with a problem document."""

    def __init__(self, status: int, detail: str = "", *, errors: list[dict[str, Any]] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.errors = errors
        self.headers = headers or {}


def new_request_id() -> str:
    return uuid.uuid4().hex


def problem_response(request: Request, status: int, detail: str = "", *,
                     errors: list[dict[str, Any]] | None = None,
                     headers: dict[str, str] | None = None) -> JSONResponse:
    kind, title = KINDS.get(status, ("error", "Error"))
    request_id = getattr(request.state, "request_id", None) or new_request_id()
    body: dict[str, Any] = {"type": BASE + kind, "title": title, "status": status,
                            "detail": detail or title, "instance": request.url.path,
                            "request_id": request_id}
    if errors:
        body["errors"] = errors
    out = {"X-Request-Id": request_id, **(headers or {})}
    return JSONResponse(body, status_code=status, media_type=MEDIA_TYPE, headers=out)


async def on_problem(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiProblem)
    return problem_response(request, exc.status, exc.detail, errors=exc.errors,
                            headers=exc.headers)


async def on_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Where and why, never the rejected value, which the caller controls.
    errors = [{"loc": [str(x) for x in e.get("loc", ())], "msg": str(e.get("msg", ""))}
              for e in exc.errors()]
    first = errors[0] if errors else None
    detail = f"{'.'.join(first['loc'])}: {first['msg']}" if first else "the request is invalid"
    return problem_response(request, 400, detail, errors=errors)


async def on_http(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    detail = exc.detail if isinstance(exc.detail, str) else ""
    if exc.status_code == 404 and detail == "Not Found":
        detail = "no such resource"
    if exc.status_code == 405 and detail == "Method Not Allowed":
        detail = "that method is not supported here"
    headers = dict(exc.headers or {})
    return problem_response(request, exc.status_code, detail, headers=headers)


def install(app: Any) -> None:
    app.add_exception_handler(ApiProblem, on_problem)
    app.add_exception_handler(RequestValidationError, on_validation)
    app.add_exception_handler(StarletteHTTPException, on_http)
