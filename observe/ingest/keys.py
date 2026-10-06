"""Host-bound ingest keys.

A key is created for exactly one host name and is valid only for ingest; no
other surface accepts it. The plaintext is returned once, at creation, and is
never stored. Only a SHA-256 digest of the secret part is kept. The secret is
256 random bits, so a fast digest is adequate here, unlike a user password.
Verification looks the key up by its public prefix, compares digests in
constant time, and requires the bound host to equal the host the caller is
reporting for. Revocation is a timestamp, so the row stays for review.

Key format: wpi_<12 hex prefix>_<43 character url-safe secret>. The wpi marker
means host ingest scope and lets a leaked key be recognised in a scan.

A plugin may own another scope, such as wpf for Pockethernet field reports. The
scope is the marker of the key and is also stored in the row, and verification
requires both to equal the scope the caller asks for. A key of one scope is
therefore refused by every surface of another scope, whatever its host or
device label says. The host column holds the host name for wpi keys and the
device label for any other scope.

Modeled on hostwatch's key handling (hostwatch, same owner), with the host
binding and revocation record added.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass

from ..storage import IntegrityConflict
from ..store import Store
from .schema import MAX_NAME

MARKER = "wpi"
_SCOPE = re.compile(r"^[a-z]{3,8}$")
PREFIX_BYTES = 6
SECRET_BYTES = 32
_DUMMY_DIGEST = hashlib.sha256(b"observe-no-such-key").hexdigest()


class IngestKeyError(ValueError):
    """Raised for an invalid host name."""


@dataclass(frozen=True)
class KeyInfo:
    prefix: str
    host: str
    created: float
    created_by: str
    revoked_at: float | None
    last_used: float | None
    scope: str = MARKER

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _split(key: str, scope: str = MARKER) -> tuple[str, str] | None:
    parts = key.split("_", 2)  # the url-safe secret may itself contain underscores
    if len(parts) != 3 or parts[0] != scope or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def _check_host(host: str) -> str:
    if not host or len(host) > MAX_NAME or any(c.isspace() or ord(c) < 32 for c in host):
        raise IngestKeyError("host must be 1 to 128 characters with no whitespace or control characters")
    return host


async def create_key(store: Store, host: str, created_by: str = "",
                     scope: str = MARKER) -> tuple[str, KeyInfo]:
    """Create a key bound to host. Returns (plaintext, info); the plaintext is not recoverable.

    For a scope other than wpi, host is the device label. Whether the scope belongs to a
    loaded plugin is checked by the caller; here it only has to be well formed.
    """
    host = _check_host(host)
    if not isinstance(scope, str) or not _SCOPE.match(scope):
        raise IngestKeyError("scope must be 3 to 8 lower-case letters")
    now = time.time()
    for _ in range(5):
        prefix = secrets.token_hex(PREFIX_BYTES)
        secret = secrets.token_urlsafe(SECRET_BYTES)
        try:
            await store.execute(
                "INSERT INTO ingest_keys (prefix, hash, host, created, created_by, scope) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (prefix, _digest(secret), host, now, created_by, scope))
        except IntegrityConflict:
            continue  # prefix collision, vanishingly rare; draw again
        return (f"{scope}_{prefix}_{secret}",
                KeyInfo(prefix, host, now, created_by, None, None, scope))
    raise RuntimeError("could not allocate a unique key prefix")


async def revoke_key(store: Store, prefix: str, now: float | None = None) -> bool:
    """Revoke by prefix. Returns False if the prefix is unknown or already revoked."""
    ts = time.time() if now is None else now
    rows = await store.execute(
        "UPDATE ingest_keys SET revoked_at = ? WHERE prefix = ? AND revoked_at IS NULL "
        "RETURNING id", (ts, prefix))
    return bool(rows)


async def list_keys(store: Store) -> list[KeyInfo]:
    rows = await store.fetch(
        "SELECT prefix, host, created, created_by, revoked_at, last_used, scope "
        "FROM ingest_keys ORDER BY id")
    return [KeyInfo(*r) for r in rows]


async def verify_key(store: Store, key: str, host: str, now: float | None = None,
                     scope: str = MARKER) -> bool:
    """True only for an unrevoked key of this scope bound to exactly this host or device.

    Records last use on success.
    """
    parts = _split(key, scope) if isinstance(key, str) else None
    if parts is None:
        return False
    prefix, secret = parts
    rows = await store.fetch(
        "SELECT hash, host, revoked_at, scope FROM ingest_keys WHERE prefix = ?", (prefix,))
    stored, bound, revoked, row_scope = rows[0] if rows else (_DUMMY_DIGEST, None, 1.0, "")
    digest_ok = hmac.compare_digest(stored, _digest(secret))
    host_ok = bound is not None and hmac.compare_digest(
        bound.encode("utf-8"), host.encode("utf-8"))
    if not (digest_ok and host_ok and revoked is None and row_scope == scope):
        return False
    await store.execute("UPDATE ingest_keys SET last_used = ? WHERE prefix = ?",
                     (time.time() if now is None else now, prefix))
    return True


async def key_host(store: Store, key: str, scope: str = MARKER) -> tuple[str, str] | None:
    """Return (prefix, bound host) for an unrevoked key of this scope whose secret matches.

    Used by the ingest endpoint to tell a bad key (401) from a good key bound to
    another host (403). It does not record use; verify_key does that.
    """
    parts = _split(key, scope) if isinstance(key, str) else None
    if parts is None:
        return None
    prefix, secret = parts
    rows = await store.fetch(
        "SELECT hash, host, revoked_at, scope FROM ingest_keys WHERE prefix = ?", (prefix,))
    stored, bound, revoked, row_scope = rows[0] if rows else (_DUMMY_DIGEST, None, 1.0, "")
    if (hmac.compare_digest(stored, _digest(secret)) and bound is not None
            and revoked is None and row_scope == scope):
        return prefix, bound
    return None
