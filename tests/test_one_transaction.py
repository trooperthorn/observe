"""Slice O-5: feed cycles in one transaction with a SAVEPOINT per item, and the current map
tables (`map_nodes`, `map_edges`, `port_current`) kept at write time.

Every case runs on SQLite, on the PostgreSQL dialect fake and, when OBSERVE_TEST_PG_DSN is set,
on a live PostgreSQL. The contract: one cycle is one commit; an item that fails is skipped and
the rest of the cycle is kept; the map tables are written in the cycle's own transaction; and a
map GET takes at most five statements, however many ports there are."""

from __future__ import annotations

import json
from typing import Any

import pytest

from observe.infra import InfraService, InfraTx
from observe.infra_map import MapService
from observe.infra_match import Matcher
from observe.portkey import switch_id
from observe.storage import DB_ERRORS, open_storage, savepoint
from observe.store import Store
from observe_pockethernet import reports as pocket_reports
from observe_pockethernet.derive import footprint
from observe_pockethernet.reports import MIGRATIONS as POCKET_MIGRATIONS
from observe_pockethernet.reports import NewReport, store_and_derive
from observe_pockethernet.schema import parse_report
from observe_unifi.feed import feed_classic, feed_integration
from observe_unifi.records import MIGRATIONS as UNIFI_MIGRATIONS
from observe_unifi.records import parse_device

from .conftest import make_config
from .fakes.pg_fake import PgFakeStorage
from .test_pockethernet_upload import FIXTURE, TAKEN_S, dump
from .test_storage import live_pg
from .test_unifi_feed import MAC1, NOW, SID1, SID2, classic_data, parsed
from .test_unifi_plugin import device

PLUGINS = {"pockethernet": POCKET_MIGRATIONS, "unifi": UNIFI_MIGRATIONS}
MONITORS = [{"name": "core sw", "type": "ping", "host": "10.0.0.1"}]
MAC3 = "AA:BB:CC:00:00:03"
SID3 = switch_id(MAC3)


@pytest.fixture(params=["sqlite", "postgres-fake", "postgres"])
def storage(request, tmp_path):
    if request.param == "postgres":
        with live_pg(plugins=PLUGINS) as s:  # skipped without OBSERVE_TEST_PG_DSN
            yield s
        return
    s = (PgFakeStorage(PLUGINS) if request.param == "postgres-fake"
         else open_storage(str(tmp_path / "s.db"), PLUGINS))
    yield s
    s.close()


@pytest.fixture
def store(storage):
    st = Store.__new__(Store)
    st.storage = storage
    st.map_stale_days = 90
    return st


class Units:
    """Counts the write units (one transaction, one commit each) a block of code submits."""

    def __init__(self, storage: Any) -> None:
        self.count = 0
        original = storage.write

        async def write(unit: Any, *, touches: Any = ()) -> Any:
            self.count += 1
            return await original(unit, touches=touches)
        storage.write = write


class Counting:
    """A connection that counts the statements a read sends."""

    def __init__(self, db: Any, sink: list[str]) -> None:
        self._inner, self._sink = db, sink

    def execute(self, sql: str, args: Any = ()) -> Any:
        self._sink.append(sql)
        return self._inner.execute(sql, args)


def count_reads(storage: Any) -> list[str]:
    sink: list[str] = []
    original = storage.read

    async def read(unit: Any) -> Any:
        return await original(lambda db: unit(Counting(db, sink)))
    storage.read = read
    return sink


def make_map(store: Store, state_of: Any = None) -> tuple[MapService, Matcher, InfraService]:
    cfg = make_config(MONITORS)
    infra = InfraService(store)
    matcher = Matcher(cfg, infra)
    return MapService(cfg, infra, matcher, state_of or (lambda slug: None), lambda: NOW), \
        matcher, infra


async def rows(storage: Any, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    return await storage.fetchall(sql, args)


# ---- savepoints ---------------------------------------------------------------------------

async def test_a_savepoint_undoes_one_item_and_keeps_the_rest(storage):
    def insert(db: Any, key: str) -> None:
        db.execute("INSERT INTO app_settings (key, value, updated) VALUES (?, '1', 1)", (key,))

    def unit(db: Any) -> None:
        insert(db, "a")
        with pytest.raises(RuntimeError):
            with savepoint(db):
                insert(db, "b")
                raise RuntimeError("item failed")
        with savepoint(db):
            insert(db, "c")

    await storage.write(unit)
    assert await rows(storage, "SELECT key FROM app_settings ORDER BY key") == [("a",), ("c",)]


async def test_a_database_error_in_one_item_does_not_poison_the_transaction(storage):
    def insert(db: Any, key: str) -> None:
        db.execute("INSERT INTO app_settings (key, value, updated) VALUES (?, '1', 1)", (key,))

    def unit(db: Any) -> None:
        insert(db, "a")
        with pytest.raises(DB_ERRORS):
            with savepoint(db):
                insert(db, "a")  # a duplicate key aborts a PostgreSQL transaction
        insert(db, "b")

    await storage.write(unit)
    assert await rows(storage, "SELECT key FROM app_settings ORDER BY key") == [("a",), ("b",)]


# ---- one commit per cycle -----------------------------------------------------------------

async def test_a_classic_cycle_is_one_commit_and_writes_the_map_with_it(store, storage):
    units = Units(storage)
    res = await feed_classic(store, parsed(classic_data()), NOW)
    assert units.count == 1
    assert (res.switches, res.skipped) == (2, 0)
    assert storage.change_seq("unifi") == 1
    assert storage.change_seq("map") == 1 and storage.change_seq("ports") == 1
    ids = {r[0] for r in await rows(storage, "SELECT id FROM map_nodes")}
    assert {f"switch:{SID1}", f"switch:{SID2}", f"port:{SID1}|port25",
            f"port:{SID2}|port7"} <= ids
    assert await rows(storage, "SELECT COUNT(*) FROM map_edges") == [(2,)]
    # The port summary holds the current values the feed wrote, in the same commit.
    ((attrs,),) = await rows(storage, "SELECT attrs FROM port_current WHERE switch_id=? AND "
                                      "port_key='port1'", SID1)
    assert json.loads(attrs)["link_speed_mbps"]["value"] == 1000


async def test_an_integration_cycle_with_its_device_rows_is_one_commit(store, storage):
    units = Units(storage)
    devices = [parse_device("s", device(1, uplink={"deviceId": "dev-2"})),
               parse_device("s", device(2))]
    res = await feed_integration(store, devices, NOW, save_devices=True)
    assert units.count == 1 and (res.switches, res.links) == (2, 1)
    assert await rows(storage, "SELECT COUNT(*) FROM unifi_devices") == [(2,)]
    assert await rows(storage, "SELECT COUNT(*) FROM map_edges") == [(1,)]
    assert storage.change_seq("unifi") == 1 and storage.change_seq("map") == 1


async def test_an_unchanged_cycle_keeps_the_map_counters(store, storage):
    await feed_classic(store, parsed(classic_data()), NOW)
    before = (storage.change_seq("map"), storage.change_seq("ports"))
    units = Units(storage)
    await feed_classic(store, parsed(classic_data()), NOW)
    assert units.count == 1
    assert (storage.change_seq("map"), storage.change_seq("ports")) == before


# ---- a failed item is skipped and the others are kept -------------------------------------

async def test_a_failing_classic_device_is_skipped_and_the_others_are_kept(store, storage):
    devs = parsed([*classic_data(), {"mac": MAC3, "name": "Broken", "port_table": [],
                                     "lldp_table": [], "uplink": None}])
    devs[2]["ports"] = {"1": {"name": "Port 1", "up": True, "speed_mbps": 1000}, "2": None}
    units = Units(storage)
    res = await feed_classic(store, devs, NOW)
    assert units.count == 1
    assert res.skipped == 1
    # Both good devices are complete, including their links.
    assert await rows(storage, "SELECT COUNT(*) FROM infra_ports WHERE switch_id IN (?, ?)",
                      SID1, SID2) == [(4,)]
    assert await rows(storage, "SELECT COUNT(*) FROM infra_links") == [(2,)]
    # The broken device keeps its switch row, but the port that came before its bad port was
    # rolled back with the device, so nothing of its half-finished work is left.
    assert await rows(storage, "SELECT COUNT(*) FROM infra_switches WHERE switch_id=?",
                      SID3) == [(1,)]
    assert await rows(storage, "SELECT COUNT(*) FROM infra_ports WHERE switch_id=?",
                      SID3) == [(0,)]
    assert await rows(storage, "SELECT COUNT(*) FROM port_properties WHERE switch_id=?",
                      SID3) == [(0,)]
    assert await rows(storage, "SELECT COUNT(*) FROM map_nodes WHERE id=?",
                      f"switch:{SID3}") == [(1,)]


async def test_a_failing_integration_device_is_skipped_and_the_others_are_kept(store, storage):
    devices = [parse_device("s", device(1, uplink={"deviceId": "dev-2"})),
               parse_device("s", device(2)),
               parse_device("s", device(3, name="x" * 300))]
    units = Units(storage)
    res = await feed_integration(store, devices, NOW)
    assert units.count == 1
    assert (res.switches, res.skipped, res.links) == (2, 1, 1)
    assert await rows(storage, "SELECT COUNT(*) FROM infra_switches") == [(2,)]


# ---- the map tables and the read -----------------------------------------------------------

async def test_the_map_read_takes_at_most_five_statements_whatever_the_size(store, storage):
    mapper, _, _ = make_map(store)
    small = parsed([{"mac": MAC1, "name": "S", "port_table": [], "lldp_table": [],
                     "uplink": None}])
    small[0]["ports"] = {"1": {"name": "Port 1", "up": True, "speed_mbps": 1000}}
    await feed_classic(store, small, NOW)
    await mapper.rebuild(NOW)
    sink = count_reads(storage)
    await mapper.map_data()
    few = len(sink)

    big = parsed(classic_data())
    for dev in big:
        dev["ports"] = {str(i): {"name": f"Port {i}", "up": True, "speed_mbps": 1000}
                        for i in range(1, 61)}
    await feed_classic(store, big, NOW)
    await mapper.rebuild(NOW)
    sink.clear()
    await mapper.map_data()
    await mapper.map_data("hq")
    assert few <= 5 and len(sink) == 2 * few
    assert await rows(storage, "SELECT COUNT(*) FROM port_current") == [(120,)]


async def test_a_get_never_writes_the_map_tables(store, storage):
    mapper, _, _ = make_map(store)
    await feed_classic(store, parsed(classic_data()), NOW)
    units = Units(storage)
    seqs = storage.change_seqs()
    await mapper.map_data()
    assert units.count == 0 and storage.change_seqs() == seqs


async def test_live_state_survives_a_write_time_rebuild(store, storage):
    mapper, matcher, _ = make_map(
        store, lambda slug: ("up", None) if slug == "core-sw" else None)
    await feed_classic(store, parsed(classic_data()), NOW)
    await matcher.link_switch(SID2, "core-sw", "admin")
    await mapper.rebuild(NOW)
    assert await rows(storage, "SELECT state FROM map_nodes WHERE id=?",
                      f"switch:{SID2}") == [("up",)]
    # A later cycle rebuilds the structure without the hook, and the live column is carried.
    changed = parsed(classic_data())
    changed[0]["ports"]["1"]["native_vlan"] = 30
    await feed_classic(store, changed, NOW + 60)
    assert await rows(storage, "SELECT state FROM map_nodes WHERE id=?",
                      f"switch:{SID2}") == [("up",)]
    data = await mapper.map_data()
    core = next(n for n in data["nodes"] if n["id"] == f"switch:{SID2}")
    assert (core["monitor"], core["state"]) == ("core-sw", "up")
    (attrs,) = (r[0] for r in await rows(
        storage, "SELECT attrs FROM port_current WHERE switch_id=? AND port_key='port1'", SID1))
    assert json.loads(attrs)["vlan"]["value"] == 30


async def test_an_infra_write_updates_the_map_in_its_own_unit(store, storage):
    mapper, _, infra = make_map(store)
    await infra.upsert_switch(SID1, name="one", now=NOW)
    await infra.upsert_port(SID1, "Gi1/0/1", role="uplink", now=NOW)
    assert await rows(storage, "SELECT label FROM map_nodes") == [("one",)]
    await infra.upsert_jack("hq/b1/r1/p1/01", room="r1", site="hq", switch=SID1,
                            port="Gi1/0/1", now=NOW)
    data = await mapper.map_data()
    assert {n["kind"] for n in data["nodes"]} == {"switch", "port", "jack"}
    assert data["nodes"][2]["building"] == "b1"


# ---- Pockethernet: one upload, one commit ---------------------------------------------------

def new_report(**changes: Any) -> tuple[NewReport, Any]:
    body = dump({**FIXTURE, **changes})
    report = parse_report(body)
    site = report.site
    return NewReport(
        source="phone", report_id=report.report_id, revision=report.revision,
        taken_at_ms=report.taken_at_ms, reported_taken_at_ms=report.taken_at_ms,
        clock_corrected=False, tester_serial=report.device.serial, status=report.status,
        site=site.site if site else "", port_id=site.port_id if site else "", body=body,
        received_at=TAKEN_S + 100, key_prefix="wpf_test"), report


async def test_an_upload_is_stored_derived_and_mapped_in_one_commit(store, storage):
    mapper, _, _ = make_map(store)
    new, report = new_report()
    units = Units(storage)
    got = await store_and_derive(store, new, report, now=TAKEN_S + 100)
    assert units.count == 1
    assert got.outcome.result == "accepted" and got.derived is not None
    assert not got.derived.skipped and got.derive_error == ""
    assert await rows(storage, "SELECT derive_status FROM field_reports") == [("ok",)]
    assert storage.change_seq("map") == 1
    ids = {r[0] for r in await rows(storage, "SELECT id FROM map_nodes")}
    assert any(i.startswith("jack:") for i in ids) and any(i.startswith("port:") for i in ids)
    # A resent copy is a duplicate: no derivation and no change to the map.
    got = await store_and_derive(store, new, report, now=TAKEN_S + 200)
    assert got.outcome.result == "duplicate" and got.derived is None
    assert storage.change_seq("map") == 1
    assert (await mapper.map_data())["nodes"]


async def test_a_failed_derivation_keeps_the_evidence_and_undoes_its_partial_rows(
        store, storage, monkeypatch):
    real = pocket_reports.derive_report_tx

    def half_then_boom(tx: InfraTx, *a: Any, **k: Any) -> None:
        tx.upsert_switch(SID3, name="half", now=TAKEN_S)
        raise RuntimeError("secret detail")
    monkeypatch.setattr(pocket_reports, "derive_report_tx", half_then_boom)
    new, report = new_report()
    units = Units(storage)
    got = await store_and_derive(store, new, report, now=TAKEN_S + 100)
    assert units.count == 1
    assert got.derive_error == "RuntimeError" and got.derived is None
    assert "secret" not in repr(got)
    assert await rows(storage, "SELECT derive_status, body IS NOT NULL FROM field_reports") == [
        ("failed", True)]
    assert await rows(storage, "SELECT COUNT(*) FROM infra_switches") == [(0,)]
    assert await rows(storage, "SELECT COUNT(*) FROM map_nodes") == [(0,)]
    monkeypatch.setattr(pocket_reports, "derive_report_tx", real)
    assert footprint(new.body).port is not None


async def test_a_replaced_revision_is_retracted_and_derived_in_the_same_commit(store, storage):
    first, report = new_report()
    await store_and_derive(store, first, report, now=TAKEN_S + 100)
    second, report2 = new_report(revision=2)
    units = Units(storage)
    got = await store_and_derive(store, second, report2, now=TAKEN_S + 200)
    assert units.count == 1
    assert got.outcome.result == "replaced" and got.derived is not None
    assert await rows(storage, "SELECT revision, derive_status FROM field_reports") == [
        (2, "ok")]


async def test_a_database_failure_in_the_feed_fails_the_cycle_and_is_not_skipped(
        store, storage, monkeypatch):
    def broken(self: Any, *a: Any, **k: Any) -> Any:
        raise DB_ERRORS[0]("disk full")

    monkeypatch.setattr(InfraTx, "upsert_switch", broken)
    with pytest.raises(DB_ERRORS):
        await feed_integration(store, [parse_device("s", device(1))], NOW)
    assert await rows(storage, "SELECT COUNT(*) FROM infra_switches") == [(0,)]


async def test_a_populated_version_17_database_gains_the_map_tables_and_fills_them(tmp_path):
    import sqlite3
    path = str(tmp_path / "v17.db")
    s = open_storage(path, PLUGINS)
    st = Store.__new__(Store)
    st.storage = s
    st.map_stale_days = 90
    await feed_classic(st, parsed(classic_data()), NOW)
    s.close()
    raw = sqlite3.connect(path)
    for table in ("map_nodes", "map_edges", "port_current"):
        raw.execute(f"DROP TABLE {table}")
    raw.execute("DELETE FROM schema_version WHERE version>=18")
    raw.commit()
    raw.close()
    s = open_storage(path, PLUGINS)
    try:
        st.storage = s
        assert await rows(s, "SELECT COUNT(*) FROM map_nodes") == [(0,)]
        assert await rows(s, "SELECT COUNT(*) FROM infra_switches") != [(0,)]
        mapper, _, _ = make_map(st)
        await mapper.tick()
        assert await rows(s, "SELECT COUNT(*) FROM map_nodes") != [(0,)]
    finally:
        s.close()
