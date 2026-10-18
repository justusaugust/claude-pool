"""Tests for `preview cua`: one phone link that lists every Cua desktop (Space) and opens each one, owner only.

Nothing here runs the real cua, systemctl or tailscale. The command tests use the recording FakeSystem from
test_preview_files plus `tailscale status --json`, `cua spaces ls --json` and the index unit; the proxy tests run the
real proxy against a small upstream on 127.0.0.1:0; the index tests call the index answer with a fake cua runner and
also run the index handler on a real ThreadingHTTPServer on 127.0.0.1:0.
"""

import asyncio
import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import test_preview_files as tpf

pv = tpf.pv
OWNER = "owner@example.com"
TS_NAME = tpf.TS_NAME
CUA = "/opt/cua/bin/cua"
TICKET = "T1ck3tSecretValue_0123456789abcdef"
VPS_DESKTOP = {"id": "direct:127.0.0.1:32191", "name": "Personal VPS Desktop", "kind": "container", "os": "linux"}
MAC = {"id": "direct:100.64.0.10:3211", "name": "Laptop", "kind": "host", "os": "macos"}


def spaces_json(*spaces):
    return json.dumps({"spaces": list(spaces)})


class CuaSystem(tpf.FakeSystem):
    """The file-share stand-in plus `tailscale status --json`, `cua spaces ls --json` and the index unit."""

    def __init__(self, login=OWNER, cua=CUA, spaces=(VPS_DESKTOP, MAC), **kwargs):
        super().__init__(**kwargs)
        self.login, self.cua = login, cua
        self.spaces_out = spaces_json(*spaces) if spaces is not None else None
        self.status_raw = None
        self.index_listens = True

    def run(self, argv, timeout=30):
        argv = list(argv)
        done = lambda out="", code=0: pv.subprocess.CompletedProcess(argv, code, out, "")
        if argv == ["tailscale", "status", "--json"]:
            self.commands.append(argv)
            if self.status_raw is not None:
                return done(self.status_raw)
            users = {"7": {"ID": 7, "LoginName": self.login}} if self.login else {}
            return done(json.dumps({"Self": {"UserID": 7, "DNSName": TS_NAME + "."}, "User": users}))
        if argv[:1] == [CUA]:
            self.commands.append(argv)
            if argv[1:] == ["spaces", "ls", "--json"]:
                return done(self.spaces_out or "", 0 if self.spaces_out is not None else 1)
            return done("", 2)
        return super().run(argv, timeout)

    def listen_for(self, unit, cmd):
        if "_cua-index" in cmd and not self.index_listens:
            return None
        return super().listen_for(unit, cmd)

    def cua_path(self):
        return self.cua


def opt(cmd, name):
    return cmd[cmd.index(name) + 1]


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

    def space_ports(self):
        return {e["space_id"]: e["port"] for e in pv.all_states() if e.get("kind") == "cua-space"}

    def test_plan_puts_the_index_on_loopback_and_gives_each_space_a_stable_port(self):
        fake = self.https(spaces=(VPS_DESKTOP, MAC, {"id": "direct:8.8.8.8:3211", "name": "public"},
                                  {"id": "cloud:abc", "name": "cloud box"}))
        plan = pv.cua_plan(fake)
        self.assertEqual((plan["port"], plan["serve"], plan["unit"]), (18500, ("127.0.0.1", 18500), "preview-cua-index.service"))
        self.assertEqual((plan["backend"], plan["owner_login"], plan["label"]), ("https", OWNER, "Cua desktops"))
        self.assertEqual(plan["argv"][:4], ["/usr/bin/python3", "-I", pv.self_path(), "_cua-index"])
        self.assertEqual(opt(plan["argv"], "--cua"), CUA)
        self.assertEqual(opt(plan["argv"], "--public-host"), TS_NAME)
        self.assertEqual([(s["id"], s["name"], s["host"], s["upstream_port"], s["port"]) for s in plan["spaces"]],
                         [(VPS_DESKTOP["id"], "Personal VPS Desktop", "127.0.0.1", 32191, 18501),
                          (MAC["id"], "Laptop", "100.64.0.10", 3211, 18502)])
        self.assertEqual(plan["skipped"], ["direct:8.8.8.8:3211"])
        self.assertFalse([a for a in fake.argvs() if a[0] == "systemctl"])
        self.assertEqual(fake.started, [])
        self.assertEqual(fake.active, {})

    def test_cua_starts_the_index_and_one_gated_https_proxy_per_space(self):
        fake = self.https()
        code, out, err = self.main(["cua"], fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), f"https://{TS_NAME}:18500")
        self.assertEqual(fake.active["preview-cua-index.service"], ("127.0.0.1", 18500))
        self.assertEqual(set(fake.serve), {18500, 18501, 18502})
        for port in (18500, 18501, 18502):
            self.assertEqual(fake.active[f"preview-{port}"][0], "127.0.0.1")
        proxies = {int(opt(c, "--target")): c for c in fake.runs("_serve")}
        self.assertEqual(set(proxies), {18500, 18501, 18502})
        for port, cmd in proxies.items():
            self.assertEqual(opt(cmd, "--owner-login"), OWNER)
            self.assertEqual(opt(cmd, "--public-url"), f"https://{TS_NAME}:{port}")
        self.assertEqual((opt(proxies[18500], "--upstream-host"), opt(proxies[18500], "--upstream-port")), ("127.0.0.1", "18500"))
        self.assertEqual((opt(proxies[18501], "--upstream-host"), opt(proxies[18501], "--upstream-port")), ("127.0.0.1", "32191"))
        self.assertEqual((opt(proxies[18502], "--upstream-host"), opt(proxies[18502], "--upstream-port")), ("100.64.0.10", "3211"))
        self.assertEqual(self.space_ports(), {VPS_DESKTOP["id"]: 18501, MAC["id"]: 18502})
        self.assertIn("Personal VPS Desktop", err)
        self.assertIn("Laptop", err)
        self.assertIn("preview stop cua", err)
        self.assertFalse([a for a in fake.argvs() if "sandbox" in a])  # tickets are minted only when a desktop is tapped
        index = pv.load_state(18500)
        self.assertEqual((index["kind"], index["label"], index["backend"]), ("cua", "Cua desktops", "https"))

    def test_rerun_keeps_ports_adds_new_spaces_and_drops_gone_ones(self):
        fake = self.https()
        self.main(["cua"], fake)
        runs = len(fake.started)
        code, out, _ = self.main(["cua"], fake)
        self.assertEqual((code, out.strip()), (0, f"https://{TS_NAME}:18500"))
        self.assertEqual(len(fake.started), runs)
        other = {"id": "direct:127.0.0.1:32300", "name": "Tunnel box"}
        fake.spaces_out = spaces_json(MAC, other)
        self.assertEqual(self.main(["cua"], fake)[0], 0)
        self.assertEqual(self.space_ports(), {MAC["id"]: 18502, other["id"]: 18501})
        self.assertNotIn(VPS_DESKTOP["id"], json.dumps(pv.all_states()))
        self.assertEqual(set(fake.serve), {18500, 18501, 18502})

    def test_http_only_backend_is_refused_with_the_doctor_hint(self):
        for fake in (CuaSystem(operator=False), self.https()):
            with self.subTest(operator=fake.operator):
                env = {} if fake.operator is False else {"PREVIEW_BACKEND": "http"}
                with mock.patch.dict(os.environ, env):
                    code, out, err = self.main(["cua"], fake)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("preview doctor", err)
                self.assertEqual(fake.started, [])
                self.assertEqual(pv.all_states(), [])

    def test_serve_refusal_never_falls_back_to_http(self):
        fake = CuaSystem(operator=True, serve_allowed=False)
        code, out, err = self.main(["cua"], fake)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("preview doctor", err)
        self.assertEqual(fake.active, {})
        self.assertEqual(fake.runs("_serve"), [])
        self.assertEqual(pv.all_states(), [])

    def test_unknown_or_tagged_owner_missing_cua_or_unreadable_spaces_are_refused(self):
        for kwargs in ({"login": None}, {"login": "tagged-devices"}, {"cua": None}, {"spaces": None}):
            with self.subTest(**kwargs):
                fake = self.https(**kwargs)
                code, out, err = self.main(["cua"], fake)
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("preview: "), err)
                self.assertEqual(fake.started, [])
        self.assertIn("cua --version", self.main(["cua"], self.https(cua=None))[2])
        fake = self.https()
        fake.status_raw = "not json"
        self.assertEqual(self.main(["cua"], fake)[0], 1)

    def test_index_that_never_listens_is_stopped(self):
        fake = self.https()
        fake.index_listens = False
        code, out, err = self.main(["cua"], fake)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("journalctl --user -u preview-cua-index.service", err)
        self.assertEqual((fake.active, fake.serve), ({}, {}))

    def test_other_previews_on_the_cua_ports_are_left_alone(self):
        fake = self.https()
        fake.busy = {18500}
        self.assertEqual(self.main(["18500"], fake)[0], 0)
        code, _, err = self.main(["cua"], fake)
        self.assertEqual(code, 1)
        self.assertIn("preview stop 18500", err)
        self.assertNotIn("preview-cua-index.service", fake.active)
        fake = self.https()
        self.main(["stop", "--all"], fake)
        self.assertEqual(self.main(["18501"], fake)[0], 0)
        self.main(["cua"], fake)
        self.assertEqual(self.space_ports(), {VPS_DESKTOP["id"]: 18502, MAC["id"]: 18503})

    def test_plain_preview_never_replaces_a_cua_share(self):
        fake = self.https()
        self.main(["cua"], fake)
        for port in (18500, 18501):
            del fake.active[f"preview-{port}"]
            code, _, err = self.main([str(port)], fake)
            self.assertEqual(code, 1, port)
            self.assertIn("preview cua", err)

    def test_dead_proxy_is_restarted_with_the_gate_on_rerun(self):
        fake = self.https()
        self.main(["cua"], fake)
        del fake.active["preview-18502"]
        code, _, err = self.main(["cua"], fake)
        self.assertEqual(code, 0, err)
        mac = [c for c in fake.runs("_serve") if opt(c, "--target") == "18502"]
        self.assertEqual(len(mac), 2)
        self.assertEqual(opt(mac[-1], "--owner-login"), OWNER)

    def test_list_shows_the_index_and_each_space(self):
        fake = self.https()
        _, out, _ = self.main(["cua"], fake)
        _, table, _ = self.main(["list"], fake)
        for words in ("Cua desktops", out.strip(), "index active", "Personal VPS Desktop", "Laptop", MAC["id"]):
            self.assertIn(words, table)
        _, js, _ = self.main(["list", "--json"], fake)
        rows = {r["port"]: r for r in json.loads(js)}
        self.assertEqual((rows[18500]["kind"], rows[18500]["index"]), ("cua", "active"))
        self.assertEqual((rows[18502]["kind"], rows[18502]["space_id"], rows[18502]["label"], rows[18502]["upstream"]),
                         ("cua-space", MAC["id"], "Laptop", "100.64.0.10:3211"))
        for row in rows.values():
            for key in ("port", "label", "link", "backend", "unit", "listen", "target_listening", "proxy"):
                self.assertIn(key, row)

    def test_stop_cua_and_stop_all_remove_the_index_and_every_space(self):
        for argv in (["stop", "cua"], ["stop", "--all"]):
            with self.subTest(argv=argv):
                fake = self.https()
                self.main(["cua"], fake)
                self.main(["5173"], fake)
                code, out, _ = self.main(argv, fake)
                self.assertEqual(code, 0)
                self.assertIn("Cua desktops", out)
                self.assertIn("Laptop", out)
                self.assertIn(["systemctl", "--user", "stop", "preview-cua-index.service"], fake.argvs())
                if argv[1] == "cua":
                    self.assertEqual(set(fake.serve), {5173})
                    self.assertEqual([e["port"] for e in pv.all_states()], [5173])
                    self.main(["stop", "--all"], fake)
                self.assertEqual((fake.active, fake.serve, pv.all_states()), ({}, {}, []))
        code, _, err = self.main(["stop", "cua"], self.https())
        self.assertEqual(code, 1)
        self.assertIn("no Cua desktops share", err)

    def test_doctor_and_help_mention_it(self):
        _, out, _ = self.main(["doctor"], self.https())
        self.assertIn("preview cua", out)
        self.assertIn(OWNER, out)
        self.assertEqual([line for line in out.splitlines() if line.startswith("backend")][0].split()[1], "https")
        self.assertIn("cua is not installed", self.main(["doctor"], self.https(cua=None))[1])
        help_out = io.StringIO()
        with contextlib.redirect_stdout(help_out), self.assertRaises(SystemExit):
            pv.parse(["--help"])
        for words in ("preview cua [--name LABEL]", "preview stop <port>|<path>|cua", "403", "18501", "audio"):
            self.assertIn(words, help_out.getvalue())

    def test_serve_refuses_a_gate_off_loopback_or_an_upstream_outside_the_tailnet(self):
        base = ["_serve", "--target", "18501", "--owner-login", OWNER, "--upstream-port", "3211"]
        for argv in (base + ["--listen", "100.101.102.103:41000", "--upstream-host", "127.0.0.1"],
                     base + ["--listen", "127.0.0.1:41000", "--upstream-host", "8.8.8.8"],
                     base + ["--listen", "127.0.0.1:41000", "--upstream-host", "0.0.0.0"],
                     ["_serve", "--target", "18501", "--listen", "127.0.0.1:41000", "--upstream-host", "127.0.0.1",
                      "--upstream-port", "3211"],
                     ["_serve", "--target", "18501", "--listen", "127.0.0.1:41000", "--owner-login", OWNER,
                      "--upstream-host", "127.0.0.1"]):
            with self.subTest(argv=argv):
                code, _, _ = tpf.quiet(pv.main, argv)
                self.assertEqual(code, pv.EXIT_CONFIG)


# ---------------------------------------------------------------------------
# The gated proxy in front of one Space's viewer
# ---------------------------------------------------------------------------

class Spacesd:
    """Stands in for a Space's cua-spacesd: records request lines and headers; /viewer/ws upgrades and echoes."""

    def __init__(self):
        self.heads = []

    async def handle(self, r, w):
        try:
            while True:
                head = await pv.read_head(r)
                if head is None:
                    break
                start, headers = pv.split_head(head)
                self.heads.append((start, dict((n.lower(), v) for n, v in headers)))
                if start.split(b" ")[1] == b"/viewer/ws":
                    w.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
                    await w.drain()
                    while data := await r.read(1024):
                        w.write(b"echo:" + data)
                        await w.drain()
                    break
                w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()


class GateTests(unittest.IsolatedAsyncioTestCase):
    SHARE = 18501

    async def asyncSetUp(self):
        self.spacesd = Spacesd()
        self.up = await asyncio.start_server(self.spacesd.handle, "127.0.0.1", 0)
        self.uport = self.up.sockets[0].getsockname()[1]
        self.public = f"https://{TS_NAME}:{self.SHARE}"
        proxy = pv.Proxy(self.SHARE, self.public, upstream_host="127.0.0.1", upstream_port=self.uport, owner_login=OWNER)
        self.proxy = await pv.start_proxy_server("127.0.0.1", 0, proxy)
        self.pport = self.proxy.sockets[0].getsockname()[1]
        self.host = f"{TS_NAME}:{self.SHARE}".encode()

    async def asyncTearDown(self):
        for s in (self.proxy, self.up):
            s.close()
            await s.wait_closed()

    async def ask(self, target=b"/viewer/", login=OWNER, extra=b"", port=None):
        r, w = await asyncio.open_connection("127.0.0.1", port or self.pport)
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
            w.close()
        return start.split(b" ")[1], headers, body

    async def test_owner_reaches_the_space_with_its_own_host(self):
        code, _, body = await self.ask(b"/viewer/viewer.js", extra=b"Origin: " + self.public.encode() + b"\r\n")
        self.assertEqual((code, body), (b"200", b"ok"))
        [(start, hdrs)] = self.spacesd.heads
        self.assertEqual(start, b"GET /viewer/viewer.js HTTP/1.1")
        self.assertEqual(hdrs[b"host"], f"127.0.0.1:{self.uport}".encode())
        self.assertEqual(hdrs[b"origin"], f"http://127.0.0.1:{self.uport}".encode())

    async def test_missing_other_or_doubled_login_gets_403_and_never_reaches_the_space(self):
        for login in (None, "mallory@example.com", "", OWNER.upper(), [OWNER, "mallory@example.com"], [OWNER, OWNER]):
            with self.subTest(login=login):
                code, _, body = await self.ask(login=login)
                self.assertEqual(code, b"403")
                self.assertNotIn(OWNER.encode(), body)
        self.assertEqual(self.spacesd.heads, [])

    async def test_websocket_passes_through(self):
        r, w = await asyncio.open_connection("127.0.0.1", self.pport)
        try:
            w.write(b"GET /viewer/ws HTTP/1.1\r\nHost: " + self.host + b"\r\nTailscale-User-Login: " + OWNER.encode() +
                    b"\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nOrigin: " + self.public.encode() +
                    b"\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
            await w.drain()
            head = await asyncio.wait_for(pv.read_head(r), 5)
            self.assertTrue(head.startswith(b"HTTP/1.1 101"), head)
            w.write(b"frame")
            await w.drain()
            self.assertEqual(await asyncio.wait_for(r.readexactly(10), 5), b"echo:frame")
        finally:
            w.close()
        self.assertEqual(self.spacesd.heads[0][1][b"host"], f"127.0.0.1:{self.uport}".encode())

    async def test_a_foreign_login_later_on_a_keepalive_connection_is_refused(self):
        r, w = await asyncio.open_connection("127.0.0.1", self.pport)
        try:
            w.write(b"GET /a HTTP/1.1\r\nHost: " + self.host + b"\r\nTailscale-User-Login: " + OWNER.encode() + b"\r\n\r\n")
            await w.drain()
            self.assertIn(b"200", await asyncio.wait_for(pv.read_head(r), 5))
            await asyncio.wait_for(r.readexactly(2), 5)
            w.write(b"GET /b HTTP/1.1\r\nHost: " + self.host + b"\r\nTailscale-User-Login: mallory@example.com\r\n\r\n")
            await w.drain()
            self.assertIn(b"403", await asyncio.wait_for(pv.read_head(r), 5))
        finally:
            w.close()
        self.assertEqual([s.split(b" ")[1] for s, _ in self.spacesd.heads], [b"/a"])

    async def test_a_redirect_to_the_space_address_becomes_the_share_link(self):
        upstream = f"http://127.0.0.1:{self.uport}/viewer/".encode()
        headers = pv.rewrite_response([(b"Location", upstream)], self.uport, self.public.encode(),
                                      f"127.0.0.1:{self.uport}".encode())
        self.assertEqual(headers, [(b"Location", self.public.encode() + b"/viewer/")])

    async def test_a_space_that_is_down_gets_a_page_and_no_hang(self):
        self.up.close()
        await self.up.wait_closed()
        code, _, body = await self.ask()
        self.assertEqual(code, b"502")
        self.assertIn(f"127.0.0.1:{self.uport}".encode(), body)

    async def test_ordinary_previews_ignore_the_tailscale_login(self):
        proxy = pv.Proxy(self.uport, f"https://{TS_NAME}:{self.uport}")
        server = await pv.start_proxy_server("127.0.0.1", 0, proxy)
        try:
            self.host = f"{TS_NAME}:{self.uport}".encode()
            code, _, _ = await self.ask(login=None, port=server.sockets[0].getsockname()[1])
            self.assertEqual(code, b"200")
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(self.spacesd.heads[0][1][b"host"], f"localhost:{self.uport}".encode())


# ---------------------------------------------------------------------------
# The index on 18500: lists the Spaces live, mints a viewer link when one is tapped
# ---------------------------------------------------------------------------

class FakeCua:
    def __init__(self, spaces=(VPS_DESKTOP, MAC), view=None, view_code=0):
        self.spaces, self.calls, self.view_code = spaces, [], view_code
        self.view = view if view is not None else json.dumps({
            "url": f"http://127.0.0.1:32191/viewer/#ticket={TICKET}&files=%2Fhome%2Fcua", "expires_at_unix": 1792338000})

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[1:] == ["spaces", "ls", "--json"]:
            return (0, spaces_json(*self.spaces)) if self.spaces is not None else (1, "")
        if argv[1:3] == ["sandbox", "view"]:
            return self.view_code, self.view
        return 2, ""


SHARES = {VPS_DESKTOP["id"]: 18501, MAC["id"]: 18502}


def answer(path, cua=None, shares=SHARES):
    cua = cua or FakeCua()
    return (*pv.cua_index_answer(path, cua, CUA, TS_NAME, shares), cua)


class IndexTests(unittest.TestCase):
    def test_index_lists_every_space_escaped_with_its_open_link(self):
        evil = {"id": "direct:127.0.0.1:32300", "name": "<script>x</script>"}
        status, headers, body, cua = answer("/", FakeCua(spaces=(VPS_DESKTOP, MAC, evil)))
        page = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('href="/open/18501"', page)
        self.assertIn('href="/open/18502"', page)
        self.assertIn("Personal VPS Desktop", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("preview cua", page)  # the unshared Space says how to share it
        self.assertIn("width=device-width", page)
        self.assertEqual(dict(headers)["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", dict(headers)["Content-Security-Policy"])
        self.assertEqual(cua.calls, [[CUA, "spaces", "ls", "--json"]])

    def test_open_mints_a_fresh_link_and_redirects_to_the_share_port_with_audio_off(self):
        status, headers, body, cua = answer("/open/18501")
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["Location"],
                         f"https://{TS_NAME}:18501/viewer/#ticket={TICKET}&files=%2Fhome%2Fcua&audio=none")
        self.assertEqual(dict(headers)["Cache-Control"], "no-store")
        self.assertEqual(dict(headers)["Referrer-Policy"], "no-referrer")
        self.assertNotIn(TICKET.encode(), body)
        self.assertEqual(cua.calls[-1], [CUA, "sandbox", "view", VPS_DESKTOP["id"], "--no-open", "--json", "--ttl", "1h"])

    def test_open_replaces_any_audio_setting_cua_gave(self):
        view = json.dumps({"url": f"http://127.0.0.1:3211/viewer/#audio=pulse&ticket={TICKET}"})
        _, headers, _, _ = answer("/open/18502", FakeCua(view=view))
        self.assertEqual(dict(headers)["Location"], f"https://{TS_NAME}:18502/viewer/#ticket={TICKET}&audio=none")

    def test_unknown_or_gone_spaces_are_404_and_mint_nothing(self):
        for path, cua in (("/open/18599", FakeCua()), ("/open/18501", FakeCua(spaces=(MAC,))), ("/nope", FakeCua()),
                          ("/open/x", FakeCua())):
            with self.subTest(path=path):
                status, _, _, cua = answer(path, cua)
                self.assertEqual(status, 404)
                self.assertFalse([c for c in cua.calls if "sandbox" in c])

    def test_failed_or_odd_mint_is_502_without_the_ticket(self):
        for cua in (FakeCua(view_code=1), FakeCua(view="not json"), FakeCua(view=json.dumps({"url": 5})),
                    FakeCua(view=json.dumps({"url": f"http://127.0.0.1:1/other/#ticket={TICKET}"})),
                    FakeCua(view=json.dumps({"url": "http://127.0.0.1:1/viewer/"})),
                    FakeCua(view=json.dumps({"url": f"http://127.0.0.1:1/viewer/#ticket={TICKET}\r\nX: 1"}))):
            with self.subTest(view=cua.view):
                status, headers, body, _ = answer("/open/18501", cua)
                self.assertEqual(status, 502)
                self.assertNotIn(TICKET.encode(), body + json.dumps(headers).encode())
        status, _, _, _ = answer("/", FakeCua(spaces=None))
        self.assertEqual(status, 502)

    def test_index_server_answers_over_http_and_logs_nothing(self):
        cua = FakeCua()
        shares = lambda: SHARES
        server = pv.ThreadingHTTPServer(("127.0.0.1", 0), pv.make_cua_index_handler(cua, CUA, TS_NAME, shares))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                for method, path, want in (("GET", "/open/18501", 303), ("GET", "/", 200), ("POST", "/", 405),
                                           ("HEAD", "/", 200)):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                    conn.request(method, path)
                    response = conn.getresponse()
                    response.read()
                    self.assertEqual(response.status, want, (method, path))
                    if path.startswith("/open"):
                        self.assertTrue(response.getheader("Location").endswith("&audio=none"))
                    conn.close()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(err.getvalue(), "")

    def test_index_command_refuses_a_relative_cua_a_foreign_port_or_an_odd_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (["--cua", "cua", "--port", "18500", "--public-host", TS_NAME],
                         ["--cua", "/bin/sh", "--port", "18499", "--public-host", TS_NAME],
                         ["--cua", "/bin/sh", "--port", "18500", "--public-host", "evil.example/x"]):
                code, _, _ = tpf.quiet(pv.main, ["_cua-index", *argv, "--state-dir", tmp])
                self.assertEqual(code, pv.EXIT_CONFIG, argv)


if __name__ == "__main__":
    unittest.main()
