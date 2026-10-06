"""Infrastructure map core: port keys, switch ids, the service API and the migration."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from observe.infra import InfraError, InfraService, UnknownPropertyError
from observe.portkey import lldp_port_key, port_key, switch_id
from observe.storage.schema import MIGRATIONS, SCHEMA_VERSION
from observe.store import Store

from .test_store_schema import make_main_schema_db, tables
from .dbq import run_sql

INFRA = {"infra_switches", "infra_ports", "infra_jacks", "infra_links", "infra_endpoints",
         "port_properties"}

# Every row is one physical port; all spellings in a row must give one key.
SAME = [
    ("gi1/0/5", ["Gi1/0/5", "GigabitEthernet1/0/5", "gigabitethernet 1/0/5", " GI1/0/5 ", "Gig1/0/5"]),
    ("te1/0/5", ["Te1/0/5", "TenGigabitEthernet1/0/5", "tengige1/0/5"]),
    ("eth1/5", ["Ethernet1/5", "Eth1/5", "Et1/5"]),
    ("fa0/1", ["Fa0/1", "FastEthernet0/1"]),
    ("po1", ["Po1", "Port-channel1", "port-channel 1"]),
    ("ge-0/0/5", ["ge-0/0/5", "GE-0/0/5", "ge-0/0/5.0"]),
    ("xe-1/2/3:1", ["xe-1/2/3:1", "xe-1/2/3:1.0"]),
    ("port5", ["Port 5", "port5", "PORT  5"]),
    ("eth0", ["eth0", "ETH0", "Eth0"]),
    ("enp3s0", ["enp3s0", "ENP3S0"]),
    ("mgmt0", ["mgmt0", "Management0"]),
]

# Pairs that look alike but are different ports and must never merge.
DISTINCT = [
    ("Gi1/0/5", "Gi1/0/50"), ("Gi1/0/5", "Te1/0/5"), ("Gi1/0/5", "Gi0/1/5"),
    ("Gi1/0/5", "Fa1/0/5"), ("Gi1/0/5", "Gi1/0/5.100"), ("Port 5", "Port 50"),
    ("Port 5", "5"), ("eth0", "eth0.100"), ("eth0", "eth1"), ("ge-0/0/5", "ge-0/0/5.1"),
    ("ge-0/0/5", "xe-0/0/5"), ("ge-0/0/5", "ge-0/0/50"), ("xe-1/2/3", "xe-1/2/3:1"),
    ("et-0/0/1", "Et0/0"), ("Vlan1", "Gi1/0/1"), ("Po1", "Po10"), ("enp3s0", "enp3s1"),
]


@pytest.mark.parametrize("key,spellings", SAME)
def test_spellings_of_one_port_share_a_key(key, spellings):
    assert {port_key(s) for s in spellings} == {key}


@pytest.mark.parametrize("a,b", DISTINCT)
def test_look_alike_ports_stay_distinct(a, b):
    assert port_key(a) != port_key(b)


@pytest.mark.parametrize("bad", ["", "   ", "Gi1/0/5\n", "a\x00b", "a|b", "x" * 200])
def test_bad_port_names_are_refused(bad):
    with pytest.raises(ValueError):
        port_key(bad)


def test_port_key_is_idempotent():
    for _, spellings in SAME:
        for s in spellings:
            assert port_key(port_key(s)) == port_key(s)


LLDP = [
    (5, "GigabitEthernet1/0/5", "gi1/0/5"),
    ("interface_name", "Gi1/0/5", "gi1/0/5"),
    (1, "Port 5", "port5"),
    (7, "ge-0/0/5.0", "ge-0/0/5"),
    (3, "AA:BB:CC:DD:EE:FF", "mac:aabbccddeeff"),
    (3, "aa-bb-cc-dd-ee-ff", "mac:aabbccddeeff"),
    (6, "0A:1B", "circuit:0a1b"),
    (4, "192.0.2.7", "addr:192.0.2.7"),
    (2, "Slot1", "pc:slot1"),
]


@pytest.mark.parametrize("subtype,value,key", LLDP)
def test_lldp_port_ids(subtype, value, key):
    assert lldp_port_key(subtype, value) == key


def test_lldp_subtypes_never_collide_with_interface_names():
    assert lldp_port_key(3, "aabbccddeeff") != port_key("aabbccddeeff")
    assert lldp_port_key(4, "eth0") != port_key("eth0")
    assert lldp_port_key(2, "eth0") != lldp_port_key(4, "eth0")


@pytest.mark.parametrize("subtype,value", [(99, "x"), ("nope", "x"), (3, "not a mac"), (5, "")])
def test_bad_lldp_port_ids_are_refused(subtype, value):
    with pytest.raises(ValueError):
        lldp_port_key(subtype, value)


def test_switch_id_prefers_the_chassis_mac_and_keeps_kinds_apart():
    assert switch_id("AA:BB:CC:DD:EE:FF", "core") == "mac:aabbccddeeff"
    assert switch_id(sys_name="Core  SW-1") == "name:core sw-1"
    assert switch_id(sys_name="aabbccddeeff") != switch_id("aa:bb:cc:dd:ee:ff")
    for kwargs in ({}, {"chassis_mac": "nope"}, {"sys_name": "a|b"}, {"sys_name": " "}):
        with pytest.raises(ValueError):
            switch_id(**kwargs)


# Service ---------------------------------------------------------------------------------

SW = switch_id("aa:bb:cc:dd:ee:ff")


@pytest.fixture
def infra(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    yield InfraService(store)
    store.close()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def seed(infra: InfraService) -> None:
    run(infra.upsert_switch(SW, name="core", now=1.0))
    run(infra.upsert_port(SW, "GigabitEthernet1/0/5", now=1.0))


def test_property_history_appends_and_identical_value_only_bumps(infra):
    seed(infra)
    first = run(infra.append_property(SW, "Gi1/0/5", "link_speed_mbps", 1000, unit="Mbps",
                                      source="field", report_id="r1", observed_at=100.0, now=101.0))
    same = run(infra.append_property(SW, "Gi1/0/5", "link_speed_mbps", 1000, unit="Mbps",
                                     source="field", report_id="r2", observed_at=200.0, now=201.0))
    changed = run(infra.append_property(SW, "Gi1/0/5", "link_speed_mbps", 100, unit="Mbps",
                                        source="field", report_id="r3", observed_at=300.0, now=301.0))
    assert (first, same, changed) == (True, False, True)
    history = run(infra.property_history(SW, "gi1/0/5", "link_speed_mbps"))
    assert [h["value"] for h in history] == [100, 1000]  # newest first, two rows not three
    assert history[1]["report_id"] == "r1"
    assert history[1]["last_verified"] == 200.0  # moved to the identical report's observation time
    assert history[1]["observed_at"] == 100.0  # the original observation is kept
    current = run(infra.current_properties(SW, "Gi1/0/5"))
    assert current["link_speed_mbps"]["value"] == 100


def test_a_value_that_returns_is_a_new_row(infra):
    seed(infra)
    for rid, v in (("a", 1000), ("b", 100), ("c", 1000)):
        run(infra.append_property(SW, "Gi1/0/5", "link_speed_mbps", v, source="field", report_id=rid))
    assert [h["value"] for h in run(infra.property_history(SW, "Gi1/0/5", "link_speed_mbps"))] == [
        1000, 100, 1000]


def test_unknown_property_name_is_rejected_and_nothing_is_stored(infra):
    seed(infra)
    for name in ("made_up", "Custom.x", "custom.", "custom.Bad Name", "custom.x-y", ""):
        with pytest.raises(UnknownPropertyError):
            run(infra.append_property(SW, "Gi1/0/5", name, "x", source="field", recorded_by="a"))
    assert run_sql(infra._store, "SELECT COUNT(*) FROM port_properties") == [(0,)]


def test_property_values_are_typed(infra):
    seed(infra)
    bad = [("link_speed_mbps", "1000"), ("link_speed_mbps", True), ("link_speed_mbps", 1.5),
           ("dhcp_ok", 1), ("vlan", None), ("pair_1_2_length_m", float("nan")),
           ("room", 5), ("room", "x" * 600), ("room", "a\nb")]
    for name, value in bad:
        with pytest.raises(InfraError):
            run(infra.append_property(SW, "Gi1/0/5", name, value, source="field"))
    assert run(infra.append_property(SW, "Gi1/0/5", "pair_1_2_length_m", 12, source="field"))
    assert run(infra.current_properties(SW, "Gi1/0/5"))["pair_1_2_length_m"]["value"] == 12.0


def test_property_needs_an_existing_port_and_source(infra):
    seed(infra)
    with pytest.raises(InfraError):
        run(infra.append_property(SW, "Gi1/0/6", "vlan", 10, source="field"))
    with pytest.raises(InfraError):
        run(infra.append_property(SW, "Gi1/0/5", "vlan", 10, source=""))


def test_custom_property_is_audited_and_needs_an_actor(infra):
    seed(infra)
    with pytest.raises(InfraError):
        run(infra.append_property(SW, "Gi1/0/5", "custom.owner", "ops", source="admin"))
    assert run(infra.append_property(SW, "Gi1/0/5", "custom.owner", "ops", source="admin",
                                     recorded_by="sean"))
    rows = run_sql(infra._store, "SELECT actor, kind, detail FROM audit")
    assert [(r[0], r[1]) for r in rows] == [("sean", "port_property_custom")]
    assert "ops" not in rows[0][2]  # the audit row names the property, not its value
    typed = run(infra.append_property(SW, "Gi1/0/5", "vlan", 10, source="field"))
    assert typed and len(run_sql(infra._store, "SELECT 1 FROM audit")) == 1  # typed writes are not audited


def test_upserts_refresh_without_blanking_and_normalise(infra):
    run(infra.upsert_switch(SW, name="core", vendor="Cisco", mgmt_addresses=["192.0.2.1"], now=1.0))
    run(infra.upsert_switch(SW, now=5.0))
    run(infra.upsert_port(SW, "Gi1/0/5", raw_port_id="Gi1/0/5", if_index=10005, role="access", now=1.0))
    key = run(infra.upsert_port(SW, "GigabitEthernet1/0/5", unifi_index=5, now=5.0))
    assert key == "gi1/0/5"
    sw = run_sql(infra._store, "SELECT name, vendor, mgmt_addresses, first_seen, last_seen "
                            "FROM infra_switches")
    assert sw == [("core", "Cisco", '["192.0.2.1"]', 1.0, 5.0)]
    port = run_sql(infra._store, "SELECT raw_port_id, if_index, unifi_index, role, first_seen, "
                              "last_seen FROM infra_ports")
    assert port == [("Gi1/0/5", 10005, 5, "access", 1.0, 5.0)]


def test_port_needs_a_known_switch_and_valid_fields(infra):
    with pytest.raises(InfraError):
        run(infra.upsert_port(SW, "Gi1/0/5"))
    run(infra.upsert_switch(SW))
    for kwargs in ({"role": "core"}, {"if_index": -1}, {"unifi_index": True}):
        with pytest.raises(InfraError):
            run(infra.upsert_port(SW, "Gi1/0/5", **kwargs))
    with pytest.raises(InfraError):
        run(infra.upsert_port("core-switch", "Gi1/0/5"))
    with pytest.raises(InfraError):
        run(infra.upsert_port(SW, "bad|name"))


def test_jack_endpoint_and_link_round_trip(infra):
    seed(infra)
    run(infra.upsert_switch(switch_id(sys_name="dist")))
    run(infra.upsert_port(switch_id(sys_name="dist"), "Te1/1/1"))
    jack = run(infra.upsert_jack("hq/a/101/p1/07", room="101", site="hq", switch=SW, port="Gi1/0/5"))
    ep = run(infra.upsert_endpoint("field", "phone-1", mac="aa:bb:cc:00:00:01"))
    assert run(infra.upsert_endpoint("field", "phone-1")) == ep  # the same endpoint, not a new one
    link = run(infra.upsert_link(infra.jack_ref(jack), infra.port_ref(SW, "Gi1/0/5"),
                                 source="field_report", confidence=0.6, now=10.0))
    again = run(infra.upsert_link(infra.port_ref(SW, "gigabitethernet1/0/5"), infra.jack_ref(jack),
                                  source="field_report", confidence=0.9, now=20.0))
    assert again == link  # direction and spelling do not make a second edge
    uplink = run(infra.upsert_link(infra.port_ref(SW, "Gi1/0/5"),
                                   infra.port_ref(switch_id(sys_name="dist"), "Te1/1/1"),
                                   source="lldp"))
    other_source = run(infra.upsert_link(infra.jack_ref(jack), infra.port_ref(SW, "Gi1/0/5"),
                                         source="config"))
    assert len({link, uplink, other_source}) == 3
    row = run_sql(infra._store, "SELECT confidence, first_seen, last_seen, closed_at FROM infra_links "
                             "WHERE id=?", (link,))
    assert row == [(0.9, 10.0, 20.0, None)]
    run(infra.upsert_link(infra.jack_ref(jack), infra.endpoint_ref(ep), source="field_report"))
    jack_row = run_sql(infra._store, "SELECT room, site, switch_id, port_key FROM infra_jacks")
    assert jack_row == [("101", "hq", SW, "gi1/0/5")]


def test_link_refuses_unknown_ends_and_bad_fields(infra):
    seed(infra)
    port = infra.port_ref(SW, "Gi1/0/5")
    for a, b, kw in [
        (port, ("jack", "nope"), {"source": "lldp"}),
        (port, ("endpoint", "99"), {"source": "lldp"}),
        (port, infra.port_ref(SW, "Gi1/0/9"), {"source": "lldp"}),
        (port, port, {"source": "lldp"}),
        (port, ("switch", "x"), {"source": "lldp"}),
        (port, ("jack", "nope"), {"source": "guess"}),
        (port, ("jack", "nope"), {"source": "lldp", "confidence": 2}),
    ]:
        with pytest.raises(InfraError):
            run(infra.upsert_link(a, b, **kw))
    assert run_sql(infra._store, "SELECT COUNT(*) FROM infra_links") == [(0,)]


def test_jack_patch_needs_both_halves_and_an_existing_port(infra):
    seed(infra)
    with pytest.raises(InfraError):
        run(infra.upsert_jack("j1", switch=SW))
    with pytest.raises(InfraError):
        run(infra.upsert_jack("j1", switch=SW, port="Gi1/0/9"))
    with pytest.raises(InfraError):
        run(infra.upsert_jack(" "))


# Migration -------------------------------------------------------------------------------

def make_phase5_db(path: str) -> None:
    """A database as the previous phase (schema version 5) left it, with rows in every area."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.commit()
    from observe.storage.schema import migrate
    old = {v: s for v, s in MIGRATIONS.items() if v > 5}
    for v in old:
        del MIGRATIONS[v]
    try:
        migrate(db)
    finally:
        MIGRATIONS.update(old)
    db.execute("INSERT INTO results VALUES ('a', 1.0, 'ok', 1.5, 2.0, 'fine')")
    db.execute("INSERT INTO hosts (host, first_seen, last_seen) VALUES ('h1', 1.0, 2.0)")
    db.execute("INSERT INTO audit (ts, actor, kind) VALUES (1.0, 'admin', 'login_ok')")
    db.execute("INSERT INTO plugin_schema VALUES ('echo', 1)")
    db.commit()
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 5
    db.close()


def test_phase5_database_migrates_keeping_rows(tmp_path):
    path = str(tmp_path / "w.db")
    make_phase5_db(path)
    assert not INFRA & tables(path)
    store = Store(path)
    counts = {t: run_sql(store, f"SELECT COUNT(*) FROM {t}")[0][0]
              for t in ("results", "hosts", "audit", "plugin_schema", *INFRA)}
    store.close()
    assert counts == {"results": 1, "hosts": 1, "audit": 1, "plugin_schema": 1,
                      **{t: 0 for t in INFRA}}
    assert INFRA <= tables(path)
    assert SCHEMA_VERSION == 17  # 15 is the change sequences, 16 the series tables, 17 the summary levels
    assert "infra_dependencies" in tables(path)


def test_main_schema_database_gets_the_infra_tables(tmp_path):
    path = str(tmp_path / "w.db")
    make_main_schema_db(path)
    Store(path).close()
    assert INFRA <= tables(path)


def test_infra_step_is_safe_to_rerun(tmp_path):
    path = str(tmp_path / "w.db")
    store = Store(path)
    infra = InfraService(store)
    run(infra.upsert_switch(SW))
    run(infra.upsert_port(SW, "Gi1/0/5"))
    run(infra.append_property(SW, "Gi1/0/5", "vlan", 10, source="field"))
    for stmt in MIGRATIONS[6]:
        run_sql(store, stmt)
    assert run_sql(store, "SELECT COUNT(*) FROM port_properties") == [(1,)]
    store.close()


def test_version_10_database_with_enrolments_survives_the_reports_migration(tmp_path):
    path = str(tmp_path / "w.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.commit()
    from observe.storage.schema import migrate
    newer = {v: s for v, s in MIGRATIONS.items() if v > 10}
    for v in newer:
        del MIGRATIONS[v]
    try:
        migrate(db)
    finally:
        MIGRATIONS.update(newer)
    db.execute("INSERT INTO enrolments (host, platform, agent, control, token_hash, created,"
               " expires_at, fetched_at) VALUES ('pending1', 'linux', 1, 0, 'h1', 1.0, 9.0, NULL)")
    db.execute("INSERT INTO enrolments (host, platform, agent, control, token_hash, created,"
               " expires_at, fetched_at) VALUES ('fetched1', 'linux', 1, 1, 'h2', 1.0, 9.0, 2.0)")
    db.commit()
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 10
    assert "step_hash" not in {r[1] for r in db.execute("PRAGMA table_info(enrolments)")}
    db.close()
    store = Store(path)
    rows = run_sql(store, "SELECT host, fetched_at, step_hash, reports FROM enrolments ORDER BY host")
    store.close()
    assert [tuple(r) for r in rows] == [("fetched1", 2.0, None, "[]"),
                                        ("pending1", None, None, "[]")]
    db = sqlite3.connect(path)
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 17
    db.close()
