"""HTTP surface: dashboard, JSON API, Prometheus metrics, and host ingest.

The dashboard endpoints do not change state. Adding, removing, or editing a
monitor means editing the YAML and restarting the container, which keeps the
config reviewable and means a stolen dashboard session can read your
inventory but can not change what is watched or silence an alert. The one
write path is POST /api/ingest (watchpost/ingest/api.py), which accepts pushed
host data with a host-bound ingest key and does not touch monitors.
"""

from __future__ import annotations

import base64
import hmac
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .alerts import Alerter
from .config import Config
from .ingest.api import build_router
from .scheduler import Scheduler
from .store import Store

STATIC = Path(__file__).parent / "static"
_STATE_NUM = {"pending": -1, "up": 0, "warn": 1, "down": 2}
_EFF_NUM = {**_STATE_NUM, "unreachable": 3}


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _monitor_view(mon: Any, st: Any, sched: Any) -> dict[str, Any]:
    last = st.last
    effective, blocker = sched.rollup.effective(mon.slug)
    fc = sched.forecasts.get(mon.slug)
    target = getattr(mon, "url", None) or getattr(mon, "host", None) or getattr(mon, "query", "")
    port = getattr(mon, "port", None)
    if port and not getattr(mon, "url", None):
        target = f"{target}:{port}"
    return {
        "slug": mon.slug,
        "name": mon.name,
        "group": mon.group,
        "type": mon.type,
        "mode": getattr(mon, "mode", None),
        "target": target,
        "state": st.state.value,
        "effective_state": effective,
        "blocked_by": blocker,
        "depends_on": [p.name for p in sched.config.parents(mon)],
        "critical": mon.critical,
        "forecast": fc.as_dict() if fc else None,
        "since": st.since,
        "last_at": st.last_at,
        "result": last.result.value if last else None,
        "message": last.message if last else "waiting for first poll",
        "value": last.value if last else None,
        "unit": last.unit if last else "",
        "latency_ms": last.latency_ms if last else None,
        "detail": last.detail if last else {},
    }


def create_app(config: Config, store: Store, scheduler: Scheduler, alerter: Alerter,
               ingest_clock: Callable[[], float] = time.monotonic) -> FastAPI:
    app = FastAPI(title="watchpost", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)
    user, pw = config.server.basic_auth_user, config.server.basic_auth_password

    def auth(request: Request) -> None:
        if not user:
            return
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                u, _, p = base64.b64decode(header[6:]).decode().partition(":")
                ok = hmac.compare_digest(u.encode(), user.encode()) & \
                    hmac.compare_digest(p.encode(), (pw or "").encode())
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="watchpost"'})

    guarded = [Depends(auth)]
    app.include_router(build_router(config, store, ingest_clock))

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        # Static assets are public (they hold no data); everything with data
        # sits behind `auth`. The CSP forbids inline script and third-party
        # origins, which backs up the textContent-only rendering in app.js.
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    by_slug = {m.slug: m for m in scheduler.monitors}

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", dependencies=guarded, include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/monitors", dependencies=guarded)
    async def monitors() -> dict[str, Any]:
        rows = [_monitor_view(m, scheduler.states[m.slug], scheduler)
                for m in scheduler.monitors]
        return {"version": __version__, "monitors": rows,
                "groups": scheduler.rollup.group_states(), "alerts": alerter.status}

    @app.get("/api/groups", dependencies=guarded)
    async def groups() -> dict[str, Any]:
        return scheduler.rollup.group_states()

    @app.get("/api/forecasts", dependencies=guarded)
    async def forecasts(refresh: bool = False) -> dict[str, Any]:
        """Current projections. refresh=true recomputes now instead of waiting
        for the hourly maintenance pass."""
        if refresh:
            await scheduler.refresh_forecasts()
        return {slug: f.as_dict() for slug, f in scheduler.forecasts.items()}

    @app.get("/api/monitors/{slug}/history", dependencies=guarded)
    async def history(slug: str, hours: float = 24) -> dict[str, Any]:
        if slug not in by_slug:
            raise HTTPException(404)
        hours = min(max(hours, 0.1), 24 * config.server.retention_days)
        return {
            "points": await store.history(slug, hours),
            "availability": await store.availability(slug, hours),
            "events": await store.events(50, slug),
        }

    @app.get("/api/events", dependencies=guarded)
    async def events(limit: int = 100) -> list[dict[str, Any]]:
        return await store.events(min(limit, 1000))

    @app.get("/metrics", dependencies=guarded, response_class=PlainTextResponse)
    async def metrics() -> str:
        lines = [
            "# HELP watchpost_state Own monitor state: -1 pending, 0 up, 1 warn, 2 down.",
            "# TYPE watchpost_state gauge",
        ]
        values = ["# HELP watchpost_value Last numeric value of the check.",
                  "# TYPE watchpost_value gauge"]
        for m in scheduler.monitors:
            st = scheduler.states[m.slug]
            labels = f'monitor="{m.slug}",group="{_label(m.group)}",type="{m.type}"'
            lines.append(f"watchpost_state{{{labels}}} {_STATE_NUM[st.state.value]}")
            if st.last and st.last.value is not None:
                values.append(f"watchpost_value{{{labels}}} {st.last.value}")
        eff = ["# HELP watchpost_effective_state After dependency rollup: -1 pending, 0 up, "
               "1 warn, 2 down, 3 unreachable.", "# TYPE watchpost_effective_state gauge"]
        fcl = ["# HELP watchpost_forecast_seconds Seconds until the trend crosses a threshold "
               "(0 = already crossed; absent = no projection).",
               "# TYPE watchpost_forecast_seconds gauge"]
        now = time.time()
        for m in scheduler.monitors:
            labels = f'monitor="{m.slug}",group="{_label(m.group)}",type="{m.type}"'
            e, _ = scheduler.rollup.effective(m.slug)
            eff.append(f"watchpost_effective_state{{{labels}}} {_EFF_NUM[e]}")
            f = scheduler.forecasts.get(m.slug)
            for level in ("warn", "crit"):
                at = getattr(f, f"{level}_at", None) if f else None
                if at is not None:
                    fcl.append(f'watchpost_forecast_seconds{{{labels},level="{level}"}} '
                               f"{max(0.0, at - now):.0f}")
        grp = ["# HELP watchpost_group_state Group rollup state, same scale as effective_state.",
               "# TYPE watchpost_group_state gauge"]
        for g, info in sorted(scheduler.rollup.group_states().items()):
            grp.append(f'watchpost_group_state{{group="{_label(g)}"}} {_EFF_NUM[info["state"]]}')
        return "\n".join(lines + values + eff + fcl + grp) + "\n"

    return app
