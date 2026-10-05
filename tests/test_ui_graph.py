"""Checks for the S9 graph engine: js/graph/force.js, render.js, view.js and css/graph.css.

The JavaScript cannot run here, so these tests read the sources and mirror the numbers that
docs/GUI-DESIGN.md section 2.10 fixes. tests/js/graph.test.mjs holds the layout tests
(determinism, anchors, fixed nodes, picking) for `node --test tests/js` in CI.
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
GRAPH = STATIC / "js" / "graph"
FILES = ["types.js", "force.js", "render.js", "view.js"]
TOKENS = (STATIC / "css" / "tokens.css").read_text(encoding="utf-8")
GRAPH_CSS = (STATIC / "css" / "graph.css").read_text(encoding="utf-8")
HEADER = ("// Ported from trooperthorn/relationship-maps, packages/graph-core (commit 0c4d268), "
          "via ha_Int_soc (MIT). No d3.")


def _raw(name: str) -> str:
    return (GRAPH / name).read_text(encoding="utf-8")


def _code(name: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", _raw(name), flags=re.S)
    return re.sub(r"(?m)(^|\s)//.*$", r"\1", text)


# ---- the numbers, mirrored from force.js ---------------------------------------------------

def tick_count(n: int) -> int:
    return 0 if n <= 0 else min(360, 60000 // n)


def node_radius(degree: int, anchor: bool, scale: float = 1.0) -> float:
    return (8 if anchor else 5) + degree ** 0.5 * (4.2 if anchor else 3.2) * scale


def test_tick_budget_follows_the_spec():
    assert [tick_count(n) for n in (1, 100, 166, 200, 300)] == [360, 360, 360, 300, 200]
    assert tick_count(0) == 0
    code = _code("force.js")
    assert "MAX_TICKS = 360" in code and "TICK_BUDGET = 60000" in code
    assert "Math.min(MAX_TICKS, Math.floor(TICK_BUDGET / n))" in code
    assert "FORCE_NODE_LIMIT = 300" in code
    # 200 nodes stay inside the Pi 3 budget of 300 ticks, and 300 nodes cost the same work as 200.
    assert tick_count(200) * 200 <= 60000 and tick_count(300) * 300 <= 60000


def test_radius_and_force_constants_match_the_spec():
    assert node_radius(0, True) == 8 and node_radius(0, False) == 5
    code = _code("force.js")
    assert "(anchor ? 8 : 5) + Math.sqrt(degree) * (anchor ? 4.2 : 3.2)" in code
    for const in ("LINK_DISTANCE = 90", "LINK_STRENGTH = 0.35", "CHARGE = -220",
                  "COLLIDE_PAD = 12", "VELOCITY_DECAY = 0.4"):
        assert const in code
    assert "n.anchor ? 0.35 : 0.06" in code
    assert "Math.min(width, height) * 0.34" in code


def test_the_layout_is_deterministic_by_construction():
    code = _code("force.js")
    assert "Math.random" not in code and "Date.now" not in code and "performance" not in code
    assert "Math.cos(a) * ring" in code  # the fixed starting ring
    assert "fx" in code and "fixed" in code  # pinned nodes for a one-switch re-layout


# ---- sources --------------------------------------------------------------------------------

@pytest.mark.parametrize("name", FILES)
def test_each_file_carries_the_attribution_header(name: str):
    assert _raw(name).splitlines()[0] == HEADER


@pytest.mark.parametrize("name", FILES)
def test_no_d3_no_framework_and_only_relative_imports(name: str):
    code = _code(name)
    assert not re.search(r'\bd3\b|d3-|from\s+["\']lit|xterm|\bhass\b|callWS', code, re.I)
    for target in re.findall(r"from\s+[\"']([^\"']+)[\"']", code):
        assert target.startswith("./"), target
    assert "require(" not in code and "import(" not in code


def test_render_reads_colours_from_tokens_at_paint_time():
    code = _code("render.js")
    assert "getComputedStyle" in code
    names = set(re.findall(r"\"(--[a-z0-9-]+)\"", code))
    names = {n for n in names if not n.endswith("-")} | {f"--cat-{i}" for i in range(1, 9)}
    assert {"--g-bg", "--g-node-stroke", "--g-label-bg", "--g-label-fg", "--g-dim",
            "--o-text-muted", "--cat-other", "--o-up", "--o-down", "--o-pending"} <= names
    for name in names:
        assert f"{name}:" in TOKENS, name
    # Hex and rgb literals appear only in the FALLBACK block, used when a token cannot be read.
    outside = re.sub(r"const FALLBACK = \{.*?\};", "", code, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", outside)


def test_state_is_never_shown_by_colour_alone():
    code = _code("render.js")
    for state in ("up", "down", "warn", "serious", "unreach"):
        assert f'case "{state}"' in code
    assert "State ring" in _raw("render.js") and "drawGlyph(ctx, p, theme, camera)" in code
    assert "setLineDash(l.stale" in code  # stale links are dashed, not just dim


def test_label_and_hit_test_rules_are_ported_without_a_quadtree():
    code = _code("render.js")
    assert "MAX_LABELS = 55" in code
    assert "collides(" in code and "buildGrid" in code and "quadtree" not in code.lower()
    assert "Math.min(1, Math.min(w / (maxX - minX), h / (maxY - minY)) * 0.94)" in code


def test_view_keeps_a_weak_client_idle_and_accessible():
    code = _code("view.js")
    assert "MAX_DPR = 2" in code and "DRAG_FRAME_MS = 33" in code
    assert "requestAnimationFrame" in code and "cancelAnimationFrame" in code
    assert "setInterval" not in code and "layoutForce" not in code  # no loop, no layout on paint
    assert "passive: false" in code and "ResizeObserver" in code
    assert 'prefers-color-scheme: dark' in code and "data-theme" in code
    assert '"role", "img"' in code and "aria-label" in code and "tabIndex = 0" in code
    for key in ('"ArrowRight"', '"ArrowLeft"', '"Enter"', '"+"', '"-"', '"0"'):
        assert key in code
    assert "destroy()" in code and "removeEventListener(\"wheel\"" in code


def test_graph_css_uses_only_tokens():
    css = re.sub(r"/\*.*?\*/", "", GRAPH_CSS, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsl", css)
    for name in set(re.findall(r"var\((--[a-z0-9-]+)\)", css)):
        assert f"{name}:" in TOKENS, name
    assert ".graph-wrap canvas" in css and "focus-visible" in css


def test_only_the_map_page_loads_the_graph():
    for page in list(STATIC.glob("*.html")) + list((STATIC / "pages").glob("*.js")):
        text = page.read_text(encoding="utf-8")
        uses = "js/graph/" in text or "graph.css" in text
        assert uses == (page.name in ("map.html", "map.js")), page.name


def test_the_graph_files_are_served_with_the_csp_header(tmp_path):
    path = str(tmp_path / "g.db")
    cfg = make_config([{"name": "r", "type": "ping", "host": "127.0.0.1"}],
                      server={"db_path": path})
    store = Store(path)
    app = create_app(cfg, store, Scheduler(cfg, store, Alerter(cfg)), Alerter(cfg))
    client = TestClient(app, base_url="https://testserver")
    for rel in ("js/graph/force.js", "js/graph/render.js", "js/graph/view.js",
                "js/graph/types.js", "css/graph.css"):
        resp = client.get("/static/" + rel, follow_redirects=False)
        assert resp.status_code == 200, rel
        assert "default-src 'self'" in resp.headers.get("Content-Security-Policy", ""), rel
        assert resp.text == (STATIC / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_the_new_files_use_lf_line_endings():
    carriage_return = bytes([13])
    for rel in ["observe/static/js/graph/" + n for n in FILES] + [
            "observe/static/css/graph.css", "tests/js/graph.test.mjs", "tests/test_ui_graph.py"]:
        assert carriage_return not in (ROOT / rel).read_bytes(), rel


def test_map_page_has_the_view_toggle_canvas_and_side_card():
    html = (STATIC / "map.html").read_text(encoding="utf-8")
    for view in ("graph", "tiers", "table"):
        assert f'data-view="{view}"' in html
    assert 'id="graphcanvas"' in html and 'id="selected"' in html and 'id="linkstable"' in html
    js = (STATIC / "pages" / "map.js").read_text(encoding="utf-8")
    assert "viewFromHash" in js and "createGraphView" in js and "layoutForce" in js
    assert "role" in (STATIC / "js" / "graph" / "view.js").read_text(encoding="utf-8")
    infra = (STATIC / "js" / "graph" / "infra.js").read_text(encoding="utf-8")
    assert 'narrow || nodeCount > FORCE_NODE_LIMIT ? "tiers" : "graph"' in infra
    assert "d3" not in infra
