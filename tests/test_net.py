import asyncio
import datetime as dt
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def _self_signed(tmp_path, days):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    cp, kp = tmp_path / f"c{days}.pem", tmp_path / f"k{days}.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    return cp, kp


async def _tls_server(cp, kp):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cp, kp)

    async def handler(r, w):
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


# ---------------------------------------------------------- dns, ping


async def test_dns_unreachable_nameserver_fails():
    res = await check({"name": "d", "type": "dns", "query": "example.invalid",
                       "nameserver": "127.0.0.1", "timeout": 1}).run()
    assert res.result is Result.FAIL


async def test_ping_loopback():
    res = await check({"name": "p", "type": "ping", "host": "127.0.0.1", "count": 2}).run()
    # Either it works, or it fails with the actionable sysctl message.
    assert res.result is Result.OK or "ping_group_range" in res.message
