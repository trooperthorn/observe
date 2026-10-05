"""Pockethernet reports become switches, ports, jacks, links and port properties, and a rebuild
from the stored reports reproduces them."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from watchpost import auth
from watchpost.portkey import switch_id

from .test_pockethernet_upload import FIXTURE, Env, run

SID = switch_id("00:11:22:33:44:55")
JACK = FIXTURE["site"]["port_id"]
PROPS = "SELECT switch_id, port_key, name, value, unit, source, report_id, observed_at, " \
        "recorded_at, recorded_by, last_verified FROM port_properties ORDER BY id"
LINKS = "SELECT a_kind, a_ref, b_kind, b_ref, source, confidence, first_seen, last_seen, " \
        "closed_at FROM infra_links ORDER BY a_ref, b_ref"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def second_port(env: Env, report_id: str, port: str = "Gi1/0/6", **changes: Any) -> dict[str, Any]:
    """The same jack, now seen on another port of the same switch."""
    n = json.loads(json.dumps(FIXTURE["neighbors"][0]))
    n["port_id"] = port
    n["lldp"]["port_id"] = port
    n["description"] = port
    return env.report(report_id=report_id, neighbors=[n], **changes)


def props(env: Env, name: str) -> list[tuple[Any, ...]]:
    return env.rows("SELECT port_key, value, unit, source, report_id, recorded_by, "
                    "observed_at, last_verified FROM port_properties WHERE name=? ORDER BY id",
                    name)


def test_report_creates_switch_port_jack_link_and_properties(env):
    assert env.post(FIXTURE).json()["result"] == "accepted"
    (sw,) = env.rows("SELECT switch_id, name, mgmt_addresses, vendor FROM infra_switches")
    assert sw == (SID, "sw-access-02", '["10.20.0.12"]', "Example Networks")
    (port,) = env.rows("SELECT switch_id, port_key, raw_port_id, role FROM infra_ports")
    assert port == (SID, "gi1/0/5", "Gi1/0/5", "access")
    (jack,) = env.rows("SELECT jack_key, room, site, switch_id, port_key FROM infra_jacks")
    assert jack == (JACK, "Room 204", "HQ", SID, "gi1/0/5")
    (link,) = env.rows("SELECT a_kind, a_ref, b_kind, b_ref, source, closed_at FROM infra_links")
    assert link == ("jack", JACK, "port", f"{SID}|gi1/0/5", "field_report", None)

    (speed,) = props(env, "link_speed_mbps")
    assert speed[:6] == ("gi1/0/5", "1000", "Mbps", "pockethernet", FIXTURE["report_id"],
                         f"{env.info.prefix}:sean-pixel")
    assert speed[6] == FIXTURE["taken_at_ms"] / 1000
    assert props(env, "poe_class")[0][1] == '"4"'  # the core keeps these as text
    assert props(env, "tester_serial")[0][1] == '"1234567"'
    assert props(env, "last_tested_at")[0][1] == str(float(FIXTURE["taken_at_ms"] / 1000))
    assert props(env, "pair_1_2_length_m")[0][1:3] == ("42.5", "m")
    assert props(env, "dhcp_ok")[0][1] == "true"
    assert props(env, "jack_label")[0][1] == json.dumps(JACK)
    names = {r[0] for r in env.rows("SELECT name FROM port_properties")}
    assert names == {n for n, v in FIXTURE["properties"].items() if v is not None
                     and n != "last_tested_at_ms"} | {"last_tested_at"}
    (audit,) = env.rows("SELECT detail FROM audit WHERE path=?", "/api/v1/field-reports")
    assert json.loads(audit[0])["derived"]["jack_linked"] is True


def test_second_report_on_another_port_repatches_the_jack(env):
    env.clock.now += 1
    env.post(FIXTURE)
    env.clock.now += 60
    assert env.post(second_port(env, "r-2")).json()["result"] == "accepted"
    (jack,) = env.rows("SELECT switch_id, port_key FROM infra_jacks")
    assert jack == (SID, "gi1/0/6")
    links = env.rows("SELECT b_ref, closed_at FROM infra_links ORDER BY b_ref")
    assert links == [(f"{SID}|gi1/0/5", env.clock.now), (f"{SID}|gi1/0/6", None)]
    # The old port keeps its history; the new port starts one.
    assert {r[0] for r in env.rows("SELECT port_key FROM port_properties")} == {
        "gi1/0/5", "gi1/0/6"}
    assert len(env.rows("SELECT 1 FROM infra_ports")) == 2


def test_identical_values_bump_last_verified_without_new_rows(env):
    env.post(FIXTURE)
    before = env.rows(PROPS)
    env.clock.now += 3600
    again = env.report(report_id="r-again", taken_at_ms=FIXTURE["taken_at_ms"] + 1_000_000)
    again["properties"] = {**FIXTURE["properties"], "last_tested_at_ms": again["taken_at_ms"]}
    assert env.post(again).json()["result"] == "accepted"
    after = env.rows(PROPS)
    changed = {r[2] for r in after[len(before):]}
    assert changed == {"last_tested_at"}  # the only value that differs
    speed = env.rows("SELECT last_verified, observed_at, report_id FROM port_properties "
                     "WHERE name='link_speed_mbps'")
    assert speed == [(env.clock.now, FIXTURE["taken_at_ms"] / 1000, FIXTURE["report_id"])]
    (links,) = [env.rows("SELECT COUNT(*), MAX(last_seen) FROM infra_links")]
    assert links == [(1, env.clock.now)]


def test_replaced_revision_derives_again_and_duplicate_does_not(env):
    env.post(FIXTURE)
    rows = env.rows(PROPS)
    assert env.post(FIXTURE).json()["result"] == "duplicate"
    assert env.rows(PROPS) == rows
    body = env.report(revision=2)
    body["properties"] = {**FIXTURE["properties"], "link_speed_mbps": 100}
    assert env.post(body).json()["result"] == "replaced"
    assert [r[1] for r in props(env, "link_speed_mbps")] == ["1000", "100"]


def test_report_without_a_neighbour_is_stored_but_derives_nothing(env):
    r = env.post(env.report(report_id="no-nb", neighbors=[]))
    assert r.json()["result"] == "accepted"
    assert env.rows("SELECT 1 FROM field_reports") != []
    assert env.rows("SELECT 1 FROM infra_switches") == []
    assert env.rows("SELECT 1 FROM port_properties") == []
    (a,) = env.rows("SELECT detail FROM audit WHERE path=?", "/api/v1/field-reports")
    assert "skipped" in json.loads(a[0])["derived"]


def test_cdp_neighbour_without_a_mac_uses_the_device_name(env):
    cdp = {"protocol": "cdp", "device_id": "Edge-SW9.lab", "port_id": "GigabitEthernet0/3",
           "cdp": {"version": 2, "ttl_s": 180, "device_id": "Edge-SW9.lab",
                   "addresses": ["10.9.9.9"], "port_id": "GigabitEthernet0/3",
                   "platform": "WS-C2960"}}
    env.post(env.report(report_id="cdp-1", neighbors=[cdp]))
    (sw,) = env.rows("SELECT switch_id, mgmt_addresses, platform FROM infra_switches")
    assert sw == ("name:edge-sw9.lab", '["10.9.9.9"]', "WS-C2960")
    assert env.rows("SELECT port_key FROM infra_ports") == [("gi0/3",)]
    assert env.rows("SELECT confidence FROM infra_links") == [(0.8,)]


def test_a_failed_derivation_does_not_fail_the_upload(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("secret detail")
    monkeypatch.setattr("watchpost_pockethernet.upload.derive_report", boom)
    r = env.post(FIXTURE)
    assert r.status_code == 200 and r.json()["result"] == "accepted"
    assert len(env.rows("SELECT 1 FROM field_reports")) == 1
    (a,) = env.rows("SELECT detail FROM audit WHERE path=?", "/api/v1/field-reports")
    detail = json.loads(a[0])
    assert detail["derive_failed"] == "RuntimeError" and "secret" not in a[0]


def snapshot(env: Env) -> dict[str, Any]:
    """Every derived table without autoincrement ids."""
    return {"props": env.rows(PROPS), "links": env.rows(LINKS),
            "jacks": env.rows("SELECT * FROM infra_jacks ORDER BY jack_key"),
            "ports": env.rows("SELECT * FROM infra_ports ORDER BY switch_id, port_key"),
            "switches": env.rows("SELECT * FROM infra_switches ORDER BY switch_id")}


def admin_headers(env: Env) -> dict[str, str]:
    async def go() -> dict[str, str]:
        await auth.create_user(env.store, env.cfg, "root", "correct horse battery", True)
        r = env.client.post("/api/login", json={"username": "root",
                                                "password": "correct horse battery"})
        return {"X-CSRF-Token": r.json()["csrf"]}
    return run(go())


def test_rebuild_reproduces_the_same_state_and_is_audited(env):
    env.clock.now += 1
    env.post(FIXTURE)
    env.clock.now += 60
    env.post(second_port(env, "r-2"))
    env.clock.now += 60
    again = env.report(report_id="r-3", taken_at_ms=FIXTURE["taken_at_ms"] + 5000)
    env.post(again)
    before = snapshot(env)
    assert before["props"] and len(before["links"]) == 2

    headers = admin_headers(env)
    r = env.client.post("/api/plugins/pockethernet/rebuild", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"reports": 3, "derived": 3, "skipped": 0, "failed": 0, "pruned": 0}
    assert snapshot(env) == before
    # Running it twice changes nothing either.
    assert env.client.post("/api/plugins/pockethernet/rebuild", headers=headers).status_code == 200
    assert snapshot(env) == before

    rows = env.rows("SELECT actor, detail FROM audit WHERE kind='plugin_request' "
                    "AND path='/api/plugins/pockethernet/rebuild'")
    assert [r[0] for r in rows] == ["root", "root"]
    assert json.loads(rows[0][1])["action"] == "rebuild"
    assert json.loads(rows[0][1])["derived"] == 3


def test_rebuild_drops_derived_rows_that_no_report_supports(env):
    env.post(FIXTURE)
    db = sqlite3.connect(env.path)
    db.execute("INSERT INTO port_properties (switch_id, port_key, name, value, source, "
               "observed_at, recorded_at, last_verified) "
               "VALUES (?,?,?,?,?,?,?,?)", (SID, "gi1/0/5", "vlan", "999", "pockethernet",
                                            1, 1, 1))
    db.commit()
    db.close()
    headers = admin_headers(env)
    assert env.client.post("/api/plugins/pockethernet/rebuild", headers=headers).status_code == 200
    assert [r[0] for r in env.rows("SELECT value FROM port_properties WHERE name='vlan'")] == ["20"]


def test_rebuild_needs_an_admin_and_a_csrf_token(env):
    url = "/api/plugins/pockethernet/rebuild"
    assert env.client.post(url).status_code in (401, 403)
    headers = admin_headers(env)
    assert env.client.post(url).status_code == 403  # logged in, but no CSRF token
    assert env.client.post(url, headers=headers).status_code == 200
    # A field key is not a session.
    assert env.client.post(url, headers={"Authorization": f"Bearer {env.key}"}).status_code in (
        401, 403)


def test_rebuild_refuses_when_retention_dropped_a_body(env):
    env.post(FIXTURE)
    before = snapshot(env)
    db = sqlite3.connect(env.path)
    db.execute("UPDATE field_reports SET body=NULL, body_pruned_at=1")
    db.commit()
    db.close()
    r = env.client.post("/api/plugins/pockethernet/rebuild", headers=admin_headers(env))
    assert r.status_code == 409
    assert snapshot(env) == before  # nothing was deleted


def test_field_report_never_overwrites_what_live_sources_know(env):
    env.post(FIXTURE)
    db = sqlite3.connect(env.path)
    db.execute("UPDATE infra_ports SET role='uplink' WHERE switch_id=?", (SID,))
    db.execute("UPDATE infra_switches SET name='core-sw', vendor='Live Vendor', "
               "platform='live-os', mgmt_addresses='[\"192.0.2.1\"]' WHERE switch_id=?", (SID,))
    db.commit()
    db.close()
    env.clock.now += 60
    env.post(second_port(env, "r-live", port="Gi1/0/5"))
    assert env.rows("SELECT role FROM infra_ports") == [("uplink",)]
    assert env.rows("SELECT name, vendor, platform, mgmt_addresses FROM infra_switches") == [
        ("core-sw", "Live Vendor", "live-os", '["192.0.2.1"]')]
    assert env.rows("SELECT last_seen FROM infra_switches")[0][0] > FIXTURE["taken_at_ms"] / 1000

    # A rebuild keeps them too.
    headers = admin_headers(env)
    assert env.client.post("/api/plugins/pockethernet/rebuild", headers=headers).status_code == 200
    assert env.rows("SELECT role FROM infra_ports") == [("uplink",)]
    assert env.rows("SELECT name FROM infra_switches") == [("core-sw",)]


def test_rebuild_after_a_replaced_revision_keeps_the_newest_values_and_drops_old_history(env):
    env.post(FIXTURE)
    env.clock.now += 60
    newer = env.report(revision=FIXTURE["revision"] + 1,
                       properties={**FIXTURE["properties"], "link_speed_mbps": 100})
    assert env.post(newer).json()["result"] == "replaced"
    assert [r[1] for r in props(env, "link_speed_mbps")] == ["1000", "100"]
    current = [l[:6] + l[8:] for l in snapshot(env)["links"]]

    r = env.client.post("/api/plugins/pockethernet/rebuild", headers=admin_headers(env))
    assert r.status_code == 200 and r.json()["derived"] == 1
    # Only the newest stored revision is replayed, so the superseded value is not recreated.
    assert [r[1] for r in props(env, "link_speed_mbps")] == ["100"]
    # The same edges come back; only the first-seen time moves to the replayed revision.
    assert [l[:6] + l[8:] for l in snapshot(env)["links"]] == current


def test_migration_2_upgrades_a_version_1_database_with_rows(tmp_path):
    from watchpost.store import migrate_plugins
    from watchpost_pockethernet.reports import MIGRATIONS

    db = sqlite3.connect(str(tmp_path / "old.db"))
    migrate_plugins(db, {"pockethernet": MIGRATIONS[:1]})
    assert "key_prefix" not in [r[1] for r in db.execute("PRAGMA table_info(field_reports)")]
    db.execute(
        "INSERT INTO field_reports (source, report_id, revision, taken_at_ms, "
        "reported_taken_at_ms, received_at, updated_at, status, body_sha256, body) "
        "VALUES ('sean-pixel', 'old-1', 1, 1000, 1000, 1.0, 1.0, 'pass', 'abc', x'7b7d')")
    db.commit()
    migrate_plugins(db, {"pockethernet": MIGRATIONS})
    assert db.execute("SELECT version FROM plugin_schema WHERE plugin='pockethernet'"
                      ).fetchone() == (2,)
    assert db.execute("SELECT report_id, key_prefix, body FROM field_reports").fetchall() == [
        ("old-1", "", b"{}")]
    db.close()
