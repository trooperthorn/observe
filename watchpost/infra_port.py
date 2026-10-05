"""The data behind the port page, and acknowledgement of field findings (docs/FIELD-DATA.md).

Findings are computed on each read by the Matcher and never stored. An acknowledgement is the
only thing stored: it names a finding by kind and port and keeps the message it was given for,
so when the facts change the finding is shown as new again. An acknowledged finding is still
listed, marked as acknowledged. Nothing here raises an alert.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from . import audit
from .infra import InfraError, InfraService
from .infra_map import MapService
from .infra_match import LiveReader, Matcher
from .portkey import port_key

HISTORY_LIMIT = 50
WORST_FIRST = ("down", "unreachable", "warn", "pending", "up")


class PortPages:
    def __init__(self, infra: InfraService, matcher: Matcher, mapper: MapService,
                 clock: Callable[[], float] = time.time) -> None:
        self._infra = infra
        self._matcher = matcher
        self._mapper = mapper
        self._clock = clock

    async def _findings(self, sid: str, key: str, live: LiveReader) -> list[dict[str, Any]]:
        found = [f for f in await self._matcher.findings(live)
                 if f.switch_id == sid and f.port_key == key]

        def go(db: Any) -> list[Any]:
            return db.execute("SELECT kind, message, acked_by, acked_at FROM infra_finding_acks "
                              "WHERE switch_id=? AND port_key=?", (sid, key)).fetchall()
        acks = {r[0]: r for r in await self._infra._run(go)}
        out = []
        for f in found:
            row: dict[str, Any] = dict(f.as_dict())
            ack = acks.get(f.kind)
            same = ack is not None and ack[1] == f.message
            row["acknowledged"] = same
            row["acked_by"] = ack[2] if same else None
            row["acked_at"] = ack[3] if same else None
            out.append(row)
        return out

    async def port_view(self, sid: str, port: str, live: LiveReader) -> dict[str, Any] | None:
        """Everything the port page shows, or None for a port that is not in the map."""
        try:
            key = port_key(port)
        except ValueError:
            return None

        def go(db: Any) -> tuple[Any, ...] | None:
            return db.execute(
                "SELECT s.name, s.mgmt_addresses, s.vendor, s.platform, "
                "p.role, p.raw_port_id, p.first_seen, p.last_seen "
                "FROM infra_ports p JOIN infra_switches s USING (switch_id) "
                "WHERE p.switch_id=? AND p.port_key=?", (sid, key)).fetchone()
        row = await self._infra._run(go)
        if row is None:
            return None
        name, addrs, vendor, platform, role, raw, first, last = row
        monitors = []
        for m in await self._matcher.port_matches(sid, key):
            reading = live(m)
            monitors.append({
                "kind": m.kind, "monitor": m.monitor, "detail": m.detail,
                **self._mapper.monitor_state(m.monitor),
                "live": None if reading is None else {
                    "speed_mbps": reading.speed_mbps, "vlan": reading.vlan,
                    "poe_w": reading.poe_w}})
        current = await self._infra.current_properties(sid, key)
        history = {n: await self._infra.property_history(sid, key, n, HISTORY_LIMIT)
                   for n in current}
        findings = await self._findings(sid, key, live)
        states = {m["state"] for m in monitors}
        state = next((w for w in WORST_FIRST if w in states), "unknown")
        if state == "up" and any(not f["acknowledged"] and f["severity"] == "warning"
                                 for f in findings):
            state = "warn"
        return {
            "switch_id": sid, "port_key": key, "raw_port_id": raw, "role": role,
            "first_seen": first, "last_seen": last, "state": state,
            "switch": {"name": name, "mgmt_addresses": json.loads(addrs), "vendor": vendor,
                       "platform": platform},
            "monitors": monitors, "properties": current, "history": history,
            "findings": findings,
        }

    async def acknowledge(self, sid: str, port: str, kind: str, live: LiveReader, actor: str,
                          remote: str = "") -> None:
        """Acknowledge a finding that exists right now. Audited."""
        if not actor:
            raise InfraError("acknowledging needs an actor")
        try:
            key = port_key(port)
        except ValueError:
            key = ""
        detail = {"switch_id": sid[:140], "port_key": key[:140], "kind": kind[:64]}
        path = "/api/admin/infra/findings/ack"
        match = None
        if key:
            match = next((f for f in await self._findings(sid, key, live)
                          if f["kind"] == kind), None)
        if match is None:
            await audit.record(self._infra._store, "infra_finding_ack_failed", actor=actor,
                               method="POST", path=path, status=422, remote=remote,
                               detail={**detail, "reason": "no such finding"})
            raise InfraError("no such finding")
        now = self._clock()

        def go(db: Any) -> None:
            db.execute(
                "INSERT INTO infra_finding_acks (kind, switch_id, port_key, message, acked_by, "
                "acked_at) VALUES (?,?,?,?,?,?) ON CONFLICT(kind, switch_id, port_key) DO UPDATE "
                "SET message=excluded.message, acked_by=excluded.acked_by, "
                "acked_at=excluded.acked_at", (kind, sid, key, match["message"], actor, now))
        await self._infra._run(go)
        await audit.record(self._infra._store, "infra_finding_acknowledged", actor=actor,
                           method="POST", path=path, status=200, remote=remote, detail=detail)
