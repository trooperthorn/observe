"""Compatibility with the old product name (watchpost).

Each helper reads the old name only when the new one is absent and logs one
warning that names the new one. Nothing here moves, copies or deletes a file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("observe")

NEW_PREFIX = "OBSERVE_"
OLD_PREFIX = "WATCHPOST_"  # compatibility: the environment prefix of the old name

DEFAULT_CONFIG = "/config/observe.yaml"
LEGACY_CONFIG = "/config/watchpost.yaml"  # compatibility: the old default config path
DEFAULT_DB = "/data/observe.db"
LEGACY_DB = "/data/watchpost.db"  # compatibility: the old default database path

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message)


def getenv(name: str, default: str | None = None) -> str | None:
    """Read an OBSERVE_* variable, falling back to its WATCHPOST_* name with a warning."""
    if name in os.environ:
        return os.environ[name]
    if name.startswith(NEW_PREFIX):
        old = OLD_PREFIX + name[len(NEW_PREFIX):]
        if old in os.environ:
            _warn_once(old, f"environment variable {old} is the old name; set {name} instead")
            return os.environ[old]
    return default


def resolve_config_path(path: str | None) -> str:
    """An explicit path is used as given, except the default path itself (which the
    Docker CMD passes). With no path, or the default, prefer observe.yaml and fall back
    to the old watchpost.yaml next to it with a warning."""
    if path and path != DEFAULT_CONFIG:
        return path
    if not Path(DEFAULT_CONFIG).exists() and Path(LEGACY_CONFIG).exists():
        _warn_once("config", f"using {LEGACY_CONFIG}, the old name; rename it to {DEFAULT_CONFIG} "
                             "(see docs/UPGRADING-FROM-WATCHPOST.md)")
        return LEGACY_CONFIG
    return DEFAULT_CONFIG


class DatabaseMissing(Exception):
    """The configured database file is missing but the same database exists under the other name."""


_DB_NAMES = ("observe.db", "watchpost.db")


def resolve_db_path(path: str) -> str:
    """Keep using an existing old database file when the default new one is absent.

    Never start on a new, empty database while the real one sits beside it under the other name:
    that happens when the file was renamed but `server.db_path` was not, and it looks like every
    user, key and host has vanished. Starting is refused with the exact fix instead.
    """
    if path == DEFAULT_DB and not Path(DEFAULT_DB).exists() and Path(LEGACY_DB).exists():
        _warn_once("db", f"using {LEGACY_DB}, the old name; rename it to {DEFAULT_DB} "
                         "(see docs/UPGRADING-FROM-WATCHPOST.md)")
        return LEGACY_DB
    target = Path(path)
    if path != ":memory:" and target.name in _DB_NAMES and not target.exists():
        for other in _DB_NAMES:
            sibling = target.with_name(other)
            if other != target.name and sibling.exists() and sibling.stat().st_size > 0:
                raise DatabaseMissing(
                    f"server.db_path is {path}, which does not exist, but {sibling} does. "
                    f"Starting now would create a new, empty database. Set server.db_path to "
                    f"{sibling.as_posix() if os.sep == '/' else sibling} in the config, or rename the "
                    f"file to {target.name}, then start again.")
    return path


def warn_mqtt_default_prefix() -> None:
    """An MQTT target without an explicit topic_prefix now publishes under observe/."""
    _warn_once("mqtt", "an mqtt alert target uses the default topic_prefix, which is now "
                       "observe; set topic_prefix: watchpost on it if subscribers still "
                       "listen on the old topics (see docs/UPGRADING-FROM-WATCHPOST.md)")
