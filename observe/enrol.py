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

Fetching the script and redeeming the token are two steps. GET /i/{token} serves the install
script without a key and without spending the token. The script runs its guards (root, the
machine's host name, not the Observe host) and only then calls the redeem endpoint, which spends
the token and returns the keys. A guard that refuses reports the reason with the token, through
`record_guard_failure`, and the token stays valid, so a command pasted on the wrong machine does
not cost the admin a new one.

The address in the command is never taken from the request's Host header, which the sender
controls. It is `server.public_url`, or an address an admin confirmed in the wizard and saved
(`set_public_url`), and never a loopback name.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any

from . import audit
from .config import normalise_public_url
from .ingest.keys import create_key
from .storage import Conn, IntegrityConflict
from .store import Store

TOKEN_MARKER = "wpe"
TOKEN_TTL_S = 30 * 60
STEP_MARKER = "wps"
STEP_TTL_S = 2 * 3600  # a step key works this long after the script fetch
INSTALL_STEPS = ("root", "hostname", "observe_host", "rerun", "pool", "download", "agent", "compose", "app",
                 "control_account", "control_install", "control_config", "sudoers", "control_unit", "done",
                 "cleanup_match", "cleanup_control", "cleanup_agent", "cleanup_files")
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
_HEADER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,31}$")
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
    # Whether agent.update may replace the agent container (the [update] table of control.toml,
    # docs/CONTROL.md). The control daemon's own self-update is never turned on from here.
    update: bool = False

    def allowlist(self) -> dict[str, Any]:
        return {"fans": [{"header": h, **({"min_duty_limit": m} if m is not None else {})}
                         for h, m in self.fans],
                "services": list(self.services), "reboot": self.reboot, "update": self.update}


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


def parse_allowlist(allow: Any) -> tuple[list[tuple[str, int | None]], list[str], bool, bool]:
    """Validate an allowlist object (fans, services, reboot, update). Raises EnrolError with a
    message safe to show. None is an empty allowlist. `update` is whether Observe may ask the
    host to update its agent container; it is false when absent."""
    if allow is not None and not isinstance(allow, dict):
        raise EnrolError("allowlist must be an object")
    allow = allow or {}
    if set(allow) - {"fans", "services", "reboot", "update"}:
        raise EnrolError("allowlist has only fans, services, reboot and update")
    fans = _fans(allow.get("fans"))
    services = _services(allow.get("services"))
    reboot = allow.get("reboot", False)
    if type(reboot) is not bool:
        raise EnrolError("reboot must be true or false")
    update = allow.get("update", False)
    if type(update) is not bool:
        raise EnrolError("update must be true or false")
    return fans, services, reboot, update


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
    fans, services, reboot, update = parse_allowlist(body.get("allowlist"))
    if control and platform == "windows":
        raise EnrolError("control is not available for Windows yet: it needs a Windows path "
                         "in thermal-control first. Enrol the agent only.")
    if not control and (fans or services or reboot or update):
        raise EnrolError("an allowlist needs control to be chosen")
    return Spec(name, platform, agent, control, tuple(fans), tuple(services), reboot, update)


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
    if await store.fetch("SELECT 1 FROM hosts WHERE host=?", (spec.name,)):
        raise EnrolError("a host with this name already exists", 409)
    token = new_token()
    try:
        await store.execute(
            "INSERT INTO enrolments (host, platform, agent, control, allowlist, token_hash, "
            "created, created_by, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (spec.name, spec.platform, int(spec.agent), int(spec.control),
             json.dumps(spec.allowlist(), sort_keys=True), _digest(token), now, created_by,
             now + TOKEN_TTL_S))
    except IntegrityConflict as err:
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
    rows = await store.execute(
        "UPDATE enrolments SET token_hash=?, created=?, expires_at=?, expiry_audited=0, "
        "guard_step=NULL, guard_reason=NULL, guard_at=NULL "
        "WHERE host=? AND fetched_at IS NULL RETURNING platform, agent, control, allowlist",
        (_digest(token), now, now + TOKEN_TTL_S, host))
    if not rows:
        return None
    platform, agent, control, allowlist = rows[0]
    return token, spec_from_row(host, platform, agent, control, allowlist)


async def reissue_enrolment(store: Store, host: str, now: float) -> tuple[str, Spec, int] | None:
    """Replace the install command of an enrolment even after its script was fetched. The
    token is replaced, every agent and control key bound to the host is revoked, and the fetch,
    the step key, the reports and the key prefixes are cleared, all in one transaction, so the
    old command, the old keys and the old install's step reports are dead at once. Returns the
    new plaintext token, the stored choices and the number of keys revoked, or None when the host
    has no enrolment. The saved allowlist is kept, and goes into the new script.
    """
    token = new_token()

    def work(db: Conn) -> tuple[tuple[Any, ...], int] | None:
        row = db.execute(
            "SELECT platform, agent, control, allowlist FROM enrolments WHERE host=?",
            (host,)).fetchone()
        if row is None:
            return None
        revoked = db.execute(
            "UPDATE ingest_keys SET revoked_at=? WHERE host=? AND scope IN ('wpi', ?) "
            "AND revoked_at IS NULL", (now, host, CONTROL_SCOPE)).rowcount
        db.execute(
            "UPDATE enrolments SET token_hash=?, created=?, expires_at=?, expiry_audited=0, "
            "fetched_at=NULL, step_hash=NULL, reports='[]', agent_prefix=NULL, "
            "control_prefix=NULL, reissued_at=?, guard_step=NULL, guard_reason=NULL, "
            "guard_at=NULL WHERE host=?",
            (_digest(token), now, now + TOKEN_TTL_S, now, host))
        # An update command made for the old install is dead in the same transaction.
        db.execute("DELETE FROM host_tasks WHERE host=? AND fetched_at IS NULL", (host,))
        return row, revoked

    made = await store.storage.write(work, touches=("admin",))
    if made is None:
        return None
    (platform, agent, control, allowlist), revoked = made
    return token, spec_from_row(host, platform, agent, control, allowlist), revoked


def spec_from_row(host: str, platform: str, agent: Any, control: Any, allowlist: str) -> Spec:
    allow = json.loads(allowlist)
    fans = tuple((f["header"], f.get("min_duty_limit")) for f in allow.get("fans", []))
    return Spec(host, platform, bool(agent), bool(control), fans,
                tuple(allow.get("services", [])), bool(allow.get("reboot", False)),
                bool(allow.get("update", False)))


@dataclass(frozen=True)
class Redeemed:
    """What the install script fetch gets. The keys are plaintext and shown to the script once."""

    host: str
    platform: str
    agent_key: str | None
    control_key: str | None
    allowlist: dict[str, Any]
    step_key: str = ""
    # When set, the script is served without keys and posts this token to the redeem endpoint
    # after its guards pass. The key fields then only say which keys the install wants.
    redeem_token: str = ""


async def peek(store: Store, token: str, now: float) -> tuple[str, bool] | None:
    """(platform, control chosen) for a token that could still be redeemed, without spending it.

    The route uses this to refuse a platform it has no script for, or a control choice it cannot
    serve, before the token is burned. Anything else, including a malformed token, is None.
    """
    if not isinstance(token, str) or not token.startswith(TOKEN_MARKER + "_"):
        return None
    rows = await store.fetch(
        "SELECT platform, control FROM enrolments WHERE token_hash=? AND fetched_at IS NULL "
        "AND expires_at>?", (_digest(token), now))
    return (rows[0][0], bool(rows[0][1])) if rows else None


async def preview(store: Store, token: str, now: float) -> Redeemed | None:
    """What the install script is built from, without spending the token or making a key.

    The key fields hold placeholders that only say which keys the install wants: the script
    fetches the real keys from the redeem endpoint once its guards pass. None for a malformed,
    unknown, used or expired token.
    """
    if not isinstance(token, str) or not token.startswith(TOKEN_MARKER + "_"):
        return None
    rows = await store.fetch(
        "SELECT host, platform, agent, control, allowlist FROM enrolments WHERE token_hash=? "
        "AND fetched_at IS NULL AND expires_at>?", (_digest(token), now))
    if not rows:
        return None
    host, platform, agent, control, allowlist = rows[0]
    placeholder = "x" * 20
    return Redeemed(host, platform, "wpi_" + placeholder if agent else None,
                    "wpc_" + placeholder if control else None, json.loads(allowlist),
                    "wps_" + placeholder, redeem_token=token)


async def redeem(store: Store, token: str, now: float, remote: str = "") -> Redeemed | None:
    """Spend a token. None for an unknown, used or expired token (the caller answers 404).

    The row is claimed with one conditional UPDATE, so two fetches of the same token cannot
    both succeed. The keys are minted after the claim and bound to the host. Both outcomes are
    audited, with the host name and never the token.
    """
    path = "/api/enrol/redeem"
    if not isinstance(token, str) or not token.startswith(TOKEN_MARKER + "_"):
        await audit.record(store, "enrol_fetch_failed", method="GET", path=path, status=404,
                           remote=remote, detail={"reason": "not a token"})
        return None
    rows = await store.execute(
        "UPDATE enrolments SET fetched_at=?, guard_step=NULL, guard_reason=NULL, guard_at=NULL "
        "WHERE token_hash=? AND fetched_at IS NULL "
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
        await store.execute("UPDATE enrolments SET agent_prefix=? WHERE host=?",
                         (info.prefix, host))
    if control:
        control_key, info = await create_key(store, host, created_by=created_by,
                                             scope=CONTROL_SCOPE)
        await store.execute("UPDATE enrolments SET control_prefix=? WHERE host=?",
                         (info.prefix, host))
    step_key = f"{STEP_MARKER}_{secrets.token_urlsafe(32)}"
    await store.execute("UPDATE enrolments SET step_hash=? WHERE host=?", (_digest(step_key), host))
    await audit.record(store, "enrol_fetched", method="GET", path=path, status=200,
                       remote=remote, detail={"host": host, "platform": platform,
                                              "agent": bool(agent), "control": bool(control)})
    return Redeemed(host, platform, agent_key, control_key, json.loads(allowlist), step_key)


GUARD_REASONS = {"root": "the command was not run as root or an administrator",
                 "hostname": "the host name does not match",
                 "observe_host": "it was run on the Observe host itself"}
_FOUND = re.compile(r"[^A-Za-z0-9._-]")


async def record_guard_failure(store: Store, token: str, step: str, found: str,
                               now: float) -> str | None:
    """Keep why the script refused to run, for the wizard and the settings page. Returns the host,
    or None when the token is not a live one (unknown, used or expired) or the step is not a guard.

    The token is not spent: the script was refused before it asked for keys, and the same
    command works on the right machine. `found` is the name the machine gave itself. It is cut
    to the characters a host name has and to 64 of them, so nothing but a name is stored. Only the
    newest refusal is kept.
    """
    if step not in GUARD_REASONS or not isinstance(token, str) \
            or not token.startswith(TOKEN_MARKER + "_"):
        return None
    found = _FOUND.sub("", found if isinstance(found, str) else "")[:64]
    rows = await store.fetch(
        "SELECT host FROM enrolments WHERE token_hash=? AND fetched_at IS NULL AND expires_at>?",
        (_digest(token), now))
    if not rows:
        return None
    host = rows[0][0]
    reason = (f"ran on {found}, expected {host}" if step == "hostname" and found
              else f"{GUARD_REASONS[step]} (expected {host})")
    await store.execute(
        "UPDATE enrolments SET guard_step=?, guard_reason=?, guard_at=? WHERE host=? "
        "AND fetched_at IS NULL", (step, reason, now, host))
    return host


async def get_public_url(store: Store, configured: str | None) -> tuple[str, str]:
    """(address, source) for install commands: `server.public_url` first ("config"), then the
    address an admin saved from the wizard ("saved"). ("", "") when neither is set."""
    if configured:
        return configured, "config"
    rows = await store.fetch("SELECT value FROM app_settings WHERE key='public_url'")
    if rows:
        try:
            return normalise_public_url(rows[0][0]), "saved"
        except ValueError:
            return "", ""  # a stored value that no longer validates is as good as none
    return "", ""


async def set_public_url(store: Store, raw: Any, now: float) -> str:
    """Validate and save the address an admin confirmed. Raises EnrolError(422) with a message
    safe to show. Returns the canonical address."""
    try:
        url = normalise_public_url(raw)
    except ValueError as err:
        raise EnrolError(str(err)) from err
    await store.execute(
        "INSERT INTO app_settings (key, value, updated) VALUES ('public_url', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
        (url, now))
    return url


async def waiting_hosts(store: Store, now: float) -> list[dict[str, Any]]:
    """Enrolled hosts that have not reported yet: the agent was chosen and no batch has ever
    arrived, so the host has no row in `hosts`. Each row names the host and how far the enrolment
    is, so the hosts list and the dashboard can say "waiting for first data" and link to the
    enrolment page. A host that reported once is an ordinary host from then on."""
    rows = await store.fetch(
        "SELECT e.host, e.platform, e.control, e.created, e.expires_at, e.fetched_at, "
        "e.guard_reason FROM enrolments e LEFT JOIN hosts h ON h.host=e.host "
        "WHERE e.agent=1 AND h.host IS NULL ORDER BY e.created, e.host")
    out = []
    for host, platform, control, created, expires_at, fetched_at, guard in rows:
        if fetched_at is not None:
            note = "install started, waiting for first data"
        elif now >= expires_at:
            note = "command expired, regenerate it"
        elif guard:
            note = f"refused: {guard}"
        else:
            note = "command not run yet"
        out.append({"host": host, "platform": platform, "control": bool(control),
                    "created": created, "state": "waiting", "note": note,
                    "enrolment_url": f"/hosts/{host}/settings"})
    return out


async def record_step(store: Store, step_key: str, step: str, status: str, note: str,
                      now: float) -> str | None:
    """Store one install step report. Returns the host, or None when the step key is not valid.

    The key is the redeemed token's step key: a digest match on a fetched enrolment, for two
    hours after the fetch. A report for the same step replaces the earlier one. The note is
    redacted of secret-shaped text and capped, so a key cannot be carried back by accident.
    """
    if not isinstance(step_key, str) or not step_key.startswith(STEP_MARKER + "_"):
        return None
    rows = await store.fetch(
        "SELECT host, reports FROM enrolments WHERE step_hash=? AND fetched_at IS NOT NULL "
        "AND fetched_at+?>?", (_digest(step_key), STEP_TTL_S, now))
    if not rows:
        return None
    host, raw = rows[0]
    note = audit.clean_note(note, MAX_NOTE)
    reports = [r for r in json.loads(raw) if r["step"] != step]
    reports.append({"step": step, "status": status, "note": note, "at": now})
    await store.execute("UPDATE enrolments SET reports=? WHERE host=?", (json.dumps(reports), host))
    return host


def install_problem(raw: Any) -> dict[str, Any] | None:
    """The newest failed or refused install step that nothing has cleared since, from the
    enrolment's step reports (JSON text or a list), or None. A step's later report replaces its
    earlier one, so a rerun that passes a step clears it; a later `ready: ok` clears them all."""
    try:
        reports = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(reports, list):
        return None
    good = [r for r in reports if isinstance(r, dict)]
    ready = max((float(r.get("at") or 0) for r in good
                 if r.get("step") == "ready" and r.get("status") == "ok"), default=0.0)
    bad = [r for r in good if r.get("status") in ("failed", "refused")
           and float(r.get("at") or 0) > ready]
    if not bad:
        return None
    r = max(bad, key=lambda x: float(x.get("at") or 0))
    return {"step": str(r.get("step", "")), "status": str(r.get("status", "")),
            "note": str(r.get("note", "")), "at": float(r.get("at") or 0)}


async def claim_expiry_audit(store: Store, host: str, now: float) -> bool:
    """True exactly once for an enrolment whose token expired unused, so the audit row is single."""
    rows = await store.execute(
        "UPDATE enrolments SET expiry_audited=1 WHERE host=? AND fetched_at IS NULL "
        "AND expires_at<=? AND expiry_audited=0 RETURNING host", (host, now))
    return bool(rows)


_STEP_LABELS = {"script": "Script fetched", "data": "First data received",
                "control": "Control first pull", "ready": "Ready"}


STALL_S = 600  # seconds without install activity before a redeemed install counts as stalled


async def progress(store: Store, host: str, now: float) -> dict[str, Any] | None:
    """The enrolment's state machine, or None for a host with no enrolment.

    Steps are script, data, control and ready. Each is done, waiting, skipped (not chosen),
    or expired (the script was never fetched before the token ran out). `state` is the last
    step reached: waiting, script_fetched, first_data, control_pulled, ready or expired.
    """
    rows = await store.fetch(
        "SELECT platform, agent, control, created, expires_at, fetched_at, control_prefix, reports, "
        "reissued_at, guard_step, guard_reason, guard_at FROM enrolments WHERE host=?", (host,))
    if not rows:
        return None
    (platform, agent, control, created, expires_at, fetched_at, control_prefix, reports,
     reissued_at, guard_step, guard_reason, guard_at) = rows[0]
    first = await store.fetch("SELECT first_seen, last_seen FROM hosts WHERE host=?", (host,))
    data_at = first[0][0] if fetched_at is not None and first else None
    if reissued_at is not None and data_at is not None:
        # A reissued command starts a new install on a host that may already have reported, so
        # data counts only when a batch arrived after the reissue.
        data_at = first[0][1] if first[0][1] >= reissued_at else None
    pulled_at = None
    if fetched_at is not None and control_prefix:
        used = await store.fetch("SELECT last_used FROM ingest_keys WHERE prefix=?",
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
    # The command's own state: valid until it is redeemed or runs out; "used" once redeemed, which
    # is when a second run of it gets 410 and the way out is a regenerated command.
    token_state = "expired" if expired else "used" if fetched_at is not None else "valid"
    # A redeemed install counts as stalled once nothing has happened for STALL_S or a step
    # reported failure; only then does the page offer to regenerate, which revokes its keys.
    report_list = json.loads(reports)
    activity = [t for t in (fetched_at, data_at, pulled_at, *(r.get("at") for r in report_list))
                if isinstance(t, (int, float))]
    stalled = (fetched_at is not None and not ready and not expired and (
        any(r.get("status") == "failed" for r in report_list)
        or (activity and now - max(activity) > STALL_S)))
    guard = ({"step": guard_step, "reason": guard_reason, "at": guard_at}
             if fetched_at is None and guard_reason else None)
    return {"host": host, "platform": platform, "agent": bool(agent), "control": bool(control),
            "state": state, "ready": ready, "expired": expired, "token_state": token_state,
            "stalled": bool(stalled),
            "guard": guard, "created": created,
            "expires_at": expires_at, "steps": steps,
            "install": [{"step": r["step"], "status": r["status"], "note": r["note"],
                         "at": r["at"]} for r in report_list]}


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
