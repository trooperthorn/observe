"""The UniFi plugin: inventory and map feed for UniFi Network.

This slice holds the settings, the tables and the devices collector. Every 120 seconds (setting
`interval`) the collector reads the site list and the device list from the Integration API with
the key of a `unifi` credential named in the settings, and keeps the current snapshot in
`unifi_devices`. Devices unseen for `retention_days` (30) are deleted by the plugin's prune hook.

After a 401 or 403 the collector backs off, doubling its pause up to an hour, and sends no
request until the pause is over, so a revoked key is not hammered. The optional classic
controller credential, when set, builds a read-only classic client (`classic.py`) used by
`classic_snapshot`; the Integration API path works without it.

The map feed (`feed.py`): the devices collector also writes each device as a switch, with its
uplink device as a link, and the optional `classic` collector adds ports, port properties and
port-level links. See docs/FIELD-DATA.md.

Clients and Protect. Every `clients_interval` seconds (300) the `clients` collector reads the
Integration API client list, enriches it from the classic views when the classic credential is
set, and keeps one row per MAC. Every `protect_interval` seconds (120), when `protect` is true,
the `protect` collector reads the Protect camera list with the same key. A failure of the classic
views never stops the Integration rows; the pages say the enrichment is unavailable. The pages
(pages.py) are the UniFi page under Network.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from observe.plugins import (Collector, Migration, NavEntry, PluginBase, PluginError,
                             PluginPage, PluginRouter)
from observe.store import Store

from .classic import (ClassicClient, ClassicForbidden, parse_devices, parse_offline_clients,
                      parse_wan_health)
from .clients import (device_index, enrich, offline_clients, parse_active_clients,
                      parse_camera, parse_client, save_cameras, save_clients)
from .feed import feed_classic, feed_integration
from .client import AuthRejected, IntegrationClient, UniFiError
from .pages import PAGE_PATH, build_pages_router, page_files
from .records import MIGRATIONS, parse_device, prune_unseen

__version__ = "0.1.0"


MAX_BACKOFF_S = 3600.0
log = logging.getLogger(__name__)


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
    clients_interval: int = Field(default=300, ge=30, le=3600)
    # Protect cameras, off by default because Protect may not be installed on the console.
    protect: bool = False
    protect_base_path: str = "/proxy/protect/integration/v1"
    protect_interval: int = Field(default=120, ge=30, le=3600)
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
        self._protect_failures = 0
        self._protect_retry_at = 0.0
        # Why the last clients poll had no classic detail, or None. Shown on the pages.
        self.classic_note: str | None = None
        self._forbidden_reported = False
        # Wall time of the last devices poll that succeeded, for the devices page.
        self.devices_ok_at: float | None = None

    def config_model(self) -> type[BaseModel]:
        return UniFiSettings

    def configure(self, settings: Any) -> None:
        self.settings = settings or UniFiSettings()
        self.api_key = None
        self.classic = None
        self._classic_login = None
        self._failures = 0
        self._retry_at = 0.0
        self._protect_failures = 0
        self._protect_retry_at = 0.0
        self.classic_note = None
        self._forbidden_reported = False
        self.devices_ok_at = None

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

    async def _classic_get(self, c: ClassicClient, path: str) -> list[Any]:
        """One classic read. A 403 after a good login is a permission problem and is logged once
        until a read succeeds again; the client's backoff already stops the retries."""
        try:
            rows = await c.get(path)
        except ClassicForbidden:
            if not self._forbidden_reported:
                self._forbidden_reported = True
                log.warning("unifi classic account is logged in but may not read the "
                            "controller views; check that it has view access to Network")
            raise
        self._forbidden_reported = False
        return rows

    async def classic_snapshot(self) -> dict[str, Any]:
        """Read the four classic views and parse them. Raises UniFiError, AuthRejected or
        ClassicBackedOff; returns an empty dict when no classic credential is set."""
        c = self._classic_client()
        if c is None:
            return {}
        devices = await self._classic_get(c, "stat/device")
        active = await self._classic_get(c, "stat/sta")
        known = await self._classic_get(c, "rest/user")
        health = await self._classic_get(c, "stat/health")
        return {"devices": parse_devices(devices), "wan": parse_wan_health(health),
                "offline_clients": parse_offline_clients(known, active)}

    async def close(self) -> None:
        """Log out of the classic session. The host has no shutdown hook yet, so a caller
        invokes this; the session is memory only and is lost on exit either way."""
        if self.classic is not None:
            await self.classic.logout()

    def migrations(self) -> list[Migration]:
        return list(MIGRATIONS)

    def routers(self) -> list[PluginRouter]:
        return [PluginRouter(build_pages_router(self))]  # a login session, enforced by the core

    def pages(self) -> list[PluginPage]:
        return page_files()

    def static_dir(self) -> Path:
        return Path(__file__).parent / "static"

    def nav_entries(self) -> list[NavEntry]:
        return [NavEntry("UniFi", PAGE_PATH)]

    def collectors(self) -> list[Collector]:
        s = self.settings
        # The timeout covers the site list and every device page, so it may not exceed the interval.
        found = [Collector("devices", self.collect_devices, float(s.interval),
                           min(float(s.interval), max(s.timeout * 3, 30.0)))]
        if s.classic_credential is not None:
            # Login plus four reads, each under the request timeout.
            found.append(Collector("classic", self.collect_classic, float(s.interval),
                                   min(float(s.interval), max(s.timeout * 5, 30.0))))
        found.append(Collector("clients", self.collect_clients, float(s.clients_interval),
                               min(float(s.clients_interval), max(s.timeout * 8, 30.0))))
        if s.protect:
            found.append(Collector("protect", self.collect_protect, float(s.protect_interval),
                                   min(float(s.protect_interval), max(s.timeout * 2, 30.0))))
        return found

    async def prune(self, store: Store, now: float) -> int:
        return await prune_unseen(store, now, self.settings.retention_days)

    def _backoff(self) -> None:
        self._failures += 1
        pause = min(self.settings.interval * 2 ** (self._failures - 1), MAX_BACKOFF_S)
        self._retry_at = self.clock() + pause

    def _gate(self) -> None:
        now = self.clock()
        if now < self._retry_at:
            raise BackedOff(f"pausing after a rejected credential, retry in "
                            f"{self._retry_at - now:.0f}s")

    def _api(self, base_path: str) -> IntegrationClient:
        s = self.settings
        if self.api_key is None:  # pragma: no cover - bind_credentials runs at load
            raise UniFiError("no API key bound")
        base = f"{'https' if s.https else 'http'}://{s.host}:{s.port}{base_path}"
        return IntegrationClient(base, self.api_key, s.verify_tls, s.ca_bundle, s.timeout,
                                 self.transport)

    async def _site_and_rows(self, suffix: str) -> tuple[str, list[Any]]:
        """The chosen site id and every row of `/sites/{id}/<suffix>`, with the shared backoff."""
        self._gate()
        api = self._api(self.settings.base_path)
        try:
            async with api.session() as c:
                sites = await api.list_all(c, "/sites")
                site_id = self._pick_site(sites)
                rows = await api.list_all(c, f"/sites/{site_id}/{suffix}")
        except AuthRejected:
            self._backoff()
            raise
        self._failures = 0
        self._retry_at = 0.0
        return site_id, rows

    async def collect_devices(self, store: Store) -> int:
        """One poll. Raises BackedOff while pausing, AuthRejected on a 401 or 403, UniFiError
        for an answer it refuses. Returns the number of devices stored."""
        site_id, rows = await self._site_and_rows("devices")
        devices = [d for d in (parse_device(site_id, r) for r in rows) if d is not None]
        now = self.wall()
        # The device rows and the map feed are one cycle in one transaction.
        await feed_integration(store, devices, now, save_devices=True)
        self.devices_ok_at = now
        return len(devices)

    async def collect_classic(self, store: Store) -> int:
        """One classic poll: ports, port properties and links into the infrastructure map.
        Raises what classic_snapshot raises. Returns the number of devices fed."""
        snap = await self.classic_snapshot()
        devices = snap.get("devices", [])
        await feed_classic(store, devices, self.wall())
        return len(devices)

    async def collect_clients(self, store: Store) -> int:
        """One clients poll. Raises what collect_devices raises. Classic detail is optional: a
        classic failure is noted for the pages and the Integration rows are still stored.
        Returns the number of client rows written."""
        site_id, rows = await self._site_and_rows("clients")
        now = self.wall()
        live = [c for c in (parse_client(site_id, r) for r in rows) if c is not None]
        known: list[dict[str, Any]] = []
        active: dict[str, dict[str, Any]] = {}
        self.classic_note = None
        classic_ok = True  # false keeps the stored ssid, uplink and port instead of blanking them
        if self._classic_client() is not None:
            try:
                snap = await self.classic_clients()
                active, known = snap["active"], snap["offline"]
            except (UniFiError, AuthRejected, httpx.HTTPError) as err:
                # Only the error class goes to the page; the message may name a path or status.
                # An HTTPError is a 5xx, a timeout or a refused connection.
                classic_ok = False
                self.classic_note = f"classic detail unavailable: {type(err).__name__}"
        live = enrich(live, active, await device_index(store, site_id))
        off = offline_clients(site_id, known, {c.mac for c in live if c.mac}, now,
                              self.settings.retention_days)
        return await save_clients(store, site_id, live, off, now, classic_ok)

    async def classic_clients(self) -> dict[str, Any]:
        """The two classic views the clients poll needs: connected detail by MAC and the known
        clients that are not connected."""
        c = self._classic_client()
        if c is None:
            return {"active": {}, "offline": []}
        active = await self._classic_get(c, "stat/sta")
        known = await self._classic_get(c, "rest/user")
        return {"active": parse_active_clients(active),
                "offline": parse_offline_clients(known, active)}

    async def collect_protect(self, store: Store) -> int:
        """One Protect poll of the unpaginated camera array, with its own backoff after a 401 or
        403. Returns the number of cameras stored."""
        now = self.clock()
        if now < self._protect_retry_at:
            raise BackedOff(f"pausing after a rejected credential, retry in "
                            f"{self._protect_retry_at - now:.0f}s")
        api = self._api(self.settings.protect_base_path)
        try:
            async with api.session() as c:
                body = await api.get(c, "/cameras")  # never paged: Protect 7.2.105 takes no offset
        except AuthRejected:
            self._protect_failures += 1
            pause = min(self.settings.protect_interval * 2 ** (self._protect_failures - 1),
                        MAX_BACKOFF_S)
            self._protect_retry_at = self.clock() + pause
            raise
        if not isinstance(body, list):
            raise UniFiError("/cameras did not return a list")
        self._protect_failures = 0
        self._protect_retry_at = 0.0
        cams = [c for c in (parse_camera(r) for r in body) if c is not None]
        return await save_cameras(store, cams, self.wall())

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
