"""Shared fixtures.

Integration tests talk to real services on loopback and skip themselves if
the service is absent:

  snmpd      udp 127.0.0.1:1161, v2c community "testcomm",
             v3 user "v3user" SHA/AES "authpass123"/"privpass123"
  mosquitto  tcp 127.0.0.1:18830, anonymous

tests/services.sh starts both with those settings.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from typing import Any

import pytest

from observe.config import Config

SNMP_PORT = int(os.environ.get("OBSERVE_TEST_SNMP_PORT", "1161"))
MQTT_PORT = int(os.environ.get("OBSERVE_TEST_MQTT_PORT", "18830"))


def make_config(monitors: list[dict[str, Any]], **extra: Any) -> Config:
    base: dict[str, Any] = {
        "defaults": {"timeout": 2, "failures_to_down": 1},
        "credentials": {
            "v2": {"type": "snmpv2c", "community": "testcomm"},
            "v2bad": {"type": "snmpv2c", "community": "wrong-community"},
            "v3": {"type": "snmpv3", "username": "v3user", "auth_protocol": "SHA",
                   "auth_password": "authpass123", "priv_protocol": "AES",
                   "priv_password": "privpass123"},
            "win": {"type": "winrm", "username": "LAB\\svc", "password": "pw"},
        },
        "monitors": monitors,
    }
    base.update(extra)
    return Config.model_validate(base)


def _snmpd_up() -> bool:
    if not shutil.which("snmpget"):
        return False
    r = subprocess.run(
        ["snmpget", "-m", "", "-v2c", "-c", "testcomm", "-t", "1", "-r", "0",
         f"udp:127.0.0.1:{SNMP_PORT}", ".1.3.6.1.2.1.1.3.0"],
        capture_output=True, check=False)
    return r.returncode == 0


def _mqtt_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", MQTT_PORT), timeout=1):
            return True
    except OSError:
        return False


# In CI, OBSERVE_REQUIRE_SERVICES=1 turns a missing service into a hard
# failure, so a green run can not silently mean "integration tests skipped".
if os.environ.get("OBSERVE_REQUIRE_SERVICES") == "1":
    assert _snmpd_up(), "OBSERVE_REQUIRE_SERVICES=1 but test snmpd is not reachable"
    assert _mqtt_up(), "OBSERVE_REQUIRE_SERVICES=1 but test mosquitto is not reachable"

needs_snmpd = pytest.mark.skipif(not _snmpd_up(), reason="test snmpd not running")
needs_mqtt = pytest.mark.skipif(not _mqtt_up(), reason="test mosquitto not running")
