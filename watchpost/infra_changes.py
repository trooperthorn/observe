"""Field change findings: what got worse, or changed, between the last two reports of a port.

The conflict findings in infra_match.py compare the newest field value with live data. These
compare the newest value of a property with the one before it in the port's property history,
whatever source wrote them. Because the history is append-only and a repeated value only moves
`last_verified`, the last two rows of a property are the last two different values. A change
therefore stays visible until the value changes again, and goes away by itself when a later
report returns it to the earlier value.

Each kind appears at most once per port, so an acknowledgement (infra_port.py) names it by kind.
A property with only one row has no earlier value and produces nothing: a first report is a
baseline, not a change. Nothing here calls an alert target; the findings are shown on the
dashboard, port page and map only.
"""

from __future__ import annotations

from typing import Any

LENGTH_TOLERANCE_M = 2.0  # cable length jitter below this is measurement noise
POE_LOAD_DROP_RATIO = 0.5  # the load fell below this share of the earlier load
VERDICT_RANK = {"pass": 0, "warn": 1, "fail": 2}  # "unknown" has no rank and never compares
_NO_FAULT = frozenset({"", "none", "ok", "pass", "no fault"})
LENGTH_PROPERTIES = ("pair_1_2_length_m", "pair_3_6_length_m", "pair_4_5_length_m",
                     "pair_7_8_length_m")
TRACKED = ("link_speed_mbps", "pair_fault", *LENGTH_PROPERTIES, "poe_class", "poe_load_w",
           "vlan", "voice_vlan", "dhcp_ok", "cable_verdict")

# (kind, severity, message) for one port.
Change = tuple[str, str, str]


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _faulty(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() not in _NO_FAULT


def _num_text(x: float) -> str:
    return str(int(x)) if x == int(x) else f"{x:.1f}"


def port_changes(prev: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    """Findings for one port. `prev` and `new` map a property name to its earlier and newest
    value, and hold only the properties that have both."""
    out: list[Change] = []

    def both(name: str) -> tuple[Any, Any] | None:
        return (prev[name], new[name]) if name in prev and name in new else None

    if (pair := both("link_speed_mbps")) and (a := _num(pair[0])) is not None \
            and (b := _num(pair[1])) is not None and b < a:
        out.append(("speed_drop", "warning", f"Field tests show the link speed dropped from "
                    f"{_num_text(a)} to {_num_text(b)} Mbit/s."))
    if (pair := both("pair_fault")) and _faulty(pair[1]) and pair[1] != pair[0]:
        out.append(("cable_fault", "warning", f"A new cable fault was reported: "
                    f"{str(pair[1]).strip()}."))
    moved = []
    for name in LENGTH_PROPERTIES:
        if (pair := both(name)) and (a := _num(pair[0])) is not None \
                and (b := _num(pair[1])) is not None and abs(b - a) > LENGTH_TOLERANCE_M:
            moved.append(f"{name[5:8].replace('_', '-')} from {_num_text(a)} to {_num_text(b)} m")
    if moved:
        out.append(("length_change", "warning",
                    "The measured cable length changed on pair " + "; pair ".join(moved) + "."))
    poe = []
    if (pair := both("poe_class")) and (a := _num(pair[0])) is not None \
            and (b := _num(pair[1])) is not None and b < a:
        poe.append(f"The PoE class dropped from {_num_text(a)} to {_num_text(b)}.")
    if (pair := both("poe_load_w")) and (a := _num(pair[0])) is not None \
            and (b := _num(pair[1])) is not None and a > 0 and b < a * POE_LOAD_DROP_RATIO:
        poe.append(f"The PoE load dropped from {_num_text(a)} to {_num_text(b)} W.")
    if poe:
        out.append(("poe_drop", "warning", " ".join(poe)))
    vlans = []
    for name, word in (("vlan", "VLAN"), ("voice_vlan", "voice VLAN")):
        if (pair := both(name)) and _num(pair[0]) is not None and _num(pair[1]) is not None \
                and pair[0] != pair[1]:
            vlans.append(f"The {word} changed from {pair[0]} to {pair[1]}.")
    if vlans:
        out.append(("vlan_change", "info", " ".join(vlans)))
    if (pair := both("dhcp_ok")) and pair[0] is True and pair[1] is False:
        out.append(("dhcp_fail", "warning", "DHCP worked in the earlier field test and failed "
                    "in the newest one."))
    if (pair := both("cable_verdict")) and pair[0] in VERDICT_RANK and pair[1] in VERDICT_RANK \
            and VERDICT_RANK[pair[1]] > VERDICT_RANK[pair[0]]:
        out.append(("verdict_worse", "warning", f"The cable verdict got worse, from "
                    f"{pair[0]} to {pair[1]}."))
    return out
