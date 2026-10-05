"""Pull computer accounts from Active Directory for discovery.

Connection: LDAPS (636) with the chain and the DC's hostname validated
(ldap3 Tls with CERT_REQUIRED, which runs its hostname check after the
handshake), then a simple bind as a read-only account. Simple bind is used
deliberately: inside TLS the password is protected, and it does not depend
on NTLM, which DCs are increasingly configured to restrict. Plain LDAP (389)
without TLS is not offered.

Query: computer objects that have a dNSHostName, paged 500 at a time (AD's
default MaxPageSize is 1000). Filtering for disabled accounts, stale
accounts, and operating system is done client-side so the rules are
visible here and testable, and every skipped account is reported with its
reason rather than silently dropped.

The account needs only default read access to computer objects; any
authenticated domain user has it unless you have tightened the defaults.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ssl
from dataclasses import dataclass
from typing import Any, Callable

from ldap3 import NONE, SUBTREE, Connection, Server, Tls

from .config import DirectorySettings, LdapCredential

ATTRS = ["dNSHostName", "operatingSystem", "operatingSystemVersion", "userAccountControl",
         "lastLogonTimestamp"]
FILTER = "(&(objectClass=computer)(dNSHostName=*))"
UAC_ACCOUNTDISABLE = 0x2
_FILETIME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)


@dataclass
class DirectoryComputer:
    name: str
    os: str | None
    os_version: str | None
    last_logon: str | None
    dn: str


def _as_datetime(value: Any) -> dt.datetime | None:
    """lastLogonTimestamp arrives as a datetime when ldap3 knows the schema,
    or as a FILETIME integer (100 ns ticks since 1601) when it does not."""
    if value in (None, "", [], 0, "0"):
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    try:
        ticks = int(value)
    except (TypeError, ValueError):
        return None
    return _FILETIME_EPOCH + dt.timedelta(microseconds=ticks // 10) if ticks > 0 else None


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def default_connection(s: DirectorySettings, cred: LdapCredential,
                       ca_bundle: str | None) -> Connection:
    tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=ca_bundle)
    server = Server(s.server, port=s.port, use_ssl=True, tls=tls, get_info=NONE,
                    connect_timeout=10)
    return Connection(server, user=cred.bind_user, password=cred.password, read_only=True,
                      raise_exceptions=True, receive_timeout=30)


def fetch_computers(
    s: DirectorySettings, cred: LdapCredential, ca_bundle: str | None,
    connection_factory: Callable[..., Connection] = default_connection,
    now: dt.datetime | None = None,
) -> tuple[list[DirectoryComputer], list[dict[str, str]]]:
    """Blocking. Returns (computers to scan, skipped accounts with reasons)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    conn = connection_factory(s, cred, ca_bundle)
    conn.bind()
    try:
        entries = conn.extend.standard.paged_search(
            s.base_dn, FILTER, SUBTREE, attributes=ATTRS, paged_size=500, generator=True)
        keep: list[DirectoryComputer] = []
        skipped: list[dict[str, str]] = []
        stale_before = now - dt.timedelta(days=s.stale_days)
        wanted = [w.lower() for w in s.os_include]
        for e in entries:
            if e.get("type") not in (None, "searchResEntry"):
                continue  # referrals
            a = e.get("attributes", {})
            name = str(_first(a.get("dNSHostName")) or "").lower()
            if not name:
                continue
            os_name = _first(a.get("operatingSystem"))
            uac = int(_first(a.get("userAccountControl")) or 0)
            last = _as_datetime(a.get("lastLogonTimestamp"))
            if uac & UAC_ACCOUNTDISABLE and not s.include_disabled:
                skipped.append({"name": name, "reason": "account disabled"})
                continue
            if last is None:
                skipped.append({"name": name, "reason": "no recorded logon"})
                continue
            if last < stale_before:
                skipped.append({"name": name,
                                "reason": f"stale: last logon {last:%Y-%m-%d}, "
                                          f"older than {s.stale_days} days"})
                continue
            if wanted and not any(w in str(os_name or "").lower() for w in wanted):
                skipped.append({"name": name, "reason": f"OS {os_name!r} not in os_include"})
                continue
            keep.append(DirectoryComputer(
                name=name, os=os_name, os_version=_first(a.get("operatingSystemVersion")),
                last_logon=last.isoformat(), dn=str(e.get("dn"))))
        keep.sort(key=lambda c: c.name)
        return keep, skipped
    finally:
        conn.unbind()


async def fetch_computers_async(s: DirectorySettings, cred: LdapCredential,
                                ca_bundle: str | None, **kw: Any
                                ) -> tuple[list[DirectoryComputer], list[dict[str, str]]]:
    return await asyncio.to_thread(fetch_computers, s, cred, ca_bundle, **kw)
