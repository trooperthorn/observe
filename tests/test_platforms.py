"""TrueNAS, Proxmox, and vSphere.

vSphere runs against vcsim, VMware's govmomi simulator, which speaks the real
vSphere SOAP API; the test starts it itself. Set OBSERVE_VCSIM to its path
if it is not on PATH. Proxmox and TrueNAS run against the fakes in
tests/fakes/servers.py.
"""

import os
import shutil
import socket
import subprocess
import time

import pytest
import yaml

from observe.checks import build_check
from observe.checks.base import Result
from observe.config import Config
from observe.discovery import discover

from .fakes.servers import TrueNASFake, leaf_cert, proxmox_server

CREDS = {
    "pve": {"type": "proxmox", "token_id": "observe@pve!monitor",
            "secret": "11111111-2222-3333-4444-555555555555"},
    "pvebad": {"type": "proxmox", "token_id": "observe@pve!monitor", "secret": "nope"},
    "tn": {"type": "truenas", "username": "observe", "api_key": "1-abcdef"},
    "tnbad": {"type": "truenas", "username": "observe", "api_key": "wrong"},
    "vs": {"type": "vsphere", "username": "watch", "password": "good"},
    "vsbad": {"type": "vsphere", "username": "watch", "password": "bad"},
}


def check(**mon):
    cfg = Config.model_validate({"defaults": {"timeout": 5}, "credentials": CREDS,
                                 "monitors": [{"name": "x", **mon}]})
    return build_check(cfg.monitors[0], cfg)


@pytest.fixture
def cert(tmp_path):
    return leaf_cert(tmp_path)


def test_proxmox_token_id_format_enforced():
    with pytest.raises(ValueError):
        Config.model_validate({"credentials": {"p": {"type": "proxmox", "token_id": "no-bang",
                                                     "secret": "s"}}})


# ------------------------------------------------------------------ Proxmox


@pytest.fixture
def pve(cert):
    srv = proxmox_server(*cert)
    yield srv.server_address[1], cert[0]
    srv.shutdown()


def pcheck(port, ca, **kw):
    return check(type="proxmox", host="localhost", port=port, credential="pve", ca_bundle=ca,
                 **kw)


async def test_proxmox_node_modes_with_pinned_self_signed_leaf(pve):
    port, ca = pve
    node = await pcheck(port, ca, mode="node", node="pve1").run()
    assert node.result is Result.OK and node.value == 10.0
    cpu = await pcheck(port, ca, mode="node_cpu", node="pve1").run()
    assert cpu.value == 12.3
    mem = await pcheck(port, ca, mode="node_memory", node="pve1").run()
    assert mem.value == 37.5
    missing = await pcheck(port, ca, mode="node", node="pve9").run()
    assert missing.result is Result.FAIL


async def test_proxmox_guest_by_name_and_vmid(pve):
    port, ca = pve
    assert (await pcheck(port, ca, mode="guest", guest="dc01").run()).result is Result.OK
    ct = await pcheck(port, ca, mode="guest", guest="200").run()
    assert ct.result is Result.OK and "container pihole" in ct.message
    stopped = await pcheck(port, ca, mode="guest", guest="old-vm").run()
    assert stopped.result is Result.FAIL and "stopped" in stopped.message


async def test_proxmox_storage_and_auth(pve):
    port, ca = pve
    st = await pcheck(port, ca, mode="storage", storage="local-lvm", node="pve1").run()
    assert st.value == 75.0
    bad = await check(type="proxmox", host="localhost", port=port, credential="pvebad",
                      ca_bundle=ca, mode="node", node="pve1").run()
    assert bad.result is Result.FAIL and "401" in bad.message


async def test_proxmox_unpinned_certificate_fails(pve):
    port, _ = pve
    res = await check(type="proxmox", host="localhost", port=port, credential="pve",
                      mode="node", node="pve1").run()
    assert res.result is Result.FAIL and "certificate" in res.message.lower()


async def test_proxmox_token_without_rights_is_explained(cert):
    srv = proxmox_server(*cert, resources=[])
    try:
        res = await pcheck(srv.server_address[1], cert[0], mode="node", node="pve1").run()
        assert res.result is Result.FAIL and "PVEAuditor" in res.message
    finally:
        srv.shutdown()


def disc_cfg(creds, **extra):
    return Config.model_validate({"credentials": CREDS, "discovery": {
        "credentials": creds, "tcp_ports": [], "timeout": 2, **extra}})


async def test_proxmox_discovery_infers_guest_dependencies(pve):
    port, ca = pve
    cfg = disc_cfg(["pve"], proxmox_port=port, ca_bundle=ca)
    text, _ = await discover(cfg, ["localhost"])
    ms = [m for m in yaml.safe_load(text)["monitors"] if m["type"] == "proxmox"]
    guests = {m["guest"]: m for m in ms if m["mode"] == "guest"}
    assert set(guests) == {"100", "200"}                      # stopped and template skipped
    node_name = next(m["name"] for m in ms if m["mode"] == "node")
    assert all(g["depends_on"] == [node_name] for g in guests.values())
    storages = {(m["storage"], m.get("node")) for m in ms if m["mode"] == "storage"}
    assert storages == {("local-lvm", "pve1"), ("nas", None)}  # shared storage once
    merged = cfg.model_dump(exclude_none=True)
    merged["monitors"] = yaml.safe_load(text)["monitors"]
    Config.model_validate(merged)


async def test_discovery_sends_no_token_to_unvalidated_tls(pve):
    port, _ = pve
    text, report = await discover(disc_cfg(["pve"], proxmox_port=port), ["localhost"])
    assert not any(m["type"] == "proxmox" for m in yaml.safe_load(text)["monitors"] or [])
    assert f"localhost:{port}" in report["unverified_certs"]
    assert "BEGIN CERTIFICATE" in report["unverified_certs"][f"localhost:{port}"]["pem"]


# ------------------------------------------------------------------ TrueNAS


@pytest.fixture
def truenas(cert):
    fake = TrueNASFake(*cert).start()
    return fake, cert[0]


def tcheck(fake, ca, **kw):
    return check(type="truenas", host="localhost", port=fake.port, credential="tn",
                 ca_bundle=ca, **kw)


async def test_truenas_login_uses_login_ex_api_key_plain(truenas):
    fake, ca = truenas
    await tcheck(fake, ca, mode="pools").run()
    assert fake.logins[0] == {"mechanism": "API_KEY_PLAIN", "username": "observe",
                              "api_key": "1-abcdef"}


async def test_truenas_pools_and_pool(truenas):
    fake, ca = truenas
    pools = await tcheck(fake, ca, mode="pools").run()
    assert pools.result is Result.FAIL and "fast DEGRADED" in pools.message
    tank = await tcheck(fake, ca, mode="pool", pool="tank").run()
    assert tank.result is Result.OK and tank.value == 60.0
    missing = await tcheck(fake, ca, mode="pool", pool="nope").run()
    assert missing.result is Result.FAIL


async def test_truenas_alerts_levels_and_dismissed(truenas):
    fake, ca = truenas
    res = await tcheck(fake, ca, mode="alerts").run()
    assert res.result is Result.WARN and res.value == 1.0   # INFO below floor, CRITICAL dismissed
    res = await tcheck(fake, ca, mode="alerts", min_alert_level="INFO").run()
    assert res.value == 2.0


async def test_truenas_bad_key(truenas):
    fake, ca = truenas
    res = await check(type="truenas", host="localhost", port=fake.port, credential="tnbad",
                      ca_bundle=ca, mode="pools").run()
    assert res.result is Result.FAIL and "AUTH_ERR" in res.message


async def test_truenas_discovery(truenas):
    fake, ca = truenas
    text, _ = await discover(disc_cfg(["tn"], https_api_port=fake.port, ca_bundle=ca),
                             ["localhost"])
    ms = [m for m in yaml.safe_load(text)["monitors"] if m["type"] == "truenas"]
    assert {(m["mode"], m.get("pool")) for m in ms} == {
        ("pools", None), ("alerts", None), ("pool", "tank"), ("pool", "fast")}


# ------------------------------------------------------------------ vSphere

VCSIM = os.environ.get("OBSERVE_VCSIM") or shutil.which("vcsim") or \
    ("/tmp/vcsim" if os.path.exists("/tmp/vcsim") else None)
if os.environ.get("OBSERVE_REQUIRE_SERVICES") == "1":
    assert VCSIM, "OBSERVE_REQUIRE_SERVICES=1 but vcsim was not found"
needs_vcsim = pytest.mark.skipif(not VCSIM, reason="vcsim not available")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def vcsim(cert):
    port = _free_port()
    proc = subprocess.Popen([VCSIM, "-esx", "-l", f"127.0.0.1:{port}", "-username", "watch",
                             "-password", "good", "-tlscert", cert[0], "-tlskey", cert[1]],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield port, cert[0]
    proc.terminate()
    proc.wait(5)


def vcheck(port, ca, **kw):
    return check(type="vsphere", host="localhost", port=port, credential="vs", ca_bundle=ca,
                 **kw)


@needs_vcsim
async def test_vsphere_host_datastore_vm(vcsim):
    port, ca = vcsim
    host = await vcheck(port, ca, mode="host").run()
    assert host.result is Result.OK and "connected" in host.message
    for mode in ("host_cpu", "host_memory"):
        r = await vcheck(port, ca, mode=mode).run()
        assert r.result is Result.OK and 0 <= r.value <= 100
    ds = await vcheck(port, ca, mode="datastore", entity="LocalDS_0").run()
    assert ds.result is Result.OK and ds.value is not None
    vm = await vcheck(port, ca, mode="vm", entity="ha-host_VM0").run()
    assert vm.result is Result.OK and "powered on" in vm.message
    gone = await vcheck(port, ca, mode="vm", entity="nope").run()
    assert gone.result is Result.FAIL


@needs_vcsim
async def test_vsphere_bad_password_and_unpinned_cert(vcsim):
    port, ca = vcsim
    bad = await check(type="vsphere", host="localhost", port=port, credential="vsbad",
                      ca_bundle=ca, mode="host").run()
    assert bad.result is Result.FAIL and "authentication" in bad.message
    untrusted = await check(type="vsphere", host="localhost", port=port, credential="vs",
                            mode="host").run()
    assert untrusted.result is Result.FAIL and "did not validate" in untrusted.message


@needs_vcsim
async def test_vsphere_discovery_with_pinned_cert(vcsim):
    port, ca = vcsim
    cfg = disc_cfg(["vs"], https_api_port=port, ca_bundle=ca)
    text, _ = await discover(cfg, ["localhost"])
    ms = [m for m in yaml.safe_load(text)["monitors"] if m["type"] == "vsphere"]
    host_name = next(m["name"] for m in ms if m["mode"] == "host")
    vms = [m for m in ms if m["mode"] == "vm"]
    assert len(vms) == 2 and all(v["depends_on"] == [host_name] for v in vms)
    assert all(m["host"] == "localhost" and m["ca_bundle"] == ca for m in ms)
    merged = cfg.model_dump(exclude_none=True)
    merged["monitors"] = yaml.safe_load(text)["monitors"]
    Config.model_validate(merged)
