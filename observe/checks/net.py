"""Network-level checks: ICMP, TCP, HTTP(S), DNS, and TLS certificate expiry."""

from __future__ import annotations

import asyncio
import datetime as dt
import ssl

import dns.asyncresolver
import dns.exception
import httpx
from cryptography import x509
from icmplib import async_ping
from icmplib.exceptions import ICMPLibError, SocketPermissionError

from ..config import Thresholds
from .base import Check, CheckResult, Result


class PingCheck(Check):
    """ICMP echo. Value is average RTT in ms; packet loss is in detail.

    Uses unprivileged ICMP (SOCK_DGRAM). The container therefore needs the
    net.ipv4.ping_group_range sysctl to include its group; the provided
    compose file sets it. No CAP_NET_RAW is required.
    """

    async def probe(self) -> CheckResult:
        m = self.monitor
        try:
            host = await async_ping(
                m.host, count=m.count, interval=0.2, timeout=self.timeout, privileged=False
            )
        except SocketPermissionError:
            return CheckResult.fail(
                "unprivileged ICMP not permitted: set sysctl net.ipv4.ping_group_range"
            )
        except ICMPLibError as err:
            return CheckResult.fail(f"ping error: {err}")
        detail = {"packet_loss": host.packet_loss, "sent": host.packets_sent}
        if not host.is_alive:
            return CheckResult.fail(f"no reply from {m.host}", detail=detail)
        msg = f"{host.avg_rtt:.1f} ms avg, {host.packet_loss:.0%} loss"
        res = CheckResult.ok(msg, value=host.avg_rtt, unit=" ms", latency_ms=host.avg_rtt,
                             detail=detail)
        if host.packet_loss > 0:
            res.result = Result.WARN
        return res


class TcpCheck(Check):
    async def probe(self) -> CheckResult:
        m = self.monitor
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(m.host, m.port), self.timeout
            )
        except (OSError, asyncio.TimeoutError) as err:
            return CheckResult.fail(f"connect {m.host}:{m.port} failed: {err or 'timeout'}")
        connect_ms = (loop.time() - start) * 1000
        try:
            if m.send:
                writer.write(m.send.encode().decode("unicode_escape").encode())
                await writer.drain()
            if m.expect:
                data = await asyncio.wait_for(reader.read(4096), self.timeout)
                if m.expect.encode() not in data:
                    return CheckResult.fail(
                        f"connected but banner did not contain {m.expect!r}",
                        latency_ms=connect_ms,
                    )
        except (OSError, asyncio.TimeoutError) as err:
            return CheckResult.fail(f"connected but exchange failed: {err or 'timeout'}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        return CheckResult.ok(
            f"port {m.port} open in {connect_ms:.1f} ms",
            value=connect_ms, unit=" ms", latency_ms=connect_ms,
        )


class HttpCheck(Check):
    async def probe(self) -> CheckResult:
        m = self.monitor
        verify: bool | ssl.SSLContext = m.verify_tls
        if m.verify_tls and m.ca_bundle:
            verify = ssl.create_default_context(cafile=m.ca_bundle)
        try:
            async with httpx.AsyncClient(
                verify=verify, timeout=self.timeout, follow_redirects=m.follow_redirects
            ) as client:
                resp = await client.request(m.method, m.url, headers=m.headers)
        except httpx.HTTPError as err:
            return CheckResult.fail(f"{type(err).__name__}: {err}")
        ms = resp.elapsed.total_seconds() * 1000
        if resp.status_code not in m.expect_status:
            return CheckResult.fail(
                f"HTTP {resp.status_code}, expected {m.expect_status}", latency_ms=ms
            )
        if m.expect_text and m.expect_text not in resp.text:
            return CheckResult.fail(f"HTTP {resp.status_code} but body lacks {m.expect_text!r}",
                                    latency_ms=ms)
        return CheckResult.ok(f"HTTP {resp.status_code} in {ms:.0f} ms", value=ms, unit=" ms",
                              latency_ms=ms)


class DnsCheck(Check):
    async def probe(self) -> CheckResult:
        m = self.monitor
        resolver = dns.asyncresolver.Resolver(configure=m.nameserver is None)
        if m.nameserver:
            resolver.nameservers = [m.nameserver]
        resolver.lifetime = self.timeout
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            answer = await resolver.resolve(m.query, m.record)
        except dns.exception.DNSException as err:
            return CheckResult.fail(f"{type(err).__name__}: {err}")
        ms = (loop.time() - start) * 1000
        got = sorted(r.to_text().rstrip(".") for r in answer)
        if m.expect:
            missing = [e for e in m.expect if e.rstrip(".") not in got]
            if missing:
                return CheckResult.fail(f"answer {got} is missing {missing}", latency_ms=ms)
        return CheckResult.ok(f"{m.record} {', '.join(got)}", value=ms, unit=" ms",
                              latency_ms=ms, detail={"answers": got})


class TlsCertCheck(Check):
    """Days until the leaf certificate expires.

    With verify: true (the default) a chain or hostname failure is FAIL,
    because a cert that validates nowhere is already an outage for clients.
    Default thresholds when none are configured: WARN at 21 days, FAIL at 7.
    """

    async def probe(self) -> CheckResult:
        m = self.monitor
        sni = m.server_name or m.host
        if m.verify:
            ctx = ssl.create_default_context(cafile=m.ca_bundle)
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(m.host, m.port, ssl=ctx, server_hostname=sni),
                self.timeout,
            )
        except ssl.SSLCertVerificationError as err:
            return CheckResult.fail(f"certificate did not validate: {err.verify_message}")
        except (OSError, asyncio.TimeoutError) as err:
            return CheckResult.fail(f"TLS connect failed: {err or 'timeout'}")
        try:
            der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass
        cert = x509.load_der_x509_certificate(der)
        remaining = cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
        days = remaining.total_seconds() / 86400
        subject = cert.subject.rfc4514_string()
        res = CheckResult.ok(
            f"{subject} expires {cert.not_valid_after_utc:%Y-%m-%d} ({days:.1f} days)",
            value=round(days, 2), unit=" days",
            detail={"issuer": cert.issuer.rfc4514_string(),
                    "not_after": cert.not_valid_after_utc.isoformat()},
        )
        if days <= 0:
            res.result = Result.FAIL
        return res

    DEFAULT_THRESHOLDS = Thresholds(direction="below", warn=21, crit=7)

    def thresholds(self) -> Thresholds | None:
        return self.monitor.thresholds or self.DEFAULT_THRESHOLDS
