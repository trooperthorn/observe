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
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from observe.plugins import (Collector, Migration, NavEntry, PluginBase, PluginError,
                             PluginPage)
from observe.store import Store

from .classic import (ClassicBackedOff, ClassicClient, ClassicForbidden, parse_devices, parse_offline_clients,
                      parse_wan_health, parse_wlans, select_gateway_row, wan_status)
from .clients import (device_index, enrich, offline_clients, parse_active_clients,
                      parse_camera, parse_client, save_cameras, save_clients)
from .feed import feed_classic, feed_integration
from .api import register as register_resources
from .client import AuthRejected, IntegrationClient, UniFiError
from .pages import PAGE_PATH, page_files
from .records import (MIGRATIONS, parse_device, parse_uplink_stats, prune_unseen, read_networks,
                      select_gateway, write_site_status_classic, write_wlans)

__version__ = "0.1.0"


MAX_BACKOFF_S = 3600.0
# A device id from the console goes into a request path only when it is a plain token.
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# Device details read per devices poll to learn uplinks the list rows do not carry.
MAX_DETAIL_READS = 64
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
        self._site_id: str | None = None

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
        self._site_id = None

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
        """Read the five classic views and parse them. Raises UniFiError, AuthRejected or
        ClassicBackedOff; returns an empty dict when no classic credential is set. A console
        that refuses rest/wlanconf, or answers it without a data list, gives an empty SSID list
        instead of failing the poll; a backoff is still raised."""
        c = self._classic_client()
        if c is None:
            return {}
        devices = await self._classic_get(c, "stat/device")
        active = await self._classic_get(c, "stat/sta")
        known = await self._classic_get(c, "rest/user")
        health = await self._classic_get(c, "stat/health")
        try:
            wlans = await self._classic_get(c, "rest/wlanconf")
        except ClassicBackedOff:
            raise
        except UniFiError as err:  # the view is optional: a 404 or an odd body leaves no SSIDs
            log.debug("unifi rest/wlanconf not read: %s", type(err).__name__)
            wlans = []
        return {"devices": parse_devices(devices), "wan": parse_wan_health(health),
                "offline_clients": parse_offline_clients(known, active),
                "wlans": parse_wlans(wlans)}

    async def close(self) -> None:
        """Log out of the classic session. The host has no shutdown hook yet, so a caller
        invokes this; the session is memory only and is lost on exit either way."""
        if self.classic is not None:
            await self.classic.logout()

    def migrations(self) -> list[Migration]:
        return list(MIGRATIONS)

    def register_api(self, api: Any) -> None:
        """The /api/v2/unifi resources (docs/DATA-API-DESIGN.md section 4.10)."""
        register_resources(api, self)

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

    async def _site_and_rows(self, suffix: str, then: Callable[..., Any] | None = None
                             ) -> tuple[str, list[Any], Any]:
        """The chosen site id and every row of `/sites/{id}/<suffix>`, with the shared backoff.
        `then(api, c, site_id, rows)` runs one more read on the same session; its result is the
        third value (None without it)."""
        self._gate()
        api = self._api(self.settings.base_path)
        extra = None
        try:
            async with api.session() as c:
                sites = await api.list_all(c, "/sites")
                site_id = self._pick_site(sites)
                self._site_id = site_id
                rows = await api.list_all(c, f"/sites/{site_id}/{suffix}")
                if then is not None:
                    extra = await then(api, c, site_id, rows)
        except AuthRejected:
            self._backoff()
            raise
        self._failures = 0
        self._retry_at = 0.0
        return site_id, rows, extra

    @staticmethod
    async def _gateway_stats(api: IntegrationClient, c: Any, site_id: str,
                             rows: list[Any]) -> tuple[str, dict[str, Any]]:
        """The gateway's id and its `statistics/latest` uplink (records.parse_uplink_stats).
        One extra GET per devices poll, only for the gateway; a refused or malformed answer
        leaves the WAN unknown and never fails the devices poll. An id is only used in a path
        when it is a plain token."""
        gw = select_gateway(rows)
        gid = gw.get("id") if gw else None
        if not isinstance(gid, str) or not ID_RE.match(gid):
            return "", parse_uplink_stats(None)
        try:
            stats = await api.get(c, f"/sites/{site_id}/devices/{gid}/statistics/latest")
        except (UniFiError, httpx.HTTPError) as err:
            log.debug("unifi gateway statistics not read: %s", type(err).__name__)
            return gid, parse_uplink_stats(None)
        return gid, parse_uplink_stats(stats)

    @staticmethod
    async def _uplinks(api: IntegrationClient, c: Any, site_id: str, rows: list[Any]) -> int:
        """Fill in the uplink of each device row that does not name one. The Integration list row
        carries no uplink; the device detail (`GET /devices/{id}`) has `uplink.deviceId`, which
        is what the map draws its links from. Without this the map had devices but no links. At
        most MAX_DETAIL_READS details per poll; a refused or malformed detail leaves that device
        without a link and never fails the poll. Returns how many uplinks were found."""
        found = 0
        for row in [r for r in rows if isinstance(r, dict)][:MAX_DETAIL_READS]:
            up = row.get("uplink")
            if (isinstance(up, dict) and up.get("deviceId")) or row.get("uplinkDeviceId"):
                continue
            did = row.get("id")
            if not isinstance(did, str) or not ID_RE.match(did):
                continue
            try:
                detail = await api.get(c, f"/sites/{site_id}/devices/{did}")
            except (UniFiError, httpx.HTTPError) as err:
                log.debug("unifi device detail not read: %s", type(err).__name__)
                continue
            dup = detail.get("uplink") if isinstance(detail, dict) else None
            if isinstance(dup, dict) and isinstance(dup.get("deviceId"), str) and dup["deviceId"]:
                row["uplink"] = {**(up if isinstance(up, dict) else {}),
                                 "deviceId": dup["deviceId"]}
                found += 1
        return found

    async def _devices_extra(self, api: IntegrationClient, c: Any, site_id: str,
                             rows: list[Any]) -> tuple[str, dict[str, Any]]:
        await self._uplinks(api, c, site_id, rows)
        return await self._gateway_stats(api, c, site_id, rows)

    async def collect_devices(self, store: Store) -> int:
        """One poll. Raises BackedOff while pausing, AuthRejected on a 401 or 403, UniFiError
        for an answer it refuses. Returns the number of devices stored."""
        site_id, rows, (gid, wan) = await self._site_and_rows("devices", self._devices_extra)
        devices = [d for d in (parse_device(site_id, r) for r in rows) if d is not None]
        now = self.wall()
        # The device rows, the site status and the map feed are one cycle in one transaction.
        await feed_integration(store, devices, now, save_devices=True,
                               site_status=(site_id, gid, wan))
        self.devices_ok_at = now
        return len(devices)

    async def collect_classic(self, store: Store) -> int:
        """One classic poll: ports, port properties and links into the infrastructure map, the
        site's WAN and network table, and the SSIDs. Raises what classic_snapshot raises.
        Returns the number of devices fed."""
        snap = await self.classic_snapshot()
        devices = snap.get("devices", [])
        now = self.wall()
        await feed_classic(store, devices, now)
        site_id = self.classic_site_id
        if site_id:
            gateway = select_gateway_row(devices)
            networks = gateway["networks"] if gateway else []
            wan = wan_status(snap.get("wan"), gateway)
            wlans = resolve_wlans(snap.get("wlans", []), networks)
            await store.storage.write(lambda db: (
                write_site_status_classic(db, site_id, wan, networks, now),
                write_wlans(db, site_id, wlans, now)), touches=("unifi",))
        return len(devices)

    @property
    def classic_site_id(self) -> str | None:
        """The Integration site id the classic rows belong to: the one the last devices or
        clients poll chose. Until a poll has run, classic site rows cannot be keyed and are
        skipped."""
        return self._site_id

    async def collect_clients(self, store: Store) -> int:
        """One clients poll. Raises what collect_devices raises. Classic detail is optional: a
        classic failure is noted for the pages and the Integration rows are still stored.
        Returns the number of client rows written."""
        site_id, rows, _ = await self._site_and_rows("clients")
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
        networks = await store.storage.read(lambda db: read_networks(db, site_id))
        live = enrich(live, active, await device_index(store, site_id), networks)
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
        return {"active": parse_active_clients(active, self.wall()),
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


def resolve_wlans(wlans: list[dict[str, Any]], networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each parsed SSID the name and VLAN of its network from the gateway's table. AP
    group ids are not resolved (rest/apgroups is not read), so `ap_names` stays empty and the
    page says the restriction cannot be named."""
    by_id = {n.get("id"): n for n in networks if n.get("id")}
    out = []
    for w in wlans:
        net = by_id.get(w["network_id"])
        out.append({**w, "network_name": net.get("name", "") if net else "",
                    "vlan": net.get("vlan") if net else None, "ap_names": []})
    return out


plugin = UniFiPlugin()
