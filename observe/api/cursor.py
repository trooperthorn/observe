"""Opaque cursors and the page parameters of a list (docs/DATA-API-DESIGN.md section 4.4).

A cursor encodes the sort key of the last item a client saw, so a row inserted later does not
shift the next page. It is URL-safe base64 of a short JSON array of strings and numbers. A
client can edit it, so a handler only uses its values as query parameters and checks how many
there are and what type each one is.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from .problems import ApiProblem

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_CURSOR_CHARS = 512


def encode(values: list[Any]) -> str:
    raw = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(token: str) -> list[Any]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        values = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ApiProblem(400, "the cursor is not valid") from None
    if not isinstance(values, list) or len(values) > 16 or not all(
            isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in values):
        raise ApiProblem(400, "the cursor is not valid")
    return values


@dataclass(frozen=True)
class PageParams:
    limit: int
    cursor: str | None

    def after(self, *types: type | tuple[type, ...]) -> list[Any] | None:
        """The decoded cursor, or None for the first page. Each value must have the type given
        for its position (a float position also takes an int)."""
        if self.cursor is None:
            return None
        values = decode(self.cursor)
        if len(values) != len(types):
            raise ApiProblem(400, "the cursor is not valid")
        for value, kind in zip(values, types):
            allowed = (int, float) if kind is float else kind
            if not isinstance(value, allowed):
                raise ApiProblem(400, "the cursor is not valid")
        return values
