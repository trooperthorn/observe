import asyncio
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from winrm.exceptions import InvalidCredentialsError

from watchpost.config import Config
from watchpost.discovery import (DiscoveryError, Discoverer, HostFinding, discover,
                                 expand_targets)

from .conftest import MQTT_PORT, SNMP_PORT, needs_mqtt, needs_snmpd
from .test_net import _self_signed

# ---------------------------------------------------------------- targets


def test_expand_cidr_range_ip_hostname_exclude_and_dedupe():
    got = expand_targets(["192.0.2.0/30", "192.0.2.10-12", "192.0.2.1", "NAS.lab.example"],
                         ["192.0.2.11"], 100)
    assert got == ["192.0.2.1", "192.0.2.2", "192.0.2.10", "192.0.2.12", "nas.lab.example"]


def test_expand_full_range_form_and_single_host_cidr():
    assert expand_targets(["198.51.100.254-198.51.101.1"], [], 10) == [
        "198.51.100.254", "198.51.100.255", "198.51.101.0", "198.51.101.1"]
    assert expand_targets(["192.0.2.7/32"], [], 10) == ["192.0.2.7"]


def test_expand_refuses_oversized_before_allocating():
    with pytest.raises(DiscoveryError, match="max_hosts"):
        expand_targets(["10.0.0.0/8"], [], 4096)
    with pytest.raises(DiscoveryError, match="max_hosts"):
        expand_targets(["192.0.2.0/25", "192.0.2.128/25"], [], 200)


def test_unknown_discovery_credential_rejected():
    with pytest.raises(ValueError, match="discovery: unknown credential"):
        Config.model_validate({"discovery": {"credentials": ["ghost"]}})


# -------------------------------------------------- loopback integration


def cfg(extra_monitors=(), **disc):
    return Config.model_validate({
        "credentials": {
            "v2": {"type": "snmpv2c", "community": "testcomm"},
            "v2wrong": {"type": "snmpv2c", "community": "nope"},
            "win": {"type": "winrm", "username": "LAB\\svc", "password": "pw"},
        },
        "discovery": {"credentials": ["v2wrong", "v2"], "snmp_port": SNMP_PORT,
                      "tcp_ports": [MQTT_PORT], "mqtt_ports": [MQTT_PORT], "timeout": 1,
                      **disc},
        "monitors": list(extra_monitors),
    })


@needs_snmpd
@needs_mqtt
async def test_discovery_proposals_load_as_valid_config_and_dedupe():
    base = cfg([{"name": "already", "type": "snmp", "host": "127.0.0.1", "port": SNMP_PORT,
                 "credential": "v2", "mode": "uptime"}])
    text, report = await discover(base, ["127.0.0.1"])
    proposed = yaml.safe_load(text)["monitors"]
    kinds = {(m["type"], m.get("mode")) for m in proposed}
    assert ("snmp", "memory") in kinds and ("mqtt", None) in kinds
    assert ("snmp", "uptime") not in kinds                       # de-duplicated
    assert report["stats"]["skipped"] == 1
    assert all(m.get("credential") != "v2wrong" for m in proposed)  # fell through to v2
    merged = base.model_dump(exclude_none=True)
    merged["monitors"] += proposed
    Config.model_validate(merged)                                 # proposals are loadable


# ---------------------------------------------------------- TLS / HTTP


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def https_server(tmp_path):
    cp, kp = _self_signed(tmp_path, 120)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cp, kp)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], str(cp)
    srv.shutdown()


async def test_https_valid_by_name_invalid_by_bare_ip(https_server):
    port, ca = https_server
    c = cfg(tcp_ports=[port], credentials=[], ca_bundle=ca)
    d = Discoverer(c)
    by_name = await d.scan_host("localhost")
    assert by_name.tls[port]["valid"] and by_name.http[port]["status"] == 204
    by_ip = await d.scan_host("127.0.0.1")
    assert not by_ip.tls[port]["valid"]
    text, _ = await discover(c, ["localhost"])
    ms = {m["type"]: m for m in yaml.safe_load(text)["monitors"]}
    assert ms["http"]["url"] == f"https://localhost:{port}/"
    assert ms["http"]["expect_status"] == [204] and ms["http"]["ca_bundle"] == ca
    assert ms["tls_cert"]["host"] == "localhost" and "verify" not in ms["tls_cert"]


# ---------------------------------------------------------------- WinRM


PROBE_JSON = ('{"computer":"dc01","domain":"lab.example","os":"Windows Server 2025",'
              '"disks":"C:","services":["NTDS","Netlogon","DNS"]}')


async def test_winrm_only_after_tls_validates_and_proposes_roles(https_server, monkeypatch):
    port, ca = https_server
    calls = []

    async def fake_run_ps(self, script):
        calls.append(self.monitor.host)
        return 0, PROBE_JSON, ""

    monkeypatch.setattr("watchpost.checks.windows.WinRMCheck.run_ps", fake_run_ps)
    c = cfg(tcp_ports=[port], winrm_port=port, credentials=["win"], ca_bundle=ca)

    # Bare IP: certificate can not validate for a name, so nothing is sent.
    f = await Discoverer(c).scan_host("127.0.0.1")
    assert calls == [] and f.winrm is None
    assert any("credentials not sent" in n for n in f.notes)

    text, _ = await discover(c, ["localhost"])
    assert calls == ["localhost"]
    ms = yaml.safe_load(text)["monitors"]
    services = {m.get("service") for m in ms if m.get("mode") == "service"}
    assert services == {"NTDS", "Netlogon", "DNS"}
    disks = [m for m in ms if m.get("mode") == "disk"]
    assert disks[0]["disk"] == "C:" and disks[0]["forecast"] is True   # scalar became list
    assert all(m["group"] == "windows" for m in ms)


async def test_wrong_winrm_password_is_retired_before_lockout(https_server, monkeypatch):
    port, ca = https_server
    attempts = 0

    async def reject(self, script):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.05)   # make overlap possible if serialisation were missing
        raise InvalidCredentialsError("401")

    monkeypatch.setattr("watchpost.checks.windows.WinRMCheck.run_ps", reject)
    c = cfg(tcp_ports=[port], winrm_port=port, credentials=["win"], ca_bundle=ca,
            max_auth_failures=3)
    d = Discoverer(c)
    findings = [HostFinding("localhost", open_ports=[port]) for _ in range(12)]
    await asyncio.gather(*(d._winrm(f) for f in findings))
    assert attempts == 3
    assert d.auth_failures["win"] == 3


async def test_tcp_probe_races_address_families(monkeypatch):
    """A name such as localhost resolves to ::1 and 127.0.0.1. The probe must
    start the next address after a short delay rather than spend its whole
    timeout on a first address that is filtered or slow to refuse."""
    import watchpost.discovery as discovery_mod
    seen = {}

    async def fake_open(host, port, **kw):
        seen.update(kw)
        raise OSError("refused")

    monkeypatch.setattr(discovery_mod.asyncio, "open_connection", fake_open)
    f = HostFinding("localhost")
    await Discoverer(cfg(credentials=[]))._tcp(f, 1)
    assert f.open_ports == []
    assert 0 < seen["happy_eyeballs_delay"] < 1
