"""Directory enumeration against ldap3's in-memory mock server. This proves
the query, paging, and filtering logic; it does not prove LDAPS against a
real domain controller."""

import datetime as dt

import yaml
from ldap3 import MOCK_SYNC, OFFLINE_AD_2012_R2, Connection, Server

from observe.config import Config, DirectorySettings, LdapCredential
from observe.directory import _as_datetime, fetch_computers
from observe.discovery import discover

NOW = dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)
BASE = "DC=lab,DC=example"


def ft(d: dt.datetime) -> int:
    return int((d - dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)).total_seconds() * 1e7)


def mock_factory(entries):
    def factory(s, cred, ca):
        server = Server(s.server, get_info=OFFLINE_AD_2012_R2)
        conn = Connection(server, user=cred.bind_user, password=cred.password,
                          client_strategy=MOCK_SYNC)
        conn.strategy.add_entry(cred.bind_user, {"userPassword": cred.password,
                                                 "sAMAccountName": "svc"})
        for dn, attrs in entries.items():
            conn.strategy.add_entry(dn, {"objectClass": ["top", "computer"], **attrs})
        return conn
    return factory


ENTRIES = {
    f"CN=DC01,OU=Domain Controllers,{BASE}": {
        "dNSHostName": "dc01.lab.example", "operatingSystem": "Windows Server 2025 Datacenter",
        "userAccountControl": 532480, "lastLogonTimestamp": ft(NOW - dt.timedelta(days=3))},
    f"CN=PI,OU=Linux,{BASE}": {
        "dNSHostName": "localhost", "operatingSystem": "Debian GNU/Linux 12",
        "userAccountControl": 4096, "lastLogonTimestamp": ft(NOW - dt.timedelta(days=1))},
    f"CN=OLD,OU=Servers,{BASE}": {
        "dNSHostName": "old.lab.example", "operatingSystem": "Windows Server 2012 R2",
        "userAccountControl": 4096, "lastLogonTimestamp": ft(NOW - dt.timedelta(days=400))},
    f"CN=OFF,OU=Servers,{BASE}": {
        "dNSHostName": "off.lab.example", "operatingSystem": "Windows Server 2022",
        "userAccountControl": 4098, "lastLogonTimestamp": ft(NOW - dt.timedelta(days=2))},
    f"CN=NEW,OU=Servers,{BASE}": {
        "dNSHostName": "new.lab.example", "operatingSystem": "Windows Server 2025",
        "userAccountControl": 4096},
    f"CN=NODNS,OU=Servers,{BASE}": {"userAccountControl": 4096},
}
S = DirectorySettings(server="dc01.lab.example", credential="ad", base_dn=BASE)
CRED = LdapCredential(type="ldap", bind_user=f"CN=svc,{BASE}", password="pw")


def test_filters_disabled_stale_and_unlogged_with_reasons():
    keep, skipped = fetch_computers(S, CRED, None, connection_factory=mock_factory(ENTRIES),
                                    now=NOW)
    assert [c.name for c in keep] == ["dc01.lab.example", "localhost"]
    reasons = {s["name"]: s["reason"] for s in skipped}
    assert reasons["off.lab.example"] == "account disabled"
    assert reasons["old.lab.example"].startswith("stale")
    assert reasons["new.lab.example"] == "no recorded logon"


def test_os_include_filter():
    s = S.model_copy(update={"os_include": ["linux"]})
    keep, _ = fetch_computers(s, CRED, None, connection_factory=mock_factory(ENTRIES), now=NOW)
    assert [c.name for c in keep] == ["localhost"]


def test_filetime_and_datetime_both_accepted():
    d = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
    assert _as_datetime(ft(d)) == d and _as_datetime(str(ft(d))) == d
    assert _as_datetime(d) == d and _as_datetime(0) is None and _as_datetime(None) is None


def test_directory_needs_an_ldap_credential():
    import pytest
    with pytest.raises(ValueError, match="not an ldap credential"):
        Config.model_validate({
            "credentials": {"ad": {"type": "mqtt", "username": "u", "password": "p"}},
            "discovery": {"directory": {"server": "dc", "credential": "ad", "base_dn": BASE}}})


async def test_discover_scans_directory_computers_and_lists_silent_ones():
    cfg = Config.model_validate({
        "credentials": {"ad": {"type": "ldap", "bind_user": f"CN=svc,{BASE}",
                               "password": "pw"}},
        "discovery": {"tcp_ports": [], "timeout": 0.5, "credentials": [],
                      "directory": {"server": "dc01.lab.example", "credential": "ad",
                                    "base_dn": BASE, "stale_days": 100000}},
    })
    text, report = await discover(cfg, directory_connection=mock_factory(ENTRIES))
    assert report["directory"]["used"]
    assert "dc01.lab.example" in report["directory"]["no_response"]
    assert "Directory computers that did not respond" in text
    responding = {h["address"] for h in report["hosts"]}
    assert responding <= {"localhost"}
    yaml.safe_load(text)
