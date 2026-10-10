"""Documentation checks for the UniFi and Home Assistant monitoring slice: the writing rules hold
and every setting of the UniFi plugin is documented."""

from __future__ import annotations

import re

import pytest

from observe_unifi import UniFiSettings

from .test_field_docs import DOCS, ROOT, stored_bytes

FILES = ["README.md", "THREAT-MODEL.md", "docs/ARCHITECTURE.md", "docs/FIELD-DATA.md"]


@pytest.mark.parametrize("name", FILES)
def test_no_em_dashes_and_lf_line_endings(name):
    raw = stored_bytes(ROOT / name)
    assert b"\r" not in raw, name
    assert "—" not in raw.decode("utf-8"), name


def test_every_unifi_plugin_setting_is_documented_in_the_readme():
    readme = DOCS["README.md"]
    for key in UniFiSettings.model_fields:
        assert f"| `{key}` |" in readme, key


def test_setup_notes_cover_every_credential_and_the_threat_rows_exist():
    readme = DOCS["README.md"]
    for needle in ("UniFi Integration API key (view only)", "Dedicated local UniFi account",
                   "Home Assistant token (non-admin)", "HA SOC Probe SNMPv3 credential",
                   "HA SOC push key"):
        assert needle in readme, needle
    threat = DOCS["THREAT-MODEL.md"]
    for row in ("Classic UniFi controller credential", "Client privacy", "HA SOC push key"):
        assert re.search(rf"^\| {row} \|", threat, re.M), row
    assert "Which UniFi API each port field comes from" in DOCS["docs/FIELD-DATA.md"]


NETWORK_ROUTES = ("/api/v2/unifi/overview", "/api/v2/unifi/wlans", "/api/v2/unifi/absent-clients")
CLASSIC_FIELDS = ("rx_bytes-r", "wired-rx_bytes-r", "network_table", "rest/wlanconf", "x_passphrase",
                  "statistics/latest", "ap_group_mode", "wlan_band", "hide_ssid", "mac_filter_policy")


def test_the_network_view_routes_tables_and_page_sections_are_documented():
    readme = " ".join(DOCS["README.md"].split())  # the prose wraps; compare on one line
    for word in ("unifi_site_status", "unifi_wlans", "`overview`", "`wlans`", "`absent-clients`",
                 "Wi-Fi join", "clients per SSID", "25, 50 or 100 per page", "`rest/wlanconf`",
                 "five read views", "bits per second"):
        assert word in readme, word
    design = (ROOT / "docs" / "DATA-API-DESIGN.md").read_text(encoding="utf-8")
    for route in NETWORK_ROUTES:
        assert route in design, route
    assert "`vlan`, `ssid`" in design
    assert "/unifi/overview" in DOCS["docs/ARCHITECTURE.md"]
    threat = DOCS["THREAT-MODEL.md"]
    assert "`rest/wlanconf`" in threat and "x_passphrase" in threat and "unifi_wlans" in threat


def test_field_data_marks_every_network_view_field_verified_or_not():
    spec = DOCS["docs/FIELD-DATA.md"]
    assert "UniFi network view: which field is verified" in spec
    for field in CLASSIC_FIELDS:
        assert field in spec, field
    table = spec.split("UniFi network view: which field is verified")[1].split("The HA SOC \"Integration\"")[0]
    rows = [r for r in table.splitlines() if r.startswith("  | ") and "---" not in r and "| Field |" not in r]
    assert len(rows) >= 12
    for row in rows:
        assert any(w in row.lower() for w in ("verified", "no verified source", "not read")), row
    # The HA core verified fields keep their qualified status; the rest are unverified.
    assert "`ap_mac`" in table and "Verified for Home Assistant core" in table
    assert "`first_seen`, `last_seen`" in table and "core's `clients_all`" in table
    assert "IPv6" in table and "no IPv6 column" in table
    assert "Integration\" column" in spec and "dropped" in spec
