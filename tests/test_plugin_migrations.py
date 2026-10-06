"""Plugin migrations, config sections, pages and static files."""

from __future__ import annotations

import sqlite3
import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from observe.alerts import Alerter
from observe.plugins import (LoadedPlugins, Migration, NavEntry, PluginBase, PluginError,
                               PluginPage, load_plugins)
from observe.scheduler import Scheduler
from observe.storage.schema import (MIGRATIONS, PluginSchemaTooNewError, SchemaTooNewError,
                                    migrate_plugins)
from observe.store import Store
from observe.web import create_app

from .conftest import make_config
from .test_auth import Env
from .test_plugins import config, ep, installed

V1 = Migration(1, ("CREATE TABLE IF NOT EXISTS echo_things (id INTEGER)",))
V2 = Migration(2, ("ALTER TABLE echo_things ADD COLUMN label TEXT",))


def loaded_echo(**extra: Any) -> LoadedPlugins:
    return load_plugins(config(plugins=["echo"], **extra), installed(ep("echo", "echo_plugin")))


def rows(path: str, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    db = sqlite3.connect(path)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def columns(path: str, table: str) -> list[str]:
    return [r[1] for r in rows(path, f"PRAGMA table_info({table})")]


def test_fresh_database_gets_core_then_plugin_schema(tmp_path):
    path = str(tmp_path / "w.db")
    Store(path, loaded_echo()).close()
    assert rows(path, "SELECT plugin, version FROM plugin_schema") == [("echo", 1)]
    assert columns(path, "echo_things") == ["id"]
    assert rows(path, "SELECT MAX(version) FROM schema_version") == [(max(MIGRATIONS),)]


def test_upgrade_applies_only_the_missing_steps(tmp_path):
    path = str(tmp_path / "w.db")
    Store(path, loaded_echo()).close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO echo_things (id) VALUES (7)")
    db.commit()
    migrate_plugins(db, {"echo": [V1, V2]})
    migrate_plugins(db, {"echo": [V1, V2]})  # a rerun changes nothing
    db.close()
    assert rows(path, "SELECT version FROM plugin_schema WHERE plugin='echo'") == [(2,)]
    assert columns(path, "echo_things") == ["id", "label"]
    assert rows(path, "SELECT id, label FROM echo_things") == [(7, None)]


def test_failed_step_rolls_back_and_keeps_the_old_version(tmp_path):
    path = str(tmp_path / "w.db")
    Store(path, loaded_echo()).close()
    bad = Migration(2, ("ALTER TABLE echo_things ADD COLUMN a TEXT", "NOT SQL"))
    db = sqlite3.connect(path)
    with pytest.raises(sqlite3.OperationalError):
        migrate_plugins(db, {"echo": [V1, bad]})
    db.close()
    assert rows(path, "SELECT version FROM plugin_schema WHERE plugin='echo'") == [(1,)]
    assert columns(path, "echo_things") == ["id"]


def test_plugin_database_newer_than_the_code_is_refused(tmp_path):
    path = str(tmp_path / "w.db")
    Store(path, loaded_echo()).close()
    db = sqlite3.connect(path)
    db.execute("UPDATE plugin_schema SET version=5 WHERE plugin='echo'")
    db.commit()
    db.close()
    with pytest.raises(PluginSchemaTooNewError, match="'echo'.*5.*newer"):
        Store(path, loaded_echo())
    assert issubclass(PluginSchemaTooNewError, SchemaTooNewError)
    # A newer plugin database is untouched, and without the plugin the store still opens.
    assert rows(path, "SELECT version FROM plugin_schema WHERE plugin='echo'") == [(5,)]
    Store(path).close()


def test_newer_plugin_refuses_before_any_other_plugin_changes(tmp_path):
    path = str(tmp_path / "w.db")
    db = sqlite3.connect(path)
    migrate_plugins(db, {"a": [V1]})
    db.execute("UPDATE plugin_schema SET version=9 WHERE plugin='a'")
    db.commit()
    other = Migration(1, ("CREATE TABLE b_things (id INTEGER)",))
    with pytest.raises(PluginSchemaTooNewError):
        migrate_plugins(db, {"b": [other], "a": [V1]})
    db.close()
    assert "b_things" not in {r[0] for r in rows(path, "SELECT name FROM sqlite_master")}


def test_disabling_a_plugin_keeps_its_tables_untouched(tmp_path):
    path = str(tmp_path / "w.db")
    Store(path, loaded_echo()).close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO echo_things (id) VALUES (42)")
    db.commit()
    db.close()
    Store(path).close()  # restarted with the plugin no longer listed
    assert rows(path, "SELECT id FROM echo_things") == [(42,)]
    assert rows(path, "SELECT plugin, version FROM plugin_schema") == [("echo", 1)]
    Store(path, loaded_echo()).close()  # and enabling it again finds everything as it was
    assert rows(path, "SELECT id FROM echo_things") == [(42,)]


def test_bad_plugin_config_fails_validation_without_echoing_the_value():
    with pytest.raises(PluginError) as err:
        loaded_echo(plugin_settings={"echo": {"greeting": ["s3cret-value"]}})
    text = str(err.value)
    assert "'echo'" in text and "plugin_settings.echo" in text and "greeting" in text
    assert "s3cret-value" not in text
    with pytest.raises(PluginError, match="nope"):
        loaded_echo(plugin_settings={"echo": {"nope": 1}})


def test_settings_sections_are_per_plugin_key():
    ok = loaded_echo(plugin_settings={"echo": {"greeting": "yo"}})
    assert ok.get("echo").settings.greeting == "yo"
    with pytest.raises(ValueError, match="not listed"):
        make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                    plugin_settings={"other": {}})


class PagesEnv(Env):
    def __init__(self, tmp_path, enabled: bool) -> None:
        super().__init__(tmp_path)
        plugins = loaded_echo() if enabled else LoadedPlugins()
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        self.client.close()
        self.client = TestClient(create_app(self.cfg, self.store, sched, alerter,
                                            auth_clock=self.clock, plugins=plugins),
                                 base_url="https://testserver")


@pytest.fixture(params=[True, False], ids=["enabled", "disabled"])
def penv(request, tmp_path):
    e = PagesEnv(tmp_path, request.param)
    e.enabled = request.param
    yield e
    e.client.close()
    e.store.close()


def test_nav_and_pages_exist_only_when_the_plugin_is_enabled(penv):
    penv.user("alice")
    penv.user("root", admin=True)
    penv.login("alice")
    nav = penv.client.get("/api/plugins").json()["nav"]
    page = penv.client.get("/plugins/echo")
    static = penv.client.get("/plugins/echo/static/echo.js")
    if penv.enabled:
        assert [n["label"] for n in nav] == ["Echo"]
        assert page.status_code == 200 and "Echo plugin" in page.text
        assert static.status_code == 200
        assert penv.client.get("/plugins/echo/a").status_code == 403  # admin page, plain user
        penv.client.cookies.clear()
        penv.login("root")
        assert [n["label"] for n in penv.client.get("/api/plugins").json()["nav"]] \
            == ["Echo", "Echo admin"]
        assert penv.client.get("/plugins/echo/a").status_code == 200
    else:
        assert nav == []
        assert page.status_code == 404 and static.status_code == 404


def test_plugin_pages_need_a_login_and_carry_the_core_headers(tmp_path):
    e = PagesEnv(tmp_path, True)
    try:
        assert e.client.get("/plugins/echo").status_code == 401
        e.user("alice")
        e.login("alice")
        for url in ("/plugins/echo", "/plugins/echo/static/echo.js"):
            h = e.client.get(url).headers
            assert "default-src 'self'" in h["content-security-policy"]
            assert h["x-content-type-options"] == "nosniff"
            assert h["cache-control"] == "no-store"
        assert e.client.get("/plugins/echo/static/../../../etc/passwd").status_code in (400, 404)
        assert e.client.get("/plugins/echo/static/missing.js").status_code == 404
    finally:
        e.client.close()
        e.store.close()


def _load_bad(**hooks: Any) -> None:
    class Bad(PluginBase):
        name = "bad"
        core_versions = ">=1"

    for k, v in hooks.items():
        setattr(Bad, k, v)
    mod = type(sys)("tests.fakes.bad_plugin")
    mod.plugin = Bad()
    sys.modules[mod.__name__] = mod
    try:
        load_plugins(config(plugins=["bad"]), installed(ep("bad", "bad_plugin")))
    finally:
        del sys.modules[mod.__name__]


@pytest.mark.parametrize("page_path", ["/", "/admin", "/plugins/other", "/plugins/bad/static",
                                       "/plugins/bad/static/x", "/plugins/bad/../x",
                                       "/plugins/bad/", "/plugins/badger"])
def test_pages_outside_the_plugin_prefix_are_refused(page_path, tmp_path):
    f = tmp_path / "p.html"
    f.write_text("<p>x</p>\n")
    with pytest.raises(PluginError, match="page path"):
        _load_bad(pages=lambda self: [PluginPage(page_path, f)])


def test_missing_page_file_static_dir_and_duplicate_page_are_refused(tmp_path):
    f = tmp_path / "p.html"
    f.write_text("<p>x</p>\n")
    with pytest.raises(PluginError, match="does not exist"):
        _load_bad(pages=lambda self: [PluginPage("/plugins/bad", tmp_path / "nope.html")])
    with pytest.raises(PluginError, match="not a folder"):
        _load_bad(static_dir=lambda self: tmp_path / "nope")
    with pytest.raises(PluginError, match="declared twice"):
        _load_bad(pages=lambda self: [PluginPage("/plugins/bad", f),
                                      PluginPage("/plugins/bad", f)])
    _load_bad(pages=lambda self: [PluginPage("/plugins/bad/x", f)],
              nav_entries=lambda self: [NavEntry("Bad", "/plugins/bad/x")])
