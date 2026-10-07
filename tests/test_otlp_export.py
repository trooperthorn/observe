"""The OTLP exporter (docs/DATA-API-DESIGN.md section 6.6) against a fake collector: batching,
gzip, retry with Retry-After and backoff, no retry on a final refusal, partial success, a cursor
that survives a restart, the lag gap record, the filters, the encoder against the decoder and the
secrets rule. The collector is an httpx mock transport, so nothing leaves the process."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging

import httpx
import pytest
from pydantic import ValidationError

from observe.config import OtlpExportConfig
from observe.otlp import encode, wire
from observe.otlp.export import Exporter, retry_after
from observe.storage import series
from observe.store import Store

from .otlp_build import proto

T0 = 1_800_000_000.0
SECRET = "collector-secret-value-123"
ENDPOINT = "https://collector.test"


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Collector:
    """A fake OTLP collector. `script` holds the answers to give in turn; once it is empty every
    request is answered 200."""

    def __init__(self, script: list | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raw = gzip.decompress(request.content)
        kind = request.url.path.rsplit("/", 1)[1]
        if request.headers["content-type"] == "application/json":
            self.bodies.append(json.loads(raw))
        else:
            self.bodies.append(wire.decode(raw, "MetricsRequest" if kind == "metrics"
                                           else "LogsRequest"))
        step = self.script.pop(0) if self.script else 200
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            status, headers, content = (step + (None, None))[:3]
            return httpx.Response(status, headers=headers or {}, content=content or b"")
        return httpx.Response(step)

    def points(self, kind: str = "metrics") -> list[tuple[str, float]]:
        out = []
        for req, body in zip(self.requests, self.bodies):
            if not req.url.path.endswith(kind):
                continue
            for rm in body.get("resourceMetrics", []):
                for sm in rm["scopeMetrics"]:
                    for m in sm["metrics"]:
                        for dp in (m.get("gauge") or m.get("sum"))["dataPoints"]:
                            out.append((m["name"], dp["asDouble"]))
        return out

    def records(self) -> list[dict]:
        out = []
        for req, body in zip(self.requests, self.bodies):
            if req.url.path.endswith("logs"):
                for rl in body["resourceLogs"]:
                    for sl in rl["scopeLogs"]:
                        out.extend(sl["logRecords"])
        return out


def attr(record: dict, key: str):
    for item in record["attributes"]:
        if item["key"] == key:
            return next(iter(item["value"].values()))
    return None


def config(**extra) -> OtlpExportConfig:
    base = {"endpoint": ENDPOINT, "settle_s": 0, "headers": {"Authorization": f"Bearer {SECRET}"}}
    base.update(extra)
    return OtlpExportConfig.model_validate(base)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "export.db"))
    yield s
    s.close()


def make(store, collector, clock=None, **extra) -> Exporter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(collector))
    return Exporter(config(**extra), store, client=client, clock=clock or Clock(),
                    rng=lambda: 1.0)


async def put(store, values, *, host="h1", scope="cpu", metric="cpu.temp", base=T0 + 10,
              step=1.0, kind="host", attrs=None):
    pts = [series.Point(scope, metric, "C", "{}", series.to_ms(base + i * step), v)
           for i, v in enumerate(values)]
    await store.storage.write(lambda db: series.record_points(
        db, kind=kind, name=host, points=pts, now=base, rollups=True,
        attrs={"host.name": host} if attrs is None else attrs))


async def started(store, collector, **extra):
    """An exporter whose cursor was created at T0, with the clock moved on so new points are due."""
    clock = Clock()
    exp = make(store, collector, clock, **extra)
    assert await exp.cycle() == exp.cfg.interval
    assert collector.requests == []
    clock.now = T0 + 1000
    return exp, clock


async def test_points_go_out_in_batches_gzip_compressed_with_the_headers(store):
    collector = Collector()
    exp, _ = await started(store, collector, max_batch_points=2)
    await put(store, [1.0, 2.0, 3.0, 4.0, 5.0])
    assert await exp.cycle() == exp.cfg.interval
    assert [len(b["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"])
            for b in collector.bodies] == [2, 2, 1]
    assert [v for _, v in collector.points()] == [1.0, 2.0, 3.0, 4.0, 5.0]
    first = collector.requests[0]
    assert first.url == f"{ENDPOINT}/v1/metrics"
    assert first.headers["content-encoding"] == "gzip"
    assert first.headers["content-type"] == "application/x-protobuf"
    assert first.headers["authorization"] == f"Bearer {SECRET}"
    body = collector.bodies[0]
    attrs = {a["key"]: a["value"] for a in body["resourceMetrics"][0]["resource"]["attributes"]}
    assert attrs["host.name"] == {"stringValue": "h1"}
    assert body["resourceMetrics"][0]["scopeMetrics"][0]["scope"]["name"] == "cpu"
    assert exp.stats.sent == 5 and exp.stats.requests == 3 and exp.stats.failed == 0
    # Nothing is sent twice.
    await exp.cycle()
    assert len(collector.requests) == 3


async def test_the_json_protocol_sends_the_otlp_json_mapping(store):
    collector = Collector()
    exp, _ = await started(store, collector, protocol="http/json")
    await put(store, [21.5])
    await exp.cycle()
    assert collector.requests[0].headers["content-type"] == "application/json"
    point = collector.bodies[0]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0][
        "gauge"]["dataPoints"][0]
    assert point["asDouble"] == 21.5 and point["timeUnixNano"] == str(int((T0 + 10) * 1e9))


async def test_a_503_with_retry_after_is_retried_without_losing_the_batch(store):
    collector = Collector([(503, {"Retry-After": "120"})])
    exp, _ = await started(store, collector)
    await put(store, [1.0, 2.0])
    wait = await exp.cycle()
    assert wait == 120.0  # the backoff is at most 1 s here, so Retry-After decides
    assert exp.stats.failed == 1 and exp.stats.consecutive_failures == 1
    assert exp.stats.last_error == "collector answered 503" and exp.stats.sent == 0
    assert exp.stats.lag_s > 0
    assert await exp.cycle() == exp.cfg.interval
    assert [v for _, v in collector.points()] == [1.0, 2.0, 1.0, 2.0]  # the same batch again
    assert exp.stats.sent == 2 and exp.stats.consecutive_failures == 0 and exp.stats.lag_s == 0


async def test_backoff_doubles_to_five_minutes_with_full_jitter(store):
    collector = Collector([502] * 12)
    exp, _ = await started(store, collector)
    await put(store, [1.0])
    waits = [await exp.cycle() for _ in range(10)]
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 300.0]
    exp.rng = lambda: 0.0
    assert await exp.cycle() == 0.0  # full jitter reaches zero


@pytest.mark.parametrize("answer", [429, 502, 504, httpx.ConnectError("refused")])
async def test_network_errors_and_the_retryable_statuses_are_retried(store, answer):
    collector = Collector([answer])
    exp, _ = await started(store, collector)
    await put(store, [7.0])
    assert await exp.cycle() == 1.0
    assert exp.stats.sent == 0
    await exp.cycle()
    assert exp.stats.sent == 1


async def test_a_400_is_not_retried_the_batch_is_dropped_and_the_stream_goes_on(store, caplog):
    collector = Collector([400])
    exp, _ = await started(store, collector, max_batch_points=2)
    await put(store, [1.0, 2.0, 3.0])
    with caplog.at_level(logging.ERROR, logger="observe.export"):
        assert await exp.cycle() == exp.cfg.interval
    assert [v for _, v in collector.points()] == [1.0, 2.0, 3.0]  # sent once each, no resend
    assert exp.stats.dropped == 2 and exp.stats.sent == 1 and exp.stats.failed == 1
    assert exp.stats.consecutive_failures == 0
    assert "dropped" in caplog.text and "400" in caplog.text


@pytest.mark.parametrize("status", [404, 405, 422])
async def test_other_client_errors_are_final_too(store, status):
    collector = Collector([status])
    exp, _ = await started(store, collector)
    await put(store, [1.0])
    await exp.cycle()
    assert len(collector.requests) == 1 and exp.stats.dropped == 1


@pytest.mark.parametrize("status", [401, 403, 408])
async def test_a_credential_refusal_holds_the_batch_instead_of_dropping_it(store, status):
    collector = Collector([status])
    exp, _ = await started(store, collector)
    await put(store, [1.0, 2.0])
    assert await exp.cycle() == 1.0
    assert exp.stats.dropped == 0 and exp.stats.sent == 0 and exp.stats.consecutive_failures == 1
    await exp.cycle()  # the token was fixed: the same points go out
    assert [v for _, v in collector.points()] == [1.0, 2.0, 1.0, 2.0]
    assert exp.stats.sent == 2 and exp.stats.dropped == 0


async def test_a_413_halves_the_batch_until_the_collector_accepts_it(store):
    collector = Collector([413, 413])
    exp, _ = await started(store, collector, max_batch_points=4)
    await put(store, [1.0, 2.0, 3.0, 4.0])
    await exp.cycle()
    sizes = [len(b["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"])
             for b in collector.bodies]
    assert sizes == [4, 2, 1, 1, 1, 1]
    assert exp.stats.dropped == 0 and exp.stats.sent == 4


async def test_a_413_for_a_single_point_is_final(store):
    collector = Collector([413])
    exp, _ = await started(store, collector, max_batch_points=1)
    await put(store, [1.0])
    await exp.cycle()
    assert exp.stats.dropped == 1 and len(collector.requests) == 1


async def test_partial_success_advances_the_cursor_and_counts_the_rejects(store):
    reply = wire.partial_success_body(2, "bad points")
    collector = Collector([(200, {"Content-Type": "application/x-protobuf"}, reply)])
    exp, _ = await started(store, collector)
    await put(store, [1.0, 2.0, 3.0])
    await exp.cycle()
    assert exp.stats.sent == 1 and exp.stats.rejected == 2 and len(collector.requests) == 1
    await exp.cycle()
    assert len(collector.requests) == 1


async def test_a_json_partial_success_is_read_too(store):
    reply = json.dumps({"partialSuccess": {"rejectedDataPoints": 1, "errorMessage": "x"}})
    collector = Collector([(200, {"Content-Type": "application/json"}, reply.encode())])
    exp, _ = await started(store, collector)
    await put(store, [1.0, 2.0])
    await exp.cycle()
    assert exp.stats.rejected == 1 and exp.stats.sent == 1


async def test_the_cursor_survives_a_restart(tmp_path):
    path = str(tmp_path / "restart.db")
    collector = Collector()
    store = Store(path)
    exp, clock = await started(store, collector)
    await put(store, [1.0, 2.0])
    await exp.cycle()
    store.close()
    # A new process: a new store on the same file, a new exporter, no memory of what it sent.
    store = Store(path)
    try:
        again = make(store, collector, clock)
        await put(store, [3.0], base=clock.now + 10)
        clock.now += 100
        await again.cycle()
        assert [v for _, v in collector.points()] == [1.0, 2.0, 3.0]
    finally:
        store.close()


async def test_a_failed_send_is_resumed_after_a_restart(tmp_path):
    path = str(tmp_path / "resume.db")
    collector = Collector([503])
    store = Store(path)
    exp, clock = await started(store, collector)
    await put(store, [1.0])
    await exp.cycle()
    store.close()
    store = Store(path)
    try:
        await make(store, collector, clock).cycle()
        assert [v for _, v in collector.points()] == [1.0, 1.0]
    finally:
        store.close()


async def test_a_cursor_behind_raw_retention_resumes_and_records_a_gap_record(store):
    collector = Collector()
    exp, clock = await started(store, collector)
    await put(store, [1.0, 2.0, 3.0], base=T0 + 100)
    # Raw retention removes them before the collector comes back; the 5 minute level remains.
    await store.storage.execute("DELETE FROM samples")
    await put(store, [9.0], base=T0 + 900)
    clock.now = T0 + 2000
    await exp.cycle()
    assert [v for _, v in collector.points()] == [9.0]
    gaps = [r for r in collector.records() if attr(r, "event.name") == "observe.export.gap"]
    assert len(gaps) == 1 and gaps[0]["severityText"] == "WARN"
    assert exp.stats.gaps == 1
    audit = await store.fetch("SELECT kind, actor FROM audit WHERE kind = 'export_gap'")
    assert audit == [("export_gap", "exporter")]


async def test_a_gap_notice_survives_a_dropped_logs_batch(store):
    collector = Collector([200, 400])  # the metrics request, then a final refusal of the logs
    exp, clock = await started(store, collector)
    await put(store, [1.0], base=T0 + 100)
    await store.storage.execute("DELETE FROM samples")
    await put(store, [9.0], base=T0 + 900)
    insert = ("INSERT INTO host_events (host, ts, kind, severity, source, title, detail, "
              "dedup_key) VALUES ('h1', ?, 'journal.match', 'warning', 'journal', 'E', '{}', ?)")
    await store.storage.write(lambda db: db.execute(insert, (T0 + 5, "k1")))
    clock.now = T0 + 2000
    await exp.cycle()
    assert exp.stats.dropped == 1 and exp.stats.gaps == 1
    await store.storage.write(lambda db: db.execute(insert, (T0 + 6, "k2")))
    await exp.cycle()
    gaps = [r for r in collector.records() if attr(r, "event.name") == "observe.export.gap"]
    assert len(gaps) == 2  # once in the dropped request and again in the next one


async def test_a_quiet_database_is_not_a_gap(store):
    collector = Collector()
    exp, clock = await started(store, collector)
    clock.now += 5000
    await exp.cycle()
    await put(store, [4.0], base=clock.now + 10)
    clock.now += 100
    await exp.cycle()
    assert exp.stats.gaps == 0 and collector.points() == [("cpu.temp", 4.0)]


async def test_a_point_inside_the_settle_window_waits_for_the_next_pass(store):
    collector = Collector()
    clock = Clock()
    exp = make(store, collector, clock, settle_s=30)
    await exp.cycle()
    await put(store, [1.0], base=T0 + 50)
    clock.now = T0 + 60  # the point is 10 s old, newer than the settle window
    await exp.cycle()
    assert collector.points() == []
    clock.now = T0 + 90
    await exp.cycle()
    assert collector.points() == [("cpu.temp", 1.0)]


async def test_a_late_point_inside_the_window_is_not_missed(store):
    collector = Collector()
    clock = Clock()
    exp = make(store, collector, clock, settle_s=30)
    await exp.cycle()
    await put(store, [2.0], base=T0 + 50)
    await put(store, [1.0], base=T0 + 40, host="h2")  # arrives after, stamped earlier
    clock.now = T0 + 100
    await exp.cycle()
    assert sorted(v for _, v in collector.points()) == [1.0, 2.0]


async def test_the_resource_filter_keeps_only_the_named_kinds(store):
    collector = Collector()
    exp, _ = await started(store, collector, resource_filter=["host"])
    await put(store, [1.0])
    await put(store, [2.0], host="sw1", kind="network_device", attrs={"observe.device": "sw1"})
    await exp.cycle()
    assert [v for _, v in collector.points()] == [1.0]
    assert exp.stats.sent == 1


async def test_a_filtered_batch_still_moves_the_cursor(store):
    collector = Collector()
    exp, _ = await started(store, collector, resource_filter=["monitor"])
    await put(store, [1.0])
    await exp.cycle()
    assert collector.requests == [] and exp.stats.dropped == 0


async def test_host_events_go_out_as_log_records(store):
    collector = Collector()
    exp, _ = await started(store, collector)
    await store.storage.write(lambda db: db.execute(
        "INSERT INTO host_events (host, ts, kind, severity, source, title, detail, dedup_key) "
        "VALUES ('h1', ?, 'journal.match', 'warning', 'journal', 'Disk error', '{}', 'k1')",
        (T0 + 5,)))
    await exp.cycle()
    [record] = collector.records()
    assert record["body"] == {"stringValue": "Disk error"} and record["severityNumber"] == 13
    assert attr(record, "event.name") == "journal.match"
    assert collector.requests[0].url.path == "/v1/logs"
    await exp.cycle()
    assert len(collector.records()) == 1


async def test_events_before_the_first_start_are_not_replayed(store):
    await store.storage.write(lambda db: db.execute(
        "INSERT INTO host_events (host, ts, kind, severity, source, title, detail, dedup_key) "
        "VALUES ('h1', 1, 'old', 'info', 'journal', 'Old', '{}', 'k0')"))
    collector = Collector()
    exp, _ = await started(store, collector)
    await exp.cycle()
    assert collector.records() == []


async def test_audit_rows_are_exported_only_when_asked(store):
    await store.write_audit("login_ok", actor="root", remote="10.0.0.9", ts=T0 + 1)
    off = Collector()
    exp, _ = await started(store, off)
    await store.write_audit("login_ok", actor="root", remote="10.0.0.9", ts=T0 + 2)
    await exp.cycle()
    assert off.records() == []
    on = Collector()
    exp, _ = await started(store, on, include_audit=True)
    await store.write_audit("logout", actor="root", ts=T0 + 3)
    await exp.cycle()
    [record] = on.records()
    assert attr(record, "event.name") == "observe.audit.logout"
    assert attr(record, "observe.audit.actor") == "root"


async def test_the_signals_setting_limits_what_is_sent(store):
    collector = Collector()
    exp, _ = await started(store, collector, signals=["logs"])
    await put(store, [1.0])
    await exp.cycle()
    assert collector.requests == []
    assert (await store.fetch("SELECT 1 FROM sqlite_master WHERE name = 'samples_ts'")) == []


async def test_status_has_the_counters_and_never_a_header_value(store):
    collector = Collector([503])
    exp, _ = await started(store, collector)
    await put(store, [1.0])
    await exp.cycle()
    status = exp.status()
    assert status["enabled"] and status["failed"] == 1 and status["endpoint"] == ENDPOINT
    assert status["consecutive_failures"] == 1
    assert SECRET not in json.dumps(status) and "Authorization" not in json.dumps(status)


async def test_a_network_error_never_puts_a_secret_in_the_log_or_the_status(store, caplog):
    boom = httpx.ConnectError(f"cannot reach {ENDPOINT}?token={SECRET}")
    collector = Collector([boom, 400])
    exp, _ = await started(store, collector)
    await put(store, [1.0])
    with caplog.at_level(logging.DEBUG):
        await exp.cycle()
        await exp.cycle()
    assert SECRET not in caplog.text and SECRET not in json.dumps(exp.status())
    assert exp.stats.last_error == "collector answered 400"


async def test_the_run_loop_waits_the_returned_time_and_survives_an_error(store):
    collector = Collector()
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 3:
            raise asyncio.CancelledError

    exp = make(store, collector)
    exp.sleep = sleep
    calls = {"n": 0}
    real = exp.cycle

    async def flaky() -> float:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return await real()

    exp.cycle = flaky
    with pytest.raises(asyncio.CancelledError):
        await exp.run()
    assert waits == [60.0, 60.0, 60.0] and "internal error" in exp.stats.last_error


async def test_a_redirect_is_not_followed(store):
    collector = Collector([(307, {"Location": "https://elsewhere.test/v1/metrics"})])
    exp, _ = await started(store, collector)
    await put(store, [1.0])
    await exp.cycle()
    assert len(collector.requests) == 1 and exp.stats.dropped == 1
    assert exp.open_client().follow_redirects is False


# ---- the encoder ----------------------------------------------------------------------------

REQUEST = {"resourceMetrics": [{
    "resource": {"attributes": [{"key": "host.name", "value": {"stringValue": "h1"}},
                                {"key": "n", "value": {"intValue": "-5"}},
                                {"key": "ok", "value": {"boolValue": True}},
                                {"key": "f", "value": {"doubleValue": 0.25}}]},
    "scopeMetrics": [{"scope": {"name": "cpu", "version": "1"}, "metrics": [
        {"name": "t", "unit": "C", "gauge": {"dataPoints": [
            {"timeUnixNano": "1800000000123000000", "asDouble": 21.5,
             "attributes": [{"key": "core", "value": {"stringValue": "0"}}]}]}},
        {"name": "c", "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": [
            {"timeUnixNano": "1800000000123000000", "asInt": "42"}]}}]}]}]}


def test_the_encoder_is_the_inverse_of_the_decoder():
    assert wire.decode(encode.to_protobuf(REQUEST, "MetricsRequest"), "MetricsRequest") == REQUEST


def test_the_encoder_matches_the_independent_test_builder():
    ours = wire.decode(encode.to_protobuf(REQUEST, "MetricsRequest"), "MetricsRequest")
    theirs = wire.decode(proto(REQUEST), "MetricsRequest")
    assert ours == theirs


def test_the_encoder_writes_a_log_request():
    request = {"resourceLogs": [{"resource": {"attributes": []}, "scopeLogs": [{
        "scope": {"name": "s"}, "logRecords": [{
            "timeUnixNano": "5", "severityNumber": 9, "severityText": "INFO",
            "body": {"stringValue": "hi"},
            "attributes": [{"key": "event.name", "value": {"stringValue": "x"}}]}]}]}]}
    got = wire.decode(encode.to_protobuf(request, "LogsRequest"), "LogsRequest")
    assert got["resourceLogs"][0]["scopeLogs"][0]["logRecords"] == \
        request["resourceLogs"][0]["scopeLogs"][0]["logRecords"]


def test_responses_are_read_in_both_encodings_and_garbage_is_ignored():
    assert encode.read_response(wire.partial_success_body(3, "no"), "application/x-protobuf") \
        == (3, "no")
    assert encode.read_response(b"", "application/x-protobuf") == (0, "")
    assert encode.read_response(b"\x0a\xff\xff", "application/x-protobuf") == (0, "")
    assert encode.read_response(b"not json", "application/json") == (0, "")
    assert encode.read_response(b'{"partialSuccess":{"rejectedLogRecords":"4"}}',
                                "application/json") == (4, "")


def test_retry_after_reads_seconds_and_dates():
    assert retry_after("30", T0) == 30.0
    assert retry_after("-5", T0) == 0.0
    assert retry_after("999999", T0) == 3600.0
    assert retry_after("Wed, 21 Oct 2026 07:28:00 GMT", 1_792_567_620.0) == 60.0
    assert retry_after("soon", T0) is None and retry_after(None, T0) is None


# ---- the configuration ----------------------------------------------------------------------

def test_the_exporter_is_off_by_default():
    from observe.config import Config
    assert Config.model_validate({}).export.otlp.enabled is False


@pytest.mark.parametrize("bad", [
    {"endpoint": "ftp://x.test"},
    {"endpoint": "https://user:pw@x.test"},
    {"endpoint": "https://x.test/?token=1"},
    {"endpoint": "http://collector.test"},
    {"endpoint": ENDPOINT, "headers": {"Content-Type": "x"}},
    {"endpoint": ENDPOINT, "headers": {"bad name": "x"}},
    {"endpoint": ENDPOINT, "headers": {"X-A": "line\nbreak"}},
    {"endpoint": ENDPOINT, "signals": []},
    {"endpoint": ENDPOINT, "signals": ["metrics", "metrics"]},
    {"endpoint": ENDPOINT, "resource_filter": ["nope"]},
    {"endpoint": ENDPOINT, "client_cert_file": "/x.pem"},
    {"endpoint": ENDPOINT, "interval": 0},
    {"endpoint": ENDPOINT, "unknown_key": 1},
])
def test_a_bad_export_setting_is_refused(bad):
    with pytest.raises(ValidationError):
        OtlpExportConfig.model_validate(bad)


def test_plaintext_is_for_loopback_or_an_explicit_choice():
    assert OtlpExportConfig(endpoint="http://127.0.0.1:4318/").endpoint == "http://127.0.0.1:4318"
    assert OtlpExportConfig(endpoint="http://lan.test:4318", allow_plaintext=True).enabled


def test_header_secrets_come_from_a_file_reference(tmp_path):
    from observe.config import load_config
    secret = tmp_path / "otlp_token"
    secret.write_text(f"Bearer {SECRET}\n", encoding="utf-8")
    cfg = tmp_path / "observe.yaml"
    cfg.write_text(f"export:\n  otlp:\n    endpoint: {ENDPOINT}\n    headers:\n"
                   f"      Authorization: ${{file:{secret.as_posix()}}}\n", encoding="utf-8")
    loaded = load_config(str(cfg))
    assert loaded.export.otlp.headers["Authorization"].get_secret_value() == f"Bearer {SECRET}"
    assert SECRET not in repr(loaded.export.otlp) and SECRET not in loaded.export.otlp.model_dump_json()


# ---- the routes -----------------------------------------------------------------------------

def test_the_status_route_and_metrics_show_the_exporter(tmp_path):
    from .api_env import ApiEnv
    env = ApiEnv(tmp_path)
    try:
        env.login("root", admin=True)
        assert env.get("/admin/exporter").json() == {
            "enabled": False, "endpoint": None, "protocol": None, "signals": [],
            "interval": None, "max_batch_points": None, "sent": 0, "failed": 0, "dropped": 0,
            "rejected": 0, "lag_seconds": 0.0, "last_success": None, "last_error": "",
            "consecutive_failures": 0, "gaps": 0}
        exp = Exporter(config(), env.store)
        exp.stats.sent, exp.stats.failed, exp.stats.dropped, exp.stats.lag_s = 7, 2, 1, 12.5
        env.store.exporter = exp
        got = env.get("/admin/exporter")
        assert got.status_code == 200 and got.json()["sent"] == 7
        assert got.json()["endpoint"] == ENDPOINT and SECRET not in got.text
        text = env.client.get("/metrics").text
        assert "observe_export_sent_total 7" in text and "observe_export_failed_total 2" in text
        assert "observe_export_dropped_total 1" in text and "observe_export_lag_seconds 12.5" in text
        env.client.cookies.clear()
        assert env.get("/admin/exporter", headers=env.token("operator", "o")).status_code == 403
    finally:
        env.store.close()
