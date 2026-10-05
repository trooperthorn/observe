"""Linux and Docker checks and SSH discovery against the loopback sshd from
tests/sshd.sh. Docker is the fake CLI in tests/fixtures/fake-docker."""

import asyncio
import os
import socket

import pytest
import yaml

from observe.checks import build_check
from observe.checks.base import Result
from observe.config import Config
from observe.discovery import Discoverer, HostFinding, discover

D = "/tmp/wp-ssh"


def _sshd_up():
    try:
        with socket.create_connection(("127.0.0.1", 2222), timeout=1):
            return os.path.exists(f"{D}/client_ed25519")
    except OSError:
        return False


if os.environ.get("OBSERVE_REQUIRE_SERVICES") == "1":
    assert _sshd_up(), "OBSERVE_REQUIRE_SERVICES=1 but test sshd is not running"
pytestmark = pytest.mark.skipif(not _sshd_up(), reason="test sshd not running")

CREDS = {
    "key": {"type": "ssh", "username": "wptest", "private_key": f"{D}/client_ed25519"},
    "pw": {"type": "ssh", "username": "wptest", "password": "wp-test-pass-1"},
    "badpw": {"type": "ssh", "username": "wptest", "password": "wrong-password"},
}


def check(**kw):
    mon = {"name": "x", "host": "127.0.0.1", "port": 2222, "credential": "key",
           "known_hosts": f"{D}/known_hosts", **kw}
    cfg = Config.model_validate({"defaults": {"timeout": 4}, "credentials": CREDS,
                                 "monitors": [mon]})
    return build_check(cfg.monitors[0], cfg)


# ------------------------------------------------------------------ linux


@pytest.mark.parametrize("mode", ["cpu", "memory", "load"])
async def test_linux_percent_modes(mode):
    res = await check(type="linux", mode=mode).run()
    assert res.result is Result.OK and 0 <= res.value <= 100 * 64, res.message


async def test_linux_disk_root_and_non_mount():
    ok = await check(type="linux", mode="disk", mount="/").run()
    assert ok.result is Result.OK and 0 < ok.value <= 100
    bad = await check(type="linux", mode="disk", mount="/tmp/wp-ssh").run()
    assert bad.result is Result.FAIL and "not a mount point" in bad.message


async def test_linux_password_credential_with_verified_key():
    res = await check(type="linux", mode="uptime", credential="pw").run()
    assert res.result is Result.OK


async def test_linux_command_mode():
    res = await check(type="linux", mode="command", command="echo 41; echo 42").run()
    assert res.result is Result.OK and res.value == 42


async def test_host_key_must_be_known(tmp_path):
    kh = tmp_path / "kh"
    kh.write_text("")
    res = await check(type="linux", mode="uptime", known_hosts=str(kh)).run()
    assert res.result is Result.FAIL and "host key" in res.message


async def test_changed_host_key_fails(tmp_path):
    kh = tmp_path / "kh"
    kh.write_text(other_key_line())
    res = await check(type="linux", mode="uptime", known_hosts=str(kh)).run()
    assert res.result is Result.FAIL and "host key" in res.message


def other_key_line(hostspec="[127.0.0.1]:2222"):
    """A valid ed25519 public key that is not the test server's."""
    import asyncssh
    pub = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().split()
    return f"{hostspec} {pub[0]} {pub[1]}\n"


def test_shell_metacharacters_rejected_at_load():
    for bad in ({"type": "linux", "mode": "service", "service": "ssh; id"},
                {"type": "linux", "mode": "disk", "mount": "/$(id)"},
                {"type": "docker", "container": "web && id"},
                {"type": "docker", "container": "web", "docker_command": "docker|sh"}):
        with pytest.raises(ValueError):
            check(**bad)


# ----------------------------------------------------------------- docker


def dcheck(**kw):
    return check(type="docker", docker_command=f"{D}/docker", **kw)


async def test_docker_container_states():
    assert (await dcheck(container="web").run()).result is Result.OK
    db = await dcheck(container="db").run()
    assert db.result is Result.FAIL and "unhealthy" in db.message
    worker = await dcheck(container="worker").run()
    assert worker.result is Result.FAIL and "exited (exit 137)" in worker.message
    gone = await dcheck(container="missing").run()
    assert gone.result is Result.FAIL and "No such container" in gone.message


async def test_docker_restart_count_increase_warns():
    c = dcheck(container="web")
    with open(f"{D}/web-restarts", "w") as fh:
        fh.write("0")
    assert (await c.run()).result is Result.OK
    with open(f"{D}/web-restarts", "w") as fh:
        fh.write("2")
    res = await c.run()
    assert res.result is Result.WARN and "restarted 2x" in res.message
    with open(f"{D}/web-restarts", "w") as fh:
        fh.write("0")


async def test_docker_summary_counts_only_real_problems():
    res = await dcheck(mode="summary").run()
    # oneshot is exited with restart=no: expected, not a problem
    assert res.result is Result.FAIL and res.value == 2
    assert res.detail["problems"] == ["db unhealthy", "worker exited (restart=unless-stopped)"]


# -------------------------------------------------------------- discovery


def dcfg(known_hosts, creds=("pw", "key"), **extra):
    return Config.model_validate({
        "credentials": CREDS,
        "discovery": {"credentials": list(creds), "ssh_port": 2222, "tcp_ports": [],
                      "ssh_known_hosts": known_hosts, "docker_command": f"{D}/docker",
                      "timeout": 2, **extra},
    })


async def test_tofu_uses_key_only_records_host_key_and_proposes(tmp_path):
    kh = tmp_path / "kh"
    kh.write_text("")
    text, report = await discover(dcfg(str(kh)), ["127.0.0.1"])
    ms = yaml.safe_load(text)["monitors"]
    assert {m["credential"] for m in ms if "credential" in m} == {"key"}  # password withheld
    assert report["new_ssh_host_keys"][0].startswith("[127.0.0.1]:2222 ssh-ed25519 ")
    modes = {(m["type"], m.get("mode")) for m in ms}
    assert {("linux", "cpu"), ("linux", "memory"), ("linux", "disk"),
            ("docker", "summary"), ("docker", "container")} <= modes
    assert {m["container"] for m in ms if m.get("container")} == {"web", "db"}
    merged = dcfg(str(kh)).model_dump(exclude_none=True)
    merged["monitors"] = ms
    Config.model_validate(merged)


async def test_tofu_off_sends_nothing(tmp_path):
    kh = tmp_path / "kh"
    kh.write_text("")
    f = await Discoverer(dcfg(str(kh), ssh_trust_on_first_use=False)).scan_host("127.0.0.1")
    assert f.ssh is None and any("trust-on-first-use is off" in n for n in f.notes)


async def test_host_key_mismatch_stops_and_flags(tmp_path):
    kh = tmp_path / "kh"
    kh.write_text(other_key_line())
    f = await Discoverer(dcfg(str(kh))).scan_host("127.0.0.1")
    assert f.ssh is None and f.ssh_new_host_key is None
    assert any("HOST KEY MISMATCH" in n for n in f.notes)


async def test_malformed_known_hosts_entry_is_not_treated_as_absent(tmp_path):
    kh = tmp_path / "kh"
    kh.write_text("[127.0.0.1]:2222 ssh-ed25519 not-a-valid-key\n")
    f = await Discoverer(dcfg(str(kh))).scan_host("127.0.0.1")
    assert f.ssh is None and f.ssh_new_host_key is None
    assert any("could not be parsed" in n for n in f.notes)


async def test_wrong_ssh_password_retired_before_lockout():
    d = Discoverer(dcfg(f"{D}/known_hosts", creds=("badpw",), max_auth_failures=2))
    findings = [HostFinding("127.0.0.1", open_ports=[2222]) for _ in range(6)]
    await asyncio.gather(*(d._ssh(f) for f in findings))
    assert d.auth_failures["badpw"] == 2
