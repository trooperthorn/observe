"""Pure normalisation of switch and port identity for the infrastructure map.

A port is known by many spellings: Cisco prints GigabitEthernet1/0/5 or Gi1/0/5, SNMP
ifName may differ from the CLI, Juniper writes ge-0/0/5.0, UniFi calls it "Port 5", Linux
uses eth0 or enp3s0, and LLDP carries the port id with a subtype that says how to read it.
port_key() maps every spelling of one port to one key and keeps different ports apart:
Gi1/0/5 and Gi1/0/50 differ, Gi1/0/5 and Te1/0/5 differ, and eth0 and eth0.100 differ. A
name that is not recognised is only lower-cased and stripped of whitespace, never guessed at.

Nothing here touches the database or the network, so it is cheap to test with a table.
"""

from __future__ import annotations

import re

KEY_MAX = 96

# Long and short interface type names mapped to one short form.
_TYPES = {
    "gigabitethernet": "gi", "gigeth": "gi", "gig": "gi", "gi": "gi",
    "tengigabitethernet": "te", "tengige": "te", "ten": "te", "te": "te",
    "twentyfivegige": "twe", "twe": "twe",
    "fortygigabitethernet": "fo", "fortygige": "fo", "fo": "fo",
    "hundredgige": "hu", "hu": "hu",
    "fastethernet": "fa", "fa": "fa",
    "ethernet": "eth", "eth": "eth", "et": "eth",
    "port-channel": "po", "portchannel": "po", "po": "po",
    "loopback": "lo", "lo": "lo",
    "vlan": "vlan", "vl": "vlan",
    "management": "mgmt", "mgmt": "mgmt",
}
_JUNIPER = re.compile(r"^(ge|xe|et|fe|xle|me)-(\d+/\d+/\d+)(:\d+)?(?:\.(\d+))?$")
_GENERIC = re.compile(r"^([a-z][a-z-]*?)(\d.*)$")
_HEX12 = re.compile(r"^[0-9a-f]{12}$")

LLDP_SUBTYPES = {
    1: "interface_alias", 2: "port_component", 3: "mac_address", 4: "network_address",
    5: "interface_name", 6: "agent_circuit_id", 7: "local",
}
_SUBTYPE_NAMES = {v: k for k, v in LLDP_SUBTYPES.items()}


def _clean(text: str, what: str, keep_spaces: bool = False) -> str:
    if not isinstance(text, str):
        raise ValueError(f"{what} must be text")
    if any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in text):
        raise ValueError(f"{what} contains control characters")
    cleaned = (" ".join(text.split()) if keep_spaces else "".join(text.split())).lower()
    if not cleaned:
        raise ValueError(f"{what} is empty")
    return cleaned


def mac_digits(text: str) -> str | None:
    """The 12 lower-case hex digits of a MAC in any common spelling, else None."""
    digits = re.sub(r"[:.\-\s]", "", text).lower()
    return digits if _HEX12.match(digits) else None


def port_key(name: str) -> str:
    """The normalised key for an interface name written in any of the common styles."""
    text = _clean(name, "port name")
    j = _JUNIPER.match(text)
    if j:
        base, unit = f"{j.group(1)}-{j.group(2)}{j.group(3) or ''}", j.group(4)
        key = base if unit in (None, "0") else f"{base}.{unit}"
    else:
        g = _GENERIC.match(text)
        if g:
            key = _TYPES.get(g.group(1), g.group(1)) + g.group(2)
        else:
            key = text
    if len(key) > KEY_MAX:
        raise ValueError("port name is too long")
    if "|" in key:
        raise ValueError("port name may not contain a vertical bar")
    return key


def lldp_port_key(subtype: int | str, value: str) -> str:
    """The port key for an LLDP port id. The subtype says how to read the value: names and
    aliases are normalised like any interface name; a MAC, network address, circuit id or
    port component keeps a prefix so it can never collide with an interface name."""
    code = _SUBTYPE_NAMES.get(subtype) if isinstance(subtype, str) else subtype
    if code not in LLDP_SUBTYPES:
        raise ValueError("unknown LLDP port id subtype")
    if code in (1, 5, 7):
        return port_key(value)
    if code == 3:
        digits = mac_digits(value)
        if digits is None:
            raise ValueError("LLDP port id is not a MAC address")
        return f"mac:{digits}"
    if code == 6:
        hexed = re.sub(r"[:\s\-]", "", _clean(value, "LLDP port id"))
        return f"circuit:{hexed}"
    prefix = "addr" if code == 4 else "pc"
    return f"{prefix}:{_clean(value, 'LLDP port id')}"


def switch_id(chassis_mac: str | None = None, sys_name: str | None = None) -> str:
    """Switch identity: the LLDP chassis MAC when known, else the lower-cased sysName."""
    if chassis_mac:
        digits = mac_digits(chassis_mac)
        if digits is None:
            raise ValueError("chassis id is not a MAC address")
        return f"mac:{digits}"
    if sys_name:
        name = _clean(sys_name, "sysName", keep_spaces=True)
        if "|" in name:
            raise ValueError("sysName may not contain a vertical bar")
        return f"name:{name}"
    raise ValueError("a chassis MAC or a sysName is required")
