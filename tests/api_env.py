"""A running Observe app for the /api/v2 tests: a file database (so the read pool is real), a
scheduler with a few monitors, a clock the test controls, and helpers for sessions and tokens."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi.testclient import TestClient

from observe import auth
from observe.alerts import Alerter
from observe.checks.base import CheckResult, Result
from observe.ingest.boot import classify_events
from observe.ingest.keys import create_key
from observe.ingest.schema import Batch
from observe.scheduler import Scheduler
from observe.store import Store
from observe.web import create_app

from .conftest import make_config

PASSWORD = "correct horse battery"
START = 1_700_000_000.0

MONITORS = [{"name": "core", "type": "ping", "host": "10.0.0.1", "group": "net"},
            {"name": "edge", "type": "ping", "host": "10.0.0.2", "group": "net",
             "depends_on": ["core"]},
            {"name": "nas", "type": "tcp", "host": "10.0.0.3", "port": 445, "group": "storage"}]


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ApiEnv:
    def __init__(self, tmp_path: Any, monitors: list[dict[str, Any]] | None = None,
                 plugins: Any = None, **server: Any) -> None:
        self.path = str(tmp_path / "api.db")
        self.store = Store(self.path, plugins)
        srv = {"db_path": self.path, "argon2_time_cost": 1, "argon2_memory_kib": 8,
               "argon2_parallelism": 1, "session_idle_s": 100_000, "session_absolute_s": 200_000,
               "api_rate_per_second": 1000, "api_burst": 1000, **server}
        self.cfg = make_config(MONITORS if monitors is None else monitors, server=srv)
        self.wall = Clock(START)
        self.mono = Clock(1000.0)
        self.alerter = Alerter(self.cfg)
        self.sched = Scheduler(self.cfg, self.store, self.alerter, clock=self.wall)
        self.app = create_app(self.cfg, self.store, self.sched, self.alerter,
                              ingest_clock=self.mono, auth_clock=self.wall, plugins=plugins)
        self.client = TestClient(self.app, base_url="https://testserver")
        self.csrf: dict[str, str] = {}

    # ---- credentials ---------------------------------------------------------------------

    def login(self, name: str = "admin", admin: bool = True) -> dict[str, str]:
        """Sign in (the cookie stays on the client) and return the CSRF header."""
        asyncio.run(auth.create_user(self.store, self.cfg, name, PASSWORD, admin,
                                     now=self.wall.now))
        r = self.client.post("/api/login", json={"username": name, "password": PASSWORD})
        assert r.status_code == 200, r.text
        self.csrf = {"X-CSRF-Token": r.json()["csrf"]}
        return self.csrf

    def token(self, role: str = "viewer", label: str = "script") -> dict[str, str]:
        plain, _ = asyncio.run(create_key(self.store, label, "test", scope="wpr", role=role))
        return {"Authorization": f"Bearer {plain}"}

    # ---- data ----------------------------------------------------------------------------

    def poll(self, slug: str, result: Result = Result.OK, value: float | None = None,
             ts: float | None = None, latency: float | None = 5.0) -> None:
        asyncio.run(self.store.record(slug, self.wall.now if ts is None else ts,
                                      CheckResult(result, "x", value=value, latency_ms=latency)))

    def poll_many(self, slug: str, points: list[tuple[float, float]]) -> None:
        """Record many (timestamp, latency) results in one event loop. A loop per result opens a
        socket pair each time, and thousands of them exhaust loopback ports on Windows and hang."""
        async def go() -> None:
            for ts, latency in points:
                await self.store.record(slug, ts,
                                        CheckResult(Result.OK, "x", value=None, latency_ms=latency))
        asyncio.run(go())

    def push(self, batch: Batch, now: float | None = None) -> None:
        asyncio.run(self.store.ingest_batch(batch, classify_events(batch.events),
                                            now=self.wall.now if now is None else now))

    def get(self, path: str, **kw: Any) -> Any:
        return self.client.get("/api/v2" + path, **kw)

    def close(self) -> None:
        self.client.close()
        self.store.close()


def host_batch(host: str = "nas01", ts: float = START - 5, events: list | None = None,
               samples: list | None = None) -> Batch:
    return Batch.model_validate({
        "schema_version": 1, "agent_version": "0.9.0", "host": host, "platform": "linux",
        "sent_at": ts,
        "sources": [{"source": "cpu", "available": True}],
        "samples": samples if samples is not None else [
            {"source": "cpu", "metric": "utilization_pct", "value": 12.0, "unit": "%",
             "labels": {}, "ts": ts}],
        "events": events or []})
