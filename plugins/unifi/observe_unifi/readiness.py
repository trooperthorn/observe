"""Why a wireless client may be unable to join an SSID.

Ported from ha_Int_soc custom_components/ha_soc/unifi_wifi.py (MIT, same owner) onto the classic
`rest/wlanconf` row the plugin stores in `unifi_wlans`. No UniFi source records an association
attempt or an authentication failure; a client that cannot join is simply absent from every
connected-client view. What the configuration does say is whether a join is permitted at all,
and that is what each finding states, with its own certainty: "this SSID is turned off" and
"this SSID may be off right now on a schedule" are not the same claim.

Every field read here (enabled, mac_filter, ap_group_mode, band, security, hidden, scheduled) is
unverified against a live console, so a finding names the condition and the API field, never a
client.
"""

from __future__ import annotations

from typing import Any

SEVERITY_BLOCKING = "blocking"  # No client can join while this holds.
SEVERITY_POSSIBLE = "possible"  # Refuses some devices, or some of the time.
SEVERITY_UNKNOWN = "unknown"  # The data does not say enough.

NOTHING_REFUSES = "Nothing in this SSID's configuration refuses a client."


def _finding(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def ssid_findings(w: dict[str, Any]) -> list[dict[str, str]]:
    """Plain statements about what this SSID's stored configuration refuses."""
    out: list[dict[str, str]] = []
    if w.get("enabled") is False:
        out.append(_finding("ssid_disabled", SEVERITY_BLOCKING,
                            "This SSID is turned off. No client can join it."))
    mac_filter = (w.get("mac_filter") or "").lower()
    if mac_filter == "allow":
        out.append(_finding("mac_allow_list", SEVERITY_BLOCKING,
                            "MAC filtering allows only listed addresses. Any device not on "
                            "that list is refused."))
    elif mac_filter in ("deny", "on"):
        out.append(_finding("mac_block_list", SEVERITY_POSSIBLE,
                            "MAC filtering blocks listed addresses. A device on that list is "
                            "refused."))
    mode = (w.get("ap_group_mode") or "").lower()
    if mode and mode != "all":
        names = w.get("ap_names") or []
        if names:
            out.append(_finding("ap_restricted", SEVERITY_POSSIBLE,
                                f"Broadcast by {len(names)} access points: {', '.join(names)}. "
                                "A client out of range of those cannot see this network, "
                                "wherever else it roams."))
        else:
            out.append(_finding("ap_restricted_by_group", SEVERITY_UNKNOWN,
                                f"Broadcast is restricted to access point groups (mode {mode}). "
                                "The group list is not read, so which access points carry "
                                "this SSID cannot be determined here."))
    band = (w.get("band") or "").lower()
    if band and band not in ("both", "2g") and "2" not in band:
        out.append(_finding("no_2ghz", SEVERITY_BLOCKING,
                            f"Broadcast only on the {band} band. A 2.4 GHz-only device, which "
                            "most IoT hardware is, cannot join."))
    security = (w.get("security") or "").lower()
    if "wpa3" in security and "wpa2" not in security:
        out.append(_finding("wpa3_only", SEVERITY_BLOCKING,
                            f"Security is {security}. A device without WPA3 support cannot "
                            "associate."))
    elif security == "open":
        out.append(_finding("open_security", SEVERITY_POSSIBLE,
                            "This SSID is open, with no passphrase."))
    if w.get("hidden") is True:
        out.append(_finding("hidden_ssid", SEVERITY_POSSIBLE,
                            "The name is hidden. Devices that only join broadcast networks, "
                            "and onboarding flows that scan for the SSID, will not find it."))
    if w.get("scheduled") is True:
        out.append(_finding("blackout_schedule", SEVERITY_UNKNOWN,
                            "A schedule limits when this SSID is on. Whether it is in force at "
                            "this moment is not evaluated here."))
    return out


def summary_of(findings: list[dict[str, str]]) -> str:
    """One plain sentence for the card: nothing refuses a client, or the refusing conditions
    by severity."""
    if not findings:
        return NOTHING_REFUSES
    blocking = [f for f in findings if f["severity"] == SEVERITY_BLOCKING]
    possible = [f for f in findings if f["severity"] == SEVERITY_POSSIBLE]
    unknown = [f for f in findings if f["severity"] == SEVERITY_UNKNOWN]
    parts = []
    if blocking:
        parts.append(f"{len(blocking)} condition{'s' if len(blocking) > 1 else ''} refuse"
                     f"{'' if len(blocking) > 1 else 's'} every client")
    if possible:
        parts.append(f"{len(possible)} refuse{'' if len(possible) > 1 else 's'} some devices")
    if unknown:
        parts.append(f"{len(unknown)} cannot be judged from the data")
    return "; ".join(parts) + "."
