"""The host page and Control restyle from docs/GUI-DESIGN.md section 3.3 and slice S6."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "observe" / "static"
BANNED = r"\.innerHTML\s*=|outerHTML|insertAdjacentHTML|document\.write|eval\("


def _read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def test_host_page_markup_loads_styles_in_order_and_modules():
    html = _read("host.html")
    order = ['href="/static/css/tokens.css"', 'href="/static/css/base.css"',
             'href="/static/css/shell.css"', 'href="/static/css/components.css"',
             'href="/static/css/host.css"']
    pos = [html.find(o) for o in order]
    assert all(p >= 0 for p in pos) and pos == sorted(pos)
    for ident in ("summary", "shell-nav", "page", "control", "footer"):
        assert f'id="{ident}"' in html, ident
    assert html.count('type="module"') == 3 and "<script src=" not in html
    assert 'id="shell-nav"' in html and html.index("<main") < html.index('id="control"')


def test_host_scripts_are_modules_that_write_only_text():
    for rel in ("host.js", "host-control.js"):
        js = _read(rel)
        assert "import " in js and not re.search(BANNED, js), rel
        assert "pill" not in js, rel
    assert 'import { statusChip } from "/static/js/chips.js"' in _read("host.js")
    ctl = _read("host-control.js")
    for needle in ("typedConfirm", "confirmDialog", "toast(", "statusChip"):
        assert needle in ctl, needle
    assert 'role", "alert"' in ctl and "Reboot host" in ctl


def test_host_css_uses_tokens_only_and_files_use_lf():
    css = _read("css/host.css")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", css)
    for rel in ("observe/static/css/host.css", "tests/test_ui_host.py"):
        assert bytes([13]) not in (ROOT / rel).read_bytes(), rel
