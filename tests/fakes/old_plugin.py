"""A plugin that supports only an old core version."""

from observe.plugins import PluginBase


class OldPlugin(PluginBase):
    name = "old"
    version = "0.1"
    core_versions = ">=1,<2"


plugin = OldPlugin()
