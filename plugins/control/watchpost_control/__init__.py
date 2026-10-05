"""The control plugin: signed actions that hosts pull and verify (docs/CONTROL.md).

This slice holds the plugin skeleton, the Ed25519 signing key, canonical JSON with sign and
verify, the host-bound wpc key scope, and the key-authenticated pull route as an empty queue.
The action catalogue, confirmation UI, command queue, results and cancel come in later slices.
watchpost never connects to a host; a host's daemon pulls with its wpc key.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from watchpost.plugins import KeyScope, PluginBase, PluginError, PluginRouter

from .keys import SCOPE
from .signing import SigningError, load_private_key, public_key_string

__version__ = "0.1.0"


class ControlSettings(BaseModel):
    """`plugin_settings.control`."""

    model_config = ConfigDict(extra="forbid")
    # Private key file, written by `--control-keygen`. Never served or logged.
    signing_key_file: str = Field(default="/run/secrets/watchpost_control_key", min_length=1,
                                  max_length=1024)
    # What the daemon is told to poll at; a hint, the daemon keeps its own floor.
    pull_interval_s: int = Field(default=5, ge=1, le=300)
    max_pending_per_action: int = Field(default=1, ge=1, le=10)
    max_commands_per_host_per_hour: int = Field(default=10, ge=1, le=1000)
    reboot_min_interval_s: int = Field(default=900, ge=60, le=86400)


def build_pull_router() -> APIRouter:
    router = APIRouter()

    @router.get("/control/commands")
    async def pull_commands(request: Request, host: str = "") -> dict[str, Any]:
        """The commands for one host. A wpc key may ask only for the host it is bound to."""
        _prefix, bound = request.state.plugin_key
        if host != bound:
            raise HTTPException(403, "this key is bound to another host")
        return {"host": bound, "commands": []}

    return router


class ControlPlugin(PluginBase):
    name = "control"
    version = __version__
    core_versions = ">=2026.9,<2027"

    def __init__(self) -> None:
        self.settings = ControlSettings()
        self.signing_key: Any = None
        self.public_key = ""

    def config_model(self) -> type[BaseModel]:
        return ControlSettings

    def configure(self, settings: Any) -> None:
        self.settings = settings or ControlSettings()
        try:
            self.signing_key = load_private_key(self.settings.signing_key_file)
        except SigningError as err:
            raise PluginError(f"plugin 'control': {err}") from err
        self.public_key = public_key_string(self.signing_key)

    def routers(self) -> list[PluginRouter]:
        return [PluginRouter(build_pull_router(), key_scope=SCOPE, public_prefix="/api/v1")]

    def key_scopes(self) -> list[KeyScope]:
        return [KeyScope(SCOPE, "Control command pull and results, bound to a host name")]


plugin = ControlPlugin()
