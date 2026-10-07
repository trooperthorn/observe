"""A plugin that adds /api/v2 resources through the optional register_api hook, for
tests/test_api_v2_plugins.py."""

from __future__ import annotations

from typing import Any

from fastapi import Query
from pydantic import BaseModel

from observe.api import ApiContext, ApiRegistry
from observe.api.cursor import PageParams, encode
from observe.plugins import Migration, PluginBase


class Thing(BaseModel):
    id: int
    name: str


class ThingPage(BaseModel):
    items: list[Thing]
    next_cursor: str | None = None


class ThingFilter(BaseModel):
    name_contains: str | None = None


class Counted(BaseModel):
    seen: int


def things(db: Any, page: PageParams, filters: ThingFilter) -> dict[str, Any]:
    """Things from the plugin's own table, read on the read pool."""
    after = page.after(int)
    rows = db.execute(
        "SELECT id, name FROM demo_things WHERE id > ? AND name LIKE ? ORDER BY id LIMIT ?",
        (after[0] if after else 0, f"%{filters.name_contains or ''}%", page.limit + 1)).fetchall()
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    return {"items": [{"id": r[0], "name": r[1]} for r in rows],
            "next_cursor": encode([rows[-1][0]]) if more else None}


class DemoPlugin(PluginBase):
    name = "demo"
    version = "1.0.0"
    core_versions = ">=2026.9,<2027"
    mode = "good"

    def migrations(self) -> list[Migration]:
        return [Migration(1, ("CREATE TABLE IF NOT EXISTS demo_things "
                              "(id INTEGER PRIMARY KEY, name TEXT NOT NULL)",))]

    def register_api(self, api: ApiRegistry) -> None:
        if self.mode == "outside":
            api.resource("/monitors/hijack", things, ThingPage, paginate=True)
            return
        api.resource("/demo/things", things, ThingPage, paginate=True, filters=ThingFilter,
                     domains=("unifi",), tags=("demo",), summary="Demo things")
        self.api = api

        async def bump(ctx: ApiContext, n: int = Query(1, ge=1, le=5)) -> dict[str, Any]:
            """Writes through the registry, which bumps only the declared domains."""
            for _ in range(n):
                await api.submit_write(lambda db: db.execute(
                    "INSERT INTO demo_things (name) VALUES ('bumped')"), touches=("unifi",))
            return {"seen": n}

        api.resource("/demo/bump", bump, Counted, domains=("unifi",), tags=("demo",), roles=("operator",),
                     etag=False, summary="Add things")


plugin = DemoPlugin()
