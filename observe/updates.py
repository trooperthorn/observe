"""Updating Observe from the console (README "Updating", THREAT-MODEL.md "Updates").

The container cannot update itself: it runs read-only, as UID 10001, with every capability
dropped and no Docker socket. What it can do is write one small request file into the data
volume and read a progress file back. A host-side helper (scripts/observe-updater.sh, started by
a systemd path unit) does the work as root and writes `state.json` at each phase.

Files, all under `server.update_dir` (the `update` folder of the data volume):

- `request.json`: written here with mode 0600 by the Update button,
  `{"v": 1, "id", "requested_by", "requested_at", "target": "origin/main", "nonce"}`. The helper
  moves it to `request.<id>.json` before it acts, so it cannot fire twice, and refuses a request
  older than ten minutes or with an id it has seen.
- `state.json`: written by the helper, `{"v": 1, "id", "phase", "message", "started_at",
  "updated_at", "old_commit", "new_commit", "log": [...]}`. Phases: received, backup, fetch,
  build, validate, restart, done, failed, with `failed_in` naming the phase a failure happened
  in. The log is read back through the audit redactor, cut to
  the last 50 lines, and every line is stripped of control characters before it is served.

The GitHub check (`server.update_check`, off by default) asks api.github.com for the newest
commit on main and the latest release tag of the upstream repository, at most once an hour. The
request carries nothing but the repository path and a User-Agent, times out in ten seconds,
follows no redirect and reads at most one megabyte. Any failure is reported as "could not
check"; the reason is logged, never served.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import audit
from .httpclient import http_client

log = logging.getLogger(__name__)

REPO = "trooperthorn/observe"
TARGET = "origin/main"
PHASES = ("received", "backup", "fetch", "build", "validate", "restart", "done", "failed")
FINAL_PHASES = ("done", "failed")
LOG_LINES = 50
LINE_LIMIT = 500
# A request the helper has not picked up blocks a new one for this long. The helper refuses
# a request older than ten minutes anyway, so after that the stale file is replaced.
REQUEST_STALE_S = 600
# A state file whose phase is not final counts as an update in progress for this long after
# its last write; after that the helper is taken to have died and a new request is allowed.
STATE_STALE_S = 1800
MAX_STATE_BYTES = 262_144
CHECK_INTERVAL_S = 3600
CHECK_TIMEOUT_S = 10.0
CHECK_MAX_BYTES = 1_048_576
_SHA = re.compile(r"[0-9a-f]{7,64}")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class UpdateOpen(Exception):
    """A request is already open (the file is there, or the helper is still working)."""


class UpdateError(Exception):
    """The request could not be written. The message is safe to show."""


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_commit() -> str:
    """The commit the image was built from, baked in by the Dockerfile's build argument."""
    value = os.environ.get("OBSERVE_GIT_COMMIT", "").strip()
    return value if _SHA.fullmatch(value) else "unknown"


# ---- the request file ---------------------------------------------------------------------

def _read_json(path: Path, limit: int = MAX_STATE_BYTES) -> dict[str, Any] | None:
    try:
        with path.open("rb") as fh:
            raw = fh.read(limit + 1)
    except OSError:
        return None
    if len(raw) > limit:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def open_request(update_dir: str | Path, now: float) -> dict[str, Any] | None:
    """The request that is still open, or None. A request is open while `request.json` is
    there and younger than ten minutes, or while `state.json` reports a phase that is not final
    and was written in the last thirty minutes."""
    base = Path(update_dir)
    req = base / "request.json"
    when = _mtime(req)
    if when is not None and now - when < REQUEST_STALE_S:
        data = _read_json(req) or {}
        return {"id": str(data.get("id", ""))[:64], "phase": "requested",
                "requested_at": data.get("requested_at")}
    state = read_state(base, now)
    if state is not None and state["phase"] not in FINAL_PHASES and not state["stale"]:
        return {"id": state["id"], "phase": state["phase"], "requested_at": None}
    return None


def write_request(update_dir: str | Path, requested_by: str, now: float) -> dict[str, Any]:
    """Write `request.json` for the helper, mode 0600, created exclusively so two admins
    cannot both win. Raises UpdateOpen when a request is open and UpdateError when the folder
    cannot be written."""
    base = Path(update_dir)
    if open_request(base, now) is not None:
        raise UpdateOpen("an update is already requested or running")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise UpdateError("the update folder cannot be created") from err
    req = base / "request.json"
    stale = _mtime(req)
    if stale is not None:
        # Older than ten minutes and never picked up: the helper is not installed or was not
        # running. The helper would refuse it anyway; replace it with a fresh request.
        try:
            req.unlink()
        except OSError as err:
            raise UpdateError("the stale request cannot be removed") from err
    body = {"v": 1, "id": str(uuid.uuid4()), "requested_by": requested_by[:128],
            "requested_at": iso(now), "target": TARGET, "nonce": secrets.token_hex(16)}
    text = json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n"
    tmp = base / f".request.{body['nonce']}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        try:
            # A hard link creation fails when the target exists, which makes the final step
            # atomic and exclusive on every POSIX file system; Windows cannot link, so rename.
            os.link(tmp, req)
        except FileExistsError:
            raise UpdateOpen("an update is already requested") from None
        except OSError:
            if req.exists():
                raise UpdateOpen("an update is already requested") from None
            os.replace(tmp, req)
    except UpdateOpen:
        raise
    except OSError as err:
        raise UpdateError("the request file cannot be written") from err
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return body


# ---- the state file -----------------------------------------------------------------------

def clean_line(line: Any) -> str:
    text = _CONTROL.sub("?", audit.redact_secrets(str(line)))
    return text[:LINE_LIMIT]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value < 4_102_444_800 else None


def read_state(update_dir: str | Path, now: float) -> dict[str, Any] | None:
    """The helper's progress, checked and redacted, or None when there is none. A file with an
    unknown phase or no id is reported as `failed` with a note, never as done."""
    path = Path(update_dir) / "state.json"
    data = _read_json(path)
    if data is None:
        return None
    phase = data.get("phase")
    note = ""
    if phase not in PHASES:
        phase, note = "failed", "the progress file is not readable"
    raw_log = data.get("log")
    lines = [clean_line(x) for x in raw_log[-LOG_LINES:]] if isinstance(raw_log, list) else []
    if note:
        lines.append(note)
    updated = _number(data.get("updated_at")) or _mtime(path) or 0.0
    out = {
        "id": clean_line(data.get("id", ""))[:64],
        "phase": phase,
        "message": clean_line(data.get("message", "")),
        "started_at": _number(data.get("started_at")),
        "updated_at": updated,
        "old_commit": _commit(data.get("old_commit")),
        "new_commit": _commit(data.get("new_commit")),
        # The phase the helper was in when it failed, so the page marks that chip.
        "failed_in": data.get("failed_in") if data.get("failed_in") in PHASES else "",
        "log": lines,
        "stale": phase not in FINAL_PHASES and now - updated > STATE_STALE_S,
    }
    return out


def _commit(value: Any) -> str:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else ""


# ---- the GitHub check ---------------------------------------------------------------------

class CheckError(ValueError):
    """A reply that is not the one expected. The message is for the log only."""


def parse_commit(body: bytes) -> str:
    """The sha of GET /repos/{repo}/commits/main."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as err:
        raise CheckError("the commit reply is not JSON") from err
    sha = data.get("sha") if isinstance(data, dict) else None
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise CheckError("the commit reply has no sha")
    return sha


def parse_release(body: bytes) -> str:
    """The tag of GET /repos/{repo}/releases/latest."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as err:
        raise CheckError("the release reply is not JSON") from err
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise CheckError("the release reply has no tag")
    return tag


Fetcher = Callable[[str], Awaitable[bytes]]


async def fetch_bounded(url: str) -> bytes:
    """GET one GitHub API URL: ten seconds, no redirect, at most one megabyte. Only the
    repository path leaves the container; there is no token and no other header."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "observe-update-check"}
    async with http_client(True, CHECK_TIMEOUT_S, follow_redirects=False) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code != 200:
                raise CheckError(f"status {resp.status_code}")
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > CHECK_MAX_BYTES:
                raise CheckError("body too large")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > CHECK_MAX_BYTES:
                    raise CheckError("body too large")
            return bytes(body)


@dataclass
class GitHubCheck:
    """The hourly check, remembered between calls. `fetch` is swapped in tests."""

    enabled: bool
    fetch: Fetcher = fetch_bounded
    repo: str = REPO
    checked_at: float | None = None
    latest_commit: str = ""
    latest_tag: str = ""
    ok: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "checked_at": self.checked_at, "ok": self.ok,
                "latest_commit": self.latest_commit, "latest_tag": self.latest_tag,
                "repo": self.repo}

    async def refresh(self, now: float) -> dict[str, Any]:
        """Ask GitHub when the check is on and the last answer is older than an hour. A failure
        keeps the previous answer out and reports `ok: false` ("could not check")."""
        if not self.enabled:
            return self.snapshot()
        if self.checked_at is not None and now - self.checked_at < CHECK_INTERVAL_S:
            return self.snapshot()
        self.checked_at = now
        base = f"https://api.github.com/repos/{self.repo}"
        try:
            commit = parse_commit(await self.fetch(f"{base}/commits/main"))
        except Exception as err:  # noqa: BLE001 - any failure is "could not check"
            log.warning("update check: could not read the newest commit: %s", err)
            self.ok, self.latest_commit, self.latest_tag = False, "", ""
            return self.snapshot()
        tag = ""
        try:
            tag = parse_release(await self.fetch(f"{base}/releases/latest"))
        except Exception as err:  # noqa: BLE001 - a repository with no release answers 404
            log.info("update check: no release tag: %s", err)
        self.ok, self.latest_commit, self.latest_tag = True, commit, tag
        return self.snapshot()


# ---- the agents card ----------------------------------------------------------------------

UPDATABLE_PLATFORMS = ("linux", "raspberry-pi")
INSTALL_ONLY = ("windows", "truenas")
# The oldest hostwatch agent whose control daemon runs agent.update. 0.1.0 answers it with
# "unknown_action: action 'agent.update' is not supported", so an update button for it can only
# fail. The daemon does not advertise its actions yet; when it does, gate on that instead.
MIN_UPDATE_AGENT_VERSION = "0.2.0"
TOO_OLD = "agent too old, reinstall from host settings"


def supports_update(agent_version: str) -> bool:
    """Whether the agent's control daemon can run agent.update. An unknown or unparsable
    version is not trusted to."""
    from packaging.version import InvalidVersion, Version
    try:
        return Version(agent_version) >= Version(MIN_UPDATE_AGENT_VERSION)
    except InvalidVersion:
        return False


NOT_ALLOWED = "agent updates are off in this host's allowlist (host settings)"


def agent_row(host: dict[str, Any], enrolled: str, has_key: bool, last_pull: float | None,
              now: float, allows_update: bool | None = None) -> dict[str, Any]:
    """What the Agents card shows for one host, from the hosts row, the enrolment platform
    and the host's control key. `eligible` means the Update agent button is offered.
    `allows_update` is the saved allowlist's `update` flag of a host enrolled with control (None
    for any other host); the control plugin refuses agent.update when it is off, so the button
    is not offered then either."""
    platform = enrolled or str(host.get("platform") or "")
    reason = ""
    if platform in INSTALL_ONLY:
        reason = "install command only"
    elif platform not in UPDATABLE_PLATFORMS:
        reason = "unknown platform"
    elif not has_key:
        reason = "no control daemon"
    elif last_pull is None:
        reason = "the control daemon has never pulled"
    elif not supports_update(str(host.get("agent_version") or "")):
        reason = TOO_OLD
    elif allows_update is False:
        reason = NOT_ALLOWED
    return {"host": host["host"], "platform": platform, "agent_version": host.get("agent_version") or "",
            "control": has_key, "control_pulled": last_pull is not None,
            "last_pull": last_pull,
            "pull_age_s": None if last_pull is None else max(0.0, now - last_pull),
            "eligible": reason == "", "reason": reason}


async def agent_rows(store: Any, now: float) -> list[dict[str, Any]]:
    """One row per host that has pushed, with its enrolment platform and control key state."""
    hosts = await store.host_rows()
    platforms: dict[str, str] = {}
    allows: dict[str, bool] = {}
    for host, platform, control, allowlist in await store.fetch(
            "SELECT host, platform, control, allowlist FROM enrolments"):
        platforms[host] = platform
        if control:
            try:
                allows[host] = bool(json.loads(allowlist or "{}").get("update"))
            except (ValueError, AttributeError):
                allows[host] = False
    keys: dict[str, tuple[bool, float | None]] = {}
    for host, last_used in await store.fetch(
            "SELECT host, MAX(last_used) FROM ingest_keys WHERE scope='wpc' AND revoked_at IS NULL "
            "GROUP BY host"):
        keys[host] = (True, last_used)
    out = []
    for row in hosts:
        has_key, last = keys.get(row["host"], (False, None))
        out.append(agent_row(row, platforms.get(row["host"], ""), has_key, last, now,
                             allows.get(row["host"])))
    return out


def unix_now() -> float:
    return time.time()
