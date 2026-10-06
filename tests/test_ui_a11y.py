"""Console fixes for copy on plain HTTP, wizard focus and announcements, and the map refresh.

The scripts cannot run here, so these read the served page and the sources. tests/js/graph.test.mjs
and tests/js/wizard.test.mjs run the pure functions under `node --test tests/js` in CI. The
contrast of text input borders is checked in tests/test_ui_tokens.py and the fan header rule in
tests/test_fan_header_rule.py."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .test_auth import Env

STATIC = Path(__file__).parent.parent / "observe" / "static"
STEPS = ["host", "agent", "allowlist", "install", "live"]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def _read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def _copy_text() -> str:
    js = _read("js/admin-ui.js")
    start = js.index("export async function copyText")
    return js[start:js.index("\n}\n", start)]


# ---- copy without a secure context ---------------------------------------------------------

def test_copy_falls_back_to_selecting_the_text_when_the_clipboard_is_missing():
    body = _copy_text()
    # The missing clipboard is detected before it is called, so reading .writeText cannot throw.
    assert "!navigator.clipboard" in body and 'typeof navigator.clipboard.writeText !== "function"' in body
    assert body.index("!navigator.clipboard") < body.index("navigator.clipboard.writeText(text)")
    assert "selectText(field)" in body and "field.select()" not in body
    assert 'SELECTED_MESSAGE = "Selected, press Ctrl+C to copy."' in _read("js/admin-ui.js")
    assert "toast(field ? SELECTED_MESSAGE" in body


def test_selecting_works_for_a_pre_as_well_as_an_input():
    js = _read("js/admin-ui.js")
    helper = js[js.index("export function selectText"):js.index("export async function copyText")]
    assert 'typeof node.select === "function"' in helper  # input and textarea
    assert "range.selectNodeContents(node)" in helper and "sel.addRange(range)" in helper  # pre


def test_the_wizard_and_settings_pages_pass_the_command_block_to_copy():
    assert 'copyText(created.command, $("cmd"))' in _read("hosts-new.js")
    assert 'copyText(shown.made.command, $("cmd"))' in _read("host-settings.js")
    for page in ("hosts-new.html", "host-settings.html"):
        assert '<pre class="cmd" id="cmd" tabindex="0"></pre>' in _read(page), page


def test_copy_message_never_carries_the_command():
    body = _copy_text()
    for call in re.findall(r"toast\([^;]*\);", body):
        assert not re.search(r"text(?![^\"]*\")", call), call


# ---- wizard focus and announcement ---------------------------------------------------------

def test_the_wizard_page_has_focusable_step_headings_and_a_live_region(env):
    env.user("root", admin=True)
    assert env.login("root").status_code == 200
    html = env.client.get("/hosts/new").text
    assert re.search(r'<p id="step-announce" class="sr-only" role="status" aria-live="polite"></p>', html)
    for step in STEPS:
        assert re.search(rf'<h3 id="step-{step}-h" tabindex="-1">', html), step


def test_a_step_change_moves_focus_to_the_heading_and_announces_the_step():
    js = _read("hosts-new.js")
    body = js[js.index("function show(step, moveFocus)"):js.index("// The command's own state")]
    assert 'stepAnnouncement(step)' in body and '$("step-announce").textContent' in body
    assert "$(`step-${step}-h`).focus()" in body and "if (moveFocus)" in body
    # Only a change the person made moves focus; the first draw of the page does not.
    assert "show(step, true)" in js and "show(stepFromHash(window.location.hash, created), true)" in js
    assert re.search(r"\n  show\(step\);\n\}\)\(\);", js)


def test_step_announcement_names_match_the_headings():
    logic = _read("js/wizard-logic.js")
    html = _read("hosts-new.html")
    names = dict(re.findall(r'(\w+): "([^"]+)"', logic[logic.index("STEP_NAMES"):logic.index("export function stepAnnouncement")]))
    assert list(names) == STEPS
    for i, step in enumerate(STEPS, 1):
        assert f'id="step-{step}-h" tabindex="-1">Step {i}. {names[step]}' in html


# ---- map refresh keeps the view ------------------------------------------------------------

def test_the_map_keeps_its_layout_and_camera_when_the_graph_keeps_its_shape():
    page = _read("pages/map.js")
    assert "structureKey(graph.entities, graph.relations, graph.anchors)" in page
    assert "mergeLayout(layout, graph.entities, graph.relations)" in page
    assert "{ sameStructure: same }" in page
    view = _read("js/graph/view.js")
    assert "export function mergeLayout" in view and "export function cameraAfterRefresh" in view
    assert "cameraAfterRefresh(st.camera, fitCamera(layout, st.w, st.h)" in view
    # Every way the person moves the view marks it, and Fit clears the mark.
    assert view.count("st.touched = true") == 3 and "st.touched = false" in view
