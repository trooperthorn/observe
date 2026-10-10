"""The agent's outbox drop counter as a host warning (observe/agentdrops.py).

truenas-svr reported "outbox over a limit: 492443 data point(s) ... dropped in total ... 1625
request(s) are queued" as an info event: it showed only under Recent events, while the host status,
the Needs-attention box and the Hosts row said nothing. The warning now feeds the host's one
verdict until the count stops growing."""

from __future__ import annotations

from observe.agentdrops import DROP_QUIET_S, agent_drops, drop_total
from observe.checks.base import Result

from . import test_host_views as hv
from .test_host_verdict import Env

NOW = hv.NOW
TITLE = ("outbox over a limit: {n} data point(s) from the oldest requests dropped in total, "
         "{q} request(s) are queued")


def report(n: int, ts: float, q: int = 1625, kind: str = "agent.outbox",
           detail: dict | None = None) -> dict:
    return {"kind": kind, "severity": "info", "source": "agent", "ts": ts,
            "title": TITLE.format(n=n, q=q), "detail": detail or {},
            "dedup_key": f"outbox:{ts}", "boot_id": None}


def test_the_total_is_read_from_a_detail_key_or_the_title():
    assert drop_total(report(492443, NOW)) == 492443
    assert drop_total(report(5, NOW, detail={"dropped_total": 7})) == 7
    assert drop_total({**report(1, NOW), "title": "outbox: 1,234 data points dropped"}) == 1234
    assert drop_total({"kind": "md.degraded", "title": "12 data points", "ts": NOW}) is None
    assert drop_total({"kind": "agent.outbox", "title": "outbox drained", "ts": NOW}) is None


def test_a_growing_count_is_a_warning_and_a_steady_one_is_not():
    first = report(57_902, NOW - 600)
    second = report(492_443, NOW - 300)
    got = agent_drops([second, first], NOW)  # newest first, as the store returns them
    assert got["dropped"] == 492_443 and got["total"] == 492_443
    assert got["since"] == NOW - 600 and got["at"] == NOW - 300
    assert got["text"].startswith("agent dropped 492,443 data points since ")
    # The agent repeats the total: it stopped dropping, and the warning clears.
    assert agent_drops([report(492_443, NOW - 60), second, first], NOW) is None
    # It grows again after standing still: only the new growth counts, from the last steady report.
    again = agent_drops([report(500_000, NOW - 30), report(492_443, NOW - 60), second], NOW)
    assert again["dropped"] == 500_000 - 492_443 and again["since"] == NOW - 60
    # A restarted agent counts from zero again.
    restart = agent_drops([report(100, NOW - 10), second], NOW)
    assert restart["dropped"] == 100 and restart["since"] == NOW - 10


def test_the_warning_clears_when_the_agent_stops_reporting_drops():
    one = [report(10, NOW - 100)]
    assert agent_drops(one, NOW)["dropped"] == 10
    assert agent_drops(one, NOW - 100 + DROP_QUIET_S + 1) is None
    assert agent_drops([], NOW) is None
    assert agent_drops([report(0, NOW)], NOW) is None


def push_reports(env: Env, *reports: dict) -> None:
    env.push(hv.batch(samples=[hv.s("hwmon", "hw.temperature", 41.0, "Cel",
                                    labels={"hw.id": "coretemp:0"})],
                      sources=[{"source": "hwmon", "available": True}], events=list(reports)))


def test_a_dropping_agent_warns_on_every_surface_until_the_count_stops(tmp_path):
    env = Env(tmp_path, [{"name": "TrueNAS", "type": "pushed_host", "host": "nas01"}])
    try:
        push_reports(env, report(57_902, NOW - 900), report(492_443, NOW - 600))
        env.login()
        res = env.poll("truenas")
        row = env.client.get("/api/v2/hosts").json()["items"][0]
        page = hv.detail(env)
        assert row["status"] == page["status"] == "warning"
        assert row["status_reason"] == page["status_reason"] == res.message
        assert page["status_reason"].startswith("alerts: agent dropped 492,443 data points since")
        assert res.result is Result.WARN
        assert page["agent_drops"]["dropped"] == row["agent_drops"]["dropped"] == 492_443
        assert page["agent_drops"]["since"].endswith("Z")  # written as RFC 3339, like "at"
        assert page["alerts"]["items"][0]["kind"] == "agent.outbox_dropping"
        # The next report repeats the total: the count stopped growing and every surface clears.
        push_reports(env, report(492_443, NOW - 60))
        row = env.client.get("/api/v2/hosts").json()["items"][0]
        page = hv.detail(env)
        assert row["status"] == page["status"] == "good"
        assert row["agent_drops"] is None and page["agent_drops"] is None
        assert env.poll("truenas").result is Result.OK
    finally:
        env.close()
