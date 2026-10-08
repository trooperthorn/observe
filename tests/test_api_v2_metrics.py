"""The metrics catalogue, latest values and the time series query (section 4.3 and 10.2)."""

from __future__ import annotations

import asyncio
import math

import pytest

from observe.api import metrics
from observe.api.cursor import encode
from observe.storage import rollups

from .api_env import START, ApiEnv, host_batch


@pytest.fixture
def env(tmp_path):
    e = ApiEnv(tmp_path)
    e.headers = e.token()
    yield e
    e.close()


def get(env, path):
    return env.get(path, headers=env.headers)


def seed_latency(env, minutes=60, every=30, slug="core"):
    """One latency value per `every` seconds for the last `minutes`, equal to its index."""
    n = minutes * 60 // every
    env.poll_many(slug, [(START - (n - i) * every, float(i)) for i in range(n)])
    return n


def settings(env, **days):
    for key, value in days.items():
        asyncio.run(env.store.execute(
            "INSERT INTO app_settings (key, value, updated) VALUES (?, ?, 0) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"retention.{key}", str(value))))


# ---- catalogue and latest -------------------------------------------------------------------

def test_catalogue_lists_metrics_with_units_counts_and_attribute_keys(env):
    env.poll("core", latency=3.0)
    env.poll("nas", latency=4.0)
    env.push(host_batch("nas01", samples=[
        {"source": "hwmon", "metric": "temp", "value": 40.0, "unit": "C",
         "labels": {"chip": "a", "sensor": "s1"}, "ts": START - 5},
        {"source": "hwmon", "metric": "temp", "value": 41.0, "unit": "C",
         "labels": {"chip": "a", "sensor": "s2"}, "ts": START - 5}]))
    items = get(env, "/metrics").json()["items"]
    by = {(i["scope"], i["metric"]): i for i in items}
    lat = by[("observe-monitor", "monitor.latency")]
    assert lat["unit"] == "ms" and lat["series_count"] == 2 and lat["resource_count"] == 2
    temp = by[("hwmon", "temp")]
    assert temp["series_count"] == 2 and temp["resource_count"] == 1
    assert temp["attribute_keys"] == ["chip", "sensor"]
    assert [(i["scope"], i["metric"]) for i in items] == sorted((i["scope"], i["metric"]) for i in items)
    assert [i["metric"] for i in get(env, "/metrics?q=temp").json()["items"]] == ["temp"]
    assert [i["metric"] for i in get(env, "/metrics?q=monitor.&kind=monitor").json()["items"]]
    assert get(env, "/metrics?kind=host&scope=hwmon").json()["items"][0]["scope"] == "hwmon"


def test_catalogue_pages(env):
    env.poll("core", latency=3.0, value=1.0)
    whole = [(i["scope"], i["metric"]) for i in get(env, "/metrics").json()["items"]]
    assert len(whole) >= 4
    seen, cursor = [], None
    while True:
        page = get(env, "/metrics?limit=2" + (f"&cursor={cursor}" if cursor else "")).json()
        seen += [(i["scope"], i["metric"]) for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == whole
    assert get(env, f"/metrics?cursor={encode([1])}").status_code == 400


def test_latest_returns_the_newest_point_and_the_one_before(env):
    env.poll("core", latency=3.0, ts=START - 60)
    env.poll("core", latency=5.0, ts=START - 30)
    body = get(env, "/metrics/latest?metric=monitor.latency&resource=core").json()
    [item] = body["items"]
    assert item["value"] == 5.0 and item["previous_value"] == 3.0 and item["unit"] == "ms"
    assert item["ts"].endswith("Z") and item["previous_ts"] < item["ts"]
    assert item["resource"]["kind"] == "monitor" and item["resource"]["name"] == "core"
    assert get(env, "/metrics/latest").status_code == 400  # a metric or a series id is needed


def test_latest_pages_and_filters_by_attribute(env):
    samples = [{"source": "hwmon", "metric": "temp", "value": 40.0 + i, "unit": "C",
                "labels": {"chip": "c" + str(i % 2), "sensor": f"s{i}"}, "ts": START - 5}
               for i in range(6)]
    env.push(host_batch("nas01", samples=samples))
    base = "/metrics/latest?metric=temp"
    assert len(get(env, base).json()["items"]) == 6
    seen, cursor = [], None
    while True:
        page = get(env, base + "&limit=4" + (f"&cursor={cursor}" if cursor else "")).json()
        seen += [i["series_id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == sorted(seen) and len(seen) == 6
    only = get(env, base + "&match[chip]=c1").json()["items"]
    assert sorted(i["attrs"]["sensor"] for i in only) == ["s1", "s3", "s5"]
    other = get(env, base + "&match[chip]!=c1").json()["items"]
    assert sorted(i["attrs"]["sensor"] for i in other) == ["s0", "s2", "s4"]
    rx = get(env, base + "&match[sensor]=~s?").json()["items"]
    assert len(rx) == 6
    rx = get(env, base + "&match[sensor]=~s[45]").json()["items"]
    assert rx == []
    rx = get(env, base + "&match[sensor]=~s4").json()["items"]
    assert [i["attrs"]["sensor"] for i in rx] == ["s4"]
    assert get(env, base + "&match[missing]=x").json()["items"] == []
    assert len(get(env, base + "&match[missing]!=x").json()["items"]) == 6
    assert len(get(env, "/metrics/latest?metric=te*").json()["items"]) == 6
    ids = [i["series_id"] for i in get(env, base).json()["items"]][:2]
    by_id = get(env, "/metrics/latest?" + "&".join(f"series_id={i}" for i in ids)).json()["items"]
    assert [i["series_id"] for i in by_id] == ids


def test_patterns_over_the_cap_are_refused(env):
    r = get(env, "/metrics/latest?metric=m&match[k]=~" + "a" * 65)
    assert r.status_code == 400 and "not allowed" in r.json()["detail"]


@pytest.mark.parametrize("pattern", ["*" * 10 + "x", "a*" * 12 + "b", ".*" * 10 + "x", "(a+)+$"])
def test_the_audits_catastrophic_patterns_run_in_linear_time(pattern):
    import time
    test = metrics._attr_test("k", "~" + pattern)
    t0 = time.perf_counter()
    assert test({"k": "a" * 1000}) is False
    assert time.perf_counter() - t0 < 0.01


def test_glob_matching_semantics():
    g = metrics.glob_match
    assert g("abc", "abc") and not g("abc", "abcd") and g("ab*", "abcd") and g("*", "")
    assert g("a?c", "abc") and not g("a?c", "ac") and g("*b*d", "abcd") and not g("a*b", "abc")


def test_filtered_latest_pages_through_all_matches_past_the_scan_limit(env, monkeypatch):
    samples = [{"source": "hwmon", "metric": "temp", "value": 40.0, "unit": "C",
                "labels": {"n": ("rare" if i % 5 == 0 else "x") + str(i)}, "ts": START - 5}
               for i in range(40)]
    env.push(host_batch("nas01", samples=samples))
    monkeypatch.setattr(metrics, "MAX_SCAN", 7)
    base = "/metrics/latest?metric=temp&match[n]=~rare*&limit=3"
    seen, cursor, pages = [], None, 0
    while True:
        page = get(env, base + (f"&cursor={cursor}" if cursor else "")).json()
        seen += [i["attrs"]["n"] for i in page["items"]]
        cursor, pages = page["next_cursor"], pages + 1
        if not cursor:
            break
        assert pages < 50
    assert sorted(seen) == sorted(f"rare{i}" for i in range(0, 40, 5))
    assert pages > 3


# ---- the query: levels ----------------------------------------------------------------------

def values_of(body, agg_index):
    return [p[agg_index] for p in body["series"][0]["points"]]


def test_a_short_range_reads_raw_samples_and_matches_them(env):
    n = seed_latency(env, minutes=60, every=30)
    body = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-1h&step=60&agg=avg,min,max,count").json()
    assert body["tier"] == "raw" and body["step"] == 60 and body["aggs"] == ["avg", "min", "max", "count"]
    pts = body["series"][0]["points"]
    assert len(pts) == n // 2
    # Buckets of two samples, 2k and 2k+1: the mean is 2k + 0.5.
    for k, (ts, avg, lo, hi, count) in enumerate(pts):
        assert (avg, lo, hi, count) == (2 * k + 0.5, 2 * k, 2 * k + 1, 2)
        assert ts % 60 == 0
    assert pts[0][0] == (START - 3600) // 60 * 60
    s = body["series"][0]
    assert s["metric"] == "monitor.latency" and s["unit"] == "ms" and s["scope"] == "observe-monitor"
    assert s["resource"]["name"] == "core" and body["start"].endswith("Z")


def test_the_tier_follows_the_step(env):
    seed_latency(env, minutes=120, every=30)
    base = "/metrics/query?metric=monitor.latency&resource=core&from=-2h&"
    assert get(env, base + "step=299").json()["tier"] == "raw"
    five = get(env, base + "step=300").json()
    assert five["tier"] == "rollup_5m" and five["step"] == 300
    assert get(env, base + "step=3599").json()["tier"] == "rollup_5m"
    hourly = get(env, base + "step=3600").json()
    assert hourly["tier"] == "rollup_1h" and hourly["step"] == 3600
    assert get(env, base + "step=86400").json()["tier"] == "rollup_1d"
    auto = get(env, base.rstrip("&")).json()
    assert auto["tier"] == "raw" and auto["step"] == 15  # 7,200 s over 500 points rounds up to 15 s


def test_a_summary_level_answers_with_min_max_and_avg_that_match_the_raw_data(env):
    seed_latency(env, minutes=120, every=30)  # 240 samples, value = index
    raw = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-2h&step=299&agg=sum,count").json()
    assert raw["tier"] == "raw"
    assert sum(p[2] for p in raw["series"][0]["points"]) == 240
    assert sum(p[1] for p in raw["series"][0]["points"]) == sum(range(240))
    five = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-2h&step=600").json()
    assert five["tier"] == "rollup_5m" and five["aggs"] == ["avg", "min", "max"]
    expected: dict[int, list[float]] = {}
    for i in range(240):  # the samples seed_latency wrote: value i at START - (240 - i) * 30
        expected.setdefault(int((START - (240 - i) * 30) // 600 * 600), []).append(float(i))
    got = {int(ts): (avg, lo, hi) for ts, avg, lo, hi in five["series"][0]["points"]}
    assert got == {b: (sum(v) / len(v), min(v), max(v)) for b, v in expected.items()}
    hour = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-2h&step=3600&agg=avg,max,count").json()
    total = sum(p[3] for p in hour["series"][0]["points"])
    assert total == 240
    assert max(p[2] for p in hour["series"][0]["points"]) == 239
    daily = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-2h&step=86400&agg=count,max").json()
    assert daily["tier"] == "rollup_1d" and daily["series"][0]["points"][0][1:] == [240, 239]


def test_last_needs_the_raw_level(env):
    seed_latency(env, minutes=10, every=30)
    ok = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-10m&step=60&agg=last,max").json()
    assert ok["tier"] == "raw" and [p[1] for p in ok["series"][0]["points"]][:3] == [1, 3, 5]
    bad = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-10m&step=600&agg=last")
    assert bad.status_code == 400 and "raw" in bad.json()["detail"]
    assert get(env, "/metrics/query?metric=m&agg=rate").status_code == 400
    assert get(env, "/metrics/query?metric=m&agg=median").status_code == 400
    assert get(env, "/metrics/query?metric=m&agg=avg,avg").status_code == 400


def test_a_range_older_than_a_level_moves_to_a_coarser_one(env):
    settings(env, raw_days=1)
    for hours in (60, 59, 58):  # about two and a half days ago
        env.poll("core", ts=START - hours * 3600, latency=float(hours))
    body = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-3d&step=60").json()
    assert body["tier"] == "rollup_5m" and body["step"] == 300
    assert "no longer holds" in body["note"]
    assert len(body["series"][0]["points"]) == 3
    # Inside raw retention the raw level is kept.
    near = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-12h&step=60").json()
    assert near["tier"] == "raw" and near["note"] is None


def test_the_step_is_raised_to_keep_at_most_a_thousand_points(env):
    for days in (1, 5, 10, 20):
        env.poll("core", ts=START - days * 86400, latency=float(days))
    body = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-10d&step=1").json()
    assert body["step"] >= math.ceil(10 * 86400 / 999) and "raised" in body["note"]
    assert body["tier"] == "rollup_5m" and body["step"] % 300 == 0
    assert all(len(s["points"]) <= 1000 for s in body["series"])
    big = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-30d&step=1").json()
    assert big["tier"] == "rollup_1h" and big["step"] == 3600  # 5 minute data is kept 14 days
    assert len(big["series"][0]["points"]) == 4  # every sample is in the hourly level
    # Even on raw data the bound holds.
    seed_latency(env, minutes=60, every=1, slug="nas")
    raw = get(env, "/metrics/query?metric=monitor.latency&resource=nas&from=-1h&step=1").json()
    assert raw["tier"] == "raw" and raw["step"] >= 4
    assert max(len(s["points"]) for s in raw["series"]) <= 1000


def test_a_query_returns_at_most_fifty_series(env):
    samples = [{"source": "hwmon", "metric": "temp", "value": float(i), "unit": "C",
                "labels": {"n": str(i)}, "ts": START - 5} for i in range(60)]
    env.push(host_batch("nas01", samples=samples))
    body = get(env, "/metrics/query?metric=temp&from=-1h").json()
    assert len(body["series"]) == 50 and body["series_truncated"] is True
    few = get(env, "/metrics/query?metric=temp&from=-1h&limit_series=3").json()
    assert len(few["series"]) == 3 and few["series_truncated"] is True
    assert get(env, "/metrics/query?metric=temp&limit_series=51").status_code == 400
    all60 = get(env, "/metrics/query?metric=temp&from=-1h&match[n]=~1?").json()
    assert len(all60["series"]) == 10 and all60["series_truncated"] is False


def test_query_selection_by_attribute_resource_kind_and_scope(env):
    samples = [{"source": "hwmon", "metric": "temp", "value": 1.0, "unit": "C",
                "labels": {"sensor": s}, "ts": START - 5} for s in ("a", "b")]
    env.push(host_batch("nas01", samples=samples))
    env.push(host_batch("nas02", samples=samples))
    q = "/metrics/query?from=-1h&metric=temp&"
    assert len(get(env, q + "resource=nas01").json()["series"]) == 2
    assert len(get(env, q + "kind=host&scope=hwmon&match[sensor]=a").json()["series"]) == 2
    assert get(env, q + "kind=monitor").json()["series"] == []
    assert len(get(env, q + "match[sensor]!=a").json()["series"]) == 2


def test_post_takes_the_same_query_in_a_body_and_needs_csrf_for_a_session(env):
    seed_latency(env, minutes=10, every=30)
    body = {"metric": "monitor.latency", "resource": "core", "from": "-10m", "step": 60,
            "agg": ["max"]}
    r = env.client.post("/api/v2/metrics/query", json=body, headers=env.headers)
    assert r.status_code == 200 and r.json()["tier"] == "raw" and r.json()["aggs"] == ["max"]
    assert "etag" not in r.headers
    bad = env.client.post("/api/v2/metrics/query", json={**body, "limit_series": 99},
                          headers=env.headers)
    assert bad.status_code == 400
    csrf = env.login("alice", admin=False)
    env.client.headers.pop("Authorization", None)
    assert env.client.post("/api/v2/metrics/query", json=body).status_code == 403
    assert env.client.post("/api/v2/metrics/query", json=body, headers=csrf).status_code == 200


def test_query_time_forms(env):
    seed_latency(env, minutes=10, every=30)
    q = "/metrics/query?metric=monitor.latency&resource=core&step=60&"
    rfc = get(env, q + "from=2023-11-14T22:03:20Z&to=2023-11-14T22:13:20Z").json()
    unix = get(env, q + f"from={START - 600}&to={START}").json()
    rel = get(env, q + "from=-10m&to=now").json()
    assert rfc["series"] == unix["series"] == rel["series"]
    for bad in ("from=tomorrow", "from=-10", "to=-1h&from=now", "from=1e400", "from=-99999999d"):
        assert get(env, q + bad).status_code == 400, bad


def test_the_query_needs_a_metric_or_series_ids(env):
    assert get(env, "/metrics/query?from=-1h").status_code == 400
    env.poll("core", latency=1.0)
    sid = get(env, "/metrics/latest?metric=monitor.latency").json()["items"][0]["series_id"]
    assert get(env, f"/metrics/query?series_id={sid}&from=-1h").status_code == 200


def test_the_reads_send_postgresql_text_and_answer_the_same(env):
    """The catalogue, the latest values, the event feed and the query run through the PostgreSQL
    rewrite (the dialect fake, no server) and answer what they answer on SQLite."""
    from types import SimpleNamespace

    from observe.api import events
    from observe.api.cursor import PageParams
    from observe.api.models import MetricQuery
    from observe.checks.base import CheckResult, Result
    from observe.state import State, Transition

    from .fakes.pg_fake import PgFakeStorage, _outside_quotes
    from .test_storage import _store_on

    pg = PgFakeStorage()
    try:
        st = _store_on(pg)
        async def seed() -> None:
            for i in range(120):
                await st.record("core", START - (120 - i) * 30,
                                CheckResult(Result.OK, "x", value=float(i), latency_ms=float(i)))
        asyncio.run(seed())
        asyncio.run(st.record_event("core", Transition(State.UP, State.DOWN, START - 5, "down")))
        seed_latency(env, minutes=60, every=30)
        env.poll("core", value=1.0)  # the same series set as the fake: value, result, up, latency
        ctx = SimpleNamespace(now=START, config=env.cfg)
        page = PageParams(100, None)

        def both(fn):
            return (pg.read_sync(fn), env.store.storage.read_sync(fn))

        for step in (60, 600, 3600, 86400):
            q = MetricQuery.model_validate({"metric": "monitor.latency", "resource": "core",
                                            "from": "-1h", "step": step, "agg": ["avg", "max", "count"]})
            fake, real = both(lambda db: metrics.run_query(db, ctx, q))
            assert fake["tier"] == real["tier"] and fake["step"] == real["step"]
            assert fake["series"][0]["points"] and real["series"][0]["points"]
        fake_cat, real_cat = both(lambda db: metrics.list_metrics(db, page, q=None, scope=None, kind=None))
        assert [(i["scope"], i["metric"], i["unit"]) for i in fake_cat["items"]] ==             [(i["scope"], i["metric"], i["unit"]) for i in real_cat["items"]]
        fake_ev, real_ev = both(lambda db: events.list_events(
            db, ctx, page, resource=None, kind=None, event_name=None, severity_min=None,
            since=None, until=None))
        assert [i["id"] for i in fake_ev["items"]] == [f"m:core:{int((START - 5) * 1000)}"]
        assert real_ev["items"] == []  # the real store has no events here
        fake_ev, _ = both(lambda db: events.list_events(
            db, ctx, PageParams(100, None), resource="core", kind="monitor",
            event_name="observe.*", severity_min=13, since="-1h", until="now"))
        assert len(fake_ev["items"]) == 1
        bare = [_outside_quotes(sql) for sql in pg.statements]
        assert all("?" not in b for b in bare)
        assert any("GROUP BY 1, 2" in b for b in bare) and not any("rowid" in b.lower() for b in bare)
    finally:
        pg.close()


def test_retention_levels_are_read_from_the_admin_settings():
    levels = rollups.RetentionLevels(raw_days=2, rollup_5m_days=3, hourly_days=100, daily_days=500)
    now = START
    plan = metrics._plan
    assert plan(levels, now, now - 3600, now, 60)[0] == "raw"
    assert plan(levels, now, now - 3 * 86400, now, 60)[0] == "rollup_5m"
    assert plan(levels, now, now - 4 * 86400, now, 60)[0] == "rollup_1h"
    assert plan(levels, now, now - 200 * 86400, now, 60)[0] == "rollup_1d"
    assert plan(levels, now, now - 600 * 86400, now, 60)[0] == "rollup_1d"  # the last level
    with pytest.raises(Exception):
        plan(levels, now, now, now, None)


def test_a_metric_with_a_shorter_raw_override_is_served_from_the_level_that_holds_it(env):
    for hours in (60, 59, 58):  # about two and a half days ago, inside the global raw level
        env.poll("core", ts=START - hours * 3600, latency=float(hours))
    path = "/metrics/query?metric=monitor.latency&resource=core&from=-3d&step=60"
    before = get(env, path).json()
    assert before["tier"] == "raw" and before["complete"] is True
    assert len(before["series"][0]["points"]) == 3
    asyncio.run(env.store.execute(
        "INSERT INTO app_settings (key, value, updated) VALUES (?, ?, 0)",
        ("retention.overrides", '{"monitor.latency": {"raw_days": 1}}')))
    after = get(env, path + "&agg=avg,min,max").json()  # a new key, so no cached reply
    assert after["tier"] == "rollup_5m" and "no longer holds" in after["note"]
    assert len(after["series"][0]["points"]) == 3
    # A metric without an override still uses the global level.
    plain = get(env, "/metrics/query?metric=monitor.up&resource=core&from=-3d&step=60").json()
    assert plain["tier"] == "raw"


def test_a_range_older_than_every_level_says_it_is_incomplete(env):
    env.poll("core", ts=START - 3600, latency=1.0)
    old = get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-800d").json()
    assert old["tier"] == "rollup_1d" and old["complete"] is False
    assert "earliest part is missing" in old["note"]
    assert get(env, "/metrics/query?metric=monitor.latency&resource=core&from=-30d"
               ).json()["complete"] is True


def test_the_plan_honours_overrides_for_the_names_it_is_given():
    levels = rollups.RetentionLevels(raw_days=7, overrides={"short": {"raw_days": 1}})
    now = START
    plan = metrics._plan
    assert plan(levels, now, now - 3 * 86400, now, 60, ["long"])[0] == "raw"
    assert plan(levels, now, now - 3 * 86400, now, 60, ["long", "short"])[0] == "rollup_5m"
    assert plan(levels, now, now - 3 * 86400, now, 60)[0] == "raw"
