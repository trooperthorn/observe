"""Configuration loading and validation.

The whole monitoring estate is declared in one YAML file. There are no write
endpoints in the web UI, so the file (kept in git if you like) is the single
source of truth. Secrets are never written into the YAML: they are referenced
as ${ENV_VAR} or ${file:/run/secrets/name} and resolved once at startup.
An unresolved reference is a startup error, never a silent empty string.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from . import compat

_REF = re.compile(r"\$\{([^}]+)\}")


class ConfigError(Exception):
    """Raised when the configuration cannot be loaded or is invalid."""


def _resolve_refs(value: Any, path: str = "") -> Any:
    """Recursively expand ${ENV} and ${file:/path} references."""
    if isinstance(value, dict):
        return {k: _resolve_refs(v, f"{path}.{k}" if path else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        ref = match.group(1)
        if ref.startswith("file:"):
            file_path = Path(ref[5:])
            try:
                return file_path.read_text(encoding="utf-8").strip()
            except OSError as err:
                raise ConfigError(f"{path}: cannot read secret file {file_path}: {err}") from err
        value = compat.getenv(ref)
        if value is None:
            raise ConfigError(f"{path}: environment variable {ref} is not set")
        return value

    return _REF.sub(repl, value)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# The address agents are told to call: http(s)://host[:port], nothing else. The character set
# holds no quote, space, slash beyond the scheme, semicolon, dollar sign, backtick or pipe, so the
# value is safe inside a shell word and a PowerShell string.
_PUBLIC_URL = re.compile(
    r"^(https?)://([A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9a-fA-F:]{2,45}\])"
    r"(?::([0-9]{1,5}))?$", re.IGNORECASE)


def normalise_public_url(value: Any) -> str:
    """The canonical form of an Observe address for install commands, or ValueError with a
    message safe to show. Never a loopback or wildcard name, because the command runs on another
    machine, where that name means the machine itself."""
    if not isinstance(value, str):
        raise ValueError("the address must be text such as https://observe.example.com:8080")
    text = value.strip()
    match = _PUBLIC_URL.fullmatch(text)
    if not match:
        raise ValueError("the address must look like https://observe.example.com or "
                         "http://192.0.2.10:8080: a scheme, a host name or IP address and an "
                         "optional port, with no path or other characters")
    scheme, host, port = match.group(1).lower(), match.group(2), match.group(3)
    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError("the port must be from 1 to 65535")
    bare = host.strip("[]").lower()
    try:
        ip = ipaddress.ip_address(bare)
        local = ip.is_loopback or ip.is_unspecified
    except ValueError:
        local = bare == "localhost" or bare.endswith(".localhost")
        if ".." in bare or bare.endswith("-") or bare.startswith("."):
            raise ValueError("the host name is not valid") from None
    if local:
        raise ValueError("the address must not be a loopback name such as localhost or "
                         "127.0.0.1: the install command runs on another machine, where that "
                         "name means the machine itself")
    return f"{scheme}://{bare if ':' not in bare else '[' + bare + ']'}" + (f":{int(port)}" if port else "")


# ----------------------------------------------------------------- credentials


class SnmpV2Credential(Strict):
    type: Literal["snmpv2c"]
    community: str


class SnmpV3Credential(Strict):
    type: Literal["snmpv3"]
    username: str
    level: Literal["authPriv", "authNoPriv"] = "authPriv"
    auth_protocol: Literal["SHA", "SHA-256", "SHA-512", "MD5"] = "SHA"
    auth_password: str
    priv_protocol: Literal["AES", "AES-256", "DES"] = "AES"
    priv_password: str | None = None

    @model_validator(mode="after")
    def _priv_needed(self) -> "SnmpV3Credential":
        if self.level == "authPriv" and not self.priv_password:
            raise ValueError("authPriv requires priv_password")
        return self


class WinRMCredential(Strict):
    type: Literal["winrm"]
    # CredSSP is not offered: the image does not ship the CredSSP extra, and
    # offering a transport that fails at runtime is worse than rejecting it
    # at config load. Kerberos needs the image built with the kerberos extra
    # (see Dockerfile) plus a keytab; it is not auto-detected here.
    transport: Literal["ntlm", "certificate", "kerberos"] = "ntlm"
    username: str | None = None
    password: str | None = None
    cert_pem: str | None = None  # path to client certificate (certificate transport)
    cert_key_pem: str | None = None  # path to its private key
    # kerberos transport: a service ticket is acquired with `kinit -kt` before
    # each session (see observe/checks/windows.py), never a stored password.
    principal: str | None = None  # e.g. observe@LAB.EXAMPLE.COM
    keytab_path: str | None = None  # path to the keytab, mounted read-only
    kerberos_hostname_override: str | None = None  # SPN host if it differs from the monitor's host

    @model_validator(mode="after")
    def _shape(self) -> "WinRMCredential":
        if self.transport == "certificate":
            if not (self.cert_pem and self.cert_key_pem):
                raise ValueError("certificate transport needs cert_pem and cert_key_pem")
        elif self.transport == "kerberos":
            if not (self.principal and self.keytab_path):
                raise ValueError("kerberos transport needs principal and keytab_path")
        elif not (self.username and self.password):
            raise ValueError(f"{self.transport} transport needs username and password")
        return self


class MqttCredential(Strict):
    type: Literal["mqtt"]
    username: str
    password: str


class SshCredential(Strict):
    """Key-based is strongly preferred. A password credential is never sent to
    a host whose key is not already in known_hosts, even during discovery."""

    type: Literal["ssh"]
    username: str
    private_key: str | None = None  # path to an OpenSSH private key
    passphrase: str | None = None
    password: str | None = None

    @model_validator(mode="after")
    def _one_method(self) -> "SshCredential":
        if not (self.private_key or self.password):
            raise ValueError("ssh credential needs private_key or password")
        return self


class LdapCredential(Strict):
    """Read-only directory account for discovery. bind_user is a UPN
    (svc@lab.example) or a DN. Always used over TLS."""

    type: Literal["ldap"]
    bind_user: str
    password: str


class TrueNASCredential(Strict):
    """A user-linked API key (TrueNAS 25.04+). Give the user the READONLY_ADMIN
    role; monitoring needs nothing more."""

    type: Literal["truenas"]
    username: str
    api_key: str


class ProxmoxCredential(Strict):
    """An API token, e.g. token_id "observe@pve!monitor". Grant PVEAuditor."""

    type: Literal["proxmox"]
    token_id: str = Field(pattern=r"^[^@\s]+@[^!\s]+![A-Za-z0-9_.\-]+$")
    secret: str


class VSphereCredential(Strict):
    """A read-only ESXi or vCenter account."""

    type: Literal["vsphere"]
    username: str
    password: str


class HomeAssistantCredential(Strict):
    """A long-lived access token from the HA user profile page. Use a
    dedicated non-admin user; reads need nothing more."""

    type: Literal["homeassistant"]
    token: str


class UniFiCredential(Strict):
    """An API key from UniFi OS Settings > Control Plane > Integrations.
    One key serves both the Network and Protect integration APIs."""

    type: Literal["unifi"]
    api_key: str


class UniFiClassicCredential(Strict):
    """A dedicated local view-only account on the UniFi OS console, used only by the observe_unifi
    plugin for the classic controller reads (PoE watts, per-port VLAN, LLDP neighbours). The
    Integration API key path works without it."""

    type: Literal["unifi_classic"]
    username: str
    password: str


class TechnitiumCredential(Strict):
    """An API token (Administration > Sessions > Create Token), ideally for a
    user whose only permission is Dashboard: View."""

    type: Literal["technitium"]
    token: str


Credential = Annotated[
    Union[SnmpV2Credential, SnmpV3Credential, WinRMCredential, MqttCredential,
          SshCredential, LdapCredential, TrueNASCredential, ProxmoxCredential,
          VSphereCredential, HomeAssistantCredential, UniFiCredential,
          UniFiClassicCredential, TechnitiumCredential],
    Field(discriminator="type"),
]

# -------------------------------------------------------------------- monitors


class Thresholds(Strict):
    """Numeric thresholds applied to a check's value.

    direction "above": value >= crit is DOWN, value >= warn is WARN.
    direction "below": value <= crit is DOWN, value <= warn is WARN.
    """

    direction: Literal["above", "below"] = "above"
    warn: float | None = None
    crit: float | None = None


class MonitorBase(Strict):
    name: str
    group: str = "default"
    interval: int | None = None
    timeout: float | None = None
    failures_to_down: int | None = None
    recoveries_to_up: int | None = None
    thresholds: Thresholds | None = None
    alerts: list[str] | None = None  # alert target names; None means all
    enabled: bool = True
    # Status rollup: names (or slugs) of monitors this one sits behind, e.g. a
    # server behind a switch. While a parent is DOWN this monitor reports
    # UNREACHABLE and its alerts are suppressed.
    depends_on: list[str] = Field(default_factory=list)
    # Group rollup: a non-critical member that is DOWN degrades its group to
    # WARN instead of DOWN.
    critical: bool = True
    # Capacity forecasting (opt-in): fit a trend to this monitor's numeric
    # history and project when it crosses its warn/crit thresholds.
    forecast: bool = False

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")


class ComponentThresholds(Thresholds):
    """Thresholds for one pushed reading, named by its source and metric.

    The latest value of every sample with this source and metric is compared.
    """

    source: str = Field(min_length=1)
    metric: str = Field(min_length=1)


class PushedHostMonitor(MonitorBase):
    """A host that pushes hostwatch batches to POST /api/ingest.

    Nothing is polled. Each check reads the latest batch from the store and
    derives Good, Warning or Critical per component. `host` is the host name
    the agent sends, which is also the name its ingest key is bound to.
    """

    type: Literal["pushed_host"]
    host: str = Field(min_length=1)
    # Seconds without a batch before the host counts as a failure. Default is
    # three monitor intervals, so one late push does not count.
    stale_after: float | None = Field(default=None, gt=0)
    components: list[ComponentThresholds] = Field(default_factory=list)
    # Sources that must be available. An unavailable one is a Warning. Other
    # unavailable sources are ignored, because a host without a GPU is normal.
    require_sources: list[str] = Field(default_factory=list)
    # A boot classified as a crash (panic, watchdog reset, power loss, unknown
    # unclean) makes the monitor WARN, or DOWN when crash_result is "fail", for
    # crash_hold_s seconds after the boot event. Both go through the normal
    # failures_to_down confirmation. A later clean boot clears it at once.
    crash_hold_s: float = Field(default=3600, gt=0)
    crash_result: Literal["warn", "fail"] = "warn"


class PingMonitor(MonitorBase):
    type: Literal["ping"]
    host: str
    count: int = 3


class TcpMonitor(MonitorBase):
    type: Literal["tcp"]
    host: str
    port: int
    send: str | None = None
    expect: str | None = None  # substring expected in the first bytes read


class HttpMonitor(MonitorBase):
    type: Literal["http"]
    url: str
    method: Literal["GET", "HEAD", "POST"] = "GET"
    expect_status: list[int] = Field(default_factory=lambda: [200])
    expect_text: str | None = None
    verify_tls: bool = True
    ca_bundle: str | None = None  # path to a PEM bundle for a private CA
    follow_redirects: bool = False
    headers: dict[str, str] = Field(default_factory=dict)


class DnsMonitor(MonitorBase):
    type: Literal["dns"]
    query: str
    record: str = "A"
    nameserver: str | None = None  # None uses the container resolver
    expect: list[str] | None = None  # every listed value must be present


class TlsCertMonitor(MonitorBase):
    type: Literal["tls_cert"]
    host: str
    port: int = 443
    server_name: str | None = None  # SNI and hostname check; defaults to host
    verify: bool = True
    ca_bundle: str | None = None


class SnmpMonitor(MonitorBase):
    type: Literal["snmp"]
    host: str
    port: int = 161
    credential: str
    mode: Literal["oid", "uptime", "interface", "cpu", "memory"] = "oid"
    oid: str | None = None  # mode oid
    interface: str | None = None  # mode interface: ifName, ifDescr, or numeric ifIndex

    @model_validator(mode="after")
    def _mode_args(self) -> "SnmpMonitor":
        if self.mode == "oid" and not self.oid:
            raise ValueError("mode oid requires oid")
        if self.mode == "interface" and not self.interface:
            raise ValueError("mode interface requires interface")
        return self


class _WsManTarget(MonitorBase):
    host: str
    credential: str
    port: int = 5986
    https: bool = True
    verify_tls: bool = True
    ca_bundle: str | None = None


class WinRMMonitor(_WsManTarget):
    type: Literal["winrm"]
    mode: Literal["service", "cpu", "memory", "disk", "powershell"] = "service"
    service: str | None = None
    disk: str = Field("C:", pattern=r"^[A-Za-z]:$")
    script: str | None = None  # mode powershell: must print a single number

    @model_validator(mode="after")
    def _mode_args(self) -> "WinRMMonitor":
        if self.mode == "service" and not self.service:
            raise ValueError("mode service requires service")
        if self.mode == "powershell" and not self.script:
            raise ValueError("mode powershell requires script")
        return self


class WmiMonitor(_WsManTarget):
    """A WQL query, transported over WinRM (WS-Management), not DCOM."""

    type: Literal["wmi"]
    namespace: str = "root/cimv2"
    query: str
    property: str  # numeric property read from the result rows
    aggregate: Literal["first", "sum", "avg", "max", "min", "count"] = "first"


class MqttMonitor(MonitorBase):
    type: Literal["mqtt"]
    host: str
    port: int = 1883
    credential: str | None = None
    tls: bool = False
    ca_bundle: str | None = None
    topic: str | None = None  # None: broker connect/auth check only
    expect: str | None = None  # substring expected in the payload
    numeric: bool = False  # parse the payload as a number for thresholds


_SAFE_UNIT = r"^[A-Za-z0-9@._:\-]+$"
_SAFE_MOUNT = r"^/[A-Za-z0-9._/\-]*$"
_SAFE_CONTAINER = r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$"
_SAFE_DOCKER_CMD = r"^(sudo -n )?(/[A-Za-z0-9._/\-]+/)?docker$"


class _SshTarget(MonitorBase):
    host: str
    port: int = 22
    credential: str
    # Host keys are always verified against this file. There is no
    # "accept any key" option for monitors.
    known_hosts: str = "/config/known_hosts"


class LinuxMonitor(_SshTarget):
    """Agentless Linux checks over SSH, reading /proc and standard tools."""

    type: Literal["linux"]
    mode: Literal["cpu", "memory", "disk", "load", "uptime", "service", "command"] = "cpu"
    mount: str = Field("/", pattern=_SAFE_MOUNT)
    service: str | None = Field(None, pattern=_SAFE_UNIT)  # systemd unit
    command: str | None = None  # mode command: must print one number

    @model_validator(mode="after")
    def _mode_args(self) -> "LinuxMonitor":
        if self.mode == "service" and not self.service:
            raise ValueError("mode service requires service")
        if self.mode == "command" and not self.command:
            raise ValueError("mode command requires command")
        return self


class DockerMonitor(_SshTarget):
    """Docker container state via the docker CLI on the host, over SSH."""

    type: Literal["docker"]
    mode: Literal["container", "summary"] = "container"
    container: str | None = Field(None, pattern=_SAFE_CONTAINER)
    # "docker", "sudo -n docker", or an absolute path to either.
    docker_command: str = Field("docker", pattern=_SAFE_DOCKER_CMD)

    @model_validator(mode="after")
    def _mode_args(self) -> "DockerMonitor":
        if self.mode == "container" and not self.container:
            raise ValueError("mode container requires container")
        return self


class _ApiTarget(MonitorBase):
    host: str
    credential: str
    verify_tls: bool = True
    # A PEM bundle to trust. It may be the device's own self-signed
    # certificate: partial-chain verification is enabled, so a single pinned
    # leaf works as a trust anchor. The hostname must still match.
    ca_bundle: str | None = None


class TrueNASMonitor(_ApiTarget):
    """TrueNAS over the JSON-RPC 2.0 WebSocket API (wss://host/api/current)."""

    type: Literal["truenas"]
    port: int = 443
    mode: Literal["pools", "pool", "alerts"] = "pools"
    pool: str | None = None
    min_alert_level: Literal["INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL"] = "WARNING"

    @model_validator(mode="after")
    def _mode_args(self) -> "TrueNASMonitor":
        if self.mode == "pool" and not self.pool:
            raise ValueError("mode pool requires pool")
        return self


class ProxmoxMonitor(_ApiTarget):
    """Proxmox VE over its REST API (/api2/json) with an API token."""

    type: Literal["proxmox"]
    port: int = 8006
    mode: Literal["node", "node_cpu", "node_memory", "guest", "storage"] = "node"
    node: str | None = None  # node name (node modes; storage when not shared)
    guest: str | None = None  # VM or container name, or numeric VMID
    storage: str | None = None

    @model_validator(mode="after")
    def _mode_args(self) -> "ProxmoxMonitor":
        if self.mode.startswith("node") and not self.node:
            raise ValueError(f"mode {self.mode} requires node")
        if self.mode == "guest" and not self.guest:
            raise ValueError("mode guest requires guest")
        if self.mode == "storage" and not self.storage:
            raise ValueError("mode storage requires storage")
        return self


class VSphereMonitor(_ApiTarget):
    """ESXi host or vCenter via the vSphere API (pyVmomi)."""

    type: Literal["vsphere"]
    port: int = 443
    mode: Literal["host", "host_cpu", "host_memory", "datastore", "vm"] = "host"
    # Name of the host, datastore, or VM. Optional for host modes against a
    # standalone ESXi host (its only host is used).
    entity: str | None = None

    @model_validator(mode="after")
    def _mode_args(self) -> "VSphereMonitor":
        if self.mode in ("datastore", "vm") and not self.entity:
            raise ValueError(f"mode {self.mode} requires entity")
        return self


class HomeAssistantMonitor(_ApiTarget):
    """Home Assistant REST API (Authorization: Bearer <token>)."""

    type: Literal["homeassistant"]
    port: int = 8123
    # HA serves plain HTTP unless you configured TLS. With https: false the
    # token crosses the network in cleartext; see THREAT-MODEL.md.
    https: bool = True
    mode: Literal["api", "entity", "unavailable", "updates", "host"] = "api"
    # host mode: the host name the readings are stored under (the Hosts page). It is polled
    # every 300 s unless interval is set.
    host_name: str = Field("homeassistant", min_length=1, max_length=128)
    entity_id: str | None = Field(None, pattern=r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
    expect: list[str] | None = None  # entity mode: allowed states
    domains: list[str] = Field(default_factory=list)  # unavailable mode: limit to these
    ignore: list[str] = Field(default_factory=list)  # unavailable mode: fnmatch patterns

    @model_validator(mode="after")
    def _mode_args(self) -> "HomeAssistantMonitor":
        if self.mode == "entity" and not self.entity_id:
            raise ValueError("mode entity requires entity_id")
        if self.mode == "host" and self.interval is None:
            self.interval = 300
        return self


class UniFiNetworkMonitor(_ApiTarget):
    """UniFi Network Integration API (X-API-KEY)."""

    type: Literal["unifi_network"]
    port: int = 443
    base_path: str = "/proxy/network/integration/v1"  # "/integration/v1" on standalone
    site: str | None = None  # site name; the only site when omitted
    mode: Literal["devices", "device", "device_cpu", "device_memory", "firmware",
                  "ports"] = "devices"
    device: str | None = None  # name or MAC address
    ignore: list[str] = Field(default_factory=list)  # device names to leave out of summaries

    @model_validator(mode="after")
    def _mode_args(self) -> "UniFiNetworkMonitor":
        needs_device = self.mode in ("device", "device_cpu", "device_memory", "ports")
        if needs_device and not self.device:
            raise ValueError(f"mode {self.mode} requires device")
        return self


class UniFiProtectMonitor(_ApiTarget):
    """UniFi Protect Integration API (X-API-KEY). This API reports device
    connection state; it does not expose recording state."""

    type: Literal["unifi_protect"]
    port: int = 443
    base_path: str = "/proxy/protect/integration/v1"
    mode: Literal["info", "cameras", "camera"] = "cameras"
    camera: str | None = None  # name or MAC address
    ignore: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _mode_args(self) -> "UniFiProtectMonitor":
        if self.mode == "camera" and not self.camera:
            raise ValueError("mode camera requires camera")
        return self


class TechnitiumMonitor(_ApiTarget):
    """Technitium DNS Server HTTP API."""

    type: Literal["technitium"]
    port: int = 5380
    https: bool = False  # the web service defaults to HTTP on 5380
    mode: Literal["stats", "update"] = "stats"
    range: Literal["LastHour", "LastDay"] = "LastHour"
    # Current Technitium accepts "Authorization: Bearer <token>". Older
    # versions only accept ?token= in the URL, which lands in logs; enable
    # this only if your version rejects the header.
    token_in_query: bool = False


Monitor = Annotated[
    Union[
        HomeAssistantMonitor,
        UniFiNetworkMonitor,
        UniFiProtectMonitor,
        TechnitiumMonitor,
        TrueNASMonitor,
        ProxmoxMonitor,
        VSphereMonitor,
        LinuxMonitor,
        DockerMonitor,
        PingMonitor,
        TcpMonitor,
        HttpMonitor,
        DnsMonitor,
        TlsCertMonitor,
        SnmpMonitor,
        WinRMMonitor,
        WmiMonitor,
        MqttMonitor,
        PushedHostMonitor,
    ],
    Field(discriminator="type"),
]

# ---------------------------------------------------------------------- alerts


class AlertBase(Strict):
    name: str
    # Named notify_on, not "on": YAML 1.1 parses a bare `on:` key as boolean true.
    notify_on: list[Literal["down", "warn", "up"]] = Field(default_factory=lambda: ["down", "up"])


class WebhookAlert(AlertBase):
    type: Literal["webhook"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class NtfyAlert(AlertBase):
    type: Literal["ntfy"]
    url: str  # full topic URL, e.g. https://ntfy.example.net/homelab
    token: str | None = None


class SmtpAlert(AlertBase):
    type: Literal["smtp"]
    host: str
    port: int = 587
    starttls: bool = True
    username: str | None = None
    password: str | None = None
    sender: str
    recipients: list[str]


class MqttAlert(AlertBase):
    """Publishes retained state per monitor, e.g. for Home Assistant."""

    type: Literal["mqtt"]
    host: str
    port: int = 1883
    credential: str | None = None
    tls: bool = False
    ca_bundle: str | None = None
    topic_prefix: str = "observe"


Alert = Annotated[
    Union[WebhookAlert, NtfyAlert, SmtpAlert, MqttAlert], Field(discriminator="type")
]

# ---------------------------------------------------------------------- server


class ServerConfig(Strict):
    listen: str = "0.0.0.0"
    port: int = 8080
    db_path: str = "/data/observe.db"
    retention_days: int = Field(default=30, ge=1)
    audit_retention_days: int = Field(default=365, ge=1)
    ingest_rate_per_minute: int = Field(default=120, ge=1)
    basic_auth_user: str | None = None
    basic_auth_password: str | None = None
    max_concurrency: int = 32
    # Logins and sessions (observe/auth.py). The argon2 defaults follow the
    # argon2-cffi RFC 9106 low-memory profile; tests lower them.
    session_idle_s: int = Field(default=1800, ge=60)
    session_absolute_s: int = Field(default=43200, ge=60)
    session_cookie_secure: bool = True
    login_max_failures: int = Field(default=5, ge=1)
    login_lock_s: int = Field(default=900, ge=1)
    login_rate_per_minute: int = Field(default=20, ge=1)
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_memory_kib: int = Field(default=65536, ge=8)
    argon2_parallelism: int = Field(default=4, ge=1)
    plugin_rate_per_minute: int = Field(default=300, ge=1)  # requests per peer to plugin routes
    # The address install commands tell hosts to call (docs/GUI-DESIGN.md section 3.10). When it
    # is unset, an admin confirms one in the Add host wizard. The request's Host header is never used.
    public_url: str | None = None

    @field_validator("public_url")
    @classmethod
    def _public_url(cls, value: str | None) -> str | None:
        return None if value is None else normalise_public_url(value)


class ForecastSettings(Strict):
    lookback_days: float = 7.0  # history window fitted
    min_points: int = 24  # hourly buckets required before projecting
    min_span_hours: float = 24.0  # history must cover at least this long
    horizon_days: float = 90.0  # crossings further out are reported as "none"
    min_r2: float = 0.5  # below this the projection is labelled low confidence


class MapSettings(Strict):
    """The infrastructure map (docs/FIELD-DATA.md)."""

    stale_days: int = Field(default=90, ge=1, le=3650)  # unconfirmed links fade, then hide at 2x
    auto_depends: bool = True  # apply LLDP or CDP confirmed dependencies without review


class DirectorySettings(Strict):
    """Pull computer accounts from Active Directory over LDAPS."""

    server: str  # a DC's DNS name; must match its LDAPS certificate
    port: int = 636
    credential: str  # an `ldap` credential
    base_dn: str  # e.g. "DC=lab,DC=example" or an OU to narrow the search
    ca_bundle: str | None = None  # falls back to discovery.ca_bundle
    stale_days: int = 60  # skip accounts with no logon for this long
    os_include: list[str] = Field(default_factory=list)  # substrings; empty = all
    include_disabled: bool = False


class DiscoverySettings(Strict):
    """Inputs for `--discover`. Discovery proposes monitors; it never edits
    the running configuration."""

    # CIDR (192.0.2.0/24), range (192.0.2.10-192.0.2.40), single IP, or hostname.
    targets: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    # Names from `credentials:` to try, in order. Only these are ever sent.
    credentials: list[str] = Field(default_factory=list)
    max_hosts: int = 4096  # refuse to expand a target list larger than this
    concurrency: int = 64
    timeout: float = 1.5
    snmp_port: int = 161
    tcp_ports: list[int] = Field(
        default_factory=lambda: [22, 80, 443, 1883, 3389, 5986, 8006, 8080, 8123, 8443, 8883])
    mqtt_ports: list[int] = Field(default_factory=lambda: [1883, 8883])
    winrm_port: int = 5986
    # WinRM credentials are only sent after the listener's TLS certificate
    # validates against this bundle (or the system store). See THREAT-MODEL.md.
    ca_bundle: str | None = None
    winrm_require_valid_tls: bool = True
    # Stop trying a credential after this many authentication failures, so a
    # wrong password can not walk a domain account into lockout.
    max_auth_failures: int = 3
    max_interfaces: int = 16  # per SNMP device, fastest links first
    directory: DirectorySettings | None = None
    # SSH: host keys are checked against ssh_known_hosts. With
    # ssh_trust_on_first_use, a host whose key is not listed may still be
    # probed with KEY-based credentials only (a password is never offered to
    # an unverified host), and its key is written to the proposed known_hosts
    # file for you to review.
    ssh_known_hosts: str = "/config/known_hosts"
    ssh_trust_on_first_use: bool = True
    ssh_port: int = 22
    # API credentials (TrueNAS, Proxmox, vSphere) are only sent after the
    # endpoint's certificate validates. Unvalidated certificates are captured
    # for review (--certs-out) so you can pin them with ca_bundle.
    api_require_valid_tls: bool = True
    proxmox_port: int = 8006
    https_api_port: int = 443  # where vSphere, TrueNAS, and UniFi APIs are probed
    homeassistant_port: int = 8123
    technitium_port: int = 5380
    max_guests: int = 50  # VMs/containers proposed per hypervisor
    docker_command: str = Field("docker", pattern=_SAFE_DOCKER_CMD)
    max_containers: int = 25  # per Docker host


class Defaults(Strict):
    interval: int = 60
    timeout: float = 5.0
    failures_to_down: int = 3
    recoveries_to_up: int = 1


class Config(Strict):
    server: ServerConfig = Field(default_factory=ServerConfig)
    defaults: Defaults = Field(default_factory=Defaults)
    forecast: ForecastSettings = Field(default_factory=ForecastSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    map: MapSettings = Field(default_factory=MapSettings)
    credentials: dict[str, Credential] = Field(default_factory=dict)
    alerts: list[Alert] = Field(default_factory=list)
    monitors: list[Monitor] = Field(default_factory=list)
    # Plugins to load by entry point name (observe/plugins.py). Installed plugins that are
    # not listed here are never imported. Each one's settings sit under plugin_settings.<name>.
    plugins: list[str] = Field(default_factory=list)
    plugin_settings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    _applied: dict[str, list[str]] = PrivateAttr(default_factory=dict)

    @field_validator("plugins")
    @classmethod
    def _unique_plugins(cls, names: list[str]) -> list[str]:
        if len(set(names)) != len(names):
            raise ValueError("plugins: a plugin is listed more than once")
        return names

    @field_validator("monitors")
    @classmethod
    def _unique_names(cls, monitors: list[Any]) -> list[Any]:
        seen: set[str] = set()
        for mon in monitors:
            if mon.slug in seen:
                raise ValueError(f"duplicate monitor name (after slugging): {mon.name}")
            seen.add(mon.slug)
        return monitors

    @model_validator(mode="after")
    def _references(self) -> "Config":
        alert_names = {a.name for a in self.alerts}
        expected = {
            "snmp": ("snmpv2c", "snmpv3"),
            "winrm": ("winrm",),
            "wmi": ("winrm",),
            "mqtt": ("mqtt",),
            "linux": ("ssh",),
            "docker": ("ssh",),
            "truenas": ("truenas",),
            "proxmox": ("proxmox",),
            "vsphere": ("vsphere",),
            "homeassistant": ("homeassistant",),
            "unifi_network": ("unifi",),
            "unifi_protect": ("unifi",),
            "technitium": ("technitium",),
        }
        for mon in self.monitors:
            cred_name = getattr(mon, "credential", None)
            if cred_name is not None:
                if cred_name not in self.credentials:
                    raise ValueError(f"monitor {mon.name!r}: unknown credential {cred_name!r}")
                if self.credentials[cred_name].type not in expected[mon.type]:
                    raise ValueError(
                        f"monitor {mon.name!r}: credential {cred_name!r} is type "
                        f"{self.credentials[cred_name].type}, expected {expected[mon.type]}"
                    )
            for alert in mon.alerts or []:
                if alert not in alert_names:
                    raise ValueError(f"monitor {mon.name!r}: unknown alert {alert!r}")
        for alert in self.alerts:
            cred_name = getattr(alert, "credential", None)
            if cred_name is not None and cred_name not in self.credentials:
                raise ValueError(f"alert {alert.name!r}: unknown credential {cred_name!r}")
        self._check_dependencies()
        for name in self.discovery.credentials:
            if name not in self.credentials:
                raise ValueError(f"discovery: unknown credential {name!r}")
        d = self.discovery.directory
        if d is not None:
            cred = self.credentials.get(d.credential)
            if cred is None or cred.type != "ldap":
                raise ValueError(f"discovery.directory: {d.credential!r} is not an ldap credential")
        for mon in self.monitors:
            if mon.forecast and mon.thresholds is None and mon.type != "tls_cert":
                raise ValueError(f"monitor {mon.name!r}: forecast needs thresholds to project to")
        for name in self.plugin_settings:
            if name not in self.plugins:
                raise ValueError(f"plugin_settings.{name}: that plugin is not listed in plugins")
        if bool(self.server.basic_auth_user) != bool(self.server.basic_auth_password):
            raise ValueError("server.basic_auth_user and basic_auth_password go together")
        return self

    def resolve_monitor(self, ref: str) -> Any | None:
        for mon in self.monitors:
            if ref in (mon.name, mon.slug):
                return mon
        return None

    def configured_parents(self, mon: Any) -> list[Any]:
        """Parents named in the YAML only."""
        return [p for p in (self.resolve_monitor(r) for r in mon.depends_on) if p is not None]

    def parents(self, mon: Any) -> list[Any]:
        """The effective parents: the YAML plus the edges the infrastructure map applied."""
        out = self.configured_parents(mon)
        for slug in self._applied.get(mon.slug, ()):
            extra = self.resolve_monitor(slug)
            if extra is not None and extra not in out:
                out.append(extra)
        return out

    def set_applied_dependencies(self, edges: dict[str, list[str]]) -> None:
        """Replace the applied map edges, child slug to parent slugs. They are never written to
        the YAML and are recomputed from the map, so removing an edge here removes it for good."""
        self._applied = {k: list(v) for k, v in edges.items()}

    def _check_dependencies(self) -> None:
        for mon in self.monitors:
            for ref in mon.depends_on:
                parent = self.resolve_monitor(ref)
                if parent is None:
                    raise ValueError(f"monitor {mon.name!r}: depends_on unknown monitor {ref!r}")
                if parent is mon:
                    raise ValueError(f"monitor {mon.name!r}: depends on itself")
        # Cycle detection by depth-first search with colouring.
        colour: dict[str, int] = {}

        def visit(mon: Any, path: list[str]) -> None:
            c = colour.get(mon.slug, 0)
            if c == 1:
                raise ValueError("dependency cycle: " + " -> ".join(path + [mon.slug]))
            if c == 2:
                return
            colour[mon.slug] = 1
            for parent in self.configured_parents(mon):
                visit(parent, path + [mon.slug])
            colour[mon.slug] = 2

        for mon in self.monitors:
            visit(mon, [])

    def effective(self, mon: Any, field: str) -> Any:
        value = getattr(mon, field)
        return getattr(self.defaults, field) if value is None else value


def load_config(path: str | Path) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    resolved = _resolve_refs(raw)
    try:
        return Config.model_validate(resolved)
    except Exception as err:  # pydantic.ValidationError, surfaced verbatim
        raise ConfigError(str(err)) from err
