"""The plugin host: discovery, version checks, and the hooks a plugin may use.

Plugins are Python packages that publish an object under the entry point group
`watchpost.plugins`. Installing one does nothing by itself. Only names listed
under `plugins:` in the config are loaded, and for an unlisted name the entry
point is never imported, so its code never runs. A listed name with no
installed package, a package whose declared core version range does not
include this release, a name clash, or a malformed hook stops startup with a
PluginError that says which plugin and why. Nothing is downloaded at runtime.

Plugins run in the same process with full trust; this module limits what a
plugin can mis-declare, not what its code can do. What it does enforce is
the HTTP surface: a plugin hands over an APIRouter and the core mounts it
under /api/plugins/<name>/ behind its own dependencies (watchpost/web.py,
mount_plugins). The plugin cannot choose weaker authentication, skip CSRF,
skip the rate limit or skip the audit log, because it never controls the
mounting. It can only ask for the stricter admin role.

Migrations are applied by the store (watchpost/store.py, migrate_plugins), and
pages and static files are served by the app (watchpost/web.py), both only for
plugins that are listed. Hooks that later slices consume (monitor types, map
contributions, key scopes) are declared and validated here so a malformed
plugin fails at startup, but they are applied by the code for those features.
"""

from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter
from fastapi.routing import APIRoute
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ValidationError

from . import __version__
from .config import Config

GROUP = "watchpost.plugins"
ROUTE_PREFIX = "/api/plugins"
PAGE_PREFIX = "/plugins"  # plugin pages and static files: /plugins/<name>/...
RESERVED_SCOPES = frozenset({"wpi"})  # the core's own ingest key scope
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SCOPE = re.compile(r"^[a-z]{3,8}$")
MAX_LABEL = 64


class PluginError(Exception):
    """A plugin is missing, incompatible or malformed. Startup must not continue."""


@dataclass(frozen=True)
class PluginRouter:
    """A router and the strictest role it needs. `admin=True` can only tighten access."""

    router: APIRouter
    admin: bool = False


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


@dataclass(frozen=True)
class NavEntry:
    label: str
    path: str
    admin_only: bool = False


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
    def configure(self, settings: BaseModel | None) -> None: ...


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

    def configure(self, settings: BaseModel | None) -> None:
        """Called once with the validated settings section, or None when there is no model."""


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

    @property
    def name(self) -> str:
        return self.plugin.name


@dataclass(frozen=True)
class LoadedPlugins:
    plugins: tuple[LoadedPlugin, ...] = ()

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.plugins]

    def get(self, name: str) -> LoadedPlugin | None:
        return next((p for p in self.plugins if p.name == name), None)

    def nav(self, is_admin: bool) -> list[dict[str, Any]]:
        return [{"plugin": p.name, "label": n.label, "path": n.path}
                for p in self.plugins for n in p.nav_entries if is_admin or not n.admin_only]


EntryPoints = Callable[[], Iterable[importlib.metadata.EntryPoint]]


def installed_entry_points() -> list[importlib.metadata.EntryPoint]:
    return list(importlib.metadata.entry_points(group=GROUP))


def _check_core_version(plugin: Any, core: str) -> None:
    name = plugin.name
    declared = plugin.core_versions
    if not isinstance(declared, str) or not declared.strip():
        raise PluginError(f"plugin {name!r}: core_versions is empty; declare the range of "
                          "watchpost versions it supports")
    try:
        spec = SpecifierSet(declared)
    except InvalidSpecifier as err:
        raise PluginError(f"plugin {name!r}: core_versions {declared!r} "
                          "is not a valid version range") from err
    try:
        current = Version(core)
    except InvalidVersion as err:
        raise PluginError(f"watchpost version {core!r} cannot be compared") from err
    if not spec.contains(current, prereleases=True):
        raise PluginError(f"plugin {name!r} supports watchpost {declared} "
                          f"but this is watchpost {core}; install a matching plugin release")


def _check_routers(name: str, routers: list[PluginRouter]) -> None:
    for pr in routers:
        if not isinstance(pr, PluginRouter) or not isinstance(pr.router, APIRouter):
            raise PluginError(f"plugin {name!r}: routers() must return PluginRouter objects")
        for route in pr.router.routes:
            # Mounts, websockets and raw routes would sit outside the core's dependencies.
            if not isinstance(route, APIRoute):
                raise PluginError(f"plugin {name!r}: only plain HTTP routes are allowed, "
                                  f"not {type(route).__name__}")


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
              settings_raw: Mapping[str, Any]) -> LoadedPlugin:
    if not isinstance(plugin, Plugin):
        raise PluginError(f"plugin {listed!r}: the entry point does not provide a Plugin")
    name = plugin.name
    if name != listed or not _NAME.match(name):
        raise PluginError(f"plugin {listed!r}: its name is {name!r}; the entry point name and "
                          "the plugin name must match and be lower-case letters, digits or _")
    _check_core_version(plugin, core)
    try:
        settings = _settings(name, plugin.config_model(), settings_raw)
        routers, scopes = plugin.routers(), plugin.key_scopes()
        migrations, pages = plugin.migrations(), plugin.pages()
        static_dir = plugin.static_dir()
        nav, mtypes = plugin.nav_entries(), plugin.monitor_types()
    except PluginError:
        raise
    except Exception as err:
        raise PluginError(f"plugin {name!r}: a hook failed: {type(err).__name__}: {err}") from err
    _check_routers(name, routers)
    _check_scopes(name, scopes, scopes_seen)
    _check_migrations(name, migrations)
    _check_nav_and_pages(name, nav, pages, static_dir)
    _check_monitor_types(name, mtypes)
    plugin.configure(settings)
    return LoadedPlugin(plugin, tuple(routers), tuple(scopes), tuple(migrations), tuple(pages),
                        static_dir, tuple(nav), dict(mtypes), settings)


def load_plugins(config: Config, entry_points: EntryPoints = installed_entry_points,
                 core_version: str = __version__) -> LoadedPlugins:
    """Load exactly the plugins named in `config.plugins`, or raise PluginError."""
    by_name: dict[str, list[importlib.metadata.EntryPoint]] = {}
    for ep in entry_points():
        by_name.setdefault(ep.name, []).append(ep)
    out: list[LoadedPlugin] = []
    scopes_seen: set[str] = set()
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
                             config.plugin_settings.get(name, {})))
    return LoadedPlugins(tuple(out))
