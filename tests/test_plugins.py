"""The plugin host: loading rules, and the core's enforcement on plugin routes."""

from __future__ import annotations

import json
import sys
from importlib.metadata import EntryPoint
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from observe.alerts import Alerter
from observe.plugins import (GROUP, KeyScope, LoadedPlugins, Migration, Plugin, PluginBase,
                               PluginError, PluginRouter, load_plugins)
from observe.plugins import Collector
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config
from .test_auth import BASIC, Clock, Env


def ep(name: str, module: str) -> EntryPoint:
    return EntryPoint(name=name, value=f"tests.fakes.{module}:plugin", group=GROUP)


def installed(*eps: EntryPoint):
    return lambda: list(eps)


def config(**extra: Any):
    return make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}], **extra)


def test_unlisted_installed_plugin_is_not_loaded():
    sys.modules.pop("tests.fakes.boom_plugin", None)
    # boom_plugin raises when imported, so importing it would fail the call.
    loaded = load_plugins(config(plugins=["echo"]),
                          installed(ep("echo", "echo_plugin"), ep("boom", "boom_plugin")))
    assert loaded.names == ["echo"]
    assert "tests.fakes.boom_plugin" not in sys.modules
    assert load_plugins(config(), installed(ep("boom", "boom_plugin"))).names == []
    assert "tests.fakes.boom_plugin" not in sys.modules


def test_listed_missing_plugin_fails_startup_clearly():
    with pytest.raises(PluginError) as err:
        load_plugins(config(plugins=["pockethernet"]), installed(ep("echo", "echo_plugin")))
    assert "'pockethernet'" in str(err.value) and "no installed package" in str(err.value)


def test_version_mismatch_fails_startup_clearly():
    with pytest.raises(PluginError) as err:
        load_plugins(config(plugins=["old"]), installed(ep("old", "old_plugin")),
                     core_version="2026.9.23.6")
    text = str(err.value)
    assert "'old'" in text and ">=1,<2" in text and "2026.9.23.6" in text


def test_core_version_range_includes_and_excludes():
    ok = load_plugins(config(plugins=["echo"]), installed(ep("echo", "echo_plugin")),
                      core_version="2026.9.23.6")
    assert ok.names == ["echo"]
    with pytest.raises(PluginError):
        load_plugins(config(plugins=["echo"]), installed(ep("echo", "echo_plugin")),
                     core_version="2027.1.0")


def test_import_failure_name_clash_and_bad_name_are_errors():
    with pytest.raises(PluginError, match="failed to import"):
        load_plugins(config(plugins=["boom"]), installed(ep("boom", "boom_plugin")))
    with pytest.raises(PluginError, match="more than one package"):
        load_plugins(config(plugins=["echo"]),
                     installed(ep("echo", "echo_plugin"), ep("echo", "old_plugin")))
    with pytest.raises(PluginError, match="must match"):
        load_plugins(config(plugins=["other"]), installed(ep("other", "echo_plugin")))


def test_settings_are_validated_and_passed_to_the_plugin():
    loaded = load_plugins(config(plugins=["echo"], plugin_settings={"echo": {"greeting": "yo"}}),
                          installed(ep("echo", "echo_plugin")))
    assert loaded.get("echo").settings.greeting == "yo"
    with pytest.raises(PluginError, match="invalid settings"):
        load_plugins(config(plugins=["echo"], plugin_settings={"echo": {"nope": 1}}),
                     installed(ep("echo", "echo_plugin")))


def test_config_rejects_settings_for_unlisted_plugin_and_duplicates():
    with pytest.raises(ValueError, match="not listed"):
        config(plugin_settings={"echo": {}})
    with pytest.raises(ValueError, match="more than once"):
        config(plugins=["echo", "echo"])


def test_hooks_are_collected():
    loaded = load_plugins(config(plugins=["echo"]), installed(ep("echo", "echo_plugin")))
    p = loaded.get("echo")
    assert isinstance(p.plugin, Plugin)
    assert [s.marker for s in p.key_scopes] == ["ech"]
    assert [m.version for m in p.migrations] == [1]
    assert "echo.ping" in p.monitor_types
    assert [n["label"] for n in loaded.nav(False)] == ["Echo"]
    assert [n["label"] for n in loaded.nav(True)] == ["Echo", "Echo admin"]


def _router_with_mount() -> APIRouter:
    r = APIRouter()
    r.routes.append(Mount("/m", app=Starlette()))
    return r


def _boom(self: Any) -> list[Any]:
    raise ZeroDivisionError("hook broke")


@pytest.mark.parametrize("hooks,message", [
    ({"key_scopes": lambda self: [KeyScope("wpi")]}, "key scope"),
    ({"migrations": lambda self: [Migration(2, ())]}, "migration versions"),
    ({"monitor_types": lambda self: {"ping": object}}, "monitor type"),
    ({"core_versions": ""}, "core_versions is empty"),
    ({"core_versions": "not a range"}, "not a valid version range"),
    ({"routers": lambda self: [PluginRouter(_router_with_mount())]}, "plain HTTP routes"),
    ({"routers": _boom}, "a hook failed"),
])
def test_malformed_plugins_are_refused(hooks, message):
    class Bad(PluginBase):
        name = "bad"
        core_versions = ">=1"

    for k, v in hooks.items():
        setattr(Bad, k, v)
    mod = type(sys)("tests.fakes.bad_plugin")
    mod.plugin = Bad()
    sys.modules[mod.__name__] = mod
    try:
        with pytest.raises(PluginError, match=message):
            load_plugins(config(plugins=["bad"]), installed(ep("bad", "bad_plugin")))
    finally:
        del sys.modules[mod.__name__]


class PluginEnv(Env):
    def __init__(self, tmp_path) -> None:
        self.path = str(tmp_path / "w.db")
        self.store = Store(self.path)
        srv = {"db_path": self.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
               "argon2_parallelism": 1, "basic_auth_user": "ui", "basic_auth_password": "uipass",
               "plugin_rate_per_minute": 5}
        self.cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                               server=srv, plugins=["echo"],
                               plugin_settings={"echo": {"greeting": "hi"}})
        self.clock = Clock()
        plugins = load_plugins(self.cfg, installed(ep("echo", "echo_plugin")))
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        app = create_app(self.cfg, self.store, sched, alerter, auth_clock=self.clock,
                         plugins=plugins)
        self.client = TestClient(app, base_url="https://testserver")


@pytest.fixture
def env(tmp_path):
    e = PluginEnv(tmp_path)
    yield e
    e.client.close()
    e.store.close()


def test_plugin_route_without_session_is_401(env):
    assert env.client.get("/api/plugins/echo/hello").status_code == 401
    # Basic auth opens read-only core routes but never a plugin route.
    assert env.client.get("/api/plugins/echo/hello", headers=BASIC).status_code == 401
    assert env.client.post("/api/plugins/echo/things", json={}).status_code == 401


def test_plugin_post_without_csrf_is_403_and_with_csrf_works(env):
    env.user("alice")
    csrf = env.csrf(env.login("alice"))
    assert env.client.get("/api/plugins/echo/hello").json() == {"greeting": "hi"}
    assert env.client.post("/api/plugins/echo/things", json={"a": 1}).status_code == 403
    bad = {"X-CSRF-Token": "0" * 64}
    assert env.client.post("/api/plugins/echo/things", json={"a": 1},
                           headers=bad).status_code == 403
    ok = env.client.post("/api/plugins/echo/things", json={"a": 1}, headers=csrf)
    assert ok.status_code == 200 and ok.json() == {"stored": {"a": 1}}


def test_plugin_cannot_loosen_admin_requirement(env):
    env.user("alice")
    env.login("alice")
    assert env.client.get("/api/plugins/echo/secret").status_code == 403
    env.client.cookies.clear()
    env.user("root", admin=True)
    env.login("root")
    assert env.client.get("/api/plugins/echo/secret").json() == {"admin": True}


def test_audit_rows_are_written_for_plugin_routes(env):
    env.user("alice")
    csrf = env.csrf(env.login("alice"))
    env.client.post("/api/plugins/echo/things", json={"a": 1}, headers=csrf)
    env.client.get("/api/plugins/echo/hello")  # reads are not audited
    rows = env.rows("SELECT actor, method, path, status, detail FROM audit "
                    "WHERE kind='plugin_request'")
    assert [r[:4] for r in rows] == [("alice", "POST", "/api/plugins/echo/things", 200)]
    assert json.loads(rows[0][4]) == {"plugin": "echo"}
    # A refused request is audited as a denial, with no actor for an anonymous caller.
    env.client.cookies.clear()
    assert env.client.post("/api/plugins/echo/things", json={}).status_code == 401
    denied = env.rows("SELECT actor, status, path FROM audit WHERE kind='plugin_denied'")
    assert denied == [("", 401, "/api/plugins/echo/things")]


def test_plugin_error_is_audited_and_not_swallowed(env):
    env.user("alice")
    csrf = env.csrf(env.login("alice"))
    with pytest.raises(RuntimeError, match="plugin bug"):
        env.client.post("/api/plugins/echo/boom", headers=csrf)
    rows = env.rows("SELECT actor, status, detail FROM audit WHERE kind='plugin_failed'")
    assert [r[:2] for r in rows] == [("alice", 500)]
    assert json.loads(rows[0][2]) == {"plugin": "echo", "error": "RuntimeError"}


def test_plugin_routes_are_rate_limited(env):
    env.user("alice")
    env.login("alice")
    codes = [env.client.get("/api/plugins/echo/hello").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]


def test_plugin_list_and_nav_need_a_session_and_respect_role(env):
    assert env.client.get("/api/v2/plugins").status_code == 401
    env.user("alice")
    env.login("alice")
    body = env.client.get("/api/v2/plugins").json()
    assert [(p["name"], p["version"]) for p in body["items"]] == [("echo", "1.2.3")]
    assert [n["label"] for n in body["items"][0]["nav"]] == ["Echo"]
    assert [g["path"] for g in body["items"][0]["pages"]] == ["/plugins/echo"]
    env.client.cookies.clear()
    env.user("root", admin=True)
    env.login("root")
    nav = env.client.get("/api/v2/plugins").json()["items"][0]["nav"]
    assert [n["label"] for n in nav] == ["Echo", "Echo admin"]


def test_core_routes_unchanged_without_plugins(tmp_path):
    e = Env(tmp_path)
    try:
        assert e.client.get("/api/plugins/echo/hello").status_code == 404
        assert LoadedPlugins().names == []
    finally:
        e.client.close()
        e.store.close()


# ---------------------------------------------------------------- collectors


class _Stop(Exception):
    """Raised by the fake pause to end a collector loop after a set number of runs."""


def _collector_sched(runs: int):
    import asyncio
    cfg = config(plugins=["echo"])
    loaded = load_plugins(cfg, installed(ep("echo", "echo_plugin")))
    sched = Scheduler(cfg, None, Alerter(cfg))  # type: ignore[arg-type]
    sched.add_collectors(loaded)
    waits: list[float] = []

    async def wait(seconds: float) -> None:
        await asyncio.sleep(0)  # yield so concurrent loops interleave
        waits.append(seconds)
        if len(waits) >= runs:
            raise _Stop

    sched._wait = wait  # type: ignore[method-assign]
    return loaded, sched, waits


async def _drive(sched):
    (plugin, c), = sched._collectors
    with pytest.raises(_Stop):
        await sched._collector_loop(plugin, c)


def test_echo_collector_runs_on_its_interval():
    import asyncio
    loaded, sched, waits = _collector_sched(3)
    asyncio.run(_drive(sched))
    assert loaded.get("echo").plugin.ticks == 3
    assert waits == [30, 30, 30]


def test_collector_exception_is_isolated_and_logged_once(caplog):
    import asyncio
    loaded, sched, waits = _collector_sched(6)
    calls = {"bad": 0, "good": 0}

    async def bad(store):
        calls["bad"] += 1
        raise RuntimeError("collector bug")

    async def good(store):
        calls["good"] += 1

    async def both():
        # Each collector has its own loop, so one failing cannot stop the other.
        with pytest.raises(_Stop):
            await asyncio.gather(sched._collector_loop("echo", Collector("bad", bad, 30, 5)),
                                 sched._collector_loop("echo", Collector("good", good, 30, 5)))

    with caplog.at_level("INFO", logger="observe"):
        asyncio.run(both())
    failures = [r for r in caplog.records if "collector echo.bad failed" in r.getMessage()]
    assert len(failures) == 1
    assert calls["bad"] >= 2 and calls["good"] >= 2


def test_collector_timeout_is_logged_once(caplog):
    import asyncio
    _, sched, _ = _collector_sched(3)

    async def slow(store):
        await asyncio.sleep(3600)

    async def go():
        with pytest.raises(_Stop):
            await sched._collector_loop("echo", Collector("slow", slow, 30, 0.01))

    with caplog.at_level("INFO", logger="observe"):
        asyncio.run(go())
    assert len([r for r in caplog.records if "echo.slow timed out" in r.getMessage()]) == 1


def _plugin_with(collectors):
    class Bad(PluginBase):
        name = "bad"
        core_versions = ">=1"

        def collectors(self):
            return collectors

    mod = type(sys)("tests.fakes.bad_plugin")
    mod.plugin = Bad()
    return mod


async def _noop(store):
    return None


@pytest.mark.parametrize("collectors,message", [
    ([Collector("c", _noop, 29.9, 5)],
     "at least 30"),
    ([Collector("c", _noop, 30, 31)],
     "timeout"),
    ([Collector("c", lambda s: None, 30, 5)],
     "async callable"),
    ([Collector("c", _noop, 30, 5),
      Collector("c", _noop, 30, 5)],
     "declared twice"),
])
def test_bad_collectors_are_refused_at_startup(collectors, message):
    mod = _plugin_with(collectors)
    sys.modules[mod.__name__] = mod
    try:
        with pytest.raises(PluginError, match=message):
            load_plugins(config(plugins=["bad"]), installed(ep("bad", "bad_plugin")))
    finally:
        del sys.modules[mod.__name__]
