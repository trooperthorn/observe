import base64
import json

import aiomqtt
from fastapi.testclient import TestClient

from observe.alerts import Alerter
from observe.checks.base import CheckResult, Result
from observe.scheduler import Scheduler
from observe.state import State, Transition
from observe.store import Store
from observe.web import create_app

from .conftest import MQTT_PORT, make_config, needs_mqtt


def app_for(cfg):
    store = Store(":memory:")
    alerter = Alerter(cfg)
    sched = Scheduler(cfg, store, alerter)
    return create_app(cfg, store, sched, alerter), sched


def test_basic_auth_and_headers():
    cfg = make_config([{"name": "p", "type": "ping", "host": "127.0.0.1"}],
                      server={"basic_auth_user": "ops", "basic_auth_password": "s3cret",
                              "db_path": ":memory:"})
    app, _ = app_for(cfg)
    c = TestClient(app)
    assert c.get("/metrics").status_code == 401
    assert c.get("/healthz").status_code == 200
    tok = base64.b64encode(b"ops:s3cret").decode()
    r = c.get("/metrics", headers={"Authorization": f"Basic {tok}"})
    assert r.status_code == 200 and "observe_state" in r.text
    # The v2 read API does not take basic auth: it needs a session or a read token.
    assert c.get("/api/v2/monitors", headers={"Authorization": f"Basic {tok}"}).status_code == 401
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    bad = base64.b64encode(b"ops:wrong").decode()
    assert c.get("/metrics", headers={"Authorization": f"Basic {bad}"}).status_code == 401


async def test_poll_records_history_and_metrics():
    cfg = make_config([{"name": "Loop Back", "type": "tcp", "host": "127.0.0.1", "port": 1,
                        "group": 'lab"core', "recheck_window": 0}],
                      server={"anonymous_read": True, "db_path": ":memory:"})
    app, sched = app_for(cfg)
    res = await sched.poll_once(sched.monitors[0])
    assert res.result is Result.FAIL
    assert sched.states["loop-back"].state is State.DOWN
    c = TestClient(app)
    one = c.get("/api/v2/monitors/loop-back").json()
    assert one["availability_24h"] == 0
    events = c.get("/api/v2/events?resource=loop-back").json()["items"]
    assert events[0]["attributes"]["observe.monitor.state"] == "down"
    metrics = c.get("/metrics").text
    assert 'observe_state{monitor="loop-back",group="lab\\"core",type="tcp"} 2' in metrics
    assert c.get("/api/v2/monitors/nope").status_code == 404


@needs_mqtt
async def test_mqtt_alert_publishes_retained_state():
    cfg = make_config(
        [{"name": "NAS", "type": "ping", "host": "127.0.0.1"}],
        alerts=[{"name": "ha", "type": "mqtt", "host": "127.0.0.1", "port": MQTT_PORT,
                 "topic_prefix": "wp-test-alert", "notify_on": ["down", "up"]}],
    )
    alerter = Alerter(cfg)
    mon = cfg.monitors[0]
    await alerter.notify(mon, Transition(State.PENDING, State.UP, 0, "ok"))  # suppressed
    assert alerter.status["ha"]["sent"] == 0
    await alerter.notify(mon, Transition(State.UP, State.DOWN, 0, "no reply"))
    assert alerter.status["ha"]["sent"] == 1, alerter.status
    async with aiomqtt.Client("127.0.0.1", MQTT_PORT) as c:
        await c.subscribe("wp-test-alert/nas/state")
        async for msg in c.messages:
            assert msg.payload == b"down" and msg.retain
            break


async def test_failed_delivery_is_reported_not_raised():
    cfg = make_config(
        [{"name": "x", "type": "ping", "host": "127.0.0.1"}],
        alerts=[{"name": "hook", "type": "webhook", "url": "http://127.0.0.1:1/"}],
    )
    alerter = Alerter(cfg)
    await alerter.notify(cfg.monitors[0], Transition(State.UP, State.DOWN, 0, "x"))
    assert alerter.status["hook"]["last_error"]


async def test_rollup_and_forecast_exposed_in_api_and_metrics():
    import time as _t

    cfg = make_config([
        {"name": "sw", "type": "tcp", "host": "127.0.0.1", "port": 1, "group": "net",
         "recheck_window": 0},
        {"name": "srv", "type": "tcp", "host": "127.0.0.1", "port": 1, "group": "net",
         "depends_on": ["sw"], "recheck_window": 0},
        {"name": "disk", "type": "tcp", "host": "127.0.0.1", "port": 1, "forecast": True,
         "thresholds": {"direction": "above", "warn": 80, "crit": 90}},
    ], server={"anonymous_read": True, "db_path": ":memory:"})
    app, sched = app_for(cfg)
    await sched.poll_once(sched.by_slug["sw"])
    await sched.poll_once(sched.by_slug["srv"])
    now = _t.time()
    for h in range(72):  # three days rising 2/day toward 80
        await sched.store.record("disk", now - (72 - h) * 3600,
                                 CheckResult(Result.OK, "", value=60 + 2 * h / 24))
    c = TestClient(app)
    data = c.get("/api/v2/monitors").json()
    srv = next(m for m in data["items"] if m["slug"] == "srv")
    assert srv["effective_state"] == "unreachable" and srv["blocked_by"] == "sw"
    groups = {g["name"]: g for g in c.get("/api/v2/groups").json()["items"]}
    assert groups["net"]["state"] == "down" and groups["net"]["worst"] == ["sw"]
    await sched.refresh_forecasts()
    fc = next(m for m in c.get("/api/v2/monitors").json()["items"] if m["slug"] == "disk")["forecast"]
    # Hourly bucket midpoints shift the fit by up to half an hour, depending on
    # the minute the test runs, so allow for that around the true 7.0 days.
    assert fc["status"] == "projected" and 6.9 < (fc["warn_at"] - now) / 86400 < 7.6
    m = c.get("/metrics").text
    assert 'observe_effective_state{monitor="srv",group="net",type="tcp"} 3' in m
    assert 'observe_forecast_seconds{monitor="disk",group="default",type="tcp",level="warn"}' in m
    assert 'observe_group_state{group="net"} 2' in m
