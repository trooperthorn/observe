"""Per-user dashboard layout (docs/GUI-DESIGN.md section 2.11, slice S14).

A layout is the order of the dashboard tiles and the set of hidden tiles for one user and one
view. It is stored on the server, so it follows the user between devices. Only the Dashboard is
customisable. Tile ids are "capacity", "findings", "events" and "group:<name>" for a monitor
group. The server cannot know which groups still exist when the layout is read, so it checks the
shape of every id and the client drops ids it no longer shows and appends new ones
(effectiveOrder in observe/static/js/tiles-logic.js). Hiding a tile never deletes data.
"""

from __future__ import annotations

import json
from typing import Any

from .ingest.schema import MAX_NAME
from .store import Store

VIEWS = ("dashboard",)
FIXED_TILES = ("capacity", "findings", "events")
GROUP_PREFIX = "group:"
MAX_TILES = 200
MAX_BODY = 32 * 1024


class LayoutError(Exception):
    def __init__(self, reason: str, status: int = 422) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def valid_tile(tile: Any) -> bool:
    if not isinstance(tile, str):
        return False
    if tile in FIXED_TILES:
        return True
    name = tile[len(GROUP_PREFIX):]
    return tile.startswith(GROUP_PREFIX) and 0 < len(name) <= MAX_NAME and name.isprintable()


def _clean(raw: Any) -> list[str]:
    """Keep valid ids once each, in order. Anything else is a stale or foreign id and is dropped."""
    out: list[str] = []
    for tile in raw if isinstance(raw, list) else []:
        if valid_tile(tile) and tile not in out:
            out.append(tile)
    return out


def parse(body: Any) -> dict[str, list[str]]:
    """Validate a PUT body of {order: [...], hidden: [...]}. Unknown ids are dropped, not stored."""
    if not isinstance(body, dict) or not isinstance(body.get("order"), list) \
            or not isinstance(body.get("hidden", []), list):
        raise LayoutError("order and hidden must be lists")
    if len(body["order"]) > MAX_TILES or len(body.get("hidden", [])) > MAX_TILES:
        raise LayoutError("too many tiles", 413)
    return {"order": _clean(body["order"]), "hidden": _clean(body.get("hidden", []))}


def check_view(view: str) -> None:
    if view not in VIEWS:
        raise LayoutError("unknown view", 404)


async def load(store: Store, user_id: int, view: str) -> dict[str, Any]:
    rows = await store._run("SELECT layout FROM ui_layouts WHERE user_id=? AND view=?",
                            (user_id, view))
    if not rows:
        return {"view": view, "order": [], "hidden": [], "saved": False}
    try:
        data = json.loads(rows[0][0])
    except ValueError:
        data = {}
    return {"view": view, "order": _clean(data.get("order")), "hidden": _clean(data.get("hidden")),
            "saved": True}


async def save(store: Store, user_id: int, view: str, layout: dict[str, list[str]],
               now: float) -> None:
    await store._run(
        "INSERT INTO ui_layouts (user_id, view, layout, updated) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, view) DO UPDATE SET layout=excluded.layout, updated=excluded.updated",
        (user_id, view, json.dumps(layout, sort_keys=True), now))


async def reset(store: Store, user_id: int, view: str) -> None:
    await store._run("DELETE FROM ui_layouts WHERE user_id=? AND view=?", (user_id, view))
