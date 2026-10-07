"""Customise the dashboard (docs/GUI-DESIGN.md sections 2.11 and 5, slice S14): the per-user layout
API, the markup and modules, the pure rules mirrored in Python (tests/js/tiles.test.mjs holds the
same cases for `node --test` in CI) and the line endings of the new files."""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from observe import layout
from observe.storage.schema import MIGRATIONS, SCHEMA_VERSION
from observe.store import Store

from .test_auth import Env
from .dbq import run_sql

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "observe" / "static"
BANNED = r"\.innerHTML\s*=|outerHTML|insertAdjacentHTML|document\.write|eval\("
URL = "/api/ui/layout/dashboard"
NEW_FILES = ["js/tiles.js", "js/tiles-logic.js"]
JSON_TYPE = {"Content-Type": "application/json"}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def login(env, name):
    env.user(name)
    return env.csrf(env.login(name))


def relogin(env, name):
    env.client.cookies.clear()
    return env.csrf(env.login(name))


def test_schema_has_the_layout_table(env):
    assert SCHEMA_VERSION == max(MIGRATIONS) and 13 in MIGRATIONS
    assert env.rows("SELECT name FROM sqlite_master WHERE name='ui_layouts'")


def test_default_is_empty_and_unsaved(env):
    login(env, "alice")
    r = env.client.get(URL)
    assert r.status_code == 200
    assert r.json() == {"view": "dashboard", "order": [], "hidden": [], "saved": False}
    assert r.headers["cache-control"] == "no-store"


def test_save_and_read_back_follows_the_user_between_sessions(env):
    hdr = login(env, "alice")
    body = {"order": ["events", "group:core", "capacity"], "hidden": ["capacity"]}
    r = env.client.put(URL, json=body, headers=hdr)
    assert r.status_code == 200 and r.json()["saved"] is True
    hdr = relogin(env, "alice")  # a second device is a second session
    got = env.client.get(URL).json()
    assert got["order"] == body["order"] and got["hidden"] == ["capacity"] and got["saved"] is True
    env.client.put(URL, json={"order": ["findings"], "hidden": []}, headers=hdr)
    assert env.client.get(URL).json()["order"] == ["findings"]
    assert len(env.rows("SELECT 1 FROM ui_layouts")) == 1


def test_layouts_are_isolated_per_user(env):
    hdr = login(env, "alice")
    env.client.put(URL, json={"order": ["events"], "hidden": ["findings"]}, headers=hdr)
    login(env, "bob")
    assert env.client.get(URL).json()["order"] == []
    hdr = relogin(env, "bob")
    env.client.put(URL, json={"order": ["capacity"], "hidden": []}, headers=hdr)
    relogin(env, "alice")
    got = env.client.get(URL).json()
    assert got["order"] == ["events"] and got["hidden"] == ["findings"]
    assert len(env.rows("SELECT 1 FROM ui_layouts")) == 2


def test_stale_and_malformed_ids_are_dropped_and_repeats_removed(env):
    hdr = login(env, "alice")
    body = {"order": ["group:core", "bogus", 7, None, "group:", "group:core", "events",
                      "group:" + "x" * 300, "capacity"],
            "hidden": ["nope", "events", "events"]}
    r = env.client.put(URL, json=body, headers=hdr)
    assert r.json()["order"] == ["group:core", "events", "capacity"]
    assert r.json()["hidden"] == ["events"]
    assert env.client.get(URL).json()["order"] == ["group:core", "events", "capacity"]


def test_an_old_row_with_bad_ids_is_cleaned_on_read(env):
    login(env, "alice")
    (uid,) = env.rows("SELECT id FROM users")[0]
    run_sql(env.store, "INSERT INTO ui_layouts VALUES (?, 'dashboard', ?, 0)",
                    (uid, '{"order": ["events", "junk", "events"], "hidden": 5}'))
    got = env.client.get(URL).json()
    assert got["order"] == ["events"] and got["hidden"] == []


def test_unknown_view_is_rejected_on_every_method(env):
    hdr = login(env, "alice")
    for call in (lambda: env.client.get("/api/ui/layout/hosts"),
                 lambda: env.client.put("/api/ui/layout/hosts", json={"order": []}, headers=hdr),
                 lambda: env.client.delete("/api/ui/layout/hosts", headers=hdr)):
        r = call()
        assert r.status_code == 404 and r.json()["detail"] == "unknown view"
    assert not env.rows("SELECT 1 FROM ui_layouts")


def test_size_caps(env):
    hdr = login(env, "alice")
    many = [f"group:g{i}" for i in range(layout.MAX_TILES + 1)]
    assert env.client.put(URL, json={"order": many, "hidden": []}, headers=hdr).status_code == 413
    exact = many[:layout.MAX_TILES]
    assert env.client.put(URL, json={"order": exact, "hidden": []}, headers=hdr).status_code == 200
    big = '{"order": [], "hidden": [], "pad": "' + "x" * layout.MAX_BODY + '"}'
    r = env.client.put(URL, content=big, headers={**hdr, **JSON_TYPE})
    assert r.status_code == 413
    assert len(env.client.get(URL).json()["order"]) == layout.MAX_TILES


@pytest.mark.parametrize("body", [None, [], "x", {}, {"order": "events"}, {"order": [], "hidden": "a"}])
def test_bad_bodies_are_422(env, body):
    hdr = login(env, "alice")
    assert env.client.put(URL, json=body, headers=hdr).status_code == 422
    assert not env.rows("SELECT 1 FROM ui_layouts")


def test_not_json_is_422(env):
    hdr = login(env, "alice")
    r = env.client.put(URL, content="{nope", headers={**hdr, **JSON_TYPE})
    assert r.status_code == 422


def test_login_csrf_and_basic_auth_rules(env):
    assert env.client.get(URL).status_code == 401
    assert env.client.get(URL, auth=("ui", "uipass")).status_code == 401  # a session, never basic
    hdr = login(env, "alice")
    ok = {"order": ["events"], "hidden": []}
    assert env.client.put(URL, json=ok).status_code == 403
    assert env.client.put(URL, json=ok, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert env.client.delete(URL).status_code == 403
    assert not env.rows("SELECT 1 FROM ui_layouts")
    assert env.client.put(URL, json=ok, headers=hdr).status_code == 200


def test_reset_returns_to_the_declared_order(env):
    hdr = login(env, "alice")
    env.client.put(URL, json={"order": ["events"], "hidden": ["capacity"]}, headers=hdr)
    r = env.client.delete(URL, headers=hdr)
    assert r.status_code == 200 and r.json()["saved"] is False
    assert env.client.get(URL).json() == {"view": "dashboard", "order": [], "hidden": [],
                                          "saved": False}


def test_a_viewer_can_customise(env):
    hdr = login(env, "viewer")  # not an admin
    assert env.client.put(URL, json={"order": ["events"], "hidden": []},
                          headers=hdr).status_code == 200


def test_the_dashboard_page_has_the_controls_and_tile_markers():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for i in ("customize", "customize-tools", "customize-reset", "customize-msg"):
        assert f'id="{i}"' in html, i
    for t in ("capacity", "findings", "events"):
        assert re.search(rf'id="{t}-panel" data-tile="{t}"', html), t
    assert 'id="customize" class="btn" type="button" aria-pressed="false"' in html
    assert 'role="status"' in html


def test_modules_render_text_only_and_the_app_wires_them_in():
    for rel in NEW_FILES + ["app.js"]:
        js = (STATIC / rel).read_text(encoding="utf-8")
        assert not re.search(BANNED, js), rel
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'from "/static/js/tiles.js"' in app and "card.dataset.tile = groupTile(name)" in app
    tiles = (STATIC / "js" / "tiles.js").read_text(encoding="utf-8")
    assert "/api/ui/layout/dashboard" in tiles
    assert "draggable" not in tiles and "dragstart" not in tiles  # no drag and drop at first
    logic = (STATIC / "js" / "tiles-logic.js").read_text(encoding="utf-8")
    assert "ha_Int_soc" in tiles and "ha_Int_soc" in logic


def stored_bytes(rel: str) -> bytes:
    """The bytes git stores for a tracked file; the working copy may carry CRLF from autocrlf."""
    try:
        return subprocess.run(["git", "-C", str(ROOT), "cat-file", "blob", f":{rel}"],
                              capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return (ROOT / rel).read_bytes()


def test_new_files_are_served_with_lf_endings(env):
    for rel in NEW_FILES:
        r = env.client.get(f"/static/{rel}")
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]
        assert bytes([13]) not in stored_bytes(f"observe/static/{rel}"), rel
    for rel in ("observe/layout.py", "tests/test_ui_customise.py", "tests/js/tiles.test.mjs"):
        assert bytes([13]) not in stored_bytes(rel), rel


def test_the_dashboard_still_loads_without_a_session_and_tiles_never_redirects(env):
    # A basic-auth viewer has no session: the page loads and the layout API says 401, which
    # tiles.js handles by reading with redirect: false (it must not use whoami, which redirects).
    assert env.client.get(URL).status_code == 401
    tiles = (STATIC / "js" / "tiles.js").read_text(encoding="utf-8")
    assert "whoami" not in tiles and '"/api/v2/session", null, { redirect: false }' in tiles
    assert "button.disabled = true" in tiles  # Customize is off until the saved layout has loaded
    assert "window.confirm" in tiles


def test_a_version_12_database_migrates_to_13(tmp_path):
    path = str(tmp_path / "v12.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.commit()
    from observe.storage.schema import migrate
    newer = {v: m for v, m in MIGRATIONS.items() if v > 12}
    assert newer
    for v in newer:
        del MIGRATIONS[v]
    try:
        migrate(db)
    finally:
        MIGRATIONS.update(newer)
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 12
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='ui_layouts'").fetchall()
    db.close()
    Store(path).close()
    db = sqlite3.connect(path)
    try:
        assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == max(MIGRATIONS)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ui_layouts'").fetchall()
    finally:
        db.close()


# The same rules as tests/js/tiles.test.mjs, in Python, so they run without node.
def effective_order(declared, saved):
    known = set(declared)
    out = []
    for i in saved if isinstance(saved, list) else []:
        if i in known and i not in out:
            out.append(i)
    return out + [d for d in declared if d not in out]


def move(order, i, delta):
    k = order.index(i) if i in order else -1
    j = k + delta
    if k < 0 or j < 0 or j >= len(order):
        return list(order)
    out = list(order)
    out[k], out[j] = out[j], out[k]
    return out


def test_effective_order_rules():
    declared = ["group:core", "group:lab", "capacity", "findings", "events"]
    assert effective_order(declared, ["events", "group:gone", "group:core", "events"]) == [
        "events", "group:core", "group:lab", "capacity", "findings"]
    assert effective_order(declared, None) == declared
    assert move(["a", "b", "c"], "b", -1) == ["b", "a", "c"]
    assert move(["a", "b", "c"], "a", -1) == ["a", "b", "c"]
    assert move(["a", "b", "c"], "x", 1) == ["a", "b", "c"]


def test_valid_tile_shapes():
    assert layout.valid_tile("events") and layout.valid_tile("group:core")
    assert not layout.valid_tile("group:") and not layout.valid_tile("group")
    assert not layout.valid_tile(5) and not layout.valid_tile("group:a\nb")


def test_migration_step_can_run_twice(tmp_path):
    path = str(tmp_path / "x.db")
    Store(path).close()
    db = sqlite3.connect(path)
    for stmt in MIGRATIONS[13]:
        db.execute(stmt)
    db.close()
