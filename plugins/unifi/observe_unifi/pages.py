"""The UniFi page under Network: devices, clients and Protect cameras.

The page is one static shell (pages/unifi.html) whose script reads three read-only routes. The
core mounts the routes behind its session check (observe/web.py), so nothing here handles
authentication, and the page itself needs a login like every console page. Every string in a row
came from the console or a client on the network (a client names itself), so the script writes it
with textContent only and this module only hands it over as JSON.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from observe.plugins import PluginPage

from .clients import read_cameras, read_clients, read_devices

if TYPE_CHECKING:  # pragma: no cover
    from . import UniFiPlugin

HERE = Path(__file__).parent
PAGE_PATH = "/plugins/unifi"
# A row is stale when its last_seen is older than this many poll intervals.
STALE_FACTOR = 2.5


def page_files() -> list[PluginPage]:
    return [PluginPage(PAGE_PATH, HERE / "pages" / "unifi.html")]


def build_pages_router(plugin: UniFiPlugin) -> APIRouter:
    router = APIRouter()

    @router.get("/devices")
    async def devices(request: Request) -> dict[str, Any]:
        rows = await asyncio.to_thread(read_devices, request.app.state.plugin_store)
        return {"devices": rows}

    @router.get("/clients")
    async def clients(request: Request) -> dict[str, Any]:
        got = await asyncio.to_thread(read_clients, request.app.state.plugin_store,
                                  plugin.wall(), STALE_FACTOR * plugin.settings.clients_interval)
        got["classic_configured"] = plugin.settings.classic_credential is not None
        got["classic_note"] = plugin.classic_note
        return got

    @router.get("/protect")
    async def protect(request: Request) -> dict[str, Any]:
        got = await asyncio.to_thread(read_cameras, request.app.state.plugin_store,
                                  plugin.wall(), STALE_FACTOR * plugin.settings.protect_interval)
        got["enabled"] = plugin.settings.protect
        return got

    return router
