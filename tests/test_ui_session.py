"""The shell draws the nav before the session round trip, sends an expired session to sign in,
and the sign-in page explains failures and only returns to a path on this site."""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "observe" / "static"


def test_the_nav_is_drawn_at_once_from_the_last_known_role():
    shell = (STATIC / "js" / "shell.js").read_text(encoding="utf-8")
    first = shell.index("renderNav(nav, visibleItems(cachedAdmin(), null)")
    # The session comes from the shared whoami() read (one request per page, round 2 R11).
    assert first < shell.index("whoami()")
    assert 'read("/api/v2/session")' not in shell
    assert "sessionStorage" in shell


def test_an_expired_session_goes_to_sign_in_and_comes_back():
    shell = (STATIC / "js" / "shell.js").read_text(encoding="utf-8")
    assert "r.status === 401" in shell and "toLogin()" in shell
    assert "/login?next=${encodeURIComponent(back)}" in shell


def test_sign_in_explains_failures_and_only_follows_local_paths():
    login = (STATIC / "login.js").read_text(encoding="utf-8")
    assert "locked for 15 minutes" in login and "Too many attempts" in login
    assert 'startsWith("//")' in login and "fromCharCode(92)" in login
    assert "window.location.assign(safeNext())" in login


def test_every_page_offers_sign_out_through_the_shell():
    """Bug plan WP8: Sign out was only on /admin. The shared header draws it for a signed-in
    visitor on every page, posting /api/logout with the session's CSRF token."""
    shell = (STATIC / "js" / "shell.js").read_text(encoding="utf-8")
    assert '"Sign out"' in shell and '"/api/logout"' in shell
    assert 'id="logout"' not in (STATIC / "admin.html").read_text(encoding="utf-8")
