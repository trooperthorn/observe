"""The UniFi plugin: inventory and map feed for UniFi Network.

This slice holds the settings, the tables and the devices collector. Every 120 seconds (setting
`interval`) the collector reads the site list and the device list from the Integration API with
the key of a `unifi` credential named in the settings, and keeps the current snapshot in
`unifi_devices`. Devices unseen for `retention_days` (30) are deleted by the plugin's prune hook.

After a 401 or 403 the collector backs off, doubling its pause up to an hour, and sends no
request until the pause is over, so a revoked key is not hammered. The optional classic
controller credential, when set, builds a read-only classic client (`classic.py`) used by
`classic_snapshot`; the Integration API path works without it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from observe.plugins import Collector, Migration, PluginBase, PluginError
from observe.store import Store

from .classic import (ClassicClient, parse_devices, parse_offline_clients,
                      parse_wan_health)
from .client import AuthRejected, IntegrationClient, UniFiError
from .records import MIGRATIONS, parse_device, prune_unseen, save_devices

__version__ = "0.1.0"


MAX_BACKOFF_S = 3600.0


class UniFiSettings(BaseModel):
    """`plugin_settings.unifi`."""

    model_config = ConfigDict(extra="forbid")
    host: str = Field(default="", max_length=253)
    port: int = Field(default=443, ge=1, le=65535)
    https: bool = True
    verify_tls: bool = True
    ca_bundle: str | None = None
    base_path: str = "/proxy/network/integration/v1"
    credential: str = ""  # name of a `unifi` credential (the Integration API key)
    # Name of a `unifi_classic` credential. Optional; enables the classic read-only client.
    classic_credential: str | None = None
    site: str | None = None  # site name; the only site when omitted
    interval: int = Field(default=120, ge=30, le=3600)
    timeout: float = Field(default=20.0, gt=0, le=120)
    retention_days: int = Field(default=30, ge=1, le=3650)


class BackedOff(Exception):
    """The collector is pausing after a rejected credential and sent no request."""


class UniFiPlugin(PluginBase):
    name = "unifi"
    version = __version__
    core_versions = ">=2026.9,<2027"

    def __init__(self) -> None:
        self.settings = UniFiSettings()
        self.api_key: str | None = None
        self.classic: ClassicClient | None = None
        self._classic_login: tuple[str, str] | None = None
        self.clock: Callable[[], float] = time.monotonic  # a test replaces it
        self.wall: Callable[[], float] = time.time
        self.transport: httpx.AsyncBaseTransport | None = None  # a test replaces it
        self._failures = 0
        self._retry_at = 0.0

    def config_model(self) -> type[BaseModel]:
        return UniFiSettings

    def configure(self, settings: Any) -> None:
        self.settings = settings or UniFiSettings()
        self.api_key = None
        self.classic = None
        self._classic_login = None
        self._failures = 0
        self._retry_at = 0.0

    def bind_credentials(self, credentials: Mapping[str, Any]) -> None:
        s = self.settings
        if not s.host:
            raise PluginError("plugin 'unifi': plugin_settings.unifi.host is required")
        cred = credentials.get(s.credential)
        if cred is None or getattr(cred, "type", None) != "unifi":
            raise PluginError(f"plugin 'unifi': credential {s.credential!r} must name a "
                              "credential of type unifi")
        if s.classic_credential is not None:
            classic = credentials.get(s.classic_credential)
            if classic is None or getattr(classic, "type", None) != "unifi_classic":
                raise PluginError(f"plugin 'unifi': classic_credential "
                                  f"{s.classic_credential!r} must name a credential of type "
                                  "unifi_classic")
        self.api_key = cred.api_key
        if s.classic_credential is not None:
            self._classic_login = (classic.username, classic.password)

    def _classic_client(self) -> ClassicClient | None:
        if self.classic is None and self._classic_login is not None:
            s = self.settings
            scheme = "https" if s.https else "http"
            self.classic = ClassicClient(
                f"{scheme}://{s.host}:{s.port}", *self._classic_login, s.site or "default",
                s.verify_tls, s.ca_bundle, s.timeout, self.transport, self.clock,
                float(s.interval))
        return self.classic

    async def classic_snapshot(self) -> dict[str, Any]:
        """Read the four classic views and parse them. Raises UniFiError, AuthRejected or
        ClassicBackedOff; returns an empty dict when no classic credential is set."""
        c = self._classic_client()
        if c is None:
            return {}
        devices = await c.get("stat/device")
        active = await c.get("stat/sta")
        known = await c.get("rest/user")
        health = await c.get("stat/health")
        return {"devices": parse_devices(devices), "wan": parse_wan_health(health),
                "offline_clients": parse_offline_clients(known, active)}

    async def close(self) -> None:
        """Log out of the classic session. The host has no shutdown hook yet, so a caller
        invokes this; the session is memory only and is lost on exit either way."""
        if self.classic is not None:
            await self.classic.logout()

    def migrations(self) -> list[Migration]:
        return list(MIGRATIONS)

    def collectors(self) -> list[Collector]:
        s = self.settings
        # The timeout covers the site list and every device page, so it may not exceed the interval.
        return [Collector("devices", self.collect_devices, float(s.interval),
                          min(float(s.interval), max(s.timeout * 3, 30.0)))]

    async def prune(self, store: Store, now: float) -> int:
        return await prune_unseen(store, now, self.settings.retention_days)

    def _backoff(self) -> None:
        self._failures += 1
        pause = min(self.settings.interval * 2 ** (self._failures - 1), MAX_BACKOFF_S)
        self._retry_at = self.clock() + pause

    async def collect_devices(self, store: Store) -> int:
        """One poll. Raises BackedOff while pausing, AuthRejected on a 401 or 403, UniFiError
        for an answer it refuses. Returns the number of devices stored."""
        now = self.clock()
        if now < self._retry_at:
            raise BackedOff(f"pausing after a rejected credential, retry in "
                            f"{self._retry_at - now:.0f}s")
        s = self.settings
        if self.api_key is None:  # pragma: no cover - bind_credentials runs at load
            raise UniFiError("no API key bound")
        base = f"{'https' if s.https else 'http'}://{s.host}:{s.port}{s.base_path}"
        api = IntegrationClient(base, self.api_key, s.verify_tls, s.ca_bundle, s.timeout,
                                self.transport)
        try:
            async with api.session() as c:
                sites = await api.list_all(c, "/sites")
                site_id = self._pick_site(sites)
                rows = await api.list_all(c, f"/sites/{site_id}/devices")
        except AuthRejected:
            self._backoff()
            raise
        self._failures = 0
        self._retry_at = 0.0
        devices = [d for d in (parse_device(site_id, r) for r in rows) if d is not None]
        return await save_devices(store, devices, self.wall())

    def _pick_site(self, sites: list[Any]) -> str:
        named = [x for x in sites if isinstance(x, dict) and isinstance(x.get("id"), str)]
        want = self.settings.site
        if want:
            match = [x for x in named if x.get("name") == want]
            if not match:
                raise UniFiError(f"site {want!r} not found; sites: "
                                 f"{[x.get('name') for x in named]}")
            return match[0]["id"]
        if len(named) != 1:
            raise UniFiError(f"{len(named)} sites; set site to one of "
                             f"{[x.get('name') for x in named]}")
        return named[0]["id"]


plugin = UniFiPlugin()
