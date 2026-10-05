"""HTTP surface: dashboard, JSON API, Prometheus metrics, and host ingest.

The dashboard and monitor endpoints do not change state. Adding, removing, or
editing a monitor means editing the YAML and restarting the container, so a
stolen session can read your inventory but can not change what is watched or
silence an alert. The write paths are POST /api/ingest (observe/ingest/api.py,
host-bound ingest key, does not touch monitors) and the login surface below.

Two credentials exist and they do not mix. The optional basic auth, and a
login session, both open the read-only API and /metrics. The host views
(/host, /api/hosts) hold hardware inventory and need a login session; basic
auth does not open them. Only a session with a
CSRF token reaches /api/logout and /api/admin/*, and an admin session is needed
for the admin routes; basic auth is never accepted there (observe/auth.py).
"""

from __future__ import annotations

import base64
import hmac
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import audit
from . import auth as authmod
from . import enrol, scripts
from . import hostview
from .alerts import Alerter
from .config import Config
from .infra import InfraError, InfraService
from .infra_map import MapService
from .infra_match import LivePort, Matcher, PortMatch
from .infra_port import PortPages
from .ingest.api import DenialAggregator, RateLimiter, build_router
from .ingest.keys import (MARKER, IngestKeyError, create_key, key_host, list_keys, revoke_key,
                          verify_key)
from .ingest.schema import MAX_NAME
from .plugins import PAGE_PREFIX, ROUTE_PREFIX, LoadedPlugins
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


def _page_handler(file: Path) -> Callable[[], Awaitable[FileResponse]]:
    async def page() -> FileResponse:
        return FileResponse(file)
    return page


def scripts_machine_id() -> str:
    """This machine's id for the install script's Observe-host guard, or empty when unreadable."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            text = Path(path).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if re.fullmatch(r"[0-9a-f]{32}", text):
            return text
    return ""


def create_app(config: Config, store: Store, scheduler: Scheduler, alerter: Alerter,
               ingest_clock: Callable[[], float] = time.monotonic,
               auth_clock: Callable[[], float] = time.time,
               plugins: LoadedPlugins | None = None,
               map_clock: Callable[[], float] = time.time) -> FastAPI:
    app = FastAPI(title="Observe", version=__version__, docs_url=None, redoc_url=None,
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
            raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="Observe"'})

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

    plugins = plugins or LoadedPlugins()
    plugin_limiter = RateLimiter(config.server.plugin_rate_per_minute, ingest_clock)
    # Key-authenticated plugin routes count three ways, so bad-key traffic never uses up a
    # valid key's allowance: per valid key, per peer for valid keys, and per peer for failures.
    key_limiter = RateLimiter(config.server.plugin_rate_per_minute, ingest_clock)
    key_peer_limiter = RateLimiter(config.server.plugin_rate_per_minute, ingest_clock)
    key_fail_limiter = RateLimiter(config.server.plugin_rate_per_minute, ingest_clock)
    plugin_denials = DenialAggregator(ingest_clock)

    async def plugin_rate_limit(request: Request) -> None:
        peer = request.client.host if request.client else "unknown"
        if not plugin_limiter.allow(peer):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})

    async def plugin_session(request: Request) -> authmod.Session:
        # Reads need a session; any other method also needs the CSRF token.
        if request.method in ("GET", "HEAD"):
            return await guards.session(request)
        return await guards.mutating(request)

    async def plugin_admin(request: Request) -> authmod.Session:
        if request.method in ("GET", "HEAD"):
            return await guards.admin(request)
        return await guards.admin_mutating(request)

    # What a plugin handler may use: the store and the wall clock, and nothing of the core's
    # auth. Key-authenticated handlers read request.state.plugin_key, set by the dependency.
    app.state.plugin_store = store
    app.state.plugin_clock = auth_clock

    def plugin_key(scope: str) -> Callable[[Request], Awaitable[tuple[str, str]]]:
        async def dependency(request: Request) -> tuple[str, str]:
            header = request.headers.get("authorization", "")
            key = header[7:].strip() if header[:7].lower() == "bearer " else ""
            bound = await key_host(store, key, scope) if key else None
            peer = request.client.host if request.client else "unknown"
            # The body is not read before this passes. A second check records last use.
            if bound is None or not await verify_key(store, key, bound[1], scope=scope):
                if not key_fail_limiter.allow(peer):
                    raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
                raise HTTPException(401, "missing or invalid key",
                                    headers={"WWW-Authenticate": "Bearer"})
            if not (key_limiter.allow(bound[0]) and key_peer_limiter.allow(peer)):
                raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
            request.state.plugin_key = bound
            return bound
        return dependency

    # The core, not the plugin, chooses the dependencies: a session route is rate limited
    # first, then checked with CSRF; a key route is checked and limited by plugin_key. A plugin can only ask for the admin role, never for less. A
    # key-authenticated router is mounted under /api/plugins/<name>, or under the /api/v1
    # prefix it asked for; either way the audit middleware below covers it.
    public_routes: list[tuple[str, str]] = []  # (static path prefix, plugin) outside ROUTE_PREFIX
    for loaded in plugins.plugins:
        for pr in loaded.routers:
            if pr.key_scope is not None:
                prefix = pr.public_prefix or f"{ROUTE_PREFIX}/{loaded.name}"
                deps = [Depends(plugin_key(pr.key_scope))]
                if pr.public_prefix is not None:
                    public_routes += [((prefix + r.path).split("{")[0], loaded.name)
                                      for r in pr.router.routes if hasattr(r, "path")]
            else:
                prefix = f"{ROUTE_PREFIX}/{loaded.name}"
                deps = [Depends(plugin_rate_limit),
                        Depends(plugin_admin if pr.admin else plugin_session)]
            app.include_router(pr.router, prefix=prefix, dependencies=deps)

    def plugin_of(path: str) -> str | None:
        if path.startswith(ROUTE_PREFIX + "/"):
            return path[len(ROUTE_PREFIX) + 1:].split("/", 1)[0][:64]
        return next((name for static, name in public_routes if path.startswith(static)), None)

    @app.middleware("http")
    async def plugin_audit(request: Request, call_next: Any) -> Response:
        """Audit every plugin request that changes state, and every refused one."""
        path = request.url.path
        plugin = plugin_of(path)
        if plugin is None:
            return await call_next(request)
        remote = request.client.host if request.client else ""
        try:
            resp: Response = await call_next(request)
        except Exception as err:
            sess = getattr(request.state, "session", None)
            key = getattr(request.state, "plugin_key", None)
            await audit.record(store, "plugin_failed",
                               actor=sess.username if sess else (key[0] if key else ""),
                               method=request.method, path=path, status=500, remote=remote,
                               detail={"plugin": plugin, "error": type(err).__name__})
            raise
        sess = getattr(request.state, "session", None)
        key = getattr(request.state, "plugin_key", None)
        if resp.status_code in (401, 403, 429):
            # Bounded like login failures: one row per peer per window, with a count.
            covered = plugin_denials.note(remote or "unknown")
            if covered is not None:
                detail: dict[str, Any] = {"plugin": plugin}
                if covered:
                    detail["denials_covered"] = covered
                await audit.record(store, "plugin_denied",
                                   actor=sess.username if sess else (key[0] if key else ""),
                                   method=request.method, path=path, status=resp.status_code,
                                   remote=remote, detail=detail)
        elif (sess is not None or key is not None) and request.method not in ("GET", "HEAD"):
            # A handler may add facts about the outcome (never secrets) in audit_detail.
            extra = getattr(request.state, "audit_detail", None)
            detail = {"plugin": plugin, **(extra if isinstance(extra, dict) else {})}
            if key is not None:
                detail["device"] = key[1]
            await audit.record(store, "plugin_request", actor=sess.username if sess else key[0],
                               method=request.method, path=path, status=resp.status_code,
                               remote=remote, detail=detail)
        return resp

    # Pages and static files exist only for listed plugins, so a disabled plugin has no
    # surface at all. load_plugins keeps every page path clear of the plugin's own /static.
    for loaded in plugins.plugins:
        for page in loaded.pages:
            app.add_api_route(
                page.path, _page_handler(page.file), methods=["GET"], include_in_schema=False,
                response_class=FileResponse,
                dependencies=[Depends(guards.admin if page.admin_only else auth)])
        if loaded.static_dir is not None:
            app.mount(f"{PAGE_PREFIX}/{loaded.name}/static",
                      StaticFiles(directory=loaded.static_dir), name=f"plugin_{loaded.name}")
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
        except (ValueError, RecursionError):
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

    @app.get("/api/plugins", include_in_schema=False)
    async def plugin_list(sess: authmod.Session = Depends(guards.session)) -> dict[str, Any]:
        """Loaded plugins and the navigation entries this user may see."""
        return {"plugins": [{"name": p.name, "version": p.plugin.version}
                            for p in plugins.plugins],
                "nav": plugins.nav(sess.is_admin)}

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

    async def user_flag(request: Request, uid: int, sess: authmod.Session, column: str,
                        kind_on: str, kind_off: str) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        value = body.get("value") if isinstance(body, dict) else None
        path = request.url.path
        remote = request.client.host if request.client else ""
        if not isinstance(value, bool):
            return JSONResponse({"detail": "value must be true or false"}, status_code=422)
        detail: dict[str, Any] = {"user_id": uid, "value": value}
        try:
            outcome = await authmod.set_user_flag(store, uid, column, value)
        except Exception as err:
            await audit.record(store, "user_change_error", actor=sess.username,
                               method="POST", path=path, status=500, remote=remote,
                               detail={**detail, "error": type(err).__name__})
            raise
        if outcome != "ok":
            status = 404 if outcome == "missing" else 409
            msg = ("unknown user" if outcome == "missing"
                   else "the last active admin cannot be removed")
            await audit.record(store, "user_change_failed", actor=sess.username,
                               method="POST", path=path, status=status, remote=remote,
                               detail={**detail, "reason": outcome})
            return JSONResponse({"detail": msg}, status_code=status)
        await audit.record(store, kind_on if value else kind_off, actor=sess.username,
                           method="POST", path=path, status=200, remote=remote, detail=detail)
        return JSONResponse({"ok": True})

    @app.post("/api/admin/users/{uid}/disabled", include_in_schema=False)
    async def admin_user_disabled(
            uid: int, request: Request,
            sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        return await user_flag(request, uid, sess, "disabled",
                               "user_disabled", "user_enabled")

    @app.post("/api/admin/users/{uid}/admin", include_in_schema=False)
    async def admin_user_role(
            uid: int, request: Request,
            sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        return await user_flag(request, uid, sess, "is_admin",
                               "user_promoted", "user_demoted")

    @app.get("/api/admin/keys", include_in_schema=False)
    async def admin_keys(_: authmod.Session = Depends(guards.admin)) -> list[dict[str, Any]]:
        """Key ids, hosts and state. The secret part is never stored, so never listed."""
        return [{"id": k.prefix, "host": k.host, "created": k.created,
                 "created_by": k.created_by, "revoked_at": k.revoked_at,
                 "last_used": k.last_used, "active": k.active, "scope": k.scope}
                for k in await list_keys(store)]

    @app.post("/api/admin/keys", include_in_schema=False)
    async def admin_create_key(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        host = body.get("host") if isinstance(body, dict) else None
        if not isinstance(host, str):
            return JSONResponse({"detail": "host is required"}, status_code=422)
        scope = body.get("scope", MARKER) if isinstance(body, dict) else MARKER
        remote = request.client.host if request.client else ""
        shown = {"host": host[:MAX_NAME], "scope": str(scope)[:8]}
        if scope not in (MARKER, *plugins.scopes):
            # Only wpi and the scopes of listed plugins can be issued; a disabled plugin's cannot.
            await audit.record(store, "key_create_failed", actor=sess.username, method="POST",
                               path="/api/admin/keys", status=422, remote=remote,
                               detail={**shown, "reason": "unknown scope"})
            return JSONResponse({"detail": "unknown key scope"}, status_code=422)
        try:
            plaintext, info = await create_key(store, host, created_by=sess.username,
                                               scope=scope)
        except IngestKeyError as err:
            await audit.record(store, "key_create_failed", actor=sess.username, method="POST",
                               path="/api/admin/keys", status=422, remote=remote,
                               detail={**shown, "reason": str(err)})
            return JSONResponse({"detail": str(err)}, status_code=422)
        except Exception as err:
            await audit.record(store, "key_create_failed", actor=sess.username, method="POST",
                               path="/api/admin/keys", status=500, remote=remote,
                               detail={**shown, "error": type(err).__name__})
            raise
        await audit.record(store, "key_created", actor=sess.username, method="POST",
                           path="/api/admin/keys", status=200, remote=remote,
                           detail={"host": info.host, "key_id": info.prefix,
                                   "scope": info.scope})
        # The plaintext appears in this one response, which is sent with Cache-Control: no-store.
        return JSONResponse({"id": info.prefix, "host": info.host, "scope": info.scope,
                             "key": plaintext})

    @app.post("/api/admin/keys/{key_id}/revoke", include_in_schema=False)
    async def admin_revoke_key(
            key_id: str, request: Request,
            sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        remote = request.client.host if request.client else ""
        path = f"/api/admin/keys/{key_id[:64]}/revoke"
        detail = {"key_id": key_id[:64]}
        try:
            done = await revoke_key(store, key_id)
        except Exception as err:
            await audit.record(store, "key_revoke_failed", actor=sess.username, method="POST",
                               path=path, status=500, remote=remote,
                               detail={**detail, "error": type(err).__name__})
            raise
        if not done:
            await audit.record(store, "key_revoke_failed", actor=sess.username, method="POST",
                               path=path, status=404, remote=remote,
                               detail={**detail, "reason": "unknown or already revoked"})
            return JSONResponse({"detail": "unknown or already revoked key"}, status_code=404)
        await audit.record(store, "key_revoked", actor=sess.username, method="POST", path=path,
                           status=200, remote=remote, detail=detail)
        return JSONResponse({"ok": True})

    infra = InfraService(store)
    matcher = Matcher(config, infra)

    def live_port(match: PortMatch) -> LivePort | None:
        """The last polled state of a matched port. Only what the check reported is used."""
        st = scheduler.states.get(match.monitor)
        detail = st.last.detail if st is not None and st.last is not None else None
        if not isinstance(detail, dict):
            return None
        if match.kind == "unifi":
            ports = detail.get("ports")
            detail = ports.get(match.detail) if isinstance(ports, dict) else None
            if not isinstance(detail, dict):
                return None
        return LivePort(detail.get("speed_mbps"), detail.get("vlan"), detail.get("poe_w"))

    def state_of(slug: str) -> tuple[str, str | None] | None:
        return scheduler.rollup.effective(slug) if slug in scheduler.states else None

    mapper = MapService(config, infra, matcher, state_of, map_clock)
    scheduler.hooks.append(mapper.refresh)

    last_prune = [0.0]

    async def prune_plugins() -> None:
        """Each listed plugin applies its own retention, about once an hour."""
        now = map_clock()
        if now - last_prune[0] < 3600:
            return
        last_prune[0] = now
        for loaded in plugins.plugins:
            await loaded.plugin.prune(store, now)

    if plugins.plugins:
        scheduler.hooks.append(prune_plugins)

    @app.get("/api/infra/map", include_in_schema=False)
    async def infra_map(site: str | None = None, building: str | None = None,
                        _: authmod.Session = Depends(guards.session)) -> dict[str, Any]:
        """Nodes and edges with live state, for a site and building when given."""
        await mapper.refresh()
        return await mapper.map_data(site, building, live_port)

    @app.get("/api/infra/dependencies", include_in_schema=False)
    async def infra_dependencies(_: authmod.Session = Depends(guards.session)) -> dict[str, Any]:
        """Applied, pending, refused and rejected dependency edges, computed now."""
        return (await mapper.refresh()).as_dict()

    async def decide_dependency(request: Request, sess: authmod.Session,
                                decision: str) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        child = body.get("child") if isinstance(body, dict) else None
        parent = body.get("parent") if isinstance(body, dict) else None
        if not isinstance(child, str) or not isinstance(parent, str):
            return JSONResponse({"detail": "child and parent are required"}, status_code=422)
        remote = request.client.host if request.client else ""
        try:
            await mapper.decide(child, parent, decision, sess.username, remote)
        except InfraError as err:
            return JSONResponse({"detail": str(err)}, status_code=422)
        return JSONResponse({"ok": True})

    @app.post("/api/admin/infra/depends/accept", include_in_schema=False)
    async def admin_depends_accept(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        return await decide_dependency(request, sess, "accepted")

    @app.post("/api/admin/infra/depends/reject", include_in_schema=False)
    async def admin_depends_reject(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        return await decide_dependency(request, sess, "rejected")

    @app.get("/api/admin/infra/unlinked", include_in_schema=False)
    async def admin_infra_unlinked(
            _: authmod.Session = Depends(guards.admin)) -> list[dict[str, Any]]:
        """Switches seen in the field that match no monitor, waiting for an admin to link."""
        return await matcher.unlinked()

    @app.post("/api/admin/infra/link", include_in_schema=False)
    async def admin_infra_link(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        sid = body.get("switch_id") if isinstance(body, dict) else None
        slug = body.get("monitor") if isinstance(body, dict) else None
        if not isinstance(sid, str) or not isinstance(slug, str):
            return JSONResponse({"detail": "switch_id and monitor are required"},
                                status_code=422)
        remote = request.client.host if request.client else ""
        try:
            await matcher.link_switch(sid, slug, sess.username, remote)
        except InfraError as err:
            return JSONResponse({"detail": str(err)}, status_code=422)
        return JSONResponse({"ok": True})

    @app.get("/api/infra/findings", include_in_schema=False)
    async def infra_findings(_: authmod.Session = Depends(guards.session)) -> dict[str, Any]:
        """Field conflicts, computed now. Dashboard only; nothing here raises an alert."""
        return {"findings": [f.as_dict() for f in await matcher.findings(live_port)]}

    ports = PortPages(infra, matcher, mapper, map_clock)

    @app.get("/api/infra/port", include_in_schema=False)
    async def infra_port(switch_id: str, port: str,
                         _: authmod.Session = Depends(guards.session)) -> dict[str, Any]:
        """One port: live state, properties and history, findings and matched monitors."""
        view = await ports.port_view(switch_id, port, live_port)
        if view is None:
            raise HTTPException(404, "unknown port")
        return view

    @app.post("/api/admin/infra/findings/ack", include_in_schema=False)
    async def admin_finding_ack(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        fields = [body.get(k) if isinstance(body, dict) else None
                  for k in ("switch_id", "port_key", "kind")]
        if not all(isinstance(f, str) for f in fields):
            return JSONResponse({"detail": "switch_id, port_key and kind are required"},
                                status_code=422)
        remote = request.client.host if request.client else ""
        try:
            await ports.acknowledge(fields[0], fields[1], fields[2], live_port, sess.username,
                                    remote)
        except InfraError as err:
            return JSONResponse({"detail": str(err)}, status_code=422)
        return JSONResponse({"ok": True})

    @app.get("/map", include_in_schema=False)
    async def map_page() -> FileResponse:
        # Like /host, the page holds no data; map.js sends a visitor without a session to /login.
        return FileResponse(STATIC / "map.html")

    @app.get("/port", include_in_schema=False)
    async def port_page() -> FileResponse:
        return FileResponse(STATIC / "port.html")

    @app.get("/admin/infra", include_in_schema=False)
    async def admin_infra_page() -> FileResponse:
        # The page holds no data; infra-admin.js needs an admin session for everything it shows.
        return FileResponse(STATIC / "infra-admin.html")

    @app.get("/admin", include_in_schema=False)
    async def admin_page() -> FileResponse:
        # Like /login, the page holds no data; admin.js sends a visitor without an admin
        # session to /login.
        return FileResponse(STATIC / "admin.html")

    @app.get("/audit", include_in_schema=False)
    async def audit_page() -> FileResponse:
        # The page holds no data; audit.js needs an admin session for everything it shows.
        return FileResponse(STATIC / "audit.html")

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

    pushed = {m.host: m for m in scheduler.monitors if m.type == "pushed_host"}

    async def host_view(host: str, row: dict[str, Any] | None) -> dict[str, Any] | None:
        mon = pushed.get(host)
        if row is None:
            if mon is None:
                return None
            # Listed in the YAML but no batch has ever arrived.
            row = {"host": host, "platform": "", "agent_version": "", "last_seen": 0.0,
                   "confirmed": 1}
            data = None
        else:
            data = await store.latest_host(host)
        now = auth_clock()
        stale_after = (mon.stale_after or 3 * config.effective(mon, "interval")) if mon             else 3 * config.defaults.interval
        state = None
        if mon is not None:
            st = scheduler.states[mon.slug]
            effective, blocker = scheduler.rollup.effective(mon.slug)
            state = {"slug": mon.slug, "name": mon.name, "state": st.state.value,
                     "effective_state": effective, "blocked_by": blocker}
        overrides = {(c.source, c.metric): c for c in mon.components} if mon else {}
        return hostview.build_host_view(
            row, data, await store.host_sources(host),
            await store.host_events(host, limit=50), now, stale_after, mon, overrides, state)

    @app.get("/api/hosts", include_in_schema=False)
    async def hosts(_: authmod.Session = Depends(guards.session)) -> dict[str, Any]:
        """Every pushed host, with the status of each hardware section. Session only."""
        rows = {r["host"]: r for r in await store.host_rows()}
        out = []
        for name in sorted({*rows, *pushed}):
            view = await host_view(name, rows.get(name))
            if view is not None:
                out.append(hostview.summarize(view))
        return {"hosts": out}

    @app.post("/api/hosts", include_in_schema=False)
    async def create_host(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """Add a host: validate, store a single-use enrolment token (30 minutes) and return the
        install command once. Admin session and CSRF. The token is in this response only, which
        is sent with Cache-Control: no-store; it is never logged or audited."""
        remote = request.client.host if request.client else ""
        try:
            body = await request.json()
        except ValueError:
            body = None
        shown = {"host": str(body.get("name"))[:MAX_NAME] if isinstance(body, dict) else ""}

        async def refused(status: int, reason: str) -> JSONResponse:
            await audit.record(store, "enrol_create_failed", actor=sess.username, method="POST",
                               path="/api/hosts", status=status, remote=remote,
                               detail={**shown, "reason": reason})
            return JSONResponse({"detail": reason}, status_code=status)

        try:
            spec = enrol.parse_spec(body)
            now = auth_clock()
            if spec.name in pushed:
                raise enrol.EnrolError("a host with this name already exists", 409)
            token = await enrol.create_enrolment(store, spec, sess.username, now)
        except enrol.EnrolError as err:
            return await refused(err.status, err.reason)
        await audit.record(store, "enrol_created", actor=sess.username, method="POST",
                           path="/api/hosts", status=200, remote=remote,
                           detail={"host": spec.name, "platform": spec.platform,
                                   "agent": spec.agent, "control": spec.control,
                                   "reboot": spec.reboot, "fans": len(spec.fans),
                                   "services": len(spec.services)})
        return JSONResponse({
            "host": spec.name, "platform": spec.platform,
            "platform_label": enrol.PLATFORMS[spec.platform],
            "expires_at": now + enrol.TOKEN_TTL_S, "ttl_s": enrol.TOKEN_TTL_S,
            "command": enrol.command_text(spec.name, spec.platform, str(request.base_url),
                                          token)})

    @app.get("/api/hosts/{host}/enrolment", include_in_schema=False)
    async def host_enrolment(host: str, request: Request,
                             sess: authmod.Session = Depends(guards.admin)) -> Response:
        """Progress of one enrolment: script fetched, first data, control first pull, ready, or
        expired. Admin session. It never returns the token or a key."""
        now = auth_clock()
        state = await enrol.progress(store, host, now)
        if state is None:
            raise HTTPException(404, "no enrolment for this host")
        if state["expired"] and await enrol.claim_expiry_audit(store, host, now):
            await audit.record(store, "enrol_expired", actor=sess.username, method="GET",
                               path=f"/api/hosts/{host[:64]}/enrolment", status=200,
                               remote=request.client.host if request.client else "",
                               detail={"host": host[:MAX_NAME]})
        return JSONResponse(state)

    enrol_limiter = RateLimiter(config.server.plugin_rate_per_minute, ingest_clock)
    app.state.observe_machine_id = scripts_machine_id()

    def control_public_key() -> str:
        loaded = plugins.get("control")
        return str(getattr(loaded.plugin, "public_key", "") or "") if loaded else ""

    @app.get("/i/{token}", include_in_schema=False)
    async def install_script(token: str, request: Request) -> Response:
        """The install script for one enrolment. No session: the single-use token in the path is
        the credential. It is spent by this fetch, so a second fetch is 410. A platform with no
        script yet, or control without a loaded control plugin, is refused before the token is
        spent. The response is `no-store` and the token is never logged."""
        remote = request.client.host if request.client else ""
        if not enrol_limiter.allow(remote or "unknown"):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
        now = auth_clock()
        known = await enrol.peek(store, token, now)
        if known is not None:
            platform, wants_control = known
            if platform not in scripts.SCRIPT_PLATFORMS:
                raise HTTPException(501, "no install script for this platform yet")
            if wants_control and not control_public_key():
                raise HTTPException(409, "control is not set up on this Observe server")
        red = await enrol.redeem(store, token, now, remote)
        if red is None:
            return PlainTextResponse("This install command was already used or has expired. "
                                     "Make a new one in the Observe console.\n", status_code=410)
        listen = scripts.usable_address(config.server.listen)
        addrs = [a for a in (listen, scripts.usable_address(request.url.hostname or ""))
                 if a is not None]
        ctx = scripts.Context(str(request.base_url).rstrip("/"),
                              app.state.observe_machine_id, tuple(dict.fromkeys(addrs)),
                              control_public_key())
        try:
            body = scripts.render_linux(red, ctx)
        except scripts.ScriptError as err:
            await audit.record(store, "enrol_script_failed", method="GET", path="/i/[token]",
                               status=500, remote=remote,
                               detail={"host": red.host, "reason": str(err)})
            return PlainTextResponse("echo 'Observe could not build this script' >&2; exit 1\n",
                                     status_code=500)
        return PlainTextResponse(body, media_type="text/x-shellscript")

    @app.post("/api/enrol/step", include_in_schema=False)
    async def install_step(request: Request) -> Response:
        """One progress report from the install script. Authenticated by the step key of the
        redeemed token (Bearer wps_...), which works for two hours after the fetch. The body is
        {"step", "status", "note"}; the note is redacted and capped."""
        remote = request.client.host if request.client else ""
        if not enrol_limiter.allow(remote or "unknown"):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
        header = request.headers.get("authorization", "")
        key = header[7:].strip() if header[:7].lower() == "bearer " else ""
        try:
            body = await request.json()
        except ValueError:
            body = None
        ok = (isinstance(body, dict) and set(body) <= {"step", "status", "note"}
              and body.get("step") in enrol.INSTALL_STEPS
              and body.get("status") in enrol.STEP_STATUSES
              and isinstance(body.get("note", ""), str))
        host = await enrol.record_step(
            store, key, str(body["step"]) if ok else "", str(body["status"]) if ok else "",
            body.get("note", "") if ok else "", auth_clock()) if key and ok else None
        if host is None:
            await audit.record(store, "enrol_step_refused", method="POST",
                               path="/api/enrol/step", status=401, remote=remote)
            raise HTTPException(401, "missing or invalid step key",
                                headers={"WWW-Authenticate": "Bearer"})
        if body["status"] in ("failed", "refused"):
            await audit.record(store, "enrol_install_problem", actor=host, method="POST",
                               path="/api/enrol/step", status=200, remote=remote,
                               detail={"host": host, "step": body["step"],
                                       "status": body["status"]})
        return Response(status_code=204)

    @app.get("/api/hosts/{host:path}", include_in_schema=False)
    async def host_detail(host: str,
                          _: authmod.Session = Depends(guards.session)) -> dict[str, Any]:
        rows = {r["host"]: r for r in await store.host_rows()}
        view = await host_view(host, rows.get(host))
        if view is None:
            raise HTTPException(404, "unknown host")
        return view

    @app.get("/host", include_in_schema=False)
    async def host_page() -> FileResponse:
        # Like /login, the page holds no data; host.js sends a visitor without a session to /login.
        return FileResponse(STATIC / "host.html")

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
            "# HELP observe_state Own monitor state: -1 pending, 0 up, 1 warn, 2 down.",
            "# TYPE observe_state gauge",
        ]
        values = ["# HELP observe_value Last numeric value of the check.",
                  "# TYPE observe_value gauge"]
        for m in scheduler.monitors:
            st = scheduler.states[m.slug]
            labels = f'monitor="{m.slug}",group="{_label(m.group)}",type="{m.type}"'
            lines.append(f"observe_state{{{labels}}} {_STATE_NUM[st.state.value]}")
            if st.last and st.last.value is not None:
                values.append(f"observe_value{{{labels}}} {st.last.value}")
        eff = ["# HELP observe_effective_state After dependency rollup: -1 pending, 0 up, "
               "1 warn, 2 down, 3 unreachable.", "# TYPE observe_effective_state gauge"]
        fcl = ["# HELP observe_forecast_seconds Seconds until the trend crosses a threshold "
               "(0 = already crossed; absent = no projection).",
               "# TYPE observe_forecast_seconds gauge"]
        now = time.time()
        for m in scheduler.monitors:
            labels = f'monitor="{m.slug}",group="{_label(m.group)}",type="{m.type}"'
            e, _ = scheduler.rollup.effective(m.slug)
            eff.append(f"observe_effective_state{{{labels}}} {_EFF_NUM[e]}")
            f = scheduler.forecasts.get(m.slug)
            for level in ("warn", "crit"):
                at = getattr(f, f"{level}_at", None) if f else None
                if at is not None:
                    fcl.append(f'observe_forecast_seconds{{{labels},level="{level}"}} '
                               f"{max(0.0, at - now):.0f}")
        grp = ["# HELP observe_group_state Group rollup state, same scale as effective_state.",
               "# TYPE observe_group_state gauge"]
        for g, info in sorted(scheduler.rollup.group_states().items()):
            grp.append(f'observe_group_state{{group="{_label(g)}"}} {_EFF_NUM[info["state"]]}')
        age = ["# HELP observe_host_age_seconds Seconds since the last batch from a pushed host.",
               "# TYPE observe_host_age_seconds gauge"]
        comp = ["# HELP observe_host_component_state Pushed host component: 0 good, "
                "1 warning, 2 critical.", "# TYPE observe_host_component_state gauge"]
        for m in scheduler.monitors:
            last = scheduler.states[m.slug].last
            if m.type != "pushed_host" or last is None:
                continue
            labels = f'monitor="{m.slug}",group="{_label(m.group)}",host="{_label(m.host)}"'
            if "age_seconds" in last.detail:
                age.append(f"observe_host_age_seconds{{{labels}}} {last.detail['age_seconds']:.0f}")
            for name, level in sorted(last.detail.get("components", {}).items()):
                comp.append(f'observe_host_component_state{{{labels},component="{_label(name)}"}} '
                            f"{_COMPONENT_NUM[level]}")
        return "\n".join(lines + values + eff + fcl + grp + age + comp) + "\n"

    return app
