"""The audit log: who did what, from where, and whether it worked.

Rows are appended to the `audit` table and are never updated by the
application; the only deletion is retention pruning in Store.prune. Every
writer goes through record(), which sanitizes the request path and redacts any
detail field whose name suggests a secret, so a careless caller cannot put a
password, session token or ingest key into the log. Callers still must not pass
secrets on purpose; the redaction is a second line, not the first.

The path sanitizer is adapted from hostwatch's sanitize_audit_path (hostwatch,
same owner): control characters, including ones decoded from percent-encoded
input, become "?" and the length is capped, so a hostile path cannot forge log
lines or bloat a row.

Kinds written today: login_ok, login_failed, login_error, logout,
user_created, user_create_failed, user_create_error, key_created,
key_create_failed, key_revoked, key_revoke_failed, user_disabled,
user_enabled, user_promoted, user_demoted, user_change_failed (refused or
unknown user), infra_switch_linked, infra_switch_link_failed, infra_depends_accepted,
infra_depends_rejected, infra_depends_failed, ingest_denied,
ingest_failed, enrol_created, enrol_create_failed, enrol_fetched, enrol_fetch_failed,
enrol_expired, enrol_reissued, host_allowlist_saved, host_task_created, host_task_fetched,
host_keys_revoked, host_removed (each with a matching _failed kind where it can be refused), api_denied (a refused /api/v2 request), plugin_request, plugin_denied, plugin_failed, and the control plugin's
control_requested, control_request_refused, control_pull and control_expired. A kind ending in _failed or _error is an action that stopped
partway or was refused after it started.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .store import Store

AUDIT_PATH_MAX = 256
MAX_LIST = 500
REDACTED = "[redacted]"
_SECRET_WORDS = ("password", "passwd", "secret", "token", "csrf", "cookie", "authorization",
                 "bearer", "api_key", "apikey", "ingest_key", "hash")


# An ingest key ("wpi_<prefix>_<secret>", even a truncated one), a read token (wpr_), a field key (wpf_), a control key (wpc_), an
# enrolment token (wpe_), an install step key (wps_), or any long run of URL-safe characters, which is what a session
# token or a key secret looks like.
_SECRET_SHAPES = re.compile(r"wp[icsetfr]_[A-Za-z0-9_-]*|[A-Za-z0-9_-]{40,}")


def redact_secrets(text: str) -> str:
    """Replace anything that looks like an ingest key or session token with REDACTED."""
    return _SECRET_SHAPES.sub(REDACTED, text)


def clean_note(note: str, limit: int) -> str:
    """A free-text report note: secret-shaped text redacted, unprintable characters dropped,
    capped at `limit`. Redaction runs before the cap so a secret cannot survive by being cut."""
    return "".join(c for c in redact_secrets(str(note)) if c.isprintable())[:limit]


def sanitize_audit_path(path: str) -> str:
    """Secret-shaped text is redacted, control characters become "?", and the result is capped
    at AUDIT_PATH_MAX. Redaction runs before the cap so a secret cannot survive by being cut."""
    cleaned = "".join("?" if (ord(c) < 32 or 0x7F <= ord(c) <= 0x9F)
                      else c for c in redact_secrets(str(path)))
    return cleaned[:AUDIT_PATH_MAX]


def _redact(detail: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        name = str(key).lower()
        if any(w in name for w in _SECRET_WORDS):
            out[str(key)] = REDACTED
        else:
            out[str(key)] = redact_secrets(value) if isinstance(value, str) else value
    return out


async def record(store: Store, kind: str, actor: str = "", method: str = "", path: str = "",
                 status: int = 0, remote: str = "",
                 detail: dict[str, Any] | None = None) -> None:
    """Append one audit row with a sanitized path and redacted detail."""
    await store.write_audit(kind, actor=actor, method=method, path=sanitize_audit_path(path),
                            status=status, remote=remote, detail=_redact(detail))


async def list_rows(store: Store, limit: int = 100, kind: str | None = None,
                    before_id: int | None = None) -> list[dict[str, Any]]:
    """Newest rows first. `before_id` pages backwards: only rows with a smaller id."""
    limit = max(1, min(int(limit), MAX_LIST))
    where, args = [], []
    if kind:
        where.append("kind=?")
        args.append(kind)
    if before_id is not None:
        where.append("id<?")
        args.append(int(before_id))
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    rows = await store.fetch(
        "SELECT id, ts, actor, kind, method, path, status, remote, detail FROM audit"
        f"{clause} ORDER BY id DESC LIMIT ?", (*args, limit))
    out = []
    for r in rows:
        try:
            detail = json.loads(r[8])
        except ValueError:
            detail = {}
        out.append({"id": r[0], "ts": r[1], "actor": r[2], "kind": r[3], "method": r[4],
                    "path": r[5], "status": r[6], "remote": r[7], "detail": detail})
    return out
