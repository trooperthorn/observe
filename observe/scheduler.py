"""Polling loop: one asyncio task per monitor, bounded by a global semaphore.

Alert decisions live here because they need the whole picture:

* A DOWN/WARN transition on a monitor with parents first confirms those
  parents. If a parent is failing but not yet confirmed DOWN (it needs
  `failures_to_down` polls), it is polled again immediately until it is
  either confirmed DOWN or recovers. This is the on-demand parent check that
  lets a switch outage suppress the alerts for everything behind it, instead
  of racing it.
* If an ancestor is DOWN, the child's alert is suppressed and the event is
  recorded with the name of the blocking ancestor.
* A failed check that means "nothing answered" starts the fast re-check (state.py): the monitor
  is Warning, "Degraded: not responding", and is polled every `recheck_interval` seconds. While a
  parent is Down the child starts no re-check of its own. While a parent is in its own re-check
  window the child's alert is held, and sent when the parent recovers if the child still fails.
* An UP alert is sent only if the matching problem alert was sent.
* When a parent recovers, any descendant that is still DOWN or WARN on its
  own is alerted then, because it is now a real, separate problem.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .alerts import Alerter
from .checks import build_check
from .checks.base import CheckResult, Result
from .config import Config
from . import recheck_settings
from .forecast import Forecast, project
from .rollup import Rollup
from .state import MonitorState, State, Transition
from .store import Store

log = logging.getLogger("observe.scheduler")

_PROBLEM = (State.DOWN, State.WARN)


class Scheduler:
    def __init__(self, config: Config, store: Store, alerter: Alerter,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.clock = clock
        self.store = store
        self.alerter = alerter
        self.monitors = [m for m in config.monitors if m.enabled]
        self.by_slug = {m.slug: m for m in self.monitors}
        self.checks = {m.slug: build_check(m, config, store) for m in self.monitors}
        self.states = {
            m.slug: MonitorState(config.effective(m, "failures_to_down"),
                                 config.effective(m, "recoveries_to_up"),
                                 recheck_window=config.effective(m, "recheck_window"),
                                 recheck_good=config.effective(m, "recheck_good"),
                                 since=clock())
            for m in self.monitors
        }
        self.rollup = Rollup(config, self.states)
        # The admin's saved re-check values (observe/recheck_settings.py): global and per monitor.
        self._recheck_global: dict[str, Any] = {}
        self._recheck_overrides: dict[str, dict[str, Any]] = {}
        self._recheck_loaded = False
        self.forecasts: dict[str, Forecast] = {}
        self._locks = {m.slug: asyncio.Lock() for m in self.monitors}
        self._sem = asyncio.Semaphore(config.server.max_concurrency)
        self._tasks: list[asyncio.Task[None]] = []
        self._pending_alerts: set[asyncio.Task[None]] = set()
        # Coroutine functions run once a minute, for example the infrastructure map's
        # dependency refresh, so that ageing and new links reach the rollup without a request.
        self.hooks: list[Callable[[], Awaitable[Any]]] = []
        self._collectors: list[tuple[str, Any]] = []

    # ---------------------------------------------------------------- polling

    def delay(self, monitor: Any) -> float:
        """Seconds until the monitor is polled again: the re-check interval while it is
        Degraded, the polling interval otherwise."""
        if self.states[monitor.slug].degraded:
            return float(self.recheck_value(monitor, "interval"))
        return float(self.config.effective(monitor, "interval"))

    def recheck_value(self, monitor: Any, name: str) -> float | int:
        """The re-check window, interval or good-reply count in force for a monitor: the saved
        per-monitor override beats the config entry of the monitor, which beats the saved global
        value, which beats the config default."""
        return recheck_settings.resolve(self.config, monitor, name, self._recheck_global,
                                        self._recheck_overrides)

    def apply_recheck(self, glob: dict[str, Any], overrides: dict[str, dict[str, Any]]) -> None:
        """Use new saved values now: a monitor already in its window keeps the window it started
        with only until its next result, which reads the new values."""
        self._recheck_global, self._recheck_overrides = dict(glob), dict(overrides)
        self._recheck_loaded = True
        for m in self.monitors:
            st = self.states[m.slug]
            st.recheck_window = float(self.recheck_value(m, "window"))
            st.recheck_good = int(self.recheck_value(m, "good"))

    async def load_recheck(self) -> None:
        """Read the saved values from the storage, once before the first poll."""
        glob, overrides = await self.store.storage.read(recheck_settings.load)
        self.apply_recheck(glob, overrides)

    async def restore(self, monitor: Any) -> None:
        """Take the monitor's state from its newest stored result (the latest table), so a
        restart does not show everything as pending. A result older than three intervals is not
        trusted. Only a good result is restored. A WARN or FAIL result leaves the monitor pending,
        so a problem that continues across a restart is evaluated again and alerts as usual."""
        found = await self.store.last_result(monitor.slug)
        st = self.states[monitor.slug]
        if found is None or st.state is not State.PENDING:
            return
        at, result = found
        if result is not Result.OK:
            return
        if self.clock() - at > 3 * self.config.effective(monitor, "interval"):
            return
        st.state = State.UP
        st.since = at

    async def _probe(self, monitor: Any) -> CheckResult:
        async with self._sem:
            check = self.checks[monitor.slug]
            check.rechecking = self.states[monitor.slug].degraded
            try:
                return await check.run()
            except Exception as err:  # noqa: BLE001 - a buggy check must not stop the loop
                log.exception("check %s raised", monitor.name)
                return CheckResult.fail(f"internal error: {type(err).__name__}: {err}")

    async def poll_once(self, monitor: Any) -> CheckResult:
        async with self._locks[monitor.slug]:
            res = await self._probe(monitor)
            now = self.clock()
            await self.store.record(monitor.slug, now, res)
            tr = self.states[monitor.slug].observe(
                res, now, allow_recheck=self.rollup.blocking_parent(monitor.slug) is None)
        if tr is not None:
            await self._on_transition(monitor, tr)
        return res

    # ----------------------------------------------------------------- alerts

    def _ancestors(self, slug: str) -> set[str]:
        seen: set[str] = set()
        stack = [p.slug for p in self.config.parents(self.by_slug[slug])]
        while stack:
            s = stack.pop()
            if s in seen or s not in self.by_slug:
                continue
            seen.add(s)
            stack.extend(p.slug for p in self.config.parents(self.by_slug[s]))
        return seen

    async def _confirm_parents(self, monitor: Any) -> None:
        for parent in self.config.parents(monitor):
            if parent.slug not in self.by_slug:
                continue
            st = self.states[parent.slug]
            attempts = 0
            # Poll while the parent is unconfirmed: failing but not yet DOWN,
            # or never polled at all (PENDING with no history, e.g. at startup).
            while (st.state is not State.DOWN and not st.degraded
                   and (st.bad > 0 or (st.state is State.PENDING and st.good == 0))
                   and attempts < st.failures_to_down):
                attempts += 1
                log.info("%s failing: confirming parent %s (%d)", monitor.name, parent.name,
                         attempts)
                await self.poll_once(parent)

    def _send(self, monitor: Any, tr: Transition) -> None:
        task = asyncio.create_task(self.alerter.notify(monitor, tr))
        self._pending_alerts.add(task)  # hold a reference until delivery finishes
        task.add_done_callback(self._pending_alerts.discard)

    async def _on_transition(self, monitor: Any, tr: Transition) -> None:
        st = self.states[monitor.slug]
        if tr.degraded and tr.current is State.WARN:
            self._send(monitor, tr)  # only targets that opted in to degraded notices get it
        elif tr.current in _PROBLEM:
            blocker = self.rollup.blocking_parent(monitor.slug)
            if blocker is None and monitor.depends_on:
                await self._confirm_parents(monitor)
                blocker = self.rollup.blocking_parent(monitor.slug)
            holding = None if blocker else self.rollup.degraded_parent(monitor.slug)
            if blocker:
                tr.message += f" [alert suppressed: {blocker} is down]"
            elif holding:
                tr.message += f" [alert held: {holding} is being re-checked]"
            else:
                st.alert_open = True
                self._send(monitor, tr)
        elif tr.current is State.UP:
            if st.alert_open:
                st.alert_open = False
                tr.degraded = False
                self._send(monitor, tr)
            elif tr.degraded:
                self._send(monitor, tr)

        log.info("%s: %s -> %s (%s)", monitor.name, tr.previous.value, tr.current.value,
                 tr.message)
        await self.store.record_event(monitor.slug, tr)
        if tr.current is State.UP:
            await self._release_children(monitor)

    async def _release_children(self, monitor: Any) -> None:
        """Parent recovered: re-poll its dependents now, and alert any that
        still fail on a fresh poll, because that is a separate problem.

        The fresh poll matters: without it, a server that was only unreachable
        would be reported "still down" in the moment between the switch
        recovering and the server's own next poll.
        """
        for child in self.monitors:
            if monitor.slug not in self._ancestors(child.slug):
                continue
            cst = self.states[child.slug]
            if cst.state not in _PROBLEM or cst.alert_open or cst.degraded:
                continue  # a child in its own re-check window is decided by that window
            res = await self.poll_once(child)
            if res.result is Result.OK or cst.state not in _PROBLEM or cst.alert_open:
                continue  # recovering, recovered, or already alerted by that poll
            if self.rollup.blocking_parent(child.slug) is not None:
                continue  # still behind another failed parent
            cst.alert_open = True
            tr = Transition(cst.state, cst.state, time.time(),
                            f"still {cst.state.value} after {monitor.name} recovered: "
                            f"{res.message}")
            self._send(child, tr)
            await self.store.record_event(child.slug, tr)

    # --------------------------------------------------------------- forecast

    async def refresh_forecasts(self) -> None:
        fc = self.config.forecast
        for m in self.monitors:
            if not m.forecast:
                continue
            th = self.checks[m.slug].thresholds()
            if th is None:
                continue
            series = await self.store.hourly_series(m.slug, fc.lookback_days)
            self.forecasts[m.slug] = project(series, th, fc)

    # ------------------------------------------------------------------ loops

    async def _loop(self, monitor: Any) -> None:
        interval = self.config.effective(monitor, "interval")
        if not self._recheck_loaded:
            await self.load_recheck()
        await self.restore(monitor)
        await asyncio.sleep(random.uniform(0, min(interval, 10)))  # spread the first wave
        while True:
            started = asyncio.get_running_loop().time()
            await self.poll_once(monitor)
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(1.0, self.delay(monitor) - elapsed))

    async def _hook_loop(self) -> None:
        while True:
            for hook in list(self.hooks):
                try:
                    await hook()
                except Exception:  # noqa: BLE001
                    log.exception("scheduler hook failed")
            await asyncio.sleep(60)

    async def _wait(self, seconds: float) -> None:
        """The pause between collector runs. A test replaces it to avoid real waiting."""
        await asyncio.sleep(seconds)

    async def _collector_loop(self, plugin: str, collector: Any) -> None:
        """Run one plugin collector forever. A failure is logged once per streak."""
        label = f"{plugin}.{collector.name}"
        failing = False
        while True:
            try:
                await asyncio.wait_for(collector.run(self.store), collector.timeout)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                if not failing:
                    log.error("collector %s timed out after %gs", label, collector.timeout)
                failing = True
            except Exception:  # noqa: BLE001
                if not failing:
                    log.exception("collector %s failed", label)
                failing = True
            else:
                if failing:
                    log.info("collector %s recovered", label)
                failing = False
            await self._wait(collector.interval)

    def add_collectors(self, plugins: Any) -> None:
        """Register the collectors of the loaded plugins; start() runs them."""
        for loaded in plugins.plugins:
            for c in loaded.collectors:
                self._collectors.append((loaded.name, c))

    async def _maintenance(self) -> None:
        await asyncio.sleep(30)  # let the first poll wave land before forecasting
        passes = 0
        while True:
            try:
                # The summary levels are kept current at ingest; forecasts and compaction run hourly.
                if passes % 12 == 0:
                    await self.refresh_forecasts()
                    removed = await self.store.prune(
                        self.config.server.retention_days, self.config.server.audit_retention_days)
                    if removed:
                        log.info("pruned %d result rows", removed)
                    await self.store.note_maintenance(removed)
            except Exception as err:  # noqa: BLE001
                log.exception("maintenance failed")
                try:
                    await self.store.note_maintenance(0, f"{type(err).__name__}: {err}")
                except Exception:  # noqa: BLE001
                    log.exception("could not record the maintenance error")
            passes += 1
            await asyncio.sleep(300)

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._loop(m), name=m.slug) for m in self.monitors]
        self._tasks.append(asyncio.create_task(self._maintenance(), name="maintenance"))
        self._tasks.append(asyncio.create_task(self._hook_loop(), name="hooks"))
        for plugin, c in self._collectors:
            self._tasks.append(asyncio.create_task(self._collector_loop(plugin, c),
                                                   name=f"collector:{plugin}.{c.name}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
