"""Tests for ~/.local/bin/preview.

Run:  cd ~/.local/bin && python3 -m unittest preview_test -v
Live system test (starts a real systemd --user unit on a free port, then removes it):
      PREVIEW_LIVE=1 python3 -m unittest preview_test -v
"""

import asyncio
import contextlib
import http.client
import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
_loader = importlib.machinery.SourceFileLoader("preview_tool", str(HERE / "preview"))
_spec = importlib.util.spec_from_loader("preview_tool", _loader)
pv = importlib.util.module_from_spec(_spec)
_loader.exec_module(pv)

TS_IP = "100.64.0.20"
TS_NAME = "example-vps.tail0000.ts.net"


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------

class ArgumentTests(unittest.TestCase):
    def parse_quiet(self, argv):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return pv.parse(argv)

    def test_port_only(self):
        cmd, a = pv.parse(["5173"])
        self.assertEqual((cmd, a.port, a.name), ("start", 5173, None))

    def test_port_with_name_and_start_word(self):
        cmd, a = pv.parse(["start", "3000", "--name", "landing page"])
        self.assertEqual((cmd, a.port, a.name), ("start", 3000, "landing page"))
        cmd, a = pv.parse(["3000", "--label", "x"])
        self.assertEqual(a.name, "x")

    def test_bad_ports_rejected(self):
        for bad in (["abc"], ["0"], ["70000"], ["-5"], ["22"], ["80"], ["443"]):
            with self.assertRaises(SystemExit) as cm:
                self.parse_quiet(bad)
            self.assertEqual(cm.exception.code, 2, bad)

    def test_stop_forms(self):
        self.assertEqual(pv.parse(["stop", "5173"])[1].port, 5173)
        self.assertTrue(pv.parse(["stop", "--all"])[1].all)
        for bad in (["stop"], ["stop", "5173", "--all"]):
            with self.assertRaises(SystemExit):
                self.parse_quiet(bad)

    def test_list_doctor_help(self):
        self.assertEqual(pv.parse(["list"])[0], "list")
        self.assertTrue(pv.parse(["list", "--json"])[1].json)
        self.assertEqual(pv.parse(["doctor"])[0], "doctor")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            pv.parse(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("WHEN TO USE", out.getvalue())
        self.assertIn("preview stop", out.getvalue())


# ---------------------------------------------------------------------------
# Bind-address guarantee
# ---------------------------------------------------------------------------

class BindAddressTests(unittest.TestCase):
    def test_allowed(self):
        for ok in ("127.0.0.1", "::1", "[::1]", TS_IP, "fd7a:115c:a1e0::5c2b:a146"):
            pv.check_listen_host(ok)

    def test_refused(self):
        for bad in ("0.0.0.0", "::", "[::]", "136.148.219.14", "192.168.1.5", "172.17.0.1", "localhost",
                    "example-vps", ""):
            with self.assertRaises(pv.UnsafeListenAddress, msg=bad):
                pv.check_listen_host(bad)

    def test_server_refuses_wildcard_at_socket_level(self):
        async def go():
            with self.assertRaises(pv.UnsafeListenAddress):
                await pv.start_proxy_server("0.0.0.0", 0, pv.Proxy(1))
            server = await pv.start_proxy_server("127.0.0.1", 0, pv.Proxy(1))
            addrs = {s.getsockname()[0] for s in server.sockets}
            server.close()
            await server.wait_closed()
            return addrs
        self.assertEqual(asyncio.run(go()), {"127.0.0.1"})

    def test_serve_command_refuses_public_listen(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for listen in ("0.0.0.0:5173", "136.148.219.14:5173", "[::]:5173"):
                args = pv.parse(["_serve", "--listen", listen, "--target", "5173"])[1]
                self.assertEqual(pv.cmd_serve(args), pv.EXIT_CONFIG, listen)

    def test_http_backend_binds_tailnet_ip_only(self):
        with tempfile.TemporaryDirectory() as d, env(PREVIEW_STATE_DIR=d):
            s = FakeSystem(operator=False)
            run_start(s, 5173)
            listen = pv.load_state(5173)["listen"]
            self.assertEqual(listen, f"{TS_IP}:5173")
            self.assertIn(f"--listen {TS_IP}:5173", " ".join(s.units["preview-5173"]["argv"]))

    def test_every_proxy_unit_knows_the_tailnet_address_it_accepts_as_host(self):
        with tempfile.TemporaryDirectory() as d, env(PREVIEW_STATE_DIR=d):
            for operator, serve in ((False, False), (True, True)):
                s = FakeSystem(operator=operator, serve_allowed=serve)
                run_start(s, 5173)
                argv = s.units["preview-5173"]["argv"]
                self.assertEqual(argv[argv.index("--tailnet-ip") + 1], TS_IP)
                self.assertEqual(pv.parse(argv[3:])[1].tailnet_ip, TS_IP)
                pv.cmd_stop(s, 5173, out=io.StringIO())


# ---------------------------------------------------------------------------
# Proxy behaviour against a real local upstream
# ---------------------------------------------------------------------------

class Upstream:
    """Tiny HTTP/1.1 server: records request heads; /ws upgrades and echoes; /stream streams; /redirect 302s."""

    def __init__(self):
        self.heads = []
        self.bodies = []
        self.stream_release = None

    async def handle(self, r, w):
        try:
            while True:
                head = await pv.read_head(r)
                if head is None:
                    break
                start, headers = pv.split_head(head)
                self.heads.append((start, dict((n.lower(), v) for n, v in headers)))
                kind, n = pv.body_length(headers)
                body = b""
                if kind == "length":
                    body = await r.readexactly(n)
                elif kind == "chunked":
                    while True:
                        size = int((await r.readuntil(b"\r\n")).split(b";")[0], 16)
                        if size == 0:
                            await r.readuntil(b"\r\n")
                            break
                        body += await r.readexactly(size)
                        await r.readexactly(2)
                self.bodies.append(body)
                path = start.split(b" ")[1]
                if path == b"/ws":
                    w.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
                    await w.drain()
                    while data := await r.read(1024):
                        w.write(b"echo:" + data)
                        await w.drain()
                    break
                if path == b"/stream":
                    w.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/event-stream\r\n\r\n")
                    w.write(b"6\r\nfirst\n\r\n")
                    await w.drain()
                    await self.stream_release.wait()
                    w.write(b"7\r\nsecond\n\r\n0\r\n\r\n")
                    await w.drain()
                    continue
                if path == b"/redirect":
                    port = w.get_extra_info("sockname")[1]
                    w.write(b"HTTP/1.1 302 Found\r\nLocation: http://localhost:%d/next?a=1\r\nContent-Length: 0\r\n\r\n" % port)
                    await w.drain()
                    continue
                payload = json.dumps({"host": self.heads[-1][1].get(b"host", b"").decode(),
                                      "body": body.decode()}).encode()
                w.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload), payload))
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.up = Upstream()
        self.up.stream_release = asyncio.Event()
        self.up_server = await asyncio.start_server(self.up.handle, "127.0.0.1", 0)
        self.port = self.up_server.sockets[0].getsockname()[1]
        self.public = f"http://{TS_NAME}:{self.port}"
        self.proxy_server = await pv.start_proxy_server("127.0.0.1", 0, pv.Proxy(self.port, self.public))
        self.pport = self.proxy_server.sockets[0].getsockname()[1]
        self.ext_host = f"{TS_NAME}:{self.port}".encode()

    async def asyncTearDown(self):
        for s in (self.proxy_server, self.up_server):
            s.close()
        for s in (self.proxy_server, self.up_server):
            await s.wait_closed()

    async def open(self):
        return await asyncio.open_connection("127.0.0.1", self.pport)

    async def read_response(self, r):
        head = await pv.read_head(r)
        start, headers = pv.split_head(head)
        kind, n = pv.body_length(headers)
        body = await r.readexactly(n) if kind == "length" else b""
        return start, dict((k.lower(), v) for k, v in headers), body

    async def test_host_origin_referer_rewritten_on_every_request_of_a_keepalive_connection(self):
        r, w = await self.open()
        h = self.ext_host
        w.write(b"GET /a HTTP/1.1\r\nHost: " + h + b"\r\nOrigin: http://" + h + b"\r\nReferer: http://" + h +
                b"/page?x=1\r\nX-Forwarded-Host: evil.example\r\nX-Forwarded-Proto: https\r\n\r\n")
        w.write(b"POST /b HTTP/1.1\r\nHost: " + h + b"\r\nOrigin: https://" + h + b"\r\nContent-Length: 5\r\n\r\nhello")
        w.write(b"POST /c HTTP/1.1\r\nHost: " + h + b"\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n")
        await w.drain()
        for expected_body in (b"", b"hello", b"abcde"):
            start, _, body = await self.read_response(r)
            self.assertIn(b"200", start)
            self.assertEqual(json.loads(body)["body"], expected_body.decode())
        w.close()
        local = f"localhost:{self.port}".encode()
        self.assertEqual(len(self.up.heads), 3)
        for _, hdrs in self.up.heads:
            self.assertEqual(hdrs[b"host"], local)
            self.assertNotIn(b"x-forwarded-host", hdrs)
            self.assertNotIn(b"x-forwarded-proto", hdrs)
            self.assertIn(b"x-forwarded-for", hdrs)
        self.assertEqual(self.up.heads[0][1][b"origin"], b"http://" + local)
        self.assertEqual(self.up.heads[0][1][b"referer"], b"http://" + local + b"/page?x=1")
        self.assertEqual(self.up.heads[1][1][b"origin"], b"http://" + local)

    async def status_for(self, host, proxy=None):
        server = await pv.start_proxy_server("127.0.0.1", 0, proxy) if proxy else None
        port = server.sockets[0].getsockname()[1] if server else self.pport
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"GET /x HTTP/1.1\r\n" + (b"Host: " + host + b"\r\n" if host is not None else b"") + b"\r\n")
            await w.drain()
            start, _, _ = await asyncio.wait_for(self.read_response(r), 5)
            w.close()
            return start.split(b" ")[1]
        finally:
            if server:
                server.close(); await server.wait_closed()

    async def test_a_foreign_host_header_gets_421_and_never_reaches_the_server(self):
        # DNS rebinding: a web page rebinds its own name to the tailnet IP; the browser then sends that name as Host.
        for host in (f"evil.example:{self.port}".encode(), b"evil.example", f"{TS_NAME}.evil.example:{self.port}".encode(), None):
            with self.subTest(host=host):
                self.assertEqual(await self.status_for(host), b"421")
        self.assertEqual(self.up.heads, [])

    async def test_tailnet_names_addresses_and_localhost_with_the_share_port_are_accepted(self):
        proxy = pv.Proxy(self.port, self.public, tailnet_ip=TS_IP)
        for host in (f"{TS_NAME}:{self.port}", f"{TS_NAME.upper()}.:{self.port}", f"example-vps:{self.port}",
                     f"{TS_IP}:{self.port}", f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(await self.status_for(host.encode(), proxy), b"200")
        self.assertEqual(await self.status_for(f"{TS_IP}:{self.port + 1}".encode(), proxy), b"421")

    async def test_a_foreign_host_later_on_a_keepalive_connection_ends_it(self):
        r, w = await self.open()
        w.write(b"GET /a HTTP/1.1\r\nHost: " + self.ext_host + b"\r\n\r\nGET /b HTTP/1.1\r\nHost: evil.example\r\n\r\n")
        await w.drain()
        start, _, _ = await self.read_response(r)
        self.assertIn(b"200", start)
        self.assertIsNone(await asyncio.wait_for(pv.read_head(r), 5))
        w.close()
        self.assertEqual([start.split(b" ")[1] for start, _ in self.up.heads], [b"/a"])

    async def test_foreign_origin_left_alone(self):
        r, w = await self.open()
        w.write(b"GET / HTTP/1.1\r\nHost: " + self.ext_host + b"\r\nOrigin: https://other.example\r\n\r\n")
        await w.drain()
        await self.read_response(r)
        w.close()
        self.assertEqual(self.up.heads[0][1][b"origin"], b"https://other.example")

    async def test_redirect_to_localhost_becomes_preview_link(self):
        r, w = await self.open()
        w.write(b"GET /redirect HTTP/1.1\r\nHost: " + self.ext_host + b"\r\n\r\n")
        await w.drain()
        start, hdrs, _ = await self.read_response(r)
        w.close()
        self.assertIn(b"302", start)
        self.assertEqual(hdrs[b"location"], f"{self.public}/next?a=1".encode())

    async def test_websocket_upgrade_passes_through(self):
        r, w = await self.open()
        w.write(b"GET /ws HTTP/1.1\r\nHost: " + self.ext_host + b"\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
                b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
        await w.drain()
        head = await asyncio.wait_for(pv.read_head(r), 5)
        self.assertTrue(head.startswith(b"HTTP/1.1 101"))
        for msg in (b"\x81\x05hello", b"frame-two"):
            w.write(msg)
            await w.drain()
            self.assertEqual(await asyncio.wait_for(r.readexactly(5 + len(msg)), 5), b"echo:" + msg)
        w.close()
        self.assertEqual(self.up.heads[0][1][b"host"], f"localhost:{self.port}".encode())

    async def test_streaming_response_is_not_buffered(self):
        r, w = await self.open()
        w.write(b"GET /stream HTTP/1.1\r\nHost: " + self.ext_host + b"\r\n\r\n")
        await w.drain()
        head = await asyncio.wait_for(pv.read_head(r), 5)
        self.assertIn(b"200", head)
        first = await asyncio.wait_for(r.readuntil(b"first\n\r\n"), 5)  # arrives while upstream still holds the rest
        self.assertTrue(first.endswith(b"first\n\r\n"))
        self.up.stream_release.set()
        rest = await asyncio.wait_for(r.readuntil(b"0\r\n\r\n"), 5)
        self.assertIn(b"second", rest)
        w.close()

    async def test_unavailable_upstream_gets_friendly_page(self):
        self.up_server.close()
        await self.up_server.wait_closed()
        r, w = await self.open()
        w.write(b"GET / HTTP/1.1\r\nHost: " + self.ext_host + b"\r\n\r\n")
        await w.drain()
        start, hdrs, body = await self.read_response(r)
        w.close()
        self.assertIn(b"502", start)
        self.assertIn(b"refresh", body)
        self.assertIn(b"Nothing is answering", body)


# ---------------------------------------------------------------------------
# Lifecycle with a fake system: idempotency, backend choice, stop scope
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update(kv)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def norm(unit):
    return unit[:-len(".service")] if unit.endswith(".service") else unit


class FakeSystem(pv.System):
    """systemd, Tailscale and the network in memory. `units` are the running services (a unit started on demand has
    "socket": its listen address), `files` the unit files this tool would have written ({name: "plain" | "on-demand" |
    "socket"}). An on-demand service in `idle` is inactive while its socket listens; `failed` and `sockets_down` break it."""

    def __init__(self, operator=False, serve_allowed=False, target_up=True, wildcard=False):
        super().__init__()
        self.operator = operator
        self.serve_allowed = serve_allowed
        self.target_up = target_up
        self.wildcard = wildcard
        self.units = {}
        self.files = {}
        self.idle, self.failed, self.sockets_down = set(), set(), set()
        self.busy = set()  # ports a connection holds (as its source port, or in TIME_WAIT): not listening, yet not bindable
        self.fail_on_demand = False
        self.connects = []
        self.serve = {8443: "http://127.0.0.1:3773"}  # someone else's entry
        self.calls = []
        self.next_free = 40000

    def run(self, argv, timeout=30):
        raise AssertionError(f"a test ran a real command: {argv}")  # no test touches systemctl, tailscale or ss

    def tailnet(self):
        return TS_IP, TS_NAME

    def operator_ok(self):
        return self.operator

    def serve_ports(self):
        return set(self.serve)

    def serve_targets(self):
        return dict(self.serve)

    def serve_add(self, port, internal):
        self.calls.append(("serve_add", port))
        if not self.serve_allowed:
            return False, "sending serve config: Access denied: serve config denied"
        self.serve[port] = f"http://127.0.0.1:{internal}"
        return True, ""

    def serve_remove(self, port):
        self.calls.append(("serve_remove", port))
        self.serve.pop(port, None)
        return True, ""

    def unit_state(self, unit):
        if unit.endswith(".socket"):
            listening = any(u.get("socket") and pv.socket_of(name) == unit for name, u in self.units.items())
            return "active" if listening and unit not in self.sockets_down else "inactive"
        name = norm(unit)
        if name in self.failed:
            return "failed"
        if not any(norm(u) == name for u in self.units):
            return "inactive"
        return "inactive" if name in self.idle else "active"

    def unit_states(self, units):
        return {u: self.unit_state(u) for u in units}

    def has_unit_file(self, unit):
        return isinstance(unit, str) and norm(unit) in self.files

    def unit_comes_back(self, unit):
        name = norm(unit)
        return name in self.files and (self.files[name] in ("plain", "socket") or pv.socket_of(name) in self.files)

    def own_unit_files(self):
        return sorted(self.files)

    def unit_start(self, unit, desc, argv, listen=None):
        self.calls.append(("unit_start", unit))
        if "--listen" in argv:
            bound = argv[argv.index("--listen") + 1]
        else:  # a file server: 127.0.0.1:<port>
            bound = f"127.0.0.1:{argv[argv.index('--port') + 1]}"
        pv.check_listen_host(pv.parse_hostport(bound)[0])
        if listen is not None:
            if self.fail_on_demand:
                self.unit_stop(unit)
                return False, "socket refused"
            pv.check_listen_host(pv.parse_hostport(listen)[0])
        name = norm(unit)
        self.units[unit] = {"argv": argv, "listen": bound, "desc": desc, "socket": listen}
        self.idle.discard(name)
        self.failed.discard(name)
        self.files[name] = "on-demand" if listen is not None else "plain"
        if listen is not None:
            self.files[pv.socket_of(name)] = "socket"
        else:
            self.files.pop(pv.socket_of(name), None)
        return True, ""

    def unit_stop(self, unit):
        self.calls.append(("unit_stop", unit))
        name = norm(unit)
        for key in [k for k in self.units if norm(k) == name]:
            self.units.pop(key)
        self.files.pop(name, None)
        if not name.endswith(".socket"):
            self.files.pop(pv.socket_of(name), None)
        self.idle.discard(name)
        self.failed.discard(name)

    def unit_log(self, unit, lines=5):
        return ""

    def listen_addresses(self, port):
        return [addr for addr, _ in self.listeners(port)]

    def listeners(self, port):
        out = [(pv.parse_hostport(u["listen"])[0], "python3") for u in self.units.values()
               if pv.parse_hostport(u["listen"])[1] == port]
        if self.target_up:
            out.append(("0.0.0.0" if self.wildcard else "127.0.0.1", "node"))
        return out

    def can_connect(self, host, port, timeout=0.5):
        self.connects.append((host, port))
        if any(pv.parse_hostport(u["listen"]) == (host, port) for u in self.units.values()):
            return True
        return self.target_up and host in ("127.0.0.1", "::1")

    def free_local_port(self, avoid=()):
        self.next_free += 1
        return self.next_free

    def can_bind(self, port):
        return port not in self.busy

    def sleep(self, s):
        pass

    def firewall_tailnet_ports(self, path=None):
        return self.fw


FakeSystem.fw = None


def run_start(s, port, name=None):
    out, err = io.StringIO(), io.StringIO()
    code = pv.cmd_start(s, port, name, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = env(PREVIEW_STATE_DIR=self.tmp.name, PREVIEW_BACKEND="")
        self.env.__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_http_fallback_when_serve_denied(self):
        s = FakeSystem(operator=None, serve_allowed=False)
        code, out, err = run_start(s, 5173)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"http://{TS_NAME}:5173")
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("HTTP until Tailscale Serve is enabled", err)
        self.assertEqual(pv.load_state(5173)["backend"], "http")

    def test_http_warns_when_firewall_blocks_port(self):
        s = FakeSystem(operator=False)
        s.fw = {22, 8443}
        code, out, err = run_start(s, 5173)
        self.assertEqual(code, 0)
        self.assertIn("will not load", err)
        s2 = FakeSystem(operator=False)
        s2.fw = {22, 5173}
        self.assertNotIn("will not load", run_start(s2, 5173)[2])

    def test_firewall_parser(self):
        conf = (
            'table inet f {\n chain input {\n type filter hook input priority 10; policy drop;\n'
            ' iifname "tailscale0" ip saddr 100.64.0.0/10 tcp dport 8443 counter accept\n'
            ' iifname "tailscale0" ip saddr 100.64.0.0/10 tcp dport { 22, 3000 } counter accept\n'
            ' iifname "eth0" tcp dport 443 counter accept\n }\n}\n')
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(conf)
        try:
            self.assertEqual(pv.System().firewall_tailnet_ports(f.name), {8443, 22, 3000})
            Path(f.name).write_text(conf.replace("policy drop", "policy accept"))
            self.assertIsNone(pv.System().firewall_tailnet_ports(f.name))
        finally:
            os.unlink(f.name)
        self.assertIsNone(pv.System().firewall_tailnet_ports("/nonexistent/nftables.conf"))

    def test_operator_false_skips_serve_attempt(self):
        s = FakeSystem(operator=False)
        run_start(s, 5173)
        self.assertNotIn(("serve_add", 5173), s.calls)

    def test_https_when_serve_allowed(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        code, out, _ = run_start(s, 5173, "vite")
        self.assertEqual(out.strip(), f"https://{TS_NAME}:5173")
        st = pv.load_state(5173)
        self.assertEqual(st["backend"], "https")
        self.assertTrue(st["listen"].startswith("127.0.0.1:"))
        self.assertIn(5173, s.serve)

    def test_idempotent(self):
        s = FakeSystem(operator=False)
        first = run_start(s, 5173, "app")
        second = run_start(s, 5173)
        self.assertEqual(first[1], second[1])
        self.assertEqual([c for c in s.calls if c[0] == "unit_start"], [("unit_start", "preview-5173")])
        self.assertEqual(len(s.units), 1)
        self.assertEqual(pv.load_state(5173)["label"], "app")
        run_start(s, 5173, "renamed")
        self.assertEqual(pv.load_state(5173)["label"], "renamed")
        self.assertEqual(len([c for c in s.calls if c[0] == "unit_start"]), 1)

    def test_idempotent_https(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        run_start(s, 5173)
        run_start(s, 5173)
        self.assertEqual(len([c for c in s.calls if c[0] == "serve_add"]), 1)

    def test_dead_unit_is_restarted(self):
        s = FakeSystem(operator=False)
        run_start(s, 5173)
        s.units.clear()
        code, out, _ = run_start(s, 5173)
        self.assertEqual(code, 0)
        self.assertIn("preview-5173", s.units)

    def test_rerun_upgrades_http_to_https_once_allowed(self):
        s = FakeSystem(operator=False)
        run_start(s, 5173)
        s.operator, s.serve_allowed = True, True
        _, out, _ = run_start(s, 5173)
        self.assertEqual(out.strip(), f"https://{TS_NAME}:5173")
        self.assertTrue(s.units["preview-5173"]["listen"].startswith("127.0.0.1:"))

    def test_refuses_publicly_listening_target(self):
        s = FakeSystem(wildcard=True)
        code, out, err = run_start(s, 5173)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("public", err)
        self.assertEqual(s.units, {})

    def test_refuses_foreign_serve_port(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        code, _, err = run_start(s, 8443)
        self.assertEqual(code, 1)
        self.assertIn("something else", err)
        self.assertEqual(s.serve[8443], "http://127.0.0.1:3773")

    def test_works_before_target_is_up(self):
        s = FakeSystem(operator=False, target_up=False)
        code, out, err = run_start(s, 5174)
        self.assertEqual(code, 0)
        self.assertIn("Nothing listens on localhost:5174 yet", err)

    def test_stop_all_only_touches_own_entries(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        run_start(s, 5173)
        s.operator = False
        run_start(s, 8000)
        s.units["unrelated.service"] = {"listen": "127.0.0.1:1", "argv": []}
        with contextlib.redirect_stdout(io.StringIO()):
            pv.cmd_stop(s, all_=True, everyone=True)
        self.assertEqual(pv.all_states(), [])
        self.assertEqual(set(s.units), {"unrelated.service"})
        self.assertEqual(s.serve, {8443: "http://127.0.0.1:3773"})
        self.assertNotIn(("serve_remove", 8443), s.calls)

    def test_stop_unknown_port(self):
        s = FakeSystem()
        err = io.StringIO()
        self.assertEqual(pv.cmd_stop(s, 9999, err=err), 1)

    def test_list(self):
        s = FakeSystem(operator=False)
        run_start(s, 5173, "vite")
        out = io.StringIO()
        pv.cmd_list(s, as_json=True, out=out)
        rows = json.loads(out.getvalue())
        self.assertEqual(rows[0]["port"], 5173)
        self.assertEqual(rows[0]["label"], "vite")
        self.assertTrue(rows[0]["target_listening"])
        self.assertEqual(rows[0]["proxy"], "active")
        out = io.StringIO()
        pv.cmd_list(s, out=out)
        self.assertIn("PORT", out.getvalue())
        self.assertIn(f"http://{TS_NAME}:5173", out.getvalue())

    def test_list_asks_systemctl_once_for_every_unit_it_shows(self):
        class Counting(FakeSystem):
            """Unit states come from `systemctl --user is-active` as System asks for them; one line per unit, or one line
            less when `short` (systemctl wrote an error for a unit to stderr instead)."""
            unit_state = pv.System.unit_state
            unit_states = pv.System.__dict__.get("unit_states", FakeSystem.unit_states)
            short, asked = False, []

            def run(self, argv, timeout=30):
                argv = list(argv)
                if argv[:3] == ["systemctl", "--user", "list-units"]:  # the batch listing of many units: cannot say
                    return subprocess.CompletedProcess(argv, 1, "", "")
                if argv[:3] != ["systemctl", "--user", "is-active"]:
                    raise AssertionError(argv)
                self.asked.append(argv)
                states = [FakeSystem.unit_state(self, u) + "\n" for u in argv[3:]]
                return subprocess.CompletedProcess(argv, 0, "".join(states[:-1] if self.short and len(states) > 1 else states), "")

        s = Counting(operator=False)
        for port in (5173, 5174, 5175, 5176):
            run_start(s, port, "p%d" % port)
        share = pv.load_state(5175)
        pv.save_state({**share, "file_unit": f"{pv.FILE_UNIT_PREFIX}5175.service", "path": "/srv/alex/shared"})
        s.units.pop(pv.load_state(5174)["unit"])
        outputs = []
        for short in (True, False):
            s.short, s.asked = short, []
            out, table = io.StringIO(), io.StringIO()
            pv.cmd_list(s, as_json=True, out=out)
            pv.cmd_list(s, out=table)
            outputs.append((json.loads(out.getvalue()), table.getvalue(), len(s.asked)))
        (fallback, fallback_table, per_unit), (rows, table, calls) = outputs
        self.assertEqual((rows, table), (fallback, fallback_table))
        self.assertEqual([r["proxy"] for r in rows], ["active", "socket inactive", "active", "active"])
        self.assertEqual(rows[2]["file_server"], "inactive")
        self.assertIn("/srv/alex/shared", table)
        self.assertEqual(calls, 2)  # one for --json, one for the table
        self.assertGreater(per_unit, calls)

    def test_doctor_names_the_one_command(self):
        s = FakeSystem(operator=False)
        s.run = lambda argv, timeout=30: subprocess.CompletedProcess(argv, 0, "yes\n", "")
        out = io.StringIO()
        pv.cmd_doctor(s, out=out)
        self.assertIn(f"sudo tailscale set --operator={pv.current_user()}", out.getvalue())

    def test_unknown_tailnet_refuses_instead_of_guessing_a_machine(self):
        """Without `tailscale status` the tool has no name to link to; it never falls back to a fixed machine."""
        s = pv.System()
        s.run = lambda argv, timeout=30: subprocess.CompletedProcess(argv, 1, "", "not logged in")
        with self.assertRaises(pv.TailnetUnknown):
            s.tailnet()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(pv.main(["5173"], sysm=s), 1)
        self.assertIn("tailscale status", err.getvalue())
        self.assertIsNone(pv.load_state(5173))
        out = io.StringIO()
        pv.cmd_doctor(s, out=out)
        self.assertIn("tailnet name   unknown", out.getvalue())



# ---------------------------------------------------------------------------
# On demand: socket activation, idle exit, unit files with their socket units
# ---------------------------------------------------------------------------

TOOL = str(HERE / "preview")

# Starts the tool the way systemd does for a socket unit: the listening socket as fd 3, LISTEN_PID = the process itself.
ACTIVATE = (
    "import os, sys\n"
    "os.dup2(int(sys.argv[1]), 3)\n"
    "os.environ['LISTEN_PID'] = str(os.getpid())\n"
    "os.environ['LISTEN_FDS'] = '1'\n"
    "os.execv(sys.executable, [sys.executable, '-I'] + sys.argv[2:])\n")


@contextlib.contextmanager
def patched(obj, **attrs):
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


@contextlib.contextmanager
def kept_signals():
    old = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def listener():
    return socket.create_server(("127.0.0.1", 0))


# ---------------------------------------------------------------------------
# File shares: byte ranges (videos start playing at once and can seek) and keep-alive
# ---------------------------------------------------------------------------

BIG_BODY = 48 * 1024


class BlockingUpstream(socketserver.ThreadingTCPServer):
    """A dev server stand-in in threads: GET answers JSON with the Host it saw, /ws upgrades and echoes."""
    daemon_threads = True
    allow_reuse_address = True

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            while True:
                head = b""
                while not head.endswith(b"\r\n\r\n"):
                    line = self.rfile.readline()
                    if not line:
                        return
                    head += line
                lines = head.split(b"\r\n")
                path = lines[0].split(b" ")[1]
                host = next((l.split(b":", 1)[1].strip() for l in lines[1:] if l.lower().startswith(b"host:")), b"")
                if path == b"/ws":
                    self.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
                    self.wfile.flush()
                    while data := self.rfile.read1(1024):
                        self.wfile.write(b"echo:" + data)
                        self.wfile.flush()
                    return
                body = json.dumps({"host": host.decode()}).encode()
                if path.startswith(b"/big"):  # a stylesheet-sized answer
                    body = b"x" * BIG_BODY
                self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                                 % (len(body), body))
                self.wfile.flush()

    def __init__(self):
        super().__init__(("127.0.0.1", 0), self.Handler)
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def close(self):
        self.shutdown()
        self.server_close()


def http_get(port, path="/", host=None, timeout=10, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers={**({"Host": host} if host else {}), **(headers or {})})
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


class IdleCountTests(unittest.TestCase):
    def test_idle_only_counts_while_nothing_is_open(self):
        now = [100.0]
        idle = pv.IdleCount(clock=lambda: now[0])
        now[0] = 105.0
        self.assertEqual(idle.idle_for(), 5.0)
        idle.opened()
        idle.opened()
        now[0] = 200.0
        self.assertEqual(idle.idle_for(), 0.0)
        idle.closed()
        self.assertEqual(idle.idle_for(), 0.0)  # one is still open
        now[0] = 210.0
        idle.closed()
        now[0] = 213.0
        self.assertEqual(idle.idle_for(), 3.0)  # counted from the last close


class InheritedSocketTests(unittest.TestCase):
    def test_without_systemd_the_server_binds_as_before(self):
        for environ in ({}, {"LISTEN_PID": "1", "LISTEN_FDS": "1"}, {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "0"},
                        {"LISTEN_PID": "x", "LISTEN_FDS": "1"}, {"LISTEN_FDS": "1"}):
            self.assertIsNone(pv.inherited_socket(dict(environ)), environ)

    def test_takes_the_socket_when_it_is_for_this_process(self):
        with listener() as s:
            fd = os.dup(s.fileno())
            environ = {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1", "LISTEN_FDNAMES": "preview-1.socket", "X": "y"}
            got = pv.inherited_socket(environ, fd=fd)
            try:
                self.assertEqual(got.fileno(), fd)
                self.assertEqual(got.getsockname(), s.getsockname())
                self.assertEqual(environ, {"X": "y"})  # nothing for a child process to take
                self.assertFalse(os.get_inheritable(fd))
            finally:
                got.close()

    def test_refuses_a_socket_that_does_not_listen(self):
        with socket.socket() as s:
            environ = {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1"}
            with self.assertRaises(OSError):
                pv.inherited_socket(environ, fd=os.dup(s.fileno()))

    def test_bind_address_guarantee_holds_for_the_socket_from_systemd(self):
        with listener() as s:
            port = s.getsockname()[1]
            pv.check_inherited(s, "127.0.0.1", port)
            for host, p in (("127.0.0.1", port + 1), (TS_IP, port)):
                with self.assertRaises(pv.UnsafeListenAddress):
                    pv.check_inherited(s, host, p)


class OnDemandProxyTests(unittest.TestCase):
    """The real `preview _serve` in a subprocess, started like systemd starts it from a socket unit."""

    def setUp(self):
        self.up = BlockingUpstream()
        self.sock = listener()
        self.lport = self.sock.getsockname()[1]
        self.tmp = tempfile.TemporaryDirectory()
        self.notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.notify.bind(os.path.join(self.tmp.name, "notify"))
        self.notify.settimeout(15)
        self.host = f"{TS_NAME}:{self.up.port}"
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(10)
            for f in (p.stdout, p.stderr):
                f.close()
        self.sock.close()
        self.notify.close()
        self.up.close()
        self.tmp.cleanup()

    def serve(self, *extra, activated=True):
        argv = [TOOL, "_serve", "--listen", f"127.0.0.1:{self.lport}", "--target", str(self.up.port),
                "--public-url", f"https://{self.host}", "--tailnet-ip", TS_IP, *extra]
        environ = {**os.environ, "NOTIFY_SOCKET": os.path.join(self.tmp.name, "notify"), "PREVIEW_STATE_DIR": self.tmp.name}
        if activated:
            cmd, fds = [sys.executable, "-c", ACTIVATE, str(self.sock.fileno()), *argv], (self.sock.fileno(),)
        else:
            cmd, fds = [sys.executable, "-I", *argv], ()
        p = subprocess.Popen(cmd, pass_fds=fds, env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(p)
        return p

    def test_serves_the_socket_from_systemd_says_ready_and_stops_when_idle(self):
        p = self.serve("--idle", "1")
        self.assertEqual(self.notify.recv(100), b"READY=1")
        status, body = http_get(self.lport, host=self.host)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["host"], f"localhost:{self.up.port}")
        self.assertEqual(p.wait(15), 0)  # idle: a clean exit, so Restart=on-failure leaves it stopped
        self.assertIn(b"no connection for 1", p.stdout.read())
        # The socket (systemd's, here the test's) still listens: a request now waits in its queue for the next start.
        late = socket.create_connection(("127.0.0.1", self.lport), timeout=10)
        try:
            late.sendall(f"GET /late HTTP/1.1\r\nHost: {self.host}\r\n\r\n".encode())
            again = self.serve("--idle", "1")
            self.assertEqual(self.notify.recv(100), b"READY=1")
            answer = http.client.HTTPResponse(late)
            answer.begin()
            self.assertEqual((answer.status, json.loads(answer.read())["host"]), (200, f"localhost:{self.up.port}"))
        finally:
            late.close()
        self.assertEqual(again.wait(15), 0)

    def test_an_open_websocket_keeps_it_running(self):
        p = self.serve("--idle", "1")
        self.assertEqual(self.notify.recv(100), b"READY=1")
        ws = socket.create_connection(("127.0.0.1", self.lport), timeout=10)
        ws.sendall(f"GET /ws HTTP/1.1\r\nHost: {self.host}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n".encode())
        self.assertTrue(ws.recv(4096).startswith(b"HTTP/1.1 101"))
        time.sleep(3)  # three idle periods: hot reload stays connected
        self.assertIsNone(p.poll())
        ws.sendall(b"frame")
        self.assertEqual(ws.recv(100), b"echo:frame")
        started = time.monotonic()
        ws.close()
        self.assertEqual(p.wait(15), 0)
        self.assertGreaterEqual(time.monotonic() - started, 0.9)

    def test_parallel_requests_all_arrive_in_full(self):
        """A page loads its files in parallel (33 stylesheets at once). Every answer arrives in full,
        none is dropped (some used to come back as 502 behind Tailscale Serve, and the journal said
        "Task was destroyed but it is pending!" for client_to_upstream and upstream_to_client)."""
        from concurrent.futures import ThreadPoolExecutor
        self.serve("--idle", "60")
        self.assertEqual(self.notify.recv(100), b"READY=1")

        def get(i):
            try:
                status, body = http_get(self.lport, path=f"/big?{i}", host=self.host, timeout=20)
                return status, len(body)
            except (OSError, http.client.HTTPException) as e:
                return type(e).__name__, 0

        with ThreadPoolExecutor(40) as pool:
            for round_ in range(6):
                got = list(pool.map(get, range(40)))
                bad = [g for g in got if g != (200, BIG_BODY)]
                self.assertEqual(bad, [], f"round {round_ + 1}: {len(bad)} of 40 requests failed")

    def test_without_a_socket_from_systemd_idle_does_not_stop_it(self):
        self.sock.close()  # the proxy binds this port itself, as an older unit has it do
        p = self.serve("--idle", "0.5", activated=False)
        self.assertEqual(self.notify.recv(100), b"READY=1")
        self.assertEqual(http_get(self.lport, host=self.host)[0], 200)
        time.sleep(2)
        self.assertIsNone(p.poll())
        p.terminate()
        self.assertEqual(p.wait(10), 0)

    def test_refuses_a_socket_on_another_address(self):
        other = listener()
        try:
            self.sock.close()
            self.sock = other  # handed over as fd 3, but --listen names a different port
            p = self.serve("--idle", "1")
            self.assertEqual(p.wait(15), pv.EXIT_CONFIG)
        finally:
            other.close()


class OnDemandFileServerTests(unittest.TestCase):
    """cmd_serve_file in this process with a socket handed over like systemd does (main thread: it sets signal handlers)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "share"
        self.root.mkdir()
        (self.root / "index.html").write_text("hallo")
        self.sock = listener()
        self.port = self.sock.getsockname()[1]

    def tearDown(self):
        self.sock.close()
        self.tmp.cleanup()

    def run_server(self, client, idle="1"):
        fd = os.dup(self.sock.fileno())
        args = pv.parse(["_serve-file", "--root", str(self.root), "--port", str(self.port), "--idle", idle])[1]
        t = threading.Thread(target=client, daemon=True)
        with patched(pv, FILE_PORTS=range(self.port, self.port + 1), SD_LISTEN_FDS_START=fd), \
                env(LISTEN_PID=str(os.getpid()), LISTEN_FDS="1"), kept_signals(), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            t.start()
            started = time.monotonic()
            code = pv.cmd_serve_file(args)
            took = time.monotonic() - started
        t.join(10)
        return code, took, out.getvalue()

    def test_serves_the_socket_from_systemd_and_stops_when_idle(self):
        got = []
        code, took, out = self.run_server(lambda: got.append(http_get(self.port)))
        self.assertEqual(code, 0)
        self.assertEqual(got, [(200, b"hallo")])
        self.assertGreaterEqual(took, 1.0)
        self.assertIn("no connection for 1", out)

    def test_an_open_connection_keeps_it_running(self):
        def client():
            c = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            time.sleep(2.5)
            c.close()
        code, took, _ = self.run_server(client)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(took, 3.4)


class UnitTextTests(unittest.TestCase):
    ARGV = ["/usr/bin/python3", "-I", "/x/preview", "_serve", "--listen", "127.0.0.1:40001"]

    def test_plain_unit_is_unchanged(self):
        text = pv.unit_file_text("preview 5173: app", self.ARGV)
        self.assertTrue(text.startswith(pv.UNIT_MARKER + "\n"))
        self.assertIn("\n[Install]\nWantedBy=default.target\n", text)
        self.assertNotIn("Type=notify", text)

    def test_on_demand_service_has_no_install_and_reports_ready(self):
        text = pv.unit_file_text("preview 5173: app", self.ARGV + pv.IDLE_ARGS, on_demand=True)
        lines = text.splitlines()
        self.assertEqual(lines[0], pv.UNIT_MARKER)
        self.assertNotIn("[Install]", text)
        for line in ("Type=notify", "Restart=on-failure", f"RestartPreventExitStatus={pv.EXIT_CONFIG}",
                     "StartLimitIntervalSec=0"):
            self.assertIn(line, lines)
        self.assertTrue(any(l.startswith("ExecStart=") and l.endswith(f"--idle {pv.IDLE_SECONDS}") for l in lines))
        self.assertEqual(pv.IDLE_SECONDS, 600)

    def test_socket_unit(self):
        lines = pv.socket_file_text("preview 5173: app", "127.0.0.1:40001").splitlines()
        self.assertEqual(lines[0], pv.UNIT_MARKER)
        for line in ("[Socket]", "ListenStream=127.0.0.1:40001", "Accept=no", "[Install]", "WantedBy=sockets.target"):
            self.assertIn(line, lines)
        self.assertNotIn("FreeBind=yes", lines)
        self.assertIn("FreeBind=yes", pv.socket_file_text("x", f"{TS_IP}:5173").splitlines())
        self.assertIn("ListenStream=[::1]:5173", pv.socket_file_text("x", "[::1]:5173").splitlines())
        for bad in ("0.0.0.0:5173", "[::]:5173", "136.148.219.14:5173", "localhost:5173"):
            with self.assertRaises(ValueError, msg=bad):
                pv.socket_file_text("x", bad)

    def test_socket_names(self):
        self.assertEqual(pv.socket_of("preview-5173"), "preview-5173.socket")
        self.assertEqual(pv.socket_of("preview-file-18400.service"), "preview-file-18400.socket")
        for name in ("preview-5173.socket", "preview-file-18400.socket"):
            self.assertTrue(pv.UNIT_NAME.fullmatch(name))
        self.assertFalse(pv.UNIT_NAME.fullmatch("preview-cua-index.socket"))


class RecordingSystem(pv.System):
    """The real System class with systemctl recorded instead of run, and the unit folder in a temporary directory."""

    def __init__(self, unit_dir):
        super().__init__()
        self.dir = Path(unit_dir)
        self.argvs = []
        self.active = set()
        self.states = {}  # {full unit name: state} of the units systemd has loaded (both is-active and list-units)
        self.json = True  # False: list-units prints its table (a systemd without -o json)
        self.refuse = set()

    def unit_dir(self):
        return self.dir

    def state_of(self, unit):
        full = unit if unit.endswith((".service", ".socket")) else unit + ".service"
        return self.states.get(full) or ("active" if unit in self.active else "inactive")

    def run(self, argv, timeout=30):
        self.argvs.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "show"]:
            return subprocess.CompletedProcess(argv, 0, str(self.unit_path(argv[-1])) + "\n", "")
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            out = "".join(self.state_of(u) + "\n" for u in argv[3:])
            return subprocess.CompletedProcess(argv, 3, out, "")
        if tuple(argv[2:4]) in self.refuse:
            return subprocess.CompletedProcess(argv, 1, "", "refused")
        if argv[:3] == ["systemctl", "--user", "list-units"]:
            rows = [{"unit": u, "load": "loaded", "active": s, "sub": "x", "description": "d"} for u, s in self.states.items()]
            out = json.dumps(rows) if self.json else "UNIT LOAD ACTIVE SUB DESCRIPTION\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[:2] != ["systemctl", "--user"]:
            raise AssertionError(f"unexpected command {argv}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verbs(self):
        return [tuple(a[2:]) for a in self.argvs]


class SystemUnitFileTests(unittest.TestCase):
    ARGV = ["/usr/bin/python3", "-I", "/x/preview", "_serve", "--listen", "127.0.0.1:40001", "--target", "5173"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = RecordingSystem(self.tmp.name)
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_on_demand_writes_socket_and_service_and_enables_only_the_socket(self):
        ok, why = self.s.unit_start("preview-5173", "preview 5173: app", self.ARGV + pv.IDLE_ARGS, listen="127.0.0.1:40001")
        self.assertTrue(ok, why)
        service = (self.dir / "preview-5173.service").read_text()
        sock = (self.dir / "preview-5173.socket").read_text()
        self.assertIn("Type=notify", service)
        self.assertNotIn("[Install]", service)
        self.assertIn("ListenStream=127.0.0.1:40001", sock)
        verbs = self.s.verbs()
        self.assertIn(("enable", "preview-5173.socket"), verbs)
        self.assertNotIn(("enable", "preview-5173"), verbs)
        self.assertIn(("disable", "preview-5173"), verbs)  # an earlier plain unit file was enabled on its own
        self.assertLess(verbs.index(("start", "preview-5173.socket")), verbs.index(("start", "preview-5173")))
        self.assertLess(verbs.index(("stop", "preview-5173")), verbs.index(("start", "preview-5173.socket")))
        self.assertTrue(self.s.has_unit_file("preview-5173.socket"))
        self.assertTrue(self.s.unit_comes_back("preview-5173"))

    def test_converting_a_plain_unit_disables_it_before_the_socket_takes_over(self):
        self.assertTrue(self.s.unit_start("preview-5173", "preview 5173: app", self.ARGV)[0])
        self.assertIn("[Install]", (self.dir / "preview-5173.service").read_text())
        self.assertTrue(self.s.unit_comes_back("preview-5173"))
        self.s.argvs.clear()
        self.assertTrue(self.s.unit_start("preview-5173", "preview 5173: app", self.ARGV + pv.IDLE_ARGS,
                                          listen="127.0.0.1:40001")[0])
        verbs = self.s.verbs()
        self.assertLess(verbs.index(("disable", "preview-5173")), verbs.index(("daemon-reload",)))
        self.assertLess(verbs.index(("stop", "preview-5173")), verbs.index(("start", "preview-5173.socket")))

    def test_back_to_plain_removes_the_socket(self):
        self.s.unit_start("preview-5173", "preview 5173: app", self.ARGV + pv.IDLE_ARGS, listen="127.0.0.1:40001")
        self.s.argvs.clear()
        self.assertTrue(self.s.unit_start("preview-5173", "preview 5173: app", self.ARGV)[0])
        self.assertFalse((self.dir / "preview-5173.socket").exists())
        self.assertIn("[Install]", (self.dir / "preview-5173.service").read_text())
        verbs = self.s.verbs()
        self.assertIn(("disable", "preview-5173.socket"), verbs)
        self.assertLess(verbs.index(("stop", "preview-5173.socket")), verbs.index(("stop", "preview-5173")))
        self.assertIn(("enable", "preview-5173"), verbs)

    def test_stop_removes_the_socket_first(self):
        self.s.unit_start("preview-file-18400.service", "preview file 18400: x",
                          ["/usr/bin/python3", "-I", "/x/preview", "_serve-file", "--root", "/srv/x", "--port", "18400"],
                          listen="127.0.0.1:18400")
        self.s.argvs.clear()
        self.s.unit_stop("preview-file-18400.service")
        self.assertEqual(list(self.dir.iterdir()), [])
        verbs = self.s.verbs()
        self.assertIn(("disable", "preview-file-18400.socket"), verbs)
        self.assertLess(verbs.index(("stop", "preview-file-18400.socket")), verbs.index(("stop", "preview-file-18400.service")))
        self.assertEqual(self.s.own_unit_files(), [])

    def test_a_socket_that_cannot_start_leaves_nothing_behind(self):
        self.s.refuse.add(("start", "preview-5173.socket"))
        ok, why = self.s.unit_start("preview-5173", "preview 5173: app", self.ARGV + pv.IDLE_ARGS, listen="127.0.0.1:40001")
        self.assertFalse(ok)
        self.assertEqual(why, "refused")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_foreign_socket_file_is_left_alone(self):
        (self.dir / "preview-5173.socket").write_text("[Socket]\nListenStream=127.0.0.1:1\n")
        ok, why = self.s.unit_start("preview-5173", "d", self.ARGV + pv.IDLE_ARGS, listen="127.0.0.1:40001")
        self.assertFalse(ok)
        self.assertIn("not written by preview", why)
        self.assertEqual((self.dir / "preview-5173.socket").read_text(), "[Socket]\nListenStream=127.0.0.1:1\n")
        self.assertEqual(self.s.verbs(), [])

    def test_refuses_a_public_socket_address(self):
        ok, why = self.s.unit_start("preview-5173", "d", self.ARGV, listen="0.0.0.0:5173")
        self.assertFalse(ok)
        self.assertEqual(self.s.verbs(), [])

    def test_unit_states_in_one_call(self):
        self.s.active = {"preview-1"}
        self.assertEqual(self.s.unit_states(["preview-1", "preview-1.socket", "preview-1"]),
                         {"preview-1": "active", "preview-1.socket": "inactive"})
        self.assertEqual(len(self.s.argvs), 1)

    def test_many_unit_states_from_list_units_or_else_is_active(self):
        """preview list asks for hundreds of units: list-units lists them all at once (is-active with 334 names took
        60 ms). A unit systemd has not loaded is not listed and counts as inactive, as is-active says."""
        names = [n for p in range(5170, 5176) for n in (f"preview-{p}", f"preview-{p}.socket")]
        self.s.states = {"preview-5170.service": "active", "preview-5170.socket": "active", "preview-5171.service": "failed",
                         "preview-5171.socket": "active", "preview-5172.service": "inactive"}
        want = {n: self.s.state_of(n) for n in names}
        self.assertEqual(want["preview-5171"], "failed")
        self.assertEqual(self.s.unit_states(names), want)
        self.assertEqual(self.s.argvs, [["systemctl", "--user", "list-units", "--all", "-o", "json", "preview-*"]])
        for broken in ("table", "exit 1"):
            self.s.argvs.clear()
            self.s.json = broken != "table"
            self.s.refuse = {("list-units", "--all")} if broken == "exit 1" else set()
            self.assertEqual(self.s.unit_states(names), want, broken)
            self.assertEqual([a[2] for a in self.s.argvs], ["list-units", "is-active"], broken)
        self.s.argvs.clear()
        self.assertEqual(len(self.s.unit_states(names[:8])), 8)  # a few units: is-active, as before
        self.assertEqual([a[2] for a in self.s.argvs], ["is-active"])


class OnDemandLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = env(PREVIEW_STATE_DIR=self.tmp.name, PREVIEW_BACKEND="", HERDR_SESSION="", CONDUCTOR_AGENT="")
        self.env.__enter__()
        self.share = Path(self.tmp.name).resolve() / "seite"
        self.share.mkdir()
        (self.share / "index.html").write_text("<p>x</p>")

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def listed(self, s):
        out = io.StringIO()
        pv.cmd_list(s, as_json=True, out=out)
        return {row["port"]: row for row in json.loads(out.getvalue())}

    def file_share(self, s, path=None):
        out, err = io.StringIO(), io.StringIO()
        code = pv.cmd_file(s, str(path or self.share), out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_new_preview_starts_on_demand(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        self.assertEqual(run_start(s, 5173)[0], 0)
        unit = s.units["preview-5173"]
        self.assertEqual(unit["socket"], pv.load_state(5173)["listen"])
        self.assertEqual(unit["argv"][-2:], ["--idle", "600"])

    def test_idle_preview_is_healthy_and_left_alone(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        first = run_start(s, 5173, "app")
        s.idle.add("preview-5173")
        entry = pv.load_state(5173)
        s.connects.clear()
        self.assertTrue(pv.entry_healthy(s, entry))
        self.assertNotIn(pv.parse_hostport(entry["listen"]), s.connects)  # a test connection would wake the proxy
        self.assertEqual(run_start(s, 5173)[1], first[1])
        self.assertEqual([c for c in s.calls if c[0] == "unit_start"], [("unit_start", "preview-5173")])
        self.assertEqual(self.listed(s)[5173]["proxy"], "idle")

    def test_broken_on_demand_preview_is_not_healthy(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        run_start(s, 5173)
        s.failed.add("preview-5173")
        self.assertFalse(pv.entry_healthy(s, pv.load_state(5173)))
        self.assertEqual(self.listed(s)[5173]["proxy"], "failed")
        run_start(s, 5173)
        self.assertEqual(len([c for c in s.calls if c[0] == "unit_start"]), 2)
        s.sockets_down.add("preview-5173.socket")
        self.assertFalse(pv.entry_healthy(s, pv.load_state(5173)))
        self.assertEqual(self.listed(s)[5173]["proxy"], "socket inactive")

    def test_cua_share_keeps_running_all_the_time(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        cua = {"kind": "cua", "cua_unit": pv.CUA_UNIT, "owner_login": "owner@example.com"}
        code = pv.cmd_start(s, pv.CUA_PORT, "Cua", out=io.StringIO(), err=io.StringIO(), cua=cua)
        self.assertEqual(code, 0)
        unit = s.units[f"preview-{pv.CUA_PORT}"]
        self.assertIsNone(unit["socket"])
        self.assertNotIn("--idle", unit["argv"])

    def test_file_share_on_demand_idle_is_not_broken_and_never_woken(self):
        s = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        code, out, err = self.file_share(s)
        self.assertEqual(code, 0, err)
        port = pv.load_state(int(out.strip().rsplit(":", 1)[1]))["port"]
        file_unit = f"preview-file-{port}.service"
        self.assertEqual(s.units[file_unit]["socket"], f"127.0.0.1:{port}")
        self.assertEqual(s.units[file_unit]["argv"][-2:], ["--idle", "600"])
        s.idle |= {file_unit[:-len(".service")], f"preview-{port}"}
        s.connects.clear()
        row = self.listed(s)[port]
        self.assertEqual((row["proxy"], row["file_server"], row["target_listening"]), ("idle", "idle", True))
        code, _, err = self.file_share(s)  # run again: same link, nothing restarted
        self.assertEqual(code, 0)
        self.assertNotIn("Nothing listens", err)
        self.assertEqual(len([c for c in s.calls if c[0] == "unit_start"]), 2)
        self.assertNotIn(("127.0.0.1", port), s.connects)  # asking would start the file server
        s.failed.add(file_unit[:-len(".service")])
        self.assertFalse(self.listed(s)[port]["target_listening"])  # the socket listens, but the server cannot start

    def test_stop_removes_the_sockets(self):
        s = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        self.file_share(s)
        run_start(s, 5173)
        self.assertIn("preview-5173.socket", s.files)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pv.cmd_stop(s, 5173), 0)
        self.assertNotIn("preview-5173.socket", s.files)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pv.cmd_stop(s, path=str(self.share)), 0)
        self.assertEqual(s.files, {})
        self.assertEqual(s.units, {})

    def plain(self, s, port, **extra):
        """A preview as the release before on demand left it: a plain, enabled unit file that runs all the time."""
        entry = {"port": port, "label": f"app {port}", "backend": "https", "link": f"https://{TS_NAME}:{port}",
                 "listen": f"127.0.0.1:{40000 + port % 1000}", "unit": f"preview-{port}", "created": 1, "persistent": True,
                 **extra}
        s.serve[port] = f"http://{entry['listen']}"
        for unit, bound in [(entry["unit"], entry["listen"])] + ([(entry["file_unit"], f"127.0.0.1:{port}")]
                                                                 if entry.get("file_unit") else []):
            s.units[unit] = {"argv": ["--listen", bound], "listen": bound, "desc": "", "socket": None}
            s.files[norm(unit)] = "plain"
        pv.save_state(entry)
        return entry

    def restore(self, s, port=None):
        out, err = io.StringIO(), io.StringIO()
        code = pv.cmd_restore(s, out=out, err=err, port=port)
        return code, out.getvalue(), err.getvalue()

    def test_restore_converts_to_on_demand_and_is_idempotent(self):
        s = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        self.plain(s, 5173)
        self.plain(s, 18400, path=str(self.share), kind="folder", file_unit="preview-file-18400.service")
        self.plain(s, 18501, kind="cua-space", space_id="direct:127.0.0.1:8006", upstream="127.0.0.1:8006",
                   owner_login="owner@example.com")
        code, out, err = self.restore(s)
        self.assertEqual(code, 0, err)
        self.assertIn("restored 5173", out)
        self.assertIn("restored 18400", out)
        self.assertEqual(s.units["preview-5173"]["socket"], pv.load_state(5173)["listen"])
        self.assertEqual(s.units["preview-file-18400.service"]["socket"], "127.0.0.1:18400")
        self.assertEqual(s.units["preview-18400"]["socket"], pv.load_state(18400)["listen"])
        self.assertIsNone(s.units["preview-18501"]["socket"])  # Cua desktops stay as they are
        starts = [c for c in s.calls if c[0] == "unit_start"]
        self.assertNotIn(("unit_start", "preview-18501"), starts)
        self.assertLess(starts.index(("unit_start", "preview-file-18400.service")), starts.index(("unit_start", "preview-18400")))
        self.assertEqual(pv.load_state(5173)["link"], f"https://{TS_NAME}:5173")
        self.assertEqual(pv.load_state(5173)["listen"], "127.0.0.1:40173")  # same port, same link
        observed = pv.load_state(5173)["observed_since"]
        self.assertAlmostEqual(observed, time.time(), delta=60)
        s.calls.clear()
        code, out, _ = self.restore(s)
        self.assertEqual(code, 0)
        self.assertIn("nothing to restore", out)
        self.assertEqual([c for c in s.calls if c[0] in ("unit_start", "unit_stop")], [])
        self.assertEqual(pv.load_state(5173)["observed_since"], observed)

    def test_restore_one_port_only(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        self.plain(s, 5173)
        self.plain(s, 5174)
        code, out, _ = self.restore(s, port=5174)
        self.assertEqual(code, 0)
        self.assertEqual([c for c in s.calls if c[0] == "unit_start"], [("unit_start", "preview-5174")])
        self.assertIsNone(s.units["preview-5173"]["socket"])
        self.assertEqual(self.restore(s, port=5999)[0], 1)

    def test_restore_falls_back_to_running_all_the_time(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        self.plain(s, 5173)
        s.fail_on_demand = True
        code, out, err = self.restore(s)
        self.assertEqual(code, 1)
        self.assertIn("runs all the time as before", err)
        self.assertIsNone(s.units["preview-5173"]["socket"])
        self.assertNotIn("--idle", s.units["preview-5173"]["argv"])
        self.assertTrue(s.unit_comes_back("preview-5173"))

    def test_doctor_counts_on_demand_and_persistence(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        s.run = lambda argv, timeout=30: subprocess.CompletedProcess(argv, 0, "yes\n", "")
        s.owner_login = lambda: None
        self.plain(s, 5173)
        run_start(s, 5174)
        out = io.StringIO()
        pv.cmd_doctor(s, out=out)
        self.assertIn("on demand      1 of 2 previews", out.getvalue())
        self.assertIn("persistent     yes", out.getvalue())
        s.files.pop("preview-5174.socket")  # the service alone would not come back after a reboot
        out = io.StringIO()
        pv.cmd_doctor(s, out=out)
        self.assertIn("persistent     no (5174", out.getvalue())


# ---------------------------------------------------------------------------
# Who made a preview and when a person last opened it
# ---------------------------------------------------------------------------

class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = env(PREVIEW_STATE_DIR=self.tmp.name, PREVIEW_BACKEND="")
        self.env.__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def listed(self, s):
        out = io.StringIO()
        pv.cmd_list(s, as_json=True, out=out)
        return {row["port"]: row for row in json.loads(out.getvalue())}

    def test_created_by_and_observed_since_on_a_new_preview(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        with env(HERDR_SESSION="website", CONDUCTOR_AGENT="board-design"):
            run_start(s, 5173)
        row = self.listed(s)[5173]
        self.assertEqual(row["created_by"], {"session": "website", "agent": "board-design"})
        self.assertEqual(row["observed_since"], row["created"])
        self.assertIsNone(row["opened_at"])
        with env(HERDR_SESSION="", CONDUCTOR_AGENT=""):
            run_start(s, 5174)
        self.assertIsNone(self.listed(s)[5174]["created_by"])
        with env(HERDR_SESSION="other", CONDUCTOR_AGENT=""):
            run_start(s, 5173)  # run again by someone else: still the first creator
        self.assertEqual(self.listed(s)[5173]["created_by"], {"session": "website", "agent": "board-design"})

    def test_old_entries_list_null(self):
        pv.save_state({"port": 5173, "label": "x", "backend": "https", "link": f"https://{TS_NAME}:5173",
                       "listen": "127.0.0.1:40001", "unit": "preview-5173", "created": 5})
        row = self.listed(FakeSystem())[5173]
        self.assertEqual((row["opened_at"], row["observed_since"], row["created_by"], row["created"]), (None, None, None, 5))

    def test_recreating_a_preview_keeps_who_made_it_and_when_it_was_opened(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        with env(HERDR_SESSION="website", CONDUCTOR_AGENT=""):
            run_start(s, 5173)
        entry = pv.load_state(5173)
        entry.update(opened_at=1793000000, observed_since=1792000000)
        pv.save_state(entry)
        s.units.clear()  # the proxy is gone: run again makes it new
        with env(HERDR_SESSION="other", CONDUCTOR_AGENT=""):
            run_start(s, 5173)
        row = self.listed(s)[5173]
        self.assertEqual((row["opened_at"], row["observed_since"], row["created_by"]),
                         (1793000000, 1792000000, {"session": "website", "agent": None}))

    def test_saving_keeps_a_newer_open_the_proxy_wrote(self):
        pv.save_state({"port": 5173, "listen": "127.0.0.1:40001", "opened_at": 100, "observed_since": 50})
        stale = {"port": 5173, "listen": "127.0.0.1:40001", "label": "new", "opened_at": 90, "observed_since": 70}
        pv.save_state(stale)
        self.assertEqual(pv.load_state(5173), {**stale, "opened_at": 100, "observed_since": 50})

    def test_record_opened_only_updates_its_own_existing_entry(self):
        path = pv.state_dir() / "5173.json"
        self.assertFalse(pv.record_opened(path, "127.0.0.1:40001", 1000))  # never creates a record
        self.assertFalse(path.exists())
        pv.save_state({"port": 5173, "listen": "127.0.0.1:40001", "label": "x"})
        self.assertFalse(pv.record_opened(path, "127.0.0.1:49999", 1000))  # another proxy's record
        self.assertTrue(pv.record_opened(path, "127.0.0.1:40001", 1000))
        self.assertFalse(pv.record_opened(path, "127.0.0.1:40001", 900))  # never goes back in time
        self.assertEqual(pv.load_state(5173), {"port": 5173, "listen": "127.0.0.1:40001", "label": "x", "opened_at": 1000})
        self.assertEqual(sorted(p.name for p in pv.state_dir().iterdir() if not p.name.startswith(".lock")), ["5173.json"])


class OpenedAtProxyTests(unittest.IsolatedAsyncioTestCase):
    """A person's request (Tailscale Serve adds Tailscale-User-Login) records opened_at; nothing else does."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = env(PREVIEW_STATE_DIR=self.tmp.name)
        self.env.__enter__()
        self.up = Upstream()
        self.up.stream_release = asyncio.Event()
        self.up_server = await asyncio.start_server(self.up.handle, "127.0.0.1", 0)
        self.port = self.up_server.sockets[0].getsockname()[1]
        self.listen = "127.0.0.1:40001"
        pv.save_state({"port": self.port, "label": "x", "listen": self.listen, "unit": f"preview-{self.port}"})
        self.path = pv.state_dir() / f"{self.port}.json"
        proxy = pv.Proxy(self.port, f"https://{TS_NAME}:{self.port}", tailnet_ip=TS_IP,
                         state_file=self.path, listen=self.listen)
        self.proxy_server = await pv.start_proxy_server("127.0.0.1", 0, proxy)
        self.pport = self.proxy_server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        for s in (self.proxy_server, self.up_server):
            s.close()
        for s in (self.proxy_server, self.up_server):
            await s.wait_closed()
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    async def get(self, *extra):
        r, w = await asyncio.open_connection("127.0.0.1", self.pport)
        w.write(b"GET / HTTP/1.1\r\nHost: " + f"{TS_NAME}:{self.port}".encode() + b"\r\n" + b"".join(e + b"\r\n" for e in extra)
                + b"\r\n")
        await w.drain()
        head = await asyncio.wait_for(pv.read_head(r), 5)
        w.close()
        return head

    async def opened(self, wait=3.0):
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            value = pv.load_state(self.port).get("opened_at")
            if value is not None:
                return value
            await asyncio.sleep(0.05)
        return None

    async def test_a_person_opening_it_is_recorded_without_the_login(self):
        self.assertIn(b"200", await self.get(b"Tailscale-User-Login: someone@example.com", b"X-Forwarded-For: 100.90.1.2"))
        when = await self.opened()
        self.assertAlmostEqual(when, time.time(), delta=60)
        self.assertNotIn("someone", self.path.read_text())

    async def test_requests_without_an_identity_or_from_this_machine_do_not_count(self):
        await self.get()  # an agent on this machine through 127.0.0.1
        await self.get(b"Tailscale-User-Login: someone@example.com", b"X-Forwarded-For: " + TS_IP.encode())  # via the link
        await self.get(b"Tailscale-User-Login: someone@example.com", b"X-Forwarded-For: ::1")  # an address of this machine
        self.assertIsNone(await self.opened(wait=1.0))

    def test_local_address(self):
        self.assertTrue(pv.local_address("127.0.0.1"))
        self.assertTrue(pv.local_address("::1"))
        self.assertFalse(pv.local_address("192.0.2.7"))  # TEST-NET-1: never on a machine
        self.assertFalse(pv.local_address("not an address"))

    async def test_at_most_once_a_minute(self):
        await self.get(b"Tailscale-User-Login: someone@example.com")
        await self.opened()
        entry = pv.load_state(self.port)
        entry["opened_at"] = 5
        self.path.write_text(json.dumps(entry))
        await self.get(b"Tailscale-User-Login: someone@example.com")
        await asyncio.sleep(0.5)
        self.assertEqual(pv.load_state(self.port)["opened_at"], 5)

    async def test_the_request_never_waits_for_the_state_file(self):
        with pv.state_lock(pv.state_dir()):  # someone else holds the state lock
            started = time.monotonic()
            self.assertIn(b"200", await self.get(b"Tailscale-User-Login: someone@example.com"))
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertNotIn("opened_at", pv.load_state(self.port))
        self.assertIsNotNone(await self.opened())


# ---------------------------------------------------------------------------
# Latency of the commands agents call: no asyncio, each read-only query once per command and at once
# ---------------------------------------------------------------------------

def full_unit(name):
    return name if name.endswith((".service", ".socket")) else name + ".service"


class Queries:
    """Stands in for subprocess.run under the real System class: answers the read-only queries preview asks (Tailscale,
    ss, systemctl is-active) after `delay` seconds each, also several at once, and records them. Any other command fails
    the test, unless it is in `allowed`; nothing real runs. `serve` is {port: listen address its Serve entry leads to}."""

    def __init__(self, delay=0.0, serve=None, states=None, listening="", tailnet_ok=True, allowed=()):
        self.delay, self.serve, self.states, self.listening = delay, dict(serve or {}), states or {}, listening
        self.tailnet_ok, self.allowed = tailnet_ok, {tuple(a) for a in allowed}
        self.lock, self.argvs, self.running, self.peak = threading.Lock(), [], 0, 0

    def __call__(self, argv, **kwargs):
        with self.lock:
            self.argvs.append(list(argv))
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.delay)
            return self.answer(list(argv))
        finally:
            with self.lock:
                self.running -= 1

    def answer(self, argv):
        done = lambda out, code=0: subprocess.CompletedProcess(argv, code, out, "")
        if argv == ["tailscale", "status", "--self", "--peers=false", "--json"]:
            me = {"Self": {"DNSName": TS_NAME + ".", "TailscaleIPs": [TS_IP]}}
            return done(json.dumps(me)) if self.tailnet_ok else done("", 1)
        if argv == ["tailscale", "serve", "status", "--json"]:
            web = {f"{TS_NAME}:{p}": {"Handlers": {"/": {"Proxy": f"http://{listen}"}}} for p, listen in self.serve.items()}
            return done(json.dumps({"TCP": {str(p): {"HTTPS": True} for p in self.serve}, "Web": web}))
        if argv == ["tailscale", "debug", "prefs"]:
            return done(json.dumps({"OperatorUser": pv.current_user()}))
        if argv[:2] in (["ss", "-Hltn"], ["ss", "-Hltnp"]):
            return done(self.listening)
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            states = [self.states.get(full_unit(u), "inactive") for u in argv[3:]]
            return done("".join(s + "\n" for s in states), 0 if all(s == "active" for s in states) else 3)
        if tuple(argv) in self.allowed:
            return done("")
        raise AssertionError(f"a test ran a command that is not a read-only query: {argv}")

    def count(self, *prefix):
        return sum(1 for a in self.argvs if a[:len(prefix)] == list(prefix))


class QueriedSystem(pv.System):
    """The real System class (its run, memo and queries) with unit files in a temporary folder; every test connection
    finds a server."""

    def __init__(self, unit_dir):
        super().__init__()
        self.dir = Path(unit_dir)

    def unit_dir(self):
        return self.dir

    def can_connect(self, host, port, timeout=0.5):
        return True

    def sleep(self, seconds):
        pass


class CliImportTests(unittest.TestCase):
    """asyncio costs ~45 ms at every start; only the proxy (preview _serve) uses it."""

    def imported(self, *args, **environ):
        r = subprocess.run([sys.executable, "-X", "importtime", TOOL, *args], capture_output=True, text=True, timeout=60,
                           env={**os.environ, **environ})
        names = {line.rsplit("|", 1)[1].strip() for line in r.stderr.splitlines() if line.startswith("import time:")}
        self.assertIn("argparse", names)  # the list of imports was read
        return r, sorted(n for n in names if n == "asyncio" or n.startswith("asyncio."))

    def test_cli_commands_do_not_import_asyncio(self):
        r, found = self.imported("--help")
        self.assertEqual((r.returncode, found), (0, []))
        with tempfile.TemporaryDirectory() as d:  # no previews recorded: list runs no system command
            r, found = self.imported("list", "--json", PREVIEW_STATE_DIR=d)
            self.assertEqual((r.returncode, r.stdout, found), (0, "[]\n", []))


class QueryMemoTests(unittest.TestCase):
    def test_a_read_only_query_is_asked_once_until_another_command_runs(self):
        q = Queries(allowed=[["systemctl", "--user", "daemon-reload"]])
        s = pv.System()
        argv = ["ss", "-Hltn", "sport = :5173"]
        with patched(pv.subprocess, run=q):
            first = s.run(argv)
            self.assertIs(s.run(argv), first)
            s.run(["ss", "-Hltn", "sport = :5174"])  # another query: its own answer
            self.assertEqual(q.count("ss"), 2)
            s.run(["systemctl", "--user", "daemon-reload"])  # may change what the queries say
            self.assertIsNot(s.run(argv), first)
            s.run(["ss", "-Hltn", "sport = :5174"])
        self.assertEqual((q.count(*argv), q.count("ss", "-Hltn", "sport = :5174")), (2, 2))

    def test_prefetch_asks_at_once_in_at_most_six_threads_and_leaves_errors_to_the_real_call(self):
        q = Queries(delay=0.1, tailnet_ok=False)
        s = pv.System()
        ports = [lambda p=p: s.listen_addresses(p) for p in range(5001, 5008)]
        with patched(pv.subprocess, run=q):
            started = time.monotonic()
            s.prefetch(s.tailnet, s.serve_ports, s.operator_ok, *ports, lambda: 1 / 0)
            took = time.monotonic() - started
            self.assertLessEqual(q.peak, 6)
            self.assertGreater(q.peak, 1)
            self.assertLess(took, 0.35)  # one after another: 1 s
            with self.assertRaises(pv.TailnetUnknown):  # the error waits for the real call
                s.tailnet()
            self.assertEqual(s.listen_addresses(5007), [])
        self.assertEqual(len(q.argvs), 10)  # the real calls were answered from the memo

    def test_listen_addresses_without_process_names_listeners_with_them(self):
        q = Queries(listening='LISTEN 0 511 127.0.0.1:5173 0.0.0.0:* users:(("node",pid=7,fd=21))\n'
                              "LISTEN 0 511 [::1]:5173 [::]:*\nLISTEN 0 4096 *:5173 *:*\n")
        s = pv.System()
        with patched(pv.subprocess, run=q):
            self.assertEqual(s.listen_addresses(5173), ["127.0.0.1", "::1", "*"])
            self.assertEqual(s.listeners(5173), [("127.0.0.1", "node"), ("::1", ""), ("*", "")])
        self.assertEqual([a[:2] for a in q.argvs], [["ss", "-Hltn"], ["ss", "-Hltnp"]])


class RerunLatencyTests(unittest.TestCase):
    """Agents run preview <port> and preview file <path> again and again on the same preview. It used to ask 7 and 15 read-only
    queries one after another (150 and 260 ms); now each query once, all at once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        (base / "state").mkdir()
        (base / "units").mkdir()
        self.env = env(PREVIEW_STATE_DIR=str(base / "state"), PREVIEW_BACKEND="")
        self.env.__enter__()
        self.s = QueriedSystem(base / "units")
        self.share = base / "seite"
        self.share.mkdir()
        (self.share / "index.html").write_text("<p>x</p>")

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def on_demand(self, port, **extra):
        """A preview as this release leaves it: on demand, asleep behind its socket, HTTPS."""
        entry = {"port": port, "label": "app", "backend": "https", "link": f"https://{TS_NAME}:{port}",
                 "listen": f"127.0.0.1:{40000 + port % 1000}", "unit": f"preview-{port}", "created": 1, "persistent": True,
                 **extra}
        pv.save_state(entry)
        for unit in pv.entry_units(entry):
            for name in (unit, pv.socket_of(unit)):
                self.s.unit_path(name).write_text(pv.UNIT_MARKER + "\n")
        return entry

    def timed(self, q, command):
        out, err = io.StringIO(), io.StringIO()
        with patched(pv.subprocess, run=q):
            started = time.monotonic()
            code = command(out, err)
            took = time.monotonic() - started
        return code, out.getvalue(), err.getvalue(), took

    def assert_each_query_once(self, q):
        self.assertEqual(sorted(map(tuple, q.argvs)), sorted({tuple(a) for a in q.argvs}))

    def test_rerun_of_a_preview(self):
        entry = self.on_demand(5173)
        q = Queries(delay=0.1, serve={5173: entry["listen"]}, states={"preview-5173.socket": "active"})
        code, out, err, took = self.timed(q, lambda out, err: pv.cmd_start(self.s, 5173, out=out, err=err))
        self.assertEqual((code, out, err), (0, f"https://{TS_NAME}:5173\n", "  HTTPS via Tailscale Serve to localhost:5173, "
                                                                           "tailnet devices only. Stop: preview stop 5173\n"))
        self.assertEqual(q.count("tailscale", "serve", "status"), 1)
        self.assert_each_query_once(q)
        self.assertLess(took, 0.2)

    def test_rerun_of_a_file_share(self):
        entry = self.on_demand(18400, path=str(self.share), kind="folder", file_unit="preview-file-18400.service")
        q = Queries(delay=0.1, serve={18400: entry["listen"]}, states={"preview-18400.socket": "active", "preview-file-18400.socket": "active"})
        code, out, err, took = self.timed(q, lambda out, err: pv.cmd_file(self.s, str(self.share), out=out, err=err))
        self.assertEqual((code, out), (0, f"https://{TS_NAME}:18400\n"))
        self.assertEqual(err, "  HTTPS via Tailscale Serve to localhost:18400, tailnet devices only. Stop: preview stop 18400\n"
                              f"  Shares {self.share} read-only. Stop: preview stop {pv.shlex.quote(str(self.share))}\n")
        self.assert_each_query_once(q)
        self.assertLess(took, 0.2)

    def test_address_check_asks_ss_without_process_names(self):
        q = Queries(listening="LISTEN 0 511 0.0.0.0:5173 0.0.0.0:*\n")
        code, out, err, _ = self.timed(q, lambda out, err: pv.cmd_start(self.s, 5173, "app", out=out, err=err))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("0.0.0.0:5173, which includes the public internet", err)
        self.assertEqual((q.count("ss", "-Hltn"), q.count("ss", "-Hltnp")), (1, 0))

    def test_tailscale_down_is_still_reported(self):
        """A guard: the query asked ahead fails quietly, the real call reports it as before."""
        entry = self.on_demand(5173)
        q = Queries(tailnet_ok=False, serve={5173: entry["listen"]})
        err = io.StringIO()
        with patched(pv.subprocess, run=q), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pv.main(["5173"], sysm=self.s), 1)
        self.assertIn("preview: tailscale status gave no tailnet name and IP", err.getvalue())
        self.assertEqual(q.count("tailscale", "status"), 1)


# ---------------------------------------------------------------------------
# One preview, three places (record, units, Tailscale Serve entry): runs that end early, Tailscale errors, two agents
# at once. F1 to F14 number the cases below.
# ---------------------------------------------------------------------------

P = 5173


def quiet_stop(s, *args, **kw):
    out, err = io.StringIO(), io.StringIO()
    return pv.cmd_stop(s, *args, out=out, err=err, **kw), out.getvalue(), err.getvalue()


def quiet_file(s, path):
    out, err = io.StringIO(), io.StringIO()
    return pv.cmd_file(s, str(path), out=out, err=err), out.getvalue(), err.getvalue()


def sharing(base, next_free):
    """A second agent: its own System object (own memo), the same systemd, Tailscale and network."""
    other = FakeSystem(operator=base.operator, serve_allowed=base.serve_allowed, target_up=base.target_up)
    for name in ("units", "files", "serve", "idle", "failed", "sockets_down", "busy", "calls", "connects"):
        setattr(other, name, getattr(base, name))
    other.next_free = next_free
    return other


def failing_remove(s):
    def remove(port):
        s.calls.append(("serve_remove", port))
        return False, "tailscale: timed out"  # the entry stays
    return remove


def meanwhile(command):
    """Agent B: `command` in its own thread, started while agent A is in the middle of a run. Waits until B is done, or
    0.5 s while B waits for A's lock; without a lock B runs all the way through first. ({"value", "done"} when B ends.)"""
    result = {}

    def run():
        result["value"] = command()
        result["done"] = time.monotonic()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(0.5)
    return thread, result


class StateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = env(PREVIEW_STATE_DIR=self.tmp.name, PREVIEW_BACKEND="", HERDR_SESSION="", CONDUCTOR_AGENT="")
        self.env.__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def folder(self, name):
        d = Path(self.tmp.name).resolve() / name
        d.mkdir()
        (d / "index.html").write_text(f"<p>{name}</p>")
        return d

    def assert_one_world(self, s, port):
        """Record, proxy unit and Serve entry exist together or not at all, and the Serve entry leads to the proxy."""
        st = pv.load_state(port)
        there = (st is not None, f"preview-{port}" in s.units, port in s.serve)
        self.assertIn(there, [(True, True, True), (False, False, False)], f"record, proxy unit, Serve entry: {there}")
        if st is not None:
            self.assertEqual(s.serve[port], "http://" + st["listen"], "the Serve entry does not lead to the proxy")

    def finished(self, agent_b):
        """Agent B's result and when it ended."""
        thread, result = agent_b[0]
        thread.join(10)
        self.assertIn("value", result, "agent B did not finish")
        return result["value"], result["done"]


class StopAllOwnerTests(StateCase):
    def start_as(self, s, pane, port):
        with env(HERDR_PANE_ID=pane, HERDR_SESSION="main"):
            run_start(s, port)

    def test_g7_stop_all_under_a_leaves_b(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        self.start_as(s, "w1:p1", 5173)
        self.start_as(s, "w2:p1", 5174)
        with env(HERDR_PANE_ID="w1:p1", HERDR_SESSION="main"):
            self.assertEqual(quiet_stop(s, all_=True)[0], 0)
        self.assertEqual([e["port"] for e in pv.all_states()], [5174])
        self.assertIn(5174, s.serve)
        self.assertNotIn(5173, s.serve)

    def test_g7_old_entries_without_owner_are_skipped_and_counted(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        self.start_as(s, "w1:p1", 5173)
        for p in (5174, 5175):
            self.start_as(s, "w1:p1", p)
            st = pv.load_state(p)
            st.pop("owner")
            pv.save_state(st)
        self.start_as(s, "w2:p1", 5176)
        with env(HERDR_PANE_ID="w1:p1", HERDR_SESSION="main"):
            code, out, err = quiet_stop(s, all_=True)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(e["port"] for e in pv.all_states()), [5174, 5175, 5176])
        self.assertIn("kept 3 previews", out)
        self.assertIn("--everyone", out)

    def test_g7_everyone_stops_all(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        self.start_as(s, "w1:p1", 5173)
        self.start_as(s, "w2:p1", 5174)
        st = pv.load_state(5174)
        st.pop("owner")
        pv.save_state(st)
        with env(HERDR_PANE_ID="w1:p1", HERDR_SESSION="main"):
            self.assertEqual(quiet_stop(s, all_=True, everyone=True)[0], 0)
        self.assertEqual(pv.all_states(), [])

    def test_g7_no_session_key_refuses_all(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        self.start_as(s, "w1:p1", 5173)
        with env(HERDR_PANE_ID="", HERDR_SESSION=""):
            code, out, err = quiet_stop(s, all_=True)
        self.assertEqual(code, 1)
        self.assertIn("--everyone", err)
        self.assertEqual([e["port"] for e in pv.all_states()], [5173])

    def test_g7_cli_everyone_needs_all(self):
        self.assertTrue(pv.parse(["stop", "--all", "--everyone"])[1].everyone)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pv.parse(["stop", "--everyone"])


class PortChoiceTests(StateCase):
    """G1: a share port must be bindable (F8); a proxy's internal port stays out of the share ports (F8) and off the
    ports of recorded previews (F14)."""

    def check(self, close_first):
        srv = socket.create_server(("127.0.0.1", 0))
        cli = socket.create_connection(srv.getsockname())  # its source port comes from 10240-65535, like any connection
        conn, _ = srv.accept()
        busy = cli.getsockname()[1]
        try:
            if close_first:
                cli.close()  # the source port stays in TIME_WAIT for 60 s
                conn.close()
            sysm = QueriedSystem(self.tmp.name)
            nothing_answers = lambda host, port, timeout=0.5: False  # true there: no server, only a connection's end
            with patched(pv, FILE_PORTS=range(busy, busy + 1)), patched(sysm, serve_ports=set, can_connect=nothing_answers):
                with patched(pv.subprocess, run=Queries()):  # ss: nothing listens there
                    self.assertIsNone(pv.pick_file_port(sysm))  # the socket of systemd would fail: Address already in use
        finally:
            for x in (cli, conn, srv):
                x.close()

    def test_f8_port_of_an_open_connection_is_skipped(self):
        self.check(close_first=False)

    def test_f8_port_in_time_wait_is_skipped(self):
        self.check(close_first=True)

    def test_f8_file_share_takes_the_next_bindable_port(self):
        s = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        s.busy.add(18400)
        code, out, err = quiet_file(s, self.folder("seite"))
        self.assertEqual((code, out), (0, f"https://{TS_NAME}:18401\n"), err)

    def test_f14_internal_ports_skip_share_ports_and_recorded_ports(self):
        offered = []

        class Offering:  # socket.socket: bind to port 0 gets the next offered port
            def __init__(self, *a, **kw):
                self.port = None

            def bind(self, address):
                self.port = offered.pop(0)

            def getsockname(self):
                return "127.0.0.1", self.port

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        with patched(pv.socket, socket=Offering):
            offered[:] = [18453, 41000]
            self.assertEqual(pv.System().free_local_port(), 41000)
            offered[:] = [18547, 5173, 41001]
            self.assertEqual(pv.System().free_local_port(avoid={5173}), 41001)


class IntentFirstTests(StateCase):
    """G3: the record is written before each effect and says what is under way; a failed `tailscale serve … off` keeps
    it; an unanswered `tailscale serve status` changes nothing."""

    def test_f1_failed_serve_off_on_stop_keeps_the_preview(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        self.assertEqual(run_start(s, P)[0], 0)
        with patched(s, serve_remove=failing_remove(s)):
            code, out, err = quiet_stop(s, P)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"preview stop {P}", err)
        self.assert_one_world(s, P)  # nothing changed: the preview still runs
        self.assertTrue(pv.entry_healthy(s, pv.load_state(P)))
        code, _, err = run_start(s, P)  # Tailscale answers again
        self.assertEqual(code, 0, err)
        self.assertEqual(quiet_stop(s, P)[0], 0)
        self.assert_one_world(s, P)

    def test_f1_stop_all_finishes_a_stop_that_failed(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        run_start(s, P)
        with patched(s, serve_remove=failing_remove(s)):
            quiet_stop(s, P)
        self.assertEqual(quiet_stop(s, all_=True, everyone=True)[0], 0)
        self.assertNotIn(P, s.serve)
        self.assert_one_world(s, P)

    def test_f2_a_create_that_ends_early_is_cleaned_up_by_the_next_run(self):
        s = FakeSystem(operator=True, serve_allowed=True)

        def killed(*a, **kw):
            raise KeyboardInterrupt  # stands for SIGTERM, SIGINT or the time limit of an agent's tool

        with patched(s, unit_start=killed), self.assertRaises(KeyboardInterrupt):
            run_start(s, P)
        self.assertEqual((pv.load_state(P) or {}).get("status"), "creating")  # the record names what it set up
        code, _, err = run_start(s, P)
        self.assertEqual(code, 0, err)
        self.assert_one_world(s, P)
        self.assertEqual(quiet_stop(s, P)[0], 0)
        self.assert_one_world(s, P)

    def test_f2_proxy_fails_and_serve_off_fails_then_the_next_run_works(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        s.fail_on_demand = True
        with patched(s, serve_remove=failing_remove(s)):
            code, _, failed = run_start(s, P)
        self.assertEqual(code, 1)
        s.fail_on_demand = False
        code, _, err = run_start(s, P)
        self.assertEqual(code, 0, err)
        self.assert_one_world(s, P)
        self.assertIn(f"preview stop {P}", failed)  # the first run said how to clean up

    def test_f11_a_file_share_that_ends_early_stops_by_its_path(self):
        s = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        share = self.folder("seite")
        own_start = s.unit_start

        def killed_at_the_proxy(unit, desc, argv, listen=None):
            if unit.startswith(pv.FILE_UNIT_PREFIX):
                return own_start(unit, desc, argv, listen)
            raise KeyboardInterrupt

        with patched(s, unit_start=killed_at_the_proxy), self.assertRaises(KeyboardInterrupt):
            quiet_file(s, share)
        self.assertEqual(quiet_stop(s, path=str(share))[0], 0)
        self.assertEqual((s.units, s.files, set(s.serve)), ({}, {}, {8443}))

    def test_f12_unanswered_serve_status_changes_nothing(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        run_start(s, P)
        before = (pv.load_state(P), dict(s.serve), dict(s.units))
        no_answer = lambda argv, timeout=30: subprocess.CompletedProcess(argv, 1, "", "tailscale: timed out")
        broken = dict(run=no_answer, serve_ports=lambda: pv.System.serve_ports(s),
                      serve_targets=lambda: pv.System.serve_targets(s), serve_remove=failing_remove(s),
                      serve_add=lambda port, internal: (False, "tailscale: timed out"))
        with patched(s, **broken):
            code, out, err = run_start(s, P)
        self.assertEqual((code, out), (1, ""))  # not rebuilt as http://… while the Serve entry may still be there
        self.assertIn("tailscale serve status", err)
        self.assertEqual((pv.load_state(P), dict(s.serve), dict(s.units)), before)
        self.assertEqual(run_start(s, P)[:2], (0, f"https://{TS_NAME}:{P}\n"))  # Tailscale answers again


class OneFlowTests(StateCase):
    """G4: creating and stopping take one lock, so a second agent waits instead of running in between (F3, F4, F6)."""

    def test_f3_two_agents_on_one_port_get_one_working_preview(self):
        a = FakeSystem(operator=True, serve_allowed=True)
        b = sharing(a, next_free=50000)
        before = set(a.serve)
        b.serve_ports = lambda: set(before)  # B asked serve status before A's serve_add (the memo keeps that answer)
        own_start, agent_b = a.unit_start, []

        def a_unit_start(unit, desc, argv, listen=None):
            if not agent_b:  # B starts while A stands between serve_add and its unit files
                agent_b.append(meanwhile(lambda: run_start(b, P)))
            return own_start(unit, desc, argv, listen)

        with patched(a, unit_start=a_unit_start):
            code_a, link_a, _ = run_start(a, P)
        a_done = time.monotonic()
        (code_b, link_b, _), b_done = self.finished(agent_b)
        self.assertEqual((code_a, code_b, link_a, link_b), (0, 0, f"https://{TS_NAME}:{P}\n", f"https://{TS_NAME}:{P}\n"))
        self.assert_one_world(a, P)
        self.assertGreater(b_done, a_done, "agent B did not wait for agent A")

    def test_f4_stop_all_while_another_agent_creates(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        own_save, agent_b = pv.save_state, []

        def save(entry):
            if entry.get("port") == P and not agent_b:
                agent_b.append(meanwhile(lambda: quiet_stop(s, all_=True, everyone=True)))  # agent B: preview stop --all
            return own_save(entry)

        with patched(pv, save_state=save):
            code = run_start(s, P)[0]
        a_done = time.monotonic()
        (code_b, _, _), b_done = self.finished(agent_b)
        self.assertEqual((code, code_b), (0, 0))
        self.assert_one_world(s, P)
        self.assertGreater(b_done, a_done, "agent B did not wait for agent A")

    def test_f6_two_folders_at_once_get_two_ports(self):
        a = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        b = sharing(a, next_free=50000)
        dir_a, dir_b = self.folder("a"), self.folder("b")
        own_start, agent_b = a.unit_start, []

        def a_unit_start(unit, desc, argv, listen=None):
            if unit.startswith(pv.FILE_UNIT_PREFIX) and not agent_b:  # B picks a port before A's file socket listens
                agent_b.append(meanwhile(lambda: quiet_file(b, dir_b)))
            return own_start(unit, desc, argv, listen)

        with patched(a, unit_start=a_unit_start):
            code_a, link_a, _ = quiet_file(a, dir_a)
        a_done = time.monotonic()
        (code_b, link_b, _), b_done = self.finished(agent_b)
        self.assertEqual((code_a, code_b), (0, 0))
        self.assertEqual({e.get("path") for e in pv.all_states()}, {str(dir_a), str(dir_b)})
        self.assertNotEqual(link_a, link_b)
        self.assertGreater(b_done, a_done, "agent B did not wait for agent A")
        self.assertEqual(quiet_stop(b, path=str(dir_b))[0], 0)
        self.assertEqual(quiet_stop(a, path=str(dir_a))[0], 0)


class ServeTargetTests(StateCase):
    """G5: healthy means the Serve entry leads to the preview's own proxy; list --json and doctor show where it leads."""

    def test_f3_entry_healthy_checks_where_serve_leads(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        run_start(s, P)
        s.serve[P] = "http://127.0.0.1:1"  # Serve leads elsewhere
        self.assertFalse(pv.entry_healthy(s, pv.load_state(P)))
        code, _, err = run_start(s, P)  # running it again repairs it
        self.assertEqual(code, 0, err)
        self.assert_one_world(s, P)

    def test_f3_list_and_doctor_show_where_serve_leads(self):
        s = FakeSystem(operator=True, serve_allowed=True)
        for port in (P, 5174, 5175):
            run_start(s, port)
        s.operator = False
        run_start(s, 8000)
        s.serve[P] = "http://127.0.0.1:1"
        del s.serve[5174]
        out = io.StringIO()
        pv.cmd_list(s, as_json=True, out=out)
        rows = {row["port"]: row.get("serve", "no field") for row in json.loads(out.getvalue())}
        self.assertEqual(rows, {P: "elsewhere", 5174: "missing", 5175: "ok", 8000: None})
        s.run = lambda argv, timeout=30: subprocess.CompletedProcess(argv, 0, "yes\n", "")
        out = io.StringIO()
        pv.cmd_doctor(s, out=out)
        self.assertIn(f"broken         {P} (serve elsewhere), 5174 (serve missing); fix: preview <port> again", out.getvalue())


class RebuildTests(StateCase):
    """G6: `preview <port>` on a file share with a broken proxy rebuilds it as the same file share (F7)."""

    def test_f7_rebuild_keeps_path_and_file_unit(self):
        s = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        share = self.folder("seite")
        code, out, err = quiet_file(s, share)
        self.assertEqual(code, 0, err)
        port = int(out.strip().rsplit(":", 1)[1])
        s.failed.add(f"preview-{port}")  # the proxy failed; an agent repairs it with preview <port>
        self.assertEqual(run_start(s, port)[0], 0)
        st = pv.load_state(port)
        self.assertEqual((st.get("path"), st.get("kind"), st.get("file_unit")),
                         (str(share), "folder", f"preview-file-{port}.service"))
        self.assertEqual(quiet_stop(s, path=str(share))[0], 0)
        self.assertEqual((s.units, s.files), ({}, {}))


class FullRangeTests(StateCase):
    """G2 (F9): when every file share port is taken, preview file ends the oldest share that is not pinned with
    --behalten and takes its port; it never ends a pinned share, one in the middle of a create or stop, or a preview
    that is not a file share."""

    def share(self, s, folder, *flags):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = pv.main(["file", str(folder), *flags], sysm=s)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def age(self, created, extra=None):
        """Set `created` of recorded previews ({port: unix time}), and other fields ({port: {field: value}})."""
        for port, when in created.items():
            entry = pv.load_state(port)
            entry.update(created=when, **(extra or {}).get(port, {}))
            pv.save_state(entry)

    def paths(self):
        return {e["port"]: e.get("path") for e in pv.all_states()}

    def test_f9_full_range_ends_the_oldest_share_and_reuses_its_port(self):
        s = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        a, b, c = self.folder("a"), self.folder("b"), self.folder("c")
        with patched(pv, FILE_PORTS=range(18400, 18402)):
            for folder in (b, a):
                self.assertEqual(self.share(s, folder)[0], 0)
            self.age({18400: 200, 18401: 100})  # a, on 18401, is the oldest
            code, out, err = self.share(s, c)
        self.assertEqual((code, out), (0, f"https://{TS_NAME}:18401\n"), err)
        self.assertIn(f"Ended the oldest file share to free its port: 18401 (a/, {a})", err)
        self.assertEqual(self.paths(), {18400: str(b), 18401: str(c)})
        self.assertIn(str(c), s.units["preview-file-18401.service"]["argv"])
        self.assert_one_world(s, 18401)

    def test_f9_a_pinned_oldest_share_stays_and_the_next_oldest_ends(self):
        s = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        a, b, c = self.folder("a"), self.folder("b"), self.folder("c")
        with patched(pv, FILE_PORTS=range(18400, 18402)):
            self.assertEqual(self.share(s, a, "--behalten")[0], 0)
            self.assertEqual(self.share(s, b)[0], 0)
            self.age({18400: 100, 18401: 200})
            code, out, err = self.share(s, c)
        self.assertEqual((code, out), (0, f"https://{TS_NAME}:18401\n"), err)
        self.assertIn(f"18401 (b/, {b})", err)
        self.assertEqual(self.paths(), {18400: str(a), 18401: str(c)})

    def test_f9_all_pinned_refuses_and_names_pinning(self):
        s = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        a, b, c = self.folder("a"), self.folder("b"), self.folder("c")
        with patched(pv, FILE_PORTS=range(18400, 18402)):
            for folder in (a, b):
                self.assertEqual(self.share(s, folder, "--behalten")[0], 0)
            code, out, err = self.share(s, c)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no free port in 18400-18401 for file shares; stop one first (preview list)", err)
        self.assertIn("--behalten", err)
        self.assertEqual(self.paths(), {18400: str(a), 18401: str(b)})

    def test_f9_behalten_on_an_existing_share_keeps_port_and_link(self):
        s = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        a, b = self.folder("a"), self.folder("b")
        code, link, _ = self.share(s, a)
        self.share(s, b)
        starts = len([c for c in s.calls if c[0] == "unit_start"])
        code, out, err = self.share(s, a, "--behalten")
        self.assertEqual((code, out), (0, link), err)
        self.assertEqual(len([c for c in s.calls if c[0] == "unit_start"]), starts)  # nothing restarted
        self.assertIs(pv.load_state(18400).get("behalten"), True)
        self.assertEqual(self.share(s, a)[1], link)  # running it again without the flag keeps the pin
        self.assertIs(pv.load_state(18400).get("behalten"), True)
        out = io.StringIO()
        pv.cmd_list(s, as_json=True, out=out)
        self.assertEqual({r["port"]: r.get("behalten") for r in json.loads(out.getvalue())}, {18400: True, 18401: False})
        out = io.StringIO()
        pv.cmd_list(s, out=out)
        lines = {line.split()[0]: line for line in out.getvalue().splitlines()}
        self.assertTrue(lines["18400"].endswith(f"{a} (behalten)"))
        self.assertTrue(lines["18401"].endswith(str(b)))

    def test_f9_skips_a_share_with_status_and_a_preview_that_is_no_file_share(self):
        s = FakeSystem(operator=True, serve_allowed=True, target_up=False)
        a, b, c = self.folder("a"), self.folder("b"), self.folder("c")
        with patched(pv, FILE_PORTS=range(18400, 18403)):
            self.assertEqual(run_start(s, 18400)[0], 0)  # a dev server's preview in the range
            for folder in (a, b):
                self.assertEqual(self.share(s, folder)[0], 0)
            self.age({18400: 50, 18401: 100, 18402: 200}, {18401: {"status": "stopping"}})  # a stop that did not finish
            code, out, err = self.share(s, c)
        self.assertEqual((code, out), (0, f"https://{TS_NAME}:18402\n"), err)
        self.assertEqual(self.paths(), {18400: None, 18401: str(a), 18402: str(c)})


# ---------------------------------------------------------------------------
# Optional live test against the real systemd --user and network (PREVIEW_LIVE=1)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Optional live test against the real systemd --user and network (PREVIEW_LIVE=1)
# ---------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.environ.get("PREVIEW_LIVE") == "1", "set PREVIEW_LIVE=1 to run against the real system")
class LiveTests(unittest.TestCase):
    def test_real_preview_roundtrip(self):
        port = free_port()
        tool = str(HERE / "preview")
        with tempfile.TemporaryDirectory() as root:
            Path(root, "index.html").write_text("live-preview-ok")
            srv = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                                   cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(0.5)
                a = subprocess.run([tool, str(port), "--name", "live-test"], capture_output=True, text=True, timeout=60)
                b = subprocess.run([tool, str(port)], capture_output=True, text=True, timeout=60)
                self.assertEqual(a.returncode, 0, a.stderr)
                self.assertEqual(a.stdout, b.stdout)
                link = a.stdout.strip()
                ss = subprocess.run(["ss", "-Hltn", f"sport = :{port}"], capture_output=True, text=True).stdout
                addrs = {line.split()[3].rsplit(":", 1)[0] for line in ss.splitlines()}
                self.assertNotIn("0.0.0.0", addrs)
                self.assertNotIn("*", addrs)
                if link.startswith("http://"):
                    self.assertEqual(addrs, {"127.0.0.1", TS_IP})
                listen = pv.load_state(port)["listen"]  # the proxy socket (tailnet IP, or loopback behind Tailscale Serve)
                req = urllib.request.Request(f"http://{listen}/index.html", headers={"Host": f"{TS_NAME}:{port}"})
                self.assertEqual(urllib.request.urlopen(req, timeout=10).read(), b"live-preview-ok")
            finally:
                subprocess.run([tool, "stop", str(port)], capture_output=True, timeout=60)
                srv.terminate()
                srv.wait(5)
        self.assertIsNone(pv.load_state(port))


if __name__ == "__main__":
    unittest.main()
