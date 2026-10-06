"""A PostgreSQL dialect fake: the SQLite engine behind the PostgreSQL translation.

Every statement a unit of work or a read sends goes through the same rewrite the PostgreSQL
backend applies (observe/storage/pg_dialect.py). The rewritten text is recorded, checked for
forms PostgreSQL does not accept, and then turned back into the few spellings SQLite needs, so
the statement can run on an in-memory database. The schema itself is created by the ordinary
SQLite migration, because its translation is checked as text in tests/test_pg_dialect.py.

This proves the SQL text and the results the callers expect without a server. It does not prove
what only a server can show (types, planner, locking); the live cases that need
OBSERVE_TEST_PG_DSN still cover that.
"""

from __future__ import annotations

import re
from typing import Any

from observe.storage.pg_dialect import table_info_query, translate_sql
from observe.storage.sqlite import SqliteStorage

# Spellings that only SQLite accepts. None may survive the rewrite.
SQLITE_ONLY = re.compile(
    r"\b(?:INSERT\s+OR|AUTOINCREMENT|WITHOUT\s+ROWID|rowid|IFNULL|GROUP_CONCAT|julianday|"
    r"strftime|IIF|GLOB|TOTAL|INSTR|sqlite_master|PRAGMA)\b|\bdatetime\s*\(|\bLIMIT\s+-", re.I)
_TRANSACTION = re.compile(r"^\s*(?:BEGIN|COMMIT|ROLLBACK)\b", re.I)
_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


def _outside_quotes(sql: str) -> str:
    return _QUOTED.sub("''", sql)


def back_to_sqlite(pg_sql: str, has_args: bool) -> str:
    """The SQLite spelling of PostgreSQL text: only what the rewrite introduced is undone."""
    out = []
    pos = 0
    for m in _QUOTED.finditer(pg_sql):
        out.append(_code(pg_sql[pos:m.start()], has_args))
        out.append(m.group(0).replace("%%", "%") if has_args else m.group(0))
        pos = m.end()
    out.append(_code(pg_sql[pos:], has_args))
    return "".join(out)


def _code(text: str, has_args: bool) -> str:
    if has_args:
        text = text.replace("%s", "?").replace("%%", "%")
    text = re.sub(r"\bGREATEST\(", "MAX(", text)
    text = re.sub(r"\bLEAST\(", "MIN(", text)
    return re.sub(r"\bctid\b", "rowid", text)


class FakePgConn:
    """The connection handed to units: PgConn's behaviour over a SQLite connection."""

    def __init__(self, inner: Any, seen: list[str]) -> None:
        self._inner = inner
        self.seen = seen

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _rewrite(self, sql: str, has_args: bool) -> str:
        pg = translate_sql(sql, has_args=has_args)
        bare = _outside_quotes(pg)
        assert not SQLITE_ONLY.search(bare), f"not PostgreSQL: {pg}"
        assert "?" not in bare, f"unconverted placeholder: {pg}"
        self.seen.append(pg)
        return back_to_sqlite(pg, has_args)

    def execute(self, sql: str, args: Any = ()) -> Any:
        if _TRANSACTION.match(sql):
            return self._inner.execute(sql)
        info = table_info_query(sql)
        if info is not None:
            self.seen.append(info[0])
            return self._inner.execute(f"PRAGMA table_info({info[1][0]})")
        args = tuple(int(a) if isinstance(a, bool) else a for a in args)
        return self._inner.execute(self._rewrite(sql, bool(args)), args)

    def executemany(self, sql: str, rows: Any) -> Any:
        rows = [tuple(int(a) if isinstance(a, bool) else a for a in r) for r in rows]
        return self._inner.executemany(self._rewrite(sql, True), rows)


class PgFakeStorage(SqliteStorage):
    """SqliteStorage whose units speak PostgreSQL. `statements` records every rewritten text."""

    backend = "postgres"

    def __init__(self, plugins: Any = None) -> None:
        super().__init__(":memory:", plugins)
        self.statements: list[str] = []
        self.timescale = False
        self._conn = FakePgConn(self._conn, self.statements)
