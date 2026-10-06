"""All database access goes through the Storage interface (docs/DATA-API-DESIGN.md section 2.11).

Outside observe/storage nothing may import a database driver, open a connection, take the old
store lock or reach the old private connection. A new violation fails the build. Tests may use a
driver to build or inspect a database file, but not the removed private paths."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "observe" / "storage"

DRIVER = [
    (re.compile(r"^\s*(?:import|from)\s+(?:sqlite3|psycopg|psycopg_pool)\b", re.M),
     "imports a database driver"),
    (re.compile(r"\b(?:sqlite3|psycopg)\.connect\("), "opens a connection"),
]
PRIVATE = [
    (re.compile(r"\._db\b"), "touches the removed private connection (_db)"),
    (re.compile(r"\._lock\b"), "takes the removed store lock (_lock)"),
    (re.compile(r"\b(?:store|_store|infra|_infra)\._run\("), "uses the removed _run helper"),
    (re.compile(r"\b(?:store|_store)\._exec\("), "uses the removed _exec helper"),
    (re.compile(r"\b(?:store|_store)\._delete\b"), "uses the removed _delete helper"),
]


def _files(*bases: str) -> list[Path]:
    return [p for base in bases for p in sorted((ROOT / base).rglob("*.py"))
            if ".venv" not in p.parts and STORAGE not in p.parents and p.name != Path(__file__).name]


def _scan(text: str, rules: list[tuple[re.Pattern[str], str]]) -> list[tuple[int, str]]:
    return [(text.count("\n", 0, m.start()) + 1, why)
            for pattern, why in rules for m in pattern.finditer(text)]


def _violations(paths: list[Path], rules: list[tuple[re.Pattern[str], str]]) -> list[str]:
    return [f"{p.relative_to(ROOT)}:{line} {why}"
            for p in paths for line, why in _scan(p.read_text(encoding="utf-8"), rules)]


def test_the_application_and_plugins_reach_the_database_only_through_storage():
    assert _violations(_files("observe", "plugins"), DRIVER + PRIVATE) == []


def test_tests_do_not_reach_into_the_removed_private_paths():
    assert _violations(_files("tests"), PRIVATE) == []


def test_the_ban_recognises_each_kind_of_violation():
    sample = ("import sqlite3\nfrom psycopg import connect\nx = sqlite3.connect('f')\n"
              "store._db.execute('x')\nwith store._lock:\n    pass\nawait store._run('s')\n"
              "store._exec('s')\nstore._delete\n")
    kinds = {why for _, why in _scan(sample, DRIVER + PRIVATE)}
    assert len(kinds) == len(DRIVER) + len(PRIVATE)
