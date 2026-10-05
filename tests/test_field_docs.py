"""Documentation checks for the report pages and field change findings: the docs name every
kind, route and page, and the files of this slice keep the project's writing rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from watchpost.infra_changes import LENGTH_TOLERANCE_M
from .test_infra_changes import FIRES

ROOT = Path(__file__).parent.parent
PLUGIN = ROOT / "plugins" / "pockethernet" / "watchpost_pockethernet"
KINDS = sorted({k for k, *_ in FIRES})
DOCS = {name: (ROOT / name).read_text(encoding="utf-8")
        for name in ("README.md", "THREAT-MODEL.md", "docs/ARCHITECTURE.md",
                     "docs/FIELD-DATA.md")}
ROUTES = ("/plugins/pockethernet", "/plugins/pockethernet/report", "/plugins/pockethernet/jack",
          "/api/plugins/pockethernet/reports")
OWNED = [ROOT / "watchpost" / "infra_changes.py", PLUGIN / "pages.py",
         PLUGIN / "static" / "pockethernet.js", *sorted((PLUGIN / "pages").glob("*.html"))]


def test_every_finding_kind_is_documented_in_the_spec_and_architecture():
    assert len(KINDS) == 7
    for kind in KINDS:
        for name in ("docs/FIELD-DATA.md", "docs/ARCHITECTURE.md"):
            assert f"`{kind}`" in DOCS[name], (kind, name)


def test_spec_marks_the_pages_and_findings_built_and_names_the_routes():
    spec = DOCS["docs/FIELD-DATA.md"]
    assert "5. Report and jack pages (built)." in spec
    assert "6. Findings on the dashboard (built)." in spec
    assert "Not built yet.** The pages" not in spec
    for route in ROUTES:
        assert route in spec, route
    assert f"more than {LENGTH_TOLERANCE_M:.0f} m" in spec


def test_readme_and_threat_model_cover_plugin_pages_and_field_findings():
    assert "/plugins/pockethernet" in DOCS["README.md"]
    assert "never send alerts" in DOCS["README.md"]
    threat = DOCS["THREAT-MODEL.md"]
    assert "| Pockethernet pages and field change findings | built |" in threat
    for phrase in ("textContent", "never call an alert target", "login session", "location"):
        assert phrase in threat, phrase


@pytest.mark.parametrize("name", sorted(DOCS))
def test_docs_use_lf_line_endings(name):
    assert b"\r" not in (ROOT / name).read_bytes()


def test_docs_added_no_em_dash_for_this_slice():
    for name, text in DOCS.items():
        for line in text.splitlines():
            if "field change" in line.lower() or "pockethernet pages" in line.lower():
                assert "\u2014" not in line, name


@pytest.mark.parametrize("path", OWNED, ids=lambda p: p.name)
def test_new_files_use_lf_and_no_em_dashes_or_model_names(path):
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    assert b"\r" not in raw and "\u2014" not in text
    assert not any(w in text.lower() for w in ("claude", "opus", "sonnet", "haiku"))
