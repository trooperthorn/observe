"""A tiny Observe plugin that exercises every hook, for tests/test_plugins.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from observe.plugins import (KeyScope, MapContribution, Migration, NavEntry, PluginBase,
                               PluginPage, PluginRouter)

STATIC = Path(__file__).parent / "echo_static"


class EchoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    greeting: str = "hello"


class EchoPlugin(PluginBase):
    name = "echo"
    version = "1.2.3"
    core_versions = ">=2026.9,<2027"

    def __init__(self) -> None:
        self.settings: EchoSettings | None = None

    def configure(self, settings: Any) -> None:
        self.settings = settings

    def config_model(self) -> type[BaseModel]:
        return EchoSettings

    def routers(self) -> list[PluginRouter]:
        router = APIRouter()

        @router.get("/hello")
        async def hello() -> dict[str, str]:
            return {"greeting": self.settings.greeting if self.settings else ""}

        @router.post("/things")
        async def things(body: dict[str, Any]) -> dict[str, Any]:
            return {"stored": body}

        @router.post("/boom")
        async def boom() -> None:
            raise RuntimeError("plugin bug")

        secret = APIRouter()

        @secret.get("/secret")
        async def only_admin() -> dict[str, bool]:
            return {"admin": True}

        return [PluginRouter(router), PluginRouter(secret, admin=True)]

    def key_scopes(self) -> list[KeyScope]:
        return [KeyScope("ech", "echo uploads")]

    def migrations(self) -> list[Migration]:
        return [Migration(1, ("CREATE TABLE IF NOT EXISTS echo_things (id INTEGER)",))]

    def pages(self) -> list[PluginPage]:
        return [PluginPage("/plugins/echo", STATIC / "echo.html"),
                PluginPage("/plugins/echo/a", STATIC / "echo.html", admin_only=True)]

    def static_dir(self) -> Path:
        return STATIC

    def nav_entries(self) -> list[NavEntry]:
        return [NavEntry("Echo", "/plugins/echo"), NavEntry("Echo admin", "/plugins/echo/a", True)]

    def monitor_types(self) -> dict[str, Any]:
        return {"echo.ping": object}

    async def map_contribution(self, store: Any) -> MapContribution:
        return MapContribution(nodes=({"id": "echo-1"},))


plugin = EchoPlugin()
