"""API correctness: valid JSON for any stored value, a bounded login body however it is framed,
and a stated reason for every point or host status the API rejects or raises."""

from __future__ import annotations

import asyncio
import json

import pytest

from observe import hostview
from observe.otelnames import collector_scope
from observe.storage import series

from .api_env import START, ApiEnv
from .otlp_build import gauge, metrics_request, number
from .test_otlp_ingest import Env as OtlpEnv
from .test_otlp_ingest import T0, rejected
from .test_storage import live_pg


@pytest.fixture
def env(tmp_path):
    e = ApiEnv(tmp_path)
    e.headers = e.token()
    yield e
    e.close()


@pytest.fixture
def otlp(tmp_path):
    e = OtlpEnv(tmp_path)
    yield e
    e.close()


# ---- v2-json-infinity -----------------------------------------------------------------------

def test_two_huge_points_still_give_valid_json_from_the_query(env):
    """The audit stored two 1.7e308 points; their sum is infinity, which JSON cannot carry."""
    pts = [series.Point("big", "m", "", "{}", int((START - 60 + i) * 1000), 1.7e308)
           for i in range(2)]
    asyncio.run(env.store.storage.write(lambda db: series.record_points(
        db, kind="host", name="h", points=pts, now=START, rollups=True)))
    for query in ("&agg=avg,min,max,sum,count", "&step=3600&agg=avg,sum", "&agg=last,sum"):
        r = env.client.get("/api/v2/metrics/query?metric=m&from=-1h" + query, headers=env.headers)
        assert r.status_code == 200, r.text
        # Strict parsing: Infinity and NaN are not JSON.
        body = json.loads(r.content, parse_constant=lambda c: pytest.fail(f"{c} in the body"))
        points = body["series"][0]["points"]
        assert points and all(v is None or abs(v) < float("inf") for p in points for v in p[1:])
    r = env.client.get("/api/v2/metrics/latest?metric=m", headers=env.headers)
    json.loads(r.content, parse_constant=lambda c: pytest.fail(c))


def test_the_sum_of_two_huge_points_is_null_not_infinity(env):
    pts = [series.Point("big", "m", "", "{}", int((START - 60 + i) * 1000), 1.7e308)
           for i in range(2)]
    asyncio.run(env.store.storage.write(lambda db: series.record_points(
        db, kind="host", name="h", points=pts, now=START, rollups=True)))
    r = env.client.get("/api/v2/metrics/query?metric=m&from=-1h&step=3600"
                       "&agg=sum,max", headers=env.headers)
    assert r.status_code == 200
    assert r.json()["series"][0]["points"][0][1:] == [None, 1.7e308]


def test_the_sum_of_two_huge_points_is_null_on_the_live_database(tmp_path):
    """On PostgreSQL and TimescaleDB a float8 sum raises on overflow, so this runs the API on the
    live server (skipped without OBSERVE_TEST_PG_DSN); on SQLite the case above covers it. The
    rows are written below the rollups, as data stored before the ingest bound existed."""
    with live_pg() as storage:
        e = None
        try:
            e = ApiEnv(tmp_path, storage=storage)
            headers = e.token()
            pts = [series.Point("big", "m", "", "{}", int((START - 60 + i) * 1000), 1.7e308)
                   for i in range(2)]
            small = [series.Point("big", "small", "", "{}", int((START - 60 + i) * 1000), v)
                     for i, v in enumerate((0.1, 0.2))]
            asyncio.run(storage.write(lambda db: series.record_points(
                db, kind="host", name="h", points=pts + small, now=START, rollups=False)))
            queries = ["&step=60&agg=sum,avg,max"]
            if storage.incremental_rollups:  # a summary table that can be written directly
                hour = int(START // 7200 * 7200) - 7200

                def fill(db):
                    sid = db.execute("SELECT id FROM series WHERE metric = 'm'").fetchone()[0]
                    for k in range(2):
                        db.execute("INSERT INTO rollup_1h (series_id, bucket, n, sum_v, min_v, "
                                   "max_v) VALUES (?,?,1,?,?,?)",
                                   (sid, (hour + 3600 * k) * 1000, 1.7e308, 1.7e308, 1.7e308))
                asyncio.run(storage.write(fill))
                queries.append("&step=7200&agg=sum,avg,max")
            if getattr(storage, "timescale", False):
                # The summary levels are real-time views that sum the unrefreshed samples
                # themselves, so step 300 reads the two huge samples through the view.
                queries.append("&step=300&agg=sum,avg,max")
            for query in queries:
                r = e.client.get("/api/v2/metrics/query?metric=m&from=-3h" + query,
                                 headers=headers)
                assert r.status_code == 200, r.text
                body = json.loads(r.content, parse_constant=lambda c: pytest.fail(c))
                assert [p[1:] for p in body["series"][0]["points"]
                        if p[3] is not None] == [[None, None, 1.7e308]] * len(
                            [p for p in body["series"][0]["points"] if p[3] is not None])
                assert any(p[1] is None and p[2] is None for p in body["series"][0]["points"])
            r = e.client.get("/api/v2/metrics/query?metric=small&from=-1h&step=60"
                             "&agg=sum,avg,count", headers=headers)
            assert r.status_code == 200, r.text
            got = r.json()["series"][0]["points"][0][1:]
            assert got[0] == pytest.approx(0.3, rel=1e-12) and got[2] == 2
        finally:
            if e is not None:
                e.close()


@pytest.mark.parametrize("total", [None, float("inf"), float("-inf"), float("nan")])
def test_pack_gives_a_null_sum_and_average_for_a_null_or_non_finite_total(total):
    from observe.api.metrics import _pack
    got = _pack(2, total, 1.0, 5.0)
    assert got == {"avg": None, "min": 1.0, "max": 5.0, "sum": None, "count": 2}
    assert _pack(2, 6.0, 1.0, 5.0)["avg"] == 3.0


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args):
        return _Rows(self.rows)


@pytest.mark.parametrize("total", [None, float("inf")])
def test_the_aggregator_answers_a_null_total_without_dividing(total):
    from observe.api.metrics import _aggregate
    for table, rows in (("samples", [(1, 0, 2, total, 1.0, 2.0)]),
                        ("metric_5m", [(1, 0, 2, total, 1.0, 2.0)])):
        name = "raw" if table == "samples" else "5m"
        out = _aggregate(_Db(rows), name, table, [1], 300, 0, 1000,
                         ["avg", "sum", "min", "max", "count"])
        assert out[1] == [[0, None, None, 1.0, 2.0, 2]]


def test_a_value_that_could_overflow_a_rollup_is_refused_at_ingest(otlp):
    key = otlp.key("nas01")
    req = metrics_request("nas01", {"s": [gauge("m", [number(1.7e308, T0),
                                                      number(5.0, T0 + 1)])]})
    got = otlp.push(req, key)
    assert got.status_code == 200
    count, message = rejected(got)
    assert count == 1 and "too large" in message
    assert [row[4] for row in otlp.stored()] == [5.0]


def test_the_json_encoder_never_writes_infinity():
    """A monitor value of nan (an MQTT payload "nan") must not turn a response into a 500."""
    from observe.api.registry import _dump
    body = _dump({"items": [{"value": float("nan"), "n": 1}, {"value": float("-inf")}],
                  "pair": (float("inf"), 2.5)})
    got = json.loads(body, parse_constant=lambda c: pytest.fail(f"{c} in the body"))
    assert got == {"items": [{"value": None, "n": 1}, {"value": None}], "pair": [None, 2.5]}


def test_a_huge_integer_point_is_refused_not_stored_or_raised():
    """asInt as a JSON number was unbounded: 1.7e308 was stored and 10**400 raised."""
    from observe.otlp.normalize import _number
    for v in (17 * 10 ** 307, -(10 ** 301), 10 ** 400, 10 ** 301):
        assert _number({"asInt": v}) == (None, "a value is too large to store")
    assert _number({"asInt": 12}) == (12.0, "")
    assert _number({"asInt": "-9223372036854775807"}) == (-9223372036854775807.0, "")


def test_a_huge_integer_point_on_the_field_path_is_refused():
    from observe.otlp.normalize import normalize_field_metrics
    dp = {"timeUnixNano": str(int(T0 * 1e9)), "asInt": 17 * 10 ** 307}
    req = {"resourceMetrics": [{"resource": {"attributes": []}, "scopeMetrics": [{
        "scope": {"name": "s"}, "metrics": [{"name": "m", "gauge": {"dataPoints": [dp]}}]}]}]}
    points, rejects = normalize_field_metrics(req, "tester1", T0)
    assert points == []
    assert rejects.reasons == {"a value is too large to store": 1}


def test_a_pull_check_drops_only_the_oversized_reading():
    from observe.checks import ha_host
    assert ha_host._number("1e301") is None
    assert ha_host._number("nan") is None
    assert ha_host._number("42.5") == 42.5


# ---- login-chunked-body-unbounded -----------------------------------------------------------

def test_a_chunked_login_body_is_cut_off_before_parsing(env, monkeypatch):

    def body():
        for _ in range(10):
            yield b" " * (1024 * 1024)  # 10 MB in all, with no Content-Length

    parsed = []
    real = json.loads
    monkeypatch.setattr("observe.web.json.loads", lambda raw, *a, **k: parsed.append(raw) or
                        real(raw, *a, **k))
    r = env.client.post("/api/login", content=body())
    assert r.status_code == 413
    assert not parsed  # refused before any parsing


def test_a_small_chunked_login_body_is_still_read(env):
    asyncio.run(__import__("observe.auth", fromlist=["x"]).create_user(
        env.store, env.cfg, "bob", "correct horse battery", False, now=env.wall.now))
    raw = json.dumps({"username": "bob", "password": "correct horse battery"}).encode()

    def body():
        yield raw[:10]
        yield raw[10:]

    assert env.client.post("/api/login", content=body()).status_code == 200


# ---- cardinality-drop-no-reason -------------------------------------------------------------

def test_a_series_cap_drop_is_named_in_the_partial_success(otlp):
    key = otlp.key("nas01")
    over = 5
    n = series.MAX_SERIES_PER_RESOURCE + over
    req = metrics_request("nas01", {"s": [gauge("m", [number(1.0, T0, {"i": str(i)})
                                                      for i in range(n)])]})
    got = otlp.push(req, key)
    assert got.status_code == 200
    count, message = rejected(got)
    assert count == over
    assert f"{over} points dropped" in message and "series limit" in message
    assert otlp.rows("SELECT COUNT(*) FROM series") == [(series.MAX_SERIES_PER_RESOURCE,)]


# ---- host-status-reason-empty ---------------------------------------------------------------

def hot_view(temp: float, stale_after: float = 900.0):
    scope = collector_scope("hwmon")
    sample = {"source": scope, "metric": "hw.temperature", "labels": {"hw.id": "cpu"},
              "value": temp, "unit": "Cel", "ts": 1000.0}
    sources = {"hwmon": {"available": True, "reason": "", "updated": 1000.0}}
    row = {"host": "nas01", "last_seen": 1000.0, "platform": "linux"}
    return hostview.build_host_view(row, {"samples": [sample], "sources": sources}, sources,
                                    [], 1000.0, stale_after, None, {}, None)


def test_a_warning_from_a_section_item_names_its_cause():
    view = hot_view(85.0)
    assert view["status"] == "warning"
    assert view["status_reason"].startswith("temperatures: ")
    assert "hw.temperature" in view["status_reason"]
    assert hostview.summarize(view)["status_reason"] == view["status_reason"]


def test_a_critical_alert_names_its_cause():
    scope = collector_scope("hwmon")
    sources = {"hwmon": {"available": True, "reason": "", "updated": 1000.0}}
    sample = {"source": scope, "metric": "hw.temperature", "labels": {}, "value": 20.0,
              "unit": "Cel", "ts": 1000.0}
    events = [{"severity": "critical", "ts": 990.0, "title": "array degraded"}]
    view = hostview.build_host_view({"host": "h", "last_seen": 1000.0, "platform": "linux"},
                                    {"samples": [sample], "sources": sources}, sources, events,
                                    1000.0, 900.0, None, {}, None)
    assert view["status"] == "critical"
    assert view["status_reason"] == "alerts: array degraded"


def test_a_good_host_has_no_reason():
    view = hot_view(40.0)
    assert view["status"] == "good" and view["status_reason"] == ""
