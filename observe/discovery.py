"""Discovery: scan targets, fingerprint what answers, propose monitors.

Discovery writes a proposal file. It never changes the running
configuration: you review the proposals and paste the ones you want into
observe.yaml, which stays the single source of truth.

Per host, in order:
  1. ICMP echo, a TCP connect sweep of `tcp_ports`, and a PTR lookup.
  2. SNMP: each SNMP credential in `discovery.credentials` is tried against
     UDP `snmp_port` (every address, since many SNMP-only devices block ICMP
     and have no TCP ports open). The first that answers is used to read
     sysName/sysDescr, interfaces, and whether CPU and RAM tables exist.
  3. WinRM, only if `winrm_port` is open. The listener's TLS certificate must
     validate for the host's DNS name before any credential is sent (see
     THREAT-MODEL.md for why). A credential that fails authentication
     `max_auth_failures` times is not tried again, and until a credential has
     succeeded once, attempts with it are made one host at a time, so parallel
     scanning can not overshoot that limit.
  4. TLS and HTTP on the remaining open ports, SSH banner on 22, and MQTT
     connect (anonymous first, then MQTT credentials) on `mqtt_ports`.

What is not inferred: dependencies (depends_on), which would need LLDP/CDP
neighbour tables; DNS monitors, which need a name you care about; and any
threshold other than the conservative defaults below. Review all of them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import json
import logging
import ssl
from dataclasses import asdict, dataclass, field
from typing import Any

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpx
import yaml
from cryptography import x509
from icmplib import async_ping
from icmplib.exceptions import ICMPLibError, SocketPermissionError
from winrm.exceptions import AuthenticationError, InvalidCredentialsError

import os

import asyncssh

from .checks.mqtt import MqttCheck
from .httpclient import http_client
from .tlscontext import http_default_context, socket_default_context
from cryptography.hazmat.primitives import hashes, serialization

from .checks.platforms import (DS_PATHS, HOST_PATHS, VM_PATHS, AuthFailed, TrueNASClient,
                               api_ssl_context, socket_api_context, vsphere_collect)
from .checks.apps import unifi_list_all
from .checks.platforms import VSPHERE_POOL, proxmox_resources
from .checks.ssh import docker_argv, ssh_connect, ssh_run
from .checks.snmp import (HR_PROCESSOR_LOAD, HR_STORAGE_RAM, HR_STORAGE_TYPE, IF_HIGH_SPEED,
                          IF_NAME, IF_OPER_STATUS, SnmpCheck, SnmpError)
from .checks.windows import WinRMCheck
from pydantic import TypeAdapter

from .config import (_REF, Config, DiscoverySettings, Monitor, MqttMonitor, SnmpMonitor,
                     WinRMMonitor)
from .directory import DirectoryComputer, fetch_computers_async

log = logging.getLogger("observe.discovery")

# A name that resolves to several addresses (localhost is ::1 and 127.0.0.1) must
# not spend the whole probe timeout on an address that never answers or, as on
# Windows, takes about two seconds to refuse. Start the next address after the
# RFC 8305 recommended delay instead of waiting for the first one to fail.
HAPPY_EYEBALLS_DELAY = 0.25

SYS_DESCR = ".1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = ".1.3.6.1.2.1.1.2.0"
SYS_NAME = ".1.3.6.1.2.1.1.5.0"
IF_TYPE = ".1.3.6.1.2.1.2.2.1.3"
# IANAifType values worth watching as links: ethernetCsmacd, fastEther,
# gigabitEthernet, ieee80211, ieee8023adLag. Loopbacks, VLAN and tunnel
# interfaces are left out.
LINK_IF_TYPES = {6, 62, 117, 71, 161}

# Windows services that mark a server role worth watching when present and
# set to start automatically.
ROLE_SERVICES = ["NTDS", "Netlogon", "Kdc", "ADWS", "DNS", "DFSR", "DHCPServer", "CertSvc",
                 "W3SVC", "MSSQLSERVER", "SQLSERVERAGENT", "vmms", "WSUSService"]

# systemd units that mark a Linux role worth watching when running.
ROLE_UNITS = ["nginx", "apache2", "httpd", "caddy", "haproxy", "postgresql", "mariadb", "mysql",
              "redis-server", "docker", "containerd", "smbd", "nfs-server", "named", "unbound",
              "pihole-FTL", "mosquitto", "k3s", "kubelet", "chrony", "chronyd", "slapd",
              "sssd", "zfs-zed"]
SKIP_MOUNT_PREFIXES = ("/boot", "/snap", "/run", "/var/lib/docker", "/var/lib/containers")


def linux_probe(docker_command: str) -> str:
    fs_excl = " ".join(f"-x {t}" for t in ("tmpfs", "devtmpfs", "overlay", "squashfs",
                                           "efivarfs", "nsfs"))
    dps = docker_argv(docker_command, "ps", "--no-trunc", "--format", "{{json .}}")
    return (
        'echo "os=$( . /etc/os-release 2>/dev/null; echo "$PRETTY_NAME")"; '
        'echo "kernel=$(uname -sr)"; echo "fqdn=$(hostname -f 2>/dev/null || hostname)"; '
        'echo "nproc=$(nproc 2>/dev/null)"; '
        f'echo "---df"; df -P -k {fs_excl} 2>/dev/null | tail -n +2 || df -P -k / | tail -n +2; '
        'echo "---units"; systemctl list-units --type=service --state=running --no-legend '
        "--plain 2>/dev/null | awk '{print $1}'; "
        f'echo "---docker"; {dps} 2>&1; echo "docker_rc=$?"'
    )


def parse_linux_probe(out: str) -> dict[str, Any]:
    info: dict[str, Any] = {"mounts": [], "units": [], "containers": [],
                            "docker": "absent"}
    section = "head"
    docker_lines: list[str] = []
    for line in out.splitlines():
        if line.startswith("---"):
            section = line[3:]
            continue
        if section == "head" and "=" in line:
            k, _, v = line.partition("=")
            info[k] = v.strip()
        elif section == "df":
            parts = line.split()
            if len(parts) >= 6 and parts[5].startswith("/") and \
                    not parts[5].startswith(SKIP_MOUNT_PREFIXES):
                info["mounts"].append(parts[5])
        elif section == "units":
            unit = line.strip().removesuffix(".service")
            if unit in ROLE_UNITS:
                info["units"].append(unit)
        elif section == "docker":
            if line.startswith("docker_rc="):
                rc = int(line.split("=", 1)[1] or 1)
                text = "\n".join(docker_lines)
                if rc == 0:
                    info["docker"] = "ok"
                    for dl in docker_lines:
                        try:
                            c = json.loads(dl)
                        except json.JSONDecodeError:
                            continue
                        info["containers"].append({"name": c.get("Names", ""),
                                                   "image": c.get("Image", ""),
                                                   "status": c.get("Status", "")})
                elif rc == 127 or "not found" in text:
                    info["docker"] = "absent"
                elif "permission denied" in text.lower():
                    info["docker"] = "denied"
                else:
                    info["docker"] = f"error: {text[:120]}"
            else:
                docker_lines.append(line)
    info["mounts"] = list(dict.fromkeys(info["mounts"]))[:6]
    return info


WINRM_PROBE = (
    "$cs = Get-CimInstance Win32_ComputerSystem; $os = Get-CimInstance Win32_OperatingSystem; "
    "$d = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | "
    "Select-Object -ExpandProperty DeviceID); "
    "$s = @(Get-Service -Name @(" + ", ".join(f"'{n}'" for n in ROLE_SERVICES) + ") "
    "-ErrorAction SilentlyContinue | Where-Object { $_.StartType -eq 'Automatic' } | "
    "Select-Object -ExpandProperty Name); "
    "[pscustomobject]@{ computer = $cs.DNSHostName; domain = $cs.Domain; "
    "os = $os.Caption; disks = $d; services = $s } | ConvertTo-Json -Compress"
)


class DiscoveryError(Exception):
    pass


# ------------------------------------------------------------------ targets


def expand_targets(targets: list[str], exclude: list[str], max_hosts: int) -> list[str]:
    """Expand CIDRs, a-b ranges, IPs, and hostnames, in order, de-duplicated.

    The size limit is checked before anything is materialised, so a typo like
    10.0.0.0/8 fails fast instead of allocating sixteen million entries.
    """

    def one(spec: str, budget: int) -> list[str]:
        spec = spec.strip()
        if "/" in spec:
            net = ipaddress.ip_network(spec, strict=False)
            usable = net.num_addresses - (2 if net.version == 4 and net.prefixlen < 31 else 0)
            if usable > budget:
                raise DiscoveryError(f"{spec} has {usable} addresses; max_hosts is {max_hosts}")
            return [str(a) for a in net.hosts()] or [str(net.network_address)]
        if "-" in spec:
            lo_s, hi_s = (p.strip() for p in spec.split("-", 1))
            lo = ipaddress.ip_address(lo_s)
            hi = ipaddress.ip_address(hi_s) if "." in hi_s or ":" in hi_s \
                else ipaddress.ip_address(lo_s.rsplit(".", 1)[0] + "." + hi_s)
            if hi < lo:
                raise DiscoveryError(f"{spec}: range end is before start")
            count = int(hi) - int(lo) + 1
            if count > budget:
                raise DiscoveryError(f"{spec} has {count} addresses; max_hosts is {max_hosts}")
            return [str(ipaddress.ip_address(int(lo) + i)) for i in range(count)]
        try:
            return [str(ipaddress.ip_address(spec))]
        except ValueError:
            return [spec.lower()]  # hostname

    excluded: set[str] = set()
    for spec in exclude:
        excluded.update(one(spec, 1 << 32))
    out: list[str] = []
    seen: set[str] = set()
    for spec in targets:
        for addr in one(spec, max_hosts - len(out)):
            if addr not in seen and addr not in excluded:
                seen.add(addr)
                out.append(addr)
    if len(out) > max_hosts:
        raise DiscoveryError(f"targets expand to {len(out)} hosts; max_hosts is {max_hosts}")
    return out


# ----------------------------------------------------------------- findings


@dataclass
class HostFinding:
    address: str
    ptr: str | None = None
    icmp: bool = False
    rtt_ms: float | None = None
    open_ports: list[int] = field(default_factory=list)
    snmp: dict[str, Any] | None = None
    winrm: dict[str, Any] | None = None
    tls: dict[int, dict[str, Any]] = field(default_factory=dict)
    http: dict[int, dict[str, Any]] = field(default_factory=dict)
    ssh_banner: str | None = None
    mqtt: dict[int, dict[str, Any]] = field(default_factory=dict)
    ssh: dict[str, Any] | None = None
    ssh_new_host_key: str | None = None  # known_hosts line recorded on first use
    directory: dict[str, Any] | None = None
    proxmox: dict[str, Any] | None = None
    homeassistant: dict[str, Any] | None = None
    unifi: dict[str, Any] | None = None
    technitium: dict[str, Any] | None = None
    vsphere: dict[str, Any] | None = None
    truenas: dict[str, Any] | None = None
    unverified_certs: dict[int, dict[str, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def responded(self) -> bool:
        return bool(self.icmp or self.open_ports or self.snmp)

    def api_host(self, port: int) -> str:
        """Name for an API monitor: the DNS name when its certificate
        validated against it, else the address as scanned."""
        tls = self.tls.get(port) or {}
        return (self.dns_name or self.address) if tls.get("valid") else self.address

    @property
    def is_ip(self) -> bool:
        try:
            ipaddress.ip_address(self.address)
            return True
        except ValueError:
            return False

    @property
    def dns_name(self) -> str | None:
        """Name for TLS and WinRM, which need one that matches a certificate:
        the target itself if it was given as a hostname, else its PTR record."""
        return self.ptr if self.is_ip else self.address

    @property
    def label(self) -> str:
        vs_hosts = (self.vsphere or {}).get("hosts") or []
        px_nodes = (self.proxmox or {}).get("nodes") or []
        for candidate in ((self.snmp or {}).get("sys_name"),
                          (self.winrm or {}).get("computer"),
                          px_nodes[0] if len(px_nodes) == 1 else None,
                          vs_hosts[0] if len(vs_hosts) == 1 else None,
                          (self.ssh or {}).get("fqdn"), self.ptr,
                          None if self.is_ip else self.address):
            if candidate:
                return str(candidate).split(".")[0]
        return self.address


# ------------------------------------------------------------------- scanner


class Discoverer:
    def __init__(self, config: Config, settings: DiscoverySettings | None = None) -> None:
        self.config = config
        self.s = settings or config.discovery
        creds = [(n, config.credentials[n]) for n in self.s.credentials]
        self.snmp_creds = [n for n, c in creds if c.type in ("snmpv2c", "snmpv3")]
        self.winrm_creds = [n for n, c in creds if c.type == "winrm"]
        self.mqtt_creds = [n for n, c in creds if c.type == "mqtt"]
        self.ssh_creds = [n for n, c in creds if c.type == "ssh"]
        self.truenas_creds = [n for n, c in creds if c.type == "truenas"]
        self.proxmox_creds = [n for n, c in creds if c.type == "proxmox"]
        self.vsphere_creds = [n for n, c in creds if c.type == "vsphere"]
        self.ha_creds = [n for n, c in creds if c.type == "homeassistant"]
        self.unifi_creds = [n for n, c in creds if c.type == "unifi"]
        self.technitium_creds = [n for n, c in creds if c.type == "technitium"]
        lockable = (self.winrm_creds + self.ssh_creds + self.truenas_creds
                    + self.proxmox_creds + self.vsphere_creds + self.ha_creds
                    + self.unifi_creds + self.technitium_creds)
        self.auth_failures = {n: 0 for n in lockable}
        self.proven: set[str] = set()
        self._cred_locks = {n: asyncio.Lock() for n in lockable}
        kh = self.s.ssh_known_hosts
        self.known_hosts = asyncssh.read_known_hosts(kh) if os.path.exists(kh) else None
        self._icmp_denied = False
        self.stats = {"scanned": 0, "responded": 0}

    # ------------------------------------------------------------- probes

    async def _ping(self, f: HostFinding) -> None:
        if self._icmp_denied:
            return
        try:
            host = await async_ping(f.address, count=2, interval=0.2, timeout=self.s.timeout,
                                    privileged=False)
        except SocketPermissionError:
            self._icmp_denied = True
            log.warning("unprivileged ICMP not permitted; ping sweep skipped "
                        "(set sysctl net.ipv4.ping_group_range)")
            return
        except ICMPLibError as err:
            f.notes.append(f"ping error: {err}")
            return
        f.icmp, f.rtt_ms = host.is_alive, (host.avg_rtt if host.is_alive else None)

    async def _tcp(self, f: HostFinding, port: int) -> None:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(f.address, port,
                                                                  happy_eyeballs_delay=HAPPY_EYEBALLS_DELAY),
                                          self.s.timeout)
        except (OSError, asyncio.TimeoutError):
            return
        f.open_ports.append(port)
        w.close()
        try:
            await w.wait_closed()
        except OSError:
            pass

    async def _ptr(self, f: HostFinding) -> None:
        if not f.is_ip:
            return
        r = dns.asyncresolver.Resolver()
        r.lifetime = self.s.timeout
        try:
            ans = await r.resolve_address(f.address)
            f.ptr = str(ans[0]).rstrip(".").lower()
        except (dns.exception.DNSException, dns.resolver.NoNameservers, IndexError):
            pass

    async def _snmp(self, f: HostFinding) -> None:
        for cred in self.snmp_creds:
            mon = SnmpMonitor(name="discovery", type="snmp", host=f.address,
                              port=self.s.snmp_port, credential=cred, mode="uptime",
                              timeout=self.s.timeout)
            chk = SnmpCheck(mon, self.config)
            try:
                sysv = await chk.get(SYS_NAME, SYS_DESCR, SYS_OBJECT_ID)
            except SnmpError:
                continue
            if not sysv:
                continue
            info: dict[str, Any] = {"credential": cred, "sys_name": sysv.get(SYS_NAME) or None,
                                    "sys_descr": (sysv.get(SYS_DESCR) or "")[:160],
                                    "sys_object_id": sysv.get(SYS_OBJECT_ID)}
            try:
                names = await chk.walk(IF_NAME)
                status = await chk.walk(IF_OPER_STATUS)
                types = await chk.walk(IF_TYPE)
                speeds = await chk.walk(IF_HIGH_SPEED)
                info["interfaces_total"] = len(status)
                links = []
                for idx, st in status.items():
                    if st != "1" or int(types.get(idx, "0") or 0) not in LINK_IF_TYPES:
                        continue
                    links.append({"index": idx, "name": names.get(idx) or idx,
                                  "speed_mbps": int(speeds.get(idx, "0") or 0)})
                links.sort(key=lambda i: (-i["speed_mbps"], i["name"]))
                info["links_up"] = links
                info["has_cpu"] = bool(await chk.walk(HR_PROCESSOR_LOAD))
                info["has_ram"] = HR_STORAGE_RAM in (await chk.walk(HR_STORAGE_TYPE)).values()
            except SnmpError as err:
                f.notes.append(f"SNMP answered but table walk failed: {err}")
            f.snmp = info
            return

    async def _tls_probe(self, host: str, port: int, sni: str | None) -> dict[str, Any] | None:
        """Handshake without validation to learn whether the port speaks TLS,
        then again with validation to learn whether a client would trust it."""
        loose = socket_default_context(False, None)
        try:
            _, w = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=loose, server_hostname=sni,
                                        happy_eyeballs_delay=HAPPY_EYEBALLS_DELAY),
                self.s.timeout + 1)
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            return None
        der = w.get_extra_info("ssl_object").getpeercert(binary_form=True)
        w.close()
        try:
            await w.wait_closed()
        except (OSError, ssl.SSLError):
            pass
        cert = x509.load_der_x509_certificate(der)
        info: dict[str, Any] = {
            "subject": cert.subject.rfc4514_string(),
            "not_after": cert.not_valid_after_utc.isoformat(),
            "sha256": cert.fingerprint(hashes.SHA256()).hex(":").upper(),
            "pem": cert.public_bytes(serialization.Encoding.PEM).decode(),
            "valid": False, "reason": "no DNS name to validate against",
        }
        if sni is None:
            return info
        strict = socket_api_context(True, self.s.ca_bundle)
        try:
            _, w = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=strict, server_hostname=sni,
                                        happy_eyeballs_delay=HAPPY_EYEBALLS_DELAY),
                self.s.timeout + 1)
            w.close()
            info.update(valid=True, reason="chain and hostname validate")
        except ssl.SSLCertVerificationError as err:
            info["reason"] = err.verify_message
        except (OSError, asyncio.TimeoutError, ssl.SSLError) as err:
            info["reason"] = f"validation handshake failed: {err}"
        return info

    async def _http_status(self, url: str, verify: bool | ssl.SSLContext) -> int | None:
        try:
            async with http_client(verify, self.s.timeout + 2) as c:
                return (await c.get(url)).status_code
        except httpx.HTTPError:
            return None

    async def _web(self, f: HostFinding, port: int) -> None:
        sni = f.dns_name
        tls = await self._tls_probe(f.address, port, sni)
        if tls is not None:
            f.tls[port] = tls
            host = sni if tls["valid"] else (sni or f.address)
            verify: bool | ssl.SSLContext = False
            if tls["valid"]:
                verify = http_default_context(True, self.s.ca_bundle)
            status = await self._http_status(f"https://{host}:{port}/", verify)
            if status is not None:
                f.http[port] = {"scheme": "https", "host": host, "status": status}
            return
        status = await self._http_status(f"http://{f.address}:{port}/", False)
        if status is not None:
            f.http[port] = {"scheme": "http", "host": f.address, "status": status}

    async def _ssh_banner(self, f: HostFinding) -> None:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(f.address, 22,
                                                                  happy_eyeballs_delay=HAPPY_EYEBALLS_DELAY),
                                          self.s.timeout)
            banner = await asyncio.wait_for(r.readline(), self.s.timeout)
            w.close()
            f.ssh_banner = banner.decode(errors="replace").strip()[:80]
        except (OSError, asyncio.TimeoutError):
            pass

    async def _mqtt(self, f: HostFinding, port: int) -> None:
        tls = await self._tls_probe(f.address, port, f.dns_name)
        attempts: list[str | None] = [None, *self.mqtt_creds]
        for cred in attempts:
            mon = MqttMonitor(name="discovery", type="mqtt",
                              host=(f.dns_name if tls and tls["valid"] else f.address),
                              port=port, credential=cred, tls=tls is not None,
                              ca_bundle=self.s.ca_bundle, timeout=self.s.timeout + 1)
            if tls is not None and not tls["valid"]:
                f.mqtt[port] = {"ok": False, "tls": True,
                                "reason": f"TLS listener did not validate: {tls['reason']}"}
                return
            res = await MqttCheck(mon, self.config).run()
            if res.result.value == "ok":
                f.mqtt[port] = {"ok": True, "tls": tls is not None, "credential": cred,
                                "host": mon.host}
                return
            f.mqtt[port] = {"ok": False, "tls": tls is not None, "reason": res.message}

    async def _winrm(self, f: HostFinding) -> None:
        name = f.dns_name
        tls = await self._tls_probe(f.address, self.s.winrm_port, name)
        if tls is None:
            f.notes.append(f"port {self.s.winrm_port} open but not TLS; WinRM not tried")
            return
        if self.s.winrm_require_valid_tls and not tls["valid"]:
            f.notes.append(f"WinRM listener certificate did not validate for "
                           f"{name or f.address} ({tls['reason']}); credentials not sent")
            return
        for cred in self.winrm_creds:
            if self.auth_failures[cred] >= self.s.max_auth_failures:
                continue
            if cred in self.proven:
                ok = await self._winrm_try(f, cred, name or f.address)
            else:
                async with self._cred_locks[cred]:  # serialise until proven
                    if self.auth_failures[cred] >= self.s.max_auth_failures:
                        continue
                    ok = await self._winrm_try(f, cred, name or f.address)
            if ok:
                return

    async def _winrm_try(self, f: HostFinding, cred: str, host: str) -> bool:
        mon = WinRMMonitor(name="discovery", type="winrm", host=host, credential=cred,
                           port=self.s.winrm_port, ca_bundle=self.s.ca_bundle,
                           verify_tls=self.s.winrm_require_valid_tls, mode="powershell",
                           script="0", timeout=max(self.s.timeout, 5))
        chk = WinRMCheck(mon, self.config)
        try:
            rc, out, err = await chk.run_ps(WINRM_PROBE)
        except (InvalidCredentialsError, AuthenticationError) as e:
            self.auth_failures[cred] += 1
            left = self.s.max_auth_failures - self.auth_failures[cred]
            f.notes.append(f"WinRM credential {cred!r} rejected ({type(e).__name__}); "
                           f"{max(left, 0)} attempts left before it is retired")
            return False
        except Exception as e:  # noqa: BLE001 - transport errors are findings, not crashes
            f.notes.append(f"WinRM with {cred!r} failed: {type(e).__name__}: {e}")
            return False
        if rc != 0:
            f.notes.append(f"WinRM probe script failed: {err.splitlines()[0] if err else rc}")
            return False
        self.proven.add(cred)
        data = json.loads(out)
        for key in ("disks", "services"):
            if isinstance(data.get(key), str):
                data[key] = [data[key]]
            data[key] = data.get(key) or []
        data["credential"] = cred
        data["host"] = host
        f.winrm = data
        return True

    def _host_key_state(self, host: str, port: int) -> str:
        """"known" (a usable entry exists), "unusable" (the host appears in the
        file but asyncssh could not parse the entry), or "unlisted".

        asyncssh skips malformed lines silently, so without the textual check
        a corrupted entry would look like no entry and fall through to
        trust-on-first-use. A listed key that differs from the server's is
        caught later, when the verified connect raises HostKeyNotVerifiable.
        """
        addr = host if HostFinding(host).is_ip else ""
        if self.known_hosts is not None and self.known_hosts.match(host, addr, port)[0]:
            return "known"
        spec = host if port == 22 else f"[{host}]:{port}"
        try:
            with open(self.s.ssh_known_hosts, encoding="utf-8") as fh:
                for line in fh:
                    fields = line.split()
                    if fields and spec in fields[0].split(","):
                        return "unusable"
        except OSError:
            pass
        return "unlisted"

    async def _ssh(self, f: HostFinding) -> None:
        port = self.s.ssh_port
        state = self._host_key_state(f.address, port)
        if state == "unusable":
            f.notes.append("known_hosts has an entry for this host that could not be parsed; "
                           "SSH not tried (fix or remove the entry)")
            return
        if state == "unlisted" and not self.s.ssh_trust_on_first_use:
            f.notes.append("SSH host key not in known_hosts and trust-on-first-use is off; "
                           "SSH not tried")
            return
        for cred_name in self.ssh_creds:
            cred = self.config.credentials[cred_name]
            if state == "unlisted" and not cred.private_key:
                f.notes.append(f"SSH credential {cred_name!r} is password-only; not offered "
                               "to a host whose key is not already trusted")
                continue
            if self.auth_failures[cred_name] >= self.s.max_auth_failures:
                continue
            if cred_name in self.proven:
                ok = await self._ssh_try(f, cred_name, state)
            else:
                async with self._cred_locks[cred_name]:
                    if self.auth_failures[cred_name] >= self.s.max_auth_failures:
                        continue
                    ok = await self._ssh_try(f, cred_name, state)
            if ok:
                return

    async def _ssh_try(self, f: HostFinding, cred_name: str, state: str) -> bool:
        cred = self.config.credentials[cred_name]
        port = self.s.ssh_port
        known = self.s.ssh_known_hosts if state == "known" else None
        try:
            async with await ssh_connect(f.address, port, cred, known,
                                         max(self.s.timeout, 5)) as conn:
                if known is None:
                    key = conn.get_server_host_key()
                    host = f.address if port == 22 else f"[{f.address}]:{port}"
                    line = key.export_public_key("openssh").decode().split()
                    f.ssh_new_host_key = f"{host} {line[0]} {line[1]}"
                    f.notes.append("SSH host key was not in known_hosts; recorded for review "
                                   "(trust on first use)")
                rc, out, err = await ssh_run(conn, linux_probe(self.s.docker_command),
                                             max(self.s.timeout, 5) + 10)
        except asyncssh.HostKeyNotVerifiable:
            f.notes.append("SSH HOST KEY MISMATCH with known_hosts; credentials not used. "
                           "Investigate before trusting this host.")
            return True  # stop trying other credentials against this host
        except asyncssh.PermissionDenied:
            self.auth_failures[cred_name] += 1
            left = self.s.max_auth_failures - self.auth_failures[cred_name]
            f.notes.append(f"SSH credential {cred_name!r} rejected; {max(left, 0)} attempts "
                           "left before it is retired")
            return False
        except (asyncssh.Error, OSError, asyncio.TimeoutError, ValueError) as e:
            f.notes.append(f"SSH with {cred_name!r} failed: {type(e).__name__}: {e}")
            return False
        self.proven.add(cred_name)
        info = parse_linux_probe(out)
        info["credential"] = cred_name
        f.ssh = info
        if info["docker"] == "denied":
            f.notes.append("Docker is installed but this account can not use it (see "
                           "THREAT-MODEL.md for a narrow sudo rule)")
        elif info["docker"].startswith("error"):
            f.notes.append(f"docker ps failed: {info['docker'][7:]}")
        return True

    async def _tls_gate(self, f: HostFinding, port: int, what: str) -> ssl.SSLContext | None:
        """Decide whether API credentials may be sent to this port. Returns
        the SSL context to use, or None (with a note) when they may not."""
        tls = f.tls.get(port)
        if tls is None:
            tls = await self._tls_probe(f.address, port, f.dns_name)
            if tls is None:
                f.notes.append(f"{what}: port {port} is not TLS; credentials not sent")
                return None
            f.tls[port] = tls
        if tls["valid"]:
            return api_ssl_context(True, self.s.ca_bundle)
        if not self.s.api_require_valid_tls:
            f.notes.append(f"{what}: certificate on {port} not valid ({tls['reason']}); "
                           "proceeding because api_require_valid_tls is false")
            return api_ssl_context(False, None)
        f.unverified_certs[port] = {"sha256": tls["sha256"], "pem": tls["pem"],
                                    "subject": tls["subject"]}
        f.notes.append(f"{what}: certificate on {port} did not validate ({tls['reason']}); "
                       f"credentials not sent. SHA-256 {tls['sha256'][:23]}... "
                       "(pin it via ca_bundle, see --certs-out)")
        return None

    async def _http_gate(self, f: HostFinding, port: int, what: str
                         ) -> tuple[bool, Any] | None:
        """Like _tls_gate, but for services that commonly serve plain HTTP
        (Home Assistant, Technitium). Returns (https, verify) or None.

        Plain HTTP is only used when api_require_valid_tls is false, because
        the token would cross the network in cleartext."""
        tls = f.tls.get(port) or await self._tls_probe(f.address, port, f.dns_name)
        if tls is not None:
            f.tls[port] = tls
            ctx = await self._tls_gate(f, port, what)
            return None if ctx is None else (True, ctx)
        if not self.s.api_require_valid_tls:
            f.notes.append(f"{what}: port {port} is plain HTTP; sending the token in cleartext "
                           "because api_require_valid_tls is false")
            return (False, False)
        f.notes.append(f"{what}: port {port} is plain HTTP, so the token was not sent. Enable "
                       "TLS on it, or set api_require_valid_tls: false for a trusted segment")
        return None

    async def _homeassistant(self, f: HostFinding) -> None:
        port = self.s.homeassistant_port
        gate = await self._http_gate(f, port, "Home Assistant")
        if gate is None:
            return
        https, verify = gate
        host = f.api_host(port) if https else f.address
        base = f"{'https' if https else 'http'}://{host}:{port}"
        for cred_name in self.ha_creds:
            cred = self.config.credentials[cred_name]

            async def attempt() -> Any:
                h = {"Authorization": f"Bearer {cred.token}"}
                async with http_client(verify, self.s.timeout + 5) as c:
                    r = await c.get(base + "/api/", headers=h)
                    if r.status_code in (401, 403):
                        raise AuthFailed(f"HTTP {r.status_code}")
                    r.raise_for_status()
                    if r.json().get("message") != "API running.":
                        raise ValueError("not a Home Assistant API")
                    states = (await c.get(base + "/api/states", headers=h)).json()
                return states

            states = await self._with_cred(f, cred_name, "Home Assistant", attempt)
            if states is None:
                continue
            f.homeassistant = {
                "credential": cred_name, "host": host, "port": port, "https": https,
                "verify": https and verify.verify_mode != ssl.CERT_NONE,
                "entities": len(states),
                "unavailable": sum(1 for x in states if x.get("state") == "unavailable"),
                "updates": sum(1 for x in states if x["entity_id"].startswith("update.")
                               and x.get("state") == "on")}
            return

    async def _unifi(self, f: HostFinding, port: int) -> bool:
        """True if this is a UniFi console (whether or not a key worked)."""
        ctx = await self._tls_gate(f, port, "UniFi")
        if ctx is None:
            return False
        host = f.api_host(port)
        base = f"https://{host}:{port}"
        for cred_name in self.unifi_creds:
            cred = self.config.credentials[cred_name]

            async def attempt() -> Any:
                h = {"X-API-KEY": cred.api_key, "Accept": "application/json"}
                net = base + "/proxy/network/integration/v1"
                async with http_client(ctx, self.s.timeout + 5) as c:
                    try:
                        sites = await unifi_list_all(c, net + "/sites", h)
                    except LookupError:
                        return "not-unifi"
                    devices = []
                    for site in sites:
                        devices += [{"site": site.get("name"), **x} for x in
                                    await unifi_list_all(c, f"{net}/sites/{site['id']}/devices", h)]
                    try:
                        cams = await unifi_list_all(
                            c, base + "/proxy/protect/integration/v1/cameras", h)
                    except (LookupError, AuthFailed, httpx.HTTPError):
                        cams = None
                return {"sites": sites, "devices": devices, "cameras": cams}

            out = await self._with_cred(f, cred_name, "UniFi", attempt)
            if out == "not-unifi":
                return False
            if out is None:
                continue
            f.unifi = {"credential": cred_name, "host": host, "port": port,
                       "verify": ctx.verify_mode != ssl.CERT_NONE,
                       "sites": [s_.get("name") for s_ in out["sites"]],
                       "devices": [{"name": d.get("name"), "model": d.get("model"),
                                    "state": d.get("state"), "site": d.get("site")}
                                   for d in out["devices"]],
                       "cameras": None if out["cameras"] is None else
                       [{"name": c_.get("name"), "state": c_.get("state")}
                        for c_ in out["cameras"]]}
            return True
        return False

    async def _technitium(self, f: HostFinding) -> None:
        port = self.s.technitium_port
        gate = await self._http_gate(f, port, "Technitium")
        if gate is None:
            return
        https, verify = gate
        host = f.api_host(port) if https else f.address
        base = f"{'https' if https else 'http'}://{host}:{port}"
        for cred_name in self.technitium_creds:
            cred = self.config.credentials[cred_name]

            async def attempt() -> Any:
                async with http_client(verify, self.s.timeout + 5) as c:
                    r = await c.get(base + "/api/dashboard/stats/get",
                                    headers={"Authorization": f"Bearer {cred.token}"},
                                    params={"type": "LastHour", "utc": "true"})
                r.raise_for_status()
                body = r.json()
                if body.get("status") == "invalid-token":
                    raise AuthFailed("invalid-token")
                if body.get("status") != "ok":
                    raise ValueError(f"status {body.get('status')!r}")
                return body["response"].get("stats") or {}

            stats = await self._with_cred(f, cred_name, "Technitium", attempt)
            if stats is None:
                continue
            f.technitium = {"credential": cred_name, "host": host, "port": port, "https": https,
                            "verify": https and verify.verify_mode != ssl.CERT_NONE,
                            "queries": stats.get("totalQueries"),
                            "clients": stats.get("totalClients")}
            return

    async def _with_cred(self, f: HostFinding, cred: str, what: str, attempt: Any) -> Any:
        """Run attempt() under the lockout rules shared by every credential
        type that can be locked out. Returns its result, or None."""
        if self.auth_failures[cred] >= self.s.max_auth_failures:
            return None

        async def once() -> Any:
            try:
                out = await attempt()
            except AuthFailed as err:
                self.auth_failures[cred] += 1
                left = self.s.max_auth_failures - self.auth_failures[cred]
                f.notes.append(f"{what} credential {cred!r} rejected ({err}); "
                               f"{max(left, 0)} attempts left before it is retired")
                return None
            except Exception as err:  # noqa: BLE001 - transport errors are findings
                f.notes.append(f"{what} with {cred!r} failed: {type(err).__name__}: {err}")
                return None
            self.proven.add(cred)
            return out

        if cred in self.proven:
            return await once()
        async with self._cred_locks[cred]:
            if self.auth_failures[cred] >= self.s.max_auth_failures:
                return None
            return await once()

    async def _proxmox(self, f: HostFinding) -> None:
        port = self.s.proxmox_port
        ctx = await self._tls_gate(f, port, "Proxmox")
        if ctx is None:
            return
        host = f.api_host(port)
        for cred_name in self.proxmox_creds:
            cred = self.config.credentials[cred_name]

            async def attempt() -> Any:
                async with http_client(ctx, self.s.timeout + 5) as c:
                    return await proxmox_resources(c, host, port, cred)

            items = await self._with_cred(f, cred_name, "Proxmox", attempt)
            if items is None:
                continue
            if not items:
                f.notes.append("Proxmox token works but sees nothing; grant PVEAuditor")
                return
            nodes = sorted(i["node"] for i in items if i.get("type") == "node")
            storages, seen = [], set()
            for i in items:
                if i.get("type") != "storage" or i.get("status") != "available":
                    continue
                key = i.get("storage") if i.get("shared") else (i.get("storage"), i.get("node"))
                if key in seen:
                    continue
                seen.add(key)
                storages.append({"storage": i.get("storage"), "node": i.get("node"),
                                 "shared": bool(i.get("shared"))})
            guests = [{"vmid": i.get("vmid"), "name": i.get("name"), "node": i.get("node"),
                       "kind": "VM" if i["type"] == "qemu" else "CT"}
                      for i in items if i.get("type") in ("qemu", "lxc")
                      and i.get("status") == "running" and not i.get("template")]
            stopped = sum(1 for i in items if i.get("type") in ("qemu", "lxc")
                          and i.get("status") != "running" and not i.get("template"))
            f.proxmox = {"credential": cred_name, "host": host, "nodes": nodes,
                         "storages": storages, "guests": guests, "stopped": stopped,
                         "verify": ctx.verify_mode != ssl.CERT_NONE}
            return

    async def _is_vsphere(self, f: HostFinding, port: int) -> bool:
        """Unauthenticated fingerprint: ESXi and vCenter serve the SOAP version
        document that pyVmomi itself reads. No credential is sent."""
        try:
            async with http_client(False, self.s.timeout + 2) as c:
                r = await c.get(f"https://{f.address}:{port}/sdk/vimServiceVersions.xml")
            return r.status_code == 200 and "urn:vim25" in r.text
        except httpx.HTTPError:
            return False

    async def _vsphere(self, f: HostFinding, port: int = 443) -> None:
        ctx = await self._tls_gate(f, port, "vSphere")
        if ctx is None:
            return
        host = f.api_host(port)
        for cred_name in self.vsphere_creds:
            cred = self.config.credentials[cred_name]
            verified = ctx.verify_mode != ssl.CERT_NONE

            async def attempt() -> Any:
                loop = asyncio.get_running_loop()
                return await asyncio.wait_for(loop.run_in_executor(
                    VSPHERE_POOL, vsphere_collect, host, port, cred.username, cred.password,
                    ctx if verified else None,
                    {"host": ["name", "runtime.connectionState"], "datastore":
                     ["name", "summary.accessible"], "vm":
                     ["name", "runtime.powerState", "config.template", "runtime.host"]}), 60)

            data = await self._with_cred(f, cred_name, "vSphere", attempt)
            if data is None:
                continue
            vms = [v for v in data["vm"] if str(v.get("runtime.powerState")) == "poweredOn"
                   and not v.get("config.template")]
            f.vsphere = {"credential": cred_name, "host": host, "port": port,
                         "hosts": sorted(h["name"] for h in data["host"]),
                         "datastores": sorted(d["name"] for d in data["datastore"]
                                              if d.get("summary.accessible")),
                         "vms": [{"name": v["name"], "host": v.get("runtime.host")}
                                 for v in vms],
                         "stopped": len(data["vm"]) - len(vms), "verify": verified}
            return

    async def _truenas(self, f: HostFinding, port: int = 443) -> None:
        ctx = await self._tls_gate(f, port, "TrueNAS")
        if ctx is None:
            return
        host = f.api_host(port)
        for cred_name in self.truenas_creds:
            cred = self.config.credentials[cred_name]

            async def attempt() -> Any:
                async with TrueNASClient(host, port, cred.username, cred.api_key, ctx,
                                         self.s.timeout + 5) as tn:
                    return await tn.call("pool.query")

            pools = await self._with_cred(f, cred_name, "TrueNAS", attempt)
            if pools is None:
                continue
            f.truenas = {"credential": cred_name, "host": host, "port": port,
                         "pools": sorted(p.get("name") for p in pools),
                         "verify": ctx.verify_mode != ssl.CERT_NONE}
            return

    # ------------------------------------------------------------- driver

    async def scan_host(self, address: str) -> HostFinding:
        f = HostFinding(address)
        ports = set(self.s.tcp_ports)
        if self.ha_creds:
            ports.add(self.s.homeassistant_port)
        if self.technitium_creds:
            ports.add(self.s.technitium_port)
        if self.unifi_creds:
            ports.add(self.s.https_api_port)
        if self.proxmox_creds:
            ports.add(self.s.proxmox_port)
        if self.vsphere_creds or self.truenas_creds:
            ports.add(self.s.https_api_port)
        if self.ssh_creds:
            ports.add(self.s.ssh_port)
        if self.winrm_creds:
            ports.add(self.s.winrm_port)
        await asyncio.gather(self._ping(f), self._ptr(f), *(self._tcp(f, p) for p in ports))
        f.open_ports.sort()
        await self._snmp(f)
        if self.s.winrm_port in f.open_ports and self.winrm_creds:
            await self._winrm(f)
        if self.s.ssh_port in f.open_ports and self.ssh_creds:
            await self._ssh(f)
        if self.s.proxmox_port in f.open_ports and self.proxmox_creds:
            await self._proxmox(f)
        if self.s.homeassistant_port in f.open_ports and self.ha_creds:
            await self._homeassistant(f)
        if self.s.technitium_port in f.open_ports and self.technitium_creds:
            await self._technitium(f)
        api = self.s.https_api_port
        if api in f.open_ports and (self.vsphere_creds or self.truenas_creds
                                    or self.unifi_creds):
            if await self._is_vsphere(f, api):
                if self.vsphere_creds:
                    await self._vsphere(f, api)
            elif self.unifi_creds and await self._unifi(f, api):
                pass
            elif self.truenas_creds:
                await self._truenas(f, api)
        tasks = []
        for port in f.open_ports:
            if port == 22 and not f.ssh:
                tasks.append(self._ssh_banner(f))
            elif port in self.s.mqtt_ports:
                tasks.append(self._mqtt(f, port))
            elif port not in (3389, self.s.winrm_port) and \
                    not (port == self.s.proxmox_port and f.proxmox) and \
                    not (port == self.s.homeassistant_port and f.homeassistant) and \
                    not (port == self.s.technitium_port and f.technitium):
                tasks.append(self._web(f, port))
        await asyncio.gather(*tasks)
        return f

    async def run(self, targets: list[str]) -> list[HostFinding]:
        sem = asyncio.Semaphore(self.s.concurrency)

        async def bounded(addr: str) -> HostFinding:
            async with sem:
                f = await self.scan_host(addr)
                self.stats["scanned"] += 1
                if f.responded:
                    self.stats["responded"] += 1
                    log.info("%s: %s", addr, f.label)
                return f

        results = await asyncio.gather(*(bounded(a) for a in targets))
        return [f for f in results if f.responded]


# ---------------------------------------------------------------- proposals


_MONITOR = TypeAdapter(Monitor)


def _key(m: dict[str, Any]) -> tuple[Any, ...]:
    """Identity of a monitor for de-duplication, after defaults are applied,
    so a proposal omitting port: 161 matches a config that spells it out.
    Validating here also guarantees every proposal would load."""
    d = _MONITOR.validate_python(m).model_dump(exclude_none=True)
    return (d.get("type"), d.get("host") or d.get("url"), d.get("port"), d.get("mode"),
            d.get("interface"), d.get("service"), d.get("disk"), d.get("topic"))


def propose(f: HostFinding, s: DiscoverySettings) -> tuple[str, list[dict[str, Any]]]:
    """Return (group, monitor dicts) for one host."""
    ms: list[dict[str, Any]] = []
    base = f.label
    ip = f.address
    ca = {"ca_bundle": s.ca_bundle} if s.ca_bundle else {}

    if f.winrm:
        group = "windows"
    elif f.ssh:
        group = "linux"
    elif f.snmp and len(f.snmp.get("links_up", [])) >= 8:
        group = "network"
    elif f.snmp:
        group = "servers"
    elif f.http:
        group = "apps"
    else:
        group = "discovered"

    def add(suffix: str, **kw: Any) -> None:
        ms.append({"name": f"{base} {suffix}", "group": group, **kw})

    if f.icmp:
        add("ping", type="ping", host=ip)

    if f.snmp:
        cred = f.snmp["credential"]
        add("uptime", type="snmp", host=ip, credential=cred, mode="uptime",
            **({"port": s.snmp_port} if s.snmp_port != 161 else {}))
        if f.snmp.get("has_cpu"):
            add("CPU", type="snmp", host=ip, credential=cred, mode="cpu",
                thresholds={"direction": "above", "warn": 85, "crit": 97})
        if f.snmp.get("has_ram"):
            add("memory", type="snmp", host=ip, credential=cred, mode="memory",
                thresholds={"direction": "above", "warn": 90, "crit": 97}, forecast=True)
        for link in f.snmp.get("links_up", [])[: s.max_interfaces]:
            add(link["name"], type="snmp", host=ip, credential=cred, mode="interface",
                interface=link["name"], forecast=True,
                thresholds={"direction": "above", "warn": 70, "crit": 90})
        if s.snmp_port != 161:
            for m in ms:
                if m["type"] == "snmp":
                    m["port"] = s.snmp_port

    if f.winrm:
        w = f.winrm
        common = {"type": "winrm", "host": w["host"], "credential": w["credential"],
                  **({"port": s.winrm_port} if s.winrm_port != 5986 else {}), **ca}
        add("CPU", mode="cpu", thresholds={"direction": "above", "warn": 85, "crit": 97},
            **common)
        add("memory", mode="memory", forecast=True,
            thresholds={"direction": "above", "warn": 90, "crit": 97}, **common)
        for disk in w["disks"]:
            add(f"{disk} disk", mode="disk", disk=disk, forecast=True,
                thresholds={"direction": "above", "warn": 85, "crit": 95}, **common)
        for svc in w["services"]:
            add(svc, mode="service", service=svc, **common)

    if f.ssh:
        sh = f.ssh
        common = {"type": "linux", "host": f.address, "credential": sh["credential"],
                  "known_hosts": s.ssh_known_hosts,
                  **({"port": s.ssh_port} if s.ssh_port != 22 else {})}
        add("CPU", mode="cpu", thresholds={"direction": "above", "warn": 85, "crit": 97},
            **common)
        add("memory", mode="memory", forecast=True,
            thresholds={"direction": "above", "warn": 90, "crit": 97}, **common)
        add("uptime", mode="uptime", **common)
        for mount in sh["mounts"]:
            add(f"disk {mount}", mode="disk", mount=mount, forecast=True,
                thresholds={"direction": "above", "warn": 85, "crit": 95}, **common)
        for unit in sh["units"]:
            add(unit, mode="service", service=unit, **common)
        if sh["docker"] == "ok":
            dk = {**common, "type": "docker",
                  **({"docker_command": s.docker_command} if s.docker_command != "docker"
                     else {})}
            ms.append({"name": f"{base} containers", "group": "docker", "mode": "summary",
                       **dk})
            for c in sh["containers"][: s.max_containers]:
                if not c["name"] or "," in c["name"]:
                    continue
                ms.append({"name": f"{base} {c['name']}", "group": "docker",
                           "mode": "container", "container": c["name"], **dk})
    elif f.ssh_banner and f.ssh_banner.startswith("SSH-2.0"):
        add("SSH", type="tcp", host=ip, port=22, expect="SSH-2.0")
    elif 22 in f.open_ports:
        add("port 22", type="tcp", host=ip, port=22)
    if 3389 in f.open_ports:
        add("RDP", type="tcp", host=ip, port=3389)

    api_ports = set()
    if f.proxmox:
        api_ports.add(s.proxmox_port)
    if f.vsphere or f.truenas or f.unifi:
        api_ports.add(s.https_api_port)
    if f.homeassistant:
        api_ports.add(s.homeassistant_port)
    if f.technitium:
        api_ports.add(s.technitium_port)
    for port, h in sorted(f.http.items()):
        if port in api_ports:
            continue
        url = f"{h['scheme']}://{h['host']}:{port}/"
        entry: dict[str, Any] = {"type": "http", "url": url, "expect_status": [h["status"]]}
        tls = f.tls.get(port)
        if tls is not None:
            entry.update(ca if tls["valid"] else {"verify_tls": False})
        add(f"web {port}", **entry)
    for port, tls in sorted(f.tls.items()):
        if port in f.mqtt or port == s.winrm_port:
            continue
        host = f.dns_name or ip
        extra = ca if tls["valid"] else {"verify": False}
        add(f"cert {port}", type="tls_cert", host=host, port=port, interval=3600, **extra)
    if not f.icmp and not ms:
        for port in f.open_ports[:1]:
            add(f"port {port}", type="tcp", host=ip, port=port)

    def api_common(info: dict[str, Any], kind: str) -> dict[str, Any]:
        out: dict[str, Any] = {"type": kind, "host": info["host"],
                               "credential": info["credential"]}
        if "port" in info and info["port"] != {"truenas": 443, "vsphere": 443}.get(kind):
            out["port"] = info["port"]
        if kind == "proxmox" and s.proxmox_port != 8006:
            out["port"] = s.proxmox_port
        out.update(ca if info["verify"] else {"verify_tls": False})
        return out

    def plat(group_name: str, suffix: str, **kw: Any) -> str:
        name = f"{base} {suffix}"
        ms.append({"name": name, "group": group_name, **kw})
        return name

    if f.proxmox:
        px = f.proxmox
        c = api_common(px, "proxmox")
        node_mon: dict[str, str] = {}
        for node in px["nodes"]:
            node_mon[node] = plat("proxmox", f"node {node}", **c, mode="node", node=node)
            plat("proxmox", f"node {node} CPU", **c, mode="node_cpu", node=node,
                 thresholds={"direction": "above", "warn": 85, "crit": 97})
            plat("proxmox", f"node {node} memory", **c, mode="node_memory", node=node,
                 forecast=True, thresholds={"direction": "above", "warn": 90, "crit": 97})
        for st in px["storages"]:
            extra = {} if st["shared"] else {"node": st["node"]}
            label = st["storage"] if st["shared"] else f"{st['storage']} on {st['node']}"
            plat("proxmox", f"storage {label}", **c, mode="storage", storage=st["storage"],
                 forecast=True, thresholds={"direction": "above", "warn": 85, "crit": 95},
                 **extra)
        for g in px["guests"][: s.max_guests]:
            deps = [node_mon[g["node"]]] if g["node"] in node_mon else []
            plat("proxmox", f"{g['kind']} {g['name'] or g['vmid']}", **c, mode="guest",
                 guest=str(g["vmid"]), depends_on=deps)

    if f.vsphere:
        vs = f.vsphere
        c = api_common(vs, "vsphere")
        single = len(vs["hosts"]) == 1
        host_mon: dict[str, str] = {}
        for h in vs["hosts"]:
            ent = {} if single else {"entity": h}
            label = "ESXi" if single else f"ESXi {h}"
            host_mon[h] = plat("vsphere", label, **c, mode="host", **ent)
            plat("vsphere", f"{label} CPU", **c, mode="host_cpu",
                 thresholds={"direction": "above", "warn": 85, "crit": 97}, **ent)
            plat("vsphere", f"{label} memory", **c, mode="host_memory", forecast=True,
                 thresholds={"direction": "above", "warn": 90, "crit": 97}, **ent)
        for d in vs["datastores"]:
            plat("vsphere", f"datastore {d}", **c, mode="datastore", entity=d, forecast=True,
                 thresholds={"direction": "above", "warn": 85, "crit": 95})
        for v in vs["vms"][: s.max_guests]:
            deps = [host_mon[v["host"]]] if v.get("host") in host_mon else []
            plat("vsphere", f"VM {v['name']}", **c, mode="vm", entity=v["name"],
                 depends_on=deps)

    def web_common(info: dict[str, Any], kind: str, default_port: int) -> dict[str, Any]:
        out: dict[str, Any] = {"type": kind, "host": info["host"],
                               "credential": info["credential"]}
        if info["port"] != default_port:
            out["port"] = info["port"]
        https_default = kind == "homeassistant"
        if info["https"] != https_default:
            out["https"] = info["https"]
        if info["https"]:
            out.update(ca if info["verify"] else {"verify_tls": False})
        return out

    if f.homeassistant:
        ha = f.homeassistant
        c = web_common(ha, "homeassistant", 8123)
        plat("homeassistant", "Home Assistant API", **c, mode="api")
        plat("homeassistant", "HA unavailable entities", **c, mode="unavailable",
             thresholds={"direction": "above", "warn": 1, "crit": 25})
        plat("homeassistant", "HA updates", **c, mode="updates")

    if f.technitium:
        tt = f.technitium
        c = web_common(tt, "technitium", 5380)
        plat("dns", "Technitium SERVFAIL rate", **c, mode="stats",
             thresholds={"direction": "above", "warn": 2, "crit": 10})
        plat("dns", "Technitium update", **c, mode="update", interval=21600)

    if f.unifi:
        un = f.unifi
        c = api_common(un, "unifi_network")
        many_sites = len(un["sites"]) > 1
        for site in un["sites"]:
            sc = {**c, **({"site": site} if many_sites else {})}
            tag = f" {site}" if many_sites else ""
            offline = sorted(d["name"] for d in un["devices"]
                             if d["site"] == site and d["state"] != "ONLINE")
            plat("unifi", f"UniFi devices{tag}", **sc, mode="devices",
                 **({"ignore": offline} if offline else {}))
            plat("unifi", f"UniFi firmware{tag}", **sc, mode="firmware", interval=21600)
            online = [d for d in un["devices"] if d["site"] == site and d["state"] == "ONLINE"]
            for d in online[: s.max_guests]:
                if not d["name"]:
                    continue
                plat("unifi", f"{d['name']}", **sc, mode="device", device=d["name"])
                plat("unifi", f"{d['name']} CPU", **sc, mode="device_cpu", device=d["name"],
                     thresholds={"direction": "above", "warn": 85, "crit": 95})
                plat("unifi", f"{d['name']} memory", **sc, mode="device_memory",
                     device=d["name"], forecast=True,
                     thresholds={"direction": "above", "warn": 85, "crit": 95})
        if un["cameras"] is not None:
            pc = {**c, "type": "unifi_protect"}
            plat("protect", "Protect cameras", **pc, mode="cameras")
            for cam in un["cameras"][: s.max_guests]:
                if cam["name"]:
                    plat("protect", f"camera {cam['name']}", **pc, mode="camera",
                         camera=cam["name"])

    if f.truenas:
        tn = f.truenas
        c = api_common(tn, "truenas")
        plat("truenas", "pools", **c, mode="pools")
        plat("truenas", "alerts", **c, mode="alerts")
        for pool in tn["pools"]:
            plat("truenas", f"pool {pool}", **c, mode="pool", pool=pool, forecast=True,
                 thresholds={"direction": "above", "warn": 80, "crit": 90})

    for port, mq in sorted(f.mqtt.items()):
        if mq.get("ok"):
            add(f"MQTT {port}", type="mqtt", host=mq["host"], port=port,
                **({"tls": True} if mq["tls"] else {}),
                **({"credential": mq["credential"]} if mq.get("credential") else {}),
                **(ca if mq["tls"] else {}))
    return group, ms


def _evidence(f: HostFinding) -> list[str]:
    lines = [f"{f.address}  {f.label}" + (f"  (PTR {f.ptr})" if f.ptr else "")]
    bits = []
    if f.icmp:
        bits.append(f"ICMP {f.rtt_ms:.1f} ms" if f.rtt_ms is not None else "ICMP")
    if f.open_ports:
        bits.append("TCP " + ",".join(map(str, f.open_ports)))
    if bits:
        lines.append("; ".join(bits))
    if f.snmp:
        sn = f.snmp
        lines.append(f"SNMP via {sn['credential']}: {sn.get('sys_descr') or 'no sysDescr'}")
        if "interfaces_total" in sn:
            lines.append(f"  {sn['interfaces_total']} interfaces, "
                         f"{len(sn.get('links_up', []))} physical links up")
    if f.directory:
        lines.append(f"AD: {f.directory.get('os') or 'no OS recorded'}, last logon "
                     f"{(f.directory.get('last_logon') or '?')[:10]}")
    if f.ssh:
        sh = f.ssh
        lines.append(f"SSH via {sh['credential']}: {sh.get('os') or '?'} ({sh.get('kernel')}), "
                     f"{sh.get('nproc')} cores")
        if sh["docker"] == "ok":
            lines.append(f"  Docker: {len(sh['containers'])} running containers")
    if f.proxmox:
        px = f.proxmox
        lines.append(f"Proxmox via {px['credential']}: nodes {', '.join(px['nodes'])}; "
                     f"{len(px['storages'])} storages; {len(px['guests'])} running guests, "
                     f"{px['stopped']} stopped (not proposed)")
    if f.vsphere:
        vs = f.vsphere
        lines.append(f"vSphere via {vs['credential']}: {len(vs['hosts'])} host(s), "
                     f"{len(vs['datastores'])} datastores, {len(vs['vms'])} powered-on VMs, "
                     f"{vs['stopped']} off or templates (not proposed)")
    if f.homeassistant:
        ha = f.homeassistant
        lines.append(f"Home Assistant via {ha['credential']} "
                     f"({'HTTPS' if ha['https'] else 'plain HTTP'}): {ha['entities']} entities, "
                     f"{ha['unavailable']} unavailable, {ha['updates']} updates pending")
    if f.technitium:
        lines.append(f"Technitium via {f.technitium['credential']}: "
                     f"{f.technitium['queries']} queries last hour, "
                     f"{f.technitium['clients']} clients")
    if f.unifi:
        un = f.unifi
        off = [d["name"] for d in un["devices"] if d["state"] != "ONLINE"]
        lines.append(f"UniFi Network via {un['credential']}: sites {', '.join(un['sites'])}; "
                     f"{len(un['devices'])} devices"
                     + (f", not online now (added to ignore): {', '.join(off)}" if off else ""))
        if un["cameras"] is not None:
            lines.append(f"UniFi Protect: {len(un['cameras'])} cameras")
        else:
            lines.append("UniFi Protect: not reachable with this key (not installed, or no "
                         "access)")
    if f.truenas:
        lines.append(f"TrueNAS via {f.truenas['credential']}: pools "
                     f"{', '.join(f.truenas['pools']) or 'none'}")
    if f.winrm:
        w = f.winrm
        lines.append(f"WinRM via {w['credential']}: {w.get('computer')} "
                     f"({w.get('os')}), domain {w.get('domain')}")
    for port, tls in sorted(f.tls.items()):
        lines.append(f"TLS {port}: {tls['subject']}, expires {tls['not_after'][:10]}, "
                     f"{'valid' if tls['valid'] else 'NOT valid: ' + tls['reason']}")
    for port, mq in sorted(f.mqtt.items()):
        if not mq.get("ok"):
            lines.append(f"MQTT {port}: {mq.get('reason')}")
    lines.extend(f"note: {n}" for n in f.notes)
    return lines


def render(findings: list[HostFinding], config: Config, targets: list[str],
           stats: dict[str, int]) -> tuple[str, dict[str, Any]]:
    """Proposal YAML text plus a JSON-able report."""
    existing = {_key(m.model_dump(exclude_none=True)): m.name for m in config.monitors}
    taken = {m.slug for m in config.monitors}
    blocks: list[str] = []
    proposed = skipped = 0
    for f in findings:
        group, ms = propose(f, config.discovery)
        keep = []
        renamed: dict[str, str] = {}
        for m in ms:
            k = _key(m)
            if k in existing:
                skipped += 1
                renamed[m["name"]] = existing[k]  # dependents point at the configured one
                continue
            name, n = m["name"], 2
            if _slug(name) in taken:
                name = f"{m['name']} ({f.address})"
                while _slug(name) in taken:
                    name, n = f"{m['name']} ({f.address}) {n}", n + 1
            renamed[m["name"]] = name
            m["name"] = name
            taken.add(_slug(name))
            keep.append(m)
        for m in keep:
            if m.get("depends_on"):
                m["depends_on"] = [renamed.get(d, d) for d in m["depends_on"]]
            elif "depends_on" in m:
                del m["depends_on"]
        keep = [_ordered(m) for m in keep]
        proposed += len(keep)
        head = "\n".join(f"  # {line}" for line in _evidence(f))
        if keep:
            body = yaml.safe_dump(keep, sort_keys=False, default_flow_style=None, width=100)
            body = "\n".join("  " + ln if ln else ln for ln in body.splitlines())
        else:
            body = "  # (nothing new to propose for this host)"
        blocks.append(f"  # {'-' * 70}\n{head}\n{body}")

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = [
        f"# observe discovery {now}",
        f"# targets: {', '.join(targets[:8])}{' ...' if len(targets) > 8 else ''}",
        f"# scanned {stats['scanned']} addresses, {stats['responded']} responded, "
        f"{proposed} monitors proposed, {skipped} skipped as already configured",
        "#",
        "# Review before use. Names, groups, and thresholds are suggestions; depends_on is",
        "# not inferred. Copy the monitors you want under `monitors:` in observe.yaml,",
        "# then run --validate and --once before restarting the service.",
        "",
        "monitors:" if proposed else "monitors: []",
    ]
    text = "\n".join(header) + "\n" + "\n\n".join(blocks) + "\n"
    report = {"generated": now, "targets": targets, "stats": {**stats, "proposed": proposed,
                                                              "skipped": skipped},
              "hosts": [{k: v for k, v in asdict(f).items() if k != "unverified_certs"}
                        for f in findings],
              "unverified_certs": {f"{f.address}:{port}": c for f in findings
                                   for port, c in f.unverified_certs.items()}}
    return text, report


_FIELD_ORDER = ["name", "group", "type", "host", "url", "port", "credential", "known_hosts",
                "mode", "interface", "service", "disk", "mount", "container", "docker_command",
                "topic", "tls", "expect", "expect_status", "verify", "verify_tls", "ca_bundle",
                "node", "guest", "storage", "pool", "entity", "site", "device", "camera",
                "entity_id", "https", "ignore", "interval", "thresholds",
                "forecast", "depends_on"]


def _ordered(m: dict[str, Any]) -> dict[str, Any]:
    """Stable, readable key order in the proposal file."""
    rank = {k: i for i, k in enumerate(_FIELD_ORDER)}
    return dict(sorted(m.items(), key=lambda kv: rank.get(kv[0], len(rank))))


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


async def discover(config: Config, targets: list[str] | None = None,
                   credentials: list[str] | None = None, use_directory: bool = True,
                   directory_connection: Any = None) -> tuple[str, dict[str, Any]]:
    s = config.discovery.model_copy()
    if targets:
        s.targets = targets
    if credentials:
        unknown = [c for c in credentials if c not in config.credentials]
        if unknown:
            raise DiscoveryError(f"unknown credential(s): {', '.join(unknown)}")
        s.credentials = credentials
    for spec in s.targets:
        if _REF.search(spec):
            raise DiscoveryError(f"target {spec!r} contains an unresolved reference")

    dir_info: dict[str, DirectoryComputer] = {}
    dir_skipped: list[dict[str, str]] = []
    if use_directory and s.directory is not None:
        d = s.directory
        kw = {"connection_factory": directory_connection} if directory_connection else {}
        try:
            computers, dir_skipped = await fetch_computers_async(
                d, config.credentials[d.credential], d.ca_bundle or s.ca_bundle, **kw)
        except Exception as err:  # noqa: BLE001 - surface LDAP errors plainly
            raise DiscoveryError(f"directory query to {d.server} failed: "
                                 f"{type(err).__name__}: {err}") from err
        dir_info = {c.name: c for c in computers}
        log.info("directory: %d computers, %d skipped", len(computers), len(dir_skipped))
    all_targets = [*s.targets, *dir_info]
    if not all_targets:
        raise DiscoveryError("no targets: set discovery.targets, pass --target, or "
                             "configure discovery.directory")
    hosts = expand_targets(all_targets, s.exclude, s.max_hosts)
    log.info("discovering %d addresses with credentials %s", len(hosts), s.credentials or "none")
    config = config.model_copy(update={"discovery": s})
    disc = Discoverer(config, s)
    findings = await disc.run(hosts)
    responded = {f.address for f in findings}
    for f in findings:
        if f.address in dir_info:
            f.directory = asdict(dir_info[f.address])
    silent = [n for n in dir_info if n not in responded]
    shown_targets = [*s.targets] + ([f"AD {s.directory.base_dn} ({len(dir_info)} computers)"]
                                    if dir_info else [])
    text, report = render(findings, config, shown_targets, disc.stats)
    new_keys = [f.ssh_new_host_key for f in findings if f.ssh_new_host_key]
    report["stats"]["retired_credentials"] = [
        c for c, n in disc.auth_failures.items() if n >= s.max_auth_failures]
    report["directory"] = {"used": bool(dir_info), "skipped": dir_skipped,
                           "no_response": silent}
    report["new_ssh_host_keys"] = new_keys
    if silent:
        text += ("\n# Directory computers that did not respond to any probe:\n"
                 + "".join(f"#   {n}\n" for n in silent))
    if report["unverified_certs"]:
        text += ("\n# API endpoints whose certificate did not validate (credentials were not\n"
                 "# sent). Verify each fingerprint, then pin it: save the PEM (--certs-out)\n"
                 "# and reference it as ca_bundle, or issue a certificate from your CA:\n"
                 + "".join(f"#   {ep}  SHA-256 {c['sha256']}\n"
                           for ep, c in report["unverified_certs"].items()))
    if new_keys:
        text += ("\n# SSH host keys seen for the first time (trust on first use). Verify\n"
                 "# each fingerprint out of band before adding them to known_hosts:\n"
                 + "".join(f"#   {k}\n" for k in new_keys))
    return text, report
