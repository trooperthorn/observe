"""The Pockethernet plugin: field reports from the Pockethernet Android app.

This slice holds the report schema and the wpf key scope. The upload endpoint,
the mapping to port properties and the pages follow (docs/FIELD-DATA.md).
"""

from __future__ import annotations

from watchpost.plugins import KeyScope, PluginBase

from .keys import SCOPE

__version__ = "0.1.0"


class PockethernetPlugin(PluginBase):
    name = "pockethernet"
    version = __version__
    # The core release series this plugin was written against. A mismatch refuses to start.
    core_versions = ">=2026.9,<2027"

    def key_scopes(self) -> list[KeyScope]:
        return [KeyScope(SCOPE, "Pockethernet field report upload, bound to a device label")]


plugin = PockethernetPlugin()
