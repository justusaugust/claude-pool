"""Tests for `preview cua`: one HTTPS link to every Cua desktop, open only to the VPS owner's Tailscale login.

Nothing here runs the real cua, systemctl or tailscale. The command tests use the recording FakeSystem from
test_preview_files; the proxy tests run the real proxy against a small local upstream on 127.0.0.1:0; the viewer
wrapper test starts `preview _cua-viewer` with a fake `cua` script and PATH set to <fake bin>:/usr/bin:/bin.
"""

import asyncio
import contextlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import test_preview_files as tpf

pv = tpf.pv
HERE = Path(__file__).resolve().parent
OWNER = "justus@example.com"
SECRET = "S3cr3tViewerKey_0123456789abcdef"
TS_NAME = tpf.TS_NAME


class CuaSystem(tpf.FakeSystem):
    """The file-share stand-in plus `tailscale status --json`, a cua binary and the viewer wrapper unit."""

    def __init__(self, login=OWNER, cua="/opt/cua/bin/cua", key=SECRET, **kwargs):
        super().__init__(**kwargs)
        self.login, self.cua, self.key = login, cua, key
        self.status_raw = None

    def run(self, argv, timeout=30):
        argv = list(argv)
        if argv == ["tailscale", "status", "--json"]:
            self.commands.append(argv)
            if self.status_raw is not None:
                return pv.subprocess.CompletedProcess(argv, 0, self.status_raw, "")
            users = {"7": {"ID": 7, "LoginName": self.login}} if self.login else {}
            data = {"Self": {"UserID": 7, "DNSName": TS_NAME + "."}, "User": users}
            return pv.subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        if argv and argv[0] == "systemd-run" and "_cua-viewer" in argv:
            self.commands.append(argv)
            unit = next(a.split("=", 1)[1] for a in argv if a.startswith("--unit="))
            cmd = argv[argv.index("/usr/bin/python3"):]
            port = int(cmd[cmd.index("--port") + 1])
            self.active[unit] = ("127.0.0.1", port)
            if self.key is not None:  # what the wrapper does once `cua viewer` printed its URL
                key_file = Path(cmd[cmd.index("--key-file") + 1].replace("$$", "$"))
                key_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                key_file.write_text(self.key + "\n")
                key_file.chmod(0o600)
            return pv.subprocess.CompletedProcess(argv, 0, "", "")
        return super().run(argv, timeout)

    def cua_path(self):
        return self.cua


def everything(fake, out="", err=""):
    """Every command line, output and log text a run produced, for the never-print-the-key checks."""
    return json.dumps(fake.argvs()) + out + err


class CuaCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.state = base / "state"
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home), "PREVIEW_STATE_DIR": str(self.state),
                                             "PREVIEW_BACKEND": ""})
        patch.start()
        self.addCleanup(patch.stop)

    def main(self, argv, sysm):
        return tpf.quiet(pv.main, argv, sysm=sysm)

    def https(self, **kwargs):
        return CuaSystem(operator=True, serve_allowed=True, **kwargs)

    def test_plan_binds_the_viewer_to_loopback_only_and_starts_nothing(self):
        fake = self.https()
        plan = pv.cua_plan(fake)
        self.assertEqual(plan["port"], pv.CUA_PORT)
        self.assertEqual(plan["serve"], ("127.0.0.1", pv.CUA_PORT))
        self.assertEqual(plan["unit"], "preview-cua-viewer.service")
        self.assertEqual(plan["backend"], "https")
        self.assertEqual(plan["owner_login"], OWNER)
        self.assertEqual(plan["label"], "Cua desktops")
        self.assertEqual(plan["viewer_argv"],
                         ["/opt/cua/bin/cua", "viewer", "--listen", f"127.0.0.1:{pv.CUA_PORT}", "--no-open"])
        self.assertEqual(plan["argv"][:4], ["/usr/bin/python3", "-I", pv.self_path(), "_cua-viewer"])
        self.assertIn("127.0.0.1", " ".join(plan["viewer_argv"]))
        self.assertNotIn("0.0.0.0", json.dumps(plan))
        self.assertTrue(plan["key_file"].startswith(str(self.state)))
        self.assertFalse([a for a in fake.argvs() if a[0] in ("systemd-run", "systemctl")])
        self.assertEqual(fake.active, {})
        self.assertNotIn(pv.CUA_PORT, pv.FILE_PORTS)

    def test_cua_starts_the_viewer_and_an_https_proxy_and_never_shows_the_key(self):
        fake = self.https()
        code, out, err = self.main(["cua"], fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), f"https://{TS_NAME}:{pv.CUA_PORT}")
        self.assertEqual(fake.active["preview-cua-viewer.service"], ("127.0.0.1", pv.CUA_PORT))
        proxy_host, _ = fake.active[f"preview-{pv.CUA_PORT}"]
        self.assertEqual(proxy_host, "127.0.0.1")
        self.assertIn(pv.CUA_PORT, fake.serve)
        [proxy_run] = [a for a in fake.argvs() if a[0] == "systemd-run" and "_serve" in a]
        cmd = proxy_run[proxy_run.index("/usr/bin/python3"):]
        self.assertEqual(cmd[cmd.index("--owner-login") + 1], OWNER)
        self.assertEqual(cmd[cmd.index("--upstream-host") + 1], "127.0.0.1")
        self.assertEqual(Path(cmd[cmd.index("--key-file") + 1]), pv.cua_key_file())
        [viewer_run] = [a for a in fake.argvs() if a[0] == "systemd-run" and "_cua-viewer" in a]
        self.assertIn("Restart=on-failure", viewer_run)
        self.assertNotIn(SECRET, everything(fake, out, err))
        self.assertIn("preview stop cua", err)
        state = pv.load_state(pv.CUA_PORT)
        self.assertEqual((state["kind"], state["label"], state["backend"]), ("cua", "Cua desktops", "https"))
        self.assertNotIn(SECRET, json.dumps(state))

    def test_rerun_keeps_the_link_and_starts_nothing_new(self):
        fake = self.https()
        first = self.main(["cua"], fake)
        runs = len([a for a in fake.argvs() if a[0] == "systemd-run"])
        second = self.main(["cua", "--name", "desktops"], fake)
        self.assertEqual((second[0], second[1]), (0, first[1]))
        self.assertEqual(len([a for a in fake.argvs() if a[0] == "systemd-run"]), runs)
        self.assertEqual(pv.load_state(pv.CUA_PORT)["label"], "desktops")

    def test_http_only_backend_is_refused_with_the_doctor_hint(self):
        for fake in (CuaSystem(operator=False), self.https()):
            with self.subTest(operator=fake.operator):
                env = {} if fake.operator is False else {"PREVIEW_BACKEND": "http"}
                with mock.patch.dict(os.environ, env):
                    code, out, err = self.main(["cua"], fake)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("preview doctor", err)
                self.assertFalse([a for a in fake.argvs() if a[0] == "systemd-run"])
                self.assertIsNone(pv.load_state(pv.CUA_PORT))

    def test_serve_refusal_never_falls_back_to_http(self):
        fake = CuaSystem(operator=True, serve_allowed=False)
        code, out, err = self.main(["cua"], fake)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("preview doctor", err)
        self.assertEqual(fake.active, {})  # the viewer that was started for it is stopped again
        self.assertFalse([a for a in fake.argvs() if a[0] == "systemd-run" and "_serve" in a])
        self.assertIsNone(pv.load_state(pv.CUA_PORT))
        self.assertFalse(pv.cua_key_file().exists())

    def test_unknown_or_tagged_owner_is_refused(self):
        for login in (None, "tagged-devices"):
            with self.subTest(login=login):
                fake = self.https(login=login)
                code, _, err = self.main(["cua"], fake)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("preview: "), err)
                self.assertFalse([a for a in fake.argvs() if a[0] == "systemd-run"])
        fake = self.https()
        fake.status_raw = "not json"
        self.assertEqual(self.main(["cua"], fake)[0], 1)

    def test_missing_cua_is_refused(self):
        fake = self.https(cua=None)
        code, _, err = self.main(["cua"], fake)
        self.assertEqual(code, 1)
        self.assertIn("cua --version", err)
        self.assertFalse([a for a in fake.argvs() if a[0] == "systemd-run"])

    def test_viewer_that_never_reports_its_key_is_stopped(self):
        fake = self.https(key=None)
        code, out, err = self.main(["cua"], fake)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("journalctl --user -u preview-cua-viewer.service", err)
        self.assertEqual(fake.active, {})
        self.assertEqual(fake.serve, {})

    def test_another_preview_on_the_cua_port_is_left_alone(self):
        fake = self.https()
        fake.busy = {pv.CUA_PORT}
        self.assertEqual(self.main([str(pv.CUA_PORT)], fake)[0], 0)
        code, _, err = self.main(["cua"], fake)
        self.assertEqual(code, 1)
        self.assertIn(f"preview stop {pv.CUA_PORT}", err)
        self.assertNotIn("preview-cua-viewer.service", fake.active)

    def test_plain_preview_never_replaces_the_cua_share(self):
        fake = self.https()
        self.main(["cua"], fake)
        del fake.active[f"preview-{pv.CUA_PORT}"]  # the proxy died; `preview 18500` must not restart it without the gate
        code, _, err = self.main([str(pv.CUA_PORT)], fake)
        self.assertEqual(code, 1)
        self.assertIn("preview cua", err)
        self.assertEqual(pv.load_state(pv.CUA_PORT)["kind"], "cua")

    def test_dead_proxy_is_restarted_with_the_gate_on_rerun(self):
        fake = self.https()
        self.main(["cua"], fake)
        del fake.active[f"preview-{pv.CUA_PORT}"]
        code, _, err = self.main(["cua"], fake)
        self.assertEqual(code, 0, err)
        runs = [a for a in fake.argvs() if a[0] == "systemd-run" and "_serve" in a]
        self.assertEqual(len(runs), 2)
        self.assertIn("--owner-login", runs[-1])

    def test_list_shows_the_cua_share(self):
        fake = self.https()
        _, out, _ = self.main(["cua"], fake)
        _, table, _ = self.main(["list"], fake)
        self.assertIn("Cua desktops", table)
        self.assertIn(out.strip(), table)
        self.assertIn("viewer active", table)
        _, js, _ = self.main(["list", "--json"], fake)
        [row] = json.loads(js)
        self.assertEqual((row["kind"], row["label"], row["link"], row["viewer"]),
                         ("cua", "Cua desktops", out.strip(), "active"))
        for key in ("port", "label", "link", "backend", "unit", "listen", "target_listening", "proxy"):
            self.assertIn(key, row)
        self.assertNotIn(SECRET, table + js)

    def test_stop_cua_stops_viewer_proxy_and_serve_and_drops_the_key(self):
        fake = self.https()
        self.main(["cua"], fake)
        self.assertTrue(pv.cua_key_file().exists())
        code, out, _ = self.main(["stop", "cua"], fake)
        self.assertEqual(code, 0)
        self.assertIn("Cua desktops", out)
        self.assertIn(["systemctl", "--user", "stop", "preview-cua-viewer.service"], fake.argvs())
        self.assertEqual((fake.active, fake.serve), ({}, {}))
        self.assertIsNone(pv.load_state(pv.CUA_PORT))
        self.assertFalse(pv.cua_key_file().exists())
        code, _, err = self.main(["stop", "cua"], fake)
        self.assertEqual(code, 1)
        self.assertIn("no Cua desktops share", err)

    def test_stop_all_includes_the_cua_share(self):
        fake = self.https()
        self.main(["cua"], fake)
        self.main(["5173"], fake)
        code, out, _ = self.main(["stop", "--all"], fake)
        self.assertEqual(code, 0)
        self.assertIn("Cua desktops", out)
        self.assertEqual((fake.active, fake.serve), ({}, {}))
        self.assertFalse(pv.cua_key_file().exists())

    def test_doctor_and_help_mention_it(self):
        _, out, _ = self.main(["doctor"], self.https())
        self.assertIn("preview cua", out)
        self.assertIn(OWNER, out)
        self.assertEqual([line for line in out.splitlines() if line.startswith("backend")][0].split()[1], "https")
        _, out, _ = self.main(["doctor"], self.https(cua=None))
        self.assertIn("cua is not installed", out)
        help_out = io.StringIO()
        with contextlib.redirect_stdout(help_out), self.assertRaises(SystemExit):
            pv.parse(["--help"])
        for words in ("preview cua [--name LABEL]", "preview stop <port>|<path>|cua", "403", "127.0.0.1:18500"):
            self.assertIn(words, help_out.getvalue())

    def test_serve_refuses_the_gate_on_a_non_loopback_listen(self):
        key = self.home / "k"
        for listen in ("100.101.102.103:41000",):
            code, _, err = tpf.quiet(pv.main, ["_serve", "--listen", listen, "--target", str(pv.CUA_PORT),
                                               "--owner-login", OWNER, "--key-file", str(key),
                                               "--upstream-host", "127.0.0.1"])
            self.assertEqual(code, pv.EXIT_CONFIG, err)
        code, _, _ = tpf.quiet(pv.main, ["_serve", "--listen", "127.0.0.1:41000", "--target", str(pv.CUA_PORT),
                                         "--owner-login", OWNER])  # a gate without a key file is half a gate
        self.assertEqual(code, pv.EXIT_CONFIG)


# ---------------------------------------------------------------------------
# The gated proxy against a real local upstream
# ---------------------------------------------------------------------------

class Viewer:
    """Behaves like `cua viewer` 0.4.1 seen on the VPS: a request with ?key=K (right key) answers 303 to `/` and sets
    the HttpOnly cookie cua_viewer_key=K; later requests authenticate by that cookie; anything else is 401. /ws upgrades
    and echoes when authenticated. /also-cookie answers 200 and sets the key cookie plus an unrelated cookie."""

    def __init__(self):
        self.heads = []

    async def handle(self, r, w):
        try:
            while True:
                head = await pv.read_head(r)
                if head is None:
                    break
                start, headers = pv.split_head(head)
                hdrs = dict((n.lower(), v) for n, v in headers)
                self.heads.append((start, hdrs))
                target = start.split(b" ")[1]
                path, _, query = target.partition(b"?")
                cookies = [c.strip() for v in [v for n, v in headers if n.lower() == b"cookie"] for c in v.split(b";")]
                authed = b"cua_viewer_key=" + SECRET.encode() in cookies
                set_key = b"Set-Cookie: cua_viewer_key=" + SECRET.encode() + b"; HttpOnly; SameSite=Strict; Path=/\r\n"
                if b"key=" + SECRET.encode() in query.split(b"&"):
                    w.write(b"HTTP/1.1 303 See Other\r\nLocation: /\r\n" + set_key + b"Content-Length: 0\r\n\r\n")
                elif not authed:
                    w.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 6\r\n\r\nno key")
                elif path == b"/ws/desktop":
                    w.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
                    await w.drain()
                    while data := await r.read(1024):
                        w.write(b"echo:" + data)
                        await w.drain()
                    break
                elif path == b"/also-cookie":
                    w.write(b"HTTP/1.1 200 OK\r\n" + set_key + b"set-cookie: theme=dark; Path=/\r\n"
                            b"Set-Cookie:  CUA_VIEWER_KEY=" + SECRET.encode() + b"\r\nContent-Length: 2\r\n\r\nok")
                else:
                    w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()


def cookie_values(hdrs, name=b"cua_viewer_key"):
    return [c.strip().split(b"=", 1)[1] for c in hdrs.get(b"cookie", b"").split(b";") if c.strip().split(b"=")[0] == name]


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_file = Path(self.tmp.name) / "cua" / "viewer.key"
        self.key_file.parent.mkdir(mode=0o700)
        self.write_key(SECRET)
        self.viewer = Viewer()
        self.up = await asyncio.start_server(self.viewer.handle, "127.0.0.1", 0)
        self.port = self.up.sockets[0].getsockname()[1]
        self.public = f"https://{TS_NAME}:{self.port}"
        proxy = pv.Proxy(self.port, self.public, target_hosts=("127.0.0.1",), upstream_host="127.0.0.1",
                         key_file=str(self.key_file), owner_login=OWNER)
        self.proxy = await pv.start_proxy_server("127.0.0.1", 0, proxy)
        self.pport = self.proxy.sockets[0].getsockname()[1]
        self.host = f"{TS_NAME}:{self.port}".encode()

    async def asyncTearDown(self):
        for s in (self.proxy, self.up):
            s.close()
            await s.wait_closed()
        self.tmp.cleanup()

    def write_key(self, key, mode=0o600):
        tmp = self.key_file.with_name("tmp")
        tmp.write_text(key + "\n")
        tmp.chmod(mode)
        tmp.replace(self.key_file)

    async def ask(self, target=b"/x", login=OWNER, extra=b""):
        r, w = await asyncio.open_connection("127.0.0.1", self.pport)
        logins = [login] if isinstance(login, str) or login is None else login
        lines = b"".join(b"Tailscale-User-Login: " + x.encode() + b"\r\n" for x in logins if x is not None)
        try:
            w.write(b"GET " + target + b" HTTP/1.1\r\nHost: " + self.host + b"\r\n" + lines + extra + b"\r\n")
            await w.drain()
            head = await asyncio.wait_for(pv.read_head(r), 5)
            start, headers = pv.split_head(head)
            kind, n = pv.body_length(headers)
            body = await asyncio.wait_for(r.readexactly(n), 5) if kind == "length" else b""
        finally:
            w.close()  # also when an assertion fails, so the servers can close in tearDown
        return start.split(b" ")[1], headers, body

    def no_key_in(self, headers, body=b""):
        self.assertNotIn(SECRET.encode(), b"".join(n + b":" + v for n, v in headers) + body)

    async def test_owner_gets_through_with_the_key_as_the_viewer_cookie(self):
        code, headers, body = await self.ask(b"/api/spaces?x=1")
        self.assertEqual((code, body), (b"200", b"ok"))
        [(start, hdrs)] = self.viewer.heads
        self.assertEqual(start, b"GET /api/spaces?x=1 HTTP/1.1")
        self.assertEqual(cookie_values(hdrs), [SECRET.encode()])
        self.assertEqual(hdrs[b"host"], f"127.0.0.1:{self.port}".encode())
        self.no_key_in(headers, body)

    async def test_page_load_is_not_redirected_and_never_loops(self):
        for extra in (b"Sec-Fetch-Mode: navigate\r\nAccept: text/html\r\n", b""):
            code, headers, body = await self.ask(b"/", extra=extra)
            self.assertEqual((code, body), (b"200", b"ok"))
            self.no_key_in(headers, body)
        for start, _ in self.viewer.heads:
            self.assertEqual(start, b"GET / HTTP/1.1")

    async def test_a_key_or_cookie_sent_by_the_browser_is_replaced(self):
        await self.ask(b"/a?key=guess&KEY=x&k%65y=y&b=2",
                       extra=b"Cookie: cua_viewer_key=guess; theme=dark\r\nCookie: cua_viewer_key=again\r\n")
        await self.ask(b"/?key=" + SECRET.encode())  # even the right key in the URL never reaches the viewer as a query
        (first, hdrs), (second, _) = self.viewer.heads
        self.assertEqual(first, b"GET /a?KEY=x&b=2 HTTP/1.1")
        self.assertEqual(second, b"GET / HTTP/1.1")
        self.assertEqual(cookie_values(hdrs), [SECRET.encode()])
        self.assertEqual(cookie_values(hdrs, b"theme"), [b"dark"])
        self.assertEqual(len([n for n in hdrs if n == b"cookie"]), 1)

    async def test_the_viewer_cookie_never_reaches_the_browser(self):
        code, headers, body = await self.ask(b"/also-cookie")
        self.assertEqual(code, b"200")
        cookies = [v for n, v in headers if n.lower() == b"set-cookie"]
        self.assertEqual(cookies, [b"theme=dark; Path=/"])
        self.no_key_in(headers, body)

    async def test_missing_other_or_doubled_login_gets_403_and_never_reaches_the_viewer(self):
        for login in (None, "mallory@example.com", "", OWNER.upper(), [OWNER, "mallory@example.com"], [OWNER, OWNER]):
            with self.subTest(login=login):
                code, headers, body = await self.ask(b"/", login=login, extra=b"Sec-Fetch-Mode: navigate\r\n")
                self.assertEqual(code, b"403")
                self.no_key_in(headers, body)
                self.assertNotIn(OWNER.encode(), body)
        self.assertEqual(self.viewer.heads, [])

    async def test_websocket_carries_the_cookie_and_passes_through(self):
        r, w = await asyncio.open_connection("127.0.0.1", self.pport)
        w.write(b"GET /ws/desktop?key=x HTTP/1.1\r\nHost: " + self.host + b"\r\nTailscale-User-Login: " + OWNER.encode() +
                b"\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nOrigin: " + self.public.encode() +
                b"\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
        try:
            await w.drain()
            head = await asyncio.wait_for(pv.read_head(r), 5)
            self.assertTrue(head.startswith(b"HTTP/1.1 101"), head)
            w.write(b"frame")
            await w.drain()
            self.assertEqual(await asyncio.wait_for(r.readexactly(10), 5), b"echo:frame")
        finally:
            w.close()
        start, hdrs = self.viewer.heads[0]
        self.assertEqual(start, b"GET /ws/desktop HTTP/1.1")
        self.assertEqual(cookie_values(hdrs), [SECRET.encode()])
        self.assertEqual(hdrs[b"origin"], f"http://127.0.0.1:{self.port}".encode())

    async def test_a_foreign_login_later_on_a_keepalive_connection_is_refused(self):
        r, w = await asyncio.open_connection("127.0.0.1", self.pport)
        try:
            w.write(b"GET /a HTTP/1.1\r\nHost: " + self.host + b"\r\nTailscale-User-Login: " + OWNER.encode() + b"\r\n\r\n")
            await w.drain()
            head = await asyncio.wait_for(pv.read_head(r), 5)
            self.assertIn(b"200", head)
            await asyncio.wait_for(r.readexactly(2), 5)
            w.write(b"GET /b HTTP/1.1\r\nHost: " + self.host + b"\r\nTailscale-User-Login: mallory@example.com\r\n\r\n")
            await w.drain()
            head = await asyncio.wait_for(pv.read_head(r), 5)
            self.assertIn(b"403", head)
        finally:
            w.close()
        self.assertEqual([s.split(b" ")[1] for s, _ in self.viewer.heads], [b"/a"])

    async def test_a_new_key_is_used_after_the_viewer_restarts(self):
        await self.ask(b"/a")
        self.write_key("N3wKeyAfterRestart_abcdef0123456789")
        await self.ask(b"/b")
        self.assertEqual(cookie_values(self.viewer.heads[1][1]), [b"N3wKeyAfterRestart_abcdef0123456789"])

    async def test_missing_or_loose_key_file_waits_and_forwards_nothing(self):
        for setup in ("missing", "loose", "symlink"):
            with self.subTest(setup=setup):
                if self.key_file.exists() or self.key_file.is_symlink():
                    self.key_file.unlink()
                if setup == "loose":
                    self.write_key(SECRET, mode=0o644)
                if setup == "symlink":
                    real = Path(self.tmp.name) / "elsewhere"
                    real.write_text(SECRET)
                    real.chmod(0o600)
                    self.key_file.symlink_to(real)
                code, _, body = await self.ask(b"/", extra=b"Sec-Fetch-Mode: navigate\r\n")
                self.assertEqual(code, b"503")
                self.assertIn(b"refresh", body)
                self.assertNotIn(SECRET.encode(), body)
        self.assertEqual(self.viewer.heads, [])

    async def test_ordinary_previews_ignore_the_tailscale_login(self):
        proxy = pv.Proxy(self.port, self.public)
        server = await pv.start_proxy_server("127.0.0.1", 0, proxy)
        try:
            r, w = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            w.write(b"GET /plain HTTP/1.1\r\nHost: " + self.host + b"\r\nCookie: a=1\r\n\r\n")
            await w.drain()
            head = await asyncio.wait_for(pv.read_head(r), 5)
            self.assertIn(b"401", head)  # no gate and no key: the viewer itself refuses
            w.close()
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(self.viewer.heads[0][0], b"GET /plain HTTP/1.1")
        self.assertEqual(self.viewer.heads[0][1][b"host"], f"localhost:{self.port}".encode())
        self.assertEqual(self.viewer.heads[0][1][b"cookie"], b"a=1")


# ---------------------------------------------------------------------------
# The viewer wrapper: runs `cua viewer`, keeps its key in a 0600 file, never prints it
# ---------------------------------------------------------------------------

FAKE_CUA = """#!/bin/sh
printf '%s\\n' "$@" > "$CUA_ARGS"
echo "Starting viewer"
echo "Viewer server: http://127.0.0.1:18500/?key=SECRETVALUE"
echo "warning: open http://127.0.0.1:18500/?key=SECRETVALUE&x=1 to view" >&2
trap 'exit 0' TERM
while :; do sleep 0.1; done
"""


class ViewerWrapperTests(unittest.TestCase):
    def test_wrapper_writes_the_key_0600_and_logs_it_never(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            cua = fake_bin / "cua"
            cua.write_text(FAKE_CUA.replace("SECRETVALUE", SECRET))
            cua.chmod(0o755)
            key_file = tmp / "state" / "cua" / "viewer.key"
            env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp), "CUA_ARGS": str(tmp / "args")}
            proc = subprocess.Popen([sys.executable, "-I", str(HERE / "preview"), "_cua-viewer", "--cua", str(cua),
                                     "--port", str(pv.CUA_PORT), "--key-file", str(key_file)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            try:
                deadline = time.monotonic() + 10
                while not key_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(key_file.read_text().strip(), SECRET)
                self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(key_file.parent.stat().st_mode), 0o700)
                time.sleep(0.3)
            finally:
                proc.send_signal(signal.SIGTERM)
                out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual((tmp / "args").read_text().split(), ["viewer", "--listen", "127.0.0.1:18500", "--no-open"])
            self.assertNotIn(SECRET.encode(), out + err)
            self.assertIn(b"key=<hidden>", out)
            self.assertIn(b"Starting viewer", out)
            self.assertFalse(key_file.exists())  # a stopped viewer's key is useless; it is removed

    def test_wrapper_refuses_a_relative_cua_or_a_foreign_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (["--cua", "cua", "--port", "18500"], ["--cua", "/bin/sh", "--port", "18499"]):
                code, _, _ = tpf.quiet(pv.main, ["_cua-viewer", *argv, "--key-file", str(Path(tmp) / "k")])
                self.assertEqual(code, pv.EXIT_CONFIG, argv)


if __name__ == "__main__":
    unittest.main()
