"""The command queue and results store (docs/CONTROL.md, "Limits and safety").

A command is written and signed in one transaction under the store's lock, so the per-host
`seq` never repeats or goes backwards and the rate limits cannot be raced past by two requests.
The signature covers the canonical command object, never the state, so a state change after
signing does not invalidate it.

States: `requested` (written), `pulled` (handed to the host's daemon), `scheduled` (the daemon
accepted a delayed action such as a reboot), and the final states `done`, `failed`, `refused`,
`cancelled` and `unknown`. `unknown` is what a command becomes when it expires with no result: it
is never shown as done, because Observe cannot tell whether the host acted. A scheduled
command has already been answered, so expiry does not apply to it.

Every function that touches the database runs in a worker thread, like the other plugin stores.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from observe import audit
from observe.plugins import Migration
from observe.storage import Conn
from observe.store import Store

from .signing import sign_command

ACTIONS = ("fan.set_floor", "fan.set_mode", "service.restart", "host.reboot", "agent.update")
UPDATE = "agent.update"
REBOOT = "host.reboot"
OPEN_STATES = ("requested", "pulled", "scheduled")
EXPIRABLE_STATES = ("requested", "pulled")  # a scheduled command has already been answered
SERVED_STATES = ("requested", "pulled")  # a scheduled command is never served again
CANCEL_STATES = ("requested", "scheduled")
CANCEL_LIST_WINDOW_S = 7 * 86400  # a cancelled-while-scheduled id stays listed until the host acknowledges it, at most this long
RESULT_STATES = ("done", "failed", "refused", "scheduled", "cancelled")
MAX_PARAMS_BYTES = 2048
MAX_OUTPUT_CHARS = 4096
MAX_HOST = 128

MIGRATIONS = (
    Migration(1, (
        """CREATE TABLE IF NOT EXISTS control_commands (
  id TEXT PRIMARY KEY,
  host TEXT NOT NULL,
  action TEXT NOT NULL,
  params TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  issued_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'requested',
  signature TEXT NOT NULL,
  pulled_at REAL,
  UNIQUE (host, seq)
)""",
        "CREATE INDEX IF NOT EXISTS control_commands_host_state ON control_commands "
        "(host, state, expires_at)",
        """CREATE TABLE IF NOT EXISTS control_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  command_id TEXT NOT NULL,
  host TEXT NOT NULL,
  state TEXT NOT NULL,
  output TEXT NOT NULL,
  output_truncated INTEGER NOT NULL DEFAULT 0,
  started_at REAL,
  finished_at REAL,
  duration_s REAL,
  received_at REAL NOT NULL,
  key_prefix TEXT NOT NULL
)""",
        "CREATE INDEX IF NOT EXISTS control_results_command ON control_results (command_id)",
    )),
    Migration(2, (
        "ALTER TABLE control_commands ADD COLUMN cancelled_from TEXT",
        "ALTER TABLE control_commands ADD COLUMN cancelled_at REAL",
    )),
)


class QueueError(Exception):
    """A request the queue refused. `status` is the HTTP status a route would use."""

    def __init__(self, reason: str, status: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Limits:
    max_pending_per_action: int = 1
    max_commands_per_host_per_hour: int = 10
    reboot_min_interval_s: int = 900
    command_ttl_s: int = 120


def _command_object(row: tuple[Any, ...]) -> dict[str, Any]:
    cid, host, action, params, by, issued, expires, seq = row
    return {"v": 1, "id": cid, "host": host, "action": action, "params": json.loads(params),
            "requested_by": by, "issued_at": issued, "expires_at": expires, "seq": seq}


_COLUMNS = "id, host, action, params, requested_by, issued_at, expires_at, seq"


def _expire(db: Conn, now: float) -> list[str]:
    """Mark unanswered expired commands unknown. Returns their ids. Runs inside a write unit."""
    marks = ",".join("?" * len(EXPIRABLE_STATES))
    ids = [r[0] for r in db.execute(
        f"SELECT id FROM control_commands WHERE state IN ({marks}) AND expires_at <= ?",
        (*EXPIRABLE_STATES, int(now))).fetchall()]
    if ids:
        db.execute(
            f"UPDATE control_commands SET state='unknown' WHERE state IN ({marks}) "
            "AND expires_at <= ?", (*EXPIRABLE_STATES, int(now)))
    return ids


async def _audit_expired(store: Store, ids: list[str]) -> None:
    for cid in ids:
        await audit.record(store, "control_expired", actor="Observe",
                           detail={"command_id": cid, "state": "unknown"})


async def expire_commands(store: Store, now: float) -> list[str]:
    """Turn commands that expired without a result into `unknown`, and audit each."""
    ids = await store.storage.write(lambda db: _expire(db, now))
    await _audit_expired(store, ids)
    return ids


def _check_request(host: str, action: str, params: Any, requested_by: str) -> str:
    if not isinstance(host, str) or not 1 <= len(host) <= MAX_HOST \
            or re.search(r"[\s\x00-\x1f\x7f]", host):
        raise QueueError("host must be 1 to 128 characters with no whitespace or control "
                         "characters")
    if action not in ACTIONS:
        raise QueueError(f"unknown action {action!r}")
    if not isinstance(params, dict) or not requested_by or len(requested_by) > 128:
        raise QueueError("params must be an object and requested_by must be a name")
    try:
        text = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False)
    except (TypeError, ValueError) as err:
        raise QueueError("params must be plain JSON without NaN or infinity") from err
    if len(text.encode("utf-8")) > MAX_PARAMS_BYTES:
        raise QueueError("params are too large")
    return text


def _enqueue(db: Conn, key: Ed25519PrivateKey, limits: Limits, host: str, action: str,
                  params_text: str, requested_by: str, now: float) -> tuple[dict[str, Any], str,
                                                                            list[str]]:
    issued = int(now)
    expired = _expire(db, now)
    marks = ",".join("?" * len(OPEN_STATES))
    pending = db.execute(
        f"SELECT COUNT(*) FROM control_commands WHERE host=? AND action=? "
        f"AND state IN ({marks})", (host, action, *OPEN_STATES)).fetchone()[0]
    if pending >= limits.max_pending_per_action:
        raise QueueError(f"{action} is already pending for this host", 429)
    hourly = db.execute("SELECT COUNT(*) FROM control_commands WHERE host=? AND issued_at>?",
                        (host, issued - 3600)).fetchone()[0]
    if hourly >= limits.max_commands_per_host_per_hour:
        raise QueueError("too many commands for this host in the last hour", 429)
    if action == REBOOT:
        recent = db.execute(
            "SELECT COUNT(*) FROM control_commands WHERE host=? AND action=? AND issued_at>?",
            (host, REBOOT, issued - limits.reboot_min_interval_s)).fetchone()[0]
        if recent:
            raise QueueError("a reboot was already requested for this host recently", 429)
    seq = (db.execute("SELECT MAX(seq) FROM control_commands WHERE host=?",
                      (host,)).fetchone()[0] or 0) + 1
    command = {"v": 1, "id": str(uuid.uuid4()), "host": host, "action": action,
               "params": json.loads(params_text), "requested_by": requested_by,
               "issued_at": issued, "expires_at": issued + limits.command_ttl_s, "seq": seq}
    signature = sign_command(key, command)
    db.execute(
        "INSERT INTO control_commands (id, host, action, params, requested_by, issued_at, "
        "expires_at, seq, state, signature) VALUES (?,?,?,?,?,?,?,?,'requested',?)",
        (command["id"], host, action, params_text, requested_by, issued,
         command["expires_at"], seq, signature))
    return command, signature, expired


async def enqueue_command(store: Store, key: Ed25519PrivateKey, limits: Limits, host: str,
                          action: str, params: dict[str, Any], requested_by: str,
                          now: float) -> dict[str, Any]:
    """Write and sign one command, or raise QueueError (429 for a rate limit). Audited.

    The confirmation step (and the typed host name for a reboot) belongs to the caller; this is
    the part that must hold whatever the caller is: seq, expiry, signature and the rate limits.
    """
    try:
        params_text = _check_request(host, action, params, requested_by)
        command, signature, expired = await store.storage.write(
            lambda db: _enqueue(db, key, limits, host, action, params_text, requested_by, now))
    except QueueError as err:
        await audit.record(store, "control_request_refused", actor=requested_by, status=err.status,
                           detail={"host": host, "action": action, "reason": err.reason})
        raise
    await _audit_expired(store, expired)
    await audit.record(store, "control_requested", actor=requested_by,
                       detail={"command_id": command["id"], "host": host, "action": action,
                               "params": params, "seq": command["seq"],
                               "expires_at": command["expires_at"]})
    return {"command": command, "signature": signature}


def _pull(db: Conn, host: str, now: float) -> tuple[list[dict[str, Any]], list[str],
                                                             list[str], list[str]]:
    expired = _expire(db, now)
    marks = ",".join("?" * len(SERVED_STATES))
    rows = db.execute(
        f"SELECT {_COLUMNS}, signature, state FROM control_commands WHERE host=? "
        f"AND state IN ({marks}) AND expires_at>? ORDER BY seq",
        (host, *SERVED_STATES, int(now))).fetchall()
    fresh = [r[0] for r in rows if r[9] == "requested"]
    for cid in fresh:
        db.execute("UPDATE control_commands SET state='pulled', pulled_at=? "
                          "WHERE id=? AND state='requested'", (now, cid))
    cancel = [r[0] for r in db.execute(
        "SELECT id FROM control_commands WHERE host=? AND state='cancelled' "
        "AND cancelled_from='scheduled' AND cancelled_at>? AND NOT EXISTS (SELECT 1 FROM "
        "control_results r WHERE r.command_id=control_commands.id AND r.state='cancelled') "
        "ORDER BY seq",
        (host, now - CANCEL_LIST_WINDOW_S)).fetchall()]
    return ([{"command": _command_object(r[:8]), "signature": r[8]} for r in rows], fresh,
            expired, cancel)


async def pull_commands(store: Store, host: str, key_prefix: str, remote: str,
                        now: float) -> dict[str, Any]:
    """The host's unexpired commands that are requested or pulled, in seq order, plus the ids
    of its commands an admin cancelled while they were scheduled.

    A scheduled command is not served again. The cancel list keeps an id until the host posts
    a `cancelled` result for it, for at most seven days.

    Only this host's rows are ever selected. A first delivery is audited (`control_pull`), so a
    5 second poll with an empty queue writes nothing.
    """
    items, fresh, expired, cancel = await store.storage.write(
        lambda db: _pull(db, host, now))
    await _audit_expired(store, expired)
    if fresh:
        await audit.record(store, "control_pull", actor=key_prefix, method="GET",
                           path="/api/v1/control/commands", status=200, remote=remote,
                           detail={"host": host, "delivered": len(fresh), "command_ids": fresh})
    return {"commands": items, "cancel": cancel}


_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\Z)", re.DOTALL)
_AUTH_HEADER = re.compile(r"\bauthorization\s*:[^\r\n]*", re.IGNORECASE)
_KEYS = re.compile(r"\b(?:wp[icf]|hw)_[A-Za-z0-9_-]*|\bbearer\s+\S+", re.IGNORECASE)
_ASSIGNED = re.compile(r"((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S+",
                       re.IGNORECASE)
_LONG_RUN = re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def clean_output(text: str) -> tuple[str, bool]:
    """Redact secret-shaped text, drop control characters, and cap the length.

    Redaction runs before the cap so a secret cannot survive by being cut in half.
    """
    red = _AUTH_HEADER.sub(audit.REDACTED, _PEM.sub(audit.REDACTED, text))
    red = _ASSIGNED.sub(lambda m: m.group(1) + audit.REDACTED, _KEYS.sub(audit.REDACTED, red))
    redacted = _LONG_RUN.sub(audit.REDACTED, audit.redact_secrets(red))
    cleaned = _CONTROL.sub("?", redacted)
    return cleaned[:MAX_OUTPUT_CHARS], len(cleaned) > MAX_OUTPUT_CHARS


def _timing(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value < 0 or value > 4_102_444_800:
        raise QueueError(f"{name} must be a time in seconds")
    return float(value)


def _result(db: Conn, host: str, key_prefix: str, command_id: str, state: str,
                 output: str, started: float | None, finished: float | None,
                 now: float) -> tuple[bool, list[str]]:
    expired = _expire(db, now)
    row = db.execute(
        "SELECT state, action, cancelled_from FROM control_commands WHERE id=? AND host=?",
        (command_id, host)).fetchone()
    if row is None:
        # An unknown id and another host's id look the same, so ids cannot be probed.
        raise QueueError("no such command for this host", 404)
    # A reboot cancelled while scheduled: the host acknowledges the cancel, or reports the
    # truth if the cancel came too late (done or failed). Anything else is refused.
    after_cancel = row[0] == "cancelled" and row[2] == "scheduled"
    if state == "cancelled" and not after_cancel:
        raise QueueError("only a reboot cancelled while scheduled can be reported cancelled", 409)
    if after_cancel and state not in ("cancelled", "done", "failed"):
        raise QueueError("the command is already cancelled", 409)
    if row[0] == "unknown":
        raise QueueError("the command expired before a result arrived", 409)
    if row[0] == "requested":
        raise QueueError("the command has not been pulled by this host", 409)
    if row[0] not in OPEN_STATES and not after_cancel:
        raise QueueError(f"the command is already {row[0]}", 409)
    if state == "scheduled" and row[0] == "scheduled":
        raise QueueError("the command is already scheduled", 409)
    if state == "scheduled" and row[1] != REBOOT:
        raise QueueError("only host.reboot can be scheduled", 422)
    text, truncated = clean_output(output)
    duration = finished - started if started is not None and finished is not None else None
    db.execute(
        "INSERT INTO control_results (command_id, host, state, output, output_truncated, "
        "started_at, finished_at, duration_s, received_at, key_prefix) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (command_id, host, state, text, int(truncated), started, finished, duration, now,
         key_prefix))
    if state != "cancelled":
        db.execute("UPDATE control_commands SET state=? WHERE id=?", (state, command_id))
    return truncated, expired


async def record_result(store: Store, host: str, key_prefix: str, command_id: str, state: str,
                        output: str, started_at: Any, finished_at: Any,
                        now: float) -> dict[str, Any]:
    """Record a host's outcome for one of its own commands, or raise QueueError.

    404 for an unknown id or another host's id (the same answer for both), 409 for a command
    that was never pulled, is already final or expired without a result, and 422 for
    `scheduled` on anything but host.reboot. An expired command stays `unknown`.
    """
    if state not in RESULT_STATES:
        raise QueueError(f"state must be one of {', '.join(RESULT_STATES)}", 422)
    if not isinstance(output, str):
        raise QueueError("output must be text", 422)
    try:
        started = _timing(started_at, "started_at")
        finished = _timing(finished_at, "finished_at")
        if started is not None and finished is not None and finished < started:
            raise QueueError("finished_at is before started_at")
    except QueueError as err:
        err.status = 422
        raise
    try:
        truncated, expired = await store.storage.write(
            lambda db: _result(db, host, key_prefix, command_id, state, output, started,
                               finished, now))
    except QueueError as err:
        await _audit_expired(store, await store.storage.write(lambda db: _expire(db, now)))
        raise err
    await _audit_expired(store, expired)
    return {"id": command_id, "state": state, "truncated": truncated}


def _cancel(db: Conn, command_id: str, now: float) -> tuple[str, str, list[str]]:
    """Returns (host, refusal reason or empty, ids expired). Raises nothing inside the
    transaction, so the expiry it wrote is kept even when the cancel is refused."""
    expired = _expire(db, now)
    row = db.execute("SELECT host, action, state FROM control_commands WHERE id=?",
                            (command_id,)).fetchone()
    if row is None:
        return "", "no such command", expired
    # One guarded UPDATE, so a result that lands at the same moment cannot be overwritten.
    # Only a reboot can be scheduled, so the state alone decides.
    changed = db.execute(
        "UPDATE control_commands SET state='cancelled', cancelled_from=state, "
        "cancelled_at=? WHERE id=? AND state IN ('requested','scheduled')",
        (now, command_id)).rowcount
    if not changed:
        return row[0], (f"the command is {row[2]}, and only a requested or scheduled one "
                        "can be cancelled"), expired
    return row[0], "", expired


async def cancel_command(store: Store, command_id: str, requested_by: str,
                         now: float) -> dict[str, Any]:
    """Cancel a requested or scheduled command, or raise QueueError (404 unknown, 409 other).

    A command cancelled while scheduled appears in the pull `cancel` list. Audited.
    """
    host, reason, expired = await store.storage.write(
        lambda db: _cancel(db, command_id, now))
    await _audit_expired(store, expired)
    if reason:
        status = 404 if reason == "no such command" else 409
        await audit.record(store, "control_cancel_refused", actor=requested_by, status=status,
                           detail={"command_id": command_id[:64], "reason": reason})
        raise QueueError(reason, status)
    await audit.record(store, "control_cancelled", actor=requested_by,
                       detail={"command_id": command_id, "host": host})
    return {"id": command_id, "host": host, "state": "cancelled"}
