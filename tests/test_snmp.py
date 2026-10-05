import asyncio

from observe.checks import build_check
from observe.checks.base import Result

from .conftest import SNMP_PORT, make_config, needs_snmpd

pytestmark = needs_snmpd


def check(**kw):
    mon = {"name": "s", "type": "snmp", "host": "127.0.0.1", "port": SNMP_PORT,
           "credential": "v2", **kw}
    cfg = make_config([mon])
    return build_check(cfg.monitors[0], cfg)


async def test_v2c_uptime():
    res = await check(mode="uptime").run()
    assert res.result is Result.OK and res.value >= 0


async def test_v3_authpriv_string_oid():
    res = await check(credential="v3", mode="oid", oid="1.3.6.1.2.1.1.5.0").run()
    assert res.result is Result.OK and res.value is None and res.detail["raw"]


async def test_numeric_oid_thresholds():
    res = await check(mode="oid", oid=".1.3.6.1.2.1.2.2.1.8.1",
                      thresholds={"direction": "above", "crit": 1}).run()
    assert res.result is Result.FAIL and "critical threshold" in res.message


async def test_missing_oid_fails():
    res = await check(mode="oid", oid=".1.3.6.1.2.1.1.99.0").run()
    assert res.result is Result.FAIL and "not present" in res.message


async def test_wrong_community_times_out_and_redacts():
    res = await check(credential="v2bad", mode="uptime", timeout=1).run()
    assert res.result is Result.FAIL and "wrong-community" not in res.message


async def test_interface_by_name_rate_on_second_poll():
    c = check(mode="interface", interface="lo")
    first = await c.run()
    assert first.result is Result.OK and "in_bps" not in first.detail
    await asyncio.sleep(1.1)
    second = await c.run()
    assert second.result is Result.OK and "in_bps" in second.detail


async def test_unknown_interface():
    res = await check(mode="interface", interface="does-not-exist0").run()
    assert res.result is Result.FAIL and "not found" in res.message


async def test_memory():
    res = await check(mode="memory").run()
    assert res.result is Result.OK and 0 < res.value <= 100


async def test_cpu_ok_or_explicitly_unsupported():
    # Some agents (including net-snmp in some containers) do not populate
    # hrProcessorLoad. The check must say so rather than report 0%.
    res = await check(mode="cpu").run()
    assert res.result is Result.OK or "not exposed" in res.message
