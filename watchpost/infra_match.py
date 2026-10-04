"""Match infrastructure switches and ports to configured monitors, and compute field findings.

Matching only links; it never creates, edits or enables a monitor. A switch matches by LLDP
chassis MAC (a UniFi device monitor configured with that MAC), by management address (the
target host of an snmp, ping or tcp monitor), or by sysName (the target host or UniFi device
name). Those keys are tried in that order, and the first key that finds candidates decides.
If the candidates of the best type tie, nothing is matched, because guessing would attach live
state to the wrong switch. A link made by an admin is kept for as long as its monitor is
configured and is never replaced here.

A port matches an snmp `interface` monitor on the same host when the monitor's interface
(ifName or ifDescr) has the same port key, or its numeric ifIndex equals the port's if_index.
It matches a UniFi device port when the device MAC is the switch's chassis MAC and the port
has a UniFi port index.

Findings are computed when asked for, from the port properties and the live state that a
LiveReader returns. They are shown on the dashboard and port page only. Nothing here calls
an alert target.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import audit
from .config import Config
from .infra import InfraError, InfraService
from .portkey import mac_digits, port_key

SWITCH_TYPES = ("snmp", "unifi_network", "ping", "tcp")
_TYPE_RANK = {"snmp": 0, "unifi_network": 1, "ping": 2, "tcp": 3}


@dataclass(frozen=True)
class PortMatch:
    kind: str  # "snmp" or "unifi"
    monitor: str  # monitor slug
    detail: str  # ifName or ifIndex for snmp, port index for unifi


@dataclass(frozen=True)
class LivePort:
    """What the live data says about a port; None means unknown, never zero."""

    speed_mbps: int | None = None
    vlan: int | None = None
    poe_w: float | None = None


# Returns the live state of a matched port, or None when there is none.
LiveReader = Callable[[PortMatch], LivePort | None]


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str  # "warning" or "info"
    switch_id: str
    port_key: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "severity": self.severity, "switch_id": self.switch_id,
                "port_key": self.port_key, "message": self.message}


def _norm_host(text: str | None) -> str:
    return (text or "").strip().lower()


def _mac_of(text: str | None) -> str | None:
    return mac_digits(text) if text else None


def _pick(cands: list[Any]) -> str | None:
    """The best-typed candidate's slug, or None when the best type is not unique."""
    if not cands:
        return None
    best = min(_TYPE_RANK[m.type] for m in cands)
    top = [m for m in cands if _TYPE_RANK[m.type] == best]
    return top[0].slug if len(top) == 1 else None


class Matcher:
    def __init__(self, config: Config, infra: InfraService) -> None:
        self._config = config
        self._infra = infra
        # Interface monitors belong to a port, so they never stand for a whole switch.
        self._monitors = [m for m in config.monitors if m.enabled and m.type in SWITCH_TYPES
                          and not (m.type == "snmp" and m.mode == "interface")]

    # Switches ---------------------------------------------------------------------------

    def match_switch(self, sid: str, name: str, addresses: list[str]) -> str | None:
        """The slug of the monitor for a switch, or None."""
        chassis = sid[4:] if sid.startswith("mac:") else None
        if chassis:
            hit = _pick([m for m in self._monitors
                         if m.type == "unifi_network" and _mac_of(m.device) == chassis])
            if hit:
                return hit
        addrs = {_norm_host(a) for a in addresses if a}
        if addrs:
            hit = _pick([m for m in self._monitors
                         if m.type != "unifi_network" and _norm_host(m.host) in addrs])
            if hit:
                return hit
        names = {_norm_host(name)} if name else set()
        if sid.startswith("name:"):
            names.add(sid[5:])
        if names:
            hit = _pick([m for m in self._monitors
                         if (m.type != "unifi_network" and _norm_host(m.host) in names)
                         or (m.type == "unifi_network" and _norm_host(m.device) in names)])
            if hit:
                return hit
        return None

    async def sync_switches(self) -> None:
        """Fill in matched_monitor for switches that have none, or whose monitor is gone."""
        usable = {m.slug for m in self._monitors}

        def go(db: Any) -> None:
            rows = db.execute("SELECT switch_id, name, mgmt_addresses, matched_monitor "
                              "FROM infra_switches").fetchall()
            for sid, name, addrs, current in rows:
                if current in usable:
                    continue
                found = self.match_switch(sid, name, json.loads(addrs))
                if found != current:
                    db.execute("UPDATE infra_switches SET matched_monitor=? WHERE switch_id=?",
                               (found, sid))
        await self._infra._run(go)

    async def unlinked(self) -> list[dict[str, Any]]:
        """The queue: switches seen but matching no monitor, oldest first."""
        await self.sync_switches()

        def go(db: Any) -> list[tuple[Any, ...]]:
            return db.execute(
                "SELECT switch_id, name, mgmt_addresses, vendor, platform, first_seen, last_seen "
                "FROM infra_switches WHERE matched_monitor IS NULL "
                "ORDER BY first_seen, switch_id").fetchall()
        return [{"switch_id": r[0], "name": r[1], "mgmt_addresses": json.loads(r[2]),
                 "vendor": r[3], "platform": r[4], "first_seen": r[5], "last_seen": r[6]}
                for r in await self._infra._run(go)]

    async def link_switch(self, sid: str, slug: str, actor: str, remote: str = "") -> None:
        """An admin links a queued switch to a configured monitor. Audited."""
        if not actor:
            raise InfraError("linking needs an actor")
        if slug not in {m.slug for m in self._monitors}:
            await self._audit("infra_switch_link_failed", actor, remote, sid, slug,
                              "unknown or unsuitable monitor")
            raise InfraError("unknown or unsuitable monitor")

        def go(db: Any) -> bool:
            cur = db.execute("UPDATE infra_switches SET matched_monitor=? WHERE switch_id=?",
                             (slug, sid))
            return bool(cur.rowcount == 1)
        if not await self._infra._run(go):
            await self._audit("infra_switch_link_failed", actor, remote, sid, slug,
                              "unknown switch")
            raise InfraError("unknown switch")
        await self._audit("infra_switch_linked", actor, remote, sid, slug, "")

    async def _audit(self, kind: str, actor: str, remote: str, sid: str, slug: str,
                     reason: str) -> None:
        detail = {"switch_id": sid[:140], "monitor": slug[:140]}
        if reason:
            detail["reason"] = reason
        await audit.record(self._infra._store, kind, actor=actor, method="POST",
                           path="/api/admin/infra/link", remote=remote, detail=detail)

    # Ports ------------------------------------------------------------------------------

    def match_port(self, sid: str, matched_monitor: str | None, addresses: list[str],
                   port: str, if_index: int | None,
                   unifi_index: int | None) -> list[PortMatch]:
        """Monitors for one port. `port` must already be a port key."""
        out: list[PortMatch] = []
        chassis = sid[4:] if sid.startswith("mac:") else None
        hosts = {_norm_host(a) for a in addresses if a}
        sw = next((m for m in self._monitors if m.slug == matched_monitor), None)
        if sw is not None and sw.type != "unifi_network":
            hosts.add(_norm_host(sw.host))
        for m in self._config.monitors:
            if not (m.enabled and m.type == "snmp" and m.mode == "interface"
                    and _norm_host(m.host) in hosts):
                continue
            want = m.interface or ""
            if want.isdigit():
                hit = if_index is not None and int(want) == if_index
            else:
                try:
                    hit = port_key(want) == port
                except ValueError:
                    hit = False
            if hit:
                out.append(PortMatch("snmp", m.slug, want))
        if unifi_index is not None and chassis:
            for m in self._monitors:
                if m.type == "unifi_network" and _mac_of(m.device) == chassis:
                    out.append(PortMatch("unifi", m.slug, str(unifi_index)))
        return out

    async def port_matches(self, sid: str, port: str) -> list[PortMatch]:
        await self.sync_switches()
        key = port_key(port)

        def go(db: Any) -> tuple[Any, ...] | None:
            return db.execute(
                "SELECT s.matched_monitor, s.mgmt_addresses, p.if_index, p.unifi_index "
                "FROM infra_ports p JOIN infra_switches s USING (switch_id) "
                "WHERE p.switch_id=? AND p.port_key=?", (sid, key)).fetchone()
        row = await self._infra._run(go)
        if row is None:
            return []
        return self.match_port(sid, row[0], json.loads(row[1]), key, row[2], row[3])

    # Findings ---------------------------------------------------------------------------

    async def findings(self, live: LiveReader) -> list[Finding]:
        """Conflicts between the newest field properties and the live state, computed now."""
        await self.sync_switches()

        def go(db: Any) -> tuple[list[Any], list[Any], list[Any]]:
            ports = db.execute(
                "SELECT p.switch_id, p.port_key, s.matched_monitor, s.mgmt_addresses, "
                "p.if_index, p.unifi_index FROM infra_ports p "
                "JOIN infra_switches s USING (switch_id) ORDER BY p.switch_id, p.port_key"
            ).fetchall()
            props = db.execute(
                "SELECT switch_id, port_key, name, value FROM port_properties WHERE id IN ("
                "SELECT MAX(id) FROM port_properties GROUP BY switch_id, port_key, name)"
            ).fetchall()
            labels = db.execute(
                "SELECT id, switch_id, port_key, value FROM port_properties "
                "WHERE name='jack_label' ORDER BY id").fetchall()
            return ports, props, labels
        ports, props, labels = await self._infra._run(go)
        cur: dict[tuple[str, str], dict[str, Any]] = {}
        for sid, key, name, value in props:
            cur.setdefault((sid, key), {})[name] = json.loads(value)

        out: list[Finding] = []
        for sid, key, mon, addrs, if_index, unifi_index in ports:
            field = cur.get((sid, key), {})
            if not field:
                continue
            matches = self.match_port(sid, mon, json.loads(addrs), key, if_index, unifi_index)
            out.extend(self._compare(sid, key, field, self._live_of(matches, live)))
        out.extend(_repatched(labels))
        return out

    @staticmethod
    def _live_of(matches: list[PortMatch], live: LiveReader) -> LivePort:
        speed = vlan = poe = None
        for m in matches:
            got = live(m)
            if got is None:
                continue
            speed = got.speed_mbps if speed is None else speed
            vlan = got.vlan if vlan is None else vlan
            poe = got.poe_w if poe is None else poe
        return LivePort(speed, vlan, poe)

    @staticmethod
    def _compare(sid: str, key: str, field: dict[str, Any], live: LivePort) -> list[Finding]:
        out: list[Finding] = []
        fs = field.get("link_speed_mbps")
        if fs is not None and live.speed_mbps is not None and fs > live.speed_mbps:
            out.append(Finding("speed_above_live", "warning", sid, key,
                               f"Field test verified {fs} Mbit/s but the port runs at "
                               f"{live.speed_mbps} Mbit/s."))
        fv = field.get("vlan")
        if fv is not None and live.vlan is not None and fv != live.vlan:
            out.append(Finding("vlan_mismatch", "warning", sid, key,
                               f"Field test saw VLAN {fv} but the port is in VLAN {live.vlan}."))
        verified = any(field.get(n) not in (None, "", 0, 0.0)
                       for n in ("poe_class", "poe_load_w", "poe_voltage_v"))
        if verified and live.poe_w is not None and live.poe_w <= 0:
            out.append(Finding("poe_no_power", "warning", sid, key,
                               "Field test verified PoE but the port reports no power."))
        return out


def _repatched(labels: list[Any]) -> list[Finding]:
    """A jack label whose newest row is on a different port than an older row for it."""
    newest: dict[str, tuple[str, str]] = {}
    earlier: dict[str, list[tuple[str, str]]] = {}
    for _id, sid, key, raw in labels:
        label = json.loads(raw)
        if label in newest and newest[label] != (sid, key):
            earlier.setdefault(label, []).append(newest[label])
        newest[label] = (sid, key)
    out = []
    for label, (sid, key) in sorted(newest.items()):
        before = [p for p in earlier.get(label, []) if p != (sid, key)]
        if before:
            was = before[-1]
            out.append(Finding("repatched", "info", sid, key,
                               f"Jack {label} was last reported on {was[0]} port {was[1]}."))
    return out
