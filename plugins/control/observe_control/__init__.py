"""The control plugin: signed actions that hosts pull and verify (docs/CONTROL.md).

This slice holds the plugin skeleton, the Ed25519 signing key, canonical JSON with sign and
verify, the host-bound wpc key scope, the signed command queue with expiry, seq and rate limits,
the key-authenticated pull and results routes, and the admin action routes.
The admin routes request an action (with confirmation, and the typed host name for a reboot),
list a host's command history and cancel a scheduled reboot.
Observe never connects to a host; a host's daemon pulls with its wpc key.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fastapi.responses import JSONResponse

from observe import audit
from observe.plugins import KeyScope, Migration, PluginBase, PluginError, PluginRouter

from .actions import CONTROLLERS, MODES, capabilities, validate
from .api import register as register_resources
from .keys import SCOPE
from .queue import (ACTIONS, MAX_HOST, MIGRATIONS, REBOOT, Limits, QueueError, cancel_command,
                    enqueue_command, list_commands, pull_commands, record_result)
from .signing import SigningError, load_private_key, public_key_string

__version__ = "0.1.0"


class ControlSettings(BaseModel):
    """`plugin_settings.control`."""

    model_config = ConfigDict(extra="forbid")
    # Private key file, written by `--control-keygen`. Never served or logged.
    signing_key_file: str = Field(default="/run/secrets/observe_control_key", min_length=1,
                                  max_length=1024)
    # What the daemon is told to poll at; a hint, the daemon keeps its own floor.
    pull_interval_s: int = Field(default=5, ge=1, le=300)
    max_pending_per_action: int = Field(default=1, ge=1, le=10)
    max_commands_per_host_per_hour: int = Field(default=10, ge=1, le=1000)
    reboot_min_interval_s: int = Field(default=900, ge=60, le=86400)
    # How long a new command stays valid. The daemon allows 30 s of clock skew on top.
    command_ttl_s: int = Field(default=120, ge=30, le=3600)


MAX_RESULT_BYTES = 262_144


class ResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    state: str = Field(min_length=1, max_length=16)
    output: str = Field(default="", max_length=MAX_RESULT_BYTES)
    started_at: float | None = Field(default=None, allow_inf_nan=False)
    finished_at: float | None = Field(default=None, allow_inf_nan=False)


def _now(request: Request) -> float:
    return request.app.state.plugin_clock()  # type: ignore[no-any-return]


def build_pull_router() -> APIRouter:
    router = APIRouter()

    @router.get("/control/commands")
    async def pull_commands_route(request: Request, host: str = "") -> dict[str, Any]:
        """This host's unexpired, not yet final commands, signed. A wpc key may ask only for
        the host it is bound to."""
        prefix, bound = request.state.plugin_key
        if host != bound:
            raise HTTPException(403, "this key is bound to another host")
        remote = request.client.host if request.client else ""
        got = await pull_commands(request.app.state.plugin_store, bound, prefix, remote,
                                  _now(request))
        return {"host": bound, **got}

    @router.post("/control/results")
    async def post_result(request: Request) -> JSONResponse:
        """Record the outcome of one of this host's commands. Output is redacted and cut."""
        prefix, bound = request.state.plugin_key

        def refuse(status: int, reason: str, command_id: str = "") -> JSONResponse:
            request.state.audit_detail = {"outcome": "refused", "reason": reason[:200],
                                          "command_id": command_id[:64]}
            return JSONResponse({"detail": reason}, status_code=status)

        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_RESULT_BYTES + 4096:
            return refuse(413, "body too large")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_RESULT_BYTES + 4096:
                return refuse(413, "body too large")
        try:
            parsed = ResultBody.model_validate_json(bytes(body))
        except ValueError:
            return refuse(422, "the result is not valid")
        try:
            done = await record_result(
                request.app.state.plugin_store, bound, prefix, parsed.id, parsed.state,
                parsed.output, parsed.started_at, parsed.finished_at, _now(request))
        except QueueError as err:
            return refuse(err.status, err.reason, parsed.id)
        request.state.audit_detail = {"outcome": parsed.state, "command_id": parsed.id,
                                      "output_truncated": done["truncated"]}
        return JSONResponse({"id": parsed.id, "state": parsed.state})

    return router


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=MAX_HOST)
    action: str = Field(min_length=1, max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)
    # The dialog's Confirm button sets this; only the JSON boolean true counts, so the
    # field is not coerced: "true", 1 and "yes" are refused.
    confirmed: Any = None
    # A reboot also needs the host name typed exactly.
    confirm_host: str = Field(default="", max_length=MAX_HOST + 64)


def build_admin_router(plugin: "ControlPlugin") -> APIRouter:
    router = APIRouter()

    @router.get("/commands")
    async def commands(request: Request, limit: int = 100, host: str = "") -> dict[str, Any]:
        """Recent commands with their state and latest result, for one host when `host` is
        given. Admin session enforced by the core. An expired command with no result is
        reported as unknown, never as done."""
        return {"commands": await list_commands(request.app.state.plugin_store,
                                                _now(request), limit, host or None)}

    @router.get("/capabilities")
    async def host_capabilities(request: Request, host: str = "") -> dict[str, Any]:
        """The actions and what the host last reported, so the page can offer valid choices."""
        data = await request.app.state.plugin_store.latest_host(host) if host else None
        return {"host": host, "known": data is not None, "actions": list(ACTIONS),
                "capabilities": capabilities(data["samples"] if data else []),
                "controllers": list(CONTROLLERS), "modes": list(MODES)}

    @router.post("/request")
    async def request_action(request: Request, body: RequestBody) -> JSONResponse:
        """Queue one signed command. Admin session and CSRF are enforced by the core. Every
        action needs `confirmed: true`, and a reboot also needs `confirm_host` equal to the host
        name, character for character."""
        store = request.app.state.plugin_store
        actor = request.state.session.username

        async def refuse(status: int, reason: str) -> JSONResponse:
            await audit.record(store, "control_request_refused", actor=actor, status=status,
                               detail={"host": body.host, "action": body.action,
                                       "reason": reason})
            return JSONResponse({"detail": reason}, status_code=status)

        data = await store.latest_host(body.host)
        if data is None:
            return await refuse(404, "this host has never reported")
        if body.confirmed is not True:
            return await refuse(400, "the request was not confirmed")
        if body.action == REBOOT and body.confirm_host != body.host:
            return await refuse(400, "type the host name exactly to confirm a reboot")
        try:
            params = validate(body.action, body.params, capabilities(data["samples"]))
            made = await enqueue_command(store, plugin.signing_key, plugin.limits(), body.host,
                                         body.action, params, actor, _now(request))
        except QueueError as err:
            if err.status == 422:
                return await refuse(422, err.reason)
            return JSONResponse({"detail": err.reason}, status_code=err.status)
        command = made["command"]
        request.state.audit_detail = {"command_id": command["id"], "action": body.action,
                                      "host": body.host}
        return JSONResponse({"id": command["id"], "state": "requested", "seq": command["seq"],
                             "expires_at": command["expires_at"]})

    @router.post("/commands/{command_id}/cancel")
    async def cancel(request: Request, command_id: str) -> JSONResponse:
        """Cancel a reboot that the host has scheduled. Only a `scheduled` command qualifies."""
        try:
            done = await cancel_command(request.app.state.plugin_store, command_id[:64],
                                        request.state.session.username, _now(request))
        except QueueError as err:
            return JSONResponse({"detail": err.reason}, status_code=err.status)
        request.state.audit_detail = {"command_id": command_id[:64], "host": done["host"]}
        return JSONResponse(done)

    return router


class ControlPlugin(PluginBase):
    name = "control"
    version = __version__
    core_versions = ">=2026.9,<2027"

    def __init__(self) -> None:
        self.settings = ControlSettings()
        self.signing_key: Any = None
        self.public_key = ""

    def limits(self) -> Limits:
        fields = Limits.__dataclass_fields__
        return Limits(**{k: v for k, v in self.settings.model_dump().items() if k in fields})

    def config_model(self) -> type[BaseModel]:
        return ControlSettings

    def configure(self, settings: Any) -> None:
        self.settings = settings or ControlSettings()
        try:
            self.signing_key = load_private_key(self.settings.signing_key_file)
        except SigningError as err:
            raise PluginError(f"plugin 'control': {err}") from err
        self.public_key = public_key_string(self.signing_key)

    def register_api(self, api: Any) -> None:
        """The /api/v2/control resources (docs/DATA-API-DESIGN.md section 4.10)."""
        register_resources(api)

    def routers(self) -> list[PluginRouter]:
        return [PluginRouter(build_pull_router(), key_scope=SCOPE, public_prefix="/api/v1"),
                PluginRouter(build_admin_router(self), admin=True)]

    def migrations(self) -> list[Migration]:
        return list(MIGRATIONS)

    def key_scopes(self) -> list[KeyScope]:
        return [KeyScope(SCOPE, "Control command pull and results, bound to a host name")]


plugin = ControlPlugin()
