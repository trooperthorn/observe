"""The UniFi clients and Protect collectors: a 500 client fixture within a time budget, upsert by
MAC, classic enrichment and offline clients, the 30 day prune, Protect cameras and the rule that
only GET requests follow the classic login. The console is an httpx mock transport replaying
shapes taken from ha_Int_soc docs/UNIFI-LOCAL-API-CONTRACT.md; no network is used."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

import httpx
import pytest

from observe_unifi import BackedOff, UniFiPlugin, plugin as unifi_plugin
from observe_unifi.client import AuthRejected, UniFiError
from observe_unifi.clients import norm_mac, parse_camera, parse_client

from .test_unifi_plugin import BASE, SITE, Console, Env, device, run

PROTECT = "/proxy/protect/integration/v1"
CLASSIC = "/proxy/network/api/s/default/"
COOKIE, CSRF = "cookie-secret-token", "csrf-secret-token"
BUDGET_S = 1.0  # the whole fixture runs in about 0.1 s here, so this leaves a tenfold margin


@pytest.fixture(autouse=True)
def reset_plugin():
    yield
    unifi_plugin.transport = None
    unifi_plugin.classic = None
    unifi_plugin.classic_note = None
    unifi_plugin.clock = UniFiPlugin().clock
    unifi_plugin.wall = UniFiPlugin().wall


def client_row(i: int, **kw: Any) -> dict[str, Any]:
    # Shape follows the contract: id, name, macAddress, ipAddress, connectedAt, type,
    # uplinkDeviceId. The values of type and the format of connectedAt are unverified.
    return {"id": f"cli-{i}", "name": f"Client {i}", "macAddress": f"02:00:00:00:{i // 256:02X}:{i % 256:02X}",
            "ipAddress": f"10.0.{i // 250}.{i % 250 + 1}", "connectedAt": "2026-09-01T10:00:00Z",
            "type": "WIRELESS" if i % 2 else "WIRED", "uplinkDeviceId": "dev-1", **kw}


class Full(Console):
    """The Integration API plus classic stat/sta and rest/user plus Protect."""

    def __init__(self, clients: list[dict[str, Any]], devices: list[dict[str, Any]] | None = None
                 ) -> None:
        super().__init__(devices if devices is not None else [device(1), device(2)])
        self.clients = clients
        self.active: list[Any] = []
        self.known: list[Any] = []
        self.cameras: Any = []
        self.protect_status = 200
        self.classic_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.method == "POST" and p == "/api/auth/login":
            self.requests.append(request)
            return httpx.Response(200, json={}, headers={
                "set-cookie": f"TOKEN={COOKIE}; path=/; secure; httponly", "x-csrf-token": CSRF})
        if request.method == "POST" and p == "/api/auth/logout":
            self.requests.append(request)
            return httpx.Response(200, json={})
        if p.startswith(CLASSIC):
            self.requests.append(request)
            if self.classic_status != 200:
                return httpx.Response(self.classic_status, json={})
            rows = {"stat/sta": self.active, "rest/user": self.known}.get(p[len(CLASSIC):])
            return httpx.Response(404 if rows is None else 200, json={"data": rows})
        if p == f"{PROTECT}/cameras":
            self.requests.append(request)
            assert "offset" not in request.url.params and "limit" not in request.url.params
            return httpx.Response(self.protect_status, json=self.cameras)
        if p == f"{BASE}/sites/{SITE['id']}/clients":
            self.requests.append(request)
            off = int(request.url.params.get("offset", "0"))
            lim = int(request.url.params.get("limit", "25"))
            return httpx.Response(200, json=self._env(self.clients, off, lim))
        return super().__call__(request)


def env_with(tmp_path, console: Full, **settings: Any) -> Env:
    env = Env(tmp_path, console, settings)
    env.plugin.transport = httpx.MockTransport(console)
    return env


def clients(env: Env) -> dict[str, tuple]:
    db = sqlite3.connect(env.path)
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(unifi_clients)")]
        return {r[cols.index("client_id")]: r for r in db.execute("SELECT * FROM unifi_clients")}
    finally:
        db.close()


def col(env: Env, name: str, cid: str) -> Any:
    db = sqlite3.connect(env.path)
    try:
        return db.execute(f"SELECT {name} FROM unifi_clients WHERE client_id=?", (cid,)).fetchone()[0]
    finally:
        db.close()


# ------------------------------------------------------------------------ parsing


def test_mac_normalisation_and_client_parsing():
    assert norm_mac("AA-BB-CC-00-11-22") == "aa:bb:cc:00:11:22"
    assert norm_mac("aabbcc001122") == "aa:bb:cc:00:11:22"
    assert norm_mac("not a mac") == "" and norm_mac(None) == ""
    c = parse_client("s", client_row(3, type="weird"))
    assert c is not None and c.client_id == "02:00:00:00:00:03" and c.kind == ""
    assert parse_client("s", {"id": "only-id"}).client_id == "only-id"
    assert parse_client("s", {"name": "no key"}) is None and parse_client("s", "junk") is None


def test_camera_recording_is_unknown_unless_the_row_says_so():
    # isRecording as a boolean is the only recording shape treated as verified.
    assert parse_camera({"id": "c1", "isRecording": True}).recording is True
    assert parse_camera({"id": "c1", "recordingSettings": {"mode": "always"}}).recording is None
    assert parse_camera({"id": "c1", "isRecording": "yes"}).recording is None
    assert parse_camera({"id": "c1", "isConnected": 1}).connected is None
    assert parse_camera({"name": "no id"}) is None


# ------------------------------------------------------------------ 500 clients


def test_500_clients_are_stored_within_the_time_budget(tmp_path):
    console = Full([client_row(i) for i in range(500)])
    env = env_with(tmp_path, console)
    run(env.plugin.collect_devices(env.store))
    started = time.perf_counter()
    assert run(env.plugin.collect_clients(env.store)) == 500
    assert time.perf_counter() - started < BUDGET_S
    rows = clients(env)
    assert len(rows) == 500
    asked = [r for r in console.requests if r.url.path.endswith("/clients")]
    assert len(asked) == 3  # 200 per page
    started = time.perf_counter()
    from observe_unifi.clients import read_clients
    got = read_clients(env.store)
    assert time.perf_counter() - started < BUDGET_S
    assert got["total"] == 500 and not got["truncated"]
    first = got["clients"][0]
    assert first["uplink_name"] == "Dev 1" and first["connected"] is True


def test_a_second_poll_updates_by_mac_and_keeps_first_seen(tmp_path):
    console = Full([client_row(1, name="Old", ipAddress="10.0.0.9")])
    env = env_with(tmp_path, console)
    run(env.plugin.collect_clients(env.store))
    env.now = 2000.0
    console.clients = [client_row(1, name="New", ipAddress="10.0.0.10")]
    run(env.plugin.collect_clients(env.store))
    (row,) = clients(env).values()
    assert (col(env, "name", row[1]), col(env, "ip", row[1])) == ("New", "10.0.0.10")
    assert (col(env, "first_seen", row[1]), col(env, "last_seen", row[1])) == (1000.0, 2000.0)
    assert len(clients(env)) == 1


def test_a_client_that_leaves_is_marked_not_connected_not_deleted(tmp_path):
    console = Full([client_row(1), client_row(2)])
    env = env_with(tmp_path, console)
    run(env.plugin.collect_clients(env.store))
    env.now = 1300.0
    console.clients = [client_row(1)]
    run(env.plugin.collect_clients(env.store))
    assert col(env, "connected", "02:00:00:00:00:01") == 1
    assert col(env, "connected", "02:00:00:00:00:02") == 0


# --------------------------------------------------------------- classic detail


def classic_env(tmp_path, console: Full) -> Env:
    return env_with(tmp_path, console, classic_credential="classic")


def test_classic_enriches_connected_clients_and_adds_offline_ones(tmp_path):
    console = Full([client_row(2, uplinkDeviceId=""), client_row(3, uplinkDeviceId="")])
    # Unverified classic fields: ap_mac, sw_mac, sw_port, essid, is_wired, last_seen.
    console.active = [
        {"mac": "02:00:00:00:00:02", "is_wired": False, "ap_mac": "AA:BB:CC:00:00:02",
         "essid": "Home"},
        {"mac": "02:00:00:00:00:03", "is_wired": True, "sw_mac": "AA:BB:CC:00:00:01", "sw_port": 7}]
    console.known = [{"mac": "02:00:00:00:00:02", "name": "Phone"},
                     {"mac": "02:00:00:00:09:09", "name": "Printer", "last_seen": 39 * 86400.0},
                     {"mac": "02:00:00:00:08:08", "hostname": "ancient", "last_seen": 5.0}]
    env = classic_env(tmp_path, console)
    env.now = 40 * 86400.0
    run(env.plugin.collect_devices(env.store))
    assert run(env.plugin.collect_clients(env.store)) == 3
    wifi, wired, printer = (clients(env)[k] for k in
                            ("02:00:00:00:00:02", "02:00:00:00:00:03", "02:00:00:00:09:09"))
    db = sqlite3.connect(env.path)
    got = db.execute("SELECT client_id, connected, ssid, sw_port, uplink_device_id, enriched, "
                     "last_seen FROM unifi_clients ORDER BY client_id").fetchall()
    db.close()
    assert got[0] == ("02:00:00:00:00:02", 1, "Home", None, "dev-2", 1, env.now)
    assert got[1] == ("02:00:00:00:00:03", 1, "", 7, "dev-1", 1, env.now)
    assert got[2][:2] == ("02:00:00:00:09:09", 0) and got[2][6] == 39 * 86400.0
    assert "02:00:00:00:08:08" not in clients(env)  # older than the retention, not re-added
    assert wifi and wired and printer
    assert env.plugin.classic_note is None
    methods = [(r.method, r.url.path) for r in console.requests]
    assert [p for m, p in methods if m != "GET"] == ["/api/auth/login"]
    assert all(m == "GET" or p.startswith("/api/auth/") for m, p in methods)


def test_offline_rows_keep_what_is_known_and_never_move_last_seen_back(tmp_path):
    console = Full([client_row(1)])
    console.known = [{"mac": "02:00:00:00:00:01", "name": "Phone"}]
    env = classic_env(tmp_path, console)
    env.now = 5000.0
    run(env.plugin.collect_clients(env.store))
    env.now = 6000.0
    console.clients = []
    console.active = []
    console.known = [{"mac": "02:00:00:00:00:01", "name": "", "last_seen": 100.0}]
    run(env.plugin.collect_clients(env.store))
    cid = "02:00:00:00:00:01"
    assert col(env, "connected", cid) == 0
    assert col(env, "name", cid) == "Client 1"  # an empty name does not blank a known one
    assert col(env, "ip", cid) == "10.0.0.2"
    assert col(env, "last_seen", cid) == 5000.0


def test_a_classic_failure_keeps_the_integration_rows_and_says_why(tmp_path):
    console = Full([client_row(1)])
    console.classic_status = 500
    env = classic_env(tmp_path, console)
    assert run(env.plugin.collect_clients(env.store)) == 1
    assert env.plugin.classic_note and "classic detail unavailable" in env.plugin.classic_note
    assert "pw-secret" not in env.plugin.classic_note
    assert col(env, "enriched", "02:00:00:00:00:01") == 0


def test_without_a_classic_credential_no_classic_request_is_sent(tmp_path):
    console = Full([client_row(1)])
    env = env_with(tmp_path, console)
    run(env.plugin.collect_clients(env.store))
    assert not [r for r in console.requests if r.url.path.startswith(("/api/auth", CLASSIC))]


def test_a_rejected_key_backs_off_the_clients_collector_too(tmp_path):
    console = Full([client_row(1)])
    console.status = 401
    env = env_with(tmp_path, console)
    with pytest.raises(AuthRejected):
        run(env.plugin.collect_clients(env.store))
    sent = len(console.requests)
    with pytest.raises(BackedOff):
        run(env.plugin.collect_clients(env.store))
    assert len(console.requests) == sent


# ----------------------------------------------------------------------- prune


def test_clients_and_cameras_unseen_for_30_days_are_pruned(tmp_path):
    console = Full([client_row(1)])
    console.cameras = [{"id": "cam-1", "name": "Door"}]
    env = env_with(tmp_path, console, protect=True)
    run(env.plugin.collect_clients(env.store))
    run(env.plugin.collect_protect(env.store))
    assert run(env.plugin.prune(env.store, 1000.0 + 29 * 86400)) == 0
    assert run(env.plugin.prune(env.store, 1000.0 + 31 * 86400)) == 2
    assert clients(env) == {} and env.rows("unifi_cameras") == []


# --------------------------------------------------------------------- Protect


def test_protect_cameras_are_stored_from_the_unpaged_array(tmp_path):
    console = Full([])
    # Shape follows ha_Int_soc _normalize_camera keys. isRecording as a boolean is the verified one.
    console.cameras = [
        {"id": "cam-1", "name": "Door", "mac": "AA:BB:CC:00:10:01", "state": "connected",
         "isConnected": True, "isRecording": True, "modelKey": "camera"},
        {"id": "cam-2", "name": "Yard", "state": "DISCONNECTED", "isConnected": False,
         "recordingSettings": {"mode": "always"}},
        {"name": "no id"}, "junk"]
    env = env_with(tmp_path, console, protect=True)
    assert [c.name for c in env.plugin.collectors()] == ["devices", "clients", "protect"]
    assert run(env.plugin.collect_protect(env.store)) == 2
    rows = {r[0]: r for r in env.rows("unifi_cameras")}
    assert rows["cam-1"][4:7] == ("CONNECTED", 1, 1)
    assert rows["cam-2"][4:7] == ("DISCONNECTED", 0, None)  # recording unknown, never guessed
    assert {r.method for r in console.requests} == {"GET"}
    assert all(r.headers["X-API-KEY"] == "key-secret-value" for r in console.requests)


def test_protect_is_off_by_default(tmp_path):
    env = env_with(tmp_path, Full([]))
    assert [c.name for c in env.plugin.collectors()] == ["devices", "clients"]


def test_protect_has_its_own_backoff(tmp_path):
    console = Full([])
    env = env_with(tmp_path, console, protect=True)
    console.protect_status = 401
    with pytest.raises(AuthRejected):
        run(env.plugin.collect_protect(env.store))
    with pytest.raises(BackedOff):
        run(env.plugin.collect_protect(env.store))
    assert len([r for r in console.requests if r.url.path.endswith("/cameras")]) == 1
    # A rejected Protect key does not pause the network collectors.
    assert run(env.plugin.collect_devices(env.store)) == 2


def test_protect_refuses_a_non_list_answer(tmp_path):
    console = Full([])
    console.cameras = {"data": []}
    env = env_with(tmp_path, console, protect=True)
    with pytest.raises(UniFiError, match="did not return a list"):
        run(env.plugin.collect_protect(env.store))
    assert json.dumps(env.rows("unifi_cameras")) == "[]"


# ------------------------------------------------------------------ migration and staleness


def test_schema_upgrades_from_version_1_keeping_rows():
    from observe.store import migrate_plugins
    from observe_unifi.records import MIGRATIONS
    db = sqlite3.connect(":memory:")
    migrate_plugins(db, {"unifi": MIGRATIONS[:1]})
    db.execute("INSERT INTO unifi_clients (site_id, client_id, mac, name, first_seen, last_seen) "
               "VALUES ('s', 'c1', 'aa', 'Old', 1.0, 2.0)")
    db.commit()
    assert db.execute("SELECT version FROM plugin_schema WHERE plugin='unifi'").fetchone()[0] == 1
    migrate_plugins(db, {"unifi": MIGRATIONS})
    assert db.execute("SELECT version FROM plugin_schema WHERE plugin='unifi'").fetchone()[0] == 2
    cols = {r[1] for r in db.execute("PRAGMA table_info(unifi_clients)")}
    assert {"connected", "connected_at", "ssid", "uplink_mac", "sw_port", "enriched"} <= cols
    row = db.execute("SELECT name, connected, ssid, enriched FROM unifi_clients").fetchone()
    assert row == ("Old", None, "", 0)  # the old row survives; connected is unknown, not false
    assert db.execute("SELECT COUNT(*) FROM unifi_cameras").fetchone()[0] == 0
    migrate_plugins(db, {"unifi": MIGRATIONS})  # a second run changes nothing
    assert db.execute("SELECT version FROM plugin_schema WHERE plugin='unifi'").fetchone()[0] == 2


def test_connected_rows_not_refreshed_lately_are_flagged_stale(tmp_path):
    from observe_unifi.clients import read_cameras, read_clients
    console = Full([client_row(1)])
    console.cameras = [{"id": "cam-1", "name": "Door", "isConnected": True, "isRecording": True}]
    env = env_with(tmp_path, console, protect=True)
    run(env.plugin.collect_devices(env.store))
    run(env.plugin.collect_clients(env.store))
    run(env.plugin.collect_protect(env.store))
    now = env.plugin.wall()
    assert read_clients(env.store, now, 750.0)["clients"][0]["stale"] is False
    assert read_clients(env.store, now + 751.0, 750.0)["clients"][0]["stale"] is True
    assert read_cameras(env.store, now, 300.0)["cameras"][0]["stale"] is False
    assert read_cameras(env.store, now + 301.0, 300.0)["cameras"][0]["stale"] is True
    assert read_clients(env.store)["clients"][0]["stale"] is False  # no clock given


def test_classic_note_carries_only_the_error_class(tmp_path):
    console = Full([client_row(1)])
    console.classic_status = 403
    env = classic_env(tmp_path, console)
    run(env.plugin.collect_clients(env.store))
    note = env.plugin.classic_note
    assert note.startswith("classic detail unavailable: ") and " " not in note.split(": ")[1]
    for secret in ("pw-secret", COOKIE, CSRF, "key-secret-value", "HTTP 403", "/proxy"):
        assert secret not in note
