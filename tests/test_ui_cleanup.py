"""Slice S15 of docs/GUI-DESIGN.md: no legacy styles remain, titles and setup docs are current."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "observe" / "static"
PAGES = sorted(STATIC.glob("*.html")) + sorted((ROOT / "plugins").glob("*/*/pages/*.html"))
SHEETS = sorted((STATIC / "css").glob("*.css")) + sorted((ROOT / "plugins").glob("*/*/static/*.css"))
LEGACY_VAR = re.compile(r"var\(--(?:bg|card|fg|muted|line|up|warn|down|pending|unreach)\)")


def test_app_css_is_gone_and_no_page_links_it():
    assert not (STATIC / "app.css").exists()
    assert len(PAGES) >= 13
    for page in PAGES:
        assert "app.css" not in page.read_text(encoding="utf-8"), page.name


def test_no_legacy_variable_is_used_or_defined():
    for sheet in SHEETS:
        text = sheet.read_text(encoding="utf-8")
        assert not LEGACY_VAR.search(text), sheet.name
        assert not re.search(r"(?m)^\s*--(?:bg|card|fg|muted|line|up|warn|down|pending|unreach)\s*:",
                             text), sheet.name
    for js in STATIC.rglob("*.js"):
        assert not LEGACY_VAR.search(js.read_text(encoding="utf-8")), js.name


def test_every_title_ends_with_observe():
    for page in PAGES:
        m = re.search(r"<title>(.*?)</title>", page.read_text(encoding="utf-8"))
        assert m and m.group(1).endswith(" - Observe"), page.name


def test_login_page_uses_its_own_stylesheet():
    html = (STATIC / "login.html").read_text(encoding="utf-8")
    assert 'href="/static/css/login.css"' in html and (STATIC / "css" / "login.css").exists()


def test_readme_setup_points_at_the_add_host_wizard():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "/hosts/new" in readme
