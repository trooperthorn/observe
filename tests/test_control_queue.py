"""The control command queue, pull and results (docs/CONTROL.md): own-host pulls, seq, expiry,
rate limits, result ownership, redaction and audit rows."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from importlib.metadata import EntryPoint

import pytest
from fastapi.testclient import TestClient

from watchpost import auth
from watchpost.alerts import Alerter
from watchpost.ingest.keys import create_key
from watchpost.plugins import GROUP, load_plugins
from watchpost.scheduler import Scheduler
from watchpost.store import Store
from watchpost.web import create_app
from watchpost_control.keys import create_control_key
from watchpost_control.queue import (MAX_OUTPUT_CHARS, Limits, QueueError, clean_output,
                                     enqueue_command, expire_commands)
from watchpost_control.signing import keygen, load_private_key, verify_command

from .conftest import make_config
from .test_auth import PASSWORD, Clock

FLOOR = {"controller": "thermalctl", "header": "pwm2", "min_duty": 20}


def run(coro):
    return asyncio.run(coro)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class Env:
    def __init__(self, tmp_path, **settings) -> None:
        self.path = str(tmp_path / "w.db")
        key_file = tmp_path / "control.key"
        self.public = keygen(key_file)
        self.signing = load_private_key(key_file)
        self.cfg = make_config(
            [{"name": "p", "type": "ping", "host": "127.0.0.1"}], plugins=["control"],
            plugin_settings={"control": {"signing_key_file": str(key_file), **settings}},
            server={"db_path": self.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
                    "argon2_parallelism": 1})
        loaded = load_plugins(self.cfg, lambda: [EntryPoint(
            "control", "watchpost_control:plugin", GROUP)])
        model = loaded.get("control").settings.model_dump()
        self.limits = Limits(**{k: v for k, v in model.items() if k in Limits.__dataclass_fields__})
        self.store = Store(self.path, loaded)
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        self.clock = Clock()
        self.client = TestClient(
            create_app(self.cfg, self.store, sched, alerter, plugins=loaded,
                       auth_clock=self.clock), base_url="https://testserver")
        self._keys: dict[str, str] = {}

    def enqueue(self, host="nas01", action="fan.set_floor", params=None, by="sean"):
        return run(enqueue_command(self.store, self.signing, self.limits, host, action,
                                   FLOOR if params is None else params, by, self.clock.now))

    def key(self, host="nas01"):
        if host not in self._keys:
            self._keys[host] = run(create_control_key(self.store, host))[0]
        return self._keys[host]

    def pull(self, key, host="nas01"):
        return self.client.get("/api/v1/control/commands", params={"host": host},
                               headers=bearer(key))

    def result(self, key, body):
        return self.client.post("/api/v1/control/results", content=json.dumps(body),
                                headers=bearer(key))

    def answer(self, key, body, host="nas01"):
        """Pull as the host would (a result is accepted only for a pulled command), then post."""
        assert self.pull(key, host).status_code == 200
        return self.result(key, body)

    def rows(self, sql, *args):
        db = sqlite3.connect(self.path)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def audit(self, kind):
        return [json.loads(r[0]) for r in self.rows(
            "SELECT detail FROM audit WHERE kind=? ORDER BY id", kind)]

    def state(self, cid):
        return self.rows("SELECT state FROM control_commands WHERE id=?", cid)[0][0]

    def close(self):
        self.client.close()
        self.store.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


# ---- pull -------------------------------------------------------------------------------

def test_pull_returns_only_own_unexpired_signed_commands(env):
    mine = env.enqueue("nas01")
    env.enqueue("nas02")
    env.enqueue("nas01", "service.restart", {"name": "nut-monitor"})
    got = env.pull(env.key("nas01")).json()
    assert got["host"] == "nas01"
    assert [c["command"]["action"] for c in got["commands"]] == ["fan.set_floor",
                                                                 "service.restart"]
    assert all(c["command"]["host"] == "nas01" for c in got["commands"])
    first = got["commands"][0]
    assert first["command"] == mine["command"] and first["signature"] == mine["signature"]
    for c in got["commands"]:
        assert verify_command(env.public, c["command"], c["signature"])
    # Past expiry nothing is returned.
    env.clock.now += env.limits.command_ttl_s
    assert env.pull(env.key("nas01")).json()["commands"] == []


def test_pull_marks_pulled_and_stays_until_final(env):
    cmd = env.enqueue()["command"]
    key = env.key()
    assert env.state(cmd["id"]) == "requested"
    assert len(env.pull(key).json()["commands"]) == 1
    assert env.state(cmd["id"]) == "pulled"
    assert len(env.pull(key).json()["commands"]) == 1  # not final, still offered
    env.result(key, {"id": cmd["id"], "state": "done"})
    assert env.pull(key).json()["commands"] == []


def test_seq_increases_per_host_and_hosts_are_independent(env):
    a1 = env.enqueue("nas01")["command"]
    b1 = env.enqueue("nas02")["command"]
    a2 = env.enqueue("nas01", "service.restart", {"name": "x"})["command"]
    a3 = env.enqueue("nas01", "fan.set_mode", {"controller": "thermalctl", "mode": "active"})
    assert [a1["seq"], a2["seq"], a3["command"]["seq"]] == [1, 2, 3]
    assert b1["seq"] == 1


def test_pull_audits_first_delivery_only(env):
    key = env.key()
    env.pull(key)
    assert env.audit("control_pull") == []  # an empty poll writes nothing
    cmd = env.enqueue()["command"]
    env.pull(key)
    env.pull(key)
    rows = env.audit("control_pull")
    assert len(rows) == 1 and rows[0]["command_ids"] == [cmd["id"]] and rows[0]["host"] == "nas01"
    (actor,), = env.rows("SELECT actor FROM audit WHERE kind='control_pull'")
    assert actor and actor != key and key not in actor  # the key prefix, never the key


# ---- results ----------------------------------------------------------------------------

def test_result_records_outcome_redacted_truncated_with_timings(env):
    cmd = env.enqueue()["command"]
    key = env.key()
    env.pull(key)
    secret = "wpc_abcdef123456_" + "s" * 32
    out = f"ok token=hunter2 {secret} " + "line of output " * 400
    env.pull(key)
    r = env.result(key, {"id": cmd["id"], "state": "done", "output": out,
                         "started_at": 100.0, "finished_at": 102.5})
    assert r.status_code == 200 and r.json() == {"id": cmd["id"], "state": "done"}
    assert env.state(cmd["id"]) == "done"
    (output, truncated, duration), = env.rows(
        "SELECT output, output_truncated, duration_s FROM control_results")
    assert "hunter2" not in output and secret not in output and "s" * 20 not in output
    assert "token=[redacted]" in output
    assert len(output) == MAX_OUTPUT_CHARS and truncated == 1 and duration == 2.5
    audit = env.audit("plugin_request")
    assert audit[-1]["outcome"] == "done" and audit[-1]["command_id"] == cmd["id"]
    assert "hunter2" not in json.dumps(audit)


def test_result_for_another_hosts_command_is_refused_and_audited(env):
    other = env.enqueue("nas02")["command"]
    key = env.key("nas01")
    r = env.result(key, {"id": other["id"], "state": "done"})
    assert r.status_code == 404
    assert env.state(other["id"]) == "requested"
    assert env.rows("SELECT COUNT(*) FROM control_results") == [(0,)]
    unknown = env.result(key, {"id": "no-such-id", "state": "done"})
    assert unknown.status_code == 404 and unknown.json() == r.json()  # same answer, no probing
    rows = env.audit("plugin_request")
    assert [d["outcome"] for d in rows] == ["refused", "refused"]
    assert rows[0]["command_id"] == other["id"] and rows[0]["device"] == "nas01"
    assert env.rows("SELECT status FROM audit WHERE kind='plugin_request'") == [(404,), (404,)]


def test_result_is_final_once_and_bad_results_are_refused(env):
    cmd = env.enqueue()["command"]
    key = env.key()
    assert env.answer(key, {"id": cmd["id"], "state": "failed",
                            "output": "boom"}).status_code == 200
    assert env.result(key, {"id": cmd["id"], "state": "done"}).status_code == 409
    assert env.state(cmd["id"]) == "failed"
    c2 = env.enqueue("nas01", "service.restart", {"name": "x"})["command"]
    env.pull(key)
    for bad in ({"id": c2["id"], "state": "cancelled"}, {"id": c2["id"], "state": "unknown"},
                {"id": c2["id"], "state": "done", "extra": 1},
                {"id": c2["id"], "state": "done", "started_at": 5, "finished_at": 1},
                {"id": c2["id"], "state": "done", "started_at": -1},
                {"id": c2["id"], "state": "done", "output": 5}, {"state": "done"}):
        assert env.result(key, bad).status_code == 422, bad
    assert env.client.post("/api/v1/control/results", content=b"{nope",
                           headers=bearer(key)).status_code == 422
    assert env.state(c2["id"]) == "pulled"


def test_scheduled_then_done_is_two_results_for_one_command(env):
    cmd = env.enqueue("nas01", "host.reboot", {})["command"]
    key = env.key()
    env.pull(key)
    assert env.result(key, {"id": cmd["id"], "state": "scheduled"}).status_code == 200
    assert env.state(cmd["id"]) == "scheduled"
    env.clock.now += 10_000  # a scheduled command has been answered, so expiry does not apply
    assert env.pull(key).json()["commands"] == []  # and it is not served again
    assert env.state(cmd["id"]) == "scheduled"
    assert env.result(key, {"id": cmd["id"], "state": "done"}).status_code == 200
    assert env.rows("SELECT COUNT(*) FROM control_results") == [(2,)]


def test_oversized_result_is_refused(env):
    key = env.key()
    big = json.dumps({"id": "x", "state": "done", "output": "a" * 300_000})
    r = env.client.post("/api/v1/control/results", content=big, headers=bearer(key))
    assert r.status_code == 413


def test_wrong_scope_keys_cannot_post_results(env):
    wpi, _ = run(create_key(env.store, "nas01"))
    cmd = env.enqueue()["command"]
    assert env.result(wpi, {"id": cmd["id"], "state": "done"}).status_code == 401
    assert env.state(cmd["id"]) == "requested"


# ---- expiry -----------------------------------------------------------------------------

def test_expiry_without_result_is_unknown_never_done(env):
    cmd = env.enqueue()["command"]
    key = env.key()
    env.pull(key)
    env.clock.now = cmd["expires_at"] - 1
    assert run(expire_commands(env.store, env.clock.now)) == []
    assert env.state(cmd["id"]) == "pulled"
    env.clock.now = cmd["expires_at"]
    assert run(expire_commands(env.store, env.clock.now)) == [cmd["id"]]
    assert env.state(cmd["id"]) == "unknown"
    assert env.audit("control_expired") == [{"command_id": cmd["id"], "state": "unknown"}]
    # A late result cannot turn it into done.
    late = env.result(key, {"id": cmd["id"], "state": "done"})
    assert late.status_code == 409 and env.state(cmd["id"]) == "unknown"
    assert env.rows("SELECT COUNT(*) FROM control_results") == [(0,)]


def test_admin_list_shows_unknown_after_expiry(env):
    cmd = env.enqueue()["command"]
    env.clock.now = cmd["expires_at"] + 5
    assert env.client.get("/api/plugins/control/commands").status_code == 401
    run(auth.create_user(env.store, env.cfg, "root", PASSWORD, True, now=env.clock()))
    assert env.client.post("/api/login", json={"username": "root",
                                               "password": PASSWORD}).status_code == 200
    listed = env.client.get("/api/plugins/control/commands").json()["commands"]
    assert [(c["id"], c["state"]) for c in listed] == [(cmd["id"], "unknown")]
    assert listed[0]["result"] is None


# ---- rate limits ------------------------------------------------------------------------

def test_one_pending_per_host_and_action(env):
    env.enqueue("nas01")
    with pytest.raises(QueueError) as err:
        env.enqueue("nas01")
    assert err.value.status == 429
    env.enqueue("nas02")  # another host is independent
    env.enqueue("nas01", "service.restart", {"name": "x"})  # another action is independent
    assert env.audit("control_request_refused")[0]["reason"].endswith("pending for this host")


def test_pending_clears_after_a_result_or_expiry(env):
    first = env.enqueue()["command"]
    env.answer(env.key(), {"id": first["id"], "state": "done"})
    second = env.enqueue()["command"]
    assert second["seq"] == 2
    env.clock.now = second["expires_at"]
    assert env.enqueue()["command"]["seq"] == 3


def test_ten_commands_per_host_per_hour(env):
    for i in range(10):
        cmd = env.enqueue("nas01", params={**FLOOR, "min_duty": 20 + i})["command"]
        env.answer(env.key("nas01"), {"id": cmd["id"], "state": "done"})
        env.clock.now += 5
    with pytest.raises(QueueError, match="too many commands"):
        env.enqueue("nas01")
    assert env.enqueue("nas02")["command"]["seq"] == 1
    env.clock.now += 3600
    assert env.enqueue("nas01")["command"]["seq"] == 11


def test_reboot_once_per_fifteen_minutes(env):
    first = env.enqueue("nas01", "host.reboot", {})["command"]
    env.answer(env.key(), {"id": first["id"], "state": "done"})
    env.clock.now += 600
    with pytest.raises(QueueError, match="reboot was already requested"):
        env.enqueue("nas01", "host.reboot", {})
    env.clock.now += 300
    assert env.enqueue("nas01", "host.reboot", {})["command"]["seq"] == 2


def test_configured_limits_apply(tmp_path):
    e = Env(tmp_path, max_commands_per_host_per_hour=2, max_pending_per_action=2,
            command_ttl_s=30)
    try:
        c1 = e.enqueue()["command"]
        assert c1["expires_at"] - c1["issued_at"] == 30
        e.enqueue("nas01", "service.restart", {"name": "x"})
        with pytest.raises(QueueError, match="too many commands"):
            e.enqueue("nas01", "fan.set_mode", {"controller": "thermalctl", "mode": "active"})
    finally:
        e.close()


def test_invalid_requests_are_refused_and_audited(env):
    cases = (("", "fan.set_floor", {}), ("a b", "fan.set_floor", {}),
             ("nas01", "rm.everything", {}), ("nas01", "host.reboot", []),
             ("nas01", "host.reboot", {"x": float("nan")}),
             ("nas01", "host.reboot", {"x": "a" * 3000}))
    for host, action, params in cases:
        with pytest.raises(QueueError):
            env.enqueue(host, action, params)
    assert env.rows("SELECT COUNT(*) FROM control_commands") == [(0,)]
    assert len(env.audit("control_request_refused")) == len(cases)


def test_request_is_audited_with_who_what_and_params(env):
    cmd = env.enqueue(by="sean")["command"]
    (row,) = env.audit("control_requested")
    assert row["command_id"] == cmd["id"] and row["params"] == FLOOR and row["seq"] == 1
    assert env.rows("SELECT actor FROM audit WHERE kind='control_requested'") == [("sean",)]
    assert "signature" not in json.dumps(row)


def test_clean_output_redacts_and_caps():
    text, cut = clean_output("Bearer abc.def password: s3cret wpf_x_y\x00ok")
    assert "abc.def" not in text and "s3cret" not in text and "wpf_x_y" not in text
    assert "\x00" not in text and not cut
    long, cut = clean_output("ab " * MAX_OUTPUT_CHARS)
    assert len(long) == MAX_OUTPUT_CHARS and cut


@pytest.mark.parametrize("secret", [
    "Bearer abc.def.ghi",
    "wpi_abc_123", "wpc_abc_123", "wpf_abc_123", "hw_abc_123",
    "password=hunter2", "token=abc123xyz",
    "Authorization: Basic dXNlcjpwYXNz",
    "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG\n-----END PRIVATE KEY-----",
    "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5+/==",
    "0123456789abcdef0123456789abcdef0123456789abcdef",
])
def test_each_secret_shape_is_redacted_in_stored_output(secret):
    text, _ = clean_output(f"before {secret} after")
    for part in ("abc.def.ghi", "abc_123", "hunter2", "abc123xyz", "dXNlcjpwYXNz",
                 "MIIEvQ", "QUJDREVG", "0123456789abcdef0123"):
        assert part not in text
    assert "before" in text


def test_redaction_runs_before_truncation():
    secret = "wpi_" + "a" * 50
    text, cut = clean_output("x" * (MAX_OUTPUT_CHARS - 10) + secret)
    assert "wpi_" not in text and "aaaa" not in text


# ---- committed files --------------------------------------------------------------------

def test_new_control_files_use_lf_and_no_em_dashes_or_model_names():
    from .test_field_docs import ROOT, stored_bytes
    plugin = ROOT / "plugins" / "control" / "watchpost_control"
    for path in (plugin / "queue.py", plugin / "__init__.py", ROOT / "docs" / "CONTROL.md"):
        raw = stored_bytes(path)
        text = raw.decode("utf-8")
        assert b"\r" not in raw and "\u2014" not in text, path
        assert not any(w in text.lower() for w in ("claude", "opus", "sonnet", "haiku")), path
