"""Host-bound ingest keys.

A key is created for exactly one host name and is valid only for ingest; no
other surface accepts it. The plaintext is returned once, at creation, and is
never stored. Only a SHA-256 digest of the secret part is kept. The secret is
256 random bits, so a fast digest is adequate here, unlike a user password.
Verification looks the key up by its public prefix, compares digests in
constant time, and requires the bound host to equal the host the caller is
reporting for. Revocation is a timestamp, so the row stays for review.

Key format: wpi_<12 hex prefix>_<43 character url-safe secret>. The wpi marker
means ingest scope and lets a leaked key be recognised in a scan.

Modeled on hostwatch's key handling (hostwatch, same owner), with the host
binding and revocation record added.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass

from ..store import Store
from .schema import MAX_NAME

MARKER = "wpi"
PREFIX_BYTES = 6
SECRET_BYTES = 32
_DUMMY_DIGEST = hashlib.sha256(b"watchpost-no-such-key").hexdigest()


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

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _split(key: str) -> tuple[str, str] | None:
    parts = key.split("_", 2)  # the url-safe secret may itself contain underscores
    if len(parts) != 3 or parts[0] != MARKER or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def _check_host(host: str) -> str:
    if not host or len(host) > MAX_NAME or any(c.isspace() or ord(c) < 32 for c in host):
        raise IngestKeyError("host must be 1 to 128 characters with no whitespace or control characters")
    return host


async def create_key(store: Store, host: str, created_by: str = "") -> tuple[str, KeyInfo]:
    """Create a key bound to host. Returns (plaintext, info); the plaintext is not recoverable."""
    host = _check_host(host)
    now = time.time()
    for _ in range(5):
        prefix = secrets.token_hex(PREFIX_BYTES)
        secret = secrets.token_urlsafe(SECRET_BYTES)
        try:
            await store._run(
                "INSERT INTO ingest_keys (prefix, hash, host, created, created_by) "
                "VALUES (?, ?, ?, ?, ?)", (prefix, _digest(secret), host, now, created_by))
        except sqlite3.IntegrityError:
            continue  # prefix collision, vanishingly rare; draw again
        return f"{MARKER}_{prefix}_{secret}", KeyInfo(prefix, host, now, created_by, None, None)
    raise RuntimeError("could not allocate a unique key prefix")


async def revoke_key(store: Store, prefix: str, now: float | None = None) -> bool:
    """Revoke by prefix. Returns False if the prefix is unknown or already revoked."""
    ts = time.time() if now is None else now
    rows = await store._run(
        "UPDATE ingest_keys SET revoked_at = ? WHERE prefix = ? AND revoked_at IS NULL "
        "RETURNING id", (ts, prefix))
    return bool(rows)


async def list_keys(store: Store) -> list[KeyInfo]:
    rows = await store._run(
        "SELECT prefix, host, created, created_by, revoked_at, last_used "
        "FROM ingest_keys ORDER BY id")
    return [KeyInfo(*r) for r in rows]


async def verify_key(store: Store, key: str, host: str, now: float | None = None) -> bool:
    """True only for an unrevoked key bound to exactly this host. Records last use on success."""
    parts = _split(key) if isinstance(key, str) else None
    if parts is None:
        return False
    prefix, secret = parts
    rows = await store._run(
        "SELECT hash, host, revoked_at FROM ingest_keys WHERE prefix = ?", (prefix,))
    stored, bound, revoked = rows[0] if rows else (_DUMMY_DIGEST, None, 1.0)
    digest_ok = hmac.compare_digest(stored, _digest(secret))
    host_ok = bound is not None and hmac.compare_digest(
        bound.encode("utf-8"), host.encode("utf-8"))
    if not (digest_ok and host_ok and revoked is None):
        return False
    await store._run("UPDATE ingest_keys SET last_used = ? WHERE prefix = ?",
                     (time.time() if now is None else now, prefix))
    return True
