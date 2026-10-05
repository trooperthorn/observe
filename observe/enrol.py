"""Host enrolment: the Add host API's state (docs/GUI-DESIGN.md section 3.10).

An admin creates a host with a platform and a choice of agent and control. Observe stores one
enrolment row for it and returns a single-use token, shown once. Only a SHA-256 digest of the
token is kept; the token is 256 random bits, so a fast digest is adequate, as for ingest keys.
The token is valid for 30 minutes and for one redemption. Redeeming it (the install script
fetch) is what mints the host-bound keys: a wpi key for the agent and a wpc key for control.
No key exists before then, so an unused or expired token leaves nothing to revoke.

Progress is derived, not stored twice. The enrolment row records when the script was fetched.
First data is the host's row in `hosts`, which only a batch from the agent creates. The first
control pull is the wpc key's last use, which only an authenticated pull records. The token and
the keys never go into the audit log or a log line. The audit rows name the host and the actor.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import audit
from .ingest.keys import create_key
from .store import Store

TOKEN_MARKER = "wpe"
TOKEN_TTL_S = 30 * 60
STEP_MARKER = "wps"
STEP_TTL_S = 2 * 3600  # a step key works this long after the script fetch
INSTALL_STEPS = ("root", "hostname", "observe_host", "rerun", "pool", "download", "agent", "compose", "app",
                 "control_account", "control_install", "control_config", "sudoers", "control_unit", "done")
STEP_STATUSES = ("ok", "failed", "skipped", "refused")
MAX_NOTE = 200
CONTROL_SCOPE = "wpc"
MAX_ALLOWLIST_ENTRIES = 32

PLATFORMS = {"linux": "Linux server", "truenas": "TrueNAS", "windows": "Windows",
             "raspberry-pi": "Raspberry Pi"}

_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# A TrueNAS pool name: letters, digits, dots, dashes and underscores, starting with a letter or digit.
POOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_POOL = "Apps"
# Same shapes the control daemon accepts (docs/CONTROL.md). None can hold a space, quote, slash,
# semicolon, dollar sign, backtick or pipe, so an entry is safe inside a shell word or a TOML string.
_HEADER = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
# Matches hostwatch SERVICE_NAME (an optional docker: prefix, no @, no leading dash, no "..").
_SERVICE = re.compile(r"^(?:docker:)?[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class EnrolError(ValueError):
    """A request that cannot be accepted. `status` is the HTTP status to answer with."""

    def __init__(self, reason: str, status: int = 422) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Spec:
    name: str
    platform: str
    agent: bool
    control: bool
    fans: tuple[tuple[str, int | None], ...]  # (header, min_duty_limit or None)
    services: tuple[str, ...]
    reboot: bool

    def allowlist(self) -> dict[str, Any]:
        return {"fans": [{"header": h, **({"min_duty_limit": m} if m is not None else {})}
                         for h, m in self.fans],
                "services": list(self.services), "reboot": self.reboot}


def _services(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ALLOWLIST_ENTRIES:
        raise EnrolError(f"services must be a list of at most {MAX_ALLOWLIST_ENTRIES} entries")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not _SERVICE.fullmatch(item) or ".." in item:
            raise EnrolError("services has an entry with characters that are not allowed")
        if item not in out:
            out.append(item)
    return out


def _fans(raw: Any) -> list[tuple[str, int | None]]:
    """Each fan entry is a header name, or {"header": name, "min_duty_limit": 0..100}.

    min_duty_limit is the lowest floor a remote request may set for that header, which
    thermalctl needs per header (thermal-control-linux, docs/ARCHITECTURE.md).
    """
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ALLOWLIST_ENTRIES:
        raise EnrolError(f"fans must be a list of at most {MAX_ALLOWLIST_ENTRIES} entries")
    out: list[tuple[str, int | None]] = []
    for item in raw:
        limit: int | None = None
        if isinstance(item, dict):
            if set(item) - {"header", "min_duty_limit"}:
                raise EnrolError("a fan entry has only header and min_duty_limit")
            limit = item.get("min_duty_limit")
            item = item.get("header")
            if limit is not None and (type(limit) is not int or not 0 <= limit <= 100):
                raise EnrolError("min_duty_limit must be a whole number from 0 to 100")
        if not isinstance(item, str) or not _HEADER.fullmatch(item):
            raise EnrolError("fans has an entry with characters that are not allowed")
        if any(h == item for h, _ in out):
            continue
        out.append((item, limit))
    return out


def parse_spec(body: Any) -> Spec:
    """Validate a create request. Raises EnrolError with a message safe to show."""
    if not isinstance(body, dict):
        raise EnrolError("the request must be a JSON object")
    name = body.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise EnrolError("name must be 1 to 63 characters: lower-case letters, digits and "
                         "dashes, starting and ending with a letter or digit")
    platform = body.get("platform")
    if platform not in PLATFORMS:
        raise EnrolError("platform must be one of " + ", ".join(PLATFORMS))
    agent, control = body.get("agent", True), body.get("control", False)
    if type(agent) is not bool or type(control) is not bool:
        raise EnrolError("agent and control must be true or false")
    if not agent and not control:
        raise EnrolError("choose the agent, control or both")
    allow = body.get("allowlist")
    if allow is not None and not isinstance(allow, dict):
        raise EnrolError("allowlist must be an object")
    allow = allow or {}
    if set(allow) - {"fans", "services", "reboot"}:
        raise EnrolError("allowlist has only fans, services and reboot")
    fans = _fans(allow.get("fans"))
    services = _services(allow.get("services"))
    reboot = allow.get("reboot", False)
    if type(reboot) is not bool:
        raise EnrolError("reboot must be true or false")
    if control and platform == "windows":
        raise EnrolError("control is not available for Windows yet: it needs a Windows path "
                         "in thermal-control first. Enrol the agent only.")
    if not control and (fans or services or reboot):
        raise EnrolError("an allowlist needs control to be chosen")
    return Spec(name, platform, agent, control, tuple(fans), tuple(services), reboot)


def parse_pool(raw: Any, platform: str) -> str:
    """The TrueNAS pool chosen in the wizard: empty for any other platform or when unset (the
    script then uses the default pool). Raises EnrolError with a message safe to show."""
    if raw is None or raw == "":
        return ""
    if platform != "truenas":
        raise EnrolError("a pool applies to TrueNAS only")
    if not isinstance(raw, str) or not POOL.fullmatch(raw) or ".." in raw:
        raise EnrolError("pool has characters that are not allowed")
    return raw


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return f"{TOKEN_MARKER}_{secrets.token_urlsafe(32)}"


async def create_enrolment(store: Store, spec: Spec, created_by: str, now: float) -> str:
    """Store the enrolment and return the plaintext token, which is not recoverable.

    Raises EnrolError(409) when the name is already enrolled or already reporting.
    """
    if await store._run("SELECT 1 FROM hosts WHERE host=?", (spec.name,)):
        raise EnrolError("a host with this name already exists", 409)
    token = new_token()
    try:
        await store._run(
            "INSERT INTO enrolments (host, platform, agent, control, allowlist, token_hash, "
            "created, created_by, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (spec.name, spec.platform, int(spec.agent), int(spec.control),
             json.dumps(spec.allowlist(), sort_keys=True), _digest(token), now, created_by,
             now + TOKEN_TTL_S))
    except sqlite3.IntegrityError as err:
        raise EnrolError("a host with this name already exists", 409) from err
    return token


async def regenerate_enrolment(store: Store, host: str, now: float) -> tuple[str, Spec] | None:
    """Replace the token of an enrolment whose script has not been fetched, and return the new
    plaintext token with the stored choices. The old token stops working at once, because only
    the new digest is kept. None when there is no such enrolment, or when the script was already
    fetched (the keys exist then, and a second enrolment would need them revoked first).

    The conditional UPDATE means a fetch that wins the race leaves nothing to replace.
    """
    token = new_token()
    rows = await store._run(
        "UPDATE enrolments SET token_hash=?, created=?, expires_at=?, expiry_audited=0 "
        "WHERE host=? AND fetched_at IS NULL RETURNING platform, agent, control, allowlist",
        (_digest(token), now, now + TOKEN_TTL_S, host))
    if not rows:
        return None
    platform, agent, control, allowlist = rows[0]
    allow = json.loads(allowlist)
    fans = tuple((f["header"], f.get("min_duty_limit")) for f in allow.get("fans", []))
    spec = Spec(host, platform, bool(agent), bool(control), fans,
                tuple(allow.get("services", [])), bool(allow.get("reboot", False)))
    return token, spec


@dataclass(frozen=True)
class Redeemed:
    """What the install script fetch gets. The keys are plaintext and shown to the script once."""

    host: str
    platform: str
    agent_key: str | None
    control_key: str | None
    allowlist: dict[str, Any]
    step_key: str = ""


async def peek(store: Store, token: str, now: float) -> tuple[str, bool] | None:
    """(platform, control chosen) for a token that could still be redeemed, without spending it.

    The route uses this to refuse a platform it has no script for, or a control choice it cannot
    serve, before the token is burned. Anything else, including a malformed token, is None.
    """
    if not isinstance(token, str) or not token.startswith(TOKEN_MARKER + "_"):
        return None
    rows = await store._run(
        "SELECT platform, control FROM enrolments WHERE token_hash=? AND fetched_at IS NULL "
        "AND expires_at>?", (_digest(token), now))
    return (rows[0][0], bool(rows[0][1])) if rows else None


async def redeem(store: Store, token: str, now: float, remote: str = "") -> Redeemed | None:
    """Spend a token. None for an unknown, used or expired token (the caller answers 404).

    The row is claimed with one conditional UPDATE, so two fetches of the same token cannot
    both succeed. The keys are minted after the claim and bound to the host. Both outcomes are
    audited, with the host name and never the token.
    """
    path = "/i/[token]"
    if not isinstance(token, str) or not token.startswith(TOKEN_MARKER + "_"):
        await audit.record(store, "enrol_fetch_failed", method="GET", path=path, status=404,
                           remote=remote, detail={"reason": "not a token"})
        return None
    rows = await store._run(
        "UPDATE enrolments SET fetched_at=? WHERE token_hash=? AND fetched_at IS NULL "
        "AND expires_at>? RETURNING host, platform, agent, control, allowlist, created_by",
        (now, _digest(token), now))
    if not rows:
        await audit.record(store, "enrol_fetch_failed", method="GET", path=path, status=404,
                           remote=remote, detail={"reason": "unknown, used or expired token"})
        return None
    host, platform, agent, control, allowlist, created_by = rows[0]
    agent_key = control_key = None
    if agent:
        agent_key, info = await create_key(store, host, created_by=created_by)
        await store._run("UPDATE enrolments SET agent_prefix=? WHERE host=?",
                         (info.prefix, host))
    if control:
        control_key, info = await create_key(store, host, created_by=created_by,
                                             scope=CONTROL_SCOPE)
        await store._run("UPDATE enrolments SET control_prefix=? WHERE host=?",
                         (info.prefix, host))
    step_key = f"{STEP_MARKER}_{secrets.token_urlsafe(32)}"
    await store._run("UPDATE enrolments SET step_hash=? WHERE host=?", (_digest(step_key), host))
    await audit.record(store, "enrol_fetched", method="GET", path=path, status=200,
                       remote=remote, detail={"host": host, "platform": platform,
                                              "agent": bool(agent), "control": bool(control)})
    return Redeemed(host, platform, agent_key, control_key, json.loads(allowlist), step_key)


async def record_step(store: Store, step_key: str, step: str, status: str, note: str,
                      now: float) -> str | None:
    """Store one install step report. Returns the host, or None when the step key is not valid.

    The key is the redeemed token's step key: a digest match on a fetched enrolment, for two
    hours after the fetch. A report for the same step replaces the earlier one. The note is
    redacted of secret-shaped text and capped, so a key cannot be carried back by accident.
    """
    if not isinstance(step_key, str) or not step_key.startswith(STEP_MARKER + "_"):
        return None
    rows = await store._run(
        "SELECT host, reports FROM enrolments WHERE step_hash=? AND fetched_at IS NOT NULL "
        "AND fetched_at+?>?", (_digest(step_key), STEP_TTL_S, now))
    if not rows:
        return None
    host, raw = rows[0]
    note = "".join(c for c in audit.redact_secrets(note) if c.isprintable())[:MAX_NOTE]
    reports = [r for r in json.loads(raw) if r["step"] != step]
    reports.append({"step": step, "status": status, "note": note, "at": now})
    await store._run("UPDATE enrolments SET reports=? WHERE host=?", (json.dumps(reports), host))
    return host


async def claim_expiry_audit(store: Store, host: str, now: float) -> bool:
    """True exactly once for an enrolment whose token expired unused, so the audit row is single."""
    rows = await store._run(
        "UPDATE enrolments SET expiry_audited=1 WHERE host=? AND fetched_at IS NULL "
        "AND expires_at<=? AND expiry_audited=0 RETURNING host", (host, now))
    return bool(rows)


_STEP_LABELS = {"script": "Script fetched", "data": "First data received",
                "control": "Control first pull", "ready": "Ready"}


async def progress(store: Store, host: str, now: float) -> dict[str, Any] | None:
    """The enrolment's state machine, or None for a host with no enrolment.

    Steps are script, data, control and ready. Each is done, waiting, skipped (not chosen),
    or expired (the script was never fetched before the token ran out). `state` is the last
    step reached: waiting, script_fetched, first_data, control_pulled, ready or expired.
    """
    rows = await store._run(
        "SELECT platform, agent, control, created, expires_at, fetched_at, control_prefix, reports "
        "FROM enrolments WHERE host=?", (host,))
    if not rows:
        return None
    platform, agent, control, created, expires_at, fetched_at, control_prefix, reports = rows[0]
    first = await store._run("SELECT first_seen FROM hosts WHERE host=?", (host,))
    data_at = first[0][0] if fetched_at is not None and first else None
    pulled_at = None
    if fetched_at is not None and control_prefix:
        used = await store._run("SELECT last_used FROM ingest_keys WHERE prefix=?",
                                (control_prefix,))
        pulled_at = used[0][0] if used else None
    expired = fetched_at is None and now >= expires_at

    def step(sid: str, chosen: bool, at: float | None) -> dict[str, Any]:
        if not chosen:
            status = "skipped"
        elif at is not None:
            status = "done"
        else:
            status = "expired" if expired and sid == "script" else "waiting"
        return {"id": sid, "label": _STEP_LABELS[sid], "status": status, "at": at}

    steps = [step("script", True, fetched_at), step("data", bool(agent), data_at),
             step("control", bool(control), pulled_at)]
    ready = all(s["status"] in ("done", "skipped") for s in steps)
    steps.append({"id": "ready", "label": _STEP_LABELS["ready"],
                  "status": "done" if ready else "waiting",
                  "at": max((s["at"] for s in steps if s["at"] is not None), default=None)
                  if ready else None})
    if expired:
        state = "expired"
    elif ready:
        state = "ready"
    elif pulled_at is not None:
        state = "control_pulled"
    elif data_at is not None:
        state = "first_data"
    elif fetched_at is not None:
        state = "script_fetched"
    else:
        state = "waiting"
    return {"host": host, "platform": platform, "agent": bool(agent), "control": bool(control),
            "state": state, "ready": ready, "expired": expired, "created": created,
            "expires_at": expires_at, "steps": steps,
            "install": [{"step": r["step"], "status": r["status"], "note": r["note"],
                         "at": r["at"]} for r in json.loads(reports)]}


def command_text(host: str, platform: str, base_url: str, token: str, pool: str = "") -> str:
    """The one-liner, headed with the host name and platform so it cannot be run on the wrong
    machine by mistake. The header is a comment in both shells. `base_url` is the origin the
    console was reached at."""
    label = PLATFORMS[platform]
    head = (f"# Observe install for {host} ({label}). Run this on {host} only. "
            f"The token expires in {TOKEN_TTL_S // 60} minutes and works once.")
    url = f"{base_url.rstrip('/')}/i/{token}"
    if pool:
        url += f"?pool={parse_pool(pool, platform)}"
    if platform == "windows":
        line = f"irm '{url}' | iex"
    else:
        line = f"curl -fsSL '{url}' | sudo sh"
    return f"{head}\n{line}"
