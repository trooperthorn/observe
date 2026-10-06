"""The observe_unifi plugin: settings, the devices collector, pagination, backoff, redirects, caps,
pruning and the rule that an unlisted plugin never runs. The console is an httpx mock transport
that replays shapes taken from ha_Int_soc; no network is used."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from importlib.metadata import EntryPoint
from typing import Any

import httpx
import pytest

from observe.plugins import GROUP, PluginError, load_plugins
from observe.store import Store
from observe_unifi import BackedOff, UniFiPlugin, plugin as unifi_plugin
from observe_unifi.client import MAX_BODY_BYTES, MAX_PAGES, PAGE_LIMIT, AuthRejected, UniFiError

from .conftest import make_config

BASE = "/proxy/network/integration/v1"
SITE = {"id": "site-1", "name": "Default"}
CREDS = {"unifi": {"type": "unifi", "api_key": "key-secret-value"},
         "classic": {"type": "unifi_classic", "username": "viewer", "password": "pw-secret"},
         "snmp": {"type": "snmpv2c", "community": "c"}}


def run(coro):
    return asyncio.run(coro)


def device(i: int, **kw: Any) -> dict[str, Any]:
    # Shape follows ha_Int_soc tests/fakes; firmwareUpdatable on the list row is unverified.
    return {"id": f"dev-{i}", "name": f"Dev {i}", "model": "USW", "state": "ONLINE",
            "macAddress": f"AA:BB:CC:00:00:{i % 256:02X}", "ipAddress": f"192.0.2.{i % 250}",
            "firmwareVersion": "7.1.0", "firmwareUpdatable": False, **kw}


class Console:
    """A fake console. `device_pages` is a function from offset to a response body."""

    def __init__(self, devices: list[dict[str, Any]], with_total: bool = True) -> None:
        self.devices = devices
        self.with_total = with_total
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.location: str | None = None
        self.raw: bytes | None = None
        self.site_list = [SITE]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            headers = {"Location": self.location} if self.location else {}
            return httpx.Response(self.status, headers=headers, json={"error": "x"})
        if self.raw is not None:
            return httpx.Response(200, content=self.raw)
        path = request.url.path
        off = int(request.url.params.get("offset", "0"))
        lim = int(request.url.params.get("limit", "25"))
        if path == f"{BASE}/sites":
            return httpx.Response(200, json=self._env(self.site_list, off, lim))
        if path == f"{BASE}/sites/{SITE['id']}/devices":
            return httpx.Response(200, json=self._env(self.devices, off, lim))
        return httpx.Response(404, json={})

    def _env(self, rows: list[Any], off: int, lim: int) -> dict[str, Any]:
        page = rows[off:off + lim]
        body: dict[str, Any] = {"offset": off, "limit": lim, "count": len(page), "data": page}
        if self.with_total:
            body["totalCount"] = len(rows)
        return body


class Env:
    def __init__(self, tmp_path, console: Console, settings: dict[str, Any] | None = None) -> None:
        self.path = str(tmp_path / "w.db")
        s = {"host": "console.invalid", "credential": "unifi", "verify_tls": False,
             **(settings or {})}
        self.cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                               plugins=["unifi"], plugin_settings={"unifi": s},
                               credentials=CREDS, server={"db_path": self.path})
        self.loaded = load_plugins(self.cfg, lambda: [EntryPoint(
            "unifi", "observe_unifi:plugin", GROUP)])
        self.plugin = self.loaded.get("unifi").plugin
        self.plugin.transport = httpx.MockTransport(console)
        self.now = 1000.0
        self.plugin.clock = lambda: self.now
        self.plugin.wall = lambda: self.now
        self.store = Store(self.path, self.loaded)

    def rows(self, table: str = "unifi_devices") -> list[tuple]:
        db = sqlite3.connect(self.path)
        try:
            return db.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        finally:
            db.close()


@pytest.fixture(autouse=True)
def reset_plugin():
    yield
    unifi_plugin.transport = None
    unifi_plugin.clock = UniFiPlugin().clock
    unifi_plugin.wall = UniFiPlugin().wall


# ------------------------------------------------------------------ pagination


def test_pagination_reads_a_short_last_page(tmp_path):
    n = PAGE_LIMIT * 2 + 7
    console = Console([device(i) for i in range(n)], with_total=False)
    env = Env(tmp_path, console)
    assert run(env.plugin.collect_devices(env.store)) == n
    offsets = [int(r.url.params["offset"]) for r in console.requests
               if r.url.path.endswith("/devices")]
    assert offsets == [0, PAGE_LIMIT, PAGE_LIMIT * 2]  # stops at the short page, no extra request
    assert len(env.rows()) == n


def test_pagination_follows_total_count_when_the_server_caps_the_limit(tmp_path):
    console = Console([device(i) for i in range(5)])
    orig = console._env
    console._env = lambda rows, off, lim: orig(rows, off, 2)  # the server serves 2 per page
    env = Env(tmp_path, console)
    assert run(env.plugin.collect_devices(env.store)) == 5
    assert len([r for r in console.requests if r.url.path.endswith("/devices")]) == 3


def test_pagination_refuses_a_runaway_list(tmp_path):
    console = Console([])
    console._env = lambda rows, off, lim: {"data": [device(off)], "totalCount": 10**9}
    env = Env(tmp_path, console)
    with pytest.raises(UniFiError, match="more than"):
        run(env.plugin.collect_devices(env.store))
    assert len([r for r in console.requests if r.url.path.endswith("/sites")]) == MAX_PAGES
    assert env.rows() == []


def test_snapshot_keeps_first_seen_and_replaces_the_row(tmp_path):
    console = Console([device(1)])
    env = Env(tmp_path, console)
    run(env.plugin.collect_devices(env.store))
    env.now = 2000.0
    console.devices = [device(1, state="OFFLINE", firmwareUpdatable=True)]
    run(env.plugin.collect_devices(env.store))
    (row,) = env.rows()
    assert row[0:2] == ("site-1", "dev-1") and row[2] == "aa:bb:cc:00:00:01"
    assert row[5] == "OFFLINE" and row[8] == 1
    assert (row[9], row[10]) == (1000.0, 2000.0)


def test_unknown_firmware_flag_is_null_and_bad_rows_are_skipped(tmp_path):
    console = Console([{"id": "a", "name": "x"}, {"name": "no id"}, "junk", device(3)])
    env = Env(tmp_path, console)
    assert run(env.plugin.collect_devices(env.store)) == 2
    by_id = {r[1]: r for r in env.rows()}
    assert by_id["a"][8] is None and set(by_id) == {"a", "dev-3"}


def test_site_selection(tmp_path):
    console = Console([device(1)])
    console.site_list = [SITE, {"id": "site-2", "name": "Other"}]
    env = Env(tmp_path, console)
    with pytest.raises(UniFiError, match="2 sites"):
        run(env.plugin.collect_devices(env.store))
    env = Env(tmp_path, console, {"site": "Default"})
    assert run(env.plugin.collect_devices(env.store)) == 1
    env = Env(tmp_path, console, {"site": "Nope"})
    with pytest.raises(UniFiError, match="not found"):
        run(env.plugin.collect_devices(env.store))


# ------------------------------------------------------------------- 401 backoff


def test_401_backs_off_without_hammering_and_recovers(tmp_path):
    console = Console([device(1)])
    console.status = 401
    env = Env(tmp_path, console, {"interval": 120})
    with pytest.raises(AuthRejected):
        run(env.plugin.collect_devices(env.store))
    assert len(console.requests) == 1
    # Inside the pause no request is sent, however often the collector is called.
    for t in (1001.0, 1060.0, 1119.0):
        env.now = t
        with pytest.raises(BackedOff):
            run(env.plugin.collect_devices(env.store))
    assert len(console.requests) == 1
    # The first pause is one interval; the next is doubled.
    env.now = 1120.0
    with pytest.raises(AuthRejected):
        run(env.plugin.collect_devices(env.store))
    assert len(console.requests) == 2 and env.plugin._retry_at == 1120.0 + 240
    env.now = 1359.0
    with pytest.raises(BackedOff):
        run(env.plugin.collect_devices(env.store))
    assert len(console.requests) == 2
    # The pause is capped at an hour, however many failures follow.
    for _ in range(8):
        env.now = env.plugin._retry_at
        with pytest.raises(AuthRejected):
            run(env.plugin.collect_devices(env.store))
        assert env.plugin._retry_at - env.now <= 3600.0
    assert env.plugin._retry_at - env.now == 3600.0
    # A good answer ends the backoff.
    console.status = 200
    env.now = env.plugin._retry_at
    assert run(env.plugin.collect_devices(env.store)) == 1
    assert env.plugin._retry_at == 0.0 and env.plugin._failures == 0


def test_403_is_treated_like_401(tmp_path):
    console = Console([])
    console.status = 403
    env = Env(tmp_path, console)
    with pytest.raises(AuthRejected):
        run(env.plugin.collect_devices(env.store))
    with pytest.raises(BackedOff):
        run(env.plugin.collect_devices(env.store))


def test_backed_off_collector_keeps_one_failure_streak(tmp_path, caplog):
    from observe.alerts import Alerter
    from observe.scheduler import Scheduler

    class Stop(Exception):
        pass

    console = Console([])
    console.status = 401
    env = Env(tmp_path, console)
    sched = Scheduler(env.cfg, env.store, Alerter(env.cfg))
    sched.add_collectors(env.loaded)
    waits: list[float] = []

    async def wait(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= 4:
            raise Stop

    sched._wait = wait  # type: ignore[method-assign]
    (name, c), = [x for x in sched._collectors if x[1].name == "devices"]

    async def go():
        with pytest.raises(Stop):
            await sched._collector_loop(name, c)

    with caplog.at_level("INFO", logger="observe"):
        run(go())
    assert not [r for r in caplog.records if "recovered" in r.getMessage()]
    assert len(console.requests) == 1  # one request, then three paused runs
    assert "key-secret-value" not in caplog.text


# --------------------------------------------------------- redirects, caps, safety


def test_redirect_is_refused_and_never_followed(tmp_path):
    console = Console([device(1)])
    console.status = 302
    console.location = "http://elsewhere.invalid/steal"
    env = Env(tmp_path, console)
    with pytest.raises(UniFiError, match="redirect"):
        run(env.plugin.collect_devices(env.store))
    assert [r.url.host for r in console.requests] == ["console.invalid"]


def test_oversized_body_is_refused(tmp_path):
    console = Console([])
    console.raw = b'{"data": [' + b'"x",' * (MAX_BODY_BYTES // 4 + 10) + b'"y"]}'
    env = Env(tmp_path, console)
    with pytest.raises(UniFiError, match="larger than"):
        run(env.plugin.collect_devices(env.store))


def test_non_json_and_wrong_shape_are_refused(tmp_path):
    console = Console([])
    console.raw = b"<html>"
    env = Env(tmp_path, console)
    with pytest.raises(UniFiError, match="JSON"):
        run(env.plugin.collect_devices(env.store))
    console.raw = b'{"items": []}'
    with pytest.raises(UniFiError, match="envelope"):
        run(env.plugin.collect_devices(env.store))


def test_only_get_requests_with_the_key_header_are_sent(tmp_path):
    console = Console([device(1)])
    env = Env(tmp_path, console)
    run(env.plugin.collect_devices(env.store))
    assert {r.method for r in console.requests} == {"GET"}
    assert all(r.headers["X-API-KEY"] == "key-secret-value" for r in console.requests)
    assert all("key-secret-value" not in str(r.url) for r in console.requests)


# --------------------------------------------------------------------------- prune


def test_prune_deletes_records_unseen_for_30_days(tmp_path):
    console = Console([device(1), device(2)])
    env = Env(tmp_path, console)
    run(env.plugin.collect_devices(env.store))
    env.now = 1000.0 + 20 * 86400
    console.devices = [device(1)]
    run(env.plugin.collect_devices(env.store))  # dev-2 is now 20 days unseen
    db = sqlite3.connect(env.path)
    db.execute("INSERT INTO unifi_clients (site_id, client_id, first_seen, last_seen) "
               "VALUES ('site-1', 'c-old', 1, 5), ('site-1', 'c-new', 1, ?)", (env.now,))
    db.commit()
    db.close()
    assert run(env.plugin.prune(env.store, 1000.0 + 29 * 86400)) == 0  # nothing is 30 days old
    assert len(env.rows()) == 2 and len(env.rows("unifi_clients")) == 2
    # dev-2 (last seen day 0) and c-old are past 30 days; dev-1 and c-new were seen on day 20.
    assert run(env.plugin.prune(env.store, 1000.0 + 31 * 86400)) == 2
    assert {r[1] for r in env.rows()} == {"dev-1"}
    assert [r[1] for r in env.rows("unifi_clients")] == ["c-new"]


# ------------------------------------------------------------- loading and settings


def test_plugin_is_off_unless_listed(tmp_path):
    def broken_entry_points():
        return [EntryPoint("unifi", "observe_unifi_not_there:plugin", GROUP)]

    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}])
    loaded = load_plugins(cfg, broken_entry_points)  # never imported, so no error
    assert loaded.names == []
    store = Store(str(tmp_path / "w.db"), loaded)
    db = sqlite3.connect(str(tmp_path / "w.db"))
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    db.close()
    assert "unifi_devices" not in tables
    assert store is not None


def test_listed_plugin_creates_tables_and_registers_a_120_second_collector(tmp_path):
    env = Env(tmp_path, Console([]))
    assert env.rows() == [] and env.rows("unifi_clients") == []
    c, clients = env.loaded.get("unifi").collectors
    assert (clients.name, clients.interval) == ("clients", 300.0)
    assert (c.name, c.interval) == ("devices", 120.0) and c.timeout <= c.interval


@pytest.mark.parametrize("settings,message", [
    ({"credential": "ghost"}, "must name a credential of type unifi"),
    ({"credential": "snmp"}, "must name a credential of type unifi"),
    ({"classic_credential": "unifi"}, "unifi_classic"),
    ({"classic_credential": "ghost"}, "unifi_classic"),
    ({"host": ""}, "host is required"),
    ({"interval": 10}, "interval"),
    ({"bogus": 1}, "bogus"),
])
def test_bad_settings_stop_startup(tmp_path, settings, message):
    with pytest.raises(PluginError, match=message):
        Env(tmp_path, Console([]), settings)


def test_classic_credential_is_accepted_and_secrets_stay_out_of_errors(tmp_path):
    env = Env(tmp_path, Console([]), {"classic_credential": "classic"})
    assert env.plugin.settings.classic_credential == "classic"
    with pytest.raises(PluginError) as err:
        Env(tmp_path, Console([]), {"credential": "classic"})
    assert "pw-secret" not in str(err.value) and "key-secret-value" not in str(err.value)


def test_collect_runs_through_the_scheduler_loop(tmp_path):
    from observe.alerts import Alerter
    from observe.scheduler import Scheduler

    class Stop(Exception):
        pass

    env = Env(tmp_path, Console([device(1)]))
    sched = Scheduler(env.cfg, env.store, Alerter(env.cfg))
    sched.add_collectors(env.loaded)

    async def wait(seconds: float) -> None:
        assert seconds == 120
        raise Stop

    sched._wait = wait  # type: ignore[method-assign]
    (name, c), = [x for x in sched._collectors if x[1].name == "devices"]

    async def go():
        with pytest.raises(Stop):
            await sched._collector_loop(name, c)

    run(go())
    assert len(env.rows()) == 1
    assert json.dumps(env.rows())  # rows are plain data
