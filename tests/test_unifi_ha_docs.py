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
