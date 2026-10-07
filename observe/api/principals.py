"""Who is calling /api/v2 and what role they read with (docs/DATA-API-DESIGN.md section 4.7).

Three kinds of caller exist. A browser holds a login session (cookie): an admin user reads as
`admin`, any other user as `viewer`. A script holds a read token (`wpr_`, bearer): it reads as
`viewer` or `operator`, never `admin`. When `server.anonymous_read` is on, a caller with
neither reads as `anonymous`, which is below `viewer`. Ingest (`wpi_`), field (`wpf_`) and
control (`wpc_`) keys are not accepted. Basic auth is not accepted either.

A lookup is remembered for `server.api_auth_cache_s` seconds, so an unchanged page can be
answered with a 304 without a database read. A revoked session or token therefore stops working
within that time, and logout forgets the session at once. A session's `last_seen` is written at
most once a minute and never by the 304 path.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from .. import auth as authmod
from ..config import Config
from ..ingest.keys import verify_read_token
from ..store import Store
from .problems import ApiProblem

ROLES = ("anonymous", "viewer", "operator", "admin")
RANK = {name: i for i, name in enumerate(ROLES)}
TOUCH_EVERY_S = 60.0
MAX_CACHED = 1024


@dataclass(frozen=True)
class Principal:
    kind: str  # "session", "token" or "anonymous"
    role: str
    name: str
    key: str  # the rate limit key
    csrf: str | None = None  # a session's CSRF token

    @property
    def rank(self) -> int:
        return RANK[self.role]


ANONYMOUS = Principal("anonymous", "anonymous", "anonymous", "")


def _digest(kind: str, secret: str) -> str:
    return hashlib.sha256(f"{kind}:{secret}".encode("utf-8")).hexdigest()


class Authenticator:
    def __init__(self, config: Config, store: Store, wall: Callable[[], float],
                 mono: Callable[[], float]) -> None:
        self._config = config
        self._store = store
        self._wall = wall
        self._mono = mono
        # digest -> [principal, valid until (monotonic), session token or None,
        #            session absolute expiry (wall), last touch (wall)]
        self._seen: dict[str, list] = {}

    def forget_session(self, token: str) -> None:
        self._seen.pop(_digest("c", token), None)

    def _remember(self, digest: str, principal: Principal, token: str | None, expires: float,
                  touched: float) -> None:
        ttl = self._config.server.api_auth_cache_s
        if ttl <= 0:
            return
        now = self._mono()
        if len(self._seen) >= MAX_CACHED:
            self._seen = {k: v for k, v in self._seen.items() if v[1] > now}
            if len(self._seen) >= MAX_CACHED:
                self._seen.clear()
        self._seen[digest] = [principal, now + ttl, token, expires, touched]

    async def _cached(self, digest: str) -> Principal | None:
        entry = self._seen.get(digest)
        if entry is None:
            return None
        principal, until, token, expires, touched = entry
        wall = self._wall()
        if self._mono() >= until or (token is not None and wall >= expires):
            del self._seen[digest]
            return None
        if token is not None and wall - touched >= TOUCH_EVERY_S:
            entry[4] = wall
            await authmod.touch_session(self._store, token, wall)
        return principal

    async def authenticate(self, request: Request) -> Principal | None:
        """The caller, or None when there is no credential and anonymous reads are off. A bearer
        value that is not a valid read token is refused, never read as anonymous."""
        header = request.headers.get("authorization", "")
        if header[:7].lower() == "bearer ":
            secret = header[7:].strip()
            digest = _digest("b", secret)
            found = await self._cached(digest)
            if found is not None:
                return found
            token = await verify_read_token(self._store, secret, self._wall())
            if token is None:
                raise ApiProblem(401, "the bearer token is not a valid read token",
                                 headers={"WWW-Authenticate": "Bearer"})
            principal = Principal("token", token.role, token.label, f"t:{token.prefix}")
            self._remember(digest, principal, None, float("inf"), 0.0)
            return principal
        cookie = request.cookies.get(authmod.COOKIE)
        if cookie:
            digest = _digest("c", cookie)
            found = await self._cached(digest)
            if found is not None:
                return found
            live = await authmod.peek_session(self._store, self._config, cookie, self._wall())
            if live is not None:
                sess, expires, last_seen = live
                principal = Principal("session", "admin" if sess.is_admin else "viewer",
                                      sess.username, f"s:{sess.user_id}", sess.csrf)
                wall = self._wall()
                touched = last_seen
                if wall - last_seen >= TOUCH_EVERY_S:
                    await authmod.touch_session(self._store, cookie, wall)
                    touched = wall
                self._remember(digest, principal, cookie, expires, touched)
                return principal
        return ANONYMOUS if self._config.server.anonymous_read else None


def csrf_ok(request: Request, principal: Principal) -> bool:
    """A session needs the CSRF header on an unsafe method; a token does not."""
    if principal.kind != "session" or principal.csrf is None:
        return True
    sent = request.headers.get(authmod.CSRF_HEADER, "")
    return hmac.compare_digest(sent.encode("utf-8"), principal.csrf.encode("utf-8"))
