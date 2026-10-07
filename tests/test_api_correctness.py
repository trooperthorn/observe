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


def test_a_value_that_could_overflow_a_rollup_is_refused_at_ingest(otlp):
    key = otlp.key("nas01")
    req = metrics_request("nas01", {"s": [gauge("m", [number(1.7e308, T0),
                                                      number(5.0, T0 + 1)])]})
    got = otlp.push(req, key)
    assert got.status_code == 200
    count, message = rejected(got)
    assert count == 1 and "too large" in message
    assert [row[4] for row in otlp.stored()] == [5.0]


def test_the_json_encoder_never_writes_infinity(env):
    from observe.api.registry import _dump
    with pytest.raises(ValueError):
        _dump({"v": float("inf")})


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
