"""The colour tokens, base styles and theme module from docs/GUI-DESIGN.md sections 2.2 and 4.3.

tokens.css is parsed directly. The contrast check uses the WCAG 2.x relative luminance formula
in pure Python, so no browser is needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from observe.alerts import Alerter
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "observe" / "static"
TOKENS = (STATIC / "css" / "tokens.css").read_text(encoding="utf-8")

STATUS = ["up", "warn", "serious", "down", "pending", "unreach"]
# Tokens every theme must define. The categorical and graph tokens that stay the same in dark
# mode (cat-3 is overridden, cat-4 and cat-6 are not) are only required in the light block.
THEMED = (["page", "surface", "surface-subtle", "border", "text", "text-muted", "accent",
           "accent-tint", "accent-line", "focus", "border-input"]
          + STATUS + [f"{s}-bg" for s in STATUS])
THEMED = [f"--o-{n}" for n in THEMED] + [
    "--cat-1", "--cat-2", "--cat-3", "--cat-4", "--cat-5", "--cat-7", "--cat-8", "--cat-other",
    "--g-bg", "--g-node-stroke", "--g-label-bg", "--g-label-fg"]
LIGHT_ONLY = [f"--o-{n}" for n in (
    "radius", "radius-sm", "radius-pill", "s1", "s2", "s3", "s4", "s5", "s6", "maxw", "font",
    "mono", "fs-xs", "fs-sm", "fs-base", "fs-title", "fs-h", "fs-kpi", "dot-warn", "ink", "dur")] + [
    "--cat-6", "--g-dim"]

def _block(text: str, opener: str) -> str:
    """The body of the first rule whose selector line starts with opener, brace matched."""
    start = text.index(opener)
    open_at = text.index("{", start)
    depth, i = 0, open_at
    while True:
        depth += text[i] == "{"
        depth -= text[i] == "}"
        if depth == 0:
            return text[open_at + 1:i]
        i += 1


def _decls(body: str) -> dict[str, str]:
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)}


# The light block is the first :root rule. The auto dark block sits inside the media query.
LIGHT = _decls(_block(TOKENS, ":root {"))
_media = _block(TOKENS, "@media (prefers-color-scheme: dark)")
AUTO_DARK = _decls(_block(_media, ':root:not([data-theme="light"])'))
MANUAL_DARK = _decls(_block(TOKENS, ':root[data-theme="dark"]'))


def test_every_required_token_is_in_light_and_both_dark_blocks():
    for name in THEMED + LIGHT_ONLY:
        assert name in LIGHT, f"light block lacks {name}"
    for name in THEMED:
        assert name in AUTO_DARK, f"system dark block lacks {name}"
        assert name in MANUAL_DARK, f"manual dark block lacks {name}"


def test_the_two_dark_blocks_are_identical():
    assert AUTO_DARK == MANUAL_DARK


def test_dark_blocks_only_override_known_tokens():
    assert set(AUTO_DARK) <= set(LIGHT)


def test_manual_light_wins_over_a_dark_system():
    # The system dark block must exclude data-theme="light", so a manual Light choice holds.
    assert ':root:not([data-theme="light"])' in TOKENS


def test_reduced_motion_zeroes_the_transition_token():
    assert re.search(r"prefers-reduced-motion: reduce\)\s*\{\s*:root\s*\{\s*--o-dur:\s*0ms", TOKENS)


def test_stylesheets_hard_code_no_hex_outside_tokens():
    for css in (STATIC / "css").glob("*.css"):
        if css.name == "tokens.css":
            continue
        text = re.sub(r"/\*.*?\*/", "", css.read_text(encoding="utf-8"), flags=re.S)
        assert not re.search(r"#[0-9a-fA-F]{3,8}b", text), css.name


# ---- contrast ---------------------------------------------------------------------------

def _parse(value: str) -> tuple[float, float, float, float]:
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", value)
    assert m, f"unparseable colour {value!r}"
    p = [x.strip() for x in m.group(1).split(",")]
    return (float(p[0]), float(p[1]), float(p[2]), float(p[3]) if len(p) > 3 else 1.0)


def _over(top: str, under: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    r, g, b, a = _parse(top)
    return (r * a + under[0] * (1 - a), g * a + under[1] * (1 - a), b * a + under[2] * (1 - a), 1.0)


def _lum(c: tuple[float, float, float, float]) -> float:
    def lin(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])


def contrast(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


THEMES = {"light": LIGHT, "dark": {**LIGHT, **MANUAL_DARK}}


def test_the_contrast_formula_matches_known_values():
    black, white = (0, 0, 0, 1.0), (255, 255, 255, 1.0)
    assert contrast(black, white) == pytest.approx(21.0)
    assert contrast(white, white) == pytest.approx(1.0)
    assert contrast(_parse("#767676"), white) == pytest.approx(4.54, abs=0.01)


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_text_tokens_reach_4_5_to_1_on_every_surface_they_sit_on(theme: str):
    t = THEMES[theme]
    page, surface = _parse(t["--o-page"]), _parse(t["--o-surface"])
    failures = []
    for token in ["--o-text", "--o-text-muted", "--o-accent"]:
        for bg_name, bg in (("page", page), ("surface", surface)):
            ratio = contrast(_parse(t[token]), bg)
            if ratio < 4.5:
                failures.append(f"{token} on {bg_name}: {ratio:.2f}")
    # Status text sits on the page, on a card, and on its own tint over a card.
    for s in STATUS:
        fg = _parse(t[f"--o-{s}"])
        tint = _over(t[f"--o-{s}-bg"], surface)
        for label, bg in (("page", page), ("surface", surface), ("own tint", tint)):
            ratio = contrast(fg, bg)
            if ratio < 4.5:
                failures.append(f"--o-{s} on {label}: {ratio:.2f}")
    muted_on_tint = contrast(_parse(t["--o-text-muted"]), _over(t["--o-accent-tint"], surface))
    if muted_on_tint < 4.5:
        failures.append(f"text-muted on accent tint: {muted_on_tint:.2f}")
    label = contrast(_parse(t["--g-label-fg"]), _over(t["--g-label-bg"], _parse(t["--g-bg"])))
    if label < 4.5:
        failures.append(f"graph label: {label:.2f}")
    assert not failures, failures


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_status_dots_and_the_focus_ring_reach_3_to_1(theme: str):
    t = THEMES[theme]
    surface, page = _parse(t["--o-surface"]), _parse(t["--o-page"])
    failures = []
    for s in STATUS:
        for label, bg in (("surface", surface), ("page", page)):
            ratio = contrast(_parse(t[f"--o-{s}"]), bg)
            if ratio < 3.0:
                failures.append(f"--o-{s} dot on {label}: {ratio:.2f}")
    for label, bg in (("surface", surface), ("page", page)):
        ratio = contrast(_parse(t["--o-focus"]), bg)
        if ratio < 3.0:
            failures.append(f"--o-focus on {label}: {ratio:.2f}")
    assert not failures, failures


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_text_input_borders_reach_3_to_1_on_the_page_and_the_surface(theme: str):
    t = THEMES[theme]
    border = _parse(t["--o-border-input"])
    for label, bg in (("surface", _parse(t["--o-surface"])), ("page", _parse(t["--o-page"]))):
        ratio = contrast(border, bg)
        assert ratio >= 3.0, f"--o-border-input on {label}: {ratio:.2f}"


def test_every_stylesheet_rule_that_styles_a_field_uses_the_input_border_token():
    """The quiet card border is 1.3:1, so a rule for an input, select or textarea must not use it."""
    failures = []
    for path in sorted((STATIC / "css").glob("*.css")):
        css = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            selector, body = m.group(1), m.group(2)
            if not re.search(r"\b(input|select|textarea)\b", selector):
                continue
            if re.search(r"border(-color)?\s*:[^;]*var\(--o-border\)", body):
                failures.append(f"{path.name}: {selector.strip()}")
    assert not failures, failures


def test_the_amber_fill_is_never_text():
    # --o-dot-warn is a fill for dots only, so it is deliberately not held to a text ratio on
    # white. Its text counterpart --o-warn is checked above. Make sure no stylesheet uses it
    # as a text colour.
    for css in (STATIC / "css").glob("*.css"):
        text = re.sub(r"/\*.*?\*/", "", css.read_text(encoding="utf-8"), flags=re.S)
        for m in re.finditer(r"(?<![-\w])color\s*:\s*var\(--(?:o-dot-warn|warn)\)", text):
            line = text[max(0, text.rfind("\n", 0, m.start())):m.end()]
            if "border" in line or "background" in line:
                continue
            raise AssertionError(line)


# ---- pages and the theme module ---------------------------------------------------------

def _client(tmp_path: Path) -> TestClient:
    path = str(tmp_path / "t.db")
    store = Store(path)
    cfg = make_config([{"name": "r", "type": "ping", "host": "10.0.0.2"}],
                      server={"db_path": path})
    app = create_app(cfg, store, Scheduler(cfg, store, Alerter(cfg)), Alerter(cfg))
    return TestClient(app, base_url="https://testserver")


def test_stylesheets_are_served_as_css_and_ordered_tokens_then_base(tmp_path: Path):
    client = _client(tmp_path)
    for name in ("tokens.css", "base.css"):
        r = client.get(f"/static/css/{name}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
    pages = sorted(STATIC.glob("*.html")) + sorted(
        (ROOT / "plugins").glob("*/*/pages/*.html"))
    assert len(pages) >= 10
    order = ['href="/static/css/tokens.css"', 'href="/static/css/base.css"']
    for page in pages:
        text = page.read_text(encoding="utf-8")
        positions = [text.find(o) for o in order]
        assert all(p >= 0 for p in positions) and positions == sorted(positions), page.name


def test_theme_module_is_served_and_stores_the_choice_safely(tmp_path: Path):
    client = _client(tmp_path)
    r = client.get("/static/js/theme.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    js = r.text
    assert 'THEMES = ["auto", "light", "dark"]' in js
    assert "localStorage" in js and js.count("try {") >= 2 and js.count("catch") >= 2
    assert "dataset.theme" in js and "delete root.dataset.theme" in js
    assert "fetch(" not in js and "document.cookie" not in js
    # The map page loads it first, so the stored theme is applied before the page renders.
    assert 'import "/static/js/theme.js";' in (STATIC / "pages" / "map.js").read_text(
        encoding="utf-8")


def test_new_files_use_lf_line_endings():
    # .gitattributes sets eol=lf, so a CR in the working file would also reach the blob.
    for rel in ["observe/static/css/tokens.css", "observe/static/css/base.css",
                "observe/static/js/theme.js", "tests/test_ui_tokens.py"]:
        assert bytes([13]) not in (ROOT / rel).read_bytes(), rel
