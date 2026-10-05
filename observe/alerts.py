"""Alert delivery on state transitions.

Delivery is best-effort with one retry. A failed delivery is logged and
recorded on the dashboard (last_error per target); it never blocks polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from typing import Any

import aiomqtt
import httpx

from .checks.mqtt import mqtt_client_kwargs
from .config import Config, MqttAlert, NtfyAlert, SmtpAlert, WebhookAlert
from .state import State, Transition

log = logging.getLogger("watchpost.alerts")

_PRIORITY = {State.DOWN: "high", State.WARN: "default", State.UP: "default"}
_TAGS = {State.DOWN: "red_circle", State.WARN: "warning", State.UP: "white_check_mark"}


def payload(monitor: Any, tr: Transition) -> dict[str, Any]:
    return {
        "monitor": monitor.name,
        "slug": monitor.slug,
        "group": monitor.group,
        "type": monitor.type,
        "previous": tr.previous.value,
        "state": tr.current.value,
        "message": tr.message,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tr.at)),
    }


class Alerter:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.status: dict[str, dict[str, Any]] = {
            a.name: {"type": a.type, "sent": 0, "last_error": None} for a in config.alerts
        }

    async def notify(self, monitor: Any, tr: Transition) -> None:
        if not tr.alertable:
            return
        targets = [a for a in self.config.alerts
                   if (monitor.alerts is None or a.name in monitor.alerts)
                   and tr.current.value in a.notify_on]
        await asyncio.gather(*(self._deliver(a, monitor, tr) for a in targets))

    async def _deliver(self, target: Any, monitor: Any, tr: Transition) -> None:
        body = payload(monitor, tr)
        for attempt in (1, 2):
            try:
                await self._send(target, body, tr)
                self.status[target.name]["sent"] += 1
                self.status[target.name]["last_error"] = None
                return
            except Exception as err:  # noqa: BLE001 - delivery must not crash polling
                msg = f"{type(err).__name__}: {err}"
                self.status[target.name]["last_error"] = msg
                log.warning("alert %s attempt %d failed: %s", target.name, attempt, msg)
                await asyncio.sleep(2)

    async def _send(self, target: Any, body: dict[str, Any], tr: Transition) -> None:
        title = f"[{body['state'].upper()}] {body['monitor']}"
        if isinstance(target, WebhookAlert):
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(target.url, json=body, headers=target.headers)
                r.raise_for_status()
        elif isinstance(target, NtfyAlert):
            headers = {"Title": title, "Priority": _PRIORITY[tr.current],
                       "Tags": _TAGS[tr.current]}
            if target.token:
                headers["Authorization"] = f"Bearer {target.token}"
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(target.url, content=body["message"].encode(), headers=headers)
                r.raise_for_status()
        elif isinstance(target, SmtpAlert):
            await asyncio.to_thread(self._smtp, target, title, body)
        elif isinstance(target, MqttAlert):
            cred = self.config.credentials.get(target.credential) if target.credential else None
            async with aiomqtt.Client(**mqtt_client_kwargs(target, cred, 10)) as c:
                base = f"{target.topic_prefix}/{body['slug']}"
                await c.publish(f"{base}/state", body["state"], qos=1, retain=True)
                await c.publish(f"{base}/event", json.dumps(body), qos=1, retain=False)

    @staticmethod
    def _smtp(target: SmtpAlert, title: str, body: dict[str, Any]) -> None:
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = target.sender
        msg["To"] = ", ".join(target.recipients)
        msg.set_content(json.dumps(body, indent=2))
        with smtplib.SMTP(target.host, target.port, timeout=15) as s:
            if target.starttls:
                s.starttls(context=ssl.create_default_context())
            if target.username:
                s.login(target.username, target.password or "")
            s.send_message(msg)
