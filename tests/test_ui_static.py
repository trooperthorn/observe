"""The static guard from docs/GUI-DESIGN.md section 4.2.

Every HTML and JavaScript file the console serves is scanned for the constructs the CSP and the
textContent-only rendering rule forbid, and every page route is asserted to carry the CSP header.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute

from observe.alerts import Alerter
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

ROOT = Path(__file__).parent.parent
STATIC_ROOTS = [ROOT / "observe" / "static"] + sorted(
    (ROOT / "plugins").glob("*/*/static"))
PAGE_ROOTS = [ROOT / "observe" / "static"] + sorted((ROOT / "plugins").glob("*/*/pages"))
HTML = sorted(f for r in PAGE_ROOTS for f in r.rglob("*.html"))
JS = sorted(f for r in STATIC_ROOTS for f in r.rglob("*.js"))
CSS = sorted(f for r in STATIC_ROOTS for f in r.rglob("*.css"))

CSP_PARTS = ("default-src 'self'", "object-src 'none'", "base-uri 'none'",
             "frame-ancestors 'none'", "form-action 'none'")

INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc\s*=)", re.I)
STYLE_ELEMENT = re.compile(r"<style\b", re.I)
STYLE_ATTR = re.compile(r"<[a-z][^>]*\sstyle\s*=", re.I)
EVENT_ATTR = re.compile(r"<[a-z][^>]*\son[a-z]+\s*=", re.I)
JS_BANNED = {
    "innerHTML": re.compile(r"\binnerHTML\b"),
    "outerHTML": re.compile(r"\bouterHTML\b"),
    "insertAdjacentHTML": re.compile(r"\binsertAdjacentHTML\b"),
    "document.write": re.compile(r"\bdocument\s*\.\s*write(ln)?\b"),
    "eval": re.compile(r"\beval\s*\("),
    "new Function": re.compile(r"\bnew\s+Function\b"),
    "string setTimeout/setInterval": re.compile(r"\bset(Timeout|Interval)\s*\(\s*['\"`]"),
    "element.style": re.compile(r"\.style\b"),
    "setAttribute style": re.compile(r"setAttribute\s*\(\s*['\"]style['\"]"),
}
URL = re.compile(r"(?:https?:)?//[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|https?://", re.I)
SVG_NS = "http://www.w3.org/2000/svg"


def _strip_js_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)(^|\s)//.*$", r"\1", text)


def _strip_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def _ids(files: list[Path]) -> list[str]:
    return [str(f.relative_to(ROOT)).replace("\\", "/") for f in files]


def test_the_scan_finds_the_files_it_is_meant_to_scan():
    names = _ids(HTML) + _ids(JS)
    assert "observe/static/index.html" in names
    assert "observe/static/app.js" in names
    assert any("pockethernet/pages/report.html" in n for n in names)
    assert any("pockethernet/static/pockethernet.js" in n for n in names)


@pytest.mark.parametrize("path", HTML, ids=_ids(HTML))
def test_html_has_no_inline_script_style_or_event_handlers(path: Path):
    text = _strip_html_comments(path.read_text(encoding="utf-8"))
    assert not INLINE_SCRIPT.search(text), "inline <script>"
    assert not STYLE_ELEMENT.search(text), "<style> element"
    assert not STYLE_ATTR.search(text), "style= attribute"
    assert not EVENT_ATTR.search(text), "on*= attribute"
    assert not URL.search(text), "off-origin URL"


@pytest.mark.parametrize("path", JS, ids=_ids(JS))
def test_js_has_no_dom_injection_eval_or_off_origin_urls(path: Path):
    text = _strip_js_comments(path.read_text(encoding="utf-8"))
    for label, pattern in JS_BANNED.items():
        assert not pattern.search(text), label
    assert not URL.search(text.replace(SVG_NS, "")), "off-origin URL"


@pytest.mark.parametrize("path", CSS, ids=_ids(CSS))
def test_css_has_no_off_origin_urls_or_imports(path: Path):
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    assert not URL.search(text), "off-origin URL"
    assert "@import" not in text


def test_the_scanner_catches_each_violation():
    bad_html = ['<script>x()</script>', '<style>a{}</style>', '<p style="x">',
                '<a onclick="x()">', '<img src="https://cdn.example.com/a.png">']
    pats = [INLINE_SCRIPT, STYLE_ELEMENT, STYLE_ATTR, EVENT_ATTR, URL]
    for sample, pat in zip(bad_html, pats):
        assert pat.search(sample), sample
    assert not INLINE_SCRIPT.search('<script type="module" src="/static/a.js"></script>')
    for sample in ["a.innerHTML = x", "a.outerHTML=x", "a.insertAdjacentHTML('x', y)",
                   "document.write(x)", "eval(x)", "new Function('x')",
                   "setTimeout('x()', 1)", "a.style.color = 'x'",
                   "a.setAttribute('style', 'x')"]:
        assert any(p.search(sample) for p in JS_BANNED.values()), sample
    assert "innerHTML" not in _strip_js_comments("// innerHTML\n/* eval( */ ok()")
    assert not any(p.search("setTimeout(() => go(), 5)") for p in JS_BANNED.values())


def _app(tmp_path: Any) -> Any:
    path = str(tmp_path / "w.db")
    store = Store(path)
    cfg = make_config([{"name": "r", "type": "ping", "host": "10.0.0.2"}],
                      server={"db_path": path})
    return create_app(cfg, store, Scheduler(cfg, store, Alerter(cfg)), Alerter(cfg))


def test_every_page_route_carries_the_csp_header(tmp_path):
    app = _app(tmp_path)
    pages = sorted(
        r.path for r in app.routes
        if isinstance(r, APIRoute) and "GET" in r.methods and "{" not in r.path
        and not r.path.startswith("/api/") and r.path != "/metrics")
    assert {"/", "/login", "/host", "/map", "/port", "/admin", "/admin/infra", "/audit"} <= set(pages)
    client = TestClient(app, base_url="https://testserver")
    for path in pages + ["/static/app.js", "/no-such-page"]:
        resp = client.get(path, follow_redirects=False)
        csp = resp.headers.get("Content-Security-Policy", "")
        for part in CSP_PARTS:
            assert part in csp, f"{path} ({resp.status_code}) lacks {part}"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff", path


def test_plugin_pages_and_static_files_carry_the_csp_header(tmp_path):
    from importlib.metadata import EntryPoint

    from observe.plugins import GROUP, load_plugins

    path = str(tmp_path / "p.db")
    cfg = make_config([{"name": "r", "type": "ping", "host": "127.0.0.1"}],
                      plugins=["pockethernet"], plugin_settings={"pockethernet": {}},
                      server={"db_path": path})
    loaded = load_plugins(cfg, lambda: [EntryPoint(
        "pockethernet", "observe_pockethernet:plugin", GROUP)])
    store = Store(path, loaded)
    app = create_app(cfg, store, Scheduler(cfg, store, Alerter(cfg)), Alerter(cfg),
                     plugins=loaded)
    client = TestClient(app, base_url="https://testserver")
    paths = ["/plugins/pockethernet", "/plugins/pockethernet/report",
             "/plugins/pockethernet/jack", "/plugins/pockethernet/static/pockethernet.js"]
    for p in paths:
        resp = client.get(p, follow_redirects=False)
        csp = resp.headers.get("Content-Security-Policy", "")
        for part in CSP_PARTS:
            assert part in csp, f"{p} ({resp.status_code}) lacks {part}"
