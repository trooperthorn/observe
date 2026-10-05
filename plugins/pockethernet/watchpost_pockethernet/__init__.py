"""The Pockethernet plugin: field reports from the Pockethernet Android app.

This holds the report schema, the wpf key scope and the upload endpoint with its report
store. The mapping to port properties and the pages follow (docs/FIELD-DATA.md).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from watchpost.plugins import KeyScope, Migration, PluginBase, PluginRouter
from watchpost.store import Store

from .keys import SCOPE
from .reports import MIGRATIONS, prune_evidence
from .upload import build_router

__version__ = "0.1.0"


class PockethernetSettings(BaseModel):
    """`plugin_settings.pockethernet`."""

    model_config = ConfigDict(extra="forbid")
    # Report bodies are the evidence. After this many days without an update the body is
    # dropped; the summary row stays so a replay is still recognised as a duplicate.
    evidence_retention_days: int = Field(default=365, ge=1, le=3650)


class PockethernetPlugin(PluginBase):
    name = "pockethernet"
    version = __version__
    # The core release series this plugin was written against. A mismatch refuses to start.
    core_versions = ">=2026.9,<2027"

    def __init__(self) -> None:
        self.settings = PockethernetSettings()

    def config_model(self) -> type[BaseModel]:
        return PockethernetSettings

    def configure(self, settings: Any) -> None:
        self.settings = settings or PockethernetSettings()

    def routers(self) -> list[PluginRouter]:
        # Key-authenticated by the core with the wpf scope, mounted at /api/v1.
        return [PluginRouter(build_router(), key_scope=SCOPE, public_prefix="/api/v1")]

    def migrations(self) -> list[Migration]:
        return list(MIGRATIONS)

    async def prune(self, store: Store, now: float) -> int:
        return await prune_evidence(store, now, self.settings.evidence_retention_days)

    def key_scopes(self) -> list[KeyScope]:
        return [KeyScope(SCOPE, "Pockethernet field report upload, bound to a device label")]


plugin = PockethernetPlugin()
