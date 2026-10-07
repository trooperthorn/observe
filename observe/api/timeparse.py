"""Times in a query (docs/DATA-API-DESIGN.md section 4.1): an RFC 3339 string or unix seconds,
and for convenience `now` and an offset such as `-24h` (units s, m, h, d, w)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .problems import ApiProblem

_OFFSET = re.compile(r"^-(\d{1,6})([smhdw])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
MIN_TS = 0.0
MAX_TS = 4_102_444_800.0  # 2100-01-01


def parse_time(text: str, now: float, name: str = "time") -> float:
    """Unix seconds for a time in a query. Raises a 400 problem when it is none of the forms."""
    text = text.strip()
    if text == "now":
        return now
    m = _OFFSET.match(text)
    if m:
        return now - int(m.group(1)) * _UNITS[m.group(2)]
    try:
        value = float(text)
    except ValueError:
        value = None
    if value is None:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ApiProblem(400, f"{name} must be an RFC 3339 time, unix seconds, now or an "
                                  "offset such as -24h") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        value = parsed.timestamp()
    if not (MIN_TS <= value <= MAX_TS) or value != value:
        raise ApiProblem(400, f"{name} is out of range")
    return value
