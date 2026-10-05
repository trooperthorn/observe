"""Stand-in API servers that emit the documented response shapes.

Proxmox: /api2/json/cluster/resources, fields as in the API schema
(type, node, status, cpu, maxcpu, mem, maxmem, disk, maxdisk, vmid, name,
template, shared, storage, hastate).
TrueNAS: JSON-RPC 2.0 over WebSocket at /api/current with auth.login_ex
(API_KEY_PLAIN), pool.query, alert.list; an event notification (no id) is
sent before each reply, as the real server may interleave them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def leaf_cert(tmp_path, cn="localhost", ca=False):
    """A self-signed certificate that is NOT a CA, like appliance defaults."""
    import ipaddress
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName(cn), x509.IPAddress(ipaddress.ip_address("127.0.0.2"))]), False)
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), True)
            .sign(key, hashes.SHA256()))
    cp, kp = tmp_path / f"{cn}-leaf.pem", tmp_path / f"{cn}-leaf.key"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.TraditionalOpenSSL,
                                     serialization.NoEncryption()))
    return str(cp), str(kp)


# ------------------------------------------------------------------ Proxmox

PVE_TOKEN = "PVEAPIToken=observe@pve!monitor=11111111-2222-3333-4444-555555555555"
PVE_RESOURCES = [
    {"id": "node/pve1", "type": "node", "node": "pve1", "status": "online", "cpu": 0.123,
     "maxcpu": 8, "mem": 12 * 2**30, "maxmem": 32 * 2**30, "uptime": 864000, "level": ""},
    {"id": "qemu/100", "type": "qemu", "vmid": 100, "name": "dc01", "node": "pve1",
     "status": "running", "cpu": 0.05, "maxcpu": 2, "mem": 3 * 2**30, "maxmem": 4 * 2**30,
     "template": 0},
    {"id": "qemu/101", "type": "qemu", "vmid": 101, "name": "old-vm", "node": "pve1",
     "status": "stopped", "template": 0},
    {"id": "qemu/9000", "type": "qemu", "vmid": 9000, "name": "tmpl", "node": "pve1",
     "status": "stopped", "template": 1},
    {"id": "lxc/200", "type": "lxc", "vmid": 200, "name": "pihole", "node": "pve1",
     "status": "running", "cpu": 0.01, "mem": 200 * 2**20, "maxmem": 512 * 2**20,
     "template": 0, "hastate": "started"},
    {"id": "storage/pve1/local-lvm", "type": "storage", "storage": "local-lvm", "node": "pve1",
     "status": "available", "disk": 300 * 2**30, "maxdisk": 400 * 2**30, "shared": 0},
    {"id": "storage/pve1/nas", "type": "storage", "storage": "nas", "node": "pve1",
     "status": "available", "disk": 1 * 2**40, "maxdisk": 8 * 2**40, "shared": 1},
]


def proxmox_server(cert, key, resources=None, token=PVE_TOKEN):
    data = PVE_RESOURCES if resources is None else resources

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != token:
                self.send_response(401)
                self.end_headers()
                return
            if self.path != "/api2/json/cluster/resources":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------------ TrueNAS

TN_POOLS = [
    {"id": 1, "name": "tank", "status": "ONLINE", "healthy": True, "warning": False,
     "status_detail": None, "size": 10 * 2**40, "allocated": 6 * 2**40, "free": 4 * 2**40},
    {"id": 2, "name": "fast", "status": "DEGRADED", "healthy": False, "warning": True,
     "status_detail": "One or more devices has been removed", "size": 2 * 2**40,
     "allocated": 1 * 2**40, "free": 1 * 2**40},
]
TN_ALERTS = [
    {"uuid": "a", "level": "WARNING", "dismissed": False, "klass": "SMART",
     "formatted": "Device /dev/sdb: 8 Currently unreadable (pending) sectors"},
    {"uuid": "b", "level": "INFO", "dismissed": False, "klass": "Update",
     "formatted": "Update available"},
    {"uuid": "c", "level": "CRITICAL", "dismissed": True, "klass": "PoolStatus",
     "formatted": "old, dismissed"},
]


class TrueNASFake:
    def __init__(self, cert, key, username="observe", api_key="1-abcdef",
                 pools=None, alerts=None):
        self.cert, self.key = cert, key
        self.username, self.api_key = username, api_key
        self.pools = TN_POOLS if pools is None else pools
        self.alerts = TN_ALERTS if alerts is None else alerts
        self.logins: list[dict] = []
        self.port = None
        self._loop = None

    async def _handler(self, ws):
        authed = False
        async for raw in ws:
            msg = json.loads(raw)
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "collection_update",
                                      "params": {"msg": "changed"}}))
            method, mid = msg["method"], msg["id"]
            if method == "auth.login_ex":
                p = msg["params"][0]
                self.logins.append(p)
                ok = (p.get("mechanism") == "API_KEY_PLAIN" and p.get("username") == self.username
                      and p.get("api_key") == self.api_key)
                authed = ok
                res = {"response_type": "SUCCESS", "user_info": None} if ok \
                    else {"response_type": "AUTH_ERR"}
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}))
            elif not authed:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32001, "message": "Method call error",
                    "data": {"reason": "Not authenticated"}}}))
            else:
                result = {"pool.query": self.pools, "alert.list": self.alerts}.get(method)
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}))

    def start(self):
        from websockets.asyncio.server import serve
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(self.cert, self.key)

            async def main():
                async with serve(self._handler, "127.0.0.1", 0, ssl=ctx) as server:
                    self.port = server.sockets[0].getsockname()[1]
                    ready.set()
                    await asyncio.Future()

            self._loop.run_until_complete(main())

        threading.Thread(target=run, daemon=True).start()
        ready.wait(5)
        return self


# ----------------------------------------------------- generic JSON server


def json_server(routes, check_header=None, cert=None):
    """routes: {path: body or callable(query)->(status, body)}. check_header:
    (name, value) required on every request, else 401. With cert=(pem, key)
    it serves HTTPS. The full request path, including query, is recorded."""
    from urllib.parse import parse_qs, urlsplit

    seen: list[tuple[str, dict]] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            seen.append((self.path, dict(self.headers)))
            if check_header and self.headers.get(check_header[0]) != check_header[1]:
                self._send(401, {"error": "Unauthorized"})
                return
            route = routes.get(parts.path)
            if route is None:
                self._send(404, {"error": "not found"})
                return
            if callable(route):
                status, body = route(parse_qs(parts.query), self.headers)
            else:
                status, body = 200, route
            self._send(status, body)

        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    if cert:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(*cert)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.seen = seen
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


HA_TOKEN = "eyJhbGciOi.test.token"
HA_STATES = [
    {"entity_id": "sensor.nas_temp", "state": "41.5",
     "attributes": {"unit_of_measurement": "°C", "friendly_name": "NAS temp"},
     "last_changed": "2026-09-23T10:00:00+00:00"},
    {"entity_id": "binary_sensor.front_door", "state": "off", "attributes": {}},
    {"entity_id": "media_player.zone4", "state": "unavailable", "attributes": {}},
    {"entity_id": "sensor.phyn_flow", "state": "unavailable", "attributes": {}},
    {"entity_id": "light.hall", "state": "unknown", "attributes": {}},
    {"entity_id": "update.home_assistant_core_update", "state": "on", "attributes": {}},
    {"entity_id": "update.esphome", "state": "off", "attributes": {}},
]


def ha_routes():
    routes = {"/api/": {"message": "API running."}, "/api/states": HA_STATES}
    for s in HA_STATES:
        routes[f"/api/states/{s['entity_id']}"] = s
    return routes


UNIFI_KEY = "unifi-test-key"
UNIFI_SITE = {"id": "88f7af54-0000-0000-0000-000000000001", "name": "Default"}
UNIFI_DEVICES = [
    {"id": f"dev-{i}", "name": n, "model": m, "state": st, "macAddress": mac,
     "ipAddress": f"192.0.2.{i}", "firmwareVersion": "7.1.0", "firmwareUpdatable": fu}
    for i, (n, m, st, mac, fu) in enumerate([
        ("Gateway", "UCG Fiber", "ONLINE", "aa:bb:cc:00:00:01", False),
        ("Core Switch", "USW Pro Max 16 PoE", "ONLINE", "aa:bb:cc:00:00:02", True),
        ("Hall AP", "U7 Pro", "ONLINE", "aa:bb:cc:00:00:03", False),
        ("Remote Flex", "USW Flex", "OFFLINE", "aa:bb:cc:00:00:04", False),
    ], start=1)]
UNIFI_STATS = {"uptimeSec": 172800, "cpuUtilizationPct": 23.4, "memoryUtilizationPct": 61.0,
               "loadAverage1Min": 0.5}
PROTECT_CAMERAS = [
    {"id": "c1", "name": "Driveway", "modelKey": "camera", "state": "CONNECTED",
     "mac": "AABBCC000011"},
    {"id": "c2", "name": "Doorbell", "modelKey": "camera", "state": "DISCONNECTED",
     "mac": "AABBCC000012"},
]


def unifi_routes(page_size=2):
    """Devices are paged page_size at a time to exercise the pager."""
    base = "/proxy/network/integration/v1"

    def devices(q, _h):
        off = int(q.get("offset", ["0"])[0])
        page = UNIFI_DEVICES[off:off + page_size]
        return 200, {"offset": off, "limit": page_size, "count": len(page),
                     "totalCount": len(UNIFI_DEVICES), "data": page}

    routes = {
        f"{base}/sites": {"offset": 0, "limit": 25, "count": 1, "totalCount": 1,
                          "data": [UNIFI_SITE]},
        f"{base}/sites/{UNIFI_SITE['id']}/devices": devices,
        "/proxy/protect/integration/v1/cameras": PROTECT_CAMERAS,
        "/proxy/protect/integration/v1/meta/info": {"applicationVersion": "6.2.88"},
    }
    for d in UNIFI_DEVICES:
        routes[f"{base}/sites/{UNIFI_SITE['id']}/devices/{d['id']}/statistics/latest"] = \
            UNIFI_STATS
    return routes


TECH_TOKEN = "technitium-test-token"
TECH_STATS = {"totalQueries": 1000, "totalNoError": 900, "totalServerFailure": 30,
              "totalNxDomain": 60, "totalRefused": 0, "totalBlocked": 42, "totalClients": 9,
              "cachedEntries": 1234}


def technitium_routes():
    """Technitium answers HTTP 200 with status "invalid-token" for a bad
    token; the header and the legacy ?token= query are both accepted."""

    def authed(q, h):
        return h.get("Authorization") == f"Bearer {TECH_TOKEN}" or \
            q.get("token", [None])[0] == TECH_TOKEN

    def stats(q, h):
        if not authed(q, h):
            return 200, {"status": "invalid-token", "errorMessage": "Invalid token or session expired."}
        return 200, {"response": {"stats": TECH_STATS}, "status": "ok"}

    return {
        "/api/dashboard/stats/get": stats,
        "/api/user/checkForUpdate": {"response": {"updateAvailable": True,
                                                  "currentVersion": "13.6",
                                                  "updateVersion": "14.0"}, "status": "ok"},
    }
