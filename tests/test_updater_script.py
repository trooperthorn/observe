"""The host-side update helper (scripts/observe-updater.sh, README "Updating"): request
validation, the single-use id, the phases written to state.json, the backup rotation, and the
rollback, run against a temporary deployment with fake git and docker on PATH."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "observe-updater.sh"
INSTALLER = ROOT / "scripts" / "install-updater.sh"
BASH = shutil.which("bash")
OLD = "2daaa87a1b2c3d4e5f60718293a4b5c6d7e8f901"
NEW = "9f2c1d0e8b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d"

pytestmark = pytest.mark.skipif(not BASH, reason="bash is needed to run the helper")

# Each stub appends its argv to $STUB_LOG and behaves as $STUB_FAIL says. The fake git keeps
# HEAD in $STUB_HEAD so pull and checkout move it like the real one.
STUBS = {
    "git": r'''#!/usr/bin/env bash
printf 'git %s\n' "$*" >>"$STUB_LOG"
case "$1 ${2:-}" in
  "status --porcelain") printf '%s' "${STUB_DIRTY:-}" ;;
  "rev-parse HEAD") cat "$STUB_HEAD" ;;
  "fetch origin") [ "${STUB_FAIL:-}" = fetch ] && { echo "fatal: no route"; exit 128; } ;;
  "pull --ff-only") [ "${STUB_FAIL:-}" = pull ] && { echo "fatal: Not possible to fast-forward"; exit 128; }; printf '%s\n' "$STUB_NEW" >"$STUB_HEAD"; echo "Updating" ;;
  "checkout --quiet") printf '%s\n' "$3" >"$STUB_HEAD" ;;
  *) echo "unexpected git $*" >&2; exit 2 ;;
esac
exit 0
''',
    "docker": r'''#!/usr/bin/env bash
printf 'docker %s\n' "$*" >>"$STUB_LOG"
[ "$1" = compose ] || { echo "unexpected docker $*" >&2; exit 2; }
case "$2" in
  build) [ "${STUB_FAIL:-}" = build ] && { echo "ERROR: failed to solve"; exit 17; }; echo "built" ;;
  run) [ "${STUB_FAIL:-}" = validate ] && { echo "config error: bad"; exit 2; }; echo "ok: 3 monitors, 2 credentials, 1 alert targets, 1 plugins" ;;
  up) n=$(grep -c '^docker compose up' "$STUB_LOG"); if [ "${STUB_FAIL:-}" = up ] || { [ "${STUB_FAIL:-}" = up-once ] && [ "$n" = 1 ]; }; then echo "Error response from daemon"; exit 1; fi; echo "Started" ;;
  *) echo "unexpected docker compose $*" >&2; exit 2 ;;
esac
exit 0
''',
    "stat": '#!/usr/bin/env bash\nprintf \'%s\\n\' "${STUB_OWNER:-10001}"\n',
    "id": '#!/usr/bin/env bash\necho 0\n',
}


def _bash_path(path: Path) -> str:
    """A path the MSYS bash on Windows and a POSIX bash both accept."""
    text = str(path)
    if os.name == "nt" and len(text) > 2 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


class Deploy:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "observe"
        (self.root / ".git").mkdir(parents=True)
        (self.root / "docker-compose.yml").write_text("services:\n  observe:\n    build: .\n")
        (self.root / ".env").write_text("OBSERVE_UI_PASSWORD=hunter2\n")
        self.data = self.root / "data"
        self.update = self.data / "update"
        self.update.mkdir(parents=True)
        (self.data / "observe.db").write_bytes(b"sqlite3 fake")
        self.stubs = tmp_path / "stubs"
        self.stubs.mkdir()
        for name, body in STUBS.items():
            path = self.stubs / name
            path.write_text(body, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        shim = self.stubs / "python3"
        shim.write_text(f'#!/usr/bin/env bash\nexec "{_bash_path(Path(sys.executable))}" "$@"\n',
                        encoding="utf-8", newline="\n")
        shim.chmod(0o755)
        self.log = tmp_path / "stub.log"
        self.log.write_text("")
        self.head = tmp_path / "head"
        self.head.write_text(OLD + "\n")

    def request(self, **over) -> dict:
        body = {"v": 1, "id": str(uuid.uuid4()), "requested_by": "sean",
                "requested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "target": "origin/main", "nonce": "ab" * 16}
        body.update(over)
        (self.update / "request.json").write_text(json.dumps(body), encoding="utf-8")
        return body

    def run(self, **env_over) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": _bash_path(self.stubs) + os.pathsep + os.environ["PATH"],
               "STUB_LOG": _bash_path(self.log), "STUB_HEAD": _bash_path(self.head),
               "STUB_NEW": NEW, **env_over}
        return subprocess.run([BASH, _bash_path(SCRIPT), _bash_path(self.root)], env=env,
                              capture_output=True, text=True, timeout=120)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def state(self) -> dict:
        return json.loads((self.update / "state.json").read_text(encoding="utf-8"))

    def files(self) -> list[str]:
        return sorted(p.name for p in self.update.iterdir())


@pytest.fixture
def deploy(tmp_path):
    return Deploy(tmp_path)


def test_scripts_pass_a_syntax_check_and_use_strict_mode():
    for script in (SCRIPT, INSTALLER):
        done = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        text = script.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text
        assert b"\r" not in script.read_bytes()


def test_the_helper_reads_no_env_file_or_secret_and_runs_python_isolated():
    text = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert ".env" not in code and "secrets" not in code
    assert "python3 -I -" in code and code.count("python3 -I -") == 2
    assert "eval" not in code and "source " not in code and ". $" not in code
    # Only state.json and stdout (journald under systemd) are written to.
    assert "logger" not in code and "syslog" not in code and "> /var/log" not in code


def test_a_valid_request_runs_every_phase_in_order_and_is_claimed_once(deploy):
    body = deploy.request()
    done = deploy.run()
    assert done.returncode == 0, done.stdout + done.stderr
    state = deploy.state()
    assert state["phase"] == "done" and state["id"] == body["id"]
    assert state["old_commit"] == OLD and state["new_commit"] == NEW and state["failed_in"] == ""
    assert state["message"] == f"updated {OLD} to {NEW}"
    assert state["log"][0] == f"received request {body['id']} from sean"
    assert any("ok: 3 monitors" in line for line in state["log"])
    assert deploy.calls() == [
        "git status --porcelain", "git rev-parse HEAD", "git fetch origin",
        "git pull --ff-only origin main", "git rev-parse HEAD",
        f"docker compose build --pull --build-arg OBSERVE_GIT_COMMIT={NEW} observe",
        "docker compose run --rm observe --config /config/observe.yaml --validate",
        "docker compose up -d observe",
    ]
    assert f"request.{body['id']}.json" in deploy.files() and "request.json" not in deploy.files()
    assert body["id"] in (deploy.update / "seen.ids").read_text()
    backups = sorted((deploy.data / "backups").iterdir())
    assert len(backups) == 1 and backups[0].name.startswith("observe.db.")
    assert backups[0].name.endswith("." + body["id"].split("-")[0])
    assert "hunter2" not in json.dumps(state) and "hunter2" not in done.stdout
    # The same request cannot fire again: a copy with the same id is refused before any work.
    deploy.log.write_text("")
    deploy.request(id=body["id"])
    again = deploy.run()
    assert again.returncode == 1
    assert deploy.state()["phase"] == "failed" and "already used" in deploy.state()["message"]
    assert deploy.calls() == [] and "request.json" not in deploy.files()


def test_nothing_to_do_when_there_is_no_request(deploy):
    done = deploy.run()
    assert done.returncode == 0 and "no request" in done.stdout and deploy.calls() == []


@pytest.mark.parametrize("over, reason", [
    ({"v": 2}, "version is not 1"),
    ({"target": "origin/dev"}, "target is not origin/main"),
    ({"id": "not-a-uuid"}, "id is not a uuid"),
    ({"id": "../../etc/passwd"}, "id is not a uuid"),
    ({"requested_by": "sean; rm -rf /"}, "requested_by"),
    ({"nonce": "short"}, "nonce"),
    ({"requested_at": "yesterday"}, "requested_at"),
    ({"extra": 1}, "exactly the fields"),
])
def test_a_malformed_request_is_refused_before_anything_runs(deploy, over, reason):
    deploy.request(**over)
    done = deploy.run()
    assert done.returncode == 1, done.stdout + done.stderr
    state = deploy.state()
    assert state["phase"] == "failed" and state["failed_in"] == "received"
    assert reason in state["message"], state["message"]
    assert deploy.calls() == []
    names = deploy.files()
    assert "request.json" not in names and any(n.startswith("request.rejected.") for n in names)


def test_a_request_that_is_not_json_or_too_old_is_refused(deploy):
    (deploy.update / "request.json").write_text("{not json", encoding="utf-8")
    assert deploy.run().returncode == 1
    assert "not valid JSON" in deploy.state()["message"] and deploy.calls() == []
    stale = datetime.fromtimestamp(time.time() - 601, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    deploy.request(requested_at=stale)
    assert deploy.run().returncode == 1
    assert "older than 600 seconds" in deploy.state()["message"] and deploy.calls() == []


def test_a_request_from_another_owner_is_ignored_without_touching_the_state(deploy):
    deploy.request()
    done = deploy.run(STUB_OWNER="0")
    assert done.returncode == 0 and "owned by uid 0" in done.stdout
    assert deploy.calls() == [] and not (deploy.update / "state.json").exists()
    assert "request.json" not in deploy.files() and any(n.startswith("request.rejected.") for n in deploy.files())


def test_a_dirty_tree_stops_before_the_pull_and_the_build(deploy):
    deploy.request()
    done = deploy.run(STUB_DIRTY=" M observe/web.py\n")
    assert done.returncode == 1
    state = deploy.state()
    assert state["phase"] == "failed" and state["failed_in"] == "fetch"
    assert "local changes" in state["message"]
    assert deploy.calls() == ["git status --porcelain"]
    assert (deploy.data / "backups").is_dir()  # the backup was made before the tree was checked


def test_a_pull_that_is_not_a_fast_forward_fails_in_fetch(deploy):
    deploy.request()
    assert deploy.run(STUB_FAIL="pull").returncode == 1
    state = deploy.state()
    assert state["failed_in"] == "fetch" and "fast-forward" in state["message"]
    assert "Not possible to fast-forward" in "\n".join(state["log"])
    assert not any(c.startswith("docker") for c in deploy.calls())


@pytest.mark.parametrize("fail, phase", [("build", "build"), ("validate", "validate")])
def test_a_failed_build_or_validate_leaves_the_running_container_alone(deploy, fail, phase):
    deploy.request()
    assert deploy.run(STUB_FAIL=fail).returncode == 1
    state = deploy.state()
    assert state["phase"] == "failed" and state["failed_in"] == phase
    assert "not touched" in state["message"]
    calls = deploy.calls()
    assert not any(c.startswith("docker compose up") for c in calls)
    assert not any(c.startswith("git checkout") for c in calls)
    assert state["old_commit"] == OLD and state["new_commit"] == NEW


def test_a_failed_restart_puts_the_old_commit_back_and_rebuilds_it(deploy):
    deploy.request()
    done = deploy.run(STUB_FAIL="up-once")
    assert done.returncode == 1, done.stdout + done.stderr
    state = deploy.state()
    assert state["phase"] == "failed" and state["failed_in"] == "restart"
    assert f"previous version {OLD} is running again" in state["message"]
    calls = deploy.calls()
    first_up = calls.index("docker compose up -d observe")
    assert calls[first_up + 1:] == [
        f"git checkout --quiet {OLD}",
        f"docker compose build --pull --build-arg OBSERVE_GIT_COMMIT={OLD} observe",
        "docker compose up -d observe",
    ]
    assert deploy.head.read_text().strip() == OLD


def test_when_the_rollback_fails_too_the_state_says_so(deploy):
    deploy.request()
    assert deploy.run(STUB_FAIL="up").returncode == 1
    state = deploy.state()
    assert "rollback" in state["message"] and "failed too" in state["message"]
    assert deploy.calls().count("docker compose up -d observe") == 2


def test_backups_keep_the_newest_five(deploy):
    backups = deploy.data / "backups"
    backups.mkdir()
    for i in range(6):
        (backups / f"observe.db.2026010{i}T000000Z.aaaaaaaa").write_bytes(b"old")
    (backups / "observe.db.20260101T000000Z.aaaaaaaa-wal").write_bytes(b"old")
    (backups / "keep-me.txt").write_bytes(b"not a backup")
    deploy.request()
    assert deploy.run().returncode == 0
    names = sorted(p.name for p in backups.iterdir())
    kept = [n for n in names if n.startswith("observe.db.") and not n.endswith("-wal")]
    assert len(kept) == 5 and "keep-me.txt" in names
    assert "observe.db.20260100T000000Z.aaaaaaaa" not in names
    assert "observe.db.20260101T000000Z.aaaaaaaa" not in names
    assert "observe.db.20260101T000000Z.aaaaaaaa-wal" not in names


def test_the_helper_refuses_to_run_outside_a_checkout_or_without_root(deploy, tmp_path):
    deploy.request()
    (deploy.root / ".git").rmdir()
    done = deploy.run()
    assert done.returncode == 1 and "not a git checkout" in done.stdout
    (deploy.root / ".git").mkdir()
    (deploy.stubs / "id").write_text("#!/usr/bin/env bash\necho 1000\n", newline="\n")
    done = deploy.run()
    assert done.returncode == 1 and "must run as root" in done.stdout
    assert deploy.calls() == [] and (deploy.update / "request.json").exists()


def test_the_installer_renders_the_units_with_the_deployment_path(tmp_path):
    text = INSTALLER.read_text(encoding="utf-8")
    assert "/usr/local/sbin/observe-updater" in text and "enable --now observe-updater.path" in text
    assert 'install -d -o 10001 -g 10001 -m 0750 "$DEPLOY/data/update"' in text
    for unit in ("observe-updater.path", "observe-updater.service"):
        body = (ROOT / "scripts" / unit).read_text(encoding="utf-8")
        assert "@@DEPLOY@@" in body
    assert "PathExists=@@DEPLOY@@/data/update/request.json" in (ROOT / "scripts" / "observe-updater.path").read_text()
    service = (ROOT / "scripts" / "observe-updater.service").read_text()
    assert "Type=oneshot" in service and "ExecStart=/usr/local/sbin/observe-updater @@DEPLOY@@" in service
    assert "Requires=docker.service" in service
    # The path is checked before it goes into a unit file verbatim.
    assert "^/[A-Za-z0-9._/-]+$" in text


def test_new_files_use_lf_and_no_em_dashes_or_model_names():
    from .test_field_docs import stored_bytes

    for rel in ("scripts/observe-updater.sh", "scripts/install-updater.sh",
                "scripts/observe-updater.path", "scripts/observe-updater.service",
                "tests/test_updater_script.py"):
        raw = stored_bytes(ROOT / rel)
        text = raw.decode("utf-8")
        assert b"\r" not in raw and chr(0x2014) not in text, rel
        names = ("cla" + "ude", "op" + "us", "son" + "net", "hai" + "ku")
        assert not any(w in text.lower() for w in names), rel
