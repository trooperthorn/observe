"""HTTP surface: dashboard, JSON API, Prometheus metrics, and host ingest.

The dashboard and monitor endpoints do not change state. Adding, removing, or
editing a monitor means editing the YAML and restarting the container, so a
stolen session can read your inventory but can not change what is watched or
silence an alert. The write paths are POST /api/ingest (watchpost/ingest/api.py,
host-bound ingest key, does not touch monitors) and the login surface below.

Two credentials exist and they do not mix. The optional basic auth, and a
login session, both open the read-only API and /metrics. Only a session with a
CSRF token reaches /api/logout and /api/admin/*, and an admin session is needed
for the admin routes; basic auth is never accepted there (watchpost/auth.py).
"""

from __future__ import annotations

import base64
import hmac
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import audit
from . import auth as authmod
from .alerts import Alerter
from .config import Config
from .ingest.api import DenialAggregator, RateLimiter, build_router
from .scheduler import Scheduler
from .store import Store

STATIC = Path(__file__).parent / "static"
_STATE_NUM = {"pending": -1, "up": 0, "warn": 1, "down": 2}
_COMPONENT_NUM = {"good": 0, "warning": 1, "critical": 2}
_EFF_NUM ={**_STATE_NUM, "unreachable": 3}


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
               ingest_clock: Callable[[], float] = time.monotonic,
               auth_clock: Callable[[], float] = time.time) -> FastAPI:
    app = FastAPI(title="watchpost", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)
    user, pw = config.server.basic_auth_user, config.server.basic_auth_password

    guards = authmod.build_guards(config, store, auth_clock)

    async def auth(request: Request) -> None:
        """Read-only surfaces: a login session, or basic auth when it is configured."""
        if not user:
            return
        if await authmod.load_session(store, config, request.cookies.get(authmod.COOKIE),
                                      auth_clock()) is not None:
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

    login_limiter = RateLimiter(config.server.login_rate_per_minute, ingest_clock)
    login_denials = DenialAggregator(ingest_clock)
    secure = config.server.session_cookie_secure

    def set_cookie(resp: Response, token: str) -> None:
        resp.set_cookie(authmod.COOKIE, token, max_age=config.server.session_absolute_s,
                        path="/", httponly=True, secure=secure, samesite="strict")

    async def login_failed(peer: str, status: int, reason: str,
                           actor: str = "") -> JSONResponse:
        # Audit growth is bounded: one row per peer per window, with a count.
        covered = login_denials.note(peer)
        if covered is not None:
            detail: dict[str, Any] = {"reason": reason}
            if covered:
                detail["denials_covered"] = covered
            await audit.record(store, "login_failed", actor=actor, method="POST",
                               path="/api/login", status=status, remote=peer, detail=detail)
        headers = {"Retry-After": "60"} if status == 429 else None
        # One generic message for every credential failure, so it reveals nothing.
        msg = "rate limit exceeded" if status == 429 else "invalid username or password"
        return JSONResponse({"detail": msg}, status_code=status, headers=headers)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    by_slug = {m.slug: m for m in scheduler.monitors}

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", dependencies=guarded, include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        return FileResponse(STATIC / "login.html")

    @app.post("/api/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        peer = request.client.host if request.client else "unknown"
        if not login_limiter.allow(peer):
            return await login_failed(peer, 429, "rate limit exceeded")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > 4096:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        try:
            body = await request.json()
        except ValueError:
            body = None
        name = body.get("username") if isinstance(body, dict) else None
        pw = body.get("password") if isinstance(body, dict) else None
        if not isinstance(name, str) or not isinstance(pw, str):
            return JSONResponse({"detail": "username and password are required"},
                                status_code=422)
        res = await authmod.check_login(store, config, name, pw, auth_clock())
        if not res.ok:
            # The attempted name is audited only when it is a real account.
            return await login_failed(peer, 401, res.reason, res.username if res.user_id else "")
        assert res.user_id is not None
        try:
            token, csrf = await authmod.create_session(store, config, res.user_id, auth_clock())
        except Exception as err:
            # The password was right but no session exists: record the partial login.
            await audit.record(store, "login_error", actor=res.username, method="POST",
                               path="/api/login", status=500, remote=peer,
                               detail={"error": type(err).__name__})
            raise
        await audit.record(store, "login_ok", actor=res.username, method="POST",
                           path="/api/login", status=200, remote=peer)
        out = JSONResponse({"username": res.username, "is_admin": res.is_admin, "csrf": csrf})
        set_cookie(out, token)
        return out

    @app.post("/api/logout", include_in_schema=False)
    async def logout(request: Request,
                     sess: authmod.Session = Depends(guards.mutating)) -> Response:
        token = request.cookies.get(authmod.COOKIE)
        if token:
            await authmod.revoke_session(store, token)
        await audit.record(store, "logout", actor=sess.username, method="POST",
                           path="/api/logout", status=200,
                           remote=request.client.host if request.client else "")
        out = JSONResponse({"ok": True})
        out.delete_cookie(authmod.COOKIE, path="/", httponly=True, secure=secure,
                          samesite="strict")
        return out

    @app.get("/api/session", include_in_schema=False)
    async def whoami(sess: authmod.Session = Depends(guards.session)) -> dict[str, Any]:
        return {"username": sess.username, "is_admin": sess.is_admin, "csrf": sess.csrf}

    @app.get("/api/admin/users", include_in_schema=False)
    async def admin_users(_: authmod.Session = Depends(guards.admin)) -> list[dict[str, Any]]:
        return await authmod.list_users(store)

    @app.post("/api/admin/users", include_in_schema=False)
    async def admin_create_user(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("username"), str) \
                or not isinstance(body.get("password"), str):
            return JSONResponse({"detail": "username and password are required"},
                                status_code=422)
        make_admin = body.get("is_admin") is True
        remote = request.client.host if request.client else ""
        shown = {"username": body["username"].strip()[:authmod.MAX_USERNAME],
                 "is_admin": make_admin}
        try:
            uid = await authmod.create_user(store, config, body["username"], body["password"],
                                            is_admin=make_admin, now=auth_clock())
        except authmod.AuthError as err:
            # AuthError texts describe the rule that failed and never echo the password.
            await audit.record(store, "user_create_failed", actor=sess.username, method="POST",
                               path="/api/admin/users", status=422, remote=remote,
                               detail={**shown, "reason": str(err)})
            return JSONResponse({"detail": str(err)}, status_code=422)
        except Exception as err:
            await audit.record(store, "user_create_error", actor=sess.username, method="POST",
                               path="/api/admin/users", status=500, remote=remote,
                               detail={**shown, "error": type(err).__name__})
            raise
        await audit.record(store, "user_created", actor=sess.username, method="POST",
                           path="/api/admin/users", status=200, remote=remote, detail=shown)
        return JSONResponse({"id": uid})

    @app.get("/api/audit", include_in_schema=False)
    async def audit_log(limit: int = 100, kind: str | None = None, before: int | None = None,
                        _: authmod.Session = Depends(guards.admin)) -> list[dict[str, Any]]:
        """Admin only, session only: basic auth never reaches this route."""
        return await audit.list_rows(store, limit, kind, before)

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
        age = ["# HELP watchpost_host_age_seconds Seconds since the last batch from a pushed host.",
               "# TYPE watchpost_host_age_seconds gauge"]
        comp = ["# HELP watchpost_host_component_state Pushed host component: 0 good, "
                "1 warning, 2 critical.", "# TYPE watchpost_host_component_state gauge"]
        for m in scheduler.monitors:
            last = scheduler.states[m.slug].last
            if m.type != "pushed_host" or last is None:
                continue
            labels = f'monitor="{m.slug}",group="{_label(m.group)}",host="{_label(m.host)}"'
            if "age_seconds" in last.detail:
                age.append(f"watchpost_host_age_seconds{{{labels}}} {last.detail['age_seconds']:.0f}")
            for name, level in sorted(last.detail.get("components", {}).items()):
                comp.append(f'watchpost_host_component_state{{{labels},component="{_label(name)}"}} '
                            f"{_COMPONENT_NUM[level]}")
        return "\n".join(lines + values + eff + fcl + grp + age + comp) + "\n"

    return app
