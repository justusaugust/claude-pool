"""Tests for ~/.local/bin/preview.

Run:  cd ~/.local/bin && python3 -m unittest preview_test -v
Live system test (starts a real systemd --user unit on a free port, then removes it):
      PREVIEW_LIVE=1 python3 -m unittest preview_test -v
"""

import asyncio
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
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


class FakeSystem(pv.System):
    def __init__(self, operator=False, serve_allowed=False, target_up=True, wildcard=False):
        super().__init__()
        self.operator = operator
        self.serve_allowed = serve_allowed
        self.target_up = target_up
        self.wildcard = wildcard
        self.units = {}
        self.serve = {8443: "http://127.0.0.1:3773"}  # someone else's entry
        self.calls = []
        self.next_free = 40000

    def tailnet(self):
        return TS_IP, TS_NAME

    def operator_ok(self):
        return self.operator

    def serve_ports(self):
        return set(self.serve)

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
        return "active" if unit in self.units else "inactive"

    def unit_start(self, unit, desc, argv):
        self.calls.append(("unit_start", unit))
        listen = argv[argv.index("--listen") + 1]
        pv.check_listen_host(pv.parse_hostport(listen)[0])
        self.units[unit] = {"argv": argv, "listen": listen, "desc": desc}
        return True, ""

    def unit_stop(self, unit):
        self.calls.append(("unit_stop", unit))
        self.units.pop(unit, None)

    def unit_log(self, unit, lines=5):
        return ""

    def listeners(self, port):
        out = [(pv.parse_hostport(u["listen"])[0], "python3") for u in self.units.values()
               if pv.parse_hostport(u["listen"])[1] == port]
        if self.target_up:
            out.append(("0.0.0.0" if self.wildcard else "127.0.0.1", "node"))
        return out

    def can_connect(self, host, port, timeout=0.5):
        if any(pv.parse_hostport(u["listen"]) == (host, port) for u in self.units.values()):
            return True
        return self.target_up and host in ("127.0.0.1", "::1")

    def free_local_port(self):
        self.next_free += 1
        return self.next_free

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
            pv.cmd_stop(s, all_=True)
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
