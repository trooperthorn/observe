"""The core /api/v2 resources: monitors, groups, status, hosts and events."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from observe.checks.base import CheckResult, Result
from observe.state import State, Transition

from .api_env import START, ApiEnv, host_batch


@pytest.fixture
def env(tmp_path):
    e = ApiEnv(tmp_path)
    e.headers = e.token()
    yield e
    e.close()


def get(env, path, **kw):
    return env.get(path, headers=env.headers, **kw)


def event(kind="md.degraded", severity="warning", ts=START - 100, key="k1", title="t", detail=None):
    return {"kind": kind, "severity": severity, "source": "journal", "ts": ts, "title": title,
            "dedup_key": key, "detail": detail or {}}


# ---- monitors -------------------------------------------------------------------------------

def test_monitor_list_has_the_dashboard_fields_and_rfc3339_times(env):
    env.poll("core", Result.OK, value=1.0)
    env.sched.states["core"].last_at = START
    body = get(env, "/monitors").json()
    assert body["next_cursor"] is None
    core = next(m for m in body["items"] if m["slug"] == "core")
    assert core["name"] == "core" and core["group"] == "net" and core["target"] == "10.0.0.1"
    assert core["detail"] is None and core["depends_on"] == []
    assert core["last_at"] == "2023-11-14T22:13:20.000Z"
    datetime.fromisoformat(core["since"].replace("Z", "+00:00"))
    edge = next(m for m in body["items"] if m["slug"] == "edge")
    assert edge["depends_on"] == ["core"] and edge["message"] == "waiting for first poll"


def test_monitor_filters_sort_and_expansion(env):
    env.sched.states["nas"].state = State.DOWN
    items = get(env, "/monitors?state=down").json()["items"]
    assert [m["slug"] for m in items] == ["nas"]
    assert [m["slug"] for m in get(env, "/monitors?group=net").json()["items"]] == ["core", "edge"]
    assert [m["slug"] for m in get(env, "/monitors?q=10.0.0.3").json()["items"]] == ["nas"]
    assert [m["slug"] for m in get(env, "/monitors?sort=-name").json()["items"]] == \
        ["nas", "edge", "core"]
    assert get(env, "/monitors?include=nonsense").status_code == 400
    env.sched.states["core"].last = CheckResult(
        Result.OK, "fine", detail={"ports": {"1": {"speed": 1000}}})
    plain = get(env, "/monitors").json()["items"]
    assert all(m["detail"] is None for m in plain)
    full = get(env, "/monitors?include=detail").json()["items"]
    assert next(m for m in full if m["slug"] == "core")["detail"] == {"ports": {"1": {"speed": 1000}}}
    d = get(env, "/monitors/core/detail").json()
    assert d == {"slug": "core", "detail": {"ports": {"1": {"speed": 1000}}}}


def test_sparse_fields(env):
    items = get(env, "/monitors?fields=slug,state").json()["items"]
    assert items[0] == {"slug": "core", "state": "pending"}
    assert get(env, "/monitors?fields=slug,bogus").status_code == 400


def test_monitor_cursor_pages_in_every_sort_order(env):
    for sort in ("slug", "-slug", "name,-group", "-since,slug"):
        seen, cursor = [], None
        while True:
            url = f"/monitors?limit=1&sort={sort}" + (f"&cursor={cursor}" if cursor else "")
            page = get(env, url).json()
            seen += [m["slug"] for m in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        whole = [m["slug"] for m in get(env, f"/monitors?sort={sort}").json()["items"]]
        assert seen == whole and sorted(seen) == ["core", "edge", "nas"], sort
    assert get(env, "/monitors?cursor=AAAA").status_code == 400


def test_one_monitor_has_availability(env):
    env.poll("core", Result.OK, ts=START - 3600)
    env.poll("core", Result.FAIL, ts=START - 1800)
    body = get(env, "/monitors/core").json()
    assert body["availability_24h"] == 50.0 and body["availability_7d"] == 50.0
    assert get(env, "/monitors/none").status_code == 404
    assert get(env, "/monitors/none/detail").status_code == 404


def test_blocked_and_held_children_match_the_dashboard_rules(env):
    core = env.sched.states["core"]
    core.state, core.degraded = State.WARN, True
    by = {m["slug"]: m for m in get(env, "/monitors").json()["items"]}
    assert by["core"]["degraded"] is True and by["edge"]["held_by"] == "core"
    core.state, core.degraded = State.DOWN, False
    env.sched.states["edge"].state = State.DOWN
    by = {m["slug"]: m for m in get(env, "/monitors").json()["items"]}
    assert by["edge"]["blocked_by"] == "core" and by["edge"]["effective_state"] == "unreachable"


# ---- groups and status ----------------------------------------------------------------------

def test_groups(env):
    env.sched.states["nas"].state = State.DOWN
    body = get(env, "/groups").json()
    assert [g["name"] for g in body["items"]] == ["net", "storage"]
    assert body["items"][1]["state"] == "down" and body["items"][1]["worst"] == ["nas"]
    page = get(env, "/groups?limit=1").json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    rest = get(env, f"/groups?limit=1&cursor={page['next_cursor']}").json()
    assert [g["name"] for g in rest["items"]] == ["storage"] and rest["next_cursor"] is None


def test_status_reports_version_and_alert_delivery(tmp_path):
    e = ApiEnv(tmp_path)
    try:
        e.alerter.status["hook"] = {"type": "webhook", "sent": 2, "last_error": "refused"}
        body = e.get("/status", headers=e.token()).json()
        assert body["version"] and body["alerts"] == [
            {"name": "hook", "type": "webhook", "sent": 2, "last_error": "refused"}]
    finally:
        e.close()


# ---- hosts ----------------------------------------------------------------------------------

def test_host_list_and_detail(env):
    env.push(host_batch("nas01"))
    env.push(host_batch("alpha"))
    env.push(host_batch("rack/nas 01?#%"))
    body = get(env, "/hosts").json()
    assert [h["host"] for h in body["items"]] == ["alpha", "nas01", "rack/nas 01?#%"]
    first = body["items"][0]
    assert first["last_seen"].endswith("Z") and first["sections"]["cpu"] == "good"
    assert set(first) >= {"status", "stale", "confirmed", "monitored", "states"}
    detail = get(env, "/hosts/nas01").json()
    assert detail["host"] == "nas01" and detail["cpu"]["status"] == "good"
    assert detail["sources"][0]["updated"].endswith("Z")
    assert all(i["ts"].endswith("Z") for i in detail["cpu"]["items"])
    odd = get(env, "/hosts/rack%2Fnas%2001%3F%23%25")
    assert odd.status_code == 200 and odd.json()["host"] == "rack/nas 01?#%"
    assert get(env, "/hosts/rack/nas 01?#%".replace("?#%", "%3F%23%25")).status_code == 200
    miss = get(env, "/hosts/nope")
    assert miss.status_code == 404 and miss.json()["type"].endswith("/not-found")


def test_host_pages_and_filter(env):
    for name in ("a1", "b2", "c3"):
        env.push(host_batch(name))
    page = get(env, "/hosts?limit=2").json()
    assert [h["host"] for h in page["items"]] == ["a1", "b2"] and page["next_cursor"]
    rest = get(env, f"/hosts?limit=2&cursor={page['next_cursor']}").json()
    assert [h["host"] for h in rest["items"]] == ["c3"] and rest["next_cursor"] is None
    assert [h["host"] for h in get(env, "/hosts?q=B").json()["items"]] == ["b2"]


def test_a_host_from_the_config_that_never_reported(tmp_path):
    e = ApiEnv(tmp_path, monitors=[{"name": "nas", "type": "pushed_host", "host": "nas01",
                                    "interval": 30}])
    try:
        h = e.get("/hosts", headers=e.token()).json()["items"]
        assert h[0]["host"] == "nas01" and h[0]["heard"] is False and h[0]["monitored"] is True
        assert h[0]["last_seen"] is None
    finally:
        e.close()


def test_waiting_hosts_are_listed_until_they_report(tmp_path):
    env = ApiEnv(tmp_path, public_url="https://observe.example")
    try:
        env.login("root", admin=True)
        r = env.client.post("/api/hosts", json={"name": "newbox", "platform": "linux",
                                                "agent": True, "control": False},
                            headers=env.csrf)
        assert r.status_code == 200, r.text
        waiting = env.get("/waiting-hosts").json()["items"]
        assert [w["host"] for w in waiting] == ["newbox"]
        assert waiting[0]["created"].endswith("Z")
        env.push(host_batch("newbox"))
        assert env.get("/waiting-hosts").json()["items"] == []
    finally:
        env.close()


# ---- events ---------------------------------------------------------------------------------

def add_monitor_event(env, monitor, ts, current="down", previous="up", message="m"):
    asyncio.run(env.store.record_event(
        monitor, Transition(State(previous), State(current), ts, message)))


def test_events_merge_monitor_transitions_and_host_events(env):
    add_monitor_event(env, "core", START - 50, "down", "up", "core down: refused")
    env.push(host_batch("nas01", events=[
        event("md.degraded", "warning", START - 100, "a", "array degraded", {"array": "md0"}),
        event("boot.unclean_shutdown", "critical", START - 200, "b", "unclean")]))
    items = get(env, "/events").json()["items"]
    assert [i["event_name"] for i in items] == [
        "observe.monitor.transition", "hostwatch.md.degraded", "hostwatch.boot.unclean_shutdown"]
    first = items[0]
    assert first["severity_number"] == 17 and first["severity_text"] == "ERROR"
    assert first["resource"] == {"kind": "monitor", "name": "core"}
    assert first["attributes"]["observe.monitor.state.previous"] == "up"
    assert first["body"] == "core down: refused" and first["ts"].endswith("Z")
    assert items[1]["attributes"]["observe.detail.array"] == "md0"
    assert items[1]["severity_number"] == 13 and items[2]["severity_number"] == 17


def test_event_filters(env):
    add_monitor_event(env, "core", START - 50, "down", "up")
    add_monitor_event(env, "nas", START - 40, "up", "down", "back")
    env.push(host_batch("nas01", events=[event(ts=START - 100, key="a"),
                                         event("zfs.degraded", "critical", START - 90, "b")]))
    names = lambda q: [i["event_name"] + ":" + i["resource"]["name"]
                       for i in get(env, "/events?" + q).json()["items"]]
    assert names("resource=core") == ["observe.monitor.transition:core"]
    assert names("kind=host") == ["hostwatch.zfs.degraded:nas01", "hostwatch.md.degraded:nas01"]
    assert names("kind=monitor") == ["observe.monitor.transition:nas",
                                     "observe.monitor.transition:core"]
    assert names("event_name=hostwatch.md.degraded") == ["hostwatch.md.degraded:nas01"]
    assert names("event_name=hostwatch.*") == ["hostwatch.zfs.degraded:nas01",
                                               "hostwatch.md.degraded:nas01"]
    assert names("event_name=hostwatch.zfs*") == ["hostwatch.zfs.degraded:nas01"]
    assert names("event_name=observe.*") == ["observe.monitor.transition:nas",
                                             "observe.monitor.transition:core"]
    assert sorted(names("severity_min=17")) == ["hostwatch.zfs.degraded:nas01",
                                                "observe.monitor.transition:core"]
    assert names(f"since={START - 95}&until={START - 45}") == [
        "observe.monitor.transition:core", "hostwatch.zfs.degraded:nas01"]
    assert names("since=-1m") == ["observe.monitor.transition:nas",
                                  "observe.monitor.transition:core"]
    assert get(env, "/events?since=yesterday").status_code == 400
    assert get(env, "/events?severity_min=99").status_code == 400
    assert get(env, "/events?kind=other").status_code == 400


def test_event_pages_are_stable_while_events_arrive(env):
    for i in range(6):
        add_monitor_event(env, f"m{i}", START - 1000 + i * 10, "down", "up", f"e{i}")
    env.push(host_batch("nas01", events=[event(ts=START - 1000 + i * 10 + 5, key=f"h{i}",
                                               title=f"h{i}") for i in range(4)]))
    whole = [i["id"] for i in get(env, "/events?limit=500").json()["items"]]
    assert len(whole) == 10
    first = get(env, "/events?limit=4").json()
    assert [i["id"] for i in first["items"]] == whole[:4] and first["next_cursor"]
    # New events arrive between the two requests: they sort before everything on page one.
    add_monitor_event(env, "late1", START - 1, "down", "up")
    env.push(host_batch("nas01", events=[event(ts=START - 2, key="late2")]))
    second = get(env, f"/events?limit=4&cursor={first['next_cursor']}").json()
    assert [i["id"] for i in second["items"]] == whole[4:8]
    third = get(env, f"/events?limit=4&cursor={second['next_cursor']}").json()
    assert [i["id"] for i in third["items"]] == whole[8:] and third["next_cursor"] is None


def test_event_pages_do_not_skip_events_that_share_an_instant(env):
    for i in range(3):
        add_monitor_event(env, f"m{i}", START - 100, "down", "up")
    env.push(host_batch("nas01", events=[event(ts=START - 100, key=f"h{i}") for i in range(3)]))
    seen, cursor = [], None
    while True:
        page = get(env, "/events?limit=2" + (f"&cursor={cursor}" if cursor else "")).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 6


def test_event_cursor_must_be_well_formed(env):
    from observe.api.cursor import encode
    for bad in (encode([1.0, 1, 5]), encode(["x", 1, "m"]), encode([1.0, 0, "text"]),
                encode([1.0, 1]), "zzzz"):
        assert get(env, f"/events?cursor={bad}").status_code == 400, bad
