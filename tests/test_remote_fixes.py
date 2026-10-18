"""Every printed fix that runs claude-pool or preview on another machine names the launcher by its home-relative path.

Non-interactive ssh reads neither ~/.profile nor (on Debian/Ubuntu) the rest of ~/.bashrc, so ~/.local/bin is not on PATH
there and `ssh <target> claude-pool ...` fails with `command not found`. ssh starts in the home folder, so
`ssh <target> .local/bin/claude-pool ...` works everywhere. This scans the help, the docs and the fixes the code builds."""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import conductor
import device_install
import device_preview
import devices_cli
import remote
from remote import Completed
from fakes import FakeRemote

BARE = re.compile(r'ssh +(?:-o +\S+ +)*[^\s;,]+ +(claude-pool|preview)\b')
VPS = {'name': 'vps', 'ssh': 'alex@vps', 'platform': 'linux', 'roles': [], 'addedAt': 1}
MAC = {'name': 'mac', 'ssh': 'alex@mac', 'platform': 'darwin', 'roles': [], 'addedAt': 1}
ENTRY = {'device': 'vps', 'email': 'team@x.de', 'organization': 'Team A', 'cwd': '/srv/c'}
PART = {'installed': True, 'running': True, 'ready': True, 'fresh': False, 'duplicates': []}


def value(): return {'devices': [MAC, VPS], 'accounts': [], 'conductor': ENTRY}


class RemoteFixTests(unittest.TestCase):
    def assert_relative(self, text, where):
        self.assertIsNone(BARE.search(text), where + ': ' + text)

    def test_the_helper_names_the_launcher_by_its_home_relative_path(self):
        self.assertEqual(remote.remote_command('alex@vps', 'claude-pool device conductor install'),
                         'ssh alex@vps .local/bin/claude-pool device conductor install')
        self.assertEqual(remote.remote_command('vps', 'preview doctor'), 'ssh vps .local/bin/preview doctor')
        self.assertEqual(remote.remote_command('vps', 'sudo loginctl enable-linger alex'), 'ssh vps sudo loginctl enable-linger alex')

    def test_help_and_docs(self):
        self.assert_relative(devices_cli.DEVICE_HELP, 'DEVICE_HELP')
        for path in sorted((ROOT / 'docs').glob('*.md')):
            self.assert_relative(path.read_text(), str(path.relative_to(ROOT)))

    def test_conductor_fixes(self):
        me = 'mac'
        import registry
        from unittest import mock
        with mock.patch.object(registry, 'self_name', return_value=me):
            cases = {
                'not running': {'vps': {'platform': 'linux', 'linger': True, 'conductor': {**PART, 'running': False, 'ready': False}}},
                'not connected': {'vps': {'platform': 'linux', 'linger': True, 'conductor': {**PART, 'ready': False}}},
                'two': {'vps': {'platform': 'linux', 'linger': True, 'conductor': PART}, 'mac': {'conductor': PART}},
                'hand-started': {'vps': {'platform': 'linux', 'linger': True, 'conductor': {**PART, 'running': False, 'duplicates': [42],
                                                                                            'adoptSessionId': 'cse_01Ab'}}},
            }
            for name, statuses in cases.items():
                with self.subTest(name=name):
                    step = conductor.check(statuses, value())
                    self.assertIn('.local/bin/claude-pool', step.fix)
                    self.assert_relative(step.fix, name)
        self.assertEqual(conductor.adopt_fix('cse_01Ab', 'alex@vps'),
                         'stop the hand-started Conductor (herdr session conductor), then run: '
                         'ssh alex@vps .local/bin/claude-pool device conductor install --session-id cse_01Ab')

    def test_service_and_preview_fixes(self):
        failing = FakeRemote('alex@vps', lambda argv, stdin: Completed(1, b'', b''))
        step = device_install.start_service(failing, 'alex@vps')
        self.assertIn('.local/bin/claude-pool --start', step.fix); self.assert_relative(step.fix, 'start_service')
        step = device_preview.check(FakeRemote('alex@vps', lambda argv, stdin: Completed(2, b'', b'')), 'alex')
        self.assertIn('.local/bin/preview doctor', step.fix); self.assert_relative(step.fix, 'preview check')

    def test_target_on_and_check_help(self):
        session = devices_cli.Session(lambda target: FakeRemote(target, lambda argv, stdin: Completed(0, b'', b'')), None)
        session.me = 'mac'
        t = devices_cli.Target(session, VPS, FakeRemote('alex@vps', None), False, None)
        self.assertEqual(t.on('claude-pool device switch'), 'ssh alex@vps .local/bin/claude-pool device switch')
        for name, (_proves, fix) in devices_cli.CHECK_HELP.items(): self.assert_relative(fix, 'CHECK_HELP ' + name)

    def test_source_has_no_bare_remote_launcher_literal(self):
        literal = re.compile(r"""['"]ssh +[^'"]*['"]? *\+[^\n]*\+ *['"] *(claude-pool|preview)\b""")
        for path in sorted((ROOT / 'gateway').glob('*.py')):
            if path.name.startswith('test_'): continue
            text = path.read_text()
            self.assert_relative(text, str(path.relative_to(ROOT)))
            self.assertIsNone(literal.search(text), str(path.relative_to(ROOT)))


if __name__ == '__main__':
    unittest.main()
