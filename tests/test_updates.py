"""Updating Observe from the console (README "Updating"): the request and state files, the
upstream check with fixtures and its failure paths, the agents rows, and the admin routes
with their 409, admin and CSRF refusals."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import httpx
import pytest

from observe import __version__, audit, updates
from observe.updates import (CheckError, GitHubCheck, UpdateError, UpdateOpen, agent_row,
                             fetch_bounded, open_request, parse_commit, parse_release,
                             read_state, write_request)

from .api_env import ApiEnv, host_batch

FIX = Path(__file__).parent / "fixtures" / "github"
NOW = 1_760_100_000.0


def run(coro):
    return asyncio.run(coro)


# ---- the request file ------------------------------------------------------------------------

def test_a_request_is_written_exclusively_with_mode_0600_and_the_documented_fields(tmp_path):
    base = tmp_path / "update"
    made = write_request(base, "sean", NOW)
    path = base / "request.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == made
    assert set(data) == {"v", "id", "requested_by", "requested_at", "target", "nonce"}
    assert data["v"] == 1 and data["target"] == "origin/main"
    assert data["requested_by"] == "sean" and data["requested_at"] == "2025-10-10T12:40:00Z"
    assert len(data["id"]) == 36 and len(data["nonce"]) == 32
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [p for p in base.iterdir() if p.name.startswith(".request")], "no temp file left"


def test_a_second_request_is_refused_while_the_first_is_open(tmp_path):
    base = tmp_path / "update"
    first = write_request(base, "sean", NOW)
    with pytest.raises(UpdateOpen):
        write_request(base, "sean", NOW + 60)
    assert json.loads((base / "request.json").read_text())["id"] == first["id"]
    assert open_request(base, NOW + 60) == {"id": first["id"], "phase": "requested",
                                            "requested_at": first["requested_at"]}


def test_a_request_the_helper_never_took_is_replaced_after_ten_minutes(tmp_path):
    base = tmp_path / "update"
    first = write_request(base, "sean", NOW)
    old = NOW - 700
    os.utime(base / "request.json", (old, old))
    assert open_request(base, NOW) is None
    second = write_request(base, "sean", NOW)
    assert second["id"] != first["id"]
    assert json.loads((base / "request.json").read_text())["id"] == second["id"]


def _state(base: Path, **over) -> None:
    data = json.loads((FIX / "state-done.json").read_text(encoding="utf-8"))
    data.update(over)
    base.mkdir(parents=True, exist_ok=True)
    (base / "state.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("phase", ["received", "backup", "fetch", "build", "validate", "restart"])
def test_a_running_update_blocks_a_new_request_until_it_ends_or_goes_stale(tmp_path, phase):
    base = tmp_path / "update"
    _state(base, phase=phase, updated_at=NOW - 60)
    assert open_request(base, NOW)["phase"] == phase
    with pytest.raises(UpdateOpen):
        write_request(base, "sean", NOW)
    # The helper died: thirty minutes after its last write a new request is allowed.
    _state(base, phase=phase, updated_at=NOW - 1801)
    assert open_request(base, NOW) is None
    write_request(base, "sean", NOW)


@pytest.mark.parametrize("phase", ["done", "failed"])
def test_a_finished_update_does_not_block(tmp_path, phase):
    base = tmp_path / "update"
    _state(base, phase=phase, updated_at=NOW - 1)
    assert open_request(base, NOW) is None
    write_request(base, "sean", NOW)


def test_an_unwritable_folder_is_an_update_error(tmp_path):
    blocker = tmp_path / "update"
    blocker.write_text("not a folder")
    with pytest.raises(UpdateError):
        write_request(blocker, "sean", NOW)


# ---- the state file --------------------------------------------------------------------------

def test_state_is_read_checked_and_redacted(tmp_path):
    base = tmp_path / "update"
    _state(base, log=[f"line {i}" for i in range(70)] + [
        "key wpc_abcdefghijklmnopqrstuvwxyz0123456789 pulled",
        "Authorization: Bearer abc\x1b[0m\x00", "token=hunter2"])
    got = read_state(base, NOW)
    assert got["phase"] == "done" and got["id"] == "6f1c2a52-8d0e-4c53-9a53-0d1f6d2f7a10"
    assert got["old_commit"].startswith("2daaa87") and got["new_commit"].startswith("9f2c1d0")
    assert got["started_at"] == 1760100000 and got["updated_at"] == 1760100180
    assert len(got["log"]) == 50 and got["log"][0] == "line 23"
    joined = "\n".join(got["log"])
    assert "wpc_abc" not in joined and "[redacted]" in joined
    assert "\x1b" not in joined and "\x00" not in joined and "?" in got["log"][-2]
    assert got["stale"] is False


def test_missing_oversized_or_broken_state_is_handled(tmp_path):
    base = tmp_path / "update"
    assert read_state(base, NOW) is None
    base.mkdir()
    (base / "state.json").write_text("{not json", encoding="utf-8")
    assert read_state(base, NOW) is None
    (base / "state.json").write_text("[]", encoding="utf-8")
    assert read_state(base, NOW) is None
    (base / "state.json").write_bytes(b'{"phase": "done", "log": ["' + b"x" * 300_000 + b'"]}')
    assert read_state(base, NOW) is None


def test_an_unknown_phase_or_bad_commit_is_never_shown_as_done(tmp_path):
    base = tmp_path / "update"
    _state(base, phase="finished", old_commit="not a sha", new_commit=None, started_at="soon")
    got = read_state(base, NOW)
    assert got["phase"] == "failed" and got["log"][-1] == "the progress file is not readable"
    assert got["old_commit"] == "" and got["new_commit"] == "" and got["started_at"] is None


def test_a_phase_that_stopped_being_written_is_stale(tmp_path):
    base = tmp_path / "update"
    _state(base, phase="build", updated_at=NOW - 1801)
    assert read_state(base, NOW)["stale"] is True
    _state(base, phase="build", updated_at=NOW - 10)
    assert read_state(base, NOW)["stale"] is False


def test_the_git_commit_comes_from_the_build_argument_or_is_unknown(monkeypatch):
    monkeypatch.delenv("OBSERVE_GIT_COMMIT", raising=False)
    assert updates.git_commit() == "unknown"
    monkeypatch.setenv("OBSERVE_GIT_COMMIT", "9f2c1d0e8b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d")
    assert updates.git_commit() == "9f2c1d0e8b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d"
    monkeypatch.setenv("OBSERVE_GIT_COMMIT", "<b>x</b>")
    assert updates.git_commit() == "unknown"


# ---- the GitHub check ------------------------------------------------------------------------

def test_the_parsers_read_the_fixtures():
    assert parse_commit((FIX / "commit.json").read_bytes()) == \
        "9f2c1d0e8b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d"
    assert parse_release((FIX / "release.json").read_bytes()) == "v2026.9.23.6"


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{"sha": 12}', b'{"sha": "zz"}',
                                  b'{"sha": "<script>"}', b"\xff\xfe"])
def test_a_commit_reply_that_is_not_a_sha_is_refused(body):
    with pytest.raises(CheckError):
        parse_commit(body)


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"tag_name": ""}',
                                  b'{"tag_name": "v1 <b>"}', b'{"tag_name": ["v1"]}'])
def test_a_release_reply_that_is_not_a_tag_is_refused(body):
    with pytest.raises(CheckError):
        parse_release(body)


class Fake:
    def __init__(self, answers):
        self.answers = answers
        self.calls: list[str] = []

    async def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        answer = self.answers[url.rsplit("/", 1)[-1]]
        if isinstance(answer, Exception):
            raise answer
        return answer


GOOD = {"main": (FIX / "commit.json").read_bytes(), "latest": (FIX / "release.json").read_bytes()}


def test_the_check_is_off_by_default_and_then_asks_nothing():
    fake = Fake(GOOD)
    check = GitHubCheck(False, fetch=fake)
    got = run(check.refresh(NOW))
    assert got["enabled"] is False and got["ok"] is False and fake.calls == []


def test_the_check_asks_only_the_two_repository_urls_and_remembers_the_answer_for_an_hour():
    fake = Fake(GOOD)
    check = GitHubCheck(True, fetch=fake)
    got = run(check.refresh(NOW))
    assert got["ok"] and got["latest_commit"].startswith("9f2c1d0") and got["latest_tag"] == "v2026.9.23.6"
    assert fake.calls == ["https://api.github.com/repos/trooperthorn/observe/commits/main",
                          "https://api.github.com/repos/trooperthorn/observe/releases/latest"]
    run(check.refresh(NOW + 3599))
    assert len(fake.calls) == 2
    run(check.refresh(NOW + 3600))
    assert len(fake.calls) == 4


def test_a_failed_check_reports_could_not_check_and_is_not_retried_for_an_hour(caplog):
    fake = Fake({"main": httpx.ConnectError("boom"), "latest": GOOD["latest"]})
    check = GitHubCheck(True, fetch=fake)
    got = run(check.refresh(NOW))
    assert got["ok"] is False and got["latest_commit"] == "" and got["latest_tag"] == ""
    assert len(fake.calls) == 1
    run(check.refresh(NOW + 10))
    assert len(fake.calls) == 1


def test_a_repository_without_a_release_still_reports_the_commit():
    fake = Fake({"main": GOOD["main"], "latest": CheckError("status 404")})
    got = run(GitHubCheck(True, fetch=fake).refresh(NOW))
    assert got["ok"] is True and got["latest_tag"] == "" and got["latest_commit"]


def _transport(handler):
    return httpx.MockTransport(handler)


def test_fetch_bounded_sends_only_the_path_refuses_redirects_and_caps_the_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["url"] = str(request.url)
        path = request.url.path
        if path.endswith("/redirect"):
            return httpx.Response(302, headers={"Location": "https://example.invalid/"})
        if path.endswith("/big"):
            return httpx.Response(200, content=b"x" * (updates.CHECK_MAX_BYTES + 1))
        if path.endswith("/declared"):
            return httpx.Response(200, headers={"content-length": str(updates.CHECK_MAX_BYTES + 1)},
                                  content=b"")
        if path.endswith("/missing"):
            return httpx.Response(404, content=b"{}")
        return httpx.Response(200, content=GOOD["main"])

    def fake_client(verify, timeout, *, follow_redirects=False):
        assert follow_redirects is False and timeout == 10.0 and verify is True
        return httpx.AsyncClient(transport=_transport(handler), timeout=timeout,
                                 follow_redirects=follow_redirects)

    monkeypatch.setattr(updates, "http_client", fake_client)
    base = "https://api.github.com/repos/trooperthorn/observe"
    assert parse_commit(run(fetch_bounded(f"{base}/commits/main"))).startswith("9f2c1d0")
    assert "authorization" not in seen["headers"] and "cookie" not in seen["headers"]
    assert seen["headers"]["user-agent"] == "observe-update-check"
    assert seen["url"] == f"{base}/commits/main"
    for tail in ("redirect", "big", "declared", "missing"):
        with pytest.raises(CheckError):
            run(fetch_bounded(f"{base}/{tail}"))


# ---- the agents rows -------------------------------------------------------------------------

@pytest.mark.parametrize("platform, enrolled, key, pull, eligible, reason", [
    ("linux", "", True, NOW - 5, True, ""),
    ("", "raspberry-pi", True, NOW - 5, True, ""),
    ("linux", "", True, None, False, "the control daemon has never pulled"),
    ("linux", "", False, None, False, "no control daemon"),
    ("windows", "", True, NOW, False, "install command only"),
    ("linux", "truenas", True, NOW, False, "install command only"),
    ("plan9", "", True, NOW, False, "unknown platform"),
])
def test_eligibility_follows_platform_key_and_pull(platform, enrolled, key, pull, eligible, reason):
    row = agent_row({"host": "nas01", "platform": platform, "agent_version": "1.2"},
                    enrolled, key, pull, NOW)
    assert row["eligible"] is eligible and row["reason"] == reason
    assert row["control"] is key and row["control_pulled"] is (pull is not None)
    assert row["agent_version"] == "1.2" and row["platform"] == (enrolled or platform)
    assert row["pull_age_s"] == (None if pull is None else NOW - pull)


# ---- the routes ------------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    e = ApiEnv(tmp_path, update_dir=str(tmp_path / "update"))
    yield e
    e.close()


def post(env, body, headers=None):
    return env.client.post("/api/admin/updates/observe", json=body,
                           headers=env.csrf if headers is None else headers)


CONFIRM = {"confirmed": True, "confirm_text": "update"}


def test_status_is_admin_only_and_shows_the_version_commit_and_phases(env, monkeypatch):
    monkeypatch.setenv("OBSERVE_GIT_COMMIT", "9f2c1d0e8b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d")
    assert env.client.get("/api/v2/updates/status").status_code == 401
    env.login("bob", admin=False)
    assert env.client.get("/api/v2/updates/status").status_code == 403
    assert env.client.get("/api/v2/updates/agents").status_code == 403
    env.login()
    r = env.client.get("/api/v2/updates/status")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["version"] == __version__ and got["commit"].startswith("9f2c1d0")
    assert got["phases"] == list(updates.PHASES) and got["request"] is None and got["state"] is None
    assert got["github"] == {"enabled": False, "ok": False, "checked_at": None,
                             "latest_commit": "", "latest_tag": "", "repo": "trooperthorn/observe"}
    assert "etag" not in {k.lower() for k in r.headers}


def test_the_update_button_writes_the_request_audits_and_refuses_a_second(env, tmp_path):
    env.login()
    r = post(env, CONFIRM)
    assert r.status_code == 202, r.text
    got = r.json()
    assert got["state"] == "requested" and got["target"] == "origin/main"
    data = json.loads((tmp_path / "update" / "request.json").read_text(encoding="utf-8"))
    assert data["id"] == got["id"] and data["requested_by"] == "admin"
    rows = run(audit.list_rows(env.store, kind="update_requested"))
    assert len(rows) == 1 and rows[0]["actor"] == "admin"
    assert rows[0]["detail"]["request_id"] == got["id"] and rows[0]["detail"]["version"] == __version__
    second = post(env, CONFIRM)
    assert second.status_code == 409 and "already" in second.json()["detail"]
    failed = run(audit.list_rows(env.store, kind="update_request_failed"))
    assert len(failed) == 1 and failed[0]["status"] == 409
    status = env.client.get("/api/v2/updates/status").json()
    assert status["request"]["id"] == got["id"] and status["request"]["phase"] == "requested"


@pytest.mark.parametrize("body", [{}, {"confirmed": True}, {"confirmed": "true", "confirm_text": "update"},
                                  {"confirmed": True, "confirm_text": "Update"},
                                  {"confirmed": True, "confirm_text": "update "}, "update", []])
def test_the_request_needs_the_json_true_and_the_typed_word(env, body, tmp_path):
    env.login()
    r = env.client.post("/api/admin/updates/observe", json=body, headers=env.csrf)
    assert r.status_code == 400, r.text
    assert not (tmp_path / "update" / "request.json").exists()
    assert run(audit.list_rows(env.store, kind="update_request_failed"))[0]["status"] == 400


def test_a_viewer_a_missing_csrf_and_no_login_are_refused(env, tmp_path):
    assert post(env, CONFIRM, headers={}).status_code == 401
    env.login()
    assert post(env, CONFIRM, headers={}).status_code == 403
    assert post(env, CONFIRM, headers={"X-CSRF-Token": "nope"}).status_code == 403
    env.login("bob", admin=False)
    assert post(env, CONFIRM).status_code == 403
    assert not (tmp_path / "update" / "request.json").exists()
    assert run(audit.list_rows(env.store, kind="update_requested")) == []


def test_status_reflects_the_helpers_progress_and_a_finished_update_frees_the_button(env, tmp_path):
    env.login()
    base = tmp_path / "update"
    assert post(env, CONFIRM).status_code == 202
    # The helper takes the request (moves the file) and reports progress.
    (base / "request.json").rename(base / "request.abc.json")
    _state(base, phase="build", updated_at=env.wall.now - 5, log=["building"])
    status = env.client.get("/api/v2/updates/status").json()
    assert status["request"]["phase"] == "build" and status["state"]["phase"] == "build"
    assert status["state"]["log"] == ["building"] and status["state"]["stale"] is False
    assert post(env, CONFIRM).status_code == 409
    _state(base, phase="done", updated_at=env.wall.now - 1)
    status = env.client.get("/api/v2/updates/status").json()
    assert status["request"] is None and status["state"]["phase"] == "done"
    assert status["state"]["new_commit"].startswith("9f2c1d0")
    assert post(env, CONFIRM).status_code == 202


def test_a_broken_state_file_is_shown_as_failed_not_done(env, tmp_path):
    env.login()
    base = tmp_path / "update"
    base.mkdir()
    (base / "state.json").write_text('{"phase": "<b>done</b>", "log": "x"}', encoding="utf-8")
    status = env.client.get("/api/v2/updates/status").json()
    assert status["state"]["phase"] == "failed" and status["request"] is None


def test_the_upstream_check_runs_through_the_status_route_when_it_is_on(tmp_path):
    e = ApiEnv(tmp_path, update_dir=str(tmp_path / "update"), update_check=True)
    try:
        fake = Fake(GOOD)
        e.app.state.v2_runtime.update_check.fetch = fake
        e.login()
        got = e.client.get("/api/v2/updates/status").json()["github"]
        assert got["enabled"] and got["ok"] and got["latest_tag"] == "v2026.9.23.6"
        assert got["checked_at"] is not None
        e.client.get("/api/v2/updates/status")
        assert len(fake.calls) == 2
    finally:
        e.close()


def test_agents_lists_pushed_hosts_with_their_control_state(env):
    from observe.ingest.keys import create_key

    env.login()
    env.push(host_batch("nas01").model_copy(update={"agent_version": "1.4.0"}))
    env.push(host_batch("win01").model_copy(update={"platform": "windows", "agent_version": "1.3.0"}))
    _plain, _prefix = run(create_key(env.store, "nas01", "test", scope="wpc"))
    rows = {r["host"]: r for r in env.client.get("/api/v2/updates/agents").json()["items"]}
    assert set(rows) == {"nas01", "win01"}
    nas = rows["nas01"]
    assert nas["control"] is True and nas["control_pulled"] is False and nas["eligible"] is False
    assert nas["reason"] == "the control daemon has never pulled" and nas["agent_version"] == "1.4.0"
    win = rows["win01"]
    assert win["control"] is False and win["reason"] == "install command only"
    run(env.store.execute("UPDATE ingest_keys SET last_used=? WHERE host='nas01'", (env.wall.now - 7,)))
    nas = {r["host"]: r for r in env.client.get("/api/v2/updates/agents").json()["items"]}["nas01"]
    assert nas["eligible"] is True and nas["pull_age_s"] == pytest.approx(7.0) and nas["last_pull"]


def test_new_files_use_lf_and_no_em_dashes_or_model_names():
    from .test_field_docs import ROOT, stored_bytes

    for rel in ("observe/updates.py", "observe/api/updates.py", "tests/test_updates.py"):
        raw = stored_bytes(ROOT / rel)
        text = raw.decode("utf-8")
        assert b"\r" not in raw and chr(0x2014) not in text, rel
        names = ("cla" + "ude", "op" + "us", "son" + "net", "hai" + "ku")
        assert not any(w in text.lower() for w in names), rel
