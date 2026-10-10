"""Stale and never-reported data is not scored as current (bug plan WP3): a neutral "no data"
state for sections and sources with nothing to grade, no threshold breach from a stale reading,
and per-host ignored readings that stay visible but stop counting."""

from __future__ import annotations

import asyncio

from observe import auth

from . import test_host_views as hv
from .test_host_views import env  # noqa: F401  (fixture)

ADMIN_PASSWORD = "correct horse battery admin"


def admin(env) -> dict[str, str]:
    asyncio.run(auth.create_user(env.store, env.cfg, "root", ADMIN_PASSWORD, True,
                                 now=env.clock()))
    r = env.client.post("/api/login", json={"username": "root", "password": ADMIN_PASSWORD})
    assert r.status_code == 200
    return {"X-CSRF-Token": r.json()["csrf"]}


def test_never_reported_host_has_no_up_or_warning_sections(tmp_path):
    env = hv.Env(tmp_path, [{"name": "truenas", "type": "pushed_host", "host": "truenas"}])
    try:
        env.login()
        d = hv.detail(env, "truenas")
        assert d["heard"] is False and d["status"] == "critical"
        for sec in hv.SECTIONS:
            assert d[sec]["status"] == "no_data", sec
            assert d[sec]["state"] == "not_reported", sec
        row = env.client.get("/api/v2/hosts").json()["items"][0]
        assert set(row["sections"].values()) <= {"no_data", "good"}  # alerts is just empty
        assert row["sections"]["cpu"] == "no_data"
    finally:
        env.close()


def test_absent_sources_and_sections_are_no_data(env):
    env.push(hv.batch(samples=[hv.s("cpu", "system.cpu.utilization", 0.05, "1")],
                      sources=[{"source": "cpu", "available": True},
                               {"source": "nut", "available": False, "present": False}]))
    env.login()
    d = hv.detail(env)
    assert d["ups"]["status"] == "no_data" and d["ha"]["status"] == "no_data"
    srcs = {x["source"]: x for x in d["sources"]}
    assert srcs["nut"]["status"] == "no_data" and srcs["cpu"]["status"] == "good"


def test_stale_reading_is_never_a_threshold_breach(env):
    # AUXTIN0 read 99 degrees ten minutes ago: shown, marked stale, but not "past 90".
    env.push(hv.batch(samples=[
        hv.s("hwmon", "hw.temperature", 99.0, "Cel", ts=hv.NOW - 600,
             labels={"hw.id": "nct6798:AUXTIN0"}),
        hv.s("cpu", "system.cpu.utilization", 0.05, "1")],
        sources=[{"source": "hwmon", "available": True}, {"source": "cpu", "available": True}]))
    env.login()
    d = hv.detail(env)
    item = hv.reading(d, "temperatures", "hw.temperature")
    assert item["value"] == 99.0 and item["stale"] is True
    assert item["status"] == "warning" and "past" not in item["reason"]
    assert d["status"] == "warning" and "critical" != d["temperatures"]["status"]


def test_ignoring_floating_sensors_and_empty_fan_header(env):
    temps = [hv.s("hwmon", "hw.temperature", t, "Cel", labels={"hw.id": f"nct6798:{n}"})
             for n, t in (("AUXTIN0", 99.0), ("AUXTIN1", 104.0), ("AUXTIN2", 103.0),
                          ("CPUTIN", 45.0))]
    fans = [hv.s("hwmon", "hw.fan.speed", 0.0, "{rpm}", labels={"hw.id": "nct6798:fan5"}),
            hv.s("hwmon", "hw.fan.speed", 900.0, "{rpm}", labels={"hw.id": "nct6798:fan1"})]
    env.push(hv.batch(samples=temps + fans, sources=[{"source": "hwmon", "available": True}]))
    csrf = admin(env)
    d = hv.detail(env)
    assert d["temperatures"]["status"] == "critical" and d["fans"]["status"] == "critical"
    url = "/api/hosts/nas01/ignored"
    ids = ["nct6798:AUXTIN0", "nct6798:AUXTIN1", "nct6798:AUXTIN2", "nct6798:fan5"]
    assert env.client.put(url, json={"ignored": ids}).status_code == 403  # no CSRF token
    r = env.client.put(url, json={"ignored": ids}, headers=csrf)
    assert r.status_code == 200 and r.json()["ignored"] == sorted(ids)
    d = hv.detail(env)
    assert d["temperatures"]["status"] == "good" and d["fans"]["status"] == "good"
    assert d["status"] == "good"
    # Ignored readings stay on the page, greyed out, never hidden.
    shown = [i for i in d["temperatures"]["items"] + d["fans"]["items"] if i["ignored"]]
    assert sorted(i["labels"]["hw.id"] for i in shown) == sorted(ids)
    audit = asyncio.run(env.store.fetch(
        "SELECT actor, detail FROM audit WHERE kind='host_readings_ignored'"))
    assert len(audit) == 1 and audit[0][0] == "root" and "AUXTIN0" in audit[0][1]
    # A bad body is refused and audited; clearing the list counts them again.
    assert env.client.put(url, json={"ignored": "x"}, headers=csrf).status_code == 422
    assert env.client.put(url, json={"ignored": []}, headers=csrf).status_code == 200
    assert hv.detail(env)["temperatures"]["status"] == "critical"


def test_viewer_cannot_change_ignored_readings(env):
    env.push(hv.batch(samples=[hv.s("cpu", "system.cpu.utilization", 0.05, "1")]))
    env.login()
    csrf = {"X-CSRF-Token": env.client.get("/api/v2/session").json().get("csrf", "")}
    r = env.client.put("/api/hosts/nas01/ignored", json={"ignored": ["a"]}, headers=csrf)
    assert r.status_code in (401, 403)
