"""A source that was present and then disappeared is not hardware the host never had (R3): it
is Critical with when it was last seen and how it clears, for as long as it stays missing, and
an admin can accept that it is gone (audited). Also: a source with no reason borrows the newest
event about it, reasons write values in the Value column's units, and a section with no data
always says why."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from observe.checks.host import grade_components
from observe.storage.schema import SOURCE_PRESENT_TABLES
from observe.tiers import Staleness
from observe.units import value_text

from . import test_host_views as hv
from .test_host_no_data import admin
from .test_host_views import env  # noqa: F401  (fixture)

HOURS_13 = 13 * 3600
TRUENAS = [hv.s("truenas", "hw.temperature", 40.0, "Cel", labels={"hw.id": f"disk:sd{c}"})
           for c in "abc"] + [hv.s("truenas", "hw.status", 1.0, "1",
                                   labels={"hw.id": "zpool:tank", "hw.state": "healthy"})]
GONE_EVENT = {"kind": "source.disappeared", "severity": "critical", "source": "hostwatch.agent",
              "title": "Source truenas disappeared after it had been present",
              "dedup_key": "gone:truenas"}


def _cpu(now: float) -> list:
    return [hv.s("cpu", "system.cpu.utilization", 0.05, "1", ts=now - 5)]


def _disappear(env, events: list | None = None) -> None:
    """truenas reports, then 13 hours later the agent says it is not present."""
    env.push(hv.batch(samples=_cpu(env.clock.now) + TRUENAS,
                      sources=[{"source": "cpu", "available": True},
                               {"source": "truenas", "available": True},
                               {"source": "win_storage", "available": False, "present": False}]))
    env.clock.now += HOURS_13
    now = env.clock.now
    env.push(hv.batch(samples=_cpu(now), sent=now - 5, events=events or [],
                      sources=[{"source": "cpu", "available": True},
                               {"source": "truenas", "available": False, "present": False},
                               {"source": "win_storage", "available": False,
                                "present": False}]))


def test_a_source_that_disappeared_is_critical_and_says_how_it_clears(env):
    _disappear(env)
    env.login()
    d = hv.detail(env)
    srcs = {x["source"]: x for x in d["sources"]}
    # Never seen: still "not present", claims nothing.
    assert srcs["win_storage"]["state"] == "absent" and srcs["win_storage"]["status"] == "no_data"
    assert srcs["win_storage"]["present_at"] is None
    # Seen, then gone: Critical, with when it was last seen.
    gone = srcs["truenas"]
    assert gone["state"] == "gone" and gone["status"] == "critical" and gone["gone"]
    assert gone["present_at"] == hv.NOW - 5
    assert "last seen 13h ago" in gone["reason"] and "reports truenas again" in gone["reason"]
    for name in ("zfs", "disks"):
        assert d[name]["state"] == "gone" and d[name]["status"] == "critical"
        assert "truenas disappeared, last seen 13h ago" in d[name]["note"]
        assert "not present" not in d[name]["note"]
    assert d["status"] == "critical"
    assert "truenas disappeared" in d["status_reason"] and "clears" in d["status_reason"]


def test_the_host_clears_when_the_source_returns_despite_the_recent_event(env):
    ev = {**GONE_EVENT, "ts": hv.NOW + HOURS_13 - 60}
    _disappear(env, events=[ev])
    env.login()
    d = hv.detail(env)
    titles = [i["title"] for i in d["alerts"]["items"]]
    assert d["status"] == "critical"
    # The agent's event is replaced by the live condition, which names what is missing.
    assert GONE_EVENT["title"] not in titles
    assert any("truenas disappeared" in t for t in titles)
    now = env.clock.now = env.clock.now + 60
    env.push(hv.batch(samples=_cpu(now) + [dict(x, ts=now - 5) for x in TRUENAS], sent=now - 5,
                      sources=[{"source": "cpu", "available": True},
                               {"source": "truenas", "available": True}]))
    d = hv.detail(env)
    assert d["status"] == "good", d["status_reason"]
    assert d["alerts"]["status"] == "good"
    # The event is still listed among the recent events.
    assert GONE_EVENT["title"] in [e["title"] for e in d["events"]]


def test_a_disappeared_event_for_a_source_the_store_never_saw_still_counts(env):
    ev = {**GONE_EVENT, "ts": hv.NOW - 60, "title": "Source mystery disappeared after it had "
          "been present", "dedup_key": "gone:mystery"}
    env.push(hv.batch(samples=_cpu(hv.NOW), events=[ev],
                      sources=[{"source": "cpu", "available": True}]))
    env.login()
    d = hv.detail(env)
    assert d["status"] == "critical" and "mystery disappeared" in d["status_reason"]


def test_an_admin_accepts_that_a_source_is_gone(env):
    _disappear(env)
    url = "/api/hosts/nas01/sources/truenas/forget"
    env.login()
    csrf = {"X-CSRF-Token": env.client.get("/api/v2/session").json().get("csrf", "")}
    assert env.client.post(url, json={}, headers=csrf).status_code == 403  # a viewer
    csrf = admin(env)
    assert env.client.post(url, json={}).status_code == 403  # no CSRF token
    assert env.client.post("/api/hosts/nas01/sources/cpu/forget", json={},
                           headers=csrf).status_code == 404  # not a disappeared source
    r = env.client.post(url, json={}, headers=csrf)
    assert r.status_code == 200 and r.json()["forgotten"] is True
    audit = asyncio.run(env.store.fetch(
        "SELECT actor, detail FROM audit WHERE kind='host_source_forgotten'"))
    assert len(audit) == 1 and audit[0][0] == "root" and "truenas" in audit[0][1]
    d = hv.detail(env)
    srcs = {x["source"]: x for x in d["sources"]}
    assert srcs["truenas"]["state"] == "absent" and srcs["truenas"]["status"] == "no_data"
    assert d["status"] == "good", d["status_reason"]
    assert env.client.post(url, json={}, headers=csrf).status_code == 404  # nothing left


def test_the_upgrade_recovers_last_present_from_stored_readings(env):
    _disappear(env)

    def backfill(db):
        db.execute("UPDATE host_sources SET present_at = NULL")
        for stmt in SOURCE_PRESENT_TABLES[1:]:
            db.execute(stmt)
    asyncio.run(env.store.storage.write(backfill))
    got = asyncio.run(env.store.host_sources("nas01"))
    assert got["truenas"]["present_at"] == hv.NOW - 5  # the newest truenas reading
    assert got["win_storage"]["present_at"] is None
    assert got["cpu"]["present_at"] == got["cpu"]["updated"]


def test_a_source_without_a_reason_takes_the_newest_event_about_it(env):
    why = "energy_uj not readable; run scripts/rapl-access.sh"
    env.push(hv.batch(samples=[hv.s("rapl", "hw.power", 9.0, "W", labels={"hw.id": "rapl:0"})],
                      events=[{"kind": "collector.error", "severity": "warning", "source": "rapl",
                               "ts": hv.NOW - 30, "title": why, "dedup_key": "rapl:1"},
                              {"kind": "collector.error", "severity": "warning",
                               "source": "rapl", "ts": hv.NOW - 3000, "title": "older",
                               "dedup_key": "rapl:0"}],
                      sources=[{"source": "rapl", "available": False}]))
    env.login()
    d = hv.detail(env)
    assert d["power"]["note"] == f"rapl: {why}"
    assert f"source unavailable: {why}" in d["power"]["items"][0]["reason"]
    assert {x["source"]: x for x in d["sources"]}["rapl"]["reason"] == why
    assert "no reason given" not in d["status_reason"]


def test_a_source_with_no_reason_and_no_event_says_so(env):
    env.push(hv.batch(samples=[], sources=[{"source": "rapl", "available": False}]))
    env.login()
    assert hv.detail(env)["power"]["note"] == "rapl: no reason given"


def test_reasons_write_values_in_the_value_columns_units(env):
    env.push(hv.batch(samples=[
        hv.s("memory", "system.memory.limit", 10e9, "By"),
        hv.s("memory", "system.memory.usage", 9.248e9, "By",
             labels={"system.memory.state": "used"}),
        hv.s("hwmon", "hw.temperature", 93.0, "Cel", labels={"hw.id": "nvme:Composite"})],
        sources=[{"source": "memory", "available": True}, {"source": "hwmon", "available": True}]))
    env.login()
    d = hv.detail(env)
    util = hv.reading(d, "memory", "system.memory.utilization")
    assert util["reason"] == "92.5 % is at or past 90 %"
    temp = hv.reading(d, "temperatures", "hw.temperature")
    assert temp["reason"] == "93°C is at or past 90°C"


def test_every_no_data_section_says_why(env):
    env.push(hv.batch(samples=[hv.s("hwmon", "hw.temperature", 41.0, "Cel",
                                    labels={"hw.id": "coretemp:0"})],
                      sources=[{"source": "hwmon", "available": True},
                               {"source": "nut", "available": False, "present": False}]))
    env.login()
    d = hv.detail(env)
    for name in ("cpu", "memory", "power", "temperatures", "fans", "raid", "zfs", "disks", "ups",
                 "ha", "containers", "integrations", "repairs", "backups", "network"):
        if d[name]["status"] == "no_data":
            assert d[name]["note"], name
    assert d["power"]["note"] == "hwmon reports but sends no reading for this section"


def test_component_reasons_use_the_value_columns_units():
    th = SimpleNamespace(source="hostwatch.collector.memory", metric="system.memory.utilization",
                         direction="above", warn=0.9, crit=0.97)
    mon = SimpleNamespace(components=[th], require_sources=[], crash_hold_s=0,
                          crash_result="fail")
    sample = {"source": th.source, "metric": th.metric, "value": 0.9248, "unit": "1",
              "ts": hv.NOW - 5, "labels": {}}
    levels, reasons = grade_components(mon, [sample], {}, Staleness(300.0), hv.NOW, None, None)
    assert levels == {"memory.system.memory.utilization": "warning"}
    assert reasons["memory.system.memory.utilization"] == "92.5 % past 90 %"


def test_value_text_matches_the_page_formatter():
    assert value_text(0.9248, "1") == "92.5 %"
    assert value_text(0.9, "1") == "90 %"
    assert value_text(52.0, "Cel") == "52°C"
    assert value_text(36 * 3600, "s") == "1d 12h"
    assert value_text(62.7 * 2**30, "By") == "62.7 GiB"
    assert value_text(30, "{entity}") == "30"
