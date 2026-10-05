"""MQTT broker and topic checks.

Without a topic: connect (and authenticate, if a credential is set) and
disconnect. That proves the listener is up and the account works.

With a topic: subscribe and wait up to `timeout` seconds for one message.
A retained message satisfies this immediately, so for a retained topic this
proves "the broker still holds a value", not "a device published recently".
For liveness of a device that publishes retained state, point this at a
topic the device updates on an interval and keep the timeout above that
interval, or use the device's LWT/availability topic with expect: online.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import aiomqtt

from .base import Check, CheckResult


def mqtt_client_kwargs(host_cfg: Any, cred: Any, timeout: float) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"hostname": host_cfg.host, "port": host_cfg.port,
                              "timeout": timeout}
    if cred is not None:
        kwargs["username"] = cred.username
        kwargs["password"] = cred.password
    if host_cfg.tls:
        kwargs["tls_context"] = ssl.create_default_context(cafile=host_cfg.ca_bundle)
    return kwargs


class MqttCheck(Check):
    async def probe(self) -> CheckResult:
        m = self.monitor
        kwargs = mqtt_client_kwargs(m, self.credential(), self.timeout)
        try:
            async with asyncio.timeout(self.timeout + 1):
                async with aiomqtt.Client(**kwargs) as client:
                    if not m.topic:
                        return CheckResult.ok(f"connected to {m.host}:{m.port}")
                    await client.subscribe(m.topic)
                    async for msg in client.messages:
                        return self._evaluate(msg)
        except TimeoutError:
            what = f"no message on {m.topic}" if m.topic else "connect timed out"
            return CheckResult.fail(f"{what} within {self.timeout:g}s")
        except aiomqtt.MqttError as err:
            return CheckResult.fail(f"MQTT: {err}")
        return CheckResult.fail("subscription ended without a message")

    def _evaluate(self, msg: aiomqtt.Message) -> CheckResult:
        m = self.monitor
        payload = msg.payload
        text = payload.decode(errors="replace") if isinstance(payload, (bytes, bytearray)) \
            else str(payload)
        tag = " (retained)" if msg.retain else ""
        if m.expect is not None and m.expect not in text:
            return CheckResult.fail(f"{msg.topic}{tag} = {text[:60]!r}, expected {m.expect!r}")
        value = None
        if m.numeric:
            try:
                value = float(text)
            except ValueError:
                return CheckResult.fail(f"{msg.topic}{tag} = {text[:60]!r} is not numeric")
        return CheckResult.ok(f"{msg.topic}{tag} = {text[:60]}", value=value,
                              detail={"retained": msg.retain})
