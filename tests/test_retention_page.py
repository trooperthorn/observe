"""The admin retention and storage pages: static pages that hold no data, an admin only read of
the retention document and the storage status from /api/v2, the rollup_state rows from the
storage, the backend name and never the DSN or its password."""

from __future__ import annotations

import json

from observe.storage import rollups

from .test_auth import Env
from .test_ui_shell import _nav_table

DSN = "postgresql://observe:hunter2secret@db.internal:5432/observe"
PASSWORD = "hunter2secret"
RETENTION = "/api/v2/admin/settings/retention"
STORAGE = "/api/v2/admin/settings/storage"


class FakeStorage:
    """Delegates to the real storage but reports its own backend and rollup_state rows. It also
    holds a DSN and a password the way a PostgreSQL backend does, to show they never leak."""

    def __init__(self, real, states):
        self._real = real
        self._states = states
        self.backend = "postgres"
        self.dsn = DSN
        self.password = PASSWORD

    async def fetchall(self, sql, args=()):
        if sql == rollups.STATE_SQL:
            return list(self._states)
        return await self._real.fetchall(sql, args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _env(tmp_path, states=()):
    env = Env(tmp_path)
    env.store.storage = FakeStorage(env.store.storage, states)
    return env


def test_the_pages_are_static_and_carry_no_token_or_data(tmp_path):
    env = _env(tmp_path)
    try:
        env.user("root", admin=True)
        token = env.login("root").json()["csrf"]
        for route, script, ident in (("/admin/retention", "admin-retention.js", "retention-form"),
                                     ("/admin/storage", "admin-storage.js", "levels")):
            r = env.client.get(route)
            assert r.status_code == 200 and "text/html" in r.headers["content-type"]
            assert f'src="/static/{script}"' in r.text and f'id="{ident}"' in r.text
            assert token not in r.text and "csrf" not in r.text.lower()
            assert "Content-Security-Policy" in r.headers
            assert "postgres" not in r.text and "db.internal" not in r.text
    finally:
        env.client.close()
        env.store.close()


def test_the_settings_documents_are_refused_without_an_admin_session(tmp_path):
    env = _env(tmp_path)
    try:
        for path in (RETENTION, STORAGE):
            assert env.client.get(path).status_code == 401
        env.user("bob")
        env.login("bob")
        for path in (RETENTION, STORAGE):
            assert env.client.get(path).status_code == 403
    finally:
        env.client.close()
        env.store.close()


def test_the_storage_status_has_the_rollup_state_and_never_the_dsn(tmp_path):
    states = [
        ("1h", 1_700_000_000.0, 42, ""),
        ("compaction", 1_700_003_600.0, 7, "OperationalError: <b>disk</b> full"),
    ]
    env = _env(tmp_path, states)
    try:
        env.user("root", admin=True)
        env.login("root")
        r = env.client.get(STORAGE)
        assert r.status_code == 200
        body = r.json()
        assert body["backend"] == "postgres"
        by_level = {row["level"]: row for row in body["levels"]}
        assert by_level["1h"]["last_rows"] == 42
        assert by_level["compaction"]["last_error"] == "OperationalError: <b>disk</b> full"
        text = json.dumps(body)
        assert DSN not in text and PASSWORD not in text and "db.internal" not in text
    finally:
        env.client.close()
        env.store.close()


def test_the_storage_status_is_empty_when_nothing_has_run(tmp_path):
    env = _env(tmp_path)
    try:
        env.user("root", admin=True)
        env.login("root")
        assert env.client.get(STORAGE).json()["levels"] == []
    finally:
        env.client.close()
        env.store.close()


def test_overrides_are_in_the_retention_document(tmp_path):
    env = _env(tmp_path)
    try:
        env.user("root", admin=True)
        hdr = env.csrf(env.login("root"))
        r = env.client.put("/api/admin/retention", headers=hdr,
                           json={"overrides": {"temp": {"raw_days": 2}}})
        assert r.status_code == 200
        doc = env.client.get(RETENTION).json()
        assert doc["settings"]["overrides"] == {"temp": {"raw_days": 2}}
        assert doc["override_fields"] and doc["bounds"]["raw_days"]["default"]
    finally:
        env.client.close()
        env.store.close()


async def test_a_real_run_leaves_a_compaction_row_with_its_error(tmp_path):
    env = Env(tmp_path)
    try:
        await env.store.note_maintenance(5)
        await env.store.note_maintenance(0, "X" * 500)
        rows = await env.store.storage.fetchall(rollups.STATE_SQL)
        assert rows[0][0] == "compaction" and rows[0][2] == 0
        assert len(rows[0][3]) == rollups.MAX_ERROR_CHARS
    finally:
        env.client.close()
        env.store.close()


def test_the_navigation_links_are_for_admins_only():
    for href in ("/admin/retention", "/admin/storage", "/admin/tiers", "/admin/rules",
                 "/admin/recheck"):
        rows = [r for r in _nav_table() if r["href"] == href]
        assert len(rows) == 1 and rows[0]["admin"] is True and rows[0]["workspace"] == "admin", href
