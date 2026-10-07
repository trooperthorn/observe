"""The CLI runs under the new name."""

from __future__ import annotations

import importlib


def test_cli_help_runs_under_the_new_name(monkeypatch, capsys):
    main_mod = importlib.import_module("observe.__main__")
    monkeypatch.setattr("sys.argv", ["observe", "--help"])
    try:
        main_mod.main()
    except SystemExit as err:
        assert err.code == 0
    assert "usage: observe" in capsys.readouterr().out
