import aiomqtt

from observe.checks import build_check
from observe.checks.base import Result

from .conftest import MQTT_PORT, make_config, needs_mqtt

pytestmark = needs_mqtt


def check(**kw):
    mon = {"name": "m", "type": "mqtt", "host": "127.0.0.1", "port": MQTT_PORT, **kw}
    cfg = make_config([mon])
    return build_check(cfg.monitors[0], cfg)


async def publish(topic, payload):
    async with aiomqtt.Client("127.0.0.1", MQTT_PORT) as c:
        await c.publish(topic, payload, retain=True)


async def test_broker_connect_only():
    assert (await check().run()).result is Result.OK


async def test_retained_numeric_with_threshold():
    await publish("wp-test/temp", "31.5")
    res = await check(topic="wp-test/temp", numeric=True,
                      thresholds={"direction": "above", "warn": 30}).run()
    assert res.result is Result.WARN and res.value == 31.5 and res.detail["retained"]


async def test_expect_mismatch():
    await publish("wp-test/avail", "offline")
    res = await check(topic="wp-test/avail", expect="online").run()
    assert res.result is Result.FAIL and "expected 'online'" in res.message


async def test_silent_topic_times_out():
    res = await check(topic="wp-test/never-published", timeout=1).run()
    assert res.result is Result.FAIL and "no message" in res.message


async def test_broker_down():
    cfg = make_config([{"name": "m", "type": "mqtt", "host": "127.0.0.1", "port": 1,
                        "timeout": 1}])
    res = await build_check(cfg.monitors[0], cfg).run()
    assert res.result is Result.FAIL
