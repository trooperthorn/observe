"""Alert delivery on state transitions.

With a store bound (the Scheduler binds its own), every alert is first written to the
`alert_outbox` table, one row per target, and deleted only after the target accepted it. A failed
delivery is logged, recorded on the dashboard (last_error per target) and retried with a growing
delay, so an outage of the target or a restart of Observe loses nothing. Delivery is at least
once: a crash between the target's answer and the delete sends that one alert again. An alert
older than ALERT_MAX_AGE_S is dropped, because a page about a day-old state is noise. Without a
store (a few tests) an alert is tried twice and then dropped. The one-shot check command binds an
in-memory store, so its alerts get one attempt and are lost when the process exits.
Alerts of one target are delivered in the order they were queued, so a recovery never overtakes
the problem it ends. Nothing here blocks polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
import time
from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

import aiomqtt

from .checks.mqtt import mqtt_client_kwargs
from .config import Config, MqttAlert, NtfyAlert, SmtpAlert, WebhookAlert
from .httpclient import http_client
from .state import State, Transition

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("observe.alerts")

# The delay after the first failure, doubling to the cap, and how often the outbox is looked at.
RETRY_FIRST_S = 10.0
RETRY_CAP_S = 300.0
OUTBOX_POLL_S = 5.0
ALERT_MAX_AGE_S = 86400.0

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
        "degraded": tr.degraded,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tr.at)),
    }


def retry_delay(attempts: int) -> float:
    """Seconds to wait after the `attempts`-th failed delivery of one alert."""
    return min(RETRY_CAP_S, RETRY_FIRST_S * 2 ** max(0, attempts - 1))


class Alerter:
    def __init__(self, config: Config, store: Store | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.store = store
        self.clock = clock
        self._flushing = asyncio.Lock()
        self.status: dict[str, dict[str, Any]] = {
            a.name: {"type": a.type, "sent": 0, "last_error": None} for a in config.alerts
        }

    def _targets(self, monitor: Any, tr: Transition) -> list[Any]:
        if not tr.alertable:
            return []
        # A degraded notice (the start or the end of a fast re-check) goes only to a target that
        # opted in with notify_degraded, whatever its notify_on says.
        return [a for a in self.config.alerts
                if (monitor.alerts is None or a.name in monitor.alerts)
                and (a.notify_degraded if tr.degraded else tr.current.value in a.notify_on)]

    async def enqueue(self, monitor: Any, tr: Transition) -> Callable[[], Awaitable[Any]]:
        """Write the alert to the outbox and return the coroutine function that delivers it.
        The caller can therefore record that the alert is open only once it is queued, so a
        crash in between repeats an alert instead of losing it. Without a store, or when the
        outbox can not take the alert, the returned function delivers it directly."""
        targets = self._targets(monitor, tr)

        async def nothing() -> None:
            return None

        async def direct() -> None:
            await asyncio.gather(*(self._deliver(a, monitor, tr) for a in targets))

        if not targets:
            return nothing
        if self.store is None:
            return direct
        body = json.dumps(payload(monitor, tr), separators=(",", ":"))
        try:
            await self.store.outbox_add([(a.name, body) for a in targets], self.clock())
        except Exception:  # noqa: BLE001 - an alert is never dropped because the queue failed
            # The outbox could not take it, so try the targets directly (twice, as without a
            # store) rather than lose the alert.
            log.exception("could not queue the alert for %s, delivering it directly", monitor.name)
            return direct
        return self.flush

    async def notify(self, monitor: Any, tr: Transition) -> None:
        await (await self.enqueue(monitor, tr))()

    def bind(self, store: Store | None) -> None:
        """Use `store` for the outbox, unless one is already bound."""
        if self.store is None and store is not None:
            self.store = store

    async def flush(self) -> int:
        """Try every queued alert that is due once. A delivered alert is deleted; a failed one
        is kept with its next attempt later. Returns the number delivered."""
        if self.store is None:
            return 0
        async with self._flushing:
            now = self.clock()
            rows = await self.store.outbox_due(now)
            known = {a.name: a for a in self.config.alerts}
            # Targets are independent, but the alerts of one target go out in the order they were
            # queued, and a failure holds back the newer ones: a recovery must never reach a
            # target before the problem it ends.
            queues: dict[str, list[tuple[Any, ...]]] = {}
            for row in rows:
                queues.setdefault(row[1], []).append(row)
            results = await asyncio.gather(*(self._drain(known.get(name), name, queue, now)
                                             for name, queue in queues.items()))
            return sum(results)

    async def _drain(self, target: Any, name: str, queue: list[tuple[Any, ...]],
                     now: float) -> int:
        sent = 0
        for row in queue:
            outcome = await self._attempt(target, row, now)
            if outcome is None:
                break
            sent += outcome
        return sent

    async def _attempt(self, target: Any, row: tuple[Any, ...], now: float) -> int | None:
        """Deliver one queued alert. Returns 1 when it was delivered, 0 when it was dropped and
        None when it failed and was kept for a later try."""
        assert self.store is not None
        alert_id, name, text, created, attempts = row
        if target is None or now - created > ALERT_MAX_AGE_S:
            why = "its target is no longer configured" if target is None else "it is too old"
            log.warning("alert %s for %s dropped: %s", alert_id, name, why)
            await self.store.outbox_done([alert_id])
            return 0
        try:
            await self._send(target, json.loads(text))
        except Exception as err:  # noqa: BLE001 - delivery must not crash polling
            msg = f"{type(err).__name__}: {err}"
            self.status[name]["last_error"] = msg
            delay = retry_delay(attempts + 1)
            log.warning("alert %s to %s failed (attempt %d), next try in %.0fs: %s",
                        alert_id, name, attempts + 1, delay, msg)
            await self.store.outbox_retry(alert_id, attempts + 1, now + delay, msg)
            return None
        self.status[name]["sent"] += 1
        self.status[name]["last_error"] = None
        await self.store.outbox_done([alert_id])
        return 1

    async def run(self) -> None:
        """Deliver what is queued, including what a previous run left behind, and keep looking
        for due retries until cancelled."""
        while True:
            try:
                await self.flush()
            except Exception:  # noqa: BLE001 - keep delivering after a storage hiccup
                log.exception("flushing the alert outbox failed")
            await asyncio.sleep(OUTBOX_POLL_S)

    async def _deliver(self, target: Any, monitor: Any, tr: Transition) -> None:
        body = payload(monitor, tr)
        for attempt in (1, 2):
            try:
                await self._send(target, body)
                self.status[target.name]["sent"] += 1
                self.status[target.name]["last_error"] = None
                return
            except Exception as err:  # noqa: BLE001 - delivery must not crash polling
                msg = f"{type(err).__name__}: {err}"
                self.status[target.name]["last_error"] = msg
                log.warning("alert %s attempt %d failed: %s", target.name, attempt, msg)
                await asyncio.sleep(2)

    async def _send(self, target: Any, body: dict[str, Any]) -> None:
        state = State(body["state"])
        title = f"[{body['state'].upper()}] {body['monitor']}"
        if isinstance(target, WebhookAlert):
            async with http_client(True, 10) as c:
                r = await c.post(target.url, json=body, headers=target.headers)
                r.raise_for_status()
        elif isinstance(target, NtfyAlert):
            headers = {"Title": title, "Priority": _PRIORITY[state],
                       "Tags": _TAGS[state]}
            if target.token:
                headers["Authorization"] = f"Bearer {target.token}"
            async with http_client(True, 10) as c:
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
