"""Tests for `preview file`: sharing one file or one folder read-only by link.

The file server runs for real on 127.0.0.1:0 (stdlib ThreadingHTTPServer). Everything that would touch systemd or
Tailscale goes through a stand-in System that records argv and never runs a command.
"""

import contextlib
import http.client
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
_loader = importlib.machinery.SourceFileLoader("preview_tool_files", str(HERE / "preview"))
pv = importlib.util.module_from_spec(importlib.util.spec_from_loader("preview_tool_files", _loader))
_loader.exec_module(pv)

TS_IP = "100.101.102.103"
TS_NAME = "box.tail0000.ts.net"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


# ---------------------------------------------------------------------------
# A stand-in for systemd --user and tailscale, recording every command line
# ---------------------------------------------------------------------------

class FakeSystem(pv.System):
    """Simulates systemd-run/systemctl/tailscale at the command level, so the real System methods build the argv."""

    def __init__(self, operator=False, serve_allowed=False):
        super().__init__()
        self.operator, self.serve_allowed = operator, serve_allowed
        self.active = {}  # unit -> (host, port) it listens on
        self.serve = {}
        self.commands = []
        self.busy = set()  # ports something else already listens on (127.0.0.1)
        self.next_free = 41000

    def argvs(self):
        return [list(argv) for argv in self.commands]

    def run(self, argv, timeout=30):
        argv = list(argv)
        self.commands.append(argv)
        done = lambda out="", code=0: pv.subprocess.CompletedProcess(argv, code, out, "")
        if argv[:2] == ["systemctl", "--user"]:
            verb, unit = argv[2], argv[3] if len(argv) > 3 else ""
            if verb == "stop":
                self.active.pop(unit, None)
            if verb == "is-active":
                return done("active\n" if unit in self.active else "inactive\n", 0 if unit in self.active else 3)
            return done()
        if argv[0] == "systemd-run":
            unit = next(a.split("=", 1)[1] for a in argv if a.startswith("--unit="))
            cmd = argv[argv.index("/usr/bin/python3"):]
            if "_serve-file" in cmd:
                listen = ("127.0.0.1", int(cmd[cmd.index("--port") + 1]))
            else:
                host, port = pv.parse_hostport(cmd[cmd.index("--listen") + 1])
                pv.check_listen_host(host)
                listen = (host, port)
            self.active[unit] = listen
            return done()
        if argv[:3] == ["tailscale", "serve", "status"]:
            return done(json.dumps({"TCP": {str(p): {} for p in self.serve}}))
        if argv[:2] == ["tailscale", "serve"] and "--bg" in argv:
            if not self.serve_allowed:
                return done("", 1)
            port = int(next(a for a in argv if a.startswith("--https=")).split("=")[1])
            self.serve[port] = argv[-1]
            return done()
        if argv[:2] == ["tailscale", "serve"] and argv[-1] == "off":
            self.serve.pop(int(argv[2].split("=")[1]), None)
            return done()
        return done()

    def tailnet(self):
        return TS_IP, TS_NAME

    def operator_ok(self):
        return self.operator

    def listeners(self, port):
        out = [(host, "python3") for host, p in self.active.values() if p == port]
        if port in self.busy:
            out.append(("127.0.0.1", "node"))
        return out

    def can_connect(self, host, port, timeout=0.5):
        return (host, port) in self.active.values() or (port in self.busy and host in ("127.0.0.1", "::1"))

    def free_local_port(self):
        self.next_free += 1
        return self.next_free

    def sleep(self, seconds):
        pass

    def firewall_tailnet_ports(self, path=None):
        return None


def quiet(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

class MarkdownTests(unittest.TestCase):
    def test_markdown_renders_headings_lists_code_and_escapes_html(self):
        html = pv.render_markdown('# T\n\n- a\n- <b>\n\n```\nx < y\n```\n', 'T')
        self.assertIn('<h1>T</h1>', html)
        self.assertIn('<li>&lt;b&gt;</li>', html)
        self.assertIn('x &lt; y', html)
        self.assertNotIn('<b>', html)

    def test_markdown_drops_javascript_urls(self):
        self.assertNotIn('javascript:', pv.render_markdown('[x](javascript:alert(1))', 'T'))
        for url in ['JavaScript:alert(1)', 'data:text/html,<script>1</script>', 'vbscript:x', 'file:///etc/passwd']:
            html = pv.render_markdown(f'[x]({url}) ![pic]({url})', 'T')
            self.assertNotIn('href', html, url)
            self.assertNotIn('<img', html, url)
            self.assertIn('x', html)

    def test_markdown_keeps_safe_links_and_images(self):
        html = pv.render_markdown('[site](https://example.com/a?b=1&c=2) [mail](mailto:j@example.com) '
                                  '[rel](sub/page.md) ![chart](img/c.png "Chart")', 'T')
        self.assertIn('<a href="https://example.com/a?b=1&amp;c=2">site</a>', html)
        self.assertIn('<a href="mailto:j@example.com">mail</a>', html)
        self.assertIn('<a href="sub/page.md">rel</a>', html)
        self.assertIn('<img src="img/c.png" alt="chart" title="Chart">', html)

    def test_markdown_inline_code_emphasis_quotes_and_ordered_lists(self):
        html = pv.render_markdown('Some *em*, **strong** and `a<b>`.\n\n> quoted <i>\n\n1. one\n2. two\n', 'T')
        self.assertIn('<em>em</em>', html)
        self.assertIn('<strong>strong</strong>', html)
        self.assertIn('<code>a&lt;b&gt;</code>', html)
        self.assertIn('<blockquote>', html)
        self.assertIn('quoted &lt;i&gt;', html)
        self.assertIn('<ol>', html)
        self.assertIn('<li>two</li>', html)

    def test_markdown_tables_and_titles_are_escaped(self):
        html = pv.render_markdown('| a | <b> |\n|---|---|\n| 1 | 2 |\n', '<script>x</script>')
        self.assertIn('<table>', html)
        self.assertIn('<th>&lt;b&gt;</th>', html)
        self.assertIn('<td>2</td>', html)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;x&lt;/script&gt;', html)

    def test_markdown_page_is_readable(self):
        html = pv.render_markdown('text', 'T')
        self.assertIn('max-width: 70ch', html)
        self.assertIn('system-ui', html)
        self.assertIn('<meta charset="utf-8">', html)

    def test_markdown_survives_pathological_input(self):
        for text in ['*' * 20000, '[' * 20000, '`' * 20000, '_a' * 10000, '> ' * 5000]:
            self.assertIn('<main>', pv.render_markdown(text, 'T'))


# ---------------------------------------------------------------------------
# The file server, for real on 127.0.0.1
# ---------------------------------------------------------------------------

class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def read(self):
        return self.body


def request(server, method, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders()
        r = conn.getresponse()
        return Response(r.status, r.headers, r.read())
    finally:
        conn.close()


def get(server, path):
    return request(server, "GET", path)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name).resolve()
        folder = cls.folder = base / "share"
        (folder / "sub" / ".git").mkdir(parents=True)
        (folder / "notes.md").write_text("# Notes\n\nHello <b>you</b>\n")
        (folder / "a.png").write_bytes(PNG)
        (folder / "a.pdf").write_bytes(PDF)
        (folder / "plain.txt").write_text("plain text\n")
        (folder / "page.html").write_text("<script>alert(1)</script>")
        (folder / "data.bin").write_bytes(b"\x00\x01\x02\xff" * 10)
        (folder / "Grüße \"x\".bin").write_bytes(b"\x00\xff")
        (folder / ".env").write_text("SECRET=1\n")
        (folder / "sub" / ".git" / "config").write_text("[core]\n")
        (folder / "sub" / "deep.txt").write_text("deep\n")
        (folder / "big.txt").write_bytes(b"x" * (3 * 1024 * 1024 + 7))
        (base / "secret.txt").write_text("outside\n")
        (folder / "link-outside").symlink_to(base / "secret.txt")
        (folder / "link-dir-outside").symlink_to(base)
        (folder / "link-to-env").symlink_to(folder / ".env")
        (folder / "link-inside.txt").symlink_to(folder / "plain.txt")
        os.mkfifo(folder / "fifo")
        cls.single = base / "single"
        cls.single.mkdir()
        (cls.single / "notes.md").write_text("# Single\n")
        (cls.single / "other.md").write_text("# Other\n")
        cls.folder_server = cls.serve(folder)
        cls.file_server = cls.serve(cls.single / "notes.md")

    @classmethod
    def serve(cls, root):
        server = pv.ThreadingHTTPServer(("127.0.0.1", 0), pv.make_file_handler(root, quiet=True))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    @classmethod
    def tearDownClass(cls):
        for server in (cls.folder_server, cls.file_server):
            server.shutdown()
            server.server_close()
        cls.tmp.cleanup()

    def test_traversal_and_dot_paths_are_404(self):
        for path in ['/../secret.txt', '/%2e%2e/secret.txt', '/.env', '/sub/.git/config', '/link-outside',
                     '/%2E%2E%2Fsecret.txt', '/sub/../../secret.txt', '/sub/%2e%2e/%2e%2e/secret.txt', '/.%65nv',
                     '/link-dir-outside/secret.txt', '/link-dir-outside/', '/link-to-env', '/%00', '/notes.md%00',
                     '/fifo', '//etc/passwd', '/sub/.git/', '/%ff', '/sub%2f..%2f..%2fsecret.txt']:
            r = get(self.folder_server, path)
            self.assertEqual(r.status, 404, path)
            self.assertNotIn(b"outside", r.body, path)
            self.assertNotIn(b"SECRET", r.body, path)

    def test_symlink_inside_the_root_is_served(self):
        self.assertEqual(get(self.folder_server, '/link-inside.txt').body, b"plain text\n")

    def test_single_file_share_serves_only_that_file(self):
        self.assertEqual(get(self.file_server, '/notes.md').status, 200)
        self.assertEqual(get(self.file_server, '/').status, 200)
        self.assertIn(b"<h1>Single</h1>", get(self.file_server, '/').body)
        for path in ['/other.md', '/../other.md', '/notes.md/x', '/sub/notes.md']:
            self.assertEqual(get(self.file_server, path).status, 404, path)

    def test_folder_listing_hides_dotfiles(self):
        r = get(self.folder_server, '/')
        body = r.read().decode()
        self.assertEqual(r.status, 200)
        self.assertIn('notes.md', body)
        self.assertNotIn('.env', body)
        self.assertNotIn('.git', body)
        self.assertNotIn('link-outside', body)
        self.assertNotIn('fifo', body)
        self.assertIn('href="/sub/"', body)
        self.assertIn('&quot;x&quot;', body)
        self.assertLess(body.index('a.pdf'), body.index('notes.md'))
        sub = get(self.folder_server, '/sub').read().decode()
        self.assertIn('href="/sub/deep.txt"', sub)
        self.assertNotIn('.git', sub)

    def test_images_and_pdf_open_inline_with_their_type(self):
        png = get(self.folder_server, '/a.png')
        self.assertEqual(png.headers['Content-Type'], 'image/png')
        self.assertTrue(png.headers['Content-Disposition'].startswith('inline'))
        self.assertEqual(png.body, PNG)
        pdf = get(self.folder_server, '/a.pdf')
        self.assertEqual(pdf.headers['Content-Type'], 'application/pdf')
        self.assertTrue(pdf.headers['Content-Disposition'].startswith('inline'))

    def test_text_and_binary(self):
        txt = get(self.folder_server, '/plain.txt')
        self.assertEqual(txt.headers['Content-Type'], 'text/plain; charset=utf-8')
        html = get(self.folder_server, '/page.html')
        self.assertEqual(html.headers['Content-Type'], 'text/plain; charset=utf-8')  # never rendered as HTML
        binary = get(self.folder_server, '/data.bin')
        self.assertEqual(binary.headers['Content-Type'], 'application/octet-stream')
        self.assertTrue(binary.headers['Content-Disposition'].startswith('attachment'))
        odd = get(self.folder_server, '/' + pv.urllib.parse.quote('Grüße "x".bin'))
        self.assertEqual(odd.status, 200)
        disposition = odd.headers['Content-Disposition']
        self.assertIn("filename*=UTF-8''Gr%C3%BC%C3%9Fe%20%22x%22.bin", disposition)
        self.assertNotIn('ü', disposition)
        self.assertEqual(disposition.count('"'), 2)

    def test_large_files_stream_whole(self):
        r = get(self.folder_server, '/big.txt')
        self.assertEqual(int(r.headers['Content-Length']), 3 * 1024 * 1024 + 7)
        self.assertEqual(len(r.body), 3 * 1024 * 1024 + 7)

    def test_markdown_raw_and_security_headers(self):
        r = get(self.folder_server, '/notes.md')
        self.assertEqual(r.headers['Content-Type'], 'text/html; charset=utf-8')
        self.assertEqual(r.headers['Content-Security-Policy'], "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'")
        self.assertIn(b'Hello &lt;b&gt;you&lt;/b&gt;', r.body)
        raw = get(self.folder_server, '/notes.md?raw=1')
        self.assertEqual(raw.headers['Content-Type'], 'text/plain; charset=utf-8')
        self.assertEqual(raw.body, b"# Notes\n\nHello <b>you</b>\n")
        for path in ['/notes.md?raw=1', '/a.png', '/a.pdf', '/plain.txt', '/data.bin', '/.env', '/nope']:
            headers = get(self.folder_server, path).headers
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff', path)
            self.assertEqual(headers['Content-Security-Policy'], 'sandbox', path)
        self.assertEqual(get(self.folder_server, '/').headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("default-src 'none'", get(self.folder_server, '/').headers['Content-Security-Policy'])

    def test_only_get_and_head(self):
        self.assertEqual(request(self.folder_server, 'POST', '/notes.md').status, 405)
        for method in ['PUT', 'DELETE', 'OPTIONS', 'PATCH', 'BREW']:
            r = request(self.folder_server, method, '/notes.md')
            self.assertEqual(r.status, 405, method)
            self.assertEqual(r.headers['X-Content-Type-Options'], 'nosniff')
        head = request(self.folder_server, 'HEAD', '/plain.txt')
        self.assertEqual((head.status, head.body), (200, b""))
        self.assertEqual(head.headers['Content-Length'], str(len("plain text\n")))

    def test_server_header_does_not_name_python(self):
        self.assertNotIn('Python', get(self.folder_server, '/').headers.get('Server', ''))


# ---------------------------------------------------------------------------
# The `file` command: refusals, plan, start, reuse, list, stop
# ---------------------------------------------------------------------------

class FileCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.home = base / "home"
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh" / "id_x").write_text("not a key\n")
        self.folder = self.home / "reports"
        self.folder.mkdir()
        (self.folder / "notes.md").write_text("# Hi\n")
        (self.folder / ".env").write_text("X=1\n")
        (self.home / "ssh-link").symlink_to(self.home / ".ssh")
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home), "PREVIEW_STATE_DIR": str(base / "state"),
                                             "PREVIEW_BACKEND": ""})
        patch.start()
        self.addCleanup(patch.stop)

    def main(self, argv, sysm):
        return quiet(pv.main, argv, sysm=sysm)

    def test_file_refuses_dot_paths_home_and_root(self):
        fake_system = FakeSystem()
        for path in [self.home / '.ssh', self.home, Path('/'), self.folder / '.env', self.home / 'ssh-link',
                     self.home / '.ssh' / 'id_x', self.home.parent, self.folder / 'missing.md',
                     Path(str(self.folder) + '/../.ssh')]:
            code, out, err = self.main(['file', str(path)], fake_system)
            self.assertNotEqual(code, 0, path)
            self.assertEqual(out, '', path)
            self.assertEqual(len(err.strip().splitlines()), 1, (path, err))
            self.assertTrue(err.startswith('preview: '), err)
        self.assertEqual(fake_system.active, {})
        self.assertFalse([a for a in fake_system.argvs() if a[0] == 'systemd-run'])

    def test_file_share_serves_on_loopback_and_reuses_the_backend(self):
        for operator, serve_allowed in [(False, False), (True, True)]:
            with self.subTest(operator=operator):
                fake = FakeSystem(operator=operator, serve_allowed=serve_allowed)
                plan = pv.file_plan(['file', str(self.folder)], fake)
                self.assertEqual(plan['serve'], ('127.0.0.1', plan['port']))
                self.assertIn(plan['port'], pv.FILE_PORTS)
                self.assertEqual(plan['unit'], f"preview-file-{plan['port']}.service")
                self.assertEqual(plan['path'], str(self.folder))
                self.assertEqual(fake.argvs(), [a for a in fake.argvs() if a[0] != 'systemd-run'])  # pure: nothing started
                self.assertEqual(fake.active, {})
                same_backend_as_port_preview = 'https' if operator else 'http'
                self.assertEqual(plan['backend'], same_backend_as_port_preview)
                code, out, err = self.main(['file', str(self.folder), '--name', 'reports'], fake)
                self.assertEqual(code, 0, err)
                state = pv.load_state(plan['port'])
                self.assertEqual(state['backend'], plan['backend'])
                self.assertEqual(state['path'], str(self.folder))
                self.assertEqual(out.strip(), state['link'])
                self.assertEqual(fake.active[plan['unit']], ('127.0.0.1', plan['port']))
                file_run = [a for a in fake.argvs() if a[0] == 'systemd-run' and '_serve-file' in a][0]
                self.assertIn(str(self.folder), file_run)
                quiet(pv.main, ['stop', '--all'], sysm=fake)

    def test_file_share_on_the_http_backend_binds_only_loopback_and_the_tailnet_ip(self):
        fake = FakeSystem(operator=False)
        code, out, _ = self.main(['file', str(self.folder / 'notes.md')], fake)
        self.assertEqual(code, 0)
        port = int(out.strip().rsplit(':', 1)[1])
        self.assertEqual(sorted(fake.active.values()), [('100.101.102.103', port), ('127.0.0.1', port)])
        self.assertEqual(out.strip(), f'http://{TS_NAME}:{port}')

    def test_same_path_reuses_its_port(self):
        fake = FakeSystem(operator=False)
        first = self.main(['file', str(self.folder)], fake)
        starts = len([a for a in fake.argvs() if a[0] == 'systemd-run'])
        second = self.main(['file', str(self.folder) + '/'], fake)
        self.assertEqual(first[1], second[1])
        self.assertEqual(len([a for a in fake.argvs() if a[0] == 'systemd-run']), starts)
        other = self.main(['file', str(self.folder / 'notes.md')], fake)
        self.assertNotEqual(other[1], first[1])
        self.assertEqual(pv.file_plan(['file', str(self.folder)], fake)['port'], int(first[1].strip().rsplit(':', 1)[1]))

    def test_dead_file_server_is_restarted_on_rerun(self):
        fake = FakeSystem(operator=False)
        _, out, _ = self.main(['file', str(self.folder)], fake)
        port = int(out.strip().rsplit(':', 1)[1])
        del fake.active[f'preview-file-{port}.service']
        code, again, _ = self.main(['file', str(self.folder)], fake)
        self.assertEqual((code, again), (0, out))
        self.assertIn(f'preview-file-{port}.service', fake.active)

    def test_busy_ports_are_skipped(self):
        fake = FakeSystem(operator=False)
        fake.busy = {18400, 18401}
        self.assertEqual(pv.file_plan(['file', str(self.folder)], fake)['port'], 18402)

    def test_list_shows_the_path(self):
        fake = FakeSystem(operator=False)
        _, out, _ = self.main(['file', str(self.folder), '--name', 'reports'], fake)
        _, table, _ = self.main(['list'], fake)
        self.assertIn(str(self.folder), table)
        self.assertIn('reports', table)
        _, js, _ = self.main(['list', '--json'], fake)
        row = json.loads(js)[0]
        self.assertEqual(row['path'], str(self.folder))
        self.assertEqual(row['link'], out.strip())
        for key in ('port', 'label', 'link', 'backend', 'unit', 'listen', 'target_listening', 'proxy'):
            self.assertIn(key, row)

    def test_stop_by_path_removes_the_share_unit(self):
        fake_system = FakeSystem(operator=True, serve_allowed=True)
        _, out, _ = self.main(['file', str(self.folder)], fake_system)
        port = int(out.strip().rsplit(':', 1)[1])
        code, _, _ = self.main(['stop', str(self.folder)], fake_system)
        self.assertEqual(code, 0)
        self.assertIn(['systemctl', '--user', 'stop', f'preview-file-{port}.service'], fake_system.argvs())
        self.assertIn(['systemctl', '--user', 'stop', f'preview-{port}'], fake_system.argvs())
        self.assertEqual(fake_system.active, {})
        self.assertEqual(fake_system.serve, {})
        self.assertIsNone(pv.load_state(port))

    def test_stop_by_port_and_all_remove_file_shares(self):
        fake = FakeSystem(operator=False)
        _, out, _ = self.main(['file', str(self.folder)], fake)
        port = int(out.strip().rsplit(':', 1)[1])
        self.assertEqual(self.main(['stop', str(port)], fake)[0], 0)
        self.assertEqual(fake.active, {})
        self.main(['file', str(self.folder)], fake)
        self.main(['file', str(self.folder / 'notes.md')], fake)
        self.assertEqual(self.main(['stop', '--all'], fake)[0], 0)
        self.assertEqual(fake.active, {})
        self.assertEqual(pv.all_states(), [])

    def test_stop_unknown_path_fails(self):
        code, _, err = self.main(['stop', str(self.folder)], FakeSystem())
        self.assertEqual(code, 1)
        self.assertIn('no file share', err)

    def test_serve_file_command_refuses_unsafe_roots(self):
        for root in [self.home, Path('/'), self.home / '.ssh']:
            code, _, err = quiet(pv.main, ['_serve-file', '--root', str(root), '--port', '18400'])
            self.assertEqual(code, pv.EXIT_CONFIG, root)

    def test_help_names_the_file_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            pv.parse(['--help'])
        self.assertIn('preview file <path>', out.getvalue())
        self.assertIn('preview stop <port>|<path>', out.getvalue())


if __name__ == "__main__":
    unittest.main()
