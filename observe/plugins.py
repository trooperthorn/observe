"""The plugin host: discovery, version checks, and the hooks a plugin may use.

Plugins are Python packages that publish an object under the entry point group
`observe.plugins`. Installing one does nothing by itself. Only names listed
under `plugins:` in the config are loaded, and for an unlisted name the entry
point is never imported, so its code never runs. A listed name with no
installed package, a package whose declared core version range does not
include this release, a name clash, or a malformed hook stops startup with a
PluginError that says which plugin and why. Nothing is downloaded at runtime.

Plugins run in the same process with full trust; this module limits what a
plugin can mis-declare, not what its code can do. What it does enforce is
the HTTP surface: a plugin hands over an APIRouter and the core mounts it
under /api/plugins/<name>/ behind its own dependencies (observe/web.py,
mount_plugins). The plugin cannot choose weaker authentication, skip CSRF,
skip the rate limit or skip the audit log, because it never controls the
mounting. It can only ask for the stricter admin role.

Collectors (periodic async jobs with a declared interval of at least 30 seconds and a
timeout) are validated here and run by the scheduler, each in its own task.

Migrations are applied through the storage layer (observe/storage, plugin DDL), and
pages and static files are served by the app (observe/web.py), both only for
plugins that are listed. Hooks that later slices consume (monitor types, map
contributions, key scopes) are declared and validated here so a malformed
plugin fails at startup, but they are applied by the code for those features.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter
from fastapi.routing import APIRoute
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ValidationError

from . import __version__
from .config import Config

log = logging.getLogger("observe")

GROUP = "observe.plugins"
LEGACY_GROUP = "watchpost.plugins"  # compatibility: the entry point group of the old name
ROUTE_PREFIX = "/api/plugins"
PAGE_PREFIX = "/plugins"  # plugin pages and static files: /plugins/<name>/...
RESERVED_SCOPES = frozenset({"wpi", "wpr"})  # the core's own ingest and read token scopes
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SCOPE = re.compile(r"^[a-z]{3,8}$")
_PUBLIC_PREFIX = re.compile(r"^/api/v1(/[a-z0-9][a-z0-9-]*)*$")
MAX_LABEL = 64
MIN_COLLECTOR_INTERVAL = 30.0  # seconds; a collector may not run more often than this


class PluginError(Exception):
    """A plugin is missing, incompatible or malformed. Startup must not continue."""


@dataclass(frozen=True)
class PluginRouter:
    """A router and the strictest role it needs. `admin=True` can only tighten access.

    `key_scope` makes the router key-authenticated instead of session-authenticated: the core
    requires a bearer key of that scope, which must be one of the plugin's own key scopes, and
    puts (key prefix, bound device) on `request.state.plugin_key`. It cannot be combined with
    `admin`. `public_prefix` mounts a key-authenticated router at `/api/v1` or below instead
    of `/api/plugins/<name>`, for a stable path that a device is configured with.
    """

    router: APIRouter
    admin: bool = False
    key_scope: str | None = None
    public_prefix: str | None = None


class LogRejected(Exception):
    """Raised by a log handler for a record it refuses. The reason is counted in the partial
    success of the response (docs/DATA-API-DESIGN.md section 6.1) and is never a server error."""


@dataclass(frozen=True)
class LogContext:
    """What a log handler is told about the request a record arrived in."""

    device: str  # the device label the field key is bound to
    key_prefix: str
    now: float
    sent_ms: int | None  # the sender's clock in ms when it sent (X-Report-Sent-Ms), if given


@dataclass(frozen=True)
class KeyScope:
    """An ingest key scope the plugin owns, e.g. marker "wpf" for keys `wpf_<id>_<secret>`."""

    marker: str
    description: str = ""


@dataclass(frozen=True)
class Migration:
    """One schema step of a plugin, numbered from 1 in its own version sequence."""

    version: int
    statements: tuple[str, ...]


NAV_WORKSPACES = frozenset({"overview", "hosts", "network", "reports", "admin"})


@dataclass(frozen=True)
class NavEntry:
    label: str
    path: str
    admin_only: bool = False
    # Which console workspace the entry sits under; see NAV_WORKSPACES.
    workspace: str = "network"


@dataclass(frozen=True)
class PluginPage:
    """A static HTML page. Like /host it must hold no data; its script fetches from the API.

    The path must sit under /plugins/<name>, so a plugin cannot take over a core page.
    """

    path: str
    file: Path
    admin_only: bool = False


@dataclass(frozen=True)
class MapContribution:
    """What a plugin adds to the infrastructure map. Rows are plain mappings."""

    nodes: tuple[Mapping[str, Any], ...] = ()
    edges: tuple[Mapping[str, Any], ...] = ()
    properties: tuple[Mapping[str, Any], ...] = ()
    dependencies: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class Collector:
    """A periodic job a plugin asks the scheduler to run.

    `run` is an async callable taking the store. It runs once at startup and then every
    `interval` seconds, and is cancelled after `timeout` seconds. An exception or a timeout is
    logged once per streak of failures and never stops other collectors or the scheduler.
    """

    name: str
    run: Callable[[Any], Awaitable[Any]]
    interval: float
    timeout: float


@runtime_checkable
class Plugin(Protocol):
    """The contract. PluginBase supplies an empty default for every hook."""

    name: str
    version: str
    core_versions: str  # a PEP 440 specifier, for example ">=2026.9,<2027"

    def routers(self) -> list[PluginRouter]: ...
    def key_scopes(self) -> list[KeyScope]: ...
    def config_model(self) -> type[BaseModel] | None: ...
    def migrations(self) -> list[Migration]: ...
    def pages(self) -> list[PluginPage]: ...
    def static_dir(self) -> Path | None: ...
    def nav_entries(self) -> list[NavEntry]: ...
    def monitor_types(self) -> dict[str, Any]: ...
    def map_contribution(self, store: Any) -> Awaitable[MapContribution]: ...
    def collectors(self) -> list[Collector]: ...
    def configure(self, settings: BaseModel | None) -> None: ...
    def prune(self, store: Any, now: float) -> Awaitable[int]: ...


class PluginBase:
    """Defaults for every hook. A plugin overrides only what it uses."""

    name: str = ""
    version: str = "0"
    core_versions: str = ""

    def routers(self) -> list[PluginRouter]:
        return []

    def key_scopes(self) -> list[KeyScope]:
        return []

    def config_model(self) -> type[BaseModel] | None:
        return None

    def migrations(self) -> list[Migration]:
        return []

    def pages(self) -> list[PluginPage]:
        return []

    def static_dir(self) -> Path | None:
        """A folder inside the plugin package, served at /plugins/<name>/static."""
        return None

    def nav_entries(self) -> list[NavEntry]:
        return []

    def monitor_types(self) -> dict[str, Any]:
        return {}

    async def map_contribution(self, store: Any) -> MapContribution:
        return MapContribution()

    def collectors(self) -> list[Collector]:
        return []

    def configure(self, settings: BaseModel | None) -> None:
        """Called once with the validated settings section, or None when there is no model."""

    async def prune(self, store: Any, now: float) -> int:
        """Apply the plugin's own retention. Called about once an hour; returns rows changed."""
        return 0


@dataclass(frozen=True)
class LoadedPlugin:
    plugin: Plugin
    routers: tuple[PluginRouter, ...]
    key_scopes: tuple[KeyScope, ...]
    migrations: tuple[Migration, ...]
    pages: tuple[PluginPage, ...]
    static_dir: Path | None
    nav_entries: tuple[NavEntry, ...]
    monitor_types: Mapping[str, Any]
    settings: BaseModel | None
    collectors: tuple[Collector, ...] = ()
    # Handlers for OTLP log records of a field key, by event name (optional hook log_handlers).
    log_handlers: Mapping[str, Callable[..., Awaitable[Mapping[str, Any]]]] = field(
        default_factory=dict)

    @property
    def name(self) -> str:
        return self.plugin.name


@dataclass(frozen=True)
class LoadedPlugins:
    plugins: tuple[LoadedPlugin, ...] = ()

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.plugins]

    @property
    def scopes(self) -> list[str]:
        """Every key scope marker the listed plugins registered."""
        return [s.marker for p in self.plugins for s in p.key_scopes]

    def log_handler(self, event: str) -> tuple[str, Callable[..., Awaitable[Mapping[str, Any]]]] | None:
        """(plugin name, handler) for an OTLP log record with this event name, if any."""
        for p in self.plugins:
            handler = p.log_handlers.get(event)
            if handler is not None:
                return p.name, handler
        return None

    def get(self, name: str) -> LoadedPlugin | None:
        return next((p for p in self.plugins if p.name == name), None)

    def nav(self, is_admin: bool) -> list[dict[str, Any]]:
        return [{"plugin": p.name, "label": n.label, "path": n.path, "workspace": n.workspace}
                for p in self.plugins for n in p.nav_entries if is_admin or not n.admin_only]


EntryPoints = Callable[[], Iterable[importlib.metadata.EntryPoint]]


def installed_entry_points() -> list[importlib.metadata.EntryPoint]:
    """Entry points in `GROUP`, plus any still published under the legacy group.

    A plugin published under the legacy group keeps loading, and one warning is logged naming
    it. A name that is also published under `GROUP` is taken from there only.
    """
    found = list(importlib.metadata.entry_points(group=GROUP))
    current = {ep.name for ep in found}
    legacy = [ep for ep in importlib.metadata.entry_points(group=LEGACY_GROUP)
              if ep.name not in current]
    if legacy:
        log.warning("plugins %s are published under the legacy entry point group %s; "
                    "republish them under %s",
                    ", ".join(sorted(ep.name for ep in legacy)), LEGACY_GROUP, GROUP)
    return found + legacy


def _check_core_version(plugin: Any, core: str) -> None:
    name = plugin.name
    declared = plugin.core_versions
    if not isinstance(declared, str) or not declared.strip():
        raise PluginError(f"plugin {name!r}: core_versions is empty; declare the range of "
                          "Observe versions it supports")
    try:
        spec = SpecifierSet(declared)
    except InvalidSpecifier as err:
        raise PluginError(f"plugin {name!r}: core_versions {declared!r} "
                          "is not a valid version range") from err
    try:
        current = Version(core)
    except InvalidVersion as err:
        raise PluginError(f"Observe version {core!r} cannot be compared") from err
    if not spec.contains(current, prereleases=True):
        raise PluginError(f"plugin {name!r} supports Observe {declared} "
                          f"but this is Observe {core}; install a matching plugin release")


def _check_routers(name: str, routers: list[PluginRouter]) -> None:
    for pr in routers:
        if not isinstance(pr, PluginRouter) or not isinstance(pr.router, APIRouter):
            raise PluginError(f"plugin {name!r}: routers() must return PluginRouter objects")
        for route in pr.router.routes:
            # Mounts, websockets and raw routes would sit outside the core's dependencies.
            if not isinstance(route, APIRoute):
                raise PluginError(f"plugin {name!r}: only plain HTTP routes are allowed, "
                                  f"not {type(route).__name__}")


def _check_router_auth(name: str, routers: list[PluginRouter], scopes: list[KeyScope]) -> None:
    """Key-authenticated routers may use only the plugin's own scopes, and never the admin role."""
    own = {s.marker for s in scopes}
    for pr in routers:
        if pr.key_scope is None:
            if pr.public_prefix is not None:
                raise PluginError(f"plugin {name!r}: public_prefix needs a key_scope")
            continue
        if pr.key_scope not in own:
            raise PluginError(f"plugin {name!r}: router key scope {pr.key_scope!r} is not one "
                              "of the plugin's own key scopes")
        if pr.admin:
            raise PluginError(f"plugin {name!r}: a key-authenticated router cannot be admin")
        if pr.public_prefix is not None and not _PUBLIC_PREFIX.match(pr.public_prefix):
            raise PluginError(f"plugin {name!r}: public_prefix {pr.public_prefix!r} must be "
                              "/api/v1 or a path below it")


def _check_scopes(name: str, scopes: list[KeyScope], seen: set[str]) -> None:
    for s in scopes:
        if not _SCOPE.match(s.marker) or s.marker in RESERVED_SCOPES:
            raise PluginError(f"plugin {name!r}: key scope {s.marker!r} must be 3 to 8 "
                              "lower-case letters and not a core scope")
        if s.marker in seen:
            raise PluginError(f"plugin {name!r}: key scope {s.marker!r} is already taken")
        seen.add(s.marker)


def _check_migrations(name: str, migrations: list[Migration]) -> None:
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise PluginError(f"plugin {name!r}: migration versions must be 1, 2, 3 ... in order")


def _check_nav_and_pages(name: str, nav: list[NavEntry], pages: list[PluginPage],
                         static_dir: Path | None) -> None:
    for n in nav:
        if not n.label or len(n.label) > MAX_LABEL or not n.path.startswith("/") \
                or n.path.startswith("//"):
            raise PluginError(f"plugin {name!r}: nav entry {n.label!r} needs a short label "
                              "and a path starting with one /")
        if n.workspace not in NAV_WORKSPACES:
            raise PluginError(f"plugin {name!r}: nav entry {n.label!r} has workspace "
                              f"{n.workspace!r}, which must be one of {sorted(NAV_WORKSPACES)}")
    base = f"{PAGE_PREFIX}/{name}"
    static = f"{base}/static"
    if static_dir is not None and not Path(static_dir).is_dir():
        raise PluginError(f"plugin {name!r}: static_dir {str(static_dir)!r} is not a folder")
    seen: set[str] = set()
    for p in pages:
        if not isinstance(p, PluginPage) or not (p.path == base or p.path.startswith(base + "/")) \
                or ".." in p.path.split("/") or p.path.endswith("/") \
                or p.path == static or p.path.startswith(static + "/"):
            raise PluginError(f"plugin {name!r}: page path {getattr(p, 'path', p)!r} must be "
                              f"{base} or below it, and not under {static}")
        if p.path in seen:
            raise PluginError(f"plugin {name!r}: page path {p.path!r} is declared twice")
        seen.add(p.path)
        if not Path(p.file).is_file():
            raise PluginError(f"plugin {name!r}: page file {str(p.file)!r} does not exist")


def _check_monitor_types(name: str, types: dict[str, Any]) -> None:
    prefix = name + "."
    for t in types:
        if not t.startswith(prefix):
            raise PluginError(f"plugin {name!r}: monitor type {t!r} must start with "
                              f"{prefix!r}, so it cannot shadow a core type")


def _check_collectors(name: str, collectors: list[Collector]) -> None:
    seen: set[str] = set()
    for c in collectors:
        if not isinstance(c, Collector) or not isinstance(c.name, str)                 or not _NAME.match(c.name) or not callable(c.run)                 or not inspect.iscoroutinefunction(c.run):
            raise PluginError(f"plugin {name!r}: collectors() must return Collector objects "
                              "with a lower-case name and an async callable")
        if c.name in seen:
            raise PluginError(f"plugin {name!r}: collector {c.name!r} is declared twice")
        seen.add(c.name)
        if isinstance(c.interval, bool) or not isinstance(c.interval, (int, float))                 or not c.interval >= MIN_COLLECTOR_INTERVAL:
            raise PluginError(f"plugin {name!r}: collector {c.name!r} interval "
                              f"{c.interval!r} must be a number of at least "
                              f"{MIN_COLLECTOR_INTERVAL:g} seconds")
        if isinstance(c.timeout, bool) or not isinstance(c.timeout, (int, float))                 or not 0 < c.timeout <= c.interval:
            raise PluginError(f"plugin {name!r}: collector {c.name!r} timeout {c.timeout!r} "
                              "must be above 0 and no longer than its interval")


def _check_log_handlers(name: str, handlers: Any, taken: set[str]) -> dict[str, Any]:
    """Event names a plugin handles must sit under observe.<plugin>. and be taken once."""
    if not isinstance(handlers, dict):
        raise PluginError(f"plugin {name!r}: log_handlers() must return a dict")
    prefix = f"observe.{name}."
    for event, handler in handlers.items():
        if not isinstance(event, str) or not event.startswith(prefix) or len(event) > MAX_LABEL * 2:
            raise PluginError(f"plugin {name!r}: log event {event!r} must start with {prefix!r}")
        if not callable(handler) or not inspect.iscoroutinefunction(handler):
            raise PluginError(f"plugin {name!r}: the handler of {event!r} must be an async callable")
        if event in taken:
            raise PluginError(f"plugin {name!r}: log event {event!r} is already taken")
        taken.add(event)
    return dict(handlers)


def _settings(name: str, model: type[BaseModel] | None,
              raw: Mapping[str, Any]) -> BaseModel | None:
    if model is None:
        if raw:
            raise PluginError(f"plugin {name!r} takes no settings, but "
                              f"plugin_settings.{name} is set")
        return None
    try:
        return model.model_validate(dict(raw))
    except ValidationError as err:
        # Only where and why, never the rejected value, which may be a secret.
        problems = "; ".join(f"{'.'.join(str(x) for x in e['loc']) or '(section)'}: {e['msg']}"
                             for e in err.errors())
        raise PluginError(f"plugin {name!r}: invalid settings in "
                          f"plugin_settings.{name}: {problems}") from err


def _validate(listed: str, plugin: Any, core: str, scopes_seen: set[str],
              settings_raw: Mapping[str, Any],
              credentials: Mapping[str, Any] | None = None,
              events_seen: set[str] | None = None) -> LoadedPlugin:
    events_seen = set() if events_seen is None else events_seen
    if not isinstance(plugin, Plugin):
        raise PluginError(f"plugin {listed!r}: the entry point does not provide a Plugin")
    name = plugin.name
    if name != listed or not _NAME.match(name):
        raise PluginError(f"plugin {listed!r}: its name is {name!r}; the entry point name and "
                          "the plugin name must match and be lower-case letters, digits or _")
    _check_core_version(plugin, core)
    try:
        settings = _settings(name, plugin.config_model(), settings_raw)
        # Configured before collectors() is read, so a collector can take its interval from the
        # settings. A plugin that fails later still stops startup, so nothing runs half set up.
        plugin.configure(settings)
        routers, scopes = plugin.routers(), plugin.key_scopes()
        migrations, pages = plugin.migrations(), plugin.pages()
        static_dir = plugin.static_dir()
        nav, mtypes = plugin.nav_entries(), plugin.monitor_types()
        collectors = plugin.collectors()
        hook = getattr(plugin, "log_handlers", None)
        handlers = hook() if callable(hook) else {}
    except PluginError:
        raise
    except Exception as err:
        raise PluginError(f"plugin {name!r}: a hook failed: {type(err).__name__}: {err}") from err
    _check_routers(name, routers)
    _check_scopes(name, scopes, scopes_seen)
    _check_router_auth(name, routers, scopes)
    _check_migrations(name, migrations)
    _check_nav_and_pages(name, nav, pages, static_dir)
    _check_monitor_types(name, mtypes)
    _check_collectors(name, collectors)
    handlers = _check_log_handlers(name, handlers, events_seen)
    bind = getattr(plugin, "bind_credentials", None)
    if callable(bind):
        # Optional hook: the plugin gets the named credentials of the config, so its settings
        # can refer to one by name. A plugin raises PluginError for a name it cannot use.
        try:
            bind(dict(credentials or {}))
        except PluginError:
            raise
        except Exception as err:
            raise PluginError(f"plugin {name!r}: a hook failed: {type(err).__name__}: "
                              f"{err}") from err
    return LoadedPlugin(plugin, tuple(routers), tuple(scopes), tuple(migrations), tuple(pages),
                        static_dir, tuple(nav), dict(mtypes), settings, tuple(collectors), handlers)


def load_plugins(config: Config, entry_points: EntryPoints = installed_entry_points,
                 core_version: str = __version__) -> LoadedPlugins:
    """Load exactly the plugins named in `config.plugins`, or raise PluginError."""
    by_name: dict[str, list[importlib.metadata.EntryPoint]] = {}
    for ep in entry_points():
        by_name.setdefault(ep.name, []).append(ep)
    out: list[LoadedPlugin] = []
    scopes_seen: set[str] = set()
    events_seen: set[str] = set()
    for name in config.plugins:
        found = by_name.get(name, [])
        if not found:
            raise PluginError(f"plugin {name!r} is listed under plugins: but no installed "
                              f"package provides it in entry point group {GROUP}")
        if len(found) > 1:
            raise PluginError(f"plugin {name!r} is provided by more than one package: "
                              + ", ".join(sorted({e.value for e in found})))
        try:
            obj = found[0].load()
            if not isinstance(obj, Plugin) and callable(obj):
                obj = obj()
        except Exception as err:
            raise PluginError(f"plugin {name!r} failed to import: "
                              f"{type(err).__name__}: {err}") from err
        out.append(_validate(name, obj, core_version, scopes_seen,
                             config.plugin_settings.get(name, {}), config.credentials, events_seen))
    return LoadedPlugins(tuple(out))
