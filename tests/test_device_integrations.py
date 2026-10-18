"""herdr and app steps run against a fake herdr on PATH and a temp app dir; nothing real is touched."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import device_integrations
import device_sync
from remote import Local

VPS = {'name': 'personal-vps', 'ssh': 'personal-vps', 'platform': 'linux', 'herdrSession': 'dashboard'}
VPS_ID = 'prof-vps-1'
SCRIPT = '''#!/bin/sh
printf '%s\\n' "$*" >> "$HERDR_LOG"
if [ "$1 $2" = "machine list" ]; then cat "$HERDR_PROFILES"; fi
exit 0
'''
always, never = (lambda ssh: True), (lambda ssh: False)


def profile(pid, label, target): return {'id': pid, 'label': label, 'target': target, 'session': 's', 'enabled': True, 'selected': False}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.log = self.root / 'log'; self.log.write_text('')
        self.profiles = self.root / 'profiles.json'; self.profiles.write_text('[]')
        env = {'PATH': str(self.bin) + os.pathsep + '/usr/bin:/bin', 'HERDR_LOG': str(self.log), 'HERDR_PROFILES': str(self.profiles)}
        patcher = mock.patch.dict(os.environ, env); patcher.start(); self.addCleanup(patcher.stop)
        selfp = mock.patch('device_integrations.registry.self_name', return_value='mac'); selfp.start(); self.addCleanup(selfp.stop)
        self.local = Local()

    def herdr(self, rows=()):
        script = self.bin / 'herdr'; script.write_text(SCRIPT); script.chmod(0o755)
        self.profiles.write_text(json.dumps(list(rows)))

    def calls(self, word): return [line.split() for line in self.log.read_text().splitlines() if word in line]


class HerdrTests(Base):
    def test_herdr_profile_added_once_with_label_and_remote_session(self):
        self.herdr()
        self.assertEqual(device_integrations.ensure_herdr(VPS, self.local, always).status, 'ok')
        self.assertEqual(self.calls('machine add'), [['machine', 'add', 'personal-vps', '--label', 'personal-vps', '--remote-session', 'dashboard']])

    def test_existing_herdr_profile_is_kept(self):
        self.herdr([profile('x', 'VPS Conductor', 'personal-vps')])
        self.assertEqual(device_integrations.ensure_herdr(VPS, self.local, always).status, 'ok')
        self.assertEqual(self.calls('machine add'), [])

    def test_no_herdr_or_unreachable_device_is_skipped_not_fatal(self):
        self.assertEqual(device_integrations.ensure_herdr(VPS, self.local, always).status, 'skipped')
        self.herdr()
        self.assertEqual(device_integrations.ensure_herdr(VPS, self.local, never).detail, 'unreachable from this machine')
        self.assertEqual(device_integrations.ensure_herdr(dict(VPS, name='mac'), self.local, always).status, 'skipped')

    def test_remove_uses_the_profile_id_of_the_exact_label(self):
        self.herdr([profile('other', 'personal-vps2', 'personal-vps'), profile(VPS_ID, 'personal-vps', 'personal-vps'),
                    profile('mine', 'VPS Conductor', 'personal-vps')])
        self.assertEqual(device_integrations.remove_herdr(VPS, self.local).status, 'ok')
        self.assertEqual(self.calls('machine remove'), [['machine', 'remove', VPS_ID]])

    def test_remove_never_touches_profiles_the_user_named(self):
        self.herdr([profile('mine', 'VPS Conductor', 'personal-vps')])
        step = device_integrations.remove_herdr(VPS, self.local)
        self.assertEqual((step.status, step.detail), ('skipped', 'no profile created by claude-pool'))
        self.assertEqual(self.calls('machine remove'), [])

    def test_hook_adds_only_missing_profiles_and_status_lists_labels(self):
        self.herdr([profile('a', 'VPS dashboard', 'personal-vps')])
        with mock.patch('device_integrations.Remote') as remote:
            remote.return_value.reachable.return_value = True
            device_integrations.apply_hook({'devices': [VPS, {'name': 'two', 'ssh': 'two', 'platform': 'linux'}, {'name': 'mac', 'ssh': 'mac', 'platform': 'darwin'}]})
        self.assertEqual(self.calls('machine add'), [['machine', 'add', 'two', '--label', 'two']])
        self.assertEqual(device_integrations.herdr_profiles(self.local).keys(), {'VPS dashboard'})
        self.assertIn(device_integrations.apply_hook, device_sync.INTEGRATION_HOOKS)
        self.assertEqual(device_integrations._status(), {'herdrProfiles': ['VPS dashboard']})


class AppTests(Base):
    def setUp(self):
        super().setUp()
        self.app_dir = self.root / 'app'; self.app_dir.mkdir()
        self.store = self.app_dir / 'ssh_configs.json'

    def write(self, trusted):
        self.store.write_text(json.dumps({'configs': [{'name': 'vps', 'sshHost': 'personal-vps', 'id': '1'}], 'trustedHosts': trusted}))

    def test_app_environment_found_reads_without_writing(self):
        self.write(['personal-vps'])
        before = (self.store.read_bytes(), self.store.stat().st_mtime_ns)
        self.assertEqual(device_integrations.check_app(VPS, self.app_dir, 'darwin').status, 'ok')
        self.assertEqual(device_integrations.app_environments(self.app_dir), {'personal-vps'})
        self.assertEqual(before, (self.store.read_bytes(), self.store.stat().st_mtime_ns))

    def test_listed_but_untrusted_asks_to_trust(self):
        self.write([])
        step = device_integrations.check_app(VPS, self.app_dir, 'darwin')
        self.assertEqual(step.status, 'manual'); self.assertIn('trust it', step.fix)

    def test_a_missing_or_unreadable_environment_list_never_says_not_found(self):
        unreadable = 'could not read the Claude app\'s environment list'
        expected_fix = ('In the Claude app: Code tab, New: personal-vps should be listed under Recent environments; if not, add it '
                        '(Code tab, environment menu, Add environment, SSH, choose personal-vps, pick the folder, trust it).')
        cases = {'absent': None, 'broken': '{nope', 'not an object': '[1]', 'no configs list': '{"trustedHosts": []}'}
        for label, content in cases.items():
            with self.subTest(label):
                if content is None: app_dir = self.root / 'nothing'
                else: self.store.write_text(content); app_dir = self.app_dir
                step = device_integrations.check_app(VPS, app_dir, 'darwin')
                self.assertEqual((step.status, step.detail, step.fix), ('manual', unreadable, expected_fix))
                self.assertNotIn('not found', step.detail)
                self.assertIsNone(device_integrations.app_environments(app_dir))

    def test_a_parsed_list_without_the_host_says_not_found(self):
        self.store.write_text(json.dumps({'configs': [{'name': 'other', 'sshHost': 'other-host', 'id': '1'}], 'trustedHosts': ['other-host']}))
        step = device_integrations.check_app(VPS, self.app_dir, 'darwin')
        self.assertEqual((step.status, step.detail), ('manual', 'environment not found in the Claude app'))
        self.assertIn('Add environment', step.fix)
        self.store.write_text(json.dumps({'configs': [], 'trustedHosts': []}))
        self.assertEqual(device_integrations.check_app(VPS, self.app_dir, 'darwin').detail, 'environment not found in the Claude app')

    def test_not_macos_is_skipped(self):
        self.assertEqual(device_integrations.check_app(VPS, self.app_dir, 'linux').status, 'skipped')


if __name__ == '__main__': unittest.main()
