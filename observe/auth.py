"""Logins, server-side sessions, CSRF tokens, and the admin role.

Passwords are stored only as Argon2id hashes. A login creates a session whose
identifier is 256 random bits; only its SHA-256 digest is stored, and the
browser holds the identifier in an HttpOnly, Secure, SameSite=Strict cookie.
The CSRF token is not stored in a form the client can replay from the
database: it is an HMAC of the session identifier, so it exists only for
someone who holds the session, and it is checked in constant time.

The optional basic auth in observe/web.py is a separate, weaker credential
for the read-only API and /metrics. Nothing in this module accepts it, so it
can never reach a route that requires a session, which includes every admin,
ingest-key, user and future action route. Adapted from hostwatch/auth.py
(hostwatch, same owner): the dummy-hash burn on unknown users, the lockout
policy and the rehash-on-login step.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request

from .config import Config
from .store import Store

COOKIE = "observe_session"
CSRF_HEADER = "x-csrf-token"
MIN_PASSWORD = 12
MAX_PASSWORD = 256
MAX_USERNAME = 64
_CSRF_CONTEXT = b"observe-csrf-v1"


class AuthError(ValueError):
    """Raised for an invalid username or password when creating a user."""


def make_hasher(cfg: Config) -> PasswordHasher:
    s = cfg.server
    return PasswordHasher(time_cost=s.argon2_time_cost, memory_cost=s.argon2_memory_kib,
                          parallelism=s.argon2_parallelism)


def hash_password(cfg: Config, password: str) -> str:
    return make_hasher(cfg).hash(password)


def verify_password(cfg: Config, stored: str, password: str) -> bool:
    """True only for a matching password. A malformed stored hash is a mismatch."""
    try:
        return make_hasher(cfg).verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


@lru_cache(maxsize=8)
def _dummy_hash(t: int, m: int, p: int) -> str:
    return PasswordHasher(time_cost=t, memory_cost=m, parallelism=p).hash("observe-dummy")


def _burn(cfg: Config, password: str) -> None:
    """One verification against a dummy hash, so a failure costs the same whether or
    not the account exists."""
    s = cfg.server
    verify_password(cfg, _dummy_hash(s.argon2_time_cost, s.argon2_memory_kib,
                                     s.argon2_parallelism), password)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_for(token: str) -> str:
    """The CSRF token for a session token: an HMAC keyed by the session secret."""
    return hmac.new(token.encode("utf-8"), _CSRF_CONTEXT, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Session:
    user_id: int
    username: str
    is_admin: bool
    csrf: str


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    reason: str  # "ok", "bad_credentials", "locked" or "disabled"
    user_id: int | None = None
    username: str = ""
    is_admin: bool = False


def _check_new(username: str, password: str) -> str:
    username = username.strip()
    if not username or len(username) > MAX_USERNAME or any(
            c.isspace() or ord(c) < 32 for c in username):
        raise AuthError("username must be 1 to 64 characters with no whitespace or control characters")
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise AuthError(f"password must be {MIN_PASSWORD} to {MAX_PASSWORD} characters")
    return username


async def create_user(store: Store, cfg: Config, username: str, password: str,
                      is_admin: bool = False, now: float | None = None) -> int:
    """Create a user. Raises AuthError for bad input or a name already in use."""
    username = _check_new(username, password)
    stored = hash_password(cfg, password)
    try:
        rows = await store.execute(
            "INSERT INTO users (username, hash, is_admin, created) VALUES (?,?,?,?) RETURNING id",
            (username, stored, int(is_admin), time.time() if now is None else now))
    except Exception as err:  # IntegrityConflict on the UNIQUE username
        if "UNIQUE" in str(err):
            raise AuthError("that username already exists") from err
        raise
    return int(rows[0][0])


async def list_users(store: Store) -> list[dict[str, Any]]:
    rows = await store.fetch(
        "SELECT id, username, is_admin, disabled, created FROM users ORDER BY id")
    return [{"id": r[0], "username": r[1], "is_admin": bool(r[2]), "disabled": bool(r[3]),
             "created": r[4]} for r in rows]


async def count_admins(store: Store) -> int:
    rows = await store.fetch("SELECT COUNT(*) FROM users WHERE is_admin=1 AND disabled=0")
    return int(rows[0][0])


async def set_user_flag(store: Store, user_id: int, column: str, value: bool) -> str:
    """Set `disabled` or `is_admin` on a user. Returns "ok", "missing" or "last_admin".

    One statement does the check and the change, so two concurrent requests cannot
    both remove the last active admin. A disabled user's sessions stop working at
    once because load_session reads the flag on every request."""
    if column not in ("disabled", "is_admin"):
        raise ValueError(column)
    rows = await store.fetch("SELECT id FROM users WHERE id=?", (user_id,))
    if not rows:
        return "missing"
    removes_admin = (column == "disabled" and value) or (column == "is_admin" and not value)
    guard = (" AND NOT (is_admin=1 AND disabled=0 AND "
             "(SELECT COUNT(*) FROM users WHERE is_admin=1 AND disabled=0) <= 1)"
             if removes_admin else "")
    done = await store.execute(
        f"UPDATE users SET {column}=? WHERE id=?{guard} RETURNING id", (int(value), user_id))
    return "ok" if done else "last_admin"


async def check_login(store: Store, cfg: Config, username: str, password: str,
                      now: float | None = None) -> LoginResult:
    """Check credentials and apply the lockout policy. An unknown, locked or disabled
    account burns one dummy verification so timing does not reveal it. A correct
    password on a locked account is refused and does not unlock it."""
    now = time.time() if now is None else now
    if len(password) > MAX_PASSWORD or len(username) > MAX_USERNAME:
        _burn(cfg, "x")
        return LoginResult(False, "bad_credentials")
    rows = await store.fetch(
        "SELECT id, hash, is_admin, disabled, failed_count, locked_until FROM users "
        "WHERE username=?", (username,))
    if not rows:
        _burn(cfg, password)
        return LoginResult(False, "bad_credentials")
    uid, stored, is_admin, disabled, failed, locked_until = rows[0]
    if locked_until is not None and locked_until > now:
        _burn(cfg, password)
        return LoginResult(False, "locked", uid, username)
    if disabled:
        _burn(cfg, password)
        return LoginResult(False, "disabled", uid, username)
    if not verify_password(cfg, stored, password):
        failed += 1
        lock = now + cfg.server.login_lock_s if failed >= cfg.server.login_max_failures else None
        await store.execute(
            "UPDATE users SET failed_count=?, locked_until=? WHERE id=?",
            (0 if lock else failed, lock, uid))
        return LoginResult(False, "locked" if lock else "bad_credentials", uid, username)
    await store.execute("UPDATE users SET failed_count=0, locked_until=NULL WHERE id=?", (uid,))
    if make_hasher(cfg).check_needs_rehash(stored):
        await store.execute("UPDATE users SET hash=? WHERE id=?", (hash_password(cfg, password), uid))
    return LoginResult(True, "ok", uid, username, bool(is_admin))


async def create_session(store: Store, cfg: Config, user_id: int,
                         now: float | None = None) -> tuple[str, str]:
    """Create a session. Returns (cookie token, csrf token). Only digests are stored."""
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    csrf = csrf_for(token)
    await store.execute(
        "INSERT INTO sessions (id_hash, user_id, csrf_hash, created, expires, last_seen) "
        "VALUES (?,?,?,?,?,?)",
        (_digest(token), user_id, _digest(csrf), now, now + cfg.server.session_absolute_s, now))
    return token, csrf


async def load_session(store: Store, cfg: Config, token: str | None,
                       now: float | None = None) -> Session | None:
    """The live session for a cookie token, or None when it is unknown, revoked, past its
    absolute expiry, idle too long, or belongs to a disabled user. Touches last_seen."""
    if not token or len(token) > 128:
        return None
    now = time.time() if now is None else now
    rows = await store.fetch(
        "SELECT s.expires, s.last_seen, s.revoked, u.id, u.username, u.is_admin, u.disabled "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id_hash=?", (_digest(token),))
    if not rows:
        return None
    expires, last_seen, revoked, uid, username, is_admin, disabled = rows[0]
    if revoked or disabled or expires <= now or now - last_seen > cfg.server.session_idle_s:
        await store.execute("UPDATE sessions SET revoked=1 WHERE id_hash=?", (_digest(token),))
        return None
    await store.execute("UPDATE sessions SET last_seen=? WHERE id_hash=?", (now, _digest(token)))
    return Session(uid, username, bool(is_admin), csrf_for(token))


async def peek_session(store: Store, cfg: Config, token: str | None,
                       now: float | None = None) -> tuple[Session, float, float] | None:
    """Like load_session, but it writes nothing: (session, absolute expiry, last_seen) or None.
    The /api/v2 reads use it so that a read never writes; they record last_seen themselves, at
    most once a minute (observe/api/auth.py)."""
    if not token or len(token) > 128:
        return None
    now = time.time() if now is None else now
    rows = await store.fetch(
        "SELECT s.expires, s.last_seen, s.revoked, u.id, u.username, u.is_admin, u.disabled "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id_hash=?", (_digest(token),))
    if not rows:
        return None
    expires, last_seen, revoked, uid, username, is_admin, disabled = rows[0]
    if revoked or disabled or expires <= now or now - last_seen > cfg.server.session_idle_s:
        return None
    return Session(uid, username, bool(is_admin), csrf_for(token)), float(expires), float(last_seen)


async def touch_session(store: Store, token: str, now: float) -> None:
    await store.execute("UPDATE sessions SET last_seen=? WHERE id_hash=?", (now, _digest(token)))


async def revoke_session(store: Store, token: str) -> None:
    await store.execute("UPDATE sessions SET revoked=1 WHERE id_hash=?", (_digest(token),))


@dataclass(frozen=True)
class Guards:
    """FastAPI dependencies. Each one needs a session cookie and ignores basic auth."""

    session: Callable[[Request], Awaitable[Session]]  # any logged-in user
    mutating: Callable[[Request], Awaitable[Session]]  # session plus CSRF token
    admin: Callable[[Request], Awaitable[Session]]  # admin session, read-only request
    admin_mutating: Callable[[Request], Awaitable[Session]]  # admin session plus CSRF


def build_guards(cfg: Config, store: Store, clock: Callable[[], float] = time.time) -> Guards:
    async def session(request: Request) -> Session:
        sess = await load_session(store, cfg, request.cookies.get(COOKIE), clock())
        if sess is None:
            # No WWW-Authenticate header: this surface never offers basic auth.
            raise HTTPException(401, "login required")
        request.state.session = sess
        return sess

    async def mutating(request: Request) -> Session:
        sess = await session(request)
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent.encode("utf-8"), sess.csrf.encode("utf-8")):
            raise HTTPException(403, "missing or invalid CSRF token")
        return sess

    async def admin(request: Request) -> Session:
        sess = await session(request)
        if not sess.is_admin:
            raise HTTPException(403, "admin role required")
        return sess

    async def admin_mutating(request: Request) -> Session:
        sess = await mutating(request)
        if not sess.is_admin:
            raise HTTPException(403, "admin role required")
        return sess

    return Guards(session, mutating, admin, admin_mutating)
