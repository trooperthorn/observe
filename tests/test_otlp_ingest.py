"""OTLP ingest, POST /v1/metrics and /v1/logs: hand-built fixtures in both encodings, the host
binding, partial success, idempotency, limits, audit, and fuzzing of the decoder and the routes."""

from __future__ import annotations

import asyncio
import copy
import gzip
import json
import random
import sqlite3
import struct
import time

import pytest
from fastapi.testclient import TestClient

from observe.alerts import Alerter
from observe.ingest.api import DenialAggregator, RateLimiter
from observe.ingest.boot import CLEAN, CRASH, UNKNOWN, classify_boot
from observe.ingest.keys import create_key, revoke_key
from observe.ingest.schema import Batch, Event
from observe.otlp import wire
from observe.otlp.api import MAX_INFLATED_BYTES, BadRequest, inflate, too_deep
from observe.plugins import PluginBase, PluginError
from observe.scheduler import Scheduler
from observe.storage import StorageBusy
from observe.store import Store
from observe.web import create_app

from .conftest import make_config
from .dbq import SAMPLE_ROWS
from .otlp_build import (attrs, fixture, from_batch, gauge, histogram, kv, ld, log_record,
                         logs_request, metrics_request, number, post_batch, proto, tag, total)
from .test_pockethernet_otlp import _load

PROTO, JSON = "application/x-protobuf", "application/json"
T0 = 1_760_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Env:
    def __init__(self, tmp_path, rate: int = 120) -> None:
        self.path = str(tmp_path / "w.db")
        self.store = Store(self.path)
        self.cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                               server={"db_path": self.path, "ingest_rate_per_minute": rate})
        self.clock = Clock()
        alerter = Alerter(self.cfg)
        sched = Scheduler(self.cfg, self.store, alerter)
        self.client = TestClient(create_app(self.cfg, self.store, sched, alerter,
                                            ingest_clock=self.clock))

    def key(self, host: str) -> str:
        return asyncio.run(create_key(self.store, host))[0]

    def push(self, request, key: str | None, path: str = "/v1/metrics", encoding: str = "json",
             headers: dict[str, str] | None = None, gzipped: bool = False, **kw):
        """Post an OTLP request (a dict, or raw bytes) as JSON or protobuf."""
        if isinstance(request, (bytes, bytearray)):
            raw = bytes(request)
        elif encoding == "proto":
            raw = proto(request)
        else:
            raw = json.dumps(request, separators=(",", ":")).encode()
        h = {"Content-Type": PROTO if encoding == "proto" else JSON}
        if key:
            h["Authorization"] = f"Bearer {key}"
        if gzipped:
            raw, h["Content-Encoding"] = gzip.compress(raw), "gzip"
        return self.client.post(path, content=raw, headers={**h, **(headers or {})}, **kw)

    def rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        db = sqlite3.connect(self.path)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def stored(self) -> list[tuple]:
        return self.rows(f"SELECT host, source, metric, labels, value, unit FROM {SAMPLE_ROWS} "
                         "ORDER BY host, source, metric, labels")

    def close(self) -> None:
        self.client.close()
        self.store.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def rejected(resp) -> tuple[int, str]:
    ps = resp.json().get("partialSuccess", {})
    return int(ps.get("rejectedDataPoints", 0)), ps.get("errorMessage", "")


def simple(host: str = "nas01", value: float = 12.5, ts: float = T0, **res) -> dict:
    return metrics_request(host, {"rapl": [gauge("package_watts", [
        number(value, ts, {"package": "0"})], "W")]}, **res)


# ---- what is stored -------------------------------------------------------------------------

def test_a_json_request_is_stored(env):
    key = env.key("nas01")
    r = post_batch(env.client, fixture("batch_minimal"), key)
    assert r.status_code == 200, r.text
    assert r.json() == {}
    assert env.stored() == [
        ("nas01", "rapl", "dram_watts", "{}", None, "W"),
        ("nas01", "rapl", "package_watts", '{"package":"0"}', 12.5, "W")]
    sources = {s: (a, r) for _, s, a, r, _ in env.rows("SELECT * FROM host_sources")}
    assert sources["rapl"] == (1, "") and sources["mdraid"] == (0, "no md arrays")
    (host,) = env.rows("SELECT host, platform, agent_version, confirmed FROM hosts")
    assert host == ("nas01", "linux", "0.9.0", 0)
    assert env.rows("SELECT last_used FROM ingest_keys")[0][0] is not None


def test_protobuf_json_and_gzip_store_the_same_series(env):
    request = simple("nas01")
    other = simple("nas02")
    third = simple("nas03")
    for host, req, kw in (("nas01", request, {}), ("nas02", other, {"encoding": "proto"}),
                          ("nas03", third, {"encoding": "proto", "gzipped": True})):
        r = env.push(req, env.key(host), **kw)
        assert r.status_code == 200, (host, r.text)
    rows = [row[1:] for row in env.stored()]
    assert len(rows) == 3 and rows[0] == rows[1] == rows[2]
    assert {r[0] for r in env.stored()} == {"nas01", "nas02", "nas03"}
    assert env.push(simple("nas01"), env.key("nas01"), gzipped=True).status_code == 200


def test_the_same_data_through_the_old_store_path_and_otlp_is_the_same_series(env):
    """Data a producer pushes and data Observe builds itself land in the same series."""
    body = fixture("batch_with_events")
    post_batch(env.client, body, env.key("nas01"))
    direct = copy.deepcopy(body)
    direct["host"] = "nas02"
    batch = Batch.model_validate(direct)
    from observe.ingest.boot import classify_events
    asyncio.run(env.store.ingest_batch(batch, classify_events(batch.events)))
    via_otlp = [r[1:] for r in env.stored() if r[0] == "nas01"]
    via_store = [r[1:] for r in env.stored() if r[0] == "nas02"]
    assert via_otlp and via_otlp == via_store
    events = env.rows("SELECT host, kind, severity, source, title, dedup_key, boot_id "
                      "FROM host_events ORDER BY host, ts")
    assert [e[1:] for e in events if e[0] == "nas01"] == [e[1:] for e in events if e[0] == "nas02"]
    state = env.rows("SELECT host, boot_id, clean_shutdown FROM hosts ORDER BY host")
    assert state[0][1:] == state[1][1:]


def test_sum_and_histogram_points_and_the_no_value_flag(env):
    key = env.key("nas01")
    req = metrics_request("nas01", {"net": [
        total("rx_bytes", [number(500, T0, {"if": "eth0"}, as_int=True)], "By"),
        histogram("latency", 4, 2.5, T0, {"op": "read"}),
        gauge("temp", [number(None, T0, flags=1)], "Cel")]})
    assert env.push(req, key).status_code == 200
    got = {(r[2], r[3]): r[4] for r in env.stored()}
    assert got[("rx_bytes", '{"if":"eth0"}')] == 500.0
    assert got[("latency.count", '{"op":"read"}')] == 4.0
    assert got[("latency.sum", '{"op":"read"}')] == 2.5
    assert got[("temp", "{}")] is None


def test_point_attributes_become_string_labels_and_a_missing_time_is_now(env):
    key = env.key("nas01")
    dp = number(1.0, 0, {"n": 5, "ok": True, "f": 1.5, "s": "x"})
    del dp["timeUnixNano"]
    req = metrics_request("nas01", {"s": [gauge("m", [dp])]})
    before = time.time()
    assert env.push(req, key).status_code == 200
    (row,) = env.rows(f"SELECT labels, ts FROM {SAMPLE_ROWS}")
    assert json.loads(row[0]) == {"n": "5", "ok": "true", "f": "1.5", "s": "x"}
    assert before * 1000 - 1 <= row[1] <= time.time() * 1000 + 1


def test_a_source_is_unavailable_or_absent_from_the_status_gauges(env):
    key = env.key("nas01")
    status = [gauge("observe.source.available", [number(0.0, T0, {
        "observe.source": "smart", "observe.source.reason": "no permission"})]),
        gauge("observe.source.present", [number(1.0, T0, {"observe.source": "smart"})]),
        gauge("observe.source.available", [number(0.0, T0, {"observe.source": "mdraid"})]),
        gauge("observe.source.present", [number(0.0, T0, {"observe.source": "mdraid"})])]
    assert env.push(metrics_request("nas01", {"observe": status}), key).status_code == 200
    rows = {s: (a, r) for _, s, a, r, _ in env.rows("SELECT * FROM host_sources")}
    assert rows["smart"] == (0, "no permission")
    assert rows["mdraid"] == (0, "not present on this host")
    assert env.stored() == []


def test_unknown_fields_are_ignored_in_json_and_protobuf(env):
    key = env.key("nas01")
    req = simple()
    req["future"] = {"a": 1}
    req["resourceMetrics"][0]["schemaUrl"] = "https://example.invalid/1"
    req["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["description"] = "d"
    assert env.push(req, key).status_code == 200
    # protobuf: an unknown varint (field 99), fixed32 (98), fixed64 (97) and length field (96).
    junk = (tag(99, 0) + b"\x07" + tag(98, 5) + b"\0\0\0\0" + tag(97, 1)
            + b"\0" * 8 + tag(96, 2) + b"\x02ab")
    raw = proto(simple("nas01", ts=T0 + 15)) + junk
    assert env.push(raw, key, headers={"Content-Type": PROTO}).status_code == 200
    assert len(env.stored()) == 2


def test_a_literal_protobuf_request_decodes_and_is_stored(env):
    """Bytes written out by hand from opentelemetry-proto, not produced by any builder here."""
    kvp = b"\x0a\x09host.name" + b"\x12\x07\x0a\x05nas01"  # KeyValue{key, AnyValue{string}}
    resource = b"\x0a" + bytes([len(kvp)]) + kvp  # Resource{attributes}
    point = (b"\x19" + struct.pack("<Q", int(T0 * 1e9))  # time_unix_nano, field 3, fixed64
             + b"\x21" + struct.pack("<d", 12.5))  # as_double, field 4, fixed64
    gauge_msg = b"\x0a" + bytes([len(point)]) + point  # Gauge{data_points}
    metric = (b"\x0a\x0dpackage_watts" + b"\x1a\x01W"  # name, unit
              + b"\x2a" + bytes([len(gauge_msg)]) + gauge_msg)  # gauge = field 5
    scope = b"\x0a\x04rapl"
    sm = b"\x0a" + bytes([len(scope)]) + scope + b"\x12" + bytes([len(metric)]) + metric
    rm = b"\x0a" + bytes([len(resource)]) + resource + b"\x12" + bytes([len(sm)]) + sm
    request = b"\x0a" + bytes([len(rm)]) + rm
    decoded = wire.decode(request, "MetricsRequest")
    assert decoded == {"resourceMetrics": [{"resource": {"attributes": [
        {"key": "host.name", "value": {"stringValue": "nas01"}}]}, "scopeMetrics": [{
            "scope": {"name": "rapl"}, "metrics": [{"name": "package_watts", "unit": "W", "gauge": {
                "dataPoints": [{"timeUnixNano": str(int(T0 * 1e9)), "asDouble": 12.5}]}}]}]}]}
    assert env.push(request, env.key("nas01"), headers={"Content-Type": PROTO}).status_code == 200
    assert env.stored() == [("nas01", "rapl", "package_watts", "{}", 12.5, "W")]


def test_as_int_and_int_attributes_decode_from_protobuf(env):
    req = metrics_request("nas01", {"s": [gauge("m", [number(-7, T0, {"n": 5}, as_int=True)])]})
    decoded = wire.decode(proto(req), "MetricsRequest")
    dp = decoded["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"][0]
    assert dp["asInt"] == "-7"
    assert dp["attributes"][0]["value"] == {"intValue": "5"}
    assert env.push(req, env.key("nas01"), encoding="proto").status_code == 200
    assert env.stored()[0][4] == -7.0


# ---- events from log records ----------------------------------------------------------------

def test_log_records_become_events_with_boot_classification(env):
    key = env.key("nas01")
    assert post_batch(env.client, fixture("batch_with_events"), key).status_code == 200
    rows = env.rows("SELECT dedup_key, kind, severity, detail FROM host_events ORDER BY ts")
    by_key = {k: (kind, sev, json.loads(d)) for k, kind, sev, d in rows}
    assert by_key["boot:aaaa"][2]["classification"] == CLEAN
    assert by_key["boot:bbbb"][2]["classification"] == CRASH
    assert by_key["boot:bbbb"][2]["previous_boot_id"] == "aaaa"
    assert by_key["boot:bbbb"][1] == "critical"
    assert env.rows("SELECT boot_id, boot_ts, clean_shutdown FROM hosts") == [
        ("bbbb", 1759999000.0, 0)]


@pytest.mark.parametrize("kind,expected,flag", [
    ("boot.clean_shutdown", CLEAN, 1), ("boot.kernel_panic", CRASH, 0),
    ("boot.watchdog_reset", CRASH, 0), ("boot.power_loss", CRASH, 0),
    ("boot.unknown_unclean", CRASH, 0), ("boot.unknown", UNKNOWN, None),
    ("boot.agent_stopped", UNKNOWN, None), ("boot.from_the_future", UNKNOWN, None)])
def test_boot_kinds_reduce_to_three_states(env, kind, expected, flag):
    ev = Event(kind=kind, severity="info", source="boot", ts=5.0, title="t", dedup_key="boot:cc",
               boot_id="cc")
    assert classify_boot(ev) == expected
    req = logs_request("nas01", [log_record(kind, 5.0, "t", "info", observe__source="boot",
                                            observe__dedup_key="boot:cc", observe__boot_id="cc")])
    assert env.push(req, env.key("nas01"), "/v1/logs").status_code == 200
    assert env.rows("SELECT boot_id, clean_shutdown FROM hosts") == [("cc", flag)]


def test_a_non_boot_event_does_not_touch_boot_state(env):
    req = logs_request("nas01", [log_record("md.degraded", 5.0, "array degraded", "error",
                                            observe__source="mdraid", observe__dedup_key="md:1",
                                            disk="sda", count=3)])
    assert env.push(req, env.key("nas01"), "/v1/logs").status_code == 200
    assert env.rows("SELECT boot_id, clean_shutdown FROM hosts") == [(None, None)]
    (kind, sev, source, title, detail) = env.rows(
        "SELECT kind, severity, source, title, detail FROM host_events")[0]
    assert (kind, sev, source, title) == ("md.degraded", "critical", "mdraid", "array degraded")
    assert json.loads(detail) == {"disk": "sda", "count": 3}


def test_severity_comes_from_the_number_or_the_text(env):
    recs = [log_record("a", 5.0, "x", observe__dedup_key="1", number_=9),
            log_record("b", 6.0, "x", observe__dedup_key="2", number_=13),
            log_record("c", 7.0, "x", observe__dedup_key="3", number_=21),
            log_record("d", 8.0, "x", "WARN", observe__dedup_key="4", number_=9),
            log_record("e", 9.0, "x", "mystery", observe__dedup_key="5")]
    assert env.push(logs_request("nas01", recs), env.key("nas01"), "/v1/logs").status_code == 200
    got = dict(env.rows("SELECT kind, severity FROM host_events"))
    assert got == {"a": "info", "b": "warning", "c": "critical", "d": "warning", "e": "warning"}


def test_a_record_without_a_dedup_key_is_deduplicated_by_content(env):
    key = env.key("nas01")
    req = logs_request("nas01", [log_record("svc.restart", 5.0, "restarted", "info", unit="x")])
    assert env.push(req, key, "/v1/logs").status_code == 200
    # A resend with another Idempotency-Key is a new request, but the same record.
    assert env.push(req, key, "/v1/logs", headers={"Idempotency-Key": "second"}).status_code == 200
    assert env.rows("SELECT dedup_key FROM host_events")[0][0].startswith("otlp:")
    assert env.rows("SELECT COUNT(*) FROM host_events") == [(1,)]


def test_older_boot_event_does_not_overwrite_newer_state(env):
    key = env.key("nas01")
    assert post_batch(env.client, fixture("batch_with_events"), key).status_code == 200
    late = fixture("batch_minimal")
    late["events"] = [{"kind": "boot.clean_shutdown", "severity": "info", "source": "boot",
                       "ts": 1.0, "title": "old", "dedup_key": "boot:zzzz", "boot_id": "zzzz"}]
    late["batch_id"] = "late"
    assert post_batch(env.client, late, key).status_code == 200
    assert env.rows("SELECT boot_id, clean_shutdown FROM hosts") == [("bbbb", 0)]


def test_a_future_dated_boot_event_does_not_freeze_boot_state(env):
    key = env.key("nas01")
    future = {"kind": "boot.clean_shutdown", "severity": "info", "source": "boot",
              "ts": 4_000_000_000.0, "title": "bad clock", "dedup_key": "boot:future",
              "boot_id": "ffff"}
    first = fixture("batch_minimal")
    first["events"], first["batch_id"] = [future], "future-batch"
    assert post_batch(env.client, first, key).status_code == 200
    real = fixture("batch_minimal")
    real["sent_at"], real["batch_id"] = real["sent_at"] + 1, "real-batch"
    real["events"] = [{"kind": "boot.kernel_panic", "severity": "critical", "source": "boot",
                       "ts": time.time() + 1, "title": "real", "dedup_key": "boot:real",
                       "boot_id": "rrrr"}]
    assert post_batch(env.client, real, key).status_code == 200
    assert env.rows("SELECT boot_id, clean_shutdown FROM hosts") == [("rrrr", 0)]
    assert env.rows("SELECT MAX(ts) FROM host_events")[0][0] < 4_000_000_000.0


# ---- authentication and host binding --------------------------------------------------------

def test_missing_wrong_revoked_and_foreign_scope_keys_are_401_and_store_nothing(env):
    key = env.key("nas01")
    req = simple()
    assert env.push(req, None).status_code == 401
    forged = key[:-1] + ("A" if key[-1] != "A" else "B")
    assert env.push(req, forged).status_code == 401
    assert env.push(req, "wpi_nosuchprefix_secret").status_code == 401
    assert env.client.post("/v1/metrics", json=req,
                           headers={"Authorization": "Basic Zm9vOmJhcg=="}).status_code == 401
    assert asyncio.run(revoke_key(env.store, key.split("_")[1]))
    r = env.push(req, key)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM hosts") == [(0,)]


def test_read_and_control_keys_are_403_and_never_write(env):
    read, _ = asyncio.run(create_key(env.store, "script", scope="wpr", role="viewer"))
    control, _ = asyncio.run(create_key(env.store, "nas01", scope="wpc"))
    for key in (read, control):
        for path, req in (("/v1/metrics", simple()), ("/v1/logs", logs_request("nas01", []))):
            assert env.push(req, key, path).status_code == 403
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]
    assert env.rows("SELECT last_used FROM ingest_keys WHERE scope IN ('wpr','wpc')") == [
        (None,), (None,)]
    # The key must still be valid to earn a 403; a bad one is a 401.
    assert env.push(simple(), "wpr_nosuchprefix_secret").status_code == 401


def test_a_key_bound_to_another_host_is_refused_with_403(env):
    key = env.key("nas02")
    r = env.push(simple("nas01"), key)
    assert r.status_code == 403
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM hosts") == [(0,)]
    r = env.push(logs_request("nas01", [log_record("x.y", 1.0, "t")]), key, "/v1/logs")
    assert r.status_code == 403 and env.rows("SELECT COUNT(*) FROM host_events") == [(0,)]


def test_a_resource_without_host_name_is_refused(env):
    key = env.key("nas01")
    assert env.push(simple(None), key).status_code == 403
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]


def test_host_name_is_compared_ignoring_case_and_space(env):
    key = env.key("nas01")
    assert env.push(simple(" NAS01 "), key).status_code == 200
    assert [r[0] for r in env.stored()] == ["nas01"]  # stored under the host the key names


def test_one_wrong_resource_is_partial_success_and_the_rest_is_stored(env):
    key = env.key("nas01")
    good = simple("nas01")["resourceMetrics"][0]
    bad = metrics_request("nas02", {"rapl": [gauge("package_watts", [
        number(1.0, T0), number(2.0, T0 + 1), number(3.0, T0 + 2)])]})["resourceMetrics"][0]
    r = env.push({"resourceMetrics": [bad, good]}, key)
    assert r.status_code == 200
    count, message = rejected(r)
    assert count == 3 and "host.name" in message
    assert [x[0] for x in env.stored()] == ["nas01"]
    assert env.rows("SELECT host FROM hosts") == [("nas01",)]


def test_partial_success_is_in_the_encoding_of_the_request(env):
    key = env.key("nas01")
    bad = simple("nas02")
    r = env.push({"resourceMetrics": simple("nas01")["resourceMetrics"]
                  + bad["resourceMetrics"]}, key, encoding="proto")
    assert r.status_code == 200 and r.headers["content-type"] == PROTO
    top = parse_fields(r.content)
    inner = parse_fields(top[1][0])
    assert inner[1][0] == 1  # rejected_data_points
    assert b"host.name" in inner[2][0]
    clean = env.push(simple("nas01", ts=T0 + 30), key, encoding="proto")
    assert clean.status_code == 200 and clean.content == b""
    logs = env.push({"resourceLogs": logs_request("nas02", [log_record("a.b", 1.0, "t")])[
        "resourceLogs"] + logs_request("nas01", [log_record("a.b", 2.0, "t")])["resourceLogs"]},
        key, "/v1/logs", encoding="proto")
    assert logs.status_code == 200
    assert parse_fields(parse_fields(logs.content)[1][0])[1][0] == 1  # rejected_log_records


def parse_fields(data: bytes) -> dict[int, list]:
    """A tiny protobuf reader for the responses, written separately from the decoder."""
    out: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        tag = data[pos]
        pos += 1
        number_, kind = tag >> 3, tag & 7
        if kind == 0:
            value, shift = 0, 0
            while True:
                b = data[pos]
                pos += 1
                value |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
        else:
            assert kind == 2
            n = data[pos]
            pos += 1
            value = data[pos:pos + n]
            pos += n
        out.setdefault(number_, []).append(value)
    return out


# ---- partial success ------------------------------------------------------------------------

def test_bad_points_are_rejected_one_by_one(env):
    key = env.key("nas01")
    pts = [number(1.0, T0), number(2.0, T0 + 1)]
    bad = [{"timeUnixNano": "x", "asDouble": 1.0}, {"timeUnixNano": str(T0), "asDouble": "no"},
           {"timeUnixNano": "1760000000000000000"}, "text", 5,
           {"timeUnixNano": "1760000000000000000", "asDouble": 1.0, "attributes": [
               kv("a", {"arrayValue": {"values": []}})]},
           {"timeUnixNano": "1760000000000000000", "asDouble": 1.0, "attributes": [
               {"key": "", "value": {"stringValue": "x"}}]},
           {"timeUnixNano": "1760000000000000000", "asDouble": 1.0, "attributes": [
               kv("a", "v" * 1025)]},
           {"timeUnixNano": "1760000000000000000", "asDouble": 1.0,
            "attributes": [kv(f"k{i}", "v") for i in range(33)]}]
    req = metrics_request("nas01", {"s": [gauge("m", pts + bad)]})
    r = env.push(req, key)
    assert r.status_code == 200
    count, message = rejected(r)
    assert count == len(bad) and message
    assert len(env.stored()) == 2


def test_non_finite_values_are_rejected_per_point(env):
    key = env.key("nas01")
    raw = (b'{"resourceMetrics":[{"resource":{"attributes":[{"key":"host.name","value":'
           b'{"stringValue":"nas01"}}]},"scopeMetrics":[{"scope":{"name":"s"},"metrics":[{"name":'
           b'"m","gauge":{"dataPoints":[{"timeUnixNano":"1760000000000000000","asDouble":NaN},'
           b'{"timeUnixNano":"1760000000000000001","asDouble":Infinity},{"timeUnixNano":'
           b'"1760000000000000002","asDouble":1.5}]}}]}]}]}')
    r = env.push(raw, key)
    assert r.status_code == 200 and rejected(r)[0] == 2
    assert [x[4] for x in env.stored()] == [1.5]
    # In protobuf an infinite double is a valid encoding and is refused the same way.
    req = metrics_request("nas01", {"s": [gauge("m", [number(float("inf"), T0),
                                                      number(float("nan"), T0 + 1),
                                                      number(2.5, T0 + 2)])]})
    r = env.push(req, key, encoding="proto")
    assert rejected_proto(r) == 2
    assert sorted(x[4] for x in env.stored()) == [1.5, 2.5]


def rejected_proto(resp) -> int:
    return parse_fields(parse_fields(resp.content)[1][0])[1][0] if resp.content else 0


def test_metric_names_units_and_kinds_are_validated(env):
    key = env.key("nas01")
    p = [number(1.0, T0)]
    req = metrics_request("nas01", {"s": [
        gauge("Bad-Name", p), gauge("1abc", p), gauge("x" * 129, p), gauge("", p),
        gauge("fine.name_1", p, "u" * 33), gauge("ok.unit", p, "{requests}/s"),
        {"name": "exp", "exponentialHistogram": {"dataPoints": [{}, {}]}},
        {"name": "summ", "summary": {"dataPoints": [{}]}}, {"name": "empty"}, "nope"]})
    r = env.push(req, key)
    count, message = rejected(r)
    assert count == 5 + 2 + 1 + 1 and "not valid" in message and "exponential" in message
    assert [x[2] for x in env.stored()] == ["ok.unit"]


def test_more_points_than_the_request_cap_are_rejected(env):
    key = env.key("nas01")
    pts = [number(float(i), T0 + i) for i in range(5003)]
    r = env.push(metrics_request("nas01", {"s": [gauge("m", pts)]}), key)
    assert r.status_code == 200 and rejected(r)[0] == 3
    assert env.rows("SELECT COUNT(*) FROM samples") == [(5000,)]


def test_more_log_records_than_the_request_cap_are_rejected(env):
    key = env.key("nas01")
    recs = [log_record("a.b", float(i), "t", observe__dedup_key=f"k{i}") for i in range(503)]
    r = env.push(logs_request("nas01", recs), key, "/v1/logs")
    assert r.status_code == 200
    assert int(r.json()["partialSuccess"]["rejectedLogRecords"]) == 3
    assert env.rows("SELECT COUNT(*) FROM host_events") == [(500,)]


def test_bad_log_records_are_rejected_one_by_one(env):
    key = env.key("nas01")
    ok = log_record("a.b", 1.0, "fine", observe__dedup_key="ok")
    no_name = {"timeUnixNano": "1", "body": {"stringValue": "x"}}
    long_source = log_record("c.d", 1.0, "x", observe__dedup_key="d", observe__source="s" * 129)
    huge = log_record("e.f", 1.0, "x", **{f"k{i}": "v" for i in range(40)})
    req = logs_request("nas01", [ok, no_name, "text", huge, long_source,
                                 {"attributes": [{"key": "event.name",
                                                  "value": {"stringValue": "g.h"}}],
                                  "timeUnixNano": "-5"}])
    r = env.push(req, key, "/v1/logs")
    assert r.status_code == 200
    assert int(r.json()["partialSuccess"]["rejectedLogRecords"]) == 4
    kinds = {k for (k,) in env.rows("SELECT kind FROM host_events")}
    assert kinds == {"a.b", "c.d"}  # a source that is too long falls back to the scope name


def test_a_request_that_is_not_an_object_or_has_wrong_types_stores_nothing(env):
    key = env.key("nas01")
    for body in (b"[]", b"5", b'"x"', b"null", b'{"resourceMetrics":5}',
                 b'{"resourceMetrics":[5,[],null,{"resource":7}]}',
                 b'{"resourceMetrics":[{"resource":{"attributes":"x"},"scopeMetrics":{}}]}'):
        r = env.push(body, key)
        assert r.status_code in (200, 403), body
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM hosts") == [(0,)]


# ---- idempotency ----------------------------------------------------------------------------

def test_an_identical_resend_is_acknowledged_and_changes_nothing(env):
    key = env.key("nas01")
    req = simple()
    assert env.push(req, key).status_code == 200
    batches = env.rows("SELECT COUNT(*) FROM ingest_batches")
    before = env.stored()
    again = env.push(req, key)
    assert again.status_code == 200 and again.json() == {}
    assert env.stored() == before and env.rows("SELECT COUNT(*) FROM ingest_batches") == batches


def test_a_resend_with_an_idempotency_key_skips_decoding(env, monkeypatch):
    key = env.key("nas01")
    headers = {"Idempotency-Key": "8f14e45f-ceea-467f-a0e6-6c1f3a1c2b11"}
    assert env.push(simple(), key, headers=headers).status_code == 200
    calls = []
    real = wire.decode
    monkeypatch.setattr(wire, "decode", lambda *a: calls.append(a) or real(*a))
    changed = simple(value=99.0, ts=T0 + 60)  # the producer resends under the same key
    r = env.push(changed, key, headers=headers, encoding="proto")
    assert r.status_code == 200 and calls == []
    assert [x[4] for x in env.stored()] == [12.5]  # nothing new was stored


def test_the_same_key_for_metrics_and_logs_is_two_batches(env):
    key = env.key("nas01")
    headers = {"Idempotency-Key": "one-batch"}
    assert env.push(simple(), key, headers=headers).status_code == 200
    req = logs_request("nas01", [log_record("a.b", 1.0, "t", observe__dedup_key="k")])
    assert env.push(req, key, "/v1/logs", headers=headers).status_code == 200
    assert env.rows("SELECT COUNT(*) FROM host_events") == [(1,)]
    assert env.rows("SELECT COUNT(*) FROM ingest_batches") == [(2,)]


def test_a_point_sent_again_in_a_new_request_is_stored_once(env):
    key = env.key("nas01")
    assert env.push(simple(), key, headers={"Idempotency-Key": "a"}).status_code == 200
    assert env.push(simple(), key, headers={"Idempotency-Key": "b"}).status_code == 200
    assert env.rows("SELECT COUNT(*) FROM samples") == [(1,)]
    assert env.rows("SELECT n FROM rollup_5m") == [(1,)]  # the summaries counted it once


def test_a_point_sent_again_with_a_new_value_replaces_it(env):
    key = env.key("nas01")
    env.push(simple(value=1.0), key)
    env.push(simple(value=3.0), key)
    assert [x[4] for x in env.stored()] == [3.0]
    assert env.rows("SELECT n, sum_v FROM rollup_5m") == [(1, 3.0)]


def test_an_invalid_idempotency_key_is_refused_before_anything_is_stored(env):
    key = env.key("nas01")
    for bad in ("", "x" * 129, "a\tb"):
        r = env.push(simple(), key, headers={"Idempotency-Key": bad})
        assert r.status_code == 400, bad
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]


def test_idempotency_is_per_host(env):
    for host in ("nas01", "nas02"):
        assert env.push(simple(host), env.key(host),
                        headers={"Idempotency-Key": "same"}).status_code == 200
    assert len(env.stored()) == 2


# ---- the empty request and the removed routes -----------------------------------------------

def test_an_empty_request_is_a_cheap_probe(env):
    key = env.key("nas01")
    assert env.push(b"", key, headers={"Content-Type": PROTO}).status_code == 200
    assert env.push(b"{}", key).status_code == 200
    assert env.push(b"", key, "/v1/logs", headers={"Content-Type": PROTO}).status_code == 200
    assert env.rows("SELECT COUNT(*) FROM hosts") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM ingest_batches") == [(0,)]
    assert env.push(b"", None, headers={"Content-Type": PROTO}).status_code == 401


def test_traces_are_a_404_problem_and_the_old_routes_are_gone(env):
    r = env.client.post("/v1/traces", content=b"")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["status"] == 404
    key = env.key("nas01")
    for path in ("/api/ingest", "/internal/v1/ingest", "/api/v1/field-reports",
                 "/api/v1/field-reports/ping"):
        assert env.client.post(path, json=fixture("batch_minimal"),
                               headers={"Authorization": f"Bearer {key}"}).status_code in (404, 405)
    assert env.client.get("/v1/metrics").status_code == 405
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]


def test_ingest_is_not_behind_basic_auth_but_dashboard_still_is(env):
    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                      server={"basic_auth_user": "ops", "basic_auth_password": "s3cret",
                              "db_path": env.path})
    alerter = Alerter(cfg)
    client = TestClient(create_app(cfg, env.store, Scheduler(cfg, env.store, alerter), alerter))
    key = env.key("nas01")
    r = client.post("/v1/metrics", json=simple(), headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert client.get("/metrics").status_code == 401
    client.close()


def test_an_ingest_key_does_not_open_other_endpoints(env):
    key = env.key("nas01")
    assert env.client.get("/api/v2/events", headers={
        "Authorization": f"Bearer {key}"}).status_code == 401


# ---- limits ---------------------------------------------------------------------------------

def test_content_type_and_encoding_are_checked_after_the_key(env):
    key = env.key("nas01")
    body = json.dumps(simple()).encode()
    for ctype in ("text/plain", "application/x-www-form-urlencoded", ""):
        r = env.client.post("/v1/metrics", content=body, headers={
            "Authorization": f"Bearer {key}", "Content-Type": ctype})
        assert r.status_code == 415, ctype
    assert env.client.post("/v1/metrics", content=body, headers={
        "Content-Type": "text/plain"}).status_code == 401
    assert env.push(body, key, headers={"Content-Encoding": "br"}).status_code == 415
    assert env.push(body, key, headers={"Content-Type": "application/json; charset=utf-8"}
                    ).status_code == 200
    assert env.push(body, key, headers={"Content-Encoding": "identity"}).status_code == 200


def test_oversized_body_is_413_and_authentication_comes_first(env):
    key = env.key("nas01")
    big = b" " * (1_048_576 + 1)
    assert env.push(big, key).status_code == 413
    assert env.push(big, None).status_code == 401
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]


def test_a_gzip_bomb_is_413_and_bad_gzip_is_400(env):
    key = env.key("nas01")
    bomb = gzip.compress(b"0" * (MAX_INFLATED_BYTES + 1))
    assert len(bomb) < 1_048_576
    r = env.push(bomb, key, headers={"Content-Encoding": "gzip"})
    assert r.status_code == 413
    ok = gzip.compress(json.dumps(simple()).encode())
    for bad in (b"not gzip", ok[:-4], ok + ok, b"\x1f\x8b" + b"\0" * 20):
        r = env.push(bad, key, headers={"Content-Encoding": "gzip"})
        assert r.status_code == 400, bad
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]


def test_inflate_is_capped_while_inflating():
    with pytest.raises(BadRequest) as err:
        inflate(gzip.compress(b"0" * (60 * 1024 * 1024)))
    assert err.value.status == 413
    assert inflate(gzip.compress(b"abc")) == b"abc"


def test_malformed_bodies_are_400_and_store_nothing(env):
    key = env.key("nas01")
    good = proto(simple())
    cases = [(b"not json", JSON), (b"{", JSON), (b"", JSON), (good[:-3], PROTO),
             (good[:5], PROTO), (b"\xff\xff\xff", PROTO), (b"\x0a\xff", PROTO),
             (b"\x0a\x05abc", PROTO), (b"\x00", PROTO), (b"\x0b", PROTO),  # field 0, a group
             (b"\x0a\x03\x0a\x01\xff", PROTO), (b"\x08" + b"\xff" * 11 + b"\x01", PROTO)]
    for body, ctype in cases:
        r = env.push(body, key, headers={"Content-Type": ctype})
        assert r.status_code == 400, (body, ctype)
    assert env.rows("SELECT COUNT(*) FROM samples") == [(0,)]


def test_a_string_that_is_not_utf8_is_400(env):
    kvp = b"\x0a\x01k\x12\x03\x0a\x01\xff"  # a string value holding byte 0xff
    resource = b"\x0a" + bytes([len(kvp)]) + kvp
    body = b"\x0a" + bytes([len(resource) + 2]) + b"\x0a" + bytes([len(resource)]) + resource
    assert env.push(body, env.key("nas01"), headers={"Content-Type": PROTO}).status_code == 400


@pytest.mark.parametrize("body", [b"[" * 10000, b'{"a":' * 10000, b"[" * 10000 + b"]" * 10000],
                         ids=["open-arrays", "open-objects", "balanced-arrays"])
def test_deeply_nested_json_is_rejected_and_recorded(env, body):
    key = env.key("nas01")
    r = env.push(body, key)
    assert r.status_code == 400
    rows = env.rows("SELECT kind, status, detail FROM audit WHERE kind='ingest_denied'")
    assert len(rows) == 1 and rows[0][1] == 400 and "nested" in rows[0][2]


def nested_request(arrays: int) -> bytes:
    """A request whose one resource attribute holds `arrays` arrays inside each other."""
    value = ld(1, b"x")  # AnyValue{string_value}
    for _ in range(arrays):
        value = ld(5, ld(1, value))  # AnyValue{array_value: ArrayValue{values: [value]}}
    resource = ld(1, ld(1, b"k") + ld(2, value))  # Resource{attributes: [KeyValue]}
    return ld(1, ld(1, resource))  # request{resource_metrics: [ResourceMetrics{resource}]}


def test_deeply_nested_protobuf_is_400(env):
    key = env.key("nas01")
    assert env.push(nested_request(3), key, headers={"Content-Type": PROTO}).status_code in (
        200, 403)
    assert env.push(nested_request(40), key, headers={"Content-Type": PROTO}).status_code == 400
    with pytest.raises(wire.WireError, match="nested"):
        wire.decode(nested_request(40), "MetricsRequest")
    wire.decode(nested_request(3), "MetricsRequest")


def test_brackets_inside_strings_do_not_count_as_nesting():
    assert not too_deep(json.dumps({"a": "[" * 500}).encode())
    assert too_deep(b"[" * 33)
    assert not too_deep(b"[" * 32 + b"]" * 32)


def test_rate_limit_returns_429_and_recovers(tmp_path):
    e = Env(tmp_path, rate=3)
    try:
        key = e.key("nas01")
        codes = [e.push(simple(), key).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        assert e.push(simple(), key).headers["retry-after"] == "60"
        e.clock.now += 61
        assert e.push(simple(), key).status_code == 200
    finally:
        e.close()


def test_one_key_is_limited_across_peers(tmp_path):
    e = Env(tmp_path, rate=2)
    try:
        key = e.key("nas01")
        assert [e.push(simple(), key).status_code for _ in range(2)] == [200, 200]
        other = TestClient(e.client.app, client=("203.0.113.9", 5000))
        r = other.post("/v1/metrics", json=simple(), headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 429
        assert other.post("/v1/metrics", json=simple(), headers={
            "Authorization": f"Bearer {e.key('nas02')}"}).status_code == 403  # the other key
        other.close()
    finally:
        e.close()


def test_last_use_of_a_key_is_recorded_at_most_once_a_minute(env):
    key = env.key("nas01")
    assert env.push(simple(), key).status_code == 200
    env.store.storage.write_sync(lambda db: db.execute("UPDATE ingest_keys SET last_used = 5.0"))
    assert env.push(simple(ts=T0 + 1), key).status_code == 200
    assert env.rows("SELECT last_used FROM ingest_keys") == [(5.0,)]
    env.clock.now += 61
    assert env.push(simple(ts=T0 + 2), key).status_code == 200
    assert env.rows("SELECT last_used FROM ingest_keys")[0][0] > 5.0
    # A revoked key stops at once, inside the minute.
    assert asyncio.run(revoke_key(env.store, key.split("_")[1]))
    assert env.push(simple(ts=T0 + 3), key).status_code == 401


def test_denials_are_audited_in_aggregate(env):
    for _ in range(5):
        assert env.push(simple(), None).status_code == 401
    rows = env.rows("SELECT kind, actor, status, remote, detail FROM audit")
    assert len(rows) == 1  # five denials in one window, one row
    kind, actor, status, remote, detail = rows[0]
    assert (kind, status, remote) == ("ingest_denied", 401, "testclient")
    env.clock.now += 61
    assert env.push(simple(), None).status_code == 401
    rows = env.rows("SELECT detail FROM audit ORDER BY id")
    assert len(rows) == 2
    assert json.loads(rows[1][0])["denials_covered"] == 5


def test_audit_never_holds_the_key(env):
    key = env.key("nas02")
    assert env.push(simple("nas01"), key).status_code == 403
    secret = key.split("_", 2)[2]
    dump = json.dumps(env.rows("SELECT * FROM audit"))
    assert secret not in dump and key not in dump
    kind, actor, detail = env.rows("SELECT kind, actor, detail FROM audit")[0]
    assert actor == key.split("_")[1]
    assert json.loads(detail)["bound_host"] == "nas02"


def test_a_busy_writer_is_503_with_retry_after(env, monkeypatch):
    key = env.key("nas01")

    async def busy(*a, **kw):
        raise StorageBusy("queue is full")

    monkeypatch.setattr(env.store, "ingest_batch", busy)
    r = env.push(simple(), key)
    assert r.status_code == 503 and r.headers["retry-after"] == "5"


def test_denial_aggregator_bounds_peers():
    clock = Clock()
    agg = DenialAggregator(clock)
    agg.MAX_PEERS = 2
    assert agg.note("a") == 0 and agg.note("b") == 0
    assert agg.note("c") == 0  # overflow bucket, first
    assert agg.note("d") is None  # shares the overflow bucket
    clock.now += 61
    assert agg.note("a") == 1


def test_rate_limiter_window():
    clock = Clock()
    rl = RateLimiter(2, clock)
    assert [rl.allow("a") for _ in range(3)] == [True, True, False]
    assert rl.allow("b")
    clock.now += 60
    assert rl.allow("a")


# ---- plugin log handlers --------------------------------------------------------------------

class _Handlers(PluginBase):
    name = "keyed"
    core_versions = ">=2026.9,<2027"

    def __init__(self, handlers) -> None:
        self._handlers = handlers

    def log_handlers(self):
        return self._handlers


async def _ok(store, ctx, record):
    return {}


def test_log_handler_names_must_sit_under_the_plugin():
    _load(_Handlers({"observe.keyed.thing": _ok}))
    for handlers in ({"observe.other.thing": _ok}, {"boot.panic": _ok}, {"observe.keyed.x": print},
                     {"observe.keyed.x": lambda *a: None}, {5: _ok}, [("a", _ok)]):
        with pytest.raises(PluginError):
            _load(_Handlers(handlers))


# ---- fuzzing --------------------------------------------------------------------------------

def valid_requests() -> list[bytes]:
    metrics, logs = from_batch(fixture("batch_with_events"))
    extra = metrics_request("nas01", {"s": [
        total("c", [number(5, T0, {"a": "b"}, as_int=True)]), histogram("h", 3, 1.5, T0),
        gauge("g", [number(1.0, T0, {"k": 7})])]})
    nested = {"resourceLogs": [{"resource": {"attributes": [kv("host.name", "nas01")]},
                                "scopeLogs": [{"logRecords": [{"attributes": [
                                    kv("event.name", "a.b"), {"key": "arr", "value": {
                                        "arrayValue": {"values": [{"stringValue": "x"}]}}}],
                                    "body": {"kvlistValue": {"values": [kv("a", "b")]}}}]}]}]}
    return [proto(metrics), proto(logs), proto(extra), proto(nested)]


def test_the_decoder_only_ever_raises_wire_error_on_random_and_mutated_bytes():
    rnd = random.Random(20261007)
    seeds = valid_requests()
    for i in range(3000):
        base = bytearray(rnd.choice(seeds))
        mode = i % 6
        if mode == 0:
            data = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 200)))
        elif mode == 1:
            for _ in range(rnd.randrange(1, 6)):
                base[rnd.randrange(len(base))] = rnd.randrange(256)
            data = bytes(base)
        elif mode == 2:
            data = bytes(base[:rnd.randrange(len(base))])
        elif mode == 3:
            at = rnd.randrange(len(base))
            data = bytes(base[:at] + bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 9)))
                         + base[at:])
        elif mode == 4:
            a = rnd.randrange(len(base))
            data = bytes(base + base[a:a + rnd.randrange(1, 40)])
        else:
            data = bytes(base[::-1])
        for message in ("MetricsRequest", "LogsRequest"):
            try:
                out = wire.decode(data, message)
            except wire.WireError:
                continue
            assert isinstance(out, dict)


def mutate(rnd: random.Random, node, depth: int = 0):
    """A copy of a JSON structure with a few values swapped for hostile ones."""
    hostile = [None, True, -1, 2**70, 1e308, "x" * 3000, "", [], {}, [[]], {"a": 1}, "NaN",
               {"stringValue": 5}, {"intValue": "abc"}, {"asDouble": "1"}, 0, "0"]
    if isinstance(node, dict):
        out = {k: mutate(rnd, v, depth + 1) for k, v in node.items()}
        if out and rnd.random() < 0.1:
            out.pop(rnd.choice(list(out)))
        return out
    if isinstance(node, list):
        out = [mutate(rnd, v, depth + 1) for v in node]
        if out and rnd.random() < 0.1:
            out.insert(rnd.randrange(len(out)), rnd.choice(hostile))
        return out
    return rnd.choice(hostile) if rnd.random() < 0.15 else node


def test_no_request_makes_the_routes_fail_with_a_server_error(tmp_path):
    e = Env(tmp_path, rate=10_000_000)
    quiet = TestClient(e.client.app, raise_server_exceptions=False)
    try:
        key = e.key("nas01")
        rnd = random.Random(7)
        metrics, logs = from_batch(fixture("batch_with_events"))
        seeds = [(metrics, "/v1/metrics"), (logs, "/v1/logs")]
        raw_seeds = valid_requests()
        statuses: set[int] = set()
        for i in range(600):
            if i % 3 == 0:
                req, path = rnd.choice(seeds)
                body = json.dumps(mutate(rnd, req)).encode()
                ctype = JSON
            else:
                base = bytearray(rnd.choice(raw_seeds))
                for _ in range(rnd.randrange(0, 5)):
                    base[rnd.randrange(len(base))] = rnd.randrange(256)
                body = bytes(base[:rnd.randrange(len(base) + 1)]) if i % 7 == 0 else bytes(base)
                ctype, path = PROTO, rnd.choice(["/v1/metrics", "/v1/logs"])
            e.clock.now += 61  # keep the denial aggregator writing, as a scanner would
            r = quiet.post(path, content=body, headers={
                "Authorization": f"Bearer {key}", "Content-Type": ctype})
            statuses.add(r.status_code)
            assert r.status_code < 500, (i, body[:200], r.text)
        assert statuses <= {200, 400, 403}
        # Whatever was stored obeys the schema's limits, and the database is still consistent.
        assert e.rows("SELECT COUNT(*) FROM series WHERE metric = ''") == [(0,)]
        assert all(len(m) <= 128 for (m,) in e.rows("SELECT metric FROM series"))
        assert e.rows("PRAGMA integrity_check") == [("ok",)]
    finally:
        quiet.close()
        e.close()


def test_hostile_attribute_and_value_shapes_are_data_not_failures(env):
    key = env.key("nas01")
    hostile = [{"key": "a", "value": {"stringValue": "\u0000\u202e<script>"}},
               {"key": "b" * 128, "value": {"intValue": "9223372036854775807"}},
               {"key": "c", "value": {"intValue": "99999999999999999999"}},
               {"key": "d", "value": {"doubleValue": "x"}},
               {"key": "e", "value": {"boolValue": "true"}},
               {"key": 7, "value": {"stringValue": "x"}}, 5, None, {"value": {}}, {"key": "f"}]
    for attrs_ in hostile:
        dp = {"timeUnixNano": "1760000000000000000", "asDouble": 1.0, "attributes": [attrs_]}
        r = env.push(metrics_request("nas01", {"s": [gauge("m", [dp])]}), key)
        assert r.status_code == 200
    # An attribute with no usable value makes its point be rejected, never stored half-read.
    assert env.rows("SELECT COUNT(*) FROM samples") == [(3,)]


def test_attrs_helper_round_trips_every_scalar_kind():
    got = attrs(a="x", b=1, c=2.5, d=True)
    assert got == [kv("a", "x"), kv("b", 1), kv("c", 2.5), kv("d", True)]
