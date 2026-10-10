import asyncio
import datetime as dt
import ipaddress
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from observe.checks import build_check
from observe.checks.base import Result

from .conftest import make_config


def check(mon):
    cfg = make_config([mon])
    return build_check(cfg.monitors[0], cfg)


# ---------------------------------------------------------------- tcp


async def test_tcp_open_with_banner():
    async def handler(r, w):
        w.write(b"SSH-2.0-test\r\n")
        await w.drain()
        w.close()

    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    async with srv:
        res = await check({"name": "t", "type": "tcp", "host": "127.0.0.1", "port": port,
                           "expect": "SSH-2.0"}).run()
        assert res.result is Result.OK
        res = await check({"name": "t", "type": "tcp", "host": "127.0.0.1", "port": port,
                           "expect": "HTTP/1.1"}).run()
        assert res.result is Result.FAIL and "banner" in res.message


async def test_tcp_refused():
    s = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = s.sockets[0].getsockname()[1]
    s.close()
    await s.wait_closed()
    res = await check({"name": "t", "type": "tcp", "host": "127.0.0.1", "port": port}).run()
    assert res.result is Result.FAIL


# --------------------------------------------------------------- http


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 503 if self.path == "/down" else 200
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b"status: healthy")

    def log_message(self, *a):
        pass


@pytest.fixture
def http_port():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


async def test_http_status_and_text(http_port):
    base = f"http://127.0.0.1:{http_port}"
    ok = await check({"name": "h", "type": "http", "url": base + "/",
                      "expect_text": "healthy"}).run()
    assert ok.result is Result.OK and ok.value is not None
    down = await check({"name": "h", "type": "http", "url": base + "/down"}).run()
    assert down.result is Result.FAIL and "503" in down.message
    text = await check({"name": "h", "type": "http", "url": base + "/",
                        "expect_text": "nope"}).run()
    assert text.result is Result.FAIL


# ---------------------------------------------------------- tls cert


def _self_signed(tmp_path, days, ip=None):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.DNSName("localhost")]
    if ip:
        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(sans), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    cp, kp = tmp_path / f"c{days}.pem", tmp_path / f"k{days}.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    return cp, kp


async def _tls_server(cp, kp, seen=None):
    """A TLS listener that closes after one byte. Each connection's (resumed, ALPN) goes to seen;
    the server speaks http/1.1, so ALPN is set only if the client offered it."""
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cp, kp)
    ctx.set_alpn_protocols(["http/1.1"])

    async def handler(r, w):
        if seen is not None:
            tls = w.get_extra_info("ssl_object")
            seen.append((tls.session_reused, tls.selected_alpn_protocol()))
        try:
            await r.read(1)
        except (ConnectionError, ssl.SSLError):
            pass
        w.close()

    return await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ctx)


@pytest.mark.parametrize("days,expected", [(90, Result.OK), (10, Result.WARN), (3, Result.FAIL)])
async def test_tls_cert_default_thresholds(tmp_path, days, expected):
    cp, kp = _self_signed(tmp_path, days)
    srv = await _tls_server(cp, kp)
    port = srv.sockets[0].getsockname()[1]
    async with srv:
        res = await check({"name": "c", "type": "tls_cert", "host": "127.0.0.1", "port": port,
                           "server_name": "localhost", "ca_bundle": str(cp)}).run()
    assert res.result is expected, res.message
    assert days - 1 < res.value <= days


async def test_tls_cert_untrusted_chain_fails(tmp_path):
    cp, kp = _self_signed(tmp_path, 90)
    srv = await _tls_server(cp, kp)
    port = srv.sockets[0].getsockname()[1]
    async with srv:
        res = await check({"name": "c", "type": "tls_cert", "host": "127.0.0.1",
                           "port": port, "server_name": "localhost"}).run()
        assert res.result is Result.FAIL and "did not validate" in res.message
        res = await check({"name": "c", "type": "tls_cert", "host": "127.0.0.1",
                           "port": port, "verify": False}).run()
        assert res.result is Result.OK


# ------------------------------------------------- shared TLS contexts
# A poll reuses the client context of the poll before it (observe/tlscontext.py), still opens a
# new connection with a full handshake, and reaches the same result a new context would.


class _TlsH(_H):
    """_H plus a redirect from /old, recording each connection's (resumed, ALPN)."""

    def setup(self):
        super().setup()
        self.server.seen.append((self.connection.session_reused,
                                 self.connection.selected_alpn_protocol()))

    def do_GET(self):
        if self.path == "/old":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        super().do_GET()


@pytest.fixture
def https(tmp_path):
    """An HTTPS server at .url; .ca is the bundle that trusts it, .key its private key."""
    cp, kp = _self_signed(tmp_path, 90, ip="127.0.0.1")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _TlsH)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cp, kp)
    ctx.set_alpn_protocols(["http/1.1"])
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.url = f"https://127.0.0.1:{srv.server_port}"
    srv.ca, srv.key, srv.seen = str(cp), str(kp), []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def contexts(monkeypatch):
    """The SSLContext each probe handed to httpx.AsyncClient or asyncio.open_connection."""
    used = []
    client, open_connection = httpx.AsyncClient, asyncio.open_connection

    def recording_client(*a, **kw):
        used.append(kw["verify"])
        return client(*a, **kw)

    async def recording_open(*a, **kw):
        used.append(kw["ssl"])
        return await open_connection(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", recording_client)
    monkeypatch.setattr(asyncio, "open_connection", recording_open)
    return used


async def _settle(done):
    """Wait for a server callback that can run just after the client has finished."""
    for _ in range(200):
        if done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the server did not see the connection")


async def test_http_polls_share_one_context(http_port, tls_builds, contexts):
    c = check({"name": "h", "type": "http", "url": f"http://127.0.0.1:{http_port}/"})
    for _ in range(2):
        assert (await c.run()).result is Result.OK
    # verify_tls is on by default, so even an http:// poll used to load certifi's roots.
    assert len(tls_builds) == 1 and contexts[0] is contexts[1] is tls_builds[0]


async def test_https_polls_share_one_context_but_no_connection(https, tls_builds, contexts):
    c = check({"name": "h", "type": "http", "url": https.url + "/", "ca_bundle": https.ca})
    for _ in range(2):
        res = await c.run()
        assert res.result is Result.OK, res.message
    assert len(tls_builds) == 1 and contexts[0] is contexts[1] is tls_builds[0]
    # Two connections, each with a full handshake: the second did not resume the first.
    assert https.seen == [(False, "http/1.1")] * 2


async def test_https_verification_as_before(https, tmp_path, tls_builds):
    untrusted = check({"name": "h", "type": "http", "url": https.url + "/"})
    unverified = check({"name": "h", "type": "http", "url": https.url + "/", "verify_tls": False,
                        "ca_bundle": str(tmp_path / "not-read.pem")})
    pinned = check({"name": "h", "type": "http", "url": https.url + "/", "ca_bundle": https.ca})
    for _ in range(2):
        res = await untrusted.run()
        assert res.result is Result.FAIL and res.unreachable
        assert "CERTIFICATE_VERIFY_FAILED" in res.message
        assert (await unverified.run()).result is Result.OK
        assert (await pinned.run()).result is Result.OK
    assert len(tls_builds) == 2  # certifi's roots and the pinned bundle, once each


async def test_https_expect_status_and_text_as_before(https):
    def poll(path, **kw):
        return check({"name": "h", "type": "http", "url": https.url + path,
                      "ca_bundle": https.ca, **kw})

    cases = [(poll("/down"), Result.FAIL, "HTTP 503, expected [200]"),
             (poll("/down", expect_status=[503]), Result.OK, "HTTP 503 in"),
             (poll("/", expect_text="healthy"), Result.OK, "HTTP 200 in"),
             (poll("/", expect_text="nope"), Result.FAIL, "HTTP 200 but body lacks 'nope'")]
    for _ in range(2):
        for c, result, message in cases:
            res = await c.run()
            assert res.result is result and message in res.message, res.message


async def test_https_redirects_follow_the_monitor(https):
    stay = check({"name": "h", "type": "http", "url": https.url + "/old", "ca_bundle": https.ca})
    follow = check({"name": "h", "type": "http", "url": https.url + "/old",
                    "ca_bundle": https.ca, "follow_redirects": True})
    for _ in range(2):
        res = await stay.run()
        assert res.result is Result.FAIL and "HTTP 302" in res.message
        res = await follow.run()
        assert res.result is Result.OK and "HTTP 200" in res.message


async def test_replaced_ca_bundle_applies_from_the_next_poll(https, tmp_path):
    other, _ = _self_signed(tmp_path, 60, ip="127.0.0.1")
    bundle = tmp_path / "bundle.pem"
    bundle.write_bytes(other.read_bytes())
    c = check({"name": "h", "type": "http", "url": https.url + "/", "ca_bundle": str(bundle)})
    res = await c.run()
    assert res.result is Result.FAIL and "CERTIFICATE_VERIFY_FAILED" in res.message
    bundle.write_bytes(open(https.ca, "rb").read())
    st = bundle.stat()  # a later mtime, as a real replacement has; the test is faster than a tick
    os.utime(bundle, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert (await c.run()).result is Result.OK


async def test_missing_or_bad_ca_bundle_fails_every_poll(https, tmp_path):
    bundle = tmp_path / "later.pem"
    c = check({"name": "h", "type": "http", "url": https.url + "/", "ca_bundle": str(bundle)})
    for _ in range(2):
        with pytest.raises(FileNotFoundError):  # the scheduler records an internal error
            await c.run()
    bundle.write_text("not a certificate")
    for _ in range(2):
        with pytest.raises(ssl.SSLError):
            await c.run()
    bundle.write_bytes(open(https.ca, "rb").read())
    assert (await c.run()).result is Result.OK


async def test_tls_cert_polls_share_one_context(tmp_path, tls_builds, contexts):
    cp, kp = _self_signed(tmp_path, 90)
    seen = []
    srv = await _tls_server(cp, kp, seen)
    port = srv.sockets[0].getsockname()[1]
    async with srv:
        pinned = check({"name": "c", "type": "tls_cert", "host": "127.0.0.1", "port": port,
                        "server_name": "localhost", "ca_bundle": str(cp)})
        unverified = check({"name": "c", "type": "tls_cert", "host": "127.0.0.1", "port": port,
                            "verify": False})
        for _ in range(2):
            assert (await pinned.run()).result is Result.OK
            assert (await unverified.run()).result is Result.OK
        await _settle(lambda: len(seen) == 4)
    assert len(tls_builds) == 2 and contexts == tls_builds * 2
    assert seen == [(False, None)] * 4  # a full handshake each poll, offering no ALPN


async def test_cert_probe_offers_no_alpn_after_an_http_poll(https):
    """httpcore writes its ALPN list into the context it is given. A tls_cert probe of a server
    with the same ca_bundle has a context of its own, so it still offers no ALPN."""
    http = check({"name": "h", "type": "http", "url": https.url + "/", "ca_bundle": https.ca})
    assert (await http.run()).result is Result.OK
    seen = []
    srv = await _tls_server(https.ca, https.key, seen)
    async with srv:
        cert = check({"name": "c", "type": "tls_cert", "host": "127.0.0.1",
                      "port": srv.sockets[0].getsockname()[1], "server_name": "localhost",
                      "ca_bundle": https.ca})
        assert (await cert.run()).result is Result.OK
        await _settle(lambda: seen)
    assert https.seen == [(False, "http/1.1")] and seen == [(False, None)]


# ---------------------------------------------------------- dns, ping


async def test_dns_unreachable_nameserver_fails():
    res = await check({"name": "d", "type": "dns", "query": "example.invalid",
                       "nameserver": "127.0.0.1", "timeout": 1}).run()
    assert res.result is Result.FAIL


async def test_ping_loopback():
    res = await check({"name": "p", "type": "ping", "host": "127.0.0.1", "count": 2}).run()
    # Either it works, or it fails with the actionable sysctl message.
    assert res.result is Result.OK or "ping_group_range" in res.message
