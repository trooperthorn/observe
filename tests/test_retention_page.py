"""The admin retention page: admin only, CSRF token in the form, the rollup_state rows from the
storage, the backend name and never the DSN or its password."""

from __future__ import annotations

import html
import re

from observe.storage import rollups

from .test_auth import Env
from .test_ui_shell import _nav_table

DSN = "postgresql://observe:hunter2secret@db.internal:5432/observe"
PASSWORD = "hunter2secret"


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


def test_the_page_renders_for_an_admin_with_the_csrf_token_in_the_form(tmp_path):
    env = _env(tmp_path)
    try:
        env.user("root", admin=True)
        token = env.login("root").json()["csrf"]
        r = env.client.get("/admin/retention")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        form = re.search(r'<form id="retention-form".*?</form>', r.text, re.S).group(0)
        assert f'name="csrf" value="{token}"' in form
        assert f'<meta name="csrf-token" content="{token}">' in r.text
        assert 'name="raw_days"' in form and 'name="metric"' in form
        assert "Content-Security-Policy" in r.headers
    finally:
        env.client.close()
        env.store.close()


def test_the_page_is_refused_without_an_admin_session(tmp_path):
    env = _env(tmp_path)
    try:
        assert env.client.get("/admin/retention").status_code == 401
        env.user("bob")
        env.login("bob")
        assert env.client.get("/admin/retention").status_code == 403
    finally:
        env.client.close()
        env.store.close()


def test_the_page_shows_the_rollup_state_and_never_the_dsn(tmp_path):
    states = [
        ("1h", 1_700_000_000.0, 42, ""),
        ("compaction", 1_700_003_600.0, 7, "OperationalError: <b>disk</b> full"),
    ]
    env = _env(tmp_path, states)
    try:
        env.user("root", admin=True)
        env.login("root")
        text = env.client.get("/admin/retention").text
        assert "2023-11-14 22:13:20 UTC" in text and "<td>42</td>" in text
        assert "OperationalError: &lt;b&gt;disk&lt;/b&gt; full" in text
        assert "<b>disk</b>" not in text
        assert '<strong id="backend">postgres</strong>' in text
        assert DSN not in html.unescape(text) and PASSWORD not in text
        assert "db.internal" not in text
    finally:
        env.client.close()
        env.store.close()


def test_the_page_says_so_when_nothing_has_run(tmp_path):
    env = _env(tmp_path)
    try:
        env.user("root", admin=True)
        env.login("root")
        assert "No compaction has run yet." in env.client.get("/admin/retention").text
    finally:
        env.client.close()
        env.store.close()


def test_overrides_are_listed_in_the_form(tmp_path):
    env = _env(tmp_path)
    try:
        env.user("root", admin=True)
        hdr = env.csrf(env.login("root"))
        r = env.client.put("/api/admin/retention", headers=hdr,
                           json={"overrides": {"temp": {"raw_days": 2}}})
        assert r.status_code == 200
        text = env.client.get("/admin/retention").text
        assert 'name="metric" type="text" maxlength="64" aria-label="Metric name for override 1" value="temp"' in text
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


def test_the_navigation_link_is_for_admins_only():
    rows = [r for r in _nav_table() if r["href"] == "/admin/retention"]
    assert len(rows) == 1 and rows[0]["admin"] is True and rows[0]["workspace"] == "admin"
