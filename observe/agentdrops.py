"""The agent's own data loss, read from its outbox reports.

hostwatch keeps every request it could not deliver in an outbox on disk. When the outbox is over
its limit it drops the oldest data and says so in an event such as "outbox over a limit: 492443
data point(s) ... dropped in total ... 1625 request(s) are queued". The count is a running total
for the agent process. That event is usually info, so on its own it shows only under Recent
events, and the host looks healthy while it is losing data.

`agent_drops` turns those reports into one host-level warning, "agent dropped N data points since
<time>", for as long as the total keeps growing. It clears when the newest report repeats the
previous total, or when no report has arrived for DROP_QUIET_S (the agent reports only while it
drops). A smaller total than the report before it is a restarted agent counting from zero, so
it starts a new run.

The hostwatch event's kind and detail keys are not specified in this repository (hostwatch is a
separate repository), so a report is recognised by "outbox" in its kind or title, and the total is
read from a numeric detail key (DETAIL_KEYS) or, failing that, from the "N data point(s)" text.
"""

from __future__ import annotations

import re
import time
from typing import Any

DROP_QUIET_S = 3600.0
DETAIL_KEYS = ("dropped_total", "dropped_points", "points_dropped", "dropped")
KIND = "agent.outbox_dropping"
_POINTS = re.compile(r"(\d[\d,]*)\s+data point", re.IGNORECASE)


def drop_total(event: dict[str, Any]) -> int | None:
    """The running total of dropped points an outbox report carries, or None for any other
    event."""
    kind = str(event.get("kind", "")).lower()
    title = str(event.get("title", ""))
    if "outbox" not in kind and "outbox" not in title.lower():
        return None
    detail = event.get("detail")
    if isinstance(detail, dict):
        for key in DETAIL_KEYS:
            v = detail.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                return int(v)
    m = _POINTS.search(title)
    return int(m.group(1).replace(",", "")) if m else None


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def agent_drops(events: list[dict[str, Any]], now: float) -> dict[str, Any] | None:
    """The warning for a host whose agent is dropping data now, or None. `dropped` is the growth
    of the total in the current run, `since` when that run was first seen, `at` the newest
    report and `total` the agent's own running total."""
    reports = sorted((float(e["ts"]), total) for e in events
                     if (total := drop_total(e)) is not None)
    if not reports:
        return None
    at, total = reports[-1]
    if now - at > DROP_QUIET_S or total <= 0:
        return None
    # The run of growing totals that ends with the newest report.
    i = len(reports) - 1
    while i > 0 and reports[i - 1][1] < reports[i][1]:
        i -= 1
    if i > 0 and reports[i - 1][1] == reports[i][1]:
        since, base = reports[i]  # the total stood still until here, then grew
    else:
        since, base = reports[i][0], 0  # the first report, or a restart that counts from zero
    dropped = total - base
    if dropped <= 0:
        return None  # the newest report repeats the total before it: the counter stopped
    text = f"agent dropped {dropped:,} data point{'s' if dropped != 1 else ''} since {_when(since)}"
    return {"dropped": dropped, "total": total, "since": since, "at": at, "text": text}


def alert_item(drops: dict[str, Any]) -> dict[str, Any]:
    """The warning as an item of the host's alerts section, shaped like an event."""
    return {"ts": drops["at"], "kind": KIND, "severity": "warning", "source": "observe",
            "title": drops["text"], "detail": {"dropped": drops["dropped"],
                                               "total": drops["total"]}, "boot_id": None}
