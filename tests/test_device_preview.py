"""Preview-link install steps run against a fake remote; no ssh, tailscale or systemd is touched."""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
sys.path.insert(0, str(ROOT / 'tests'))
import device_install
import device_preview
import device_sync
from fakes import FakeRemote
from remote import Completed

TOOL_SHA = 'b3e645e7ba9750057b68c12f7dd518d19ff792a98062eeea857115b194358d94'
OTHER_SHA = '0' * 64
DOCTOR_HTTPS = 'tailnet name   box.ts.net\nbackend        https (Tailscale Serve is allowed for this user)\n'
DOCTOR_HTTP = 'tailnet name   box.ts.net\nbackend        http (Tailscale Serve not allowed for this user; links look like http://x:<port>)\n'


def done(out='', code=0): return Completed(code, out.encode(), b'')


def load_tool():
    loader = importlib.machinery.SourceFileLoader('preview_tool_under_test', str(ROOT / 'gateway/preview'))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def device(system='Linux', target_sha=None, doctor=DOCTOR_HTTPS, doctor_code=0):
    """A fake Linux (or macOS) machine. target_sha None means no preview file yet."""
    def handler(argv, stdin):
        if argv == ['uname', '-s']: return done(system + '\n')
        if argv[0] == 'sh' and 'sha256' in argv[2]: return done('absent\n' if target_sha is None else target_sha + '\n')
        if argv[0] == 'sh' and 'mktemp' in argv[2]: return done()
        if argv[-1] == 'doctor': return done(doctor, doctor_code)
        return done()
    return FakeRemote('box', handler)


def argvs(remote): return [argv for argv, _ in remote.calls]
def installs(remote): return [(a, s) for a, s in remote.calls if a[0] == 'sh' and 'mktemp' in a[2]]


class InstallTests(unittest.TestCase):
    def test_tool_is_the_proven_standalone_copy(self):
        self.assertEqual(hashlib.sha256(device_preview.TOOL.read_bytes()).hexdigest(), TOOL_SHA)

    def test_install_adopts_identical_file(self):
        remote = device(target_sha=TOOL_SHA)
        self.assertEqual(device_preview.install(remote).status, 'ok')
        self.assertEqual(installs(remote), [])

    def test_install_refuses_to_overwrite_a_different_preview(self):
        remote = device(target_sha=OTHER_SHA)
        step = device_preview.install(remote)
        self.assertEqual(step.status, 'failed')
        self.assertIn('rename', step.fix)
        self.assertIn('claude-pool device add box', step.fix)
        self.assertEqual(installs(remote), [])

    def test_install_streams_the_tool_on_stdin(self):
        remote = device()
        step = device_preview.install(remote)
        self.assertEqual(step.status, 'ok')
        argv, stdin = remote.calls[-1]
        self.assertEqual(stdin, device_preview.TOOL.read_bytes())
        script = argv[2]
        self.assertIn('0755', script); self.assertIn('mv ', script)
        self.assertNotIn(TOOL_SHA, ' '.join(argv))

    def test_a_failed_install_is_reported(self):
        remote = FakeRemote('box', lambda argv, stdin: done('Linux\n') if argv[0] == 'uname' else
                            done('absent\n') if 'sha256' in argv[2] else Completed(1, b'', b'disk full'))
        self.assertEqual(device_preview.install(remote).status, 'failed')

    def test_macos_skips_preview(self):
        remote = device(system='Darwin')
        step = device_preview.install(remote)
        self.assertEqual(step.status, 'skipped')
        self.assertIn('Linux only', step.detail)
        self.assertEqual(installs(remote), [])


class CheckTests(unittest.TestCase):
    def test_check_reports_http_only_with_operator_fix(self):
        step = device_preview.check(device(doctor=DOCTOR_HTTP), 'justus')
        self.assertEqual((step.status, step.detail, step.fix), ('manual', 'http-only', 'sudo tailscale set --operator=justus'))

    def test_check_ok_when_serve_is_permitted(self):
        self.assertEqual(device_preview.check(device(), 'justus').status, 'ok')

    def test_check_missing_when_the_tool_is_absent(self):
        step = device_preview.check(device(doctor='', doctor_code=127), 'justus')
        self.assertEqual(step.status, 'missing')
        self.assertIn('claude-pool device add box', step.fix)

    def test_check_failed_when_doctor_is_unreadable(self):
        self.assertEqual(device_preview.check(device(doctor='nonsense\n'), 'justus').status, 'failed')


class RulesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / '.claude' / 'CLAUDE.md'

    def verb(self, action):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): code = device_preview.rules_verb(action, self.file)
        return code

    def test_rules_block_is_idempotent_and_keeps_the_rest_of_claude_md(self):
        self.file.parent.mkdir(); original = b'# Mine\r\n\r\nkeep this \xff exactly\r\n'
        self.file.write_bytes(original)
        self.assertEqual(self.verb('check'), 1)
        self.assertEqual(self.verb('add'), 0)
        once = self.file.read_bytes()
        self.assertEqual(self.verb('add'), 0)
        self.assertEqual(self.file.read_bytes(), once)
        self.assertEqual(self.verb('check'), 0)
        text = once.decode('utf-8', 'replace')
        self.assertEqual(text.count(device_preview.RULES_START), 1)
        for word in ('preview <port>', 'preview file <path>', 'preview stop'): self.assertIn(word, text)
        self.assertTrue(once.startswith(original))
        self.assertEqual(self.verb('remove'), 0)
        self.assertEqual(self.file.read_bytes(), original)

    def test_add_creates_the_file_and_remove_deletes_only_the_block(self):
        self.assertEqual(self.verb('add'), 0)
        self.assertTrue(self.file.exists())
        self.assertEqual(self.verb('remove'), 0)
        self.assertNotIn(device_preview.RULES_START, self.file.read_text())

    def test_malformed_markers_are_reported_and_not_rewritten(self):
        self.file.parent.mkdir(); broken = (device_preview.RULES_START + '\nno end\n').encode()
        self.file.write_bytes(broken)
        self.assertEqual(self.verb('add'), 1)
        self.assertEqual(self.file.read_bytes(), broken)

    def test_unknown_action_is_an_error(self):
        self.assertEqual(self.verb('explode'), 2)


class RemoveTests(unittest.TestCase):
    def test_remove_stops_only_previews_it_created(self):
        remote = device(target_sha=TOOL_SHA)
        step = device_preview.remove(remote)
        self.assertEqual(step.status, 'ok')
        self.assertIn(['.local/bin/preview', 'stop', '--all'], argvs(remote))
        self.assertNotIn('systemctl', ' '.join(' '.join(map(str, argv)) for argv in argvs(remote)))
        self.assertIn(['.local/bin/claude-pool', 'device', 'preview-rules', 'remove'], argvs(remote))

    def test_remove_unlinks_only_an_identical_tool(self):
        remote = device(target_sha=TOOL_SHA)
        device_preview.remove(remote)
        unlink = [a for a in argvs(remote) if a[0] == 'sh' and 'rm -f' in a[2]][0]
        self.assertIn(TOOL_SHA, unlink)  # the identity check happens on the device, against the packaged hash

    def test_remove_skips_macos(self):
        self.assertEqual(device_preview.remove(device(system='Darwin')).status, 'skipped')


class ToolSafetyTests(unittest.TestCase):
    def test_proxy_binds_only_to_the_tailscale_ip(self):
        tool = load_tool()

        class Stand(tool.System):
            def __init__(self): super().__init__(); self.started = []
            def tailnet(self): return '100.101.102.103', 'box.ts.net'
            def operator_ok(self): return False
            def serve_ports(self): return set()
            def listeners(self, port): return []
            def unit_state(self, unit): return 'inactive'
            def unit_start(self, unit, desc, argv): self.started.append(argv[argv.index('--listen') + 1]); return True, ''
            def unit_stop(self, unit): pass
            def unit_log(self, unit, lines=5): return ''
            def can_connect(self, host, port, timeout=0.5): return True
            def sleep(self, s): pass
            def firewall_tailnet_ports(self, path=None): return None

        stand = Stand()
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {'PREVIEW_STATE_DIR': state, 'PREVIEW_BACKEND': 'http'}):
            code = tool.cmd_start(stand, 5173, 'x', out=io.StringIO(), err=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(stand.started, ['100.101.102.103:5173'])
        for bad in ['0.0.0.0', '::', '8.8.8.8']:
            with self.assertRaises(tool.UnsafeListenAddress): tool.check_listen_host(bad)
        self.assertEqual(tool.check_listen_host('127.0.0.1'), '127.0.0.1')
        self.assertEqual(tool.check_listen_host('100.101.102.103'), '100.101.102.103')


class PackageTests(unittest.TestCase):
    def package(self):
        spec = importlib.util.spec_from_file_location('claude_pool_package', ROOT / 'scripts/package.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_package_includes_the_preview_tool(self):
        self.assertIn('gateway/preview', set(self.package().source_files(ROOT)))

    def test_release_archive_carries_the_tool_executable(self):
        import io as _io, tarfile
        data = device_install.source_archive(ROOT)
        with tarfile.open(fileobj=_io.BytesIO(data), mode='r:gz') as archive:
            info = archive.getmember('claude-pool/gateway/preview')
            self.assertEqual(info.mode, 0o755)
            self.assertEqual(archive.extractfile(info).read(), device_preview.TOOL.read_bytes())
            self.assertEqual(archive.getmember('claude-pool/gateway/remote.py').mode, 0o644)


class StatusTests(unittest.TestCase):
    def test_status_part_reports_installed_and_https(self):
        with tempfile.TemporaryDirectory() as home:
            tool = Path(home) / '.local/bin/preview'; tool.parent.mkdir(parents=True)
            tool.write_text('#!/bin/sh\nprintf "backend        https (ok)\\n"\n'); tool.chmod(0o755)
            with mock.patch.object(Path, 'home', return_value=Path(home)):
                self.assertEqual(device_preview._status(), {'preview': {'installed': True, 'https': True}})

    def test_status_part_when_not_installed(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(Path, 'home', return_value=Path(home)):
            self.assertEqual(device_preview._status(), {'preview': {'installed': False, 'https': None}})

    def test_status_part_is_registered(self):
        self.assertIn(device_preview._status, device_sync.STATUS_PARTS)


if __name__ == '__main__': unittest.main()
