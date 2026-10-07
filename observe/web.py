"""HTTP surface: dashboard, JSON API, Prometheus metrics, and host ingest.

The dashboard and the /api/v2 read API do not change state. Adding, removing, or
editing a monitor means editing the YAML and restarting the container, so a
stolen session can read your inventory but can not change what is watched or
silence an alert. The write paths are POST /v1/metrics and /v1/logs (observe/otlp/api.py,
host-bound ingest key, does not touch monitors) and the login surface below.

Two credentials exist and they do not mix. The optional basic auth, and a
login session, both open /metrics and the page shells. The /api/v2 read API
(observe/api) takes a login session or a read token and never basic auth, and its
host views hold hardware inventory, so they need a login or a token even when
server.anonymous_read is on. Only a session with a
CSRF token reaches /api/logout and /api/admin/*, and an admin session is needed
for the admin routes; basic auth is never accepted there (observe/auth.py).
"""

from __future__ import annotations

import base64
import hmac
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import api as apimod
from . import audit
from . import auth as authmod
from . import (enrol, hosttasks, layout, recheck_settings, retention, rules, scripts, taskscripts,
               tiers)
from .alerts import Alerter
from .config import Config
from .infra import InfraError, InfraService
from .infra_map import MapService
from .infra_match import LivePort, Matcher, PortMatch
from .infra_port import PortPages
from .ingest.api import DenialAggregator, Guard, RateLimiter, build_router
from .otlp.api import build_router as build_otlp_router
from .ingest.keys import (MARKER, READ_MARKER, IngestKeyError, create_key, key_host, list_keys,
                          revoke_key, verify_key)
from .ingest.schema import MAX_NAME
from .plugins import PAGE_PREFIX, ROUTE_PREFIX, LoadedPlugins, PluginError
from .scheduler import Scheduler
from .store import Store

STATIC = Path(__file__).parent / "static"
_STATE_NUM = {"pending": -1, "up": 0, "warn": 1, "down": 2}
_COMPONENT_NUM = {"good": 0, "warning": 1, "critical": 2}
_EFF_NUM ={**_STATE_NUM, "unreachable": 3}


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


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
    ingest_guard = Guard(config, store, ingest_clock)
    app.include_router(build_router(config, store, ingest_guard))

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
    app.include_router(build_otlp_router(store, ingest_guard, plugins, auth_clock))

    # The v2 read API (observe/api): its own sub-application, mounted at /api/v2. It takes a
    # session or a read token, never basic auth, and answers with problem details. A plugin
    # adds resources through its optional register_api(api) hook, only under /<plugin name>.
    runtime = apimod.ApiRuntime(config, store, scheduler=scheduler, alerter=alerter,
                                plugins=plugins, auth_clock=auth_clock, clock=ingest_clock)
    v2, v2_registry = apimod.build(runtime)
    app.state.v2_runtime = runtime
    for loaded in plugins.plugins:
        register_api = getattr(loaded.plugin, "register_api", None)
        if callable(register_api):
            try:
                register_api(v2_registry.for_plugin(loaded.name))
            except (ValueError, TypeError) as err:
                raise PluginError(f"plugin {loaded.name!r}: register_api failed: {err}") from err
    app.mount(apimod.PREFIX, v2, name="api_v2")
    plugin_limiter = RateLimiter(config.server.plugin_rate_per_minute, ingest_clock)
    # Key-authenticated plugin routes count three ways, so bad-key traffic never uses up a
    # valid key's allowance: per valid key, per peer for valid keys, and per peer for failures.
    key_limiter = RateLimiter(config.server.plugin_rate_per_minute, ingest_clock)
    key_peer_limiter = RateLimiter(config.server.plugin_rate_per_minute, ingest_clock)
    key_fail_limiter = RateLimiter(config.server.plugin_rate_per_minute, ingest_clock)
    plugin_denials = DenialAggregator(ingest_clock)
    step_denials = DenialAggregator(ingest_clock)

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
            runtime.auth.forget_session(token)
            await authmod.revoke_session(store, token)
        await audit.record(store, "logout", actor=sess.username, method="POST",
                           path="/api/logout", status=200,
                           remote=request.client.host if request.client else "")
        out = JSONResponse({"ok": True})
        out.delete_cookie(authmod.COOKIE, path="/", httponly=True, secure=secure,
                          samesite="strict")
        return out

    @app.get("/api/ui/layout/{view}", include_in_schema=False)
    async def ui_layout_get(view: str,
                            sess: authmod.Session = Depends(guards.session)) -> Response:
        """This user's saved tile order and hidden tiles for one view. Never another user's."""
        try:
            layout.check_view(view)
        except layout.LayoutError as err:
            return JSONResponse({"detail": err.reason}, status_code=err.status)
        return JSONResponse(await layout.load(store, sess.user_id, view),
                            headers={"Cache-Control": "no-store"})

    @app.put("/api/ui/layout/{view}", include_in_schema=False)
    async def ui_layout_put(view: str, request: Request,
                            sess: authmod.Session = Depends(guards.mutating)) -> Response:
        """Save this user's layout. Session and CSRF; ids of the wrong shape are dropped."""
        try:
            layout.check_view(view)
            raw = await request.body()
            if len(raw) > layout.MAX_BODY:
                raise layout.LayoutError("layout is too large", 413)
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            clean = layout.parse(body)
        except layout.LayoutError as err:
            return JSONResponse({"detail": err.reason}, status_code=err.status)
        await layout.save(store, sess.user_id, view, clean, auth_clock())
        return JSONResponse({"view": view, **clean, "saved": True})

    @app.delete("/api/ui/layout/{view}", include_in_schema=False)
    async def ui_layout_reset(view: str,
                              sess: authmod.Session = Depends(guards.mutating)) -> Response:
        """Back to the declared order for this user."""
        try:
            layout.check_view(view)
        except layout.LayoutError as err:
            return JSONResponse({"detail": err.reason}, status_code=err.status)
        await layout.reset(store, sess.user_id, view)
        return JSONResponse({"view": view, "order": [], "hidden": [], "saved": False})

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
        runtime.auth.forget_user(uid)  # the change must show on the next v2 read
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
        role = body.get("role", "") if isinstance(body, dict) else ""
        if not isinstance(role, str):
            return JSONResponse({"detail": "role must be text"}, status_code=422)
        shown = {"host": host[:MAX_NAME], "scope": str(scope)[:8], "role": role[:16]}
        if scope not in (MARKER, READ_MARKER, *plugins.scopes):
            # Only wpi and the scopes of listed plugins can be issued; a disabled plugin's cannot.
            await audit.record(store, "key_create_failed", actor=sess.username, method="POST",
                               path="/api/admin/keys", status=422, remote=remote,
                               detail={**shown, "reason": "unknown scope"})
            return JSONResponse({"detail": "unknown key scope"}, status_code=422)
        try:
            plaintext, info = await create_key(store, host, created_by=sess.username,
                                               scope=scope, role=role)
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
                                   "scope": info.scope, "role": info.role})
        # The plaintext appears in this one response, which is sent with Cache-Control: no-store.
        return JSONResponse({"id": info.prefix, "host": info.host, "scope": info.scope,
                             "role": info.role, "key": plaintext})

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

    infra = InfraService(store, map_clock)
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

    mapper = MapService(config, infra, matcher, state_of, map_clock, live_port)
    app.state.mapper = mapper
    scheduler.hooks.append(mapper.tick)

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
        scheduler.add_collectors(plugins)

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
        await mapper.rebuild()  # the switch now shows its monitor
        return JSONResponse({"ok": True})

    ports = PortPages(infra, matcher, mapper, map_clock)
    # The v2 map, port and finding resources read through these services.
    runtime.infra = SimpleNamespace(mapper=mapper, matcher=matcher, ports=ports,
                                    live_port=live_port)

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

    @app.get("/hosts/new", include_in_schema=False)
    async def add_host_page() -> FileResponse:
        # The page holds no data; hosts-new.js needs an admin session for everything it does.
        return FileResponse(STATIC / "hosts-new.html")

    @app.get("/admin/tiers", include_in_schema=False)
    async def admin_tiers_page() -> FileResponse:
        # The page holds no data; it reads and writes through /api/v2 with an admin session.
        return FileResponse(STATIC / "admin-tiers.html")

    @app.get("/admin/retention", include_in_schema=False)
    async def admin_retention_page() -> FileResponse:
        # The page holds no data; it reads and writes through /api/v2 with an admin session.
        return FileResponse(STATIC / "admin-retention.html")

    @app.get("/admin/recheck", include_in_schema=False)
    async def admin_recheck_page() -> FileResponse:
        # The page holds no data; it reads and writes through /api/v2 with an admin session.
        return FileResponse(STATIC / "admin-recheck.html")

    @app.get("/admin/rules", include_in_schema=False)
    async def admin_rules_page() -> FileResponse:
        # The page holds no data; it reads and writes through /api/v2 with an admin session.
        return FileResponse(STATIC / "admin-rules.html")

    @app.get("/admin/storage", include_in_schema=False)
    async def admin_storage_page() -> FileResponse:
        # The page holds no data; it reads and writes through /api/v2 with an admin session.
        return FileResponse(STATIC / "admin-storage.html")

    @app.get("/audit", include_in_schema=False)
    async def audit_page() -> FileResponse:
        # The page holds no data; audit.js needs an admin session for everything it shows.
        return FileResponse(STATIC / "audit.html")

    pushed = {m.host: m for m in scheduler.monitors if m.type == "pushed_host"}
    # The address install commands carry. Never the request's Host header, which the sender
    # controls: `server.public_url`, or the address an admin confirmed and saved.
    async def public_url() -> str:
        return (await enrol.get_public_url(store, config.server.public_url))[0]

    URL_NEEDED = {"detail": "Observe does not know the address hosts should use to reach it. "
                            "Confirm it, then try again.", "code": "public_url_required"}

    @app.get("/api/enrol/public-url", include_in_schema=False)
    async def get_public_url(_: authmod.Session = Depends(guards.admin)) -> dict[str, Any]:
        """The Observe address install commands use and where it came from: "config" (set in the
        file, so it cannot be changed here), "saved" (confirmed in the wizard) or "" (not set).
        Admin session."""
        url, source = await enrol.get_public_url(store, config.server.public_url)
        return {"url": url, "source": source}

    @app.put("/api/enrol/public-url", include_in_schema=False)
    async def put_public_url(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """Save the address an admin confirmed. It must be http(s)://host[:port] with no path and
        no loopback name. Refused with 409 when `server.public_url` is set, because the file
        wins. Admin session and CSRF."""
        remote = request.client.host if request.client else ""
        body = await body_of(request)

        async def refused(status: int, reason: str) -> JSONResponse:
            await audit.record(store, "enrol_public_url_failed", actor=sess.username, method="PUT",
                               path="/api/enrol/public-url", status=status, remote=remote,
                               detail={"reason": reason})
            return JSONResponse({"detail": reason}, status_code=status)

        if config.server.public_url:
            return await refused(409, "the address is set in the Observe config as "
                                      "server.public_url, so it is changed there")
        try:
            url = await enrol.set_public_url(store, body.get("url"), auth_clock())
        except enrol.EnrolError as err:
            return await refused(err.status, err.reason)
        await audit.record(store, "enrol_public_url_set", actor=sess.username, method="PUT",
                           path="/api/enrol/public-url", status=200, remote=remote,
                           detail={"url": url})
        return JSONResponse({"url": url, "source": "saved"})

    @app.put("/api/admin/retention", include_in_schema=False)
    async def put_retention(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """Change any of the settings in the body (null resets one; `overrides` replaces the
        whole per-metric list). Admin session and CSRF. Refused values answer 422 and are
        audited; a change is audited with its old and new values."""
        remote = request.client.host if request.client else ""
        body = await body_of(request)
        try:
            out = await retention.update_settings(
                store, body, actor=sess.username, remote=remote, now=auth_clock(),
                fallback_raw_days=config.server.retention_days)
        except retention.RetentionError as err:
            await audit.record(store, "retention_settings_failed", actor=sess.username,
                               method="PUT", path=retention.PATH, status=422, remote=remote,
                               detail={"reason": str(err)})
            return JSONResponse({"detail": str(err)}, status_code=422)
        return JSONResponse(out)

    @app.put("/api/admin/recheck", include_in_schema=False)
    async def put_recheck(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """Change any global value in the body (null resets one) and, with `overrides`, replace
        the whole per-monitor list. Admin session and CSRF. Refused values answer 422 and are
        audited; a change is audited with its old and new values and applies at the next result
        of each monitor."""
        remote = request.client.host if request.client else ""
        body = await body_of(request)
        try:
            changes, overrides = recheck_settings.validate(body, {m.slug for m in config.monitors})
        except recheck_settings.RecheckError as err:
            await audit.record(store, "recheck_settings_failed", actor=sess.username,
                               method="PUT", path=recheck_settings.PATH, status=422,
                               remote=remote, detail={"reason": str(err)})
            return JSONResponse({"detail": str(err)}, status_code=422)
        await store.storage.write(lambda db: recheck_settings.save(
            db, changes, overrides, now=auth_clock(), actor=sess.username, remote=remote),
            touches=("admin", "audit"))
        glob, saved = await store.storage.read(recheck_settings.load)
        scheduler.apply_recheck(glob, saved)
        return JSONResponse(recheck_settings.describe(config, glob, saved))

    @app.put("/api/admin/tiers", include_in_schema=False)
    async def put_tiers(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """Change global rates (`global`, null resets one) and, with `hosts`, replace the whole
        per-host override map. Admin session and CSRF. A refused value answers 422 and is audited;
        a change is audited with its old and new values and reaches an agent the next time it
        reads its config."""
        remote = request.client.host if request.client else ""
        body = await body_of(request)
        known = set(await store.storage.read(tiers.known_hosts))
        try:
            changes, overrides = tiers.validate(body, known)
        except tiers.TierError as err:
            await audit.record(store, "tier_rates_failed", actor=sess.username,
                               method="PUT", path=tiers.PATH, status=422,
                               remote=remote, detail={"reason": str(err)})
            return JSONResponse({"detail": str(err)}, status_code=422)
        await store.storage.write(lambda db: tiers.save(
            db, changes, overrides, now=auth_clock(), actor=sess.username, remote=remote),
            touches=("admin", "audit"))
        glob, saved = await store.storage.read(tiers.load)
        known = await store.storage.read(tiers.known_hosts)
        return JSONResponse(tiers.describe(glob, saved, known))

    @app.put("/api/admin/rules", include_in_schema=False)
    async def put_rules(
            request: Request, sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """Replace the whole threshold rule list. Admin session and CSRF. A refused rule answers
        422 and is audited; a change is audited with the old and new rules. Saved rules are
        stored for the rule engine; nothing evaluates them yet (docs/ARCHITECTURE.md)."""
        remote = request.client.host if request.client else ""
        body = await body_of(request)
        try:
            parsed = rules.validate(body)
        except rules.RuleError as err:
            await audit.record(store, "rules_failed", actor=sess.username, method="PUT",
                               path=rules.PATH, status=422, remote=remote,
                               detail={"reason": str(err)})
            return JSONResponse({"detail": str(err)}, status_code=422)
        await store.storage.write(lambda db: rules.save(
            db, parsed, now=auth_clock(), actor=sess.username, remote=remote),
            touches=("admin", "audit"))
        return JSONResponse({"rules": [r.as_dict() for r in parsed], "max_rules": rules.MAX_RULES})

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

        async def refused(status: int, reason: str, **extra: str) -> JSONResponse:
            await audit.record(store, "enrol_create_failed", actor=sess.username, method="POST",
                               path="/api/hosts", status=status, remote=remote,
                               detail={**shown, "reason": reason})
            return JSONResponse({"detail": reason, **extra}, status_code=status)

        try:
            spec = enrol.parse_spec(body)
            pool = enrol.parse_pool(body.get("pool"), spec.platform)
            now = auth_clock()
            if spec.name in pushed:
                raise enrol.EnrolError("a host with this name already exists", 409)
            base = await public_url()
            if not base:
                # Before the token is made, so a missing address costs nothing.
                return await refused(409, URL_NEEDED["detail"], code=URL_NEEDED["code"])
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
            "command": enrol.command_text(spec.name, spec.platform, base, token, pool)})

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

    @app.post("/api/hosts/{host}/enrolment/regenerate", include_in_schema=False)
    async def regenerate_enrolment(
            host: str, request: Request,
            sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """A new install command for an enrolment whose script was not fetched yet, which is how
        the wizard recovers from an expired token. The old token is revoked by being replaced.
        Admin session and CSRF. Like create, the token is in this response only (no-store)."""
        remote = request.client.host if request.client else ""
        try:
            body = await request.json()
        except ValueError:
            body = None
        raw_pool = body.get("pool") if isinstance(body, dict) else None
        now = auth_clock()
        base = await public_url()
        if not base:
            return JSONResponse(URL_NEEDED, status_code=409)
        made = await enrol.regenerate_enrolment(store, host, now)
        if made is None:
            await audit.record(store, "enrol_regenerate_failed", actor=sess.username,
                               method="POST", path="/api/hosts/[host]/enrolment/regenerate",
                               status=404, remote=remote, detail={"host": host[:MAX_NAME]})
            raise HTTPException(404, "no enrolment waiting for its script for this host")
        token, spec = made
        try:
            pool = enrol.parse_pool(raw_pool, spec.platform)
        except enrol.EnrolError:
            pool = ""
        await audit.record(store, "enrol_regenerated", actor=sess.username, method="POST",
                           path="/api/hosts/[host]/enrolment/regenerate", status=200,
                           remote=remote, detail={"host": spec.name, "platform": spec.platform})
        return JSONResponse({
            "host": spec.name, "platform": spec.platform,
            "platform_label": enrol.PLATFORMS[spec.platform],
            "expires_at": now + enrol.TOKEN_TTL_S, "ttl_s": enrol.TOKEN_TTL_S,
            "command": enrol.command_text(spec.name, spec.platform, base, token, pool)})

    enrol_limiter = RateLimiter(config.server.plugin_rate_per_minute, ingest_clock)
    app.state.observe_machine_id = scripts_machine_id()

    def control_public_key() -> str:
        loaded = plugins.get("control")
        return str(getattr(loaded.plugin, "public_key", "") or "") if loaded else ""

    @app.get("/i/{token}", include_in_schema=False)
    async def install_script(token: str, request: Request) -> Response:
        """The install script for one enrolment. No session: the token in the path is the
        credential. This only serves the script. It holds no key and it does not spend the token,
        so a command pasted on the wrong machine, or fetched twice, costs nothing. The script runs
        its guards and only then posts the token to POST /api/enrol/redeem. A platform with no
        script yet, or control without a loaded control plugin, is refused here. The response is
        `no-store` and the token is never logged."""
        remote = request.client.host if request.client else ""
        if not enrol_limiter.allow(remote or "unknown"):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
        now = auth_clock()
        plan = await enrol.preview(store, token, now)
        if plan is None:
            await audit.record(store, "enrol_fetch_failed", method="GET", path="/i/[token]",
                               status=410, remote=remote,
                               detail={"reason": "unknown, used or expired token"})
            return PlainTextResponse("This install command was already used or has expired. "
                                     "Make a new one in the Observe console.\n", status_code=410)
        platform, wants_control = plan.platform, plan.control_key is not None
        if platform not in scripts.SCRIPT_PLATFORMS:
            raise HTTPException(501, "no install script for this platform yet")
        if wants_control and platform in scripts.AGENT_ONLY_PLATFORMS:
            raise HTTPException(409, "control is not available for this platform yet")
        if wants_control and not control_public_key():
            raise HTTPException(409, "control is not set up on this Observe server")
        if platform == "truenas" and not scripts.valid_pool(request.query_params.get("pool", "")):
            raise HTTPException(400, "the pool name has characters that are not allowed")
        ctx = await script_context()
        if ctx is None:
            return PlainTextResponse(
                scripts.error_body(platform, "Observe has no confirmed address yet. "
                                             "Confirm it in the Observe console and make a new command"),
                status_code=409)
        try:
            body = scripts.render(plan, ctx, request.query_params.get("pool", ""))
        except scripts.ScriptError as err:
            await audit.record(store, "enrol_script_failed", method="GET", path="/i/[token]",
                               status=500, remote=remote,
                               detail={"host": plan.host, "reason": str(err), "token_spent": False})
            return PlainTextResponse(scripts.error_body(platform), status_code=500)
        return PlainTextResponse(body, media_type=scripts.media_type(platform))

    @app.post("/api/enrol/guard", include_in_schema=False)
    async def install_guard(request: Request) -> Response:
        """A guard of the install script refused to run (wrong machine, not root, the Observe host
        itself). The body is {"token", "step", "found"}. The reason is kept for the wizard and the
        settings page and the token stays valid, so the same command works on the right machine.
        The token is the credential. `found` is the name the machine gave itself, cut to the
        characters a host name has."""
        remote = request.client.host if request.client else ""
        if not enrol_limiter.allow(remote or "unknown"):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
        body = await body_of(request)
        step = body.get("step") if isinstance(body.get("step"), str) else ""
        host = await enrol.record_guard_failure(
            store, body.get("token", ""), step, body.get("found", ""), auth_clock())
        if host is None:
            raise HTTPException(404, "unknown, used or expired token")
        await audit.record(store, "enrol_guard_refused", actor=host, method="POST",
                           path="/api/enrol/guard", status=200, remote=remote,
                           detail={"host": host, "step": step})
        return Response(status_code=204)

    @app.post("/api/enrol/redeem", include_in_schema=False)
    async def install_redeem(request: Request) -> Response:
        """Spend the token and return the keys, to the install script after its guards passed.
        The body is {"token"}. One call succeeds per token; a second, or an expired token, is
        410. The reply is sent with Cache-Control: no-store and never logged or audited."""
        remote = request.client.host if request.client else ""
        if not enrol_limiter.allow(remote or "unknown"):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
        body = await body_of(request)
        token = body.get("token", "")
        red = await enrol.redeem(store, token if isinstance(token, str) else "", auth_clock(),
                                 remote)
        if red is None:
            return JSONResponse({"detail": "This install command was already used or has "
                                           "expired. Make a new one in the Observe console."},
                                status_code=410)
        return JSONResponse({"host": red.host, "agent_key": red.agent_key,
                             "control_key": red.control_key, "step_key": red.step_key})

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
        args = (key, str(body["step"]) if ok else "", str(body["status"]) if ok else "",
                body.get("note", "") if ok else "", auth_clock())
        host = await enrol.record_step(store, *args) if key and ok else None
        if host is None and key and ok:
            # Not an install's step key: it may belong to an update or cleanup task.
            host = await hosttasks.record_step(store, *args)
        if host is None:
            # Bounded like login and plugin denials: one row per peer per window, with a count.
            covered = step_denials.note(remote or "unknown")
            if covered is not None:
                detail: dict[str, Any] = {}
                if covered:
                    detail["denials_covered"] = covered
                await audit.record(store, "enrol_step_refused", method="POST",
                                   path="/api/enrol/step", status=401, remote=remote,
                                   detail=detail)
            raise HTTPException(401, "missing or invalid step key",
                                headers={"WWW-Authenticate": "Bearer"})
        if body["status"] in ("failed", "refused"):
            await audit.record(store, "enrol_install_problem", actor=host, method="POST",
                               path="/api/enrol/step", status=200, remote=remote,
                               detail={"host": host, "step": body["step"],
                                       "status": body["status"]})
        return Response(status_code=204)

    # ---- Host settings (docs/GUI-DESIGN.md section 3.11) ----
    async def script_context() -> scripts.Context | None:
        """What the server knows that the enrolment row does not, or None when no Observe address
        is configured or confirmed. The address is never the request's Host header; its host part
        feeds the Observe-host guard when it is an IP address."""
        base = await public_url()
        if not base:
            return None
        listen = scripts.usable_address(config.server.listen)
        addrs = [a for a in (listen, scripts.usable_address(urlsplit(base).hostname or ""))
                 if a is not None]
        return scripts.Context(base, app.state.observe_machine_id,
                               tuple(dict.fromkeys(addrs)), control_public_key())

    async def body_of(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    async def enrolment_row(host: str) -> tuple[Any, ...] | None:
        rows = await store.fetch(
            "SELECT platform, agent, control, allowlist, created, created_by, fetched_at, "
            "allowlist_rev FROM enrolments WHERE host=?", (host,))
        return rows[0] if rows else None

    async def settings_refused(sess: authmod.Session, request: Request, kind: str, host: str,
                               status: int, reason: str, **extra: str) -> JSONResponse:
        await audit.record(store, kind, actor=sess.username, method=request.method,
                           path="/api/hosts/[host]/" + request.url.path.rsplit("/", 1)[-1],
                           status=status, remote=request.client.host if request.client else "",
                           detail={"host": host[:MAX_NAME], "reason": reason})
        return JSONResponse({"detail": reason, **extra}, status_code=status)

    async def make_task(request: Request, sess: authmod.Session, host: str, kind: str,
                        row: tuple[Any, ...], now: float) -> JSONResponse:
        """Store a task for the host and return its headed command, or a JSON refusal."""
        platform, control, allowlist, rev = row[0], row[2], row[3], row[7]
        if not taskscripts.task_supported(kind, platform):
            return await settings_refused(sess, request, "host_task_failed", host, 409,
                                          "this kind of command is not available for this platform")
        if kind == "update" and not control:
            return await settings_refused(sess, request, "host_task_failed", host, 409,
                                          "control was not chosen for this host")
        if kind == "update" and not control_public_key():
            return await settings_refused(sess, request, "host_task_failed", host, 409,
                                          "control is not set up on this Observe server")
        ctx = await script_context()
        if ctx is None:
            return await settings_refused(sess, request, "host_task_failed", host, 409,
                                          URL_NEEDED["detail"], code=URL_NEEDED["code"])
        # Dry run with a placeholder step key: a bad listen address is found before anything is
        # stored.
        try:
            taskscripts.render_task(hosttasks.RedeemedTask(
                host, kind, platform, json.loads(allowlist), rev, "wps_" + "x" * 20), ctx)
        except scripts.ScriptError as err:
            return await settings_refused(sess, request, "host_task_failed", host, 500,
                                          f"Observe could not build the script: {err}")
        token = await hosttasks.create_task(store, host, kind, platform, json.loads(allowlist),
                                            rev, sess.username, now)
        await audit.record(store, "host_task_created", actor=sess.username, method=request.method,
                           path="/api/hosts/[host]/tasks", status=200,
                           remote=request.client.host if request.client else "",
                           detail={"host": host[:MAX_NAME], "kind": kind, "platform": platform,
                                   "rev": rev})
        return JSONResponse({
            "host": host, "kind": kind, "platform": platform,
            "platform_label": enrol.PLATFORMS[platform],
            "expires_at": now + enrol.TOKEN_TTL_S, "ttl_s": enrol.TOKEN_TTL_S,
            "command": hosttasks.task_command_text(host, platform, kind, ctx.observe_url,
                                                   token)})

    @app.get("/hosts/{host}/settings", include_in_schema=False)
    async def host_settings_page(host: str) -> FileResponse:
        # The page holds no data; host-settings.js needs an admin session for everything it does.
        return FileResponse(STATIC / "host-settings.html")

    @app.get("/api/hosts/{host}/settings", include_in_schema=False)
    async def host_settings(host: str, _: authmod.Session = Depends(guards.admin)) -> Response:
        """What the settings page shows for one host: identity, the saved allowlist, whether the
        host has picked it up, and the newest update or cleanup task. Admin session. It never
        returns a token or a key."""
        now = auth_clock()
        row = await enrolment_row(host)
        reporting = {r["host"]: r for r in await store.host_rows()}.get(host)
        keys = await store.fetch(
            "SELECT COUNT(*) FROM ingest_keys WHERE host=? AND scope IN ('wpi', 'wpc') "
            "AND revoked_at IS NULL", (host,))
        if row is None and reporting is None and host not in pushed and not keys[0][0]:
            raise HTTPException(404, "unknown host")
        out: dict[str, Any] = {
            "host": host, "enrolled": row is not None, "in_config": host in pushed,
            "reporting": reporting is not None, "active_keys": keys[0][0],
            "agent_version": reporting["agent_version"] if reporting else "",
            "control_ready": bool(control_public_key()), "ttl_s": enrol.TOKEN_TTL_S,
            "platform": "", "platform_label": "", "agent": False, "control": False,
            "created": None, "created_by": "", "installed": False, "allowlist": None,
            "allowlist_status": None, "can_update": False, "can_cleanup": False, "task": None,
            "enrolment": None}
        url, source = await enrol.get_public_url(store, config.server.public_url)
        out["public_url"] = {"url": url, "source": source}
        if row is not None:
            platform, agent, control, allowlist, created, created_by, fetched_at, _rev = row
            out.update(
                platform=platform, platform_label=enrol.PLATFORMS[platform], agent=bool(agent),
                control=bool(control), created=created, created_by=created_by,
                installed=fetched_at is not None,
                allowlist=enrol.spec_from_row(host, platform, agent, control, allowlist).allowlist(),
                allowlist_status=await hosttasks.allowlist_status(store, host),
                can_update=bool(control) and taskscripts.task_supported("update", platform),
                can_cleanup=taskscripts.task_supported("cleanup", platform))
            prog = await enrol.progress(store, host, now)
            if prog is not None:
                # The state of the install command itself, so the page can say "already used or
                # expired" and offer Regenerate, or show why the script refused to run.
                out["enrolment"] = {"token_state": prog["token_state"], "stalled": prog["stalled"], "guard": prog["guard"],
                                    "expires_at": prog["expires_at"], "state": prog["state"]}
            task = await hosttasks.latest(store, host, now)
            if task is not None and task["state"] == "expired" \
                    and await hosttasks.claim_expiry_audit(store, task["id"], now):
                await audit.record(store, "host_task_expired", method="GET",
                                   path="/api/hosts/[host]/settings", status=200,
                                   detail={"host": host[:MAX_NAME], "kind": task["kind"]})
            out["task"] = task
        return JSONResponse(out)

    @app.put("/api/hosts/{host}/allowlist", include_in_schema=False)
    async def save_allowlist(
            host: str, request: Request,
            sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """Save the control allowlist of an enrolled host and, when the install command was
        already run, make the short update command that rewrites control.toml on the host. The
        body is the allowlist (fans, services, reboot) and `confirmed: true`, which the diff
        dialog sets. Before the install command is run there is no host to update, so the
        saved allowlist simply goes into the install script, and no command is made."""
        body = await body_of(request)
        row = await enrolment_row(host)
        if row is None:
            return await settings_refused(sess, request, "host_allowlist_failed", host, 404,
                                          "this host was not added through the console")
        platform, agent, control, stored, _created, _by, fetched_at, _rev = row
        if not control:
            return await settings_refused(sess, request, "host_allowlist_failed", host, 409,
                                          "control was not chosen for this host")
        if body.get("confirmed") is not True:
            return await settings_refused(sess, request, "host_allowlist_failed", host, 400,
                                          "the change was not confirmed")
        try:
            fans, services, reboot = enrol.parse_allowlist(
                {k: v for k, v in body.items() if k != "confirmed"})
        except enrol.EnrolError as err:
            return await settings_refused(sess, request, "host_allowlist_failed", host,
                                          err.status, err.reason)
        old = enrol.spec_from_row(host, platform, agent, control, stored)
        new = enrol.Spec(host, platform, bool(agent), True, tuple(fans), tuple(services), reboot)
        if new.allowlist() == old.allowlist():
            return await settings_refused(sess, request, "host_allowlist_failed", host, 409,
                                          "the allowlist is unchanged")
        now = auth_clock()
        saved = await store.execute(
            "UPDATE enrolments SET allowlist=?, allowlist_rev=allowlist_rev+1, "
            "allowlist_saved_at=? WHERE host=? RETURNING allowlist_rev",
            (json.dumps(new.allowlist(), sort_keys=True), now, host))
        rev = saved[0][0]
        old_fans, new_fans = {h for h, _ in old.fans}, {h for h, _ in new.fans}
        await audit.record(store, "host_allowlist_saved", actor=sess.username, method="PUT",
                           path="/api/hosts/[host]/allowlist", status=200,
                           remote=request.client.host if request.client else "",
                           detail={"host": host[:MAX_NAME], "rev": rev,
                                   "fans_added": len(new_fans - old_fans),
                                   "fans_removed": len(old_fans - new_fans),
                                   "services_added": len(set(new.services) - set(old.services)),
                                   "services_removed": len(set(old.services) - set(new.services)),
                                   "reboot": reboot})
        out: dict[str, Any] = {"saved": True, "rev": rev, "command": None,
                               "allowlist": new.allowlist()}
        if fetched_at is not None:
            fresh = await enrolment_row(host)
            assert fresh is not None
            made = await make_task(request, sess, host, "update", fresh, now)
            if made.status_code != 200:
                # The allowlist is saved; only the command could not be made. The page shows the
                # reason and the pending status, and the command can be asked for again.
                out["command_error"] = json.loads(made.body)["detail"]
            else:
                out.update(json.loads(made.body))
        out["allowlist_status"] = await hosttasks.allowlist_status(store, host)
        return JSONResponse(out)

    @app.post("/api/hosts/{host}/tasks", include_in_schema=False)
    async def create_host_task(
            host: str, request: Request,
            sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """A new update command for the saved allowlist (when the earlier one expired) or a
        cleanup command for the machine that holds the install. The body is `kind` (update or
        cleanup) and `confirmed: true`. The token is in this response only (no-store)."""
        body = await body_of(request)
        kind = body.get("kind")
        if kind not in hosttasks.KINDS:
            return await settings_refused(sess, request, "host_task_failed", host, 422,
                                          "kind must be update or cleanup")
        if body.get("confirmed") is not True:
            return await settings_refused(sess, request, "host_task_failed", host, 400,
                                          "the request was not confirmed")
        row = await enrolment_row(host)
        if row is None:
            return await settings_refused(sess, request, "host_task_failed", host, 404,
                                          "this host was not added through the console")
        if row[6] is None:
            what = ("update. The saved allowlist is in the install command."
                    if kind == "update" else "clean up.")
            return await settings_refused(
                sess, request, "host_task_failed", host, 409,
                "the install command has not been run yet, so there is nothing to " + what)
        return await make_task(request, sess, host, kind, row, auth_clock())

    @app.post("/api/hosts/{host}/enrolment/reissue", include_in_schema=False)
    async def reissue_enrolment(
            host: str, request: Request,
            sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """A new full install command for a host whose script was already run: the old token,
        the old keys and the old install's reports are revoked at once. The body is
        `confirmed: true` (the dialog) and optionally `pool`. The saved allowlist goes into the
        new script. The host's data is kept. The token is in this response only (no-store)."""
        body = await body_of(request)
        if body.get("confirmed") is not True:
            return await settings_refused(sess, request, "enrol_reissue_failed", host, 400,
                                          "the request was not confirmed")
        now = auth_clock()
        base = await public_url()
        if not base:
            return JSONResponse(URL_NEEDED, status_code=409)
        row = await store.fetch("SELECT platform FROM enrolments WHERE host=?", (host,))
        if not row:
            return await settings_refused(sess, request, "enrol_reissue_failed", host, 404,
                                          "this host was not added through the console")
        # A pool that does not validate is refused, never replaced by the default, so a command
        # is never made for a different pool than the one asked for.
        try:
            pool = enrol.parse_pool(body.get("pool"), row[0][0])
        except enrol.EnrolError as exc:
            return await settings_refused(sess, request, "enrol_reissue_failed", host, 400,
                                          str(exc))
        made = await enrol.reissue_enrolment(store, host, now)
        if made is None:
            return await settings_refused(sess, request, "enrol_reissue_failed", host, 404,
                                          "this host was not added through the console")
        token, spec, revoked = made
        await audit.record(store, "enrol_reissued", actor=sess.username, method="POST",
                           path="/api/hosts/[host]/enrolment/reissue", status=200,
                           remote=request.client.host if request.client else "",
                           detail={"host": host[:MAX_NAME], "platform": spec.platform,
                                   "keys_revoked": revoked})
        return JSONResponse({
            "host": spec.name, "platform": spec.platform,
            "platform_label": enrol.PLATFORMS[spec.platform], "keys_revoked": revoked,
            "expires_at": now + enrol.TOKEN_TTL_S, "ttl_s": enrol.TOKEN_TTL_S,
            "command": enrol.command_text(spec.name, spec.platform, base, token, pool)})

    @app.post("/api/hosts/{host}/keys/revoke", include_in_schema=False)
    async def revoke_host_keys(
            host: str, request: Request,
            sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """Revoke every agent and control key bound to the host. The body is `confirm_host`,
        which must equal the host name character for character (the typed confirmation)."""
        body = await body_of(request)
        if body.get("confirm_host") != host:
            return await settings_refused(sess, request, "host_keys_revoke_failed", host, 400,
                                          "type the host name exactly to confirm")
        count = await hosttasks.revoke_host_keys(store, host, auth_clock())
        await audit.record(store, "host_keys_revoked", actor=sess.username, method="POST",
                           path="/api/hosts/[host]/keys/revoke", status=200,
                           remote=request.client.host if request.client else "",
                           detail={"host": host[:MAX_NAME], "keys_revoked": count})
        return JSONResponse({"host": host, "keys_revoked": count})

    @app.post("/api/hosts/{host}/remove", include_in_schema=False)
    async def remove_host(
            host: str, request: Request,
            sess: authmod.Session = Depends(guards.admin_mutating)) -> Response:
        """Remove the host: revoke its keys and delete its enrolment, tasks and stored hardware
        data. The audit log and the control command history are kept. The body is
        `confirm_host`, which must equal the host name exactly. A host that is listed in the
        Observe config is refused (409), because it would come back."""
        body = await body_of(request)
        if body.get("confirm_host") != host:
            return await settings_refused(sess, request, "host_remove_failed", host, 400,
                                          "type the host name exactly to confirm")
        if host in pushed:
            return await settings_refused(sess, request, "host_remove_failed", host, 409,
                                          "this host is listed in the Observe config; remove "
                                          "it there first")
        counts = await hosttasks.remove_host(store, host, auth_clock())
        if counts is None:
            return await settings_refused(sess, request, "host_remove_failed", host, 404,
                                          "unknown host")
        await audit.record(store, "host_removed", actor=sess.username, method="POST",
                           path="/api/hosts/[host]/remove", status=200,
                           remote=request.client.host if request.client else "",
                           detail={"host": host[:MAX_NAME], **counts})
        return JSONResponse({"host": host, "removed": counts})

    @app.get("/t/{token}", include_in_schema=False)
    async def task_script(token: str, request: Request) -> Response:
        """The script of an update or cleanup task. No session: the single-use token in the path
        is the credential, spent by this fetch (a second fetch is 410). A kind with no script for
        the platform, or an update without a loaded control plugin, is refused before the token
        is spent. The response is `no-store` and the token is never logged."""
        remote = request.client.host if request.client else ""
        if not enrol_limiter.allow(remote or "unknown"):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
        now = auth_clock()
        known = await hosttasks.peek(store, token, now)
        platform_of = known[1] if known is not None else ""
        ctx = await script_context()
        if ctx is None:
            raise HTTPException(409, "Observe has no confirmed address yet")
        if known is not None:
            kind, platform, host, allowlist = known
            if not taskscripts.task_supported(kind, platform):
                raise HTTPException(501, "no script for this platform")
            if kind == "update" and not control_public_key():
                raise HTTPException(409, "control is not set up on this Observe server")
            try:
                taskscripts.render_task(hosttasks.RedeemedTask(
                    host, kind, platform, allowlist, 0, "wps_" + "x" * 20), ctx)
            except scripts.ScriptError as err:
                await audit.record(store, "host_task_script_failed", method="GET",
                                   path="/t/[token]", status=500, remote=remote,
                                   detail={"reason": str(err), "token_spent": False})
                return PlainTextResponse(scripts.error_body(platform_of), status_code=500)
        task = await hosttasks.redeem(store, token, now, remote)
        if task is None:
            return PlainTextResponse("This command was already used or has expired. "
                                     "Make a new one in the Observe console.\n", status_code=410)
        try:
            body = taskscripts.render_task(task, ctx)
        except scripts.ScriptError as err:
            await audit.record(store, "host_task_script_failed", method="GET", path="/t/[token]",
                               status=500, remote=remote,
                               detail={"host": task.host, "reason": str(err)})
            return PlainTextResponse(scripts.error_body(platform_of), status_code=500)
        return PlainTextResponse(body, media_type=scripts.media_type(task.platform))

    @app.get("/host", include_in_schema=False)
    async def host_page() -> FileResponse:
        # Like /login, the page holds no data; host.js sends a visitor without a session to /login.
        return FileResponse(STATIC / "host.html")

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
