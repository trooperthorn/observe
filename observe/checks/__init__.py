"""Check implementations, keyed by monitor `type`."""

from __future__ import annotations

from typing import Any

from ..config import Config
from .apps import HomeAssistantCheck, TechnitiumCheck, UniFiNetworkCheck, UniFiProtectCheck
from .base import Check, CheckResult, Result
from .mqtt import MqttCheck
from .host import PushedHostCheck
from .net import DnsCheck, HttpCheck, PingCheck, TcpCheck, TlsCertCheck
from .platforms import ProxmoxCheck, TrueNASCheck, VSphereCheck
from .snmp import SnmpCheck
from .ssh import DockerCheck, LinuxCheck
from .windows import WinRMCheck, WmiCheck

REGISTRY: dict[str, type[Check]] = {
    "ping": PingCheck,
    "tcp": TcpCheck,
    "http": HttpCheck,
    "dns": DnsCheck,
    "tls_cert": TlsCertCheck,
    "snmp": SnmpCheck,
    "winrm": WinRMCheck,
    "wmi": WmiCheck,
    "mqtt": MqttCheck,
    "linux": LinuxCheck,
    "docker": DockerCheck,
    "truenas": TrueNASCheck,
    "proxmox": ProxmoxCheck,
    "vsphere": VSphereCheck,
    "homeassistant": HomeAssistantCheck,
    "unifi_network": UniFiNetworkCheck,
    "unifi_protect": UniFiProtectCheck,
    "technitium": TechnitiumCheck,
    "pushed_host": PushedHostCheck,
}


def build_check(monitor: Any, config: Config, store: Any = None) -> Check:
    cls = REGISTRY[monitor.type]
    if cls is PushedHostCheck:
        return PushedHostCheck(monitor, config, store)
    if cls is HomeAssistantCheck:
        return HomeAssistantCheck(monitor, config, store)
    if cls is SnmpCheck:
        return SnmpCheck(monitor, config, store)
    return cls(monitor, config)


__all__ = ["Check", "CheckResult", "Result", "REGISTRY", "build_check"]
