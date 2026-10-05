"""The rename to Observe: the CLI imports and the old plugin entry point group still loads."""

from __future__ import annotations

import importlib
import importlib.metadata
import logging

from observe import plugins


def test_cli_help_runs_under_the_new_name(monkeypatch, capsys):
    main_mod = importlib.import_module("observe.__main__")
    monkeypatch.setattr("sys.argv", ["observe", "--help"])
    try:
        main_mod.main()
    except SystemExit as err:
        assert err.code == 0
    assert "usage: observe" in capsys.readouterr().out


class _EP:
    def __init__(self, name, group):
        self.name, self.group, self.value = name, group, "x:y"


def test_legacy_group_is_read_with_one_warning(monkeypatch, caplog):
    def fake(group):
        return {plugins.LEGACY_GROUP: [_EP("old", group)],
                plugins.GROUP: [_EP("new", group)]}.get(group, [])
    monkeypatch.setattr(importlib.metadata, "entry_points", fake)
    with caplog.at_level(logging.WARNING, logger="observe"):
        found = plugins.installed_entry_points()
    assert sorted(e.name for e in found) == ["new", "old"]
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1 and "watchpost.plugins" in warns[0].getMessage()


def test_no_warning_without_legacy_entries(monkeypatch, caplog):
    monkeypatch.setattr(importlib.metadata, "entry_points",
                        lambda group: [_EP("new", group)] if group == plugins.GROUP else [])
    with caplog.at_level(logging.WARNING, logger="observe"):
        assert [e.name for e in plugins.installed_entry_points()] == ["new"]
    assert not caplog.records
