"""Host settings tasks: the short update and cleanup commands (docs/GUI-DESIGN.md section 3.11).

A task is a one-time command for one enrolled host, shown in the console and run by the admin on
the machine. Like an install token it is 256 random bits shown once, kept only as a SHA-256
digest, valid for 30 minutes and for one fetch. There are two kinds:

- `update` rewrites control.toml and the sudoers rules on a Linux or Raspberry Pi host after the
  allowlist was saved, and restarts the control service. It carries no key, and it does not
  touch the keys, so nothing needs revoking.
- `cleanup` undoes an install on the machine that holds it, for example one that was run on the
  wrong machine. It carries no key either. It removes only an install made for the named host.

Fetching a task script does not mint keys. The fetch mints a step key (wps_, a digest is kept,
two hours) so the script can report its progress like an install script does. Progress is
derived from those reports and from the control key's last use.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from . import audit
from .enrol import PLATFORMS, STEP_MARKER, STEP_TTL_S, TOKEN_TTL_S, MAX_NOTE
from .storage import Conn, series
from .store import Store

TASK_MARKER = "wpt"
KINDS = ("update", "cleanup")
# A platform that has a script for each kind. Control, and so an update, exists only on Linux
# and Raspberry Pi. A cleanup exists for every platform that has an install script.
UPDATE_PLATFORMS = ("linux", "raspberry-pi")
CLEANUP_PLATFORMS = ("linux", "raspberry-pi", "truenas", "windows")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_token() -> str:
    return f"{TASK_MARKER}_{secrets.token_urlsafe(32)}"


@dataclass(frozen=True)
class RedeemedTask:
    host: str
    kind: str
    platform: str
    allowlist: dict[str, Any]
    rev: int
    step_key: str


async def create_task(store: Store, host: str, kind: str, platform: str,
                      allowlist: dict[str, Any], rev: int, created_by: str, now: float) -> str:
    """Store a task and return its plaintext token. An earlier task of the same kind for the
    host that was not fetched is removed in the same transaction, so only the newest command
    works and the older token is dead."""
    token = new_token()

    def work(db: Conn) -> None:
        db.execute("DELETE FROM host_tasks WHERE host=? AND kind=? AND fetched_at IS NULL",
                   (host, kind))
        db.execute(
            "INSERT INTO host_tasks (host, kind, platform, allowlist, rev, token_hash, created, "
            "created_by, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (host, kind, platform, json.dumps(allowlist, sort_keys=True), rev,
             _digest(token), now, created_by, now + TOKEN_TTL_S))

    await store.storage.write(work, touches=("admin",))
    return token


async def peek(store: Store, token: str,
               now: float) -> tuple[str, str, str, dict[str, Any]] | None:
    """(kind, platform, host, allowlist) for a token that could still be fetched, without
    spending it, so the route can render a dry run before the token is burned."""
    if not isinstance(token, str) or not token.startswith(TASK_MARKER + "_"):
        return None
    rows = await store.fetch(
        "SELECT kind, platform, host, allowlist FROM host_tasks WHERE token_hash=? "
        "AND fetched_at IS NULL AND expires_at>?", (_digest(token), now))
    if not rows:
        return None
    kind, platform, host, allowlist = rows[0]
    return kind, platform, host, json.loads(allowlist)


async def redeem(store: Store, token: str, now: float, remote: str = "") -> RedeemedTask | None:
    """Spend a task token. None for an unknown, used or expired token (the caller answers 410).
    One conditional UPDATE claims it, so two fetches cannot both succeed. The audit row names
    the host and the kind, never the token."""
    path = "/t/[token]"
    if not isinstance(token, str) or not token.startswith(TASK_MARKER + "_"):
        await audit.record(store, "host_task_fetch_failed", method="GET", path=path, status=404,
                           remote=remote, detail={"reason": "not a task token"})
        return None
    step_key = f"{STEP_MARKER}_{secrets.token_urlsafe(32)}"
    rows = await store.execute(
        "UPDATE host_tasks SET fetched_at=?, step_hash=? WHERE token_hash=? AND fetched_at IS NULL "
        "AND expires_at>? RETURNING host, kind, platform, allowlist, rev",
        (now, _digest(step_key), _digest(token), now))
    if not rows:
        await audit.record(store, "host_task_fetch_failed", method="GET", path=path, status=404,
                           remote=remote, detail={"reason": "unknown, used or expired token"})
        return None
    host, kind, platform, allowlist, rev = rows[0]
    await audit.record(store, "host_task_fetched", method="GET", path=path, status=200,
                       remote=remote, detail={"host": host, "kind": kind, "platform": platform})
    return RedeemedTask(host, kind, platform, json.loads(allowlist), rev, step_key)


async def record_step(store: Store, step_key: str, step: str, status: str, note: str,
                      now: float) -> str | None:
    """Store one report from a task script. Returns the host, or None when the step key is not
    valid (a digest match on a fetched task, for two hours after the fetch). The note is
    redacted and capped, as for install reports."""
    if not isinstance(step_key, str) or not step_key.startswith(STEP_MARKER + "_"):
        return None
    rows = await store.fetch(
        "SELECT id, host, reports FROM host_tasks WHERE step_hash=? AND fetched_at IS NOT NULL "
        "AND fetched_at+?>?", (_digest(step_key), STEP_TTL_S, now))
    if not rows:
        return None
    task_id, host, raw = rows[0]
    note = "".join(c for c in audit.redact_secrets(note) if c.isprintable())[:MAX_NOTE]
    reports = [r for r in json.loads(raw) if r["step"] != step]
    reports.append({"step": step, "status": status, "note": note, "at": now})
    await store.execute("UPDATE host_tasks SET reports=? WHERE id=?", (json.dumps(reports), task_id))
    return str(host)


async def claim_expiry_audit(store: Store, task_id: int, now: float) -> bool:
    rows = await store.execute(
        "UPDATE host_tasks SET expiry_audited=1 WHERE id=? AND fetched_at IS NULL "
        "AND expires_at<=? AND expiry_audited=0 RETURNING id", (task_id, now))
    return bool(rows)


def _state(fetched_at: float | None, expires_at: float, reports: list[dict[str, Any]],
           now: float) -> str:
    if fetched_at is None:
        return "expired" if now >= expires_at else "waiting"
    if any(r["status"] in ("failed", "refused") for r in reports):
        return "failed"
    if any(r["step"] == "done" and r["status"] == "ok" for r in reports):
        return "done"
    return "fetched"


async def latest(store: Store, host: str, now: float) -> dict[str, Any] | None:
    """The newest task of the host with its state: waiting (command shown, not run), fetched
    (the script is running), done, failed (a step failed or was refused) or expired."""
    rows = await store.fetch(
        "SELECT id, kind, platform, rev, created, expires_at, fetched_at, reports "
        "FROM host_tasks WHERE host=? ORDER BY id DESC LIMIT 1", (host,))
    if not rows:
        return None
    task_id, kind, platform, rev, created, expires_at, fetched_at, raw = rows[0]
    reports = json.loads(raw)
    return {"id": task_id, "kind": kind, "platform": platform, "rev": rev, "created": created,
            "expires_at": expires_at, "fetched_at": fetched_at,
            "state": _state(fetched_at, expires_at, reports, now),
            "install": [{"step": r["step"], "status": r["status"], "note": r["note"],
                         "at": r["at"]} for r in reports]}


async def allowlist_status(store: Store, host: str) -> dict[str, Any] | None:
    """Whether the host has picked up the saved allowlist. None for a host with no enrolment.

    The state is `none` when control was not chosen, `pending` while the saved allowlist has not
    reached the host (the update command was not run, or the install was not run yet), `written`
    the file is on the host and the service was restarted but the host has not pulled since, and
    `applied` after the control daemon's first pull with its key later than the restart. An
    update counts as written only when its script reported the restart and no step of that task
    failed, because the daemon reads control.toml only at start. `applied_at` is that pull.
    """
    rows = await store.fetch(
        "SELECT control, allowlist_rev, allowlist_saved_at, created, fetched_at, control_prefix, "
        "reissued_at, reports FROM enrolments WHERE host=?", (host,))
    if not rows:
        return None
    control, rev, saved_at, created, fetched_at, prefix, reissued_at, raw_reports = rows[0]
    install_failed = any(r["status"] in ("failed", "refused") for r in json.loads(raw_reports))
    saved = saved_at if saved_at is not None else created
    out: dict[str, Any] = {"state": "none", "rev": rev, "saved_at": saved_at,
                           "written_at": None, "applied_at": None}
    if not control:
        return out
    written: float | None = None
    # The install script writes the saved allowlist when it is fetched after the save. A fetch at
    # the very instant of the save is not counted: pending is the safe answer.
    if fetched_at is not None and not install_failed and (fetched_at > saved or (rev == 0 and fetched_at >= saved)):
        written = fetched_at
    if rev:
        tasks = await store.fetch(
            "SELECT reports FROM host_tasks WHERE host=? AND kind='update' AND rev=? "
            "AND fetched_at IS NOT NULL AND created>=? ORDER BY id DESC",
            (host, rev, reissued_at if reissued_at is not None else 0.0))
        for (raw,) in tasks:
            reports = json.loads(raw)
            # The daemon reads control.toml only when it starts, so the file counts as picked up
            # only when the script got as far as restarting the service, and never after any
            # failed or refused step. The restart report is the time the new file was loaded.
            if any(r["status"] in ("failed", "refused") for r in reports):
                continue
            for r in reports:
                if r["step"] == "control_unit" and r["status"] == "ok":
                    written = max(written or 0.0, r["at"])
    out["written_at"] = written
    pulled = None
    if prefix:
        used = await store.fetch("SELECT last_used FROM ingest_keys WHERE prefix=?", (prefix,))
        pulled = used[0][0] if used else None
    if written is None:
        out["state"] = "pending"
    elif pulled is not None and pulled >= written:
        out["state"] = "applied"
        out["applied_at"] = pulled
    else:
        out["state"] = "written"
    return out


def task_command_text(host: str, platform: str, kind: str, base_url: str, token: str) -> str:
    """The one-liner for a task, headed with the host name and platform so it is not run on the
    wrong machine. The header is a comment in both shells."""
    label = PLATFORMS[platform]
    minutes = TOKEN_TTL_S // 60
    if kind == "update":
        head = (f"# Observe settings update for {host} ({label}). Run this on {host} only. "
                f"The token expires in {minutes} minutes and works once.")
    else:
        head = (f"# Observe cleanup for {host} ({label}). Run this only on the machine that holds "
                f"the install made for {host}; it refuses any other machine. "
                f"The token expires in {minutes} minutes and works once.")
    url = f"{base_url.rstrip('/')}/t/{token}"
    if platform == "windows":
        line = f"irm '{url}' | iex"
    else:
        line = f"curl -fsSL '{url}' | sudo sh"
    return f"{head}\n{line}"


async def revoke_host_keys(store: Store, host: str, now: float) -> int:
    """Revoke every unrevoked agent and control key bound to the host. Returns how many."""
    rows = await store.execute(
        "UPDATE ingest_keys SET revoked_at=? WHERE host=? AND scope IN ('wpi', 'wpc') "
        "AND revoked_at IS NULL RETURNING id", (now, host))
    return len(rows)


async def remove_host(store: Store, host: str, now: float) -> dict[str, int] | None:
    """Remove a host from Observe in one transaction: revoke its keys, delete its enrolment, its
    tasks and its stored hardware data (the host row, samples, sources, events and batch ids).
    The audit log and the control command history are kept. Returns the counts, or None when
    nothing was known about the host."""
    def work(db: Conn) -> dict[str, int] | None:
        counts = {"keys": db.execute(
            "UPDATE ingest_keys SET revoked_at=? WHERE host=? AND scope IN ('wpi', 'wpc') "
            "AND revoked_at IS NULL", (now, host)).rowcount}
        for name, sql in (("enrolments", "DELETE FROM enrolments WHERE host=?"),
                          ("tasks", "DELETE FROM host_tasks WHERE host=?"),
                          ("hosts", "DELETE FROM hosts WHERE host=?"),
                          ("sources", "DELETE FROM host_sources WHERE host=?"),
                          ("events", "DELETE FROM host_events WHERE host=?"),
                          ("batches", "DELETE FROM ingest_batches WHERE host=?")):
            counts[name] = db.execute(sql, (host,)).rowcount
        counts["samples"] = series.remove_resource(
            db, "host", host, rollups=store.storage.incremental_rollups)
        return counts if (counts["enrolments"] or counts["hosts"] or counts["keys"]) else None

    return await store.storage.write(work, touches=("hosts", "metrics", "events", "admin"))
