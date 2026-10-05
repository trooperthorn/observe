"""The control plugin skeleton: signing key, canonical JSON, the shared test vector, and the
wpc key scope on every surface (docs/CONTROL.md)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sqlite3
import types
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.ingest.keys import create_key, list_keys, revoke_key
from observe.plugins import GROUP, PluginError, load_plugins
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app
from observe_control import plugin as control_plugin
from observe_control.keys import (SCOPE, control_key_host, create_control_key,
                                    verify_control_key)
from observe_control.signing import (SigningError, canonical_json, keygen, load_private_key,
                                       private_from_seed, public_key_string, sign_command,
                                       verify_command)
from observe_pockethernet.keys import create_field_key

from .conftest import make_config
from .test_auth import PASSWORD, Clock
from .test_pockethernet_keys import HOST_BATCH

ROOT = Path(__file__).parent.parent

# The shared test vector. docs/CONTROL.md carries the same values and the hostwatch-control
# daemon must reproduce them.
VECTOR_SEED = bytes(range(32))
VECTOR_PUBLIC = "ed25519:A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
VECTOR_COMMAND = {
    "v": 1, "id": "6f1c2a52-8d0e-4c53-9a53-0d1f6d2f7a10", "host": "MediaIn-SVR",
    "action": "fan.set_floor",
    "params": {"controller": "thermalctl", "header": "pwm2", "min_duty": 20},
    "requested_by": "sean", "issued_at": 1759600000, "expires_at": 1759600120, "seq": 42}
VECTOR_CANONICAL = (
    '{"action":"fan.set_floor","expires_at":1759600120,"host":"MediaIn-SVR",'
    '"id":"6f1c2a52-8d0e-4c53-9a53-0d1f6d2f7a10","issued_at":1759600000,'
    '"params":{"controller":"thermalctl","header":"pwm2","min_duty":20},'
    '"requested_by":"sean","seq":42,"v":1}')
VECTOR_SIGNATURE = ("ctLtz6sxgqiI2PRdESeNINEzokkV7Nq+X60xM++KglMxeMpVoqW7/xqXGQtRxXNu/"
                    "p0aSkfjNSdzIAvoqA+ICg==")


def run(coro):
    return asyncio.run(coro)


# ---- signing and the test vector --------------------------------------------------------

def test_vector_canonical_json_public_key_and_signature():
    key = private_from_seed(VECTOR_SEED)
    assert public_key_string(key) == VECTOR_PUBLIC
    assert canonical_json(VECTOR_COMMAND) == VECTOR_CANONICAL.encode("utf-8")
    assert sign_command(key, VECTOR_COMMAND) == VECTOR_SIGNATURE
    assert verify_command(VECTOR_PUBLIC, VECTOR_COMMAND, VECTOR_SIGNATURE)


def test_canonical_json_ignores_key_order_and_keeps_utf8():
    shuffled = dict(reversed(list(VECTOR_COMMAND.items())))
    assert canonical_json(shuffled) == canonical_json(VECTOR_COMMAND)
    assert canonical_json({"b": "café", "a": 1}) == '{"a":1,"b":"café"}'.encode()


def test_tampering_with_any_field_fails():
    def variants(cmd, path=()):
        for k, v in cmd.items():
            if isinstance(v, dict):
                yield from variants(v, path + (k,))
            else:
                yield path + (k,), v
    count = 0
    for path, value in variants(VECTOR_COMMAND):
        tampered = json.loads(json.dumps(VECTOR_COMMAND))
        node = tampered
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = value + 1 if isinstance(value, int) else value + "x"
        assert not verify_command(VECTOR_PUBLIC, tampered, VECTOR_SIGNATURE), path
        count += 1
    assert count == 11
    extra = {**VECTOR_COMMAND, "extra": 1}
    assert not verify_command(VECTOR_PUBLIC, extra, VECTOR_SIGNATURE)
    missing = {k: v for k, v in VECTOR_COMMAND.items() if k != "seq"}
    assert not verify_command(VECTOR_PUBLIC, missing, VECTOR_SIGNATURE)


def test_bad_signatures_and_keys_fail_without_raising():
    other = public_key_string(private_from_seed(bytes(32)))
    assert not verify_command(other, VECTOR_COMMAND, VECTOR_SIGNATURE)
    for sig in ("", "not base64!", "AAAA", VECTOR_SIGNATURE[:-4], None):
        assert not verify_command(VECTOR_PUBLIC, VECTOR_COMMAND, sig)
    for pub in ("", "ed25519:", "ed25519:AAAA", "rsa:" + VECTOR_PUBLIC[8:], None):
        assert not verify_command(pub, VECTOR_COMMAND, VECTOR_SIGNATURE)


# ---- keygen, load, permissions ----------------------------------------------------------

def test_keygen_writes_a_loadable_key_and_returns_only_the_public_key(tmp_path):
    path = tmp_path / "control.key"
    public = keygen(path)
    assert re.fullmatch(r"ed25519:[A-Za-z0-9+/]{43}=", public)
    key = load_private_key(path)
    assert public_key_string(key) == public
    assert verify_command(public, VECTOR_COMMAND, sign_command(key, VECTOR_COMMAND))
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_keygen_never_overwrites(tmp_path):
    path = tmp_path / "control.key"
    keygen(path)
    before = path.read_bytes()
    with pytest.raises(SigningError):
        keygen(path)
    assert path.read_bytes() == before


def test_group_or_world_readable_key_is_refused(tmp_path):
    path = tmp_path / "control.key"
    keygen(path)
    for mode in (0o640, 0o604, 0o644, 0o660, 0o777):
        fake = lambda p, m=mode: types.SimpleNamespace(st_mode=0o100000 | m)  # noqa: E731
        with pytest.raises(SigningError, match="readable by group or others"):
            load_private_key(path, posix=True, stat=fake)
    ok = lambda p: types.SimpleNamespace(st_mode=0o100600)  # noqa: E731
    assert load_private_key(path, posix=True, stat=ok) is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_real_group_readable_file_is_refused(tmp_path):
    path = tmp_path / "control.key"
    keygen(path)
    os.chmod(path, 0o640)
    with pytest.raises(SigningError, match="readable by group or others"):
        load_private_key(path)


def test_missing_garbage_and_wrong_type_keys_are_refused(tmp_path):
    with pytest.raises(SigningError, match="cannot read"):
        load_private_key(tmp_path / "absent.key")
    junk = tmp_path / "junk.key"
    junk.write_text("not a key", encoding="utf-8")
    with pytest.raises(SigningError, match="not an unencrypted PEM"):
        load_private_key(junk)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
    rsa = tmp_path / "rsa.key"
    rsa.write_bytes(generate_private_key(65537, 2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    with pytest.raises(SigningError, match="not an Ed25519"):
        load_private_key(rsa)


def test_error_messages_never_hold_key_material(tmp_path):
    path = tmp_path / "control.key"
    keygen(path)
    secret = path.read_text(encoding="ascii").splitlines()[1]
    fake = lambda p: types.SimpleNamespace(st_mode=0o100644)  # noqa: E731
    with pytest.raises(SigningError) as err:
        load_private_key(path, posix=True, stat=fake)
    assert secret not in str(err.value)


# ---- plugin load ------------------------------------------------------------------------

def _entry_points():
    return lambda: [EntryPoint("control", "observe_control:plugin", GROUP),
                    EntryPoint("pockethernet", "observe_pockethernet:plugin", GROUP)]


def test_plugin_refuses_to_start_without_a_usable_key(tmp_path):
    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}], plugins=["control"],
                      plugin_settings={"control": {"signing_key_file": str(tmp_path / "no")}})
    with pytest.raises(PluginError, match="cannot read the signing key file"):
        load_plugins(cfg, _entry_points())


def test_plugin_refuses_insecure_key_at_startup(tmp_path, monkeypatch):
    path = tmp_path / "control.key"
    keygen(path)
    import observe_control
    real = observe_control.load_private_key
    monkeypatch.setattr(observe_control, "load_private_key", lambda p: real(
        p, posix=True, stat=lambda q: types.SimpleNamespace(st_mode=0o100644)))
    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}], plugins=["control"],
                      plugin_settings={"control": {"signing_key_file": str(path)}})
    with pytest.raises(PluginError, match="readable by group or others"):
        load_plugins(cfg, _entry_points())


def test_settings_reject_unknown_and_out_of_range(tmp_path):
    for bad in ({"nope": 1}, {"pull_interval_s": 0}, {"max_commands_per_host_per_hour": 0}):
        cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                          plugins=["control"], plugin_settings={"control": bad})
        with pytest.raises(PluginError, match="invalid settings"):
            load_plugins(cfg, _entry_points())


def test_plugin_loads_and_registers_the_wpc_scope(tmp_path):
    path = tmp_path / "control.key"
    public = keygen(path)
    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}], plugins=["control"],
                      plugin_settings={"control": {"signing_key_file": str(path),
                                                   "pull_interval_s": 10}})
    loaded = load_plugins(cfg, _entry_points())
    assert loaded.names == ["control"] and loaded.scopes == ["wpc"] == [SCOPE]
    assert control_plugin.public_key == public
    assert control_plugin.settings.pull_interval_s == 10


# ---- the wpc scope on every surface -----------------------------------------------------

class Env:
    def __init__(self, tmp_path) -> None:
        self.path = str(tmp_path / "w.db")
        key_file = tmp_path / "control.key"
        keygen(key_file)
        self.cfg = make_config(
            [{"name": "p", "type": "ping", "host": "127.0.0.1"}],
            plugins=["control", "pockethernet"],
            plugin_settings={"control": {"signing_key_file": str(key_file)}},
            server={"db_path": self.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
                    "argon2_parallelism": 1})
        loaded = load_plugins(self.cfg, _entry_points())
        self.store = Store(self.path, loaded)
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

    def rows(self, sql: str, *args):
        db = sqlite3.connect(self.path)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def pull(self, key: str | None, host: str = "nas01"):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return self.client.get("/api/v1/control/commands", params={"host": host},
                               headers=headers)

    def close(self) -> None:
        self.client.close()
        self.store.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_wpc_key_is_bound_to_a_host(env):
    key, info = run(create_control_key(env.store, "nas01", created_by="tester"))
    assert key.startswith("wpc_") and info.scope == "wpc" and info.host == "nas01"
    assert run(verify_control_key(env.store, key, "nas01", now=9.0))
    assert not run(verify_control_key(env.store, key, "nas02"))
    assert run(control_key_host(env.store, key)) == (info.prefix, "nas01")
    assert run(revoke_key(env.store, info.prefix))
    assert not run(verify_control_key(env.store, key, "nas01"))
    assert run(control_key_host(env.store, key)) is None


def test_wpc_key_pulls_only_for_its_own_host(env):
    key, _ = run(create_control_key(env.store, "nas01"))
    ok = env.pull(key, "nas01")
    assert ok.status_code == 200 and ok.json() == {"host": "nas01", "commands": [], "cancel": []}
    assert env.pull(key, "nas02").status_code == 403
    assert env.pull(key, "").status_code == 403
    assert env.pull(None).status_code == 401
    assert env.pull("wpc_nope_nope").status_code == 401


def test_revoked_wpc_key_is_refused_on_pull(env):
    key, info = run(create_control_key(env.store, "nas01"))
    assert env.pull(key).status_code == 200
    assert run(revoke_key(env.store, info.prefix))
    assert env.pull(key).status_code == 401


def test_wpc_key_is_refused_on_host_ingest(env):
    key, _ = run(create_control_key(env.store, "nas01"))  # the batch is for host nas01
    r = env.client.post("/api/ingest", json=HOST_BATCH, headers=bearer(key))
    assert r.status_code == 401
    swapped = "wpi_" + key.split("_", 1)[1]
    assert env.client.post("/api/ingest", json=HOST_BATCH,
                           headers=bearer(swapped)).status_code == 401
    assert env.rows("SELECT COUNT(*) FROM host_samples") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM hosts") == [(0,)]
    assert env.rows("SELECT last_used FROM ingest_keys") == [(None,)]


def test_wpc_key_is_refused_on_field_reports(env):
    key, _ = run(create_control_key(env.store, "sean-pixel"))
    for method, path in (("post", "/api/v1/field-reports"), ("get", "/api/v1/field-reports/ping")):
        r = getattr(env.client, method)(path, content=b"{}", headers=bearer(key)) \
            if method == "post" else env.client.get(path, headers=bearer(key))
        assert r.status_code == 401, path
    swapped = "wpf_" + key.split("_", 1)[1]
    assert env.client.post("/api/v1/field-reports", content=b"{}",
                           headers=bearer(swapped)).status_code == 401
    assert env.rows("SELECT last_used FROM ingest_keys") == [(None,)]


def test_wpi_and_wpf_keys_are_refused_on_control_endpoints(env):
    host_key, _ = run(create_key(env.store, "nas01"))
    field_key, _ = run(create_field_key(env.store, "nas01"))  # even with a matching label
    for key in (host_key, field_key,
                "wpc_" + host_key.split("_", 1)[1], "wpc_" + field_key.split("_", 1)[1]):
        assert env.pull(key).status_code == 401
    assert env.rows("SELECT last_used FROM ingest_keys") == [(None,), (None,)]


def test_wpc_key_is_refused_on_session_routes_and_admin_routes(env):
    key, _ = run(create_control_key(env.store, "nas01"))
    for path in ("/api/admin/keys", "/api/plugins/pockethernet/reports"):
        assert env.client.get(path, headers=bearer(key)).status_code in (401, 403), path
    assert env.client.post("/api/admin/keys", json={"host": "h", "scope": "wpc"},
                           headers=bearer(key)).status_code in (401, 403)
    assert run(list_keys(env.store))[0].last_used is None


def test_admin_creates_and_revokes_wpc_keys(env):
    hdr = env.admin()
    made = env.client.post("/api/admin/keys", json={"host": "nas01", "scope": "wpc"},
                           headers=hdr)
    assert made.status_code == 200
    body = made.json()
    assert body["scope"] == "wpc" and body["key"].startswith("wpc_")
    assert env.pull(body["key"]).status_code == 200
    listing = env.client.get("/api/admin/keys")
    assert [(k["host"], k["scope"]) for k in listing.json()] == [("nas01", "wpc")]
    assert body["key"] not in listing.text
    revoked = env.client.post(f"/api/admin/keys/{body['id']}/revoke", headers=hdr)
    assert revoked.status_code == 200
    assert env.pull(body["key"]).status_code == 401
    assert len(env.rows("SELECT id FROM audit WHERE kind='key_created'")) == 1
    assert len(env.rows("SELECT id FROM audit WHERE kind='key_revoked'")) == 1


def test_wpc_scope_is_not_issuable_when_the_plugin_is_not_listed(tmp_path):
    path = str(tmp_path / "w.db")
    store = Store(path)
    clock = Clock()
    cfg2 = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                       server={"db_path": path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
                               "argon2_parallelism": 1})
    alerter = Alerter(cfg2)
    client = TestClient(create_app(cfg2, store, Scheduler(cfg2, store, alerter), alerter,
                                   plugins=load_plugins(cfg2, _entry_points()),
                                   auth_clock=clock), base_url="https://testserver")
    try:
        run(auth.create_user(store, cfg2, "root", PASSWORD, True, now=clock()))
        r = client.post("/api/login", json={"username": "root", "password": PASSWORD})
        r = client.post("/api/admin/keys", json={"host": "h", "scope": "wpc"},
                        headers={"X-CSRF-Token": r.json()["csrf"]})
        assert r.status_code == 422
    finally:
        client.close()
        store.close()


# ---- CLI --------------------------------------------------------------------------------

def test_cli_keygen_prints_only_the_public_key(tmp_path, capsys):
    from observe.__main__ import _control_keygen
    path = tmp_path / "cli.key"
    assert _control_keygen(str(path)) == 0
    out = capsys.readouterr()
    public = out.out.strip()
    assert out.out.count("\n") == 1 and re.fullmatch(r"ed25519:[A-Za-z0-9+/]{43}=", public)
    assert "PRIVATE" not in out.out + out.err
    assert public_key_string(load_private_key(path)) == public
    assert _control_keygen(str(path)) == 2  # never overwrites
    assert "already exists" in capsys.readouterr().err


def test_main_registers_control_keygen(tmp_path, monkeypatch, capsys):
    from observe.__main__ import main
    path = tmp_path / "main.key"
    monkeypatch.setattr("sys.argv", ["observe", "--config", str(tmp_path / "none.yaml"),
                                     "--control-keygen", str(path)])
    assert main() == 0
    out = capsys.readouterr().out.strip()
    assert out == public_key_string(load_private_key(path))


def test_keygen_removes_a_partial_file_when_the_write_fails(tmp_path, monkeypatch):
    import observe_control.signing as signing
    path = tmp_path / "partial.key"

    class Boom:
        def __init__(self, fd):
            self.fd = fd

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            os.close(self.fd)

        def write(self, data):
            raise OSError("disk full")

    monkeypatch.setattr(signing.os, "fdopen", lambda fd, mode: Boom(fd))
    with pytest.raises(OSError):
        keygen(path)
    assert not path.exists()
    monkeypatch.undo()
    keygen(path)  # a retry works without manual cleanup


def test_cli_creates_and_revokes_wpc_keys(tmp_path, monkeypatch, capsys):
    import observe.__main__ as cli
    path = tmp_path / "control.key"
    keygen(path)
    db = tmp_path / "cli.db"
    cfg = tmp_path / "c.yaml"
    lines = ["server:", f"  db_path: {db.as_posix()}", "  argon2_time_cost: 1",
             "  argon2_memory_kib: 8", "  argon2_parallelism: 1", "monitors:",
             "  - {name: p, type: ping, host: 127.0.0.1}", "plugins: [control]",
             "plugin_settings:", f"  control: {{signing_key_file: '{path.as_posix()}'}}"]
    cfg.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    real = cli.load_plugins
    monkeypatch.setattr(cli, "load_plugins", lambda config: real(config, _entry_points()))

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["observe", "--config", str(cfg), *argv])
        return cli.main()

    assert run("--ingest-key-create", "nas01", "--ingest-key-scope", "wpc") == 0
    key = capsys.readouterr().out.strip()
    assert key.startswith("wpc_")
    prefix = key.split("_")[1]
    assert run("--ingest-key-revoke", prefix) == 0
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT scope, revoked_at FROM ingest_keys").fetchone()
    finally:
        conn.close()
    assert row[0] == "wpc" and row[1] is not None


# ---- documentation and writing rules ----------------------------------------------------

def test_spec_carries_the_test_vector():
    spec = (ROOT / "docs" / "CONTROL.md").read_text(encoding="utf-8")
    for value in (VECTOR_PUBLIC, VECTOR_SIGNATURE, VECTOR_CANONICAL,
                  base64.b64encode(VECTOR_SEED).decode()):
        assert value in spec


OWNED = [ROOT / "plugins" / "control" / "pyproject.toml",
         *sorted((ROOT / "plugins" / "control" / "observe_control").glob("*.py")),
         ROOT / "tests" / "test_control_keys.py", ROOT / "docs" / "CONTROL.md"]


def test_files_use_lf_and_the_writing_rules():
    from .test_field_docs import stored_bytes
    for path in OWNED:
        raw = stored_bytes(path)
        assert b"\r" not in raw, path
        text = raw.decode("utf-8")
        assert chr(0x2014) not in text, path
        assert not re.search("|".join(("cla" + "ude", "op" + "us", "son" + "net", "hai" + "ku")),
                             text, re.I), path


def test_control_doc_carries_the_test_vector_and_follows_the_writing_rules():
    from .test_field_docs import stored_bytes
    names = ("docs/CONTROL.md", "THREAT-MODEL.md", "README.md")
    for name in names[:2]:
        raw = stored_bytes(ROOT / name)
        assert b"\r" not in raw, name
        assert chr(0x2014) not in raw.decode("utf-8"), name
    doc = stored_bytes(ROOT / "docs/CONTROL.md").decode("utf-8")
    for value in (base64.b64encode(VECTOR_SEED).decode(), VECTOR_PUBLIC, VECTOR_CANONICAL,
                  VECTOR_SIGNATURE, json.dumps(VECTOR_COMMAND, separators=(",", ":"))):
        assert value in doc
    for step in ("--control-keygen", "--ingest-key-scope wpc", "watchpost_public_key"  # legacy name):
        assert step in doc
