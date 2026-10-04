"""Importing this module fails, which proves an unlisted plugin is never imported."""

raise RuntimeError("boom_plugin was imported")
