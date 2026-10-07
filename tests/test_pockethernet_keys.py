"""The wpf key scope: bound to a device label, refused wherever wpi is required, and back."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.ingest.keys import IngestKeyError, create_key, list_keys, revoke_key, verify_key
from observe.plugins import GROUP, load_plugins
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app
from observe_pockethernet.keys import (SCOPE, create_field_key, field_key_device,
                                         verify_field_key)

from .conftest import make_config
from .otlp_build import post_batch
from .test_auth import PASSWORD, Clock

HOST_BATCH = json.loads((Path(__file__).parent / "fixtures" / "hostwatch"
                         / "batch_minimal.json").read_text(encoding="utf-8"))


def run(coro):
    return asyncio.run(coro)


class Env:
    def __init__(self, tmp_path, plugins: bool = True) -> None:
        self.path = str(tmp_path / "w.db")
        self.store = Store(self.path)
        self.cfg = make_config(
            [{"name": "p", "type": "ping", "host": "127.0.0.1"}],
            plugins=["pockethernet"] if plugins else [],
            server={"db_path": self.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
                    "argon2_parallelism": 1})
        loaded = load_plugins(self.cfg, lambda: [EntryPoint(
            "pockethernet", "observe_pockethernet:plugin", GROUP)])
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        self.clock = Clock()
        self.client = TestClient(
            create_app(self.cfg, self.store, sched, alerter, plugins=loaded,
                       auth_clock=self.clock), base_url="https://testserver")

    def admin(self) -> dict[str, str]:
        run(auth.create_user(self.store, self.cfg, "root", PASSWORD, True, now=self.clock()))
        r = self.client.post("/api/login", json={"username": "root", "password": PASSWORD})
        assert r.status_code == 200
        return {"X-CSRF-Token": r.json()["csrf"]}

    def ingest(self, key: str | None, body: dict | None = None):
        return post_batch(self.client, body or HOST_BATCH, key)

    def rows(self, sql: str, *args):
        db = sqlite3.connect(self.path)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def close(self) -> None:
        self.client.close()
        self.store.close()


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "k.db"))
    yield s
    s.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_plugin_registers_the_wpf_scope():
    from observe_pockethernet import plugin
    assert [s.marker for s in plugin.key_scopes()] == [SCOPE] == ["wpf"]


def test_plugin_loads_through_the_entry_point_contract():
    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                      plugins=["pockethernet"])
    loaded = load_plugins(cfg, lambda: [EntryPoint(
        "pockethernet", "observe_pockethernet:plugin", GROUP)])
    assert loaded.names == ["pockethernet"] and loaded.scopes == ["wpf"]


def test_wpf_key_is_bound_to_a_device_label(store):
    key, info = run(create_field_key(store, "sean-pixel", created_by="tester"))
    assert key.startswith("wpf_") and info.scope == "wpf" and info.host == "sean-pixel"
    assert run(verify_field_key(store, key, "sean-pixel", now=50.0))
    assert not run(verify_field_key(store, key, "other-phone"))
    assert not run(verify_field_key(store, key, "SEAN-PIXEL"))
    assert run(field_key_device(store, key)) == (info.prefix, "sean-pixel")
    (listed,) = run(list_keys(store))
    assert listed.scope == "wpf" and listed.last_used == 50.0


def test_revoked_wpf_key_is_refused(store):
    key, info = run(create_field_key(store, "sean-pixel"))
    assert run(revoke_key(store, info.prefix))
    assert not run(verify_field_key(store, key, "sean-pixel"))
    assert run(field_key_device(store, key)) is None
    (listed,) = run(list_keys(store))
    assert not listed.active and listed.last_used is None


def test_wpi_key_is_refused_on_field_reports(store):
    host_key, _ = run(create_key(store, "nas01"))
    assert host_key.startswith("wpi_")
    assert not run(verify_field_key(store, host_key, "nas01"))
    assert run(field_key_device(store, host_key)) is None
    assert run(list_keys(store))[0].last_used is None  # a refusal is not a use


def test_marker_swapped_keys_are_refused_both_ways(store):
    """The marker is not trusted alone: the stored scope must agree with it."""
    field_key, _ = run(create_field_key(store, "sean-pixel"))
    host_key, _ = run(create_key(store, "nas01"))
    as_host = "wpi_" + field_key.split("_", 1)[1]
    as_field = "wpf_" + host_key.split("_", 1)[1]
    assert not run(verify_key(store, as_host, "sean-pixel"))
    assert not run(verify_field_key(store, as_field, "nas01"))
    assert run(field_key_device(store, as_field)) is None
    assert run(verify_field_key(store, field_key, "sean-pixel"))
    assert run(verify_key(store, host_key, "nas01"))


def test_scope_must_be_well_formed(store):
    for bad in ("", "wp", "WPF", "wpf1", "toolongscope", "w_f"):
        with pytest.raises(IngestKeyError):
            run(create_key(store, "dev", scope=bad))
    assert run(list_keys(store)) == []


def test_wpf_key_never_writes_host_data(env):
    field_key, _ = run(create_field_key(env.store, "nas01"))  # even with a matching label
    # A field key is accepted by the OTLP routes, but what it sends is a field tester resource
    # named by the key, never a host.
    assert env.ingest(field_key).status_code == 200
    assert env.rows("SELECT COUNT(*) FROM hosts") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM host_sources") == [(0,)]
    assert env.rows("SELECT DISTINCT kind FROM resources") == [("field_tester",)]
    swapped = "wpi_" + field_key.split("_", 1)[1]
    assert env.ingest(swapped).status_code == 401
    assert env.rows("SELECT COUNT(*) FROM hosts") == [(0,)]
    assert len(env.rows("SELECT id FROM audit WHERE kind='ingest_denied'")) >= 1


def test_wpi_key_still_works_on_host_ingest(env):
    host_key, _ = run(create_key(env.store, "nas01"))
    assert env.ingest(host_key).status_code == 200  # host nas01, as in the batch


def test_existing_keys_default_to_the_wpi_scope(tmp_path):
    path = str(tmp_path / "old.db")
    Store(path).close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO ingest_keys (prefix, hash, host, created) VALUES ('p', 'h', 'x', 1)")
    db.commit()
    assert db.execute("SELECT scope FROM ingest_keys").fetchall() == [("wpi",)]
    db.close()


def test_admin_issues_wpf_keys_only_when_the_plugin_is_listed(env):
    hdr = env.admin()
    made = env.client.post("/api/admin/keys", json={"host": "sean-pixel", "scope": "wpf"},
                           headers=hdr)
    assert made.status_code == 200
    body = made.json()
    assert body["scope"] == "wpf" and body["key"].startswith("wpf_")
    assert run(verify_field_key(env.store, body["key"], "sean-pixel"))
    listing = env.client.get("/api/v2/admin/keys")
    assert [(k["host"], k["scope"]) for k in listing.json()["items"]] == [("sean-pixel", "wpf")]
    assert body["key"] not in listing.text
    default = env.client.post("/api/admin/keys", json={"host": "h"}, headers=hdr)
    assert default.json()["scope"] == "wpi"
    for bad in ("nope", "WPF", 7, None):
        r = env.client.post("/api/admin/keys", json={"host": "h", "scope": bad}, headers=hdr)
        assert r.status_code == 422
    assert env.rows("SELECT COUNT(*) FROM ingest_keys") == [(2,)]
    assert len(env.rows("SELECT id FROM audit WHERE kind='key_create_failed'")) == 4


def test_admin_cannot_issue_wpf_keys_when_the_plugin_is_not_listed(tmp_path):
    e = Env(tmp_path, plugins=False)
    try:
        hdr = e.admin()
        r = e.client.post("/api/admin/keys", json={"host": "p", "scope": "wpf"}, headers=hdr)
        assert r.status_code == 422
        assert e.rows("SELECT COUNT(*) FROM ingest_keys") == [(0,)]
    finally:
        e.close()
