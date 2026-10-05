"""Checks for the S4 component modules: chips, tables, dialogs, toasts and components.css.

The JavaScript cannot run here, so these tests read the sources. The sort and paging rules are
mirrored in Python; tests/js/table-core.test.mjs holds the same cases for `node --test` in CI.
"""

from __future__ import annotations

import re
from functools import cmp_to_key
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
JS = STATIC / "js"
TOKENS = (STATIC / "css" / "tokens.css").read_text(encoding="utf-8")
COMPONENTS = (STATIC / "css" / "components.css").read_text(encoding="utf-8")
CHIP_STATES = (JS / "chip-states.js").read_text(encoding="utf-8")
NEW_FILES = ["css/components.css", "js/chip-states.js", "js/chips.js", "js/table-core.js",
             "js/table.js", "js/dialog-logic.js", "js/dialog.js", "js/toast.js"]


def _strip_css(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


# ---- the sort rules, mirrored from table-core.js -----------------------------------------

def sort_rows(rows, get, direction):
    if direction not in ("asc", "desc"):
        return list(rows)
    sign = 1 if direction == "asc" else -1
    keyed = [(i, r, get(r)) for i, r in enumerate(rows)]

    def cmp(x, y):
        xn, yn = x[2] is None, y[2] is None
        if xn or yn:
            return 0 if xn == yn else (1 if xn else -1)
        return sign * ((x[2] > y[2]) - (x[2] < y[2]))

    keyed.sort(key=cmp_to_key(lambda x, y: cmp(x, y) or (x[0] - y[0])))
    return [r for _, r, _ in keyed]


def next_sort(current, key):
    if current is None or current[0] != key:
        return (key, "asc")
    return (key, "desc") if current[1] == "asc" else None


ROWS = [("b", 2), ("a", None), ("c", 1), ("d", 2)]


def test_sort_is_stable_and_nulls_sink_both_ways():
    def names(direction):
        return [r[0] for r in sort_rows(ROWS, lambda r: r[1], direction)]

    assert names("asc") == ["c", "b", "d", "a"]
    assert names("desc") == ["b", "d", "c", "a"]
    assert names(None) == ["b", "a", "c", "d"]


def test_next_sort_cycles_and_the_js_declares_the_same_cycle():
    s = next_sort(None, "a")
    assert s == ("a", "asc")
    s = next_sort(s, "a")
    assert s == ("a", "desc")
    assert next_sort(s, "a") is None
    assert next_sort(s, "b") == ("b", "asc")
    core = (JS / "table-core.js").read_text(encoding="utf-8")
    assert 'current.dir === "asc" ? { key, dir: "desc" } : null' in core
    assert "x.i - y.i" in core  # stable tie break
    assert "PAGE_SIZES = [10, 25, 100]" in core


# ---- chips ---------------------------------------------------------------------------------

def _states() -> dict[str, tuple[str, str, str]]:
    block = CHIP_STATES.split("export const STATES = {")[1].split("};")[0]
    return {m[0]: (m[1], m[2], m[3]) for m in re.findall(
        r'(\w+): \{ role: "(\w+)", icon: "(\w+)", word: "([^"]+)" \}', block)}


def _icons() -> set[str]:
    block = CHIP_STATES.split("export const ICONS = {")[1].split("\n};")[0]
    return set(re.findall(r"^  (\w+): \[", block, flags=re.M))


def test_every_state_has_a_word_a_known_icon_and_a_token_role():
    states = _states()
    assert {"up", "warn", "serious", "down", "unreachable", "pending", "stale", "unavailable",
            "absent", "not_reported"} <= set(states)
    icons = _icons()
    for name, (role, icon, word) in states.items():
        assert word.strip(), name
        assert icon in icons, name
        for suffix in ("", "-bg"):
            assert f"--o-{role}{suffix}:" in TOKENS, (name, role)


def test_icon_shapes_are_not_shared_between_roles():
    seen: dict[str, str] = {}
    for role, icon, _ in _states().values():
        assert seen.setdefault(icon, role) == role, f"{icon} shared by roles"


def test_components_css_styles_every_role_for_chips_tiles_and_toasts():
    css = _strip_css(COMPONENTS)
    for role in {r for r, _, _ in _states().values()}:
        for part in (f".chip.s-{role}", f".tile.s-{role}", f".toast.s-{role}"):
            assert part in css, part
        assert f"var(--o-{role})" in css and f"var(--o-{role}-bg)" in css


# ---- stylesheet rules ----------------------------------------------------------------------

def test_components_css_uses_only_tokens_for_colour():
    css = _strip_css(COMPONENTS)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not re.search(r"\b(?:rgb|rgba|hsl|hsla)\(", css)
    assert not re.search(r"(?m)^\s*--[a-z-]+\s*:", css)
    for name in set(re.findall(r"var\((--[\w-]+)", css)):
        assert re.search(rf"{re.escape(name)}\s*:", TOKENS), name


def test_components_css_keeps_tables_contained_and_motion_optional():
    css = _strip_css(COMPONENTS)
    assert re.search(r"\.table-wrap\s*\{[^}]*overflow-x:\s*auto", css)
    assert "prefers-reduced-motion: reduce" in css
    assert "dialog.dlg::backdrop" in css
    assert not re.search(r"(?<![-\w])color\s*:\s*var\(--(?:o-dot-warn|warn)\)", css)


# ---- JavaScript rules ----------------------------------------------------------------------

def _js_code(name: str) -> str:
    text = (JS / name).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)(^|\s)//.*$", r"\1", text)


PILL_NO_TEXT = re.compile(r'el\(\s*"[^"]*\bpill\b[^"]*"\s*(?:,\s*(?:null|undefined))?\s*\)')
PILL_CLASS_ONLY = re.compile(r'el\(\s*"[^"]+"\s*,\s*[`"][^`"]*\bpill\b[^`"]*[`"]\s*\)')


def test_the_pill_check_catches_a_textless_pill():
    assert PILL_CLASS_ONLY.search('el("span", "pill s-up")')
    assert PILL_CLASS_ONLY.search('el("span", `pill s-${x}`)')
    assert not PILL_CLASS_ONLY.search('el("span", "pill s-up", "Up")')


@pytest.mark.parametrize("path", sorted(JS.glob("*.js")) + sorted((STATIC / "pages").glob("*.js")),
                         ids=lambda p: p.name)
def test_no_pill_element_is_built_without_text(path: Path):
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    assert not PILL_NO_TEXT.search(text) and not PILL_CLASS_ONLY.search(text)


def test_every_chip_is_built_with_an_icon_and_a_text_child():
    code = _js_code("chips.js")
    assert 'chip.append(statusIcon(info.icon), el("span", null, text || info.word))' in code
    assert '"aria-hidden": "true"' in code
    assert "createElement" not in code  # only el() and svg() from dom.js


def test_modules_use_the_dom_helpers_and_absolute_same_origin_imports():
    for name in ("chips.js", "table.js", "dialog.js", "toast.js"):
        code = _js_code(name)
        assert 'from "/static/js/dom.js"' in code, name
        for target in re.findall(r'from "([^"]+)"', code):
            assert target.startswith("/static/js/"), (name, target)
    assert "import" not in _js_code("table-core.js")
    assert "import" not in _js_code("chip-states.js")
    assert "import" not in _js_code("dialog-logic.js")


def test_table_renders_nodes_only_and_labels_sort_state():
    code = _js_code("table.js")
    assert "instanceof Node" in code and "td.textContent" in code
    assert 'setAttribute("aria-sort"' in code and '"aria-hidden", "true"' in code
    assert "table-wrap" in code and "Showing ${view.rows.length} of ${view.total}" in code
    assert "Rows per page" in code


def test_dialog_uses_native_dialog_returns_focus_and_gates_on_typed_name():
    code = _js_code("dialog.js")
    assert "showModal()" in code and "opener.focus()" in code
    assert "typedMatches(input.value, typedName)" in code
    assert "export function confirmDialog" in code and "export function typedConfirm" in code
    assert "name.length > 0 && typed === name" in _js_code("dialog-logic.js")


def test_toasts_use_status_and_alert_regions_and_errors_persist():
    code = _js_code("toast.js")
    assert '"role", kind === "error" ? "alert" : "status"' in code
    assert 'setAttribute("aria-live", "polite")' in code
    assert "DISMISS_MS = 6000" in code
    # Only the non-error branch schedules removal.
    assert code.index("setTimeout(remove") > code.index("} else {")
    assert "localStorage" not in code


# ---- serving and line endings --------------------------------------------------------------

def test_new_files_are_served_with_the_right_content_type(tmp_path: Path):
    path = str(tmp_path / "c.db")
    store = Store(path)
    cfg = make_config([{"name": "r", "type": "ping", "host": "10.0.0.2"}],
                      server={"db_path": path})
    app = create_app(cfg, store, Scheduler(cfg, store, Alerter(cfg)), Alerter(cfg))
    client = TestClient(app, base_url="https://testserver")
    for rel in NEW_FILES:
        r = client.get(f"/static/{rel}")
        assert r.status_code == 200, rel
        expect = "text/css" if rel.endswith(".css") else "javascript"
        assert expect in r.headers["content-type"], rel
        assert "Content-Security-Policy" in r.headers, rel


def test_new_files_use_lf_line_endings():
    for rel in [f"observe/static/{n}" for n in NEW_FILES] + [
            "tests/test_ui_components.py", "tests/js/table-core.test.mjs"]:
        assert bytes([13]) not in (ROOT / rel).read_bytes(), rel
