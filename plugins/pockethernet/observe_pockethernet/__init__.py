"""The Pockethernet plugin: field reports from the Pockethernet Android app.

This holds the report schema, the wpf key scope, the handler for report log records (reports
arrive as OTLP logs at the core's POST /v1/logs) with its report store,
the mapping from reports to port properties and map edges with an admin rebuild, and the report
list, report detail and jack pages (docs/FIELD-DATA.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from observe.plugins import (KeyScope, Migration, NavEntry, PluginBase, PluginPage,
                               PluginRouter)
from observe.store import Store

from .api import register as register_resources
from .keys import SCOPE
from .derive import rebuild, retry_failed
from .pages import build_pages_router
from .reports import MIGRATIONS, prune_evidence
from .otlp import EVENT, handle_report

__version__ = "0.1.0"

HERE = Path(__file__).parent
BASE = "/plugins/pockethernet"


class PockethernetSettings(BaseModel):
    """`plugin_settings.pockethernet`."""

    model_config = ConfigDict(extra="forbid")
    # Report bodies are the evidence. After this many days without an update the body is
    # dropped; the summary row stays so a replay is still recognised as a duplicate.
    evidence_retention_days: int = Field(default=365, ge=1, le=3650)


def build_admin_router() -> APIRouter:
    router = APIRouter()

    @router.post("/rebuild")
    async def rebuild_derived(request: Request) -> dict[str, Any]:
        """Clear what field reports derived and derive it again from the stored bodies.

        Admin session and CSRF token are enforced by the core; the core audits the request and
        this adds the counts. Refused (409) when retention has dropped any report body.
        """
        result = await rebuild(request.app.state.plugin_store, request.app.state.plugin_clock())
        request.state.audit_detail = {"action": "rebuild", **result.as_detail()}
        if result.pruned:
            raise HTTPException(409, f"{result.pruned} report bodies were dropped by retention, "
                                "so the derived data cannot be rebuilt from reports")
        if result.failed:
            # Rolled back: the old derived data is intact and these reports need attention.
            raise HTTPException(409, {"message": "rebuild rolled back; the old derived data "
                                      "was kept", "failed_reports": result.failures})
        return result.as_detail()

    @router.post("/retry")
    async def retry_derivation(request: Request) -> dict[str, Any]:
        """Derive again every stored report whose derivation failed. Admin and CSRF enforced."""
        result = await retry_failed(request.app.state.plugin_store,
                                    request.app.state.plugin_clock())
        request.state.audit_detail = {"action": "retry", **result.as_detail()}
        return result.as_detail()

    return router


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

    def register_api(self, api: Any) -> None:
        """The /api/v2/pockethernet resources (docs/DATA-API-DESIGN.md section 4.10)."""
        register_resources(api)

    def routers(self) -> list[PluginRouter]:
        return [PluginRouter(build_pages_router()),  # a login session, enforced by the core
                PluginRouter(build_admin_router(), admin=True)]

    def pages(self) -> list[PluginPage]:
        # Static shells with no data; their script reads the session-guarded routes above.
        return [PluginPage(BASE, HERE / "pages" / "reports.html"),
                PluginPage(f"{BASE}/report", HERE / "pages" / "report.html"),
                PluginPage(f"{BASE}/jack", HERE / "pages" / "jack.html")]

    def static_dir(self) -> Path:
        return HERE / "static"

    def nav_entries(self) -> list[NavEntry]:
        return [NavEntry("Field reports", BASE)]

    def migrations(self) -> list[Migration]:
        return list(MIGRATIONS)

    async def prune(self, store: Store, now: float) -> int:
        return await prune_evidence(store, now, self.settings.evidence_retention_days)

    def log_handlers(self) -> dict[str, Any]:
        """A field report is the OTLP log record named observe.pockethernet.report."""
        return {EVENT: handle_report}

    def key_scopes(self) -> list[KeyScope]:
        return [KeyScope(SCOPE, "Pockethernet field report push (OTLP), bound to a device label")]


plugin = PockethernetPlugin()
