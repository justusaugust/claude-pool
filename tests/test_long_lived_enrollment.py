"""Enrolling an account with a long-lived (setup-token) token: identity without profile scope, never a printed token."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import account_login
import accounts_cli as setup
import long_lived
import registry
import settings
from remote import Completed
from fakes import FakeRemote, SecretWatch, fixture_token, long_lived_file, refreshable_file

SELF = {'name': 'mac', 'ssh': 'alex@mac', 'platform': 'darwin', 'roles': [], 'addedAt': 1}
OWNER = {'name': 'example-vps', 'ssh': 'alex@vps', 'platform': 'linux', 'roles': ['usage'], 'addedAt': 2}


def mode(path): return path.stat().st_mode & 0o777


def returns_json(value):
    return lambda argv, stdin: Completed(0, json.dumps(value).encode(), b'')


FAKE_SETUP_TOKEN_SHUNT = '''
import os, sys, json, getpass
from pathlib import Path
if 'check' in sys.argv: sys.exit(0)
mode = sys.argv[sys.argv.index('--mode') + 1]
assert mode == 'setup-token', mode
assert '--manual' not in sys.argv, 'setup-token mode has no --manual flag'
name = sys.argv[sys.argv.index('--name') + 1]
print('https://claude.com/cai/oauth/authorize?state=fixture-state&scope=user%3Ainference', flush=True)
value = getpass.getpass('Paste authorization code (input hidden): ')
assert value == 'fixture-code#fixture-state'
token = ''.join(['sk', '-ant', '-oat01', '-fixture-', 'a' * 32])
print('DO-NOT-LOG ' + token + ' ' + value, flush=True)
p = Path(os.environ['SHUNT_CLAUDE_ACCOUNTS_DIR']) / (name + '.json')
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump({'claudeAiOauth': {'accessToken': token, 'expiresAt': 4000000000000, 'shuntCredentialKind': 'setup_token'},
               'shuntAccountUuid': 'user-a', 'shuntOrganizationUuid': 'team-a'}, f)
'''


class LongLivedEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stores = self.root / 'credentials'; self.stores.mkdir(mode=0o700)
        self.long_lived = self.root / 'claude-long-lived'
        self.runtime = self.root / 'runtime'; self.runtime.mkdir()
        self.cfg = self.root / 'cfg'; self.cfg.mkdir(mode=0o700)
        self.config = self.cfg / 'config.json'; self.config.write_text('{"accounts": []}')
        self.reg = self.cfg / 'devices.json'
        self.self_path = self.cfg / 'device.json'
        self.backend = self.runtime / 'shunt.toml'
        self.backend.write_text('[providers.anthropic]\nauth="claude_oauth"\naccounts=[]\n')
        self.binary = self.root / 'fake-shunt'
        self.binary.write_text('#!' + sys.executable + '\n' + FAKE_SETUP_TOKEN_SHUNT)
        self.binary.chmod(0o700)
        self.staging = self.root / 'staging'; self.staging.mkdir(mode=0o700)

    def info(self, email='operations@example.test', user='user-a', org='team-a', organization='Example Team'):
        return {'email': email, 'organization': organization, 'plan': 'Team Premium', 'userId': user, 'organizationId': org}

    def staged(self, **kwargs):
        return long_lived_file(self.staging, 'token', expires_ms=4_000_000_000_000, **kwargs)

    def devices(self, *rows, me='mac'):
        registry.update(lambda value: value['devices'].extend(rows), self.reg)
        registry.set_self(me, self.self_path)

    def run_cli(self, *extra, overview=None, lookup=None):
        lookup = lookup or Mock(return_value=None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = setup.cli(['add', 'operations@example.test', '--name', 'operations', '--label', 'Operations', *extra],
                               config=self.config, stores=self.stores, binary=self.binary, runtime=self.runtime,
                               overview=overview, long_lived=self.long_lived, registry_path=self.reg,
                               self_path=self.self_path, lookup=lookup)
        return result, out.getvalue()

    def snapshot(self):
        return [p.read_bytes() if p.exists() else None for p in (self.config, self.backend, self.reg)]

    def test_setup_token_login_uses_setup_token_mode_and_hidden_code(self):
        self.long_lived.mkdir(mode=0o700)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            account_login.login(self.binary, self.long_lived, 'operations', 'Operations',
                                code_reader=lambda label: 'fixture-code#fixture-state', timeout=5, mode='setup-token')
        value = setup.credential(self.long_lived / 'operations.json')
        self.assertEqual(long_lived.kind(value), 'long-lived')
        SecretWatch(fixture_token(), 'DO-NOT-LOG', 'fixture-code').text(out.getvalue())
        self.assertIn('Browser authorization opened', out.getvalue())

    def test_login_refuses_an_unknown_mode(self):
        with self.assertRaises(ValueError):
            account_login.login(self.binary, self.long_lived, 'operations', 'Operations', mode='import')

    def test_long_lived_enrollment_logs_in_into_the_long_lived_folder(self):
        def fake_login(binary, directory, name, label, mode='oauth'):
            self.assertEqual((Path(directory), mode), (self.long_lived, 'setup-token'))
            self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o700)
            long_lived_file(directory, name, expires_ms=4_000_000_000_000)
        refreshable_file(self.stores, 'team-own', 'user-a', 'team-a')
        with patch.object(setup, 'login', side_effect=fake_login) as login, patch.object(setup, 'profile', return_value=self.info()):
            code, out = self.run_cli('--long-lived')
        login.assert_called_once()
        self.assertEqual(code, 0)
        SecretWatch(fixture_token()).text(out)

    def test_long_lived_enrollment_verifies_identity_with_local_refreshable_grant(self):
        staged = self.staged()
        own = refreshable_file(self.stores, 'team-own', 'user-a', 'team-a')
        refreshable_file(self.stores, 'other', 'user-b', 'team-a')
        lookup = Mock(return_value=None)
        with patch.object(setup, 'profile', return_value=self.info()) as profile:
            code, out = self.run_cli('--long-lived', '--from-file', str(staged), lookup=lookup)
        profile.assert_called_once_with(own)
        lookup.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn('operations@example.test · Example Team · Team Premium', out)

    def test_long_lived_enrollment_asks_the_usage_owner_when_no_local_grant(self):
        staged = self.staged()
        lookup = Mock(return_value=self.info())
        with patch.object(setup, 'profile') as profile:
            code, out = self.run_cli('--long-lived', '--from-file', str(staged), lookup=lookup)
        lookup.assert_called_once_with('user-a', 'team-a')
        profile.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.config.read_text())['accounts'],
                         [{'name': 'operations', 'label': 'Operations', 'alias': 'operations', 'plan': 'Team Premium'}])
        self.assertIn('name = "operations"', self.backend.read_text())
        entry = registry.account(registry.load(self.reg), 'operations')
        self.assertEqual({k: entry[k] for k in ('email', 'organization', 'userId', 'organizationId', 'expiresAt')},
                         {'email': 'operations@example.test', 'organization': 'Example Team', 'userId': 'user-a',
                          'organizationId': 'team-a', 'expiresAt': 4_000_000_000_000})

    def test_owner_lookup_uses_the_injected_remote_factory(self):
        owner = FakeRemote('vps', returns_json(self.info()))
        targets = []
        self.devices(SELF, OWNER)
        with patch.object(settings, 'REGISTRY', self.reg), patch.object(settings, 'SELF', self.self_path):
            got = setup.owner_lookup('user-a', 'team-a', remotes=lambda target: targets.append(target) or owner)
        self.assertEqual(got, self.info())
        self.assertEqual(targets, ['alex@vps'])
        self.assertEqual(owner.calls[-1][0], ['.local/bin/claude-pool', 'device', 'identity', 'user-a', 'team-a'])

    def test_owner_lookup_is_none_without_an_owner_on_self_or_on_failure(self):
        calls = []
        def factory(target):
            calls.append(target)
            return FakeRemote('vps', lambda argv, stdin: Completed(255, b'', b'unreachable'))
        with patch.object(settings, 'REGISTRY', self.reg), patch.object(settings, 'SELF', self.self_path):
            self.assertIsNone(setup.owner_lookup('user-a', 'team-a', remotes=factory))  # no registry at all
            self.devices(SELF)
            self.assertIsNone(setup.owner_lookup('user-a', 'team-a', remotes=factory))  # no usage owner
            self.assertEqual(calls, [])
            registry.update(lambda value: value['devices'].append(OWNER), self.reg)
            self.assertIsNone(setup.owner_lookup('user-a', 'team-a', remotes=factory))  # unreachable owner
            self.assertEqual(calls, ['alex@vps'])
            registry.set_self('example-vps', self.self_path)
            self.assertIsNone(setup.owner_lookup('user-a', 'team-a', remotes=factory))  # the owner is this machine
            self.assertEqual(calls, ['alex@vps'])
        owner = FakeRemote('vps', lambda argv, stdin: Completed(0, b'not json', b''))
        registry.set_self('mac', self.self_path)
        with patch.object(settings, 'REGISTRY', self.reg), patch.object(settings, 'SELF', self.self_path):
            self.assertIsNone(setup.owner_lookup('user-a', 'team-a', remotes=lambda target: owner))

    def test_identity_mismatch_or_unreachable_owner_enrolls_nothing(self):
        self.devices(SELF, OWNER)
        cases = [
            ('unreachable', Mock(return_value=None), 'usage owner'),
            ('other email', Mock(return_value=self.info(email='someone-else@example.test')), 'Signed in as'),
            ('other user', Mock(return_value=self.info(user='user-b')), 'differ'),
            ('incomplete', Mock(return_value={'email': 'operations@example.test'}), 'identity'),
        ]
        for label, lookup, message in cases:
            with self.subTest(label):
                staged = self.staged()
                before = self.snapshot()
                with self.assertRaisesRegex(RuntimeError, message):
                    self.run_cli('--long-lived', '--from-file', str(staged), lookup=lookup)
                self.assertEqual(self.snapshot(), before)
                self.assertTrue((self.long_lived / 'operations.json').is_file())
                self.assertFalse((self.stores / 'operations.json').exists())
                (self.long_lived / 'operations.json').unlink()

    def test_from_file_adopts_without_printing_and_removes_the_staged_copy(self):
        staged = self.staged()
        code, out = self.run_cli('--long-lived', '--from-file', str(staged), lookup=Mock(return_value=self.info()))
        self.assertEqual(code, 0)
        SecretWatch(fixture_token()).text(out)
        self.assertFalse(staged.exists())
        self.assertEqual(mode(self.long_lived / 'operations.json'), 0o600)
        self.assertEqual(mode(self.long_lived), 0o700)
        self.assertEqual(mode(self.stores / 'operations.json'), 0o600)
        value = registry.load(self.reg)
        self.assertRegex(value['accounts'][0]['fingerprint'], r'^[0-9a-f]{12}$')
        stored = setup.credential(self.stores / 'operations.json')
        self.assertEqual(value['accounts'][0]['fingerprint'], long_lived.fingerprint(stored))
        SecretWatch(fixture_token()).tree(self.root, [self.long_lived / 'operations.json', self.stores / 'operations.json'])

    def test_from_file_refuses_refreshable_or_group_readable_files(self):
        refreshable = refreshable_file(self.staging, 'refreshable', 'user-a', 'team-a')
        loose = self.staged(); loose.chmod(0o644)
        for staged, message in [(refreshable, 'long-lived'), (loose, 'private 0600')]:
            with self.subTest(staged.name):
                before = staged.read_bytes()
                with self.assertRaisesRegex(RuntimeError, message):
                    long_lived.adopt(staged, self.long_lived, 'operations')
                self.assertEqual(staged.read_bytes(), before)
                self.assertFalse((self.long_lived / 'operations.json').exists())

    def test_adopt_refuses_a_different_saved_token_and_keeps_both(self):
        saved = long_lived_file(self.long_lived, 'operations', token=fixture_token('b'))
        saved_before = saved.read_bytes()
        staged = self.staged()
        with self.assertRaisesRegex(RuntimeError, 'different'):
            long_lived.adopt(staged, self.long_lived, 'operations')
        self.assertTrue(staged.exists())
        self.assertEqual(saved.read_bytes(), saved_before)

    def test_adopt_of_the_target_itself_keeps_the_file(self):
        saved = long_lived_file(self.long_lived, 'operations')
        self.assertEqual(long_lived.adopt(saved, self.long_lived, 'operations'), saved)
        self.assertTrue(saved.is_file())

    def test_existing_refreshable_store_is_never_replaced_by_enrollment(self):
        before = refreshable_file(self.stores, 'operations', 'user-a', 'team-a').read_bytes()
        staged = self.staged()
        with patch.object(setup, 'profile', return_value=self.info()):
            code, out = self.run_cli('--long-lived', '--from-file', str(staged))
        self.assertEqual(code, 0)
        self.assertEqual((self.stores / 'operations.json').read_bytes(), before)
        self.assertIn('This machine keeps its own login for Operations; run claude-pool device switch when no task is running.', out)
        self.assertEqual(registry.account(registry.load(self.reg), 'operations')['fingerprint'],
                         long_lived.fingerprint(setup.credential(self.long_lived / 'operations.json')))

    def test_a_second_name_for_the_same_subscription_writes_nothing(self):
        refreshable_file(self.stores, 'existing', 'user-a', 'team-a')
        self.config.write_text(json.dumps({'accounts': [{'name': 'existing', 'label': 'Existing'}]}))
        before = self.snapshot()
        with patch.object(setup, 'profile', return_value=self.info()), self.assertRaisesRegex(RuntimeError, 'already connected as Existing'):
            self.run_cli('--long-lived', '--from-file', str(self.staged()))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.stores / 'operations.json').exists())
        (self.stores / 'existing.json').unlink(); self.config.write_text('{"accounts": []}')
        registry.update(lambda value: value['accounts'].append({'name': 'team', 'label': 'Team', 'userId': 'user-a', 'organizationId': 'team-a'}), self.reg)
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'already connected as Team'):
            self.run_cli('--long-lived', lookup=Mock(return_value=self.info()))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.stores / 'operations.json').exists())

    def test_usage_owner_never_gets_a_long_lived_store_copy(self):
        self.devices(SELF, OWNER, me='example-vps')
        staged = self.staged()
        code, out = self.run_cli('--long-lived', '--from-file', str(staged), lookup=Mock(return_value=self.info()))
        self.assertEqual(code, 0)
        self.assertFalse((self.stores / 'operations.json').exists())
        self.assertTrue((self.long_lived / 'operations.json').is_file())
        self.assertEqual(json.loads(self.config.read_text())['accounts'], [])
        self.assertIn('usage owner', out)
        self.assertIsNotNone(registry.account(registry.load(self.reg), 'operations'))

    def test_readiness_accepts_no_reading_yet_for_long_lived(self):
        staged = self.staged()
        code, out = self.run_cli('--long-lived', '--from-file', str(staged), lookup=Mock(return_value=self.info()),
                                 overview=lambda: {'accounts': [{'name': 'operations', 'status': 'unknown'}]})
        self.assertEqual(code, 0)
        self.assertIn('Connected Operations · no reading yet (first request fills it)', out)

    def test_readiness_reauthenticate_names_the_reenroll_fix(self):
        staged = self.staged()
        code, out = self.run_cli('--long-lived', '--from-file', str(staged), lookup=Mock(return_value=self.info()),
                                 overview=lambda: {'accounts': [{'name': 'operations', 'status': 'reauthenticate'}]})
        self.assertEqual(code, 2)
        self.assertIn('account add operations@example.test --long-lived --replace --name operations', out)

    def test_rerun_is_idempotent(self):
        staged = self.staged()
        lookup = Mock(return_value=self.info())
        self.run_cli('--long-lived', '--from-file', str(staged), lookup=lookup)
        first = registry.load(self.reg)['generation']
        store = (self.stores / 'operations.json').read_bytes()
        with patch.object(setup, 'login') as login:
            code, out = self.run_cli('--long-lived', lookup=lookup)
        login.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn('Using saved long-lived token', out)
        self.assertEqual(len(json.loads(self.config.read_text())['accounts']), 1)
        value = registry.load(self.reg)
        self.assertEqual(len(value['accounts']), 1)
        self.assertLessEqual(value['generation'] - first, 1)
        self.assertEqual(self.backend.read_text().count('name = "operations"'), 1)
        self.assertEqual((self.stores / 'operations.json').read_bytes(), store)

    def enrolled(self):
        """Enroll token a, then let the pool report it revoked."""
        self.run_cli('--long-lived', '--from-file', str(self.staged()), lookup=Mock(return_value=self.info()))
        code, out = self.run_cli('--long-lived', lookup=Mock(return_value=self.info()),
                                 overview=lambda: {'accounts': [{'name': 'operations', 'status': 'reauthenticate'}]})
        self.assertEqual(code, 2)
        return out

    def asides(self, directory):
        return sorted(p for p in directory.iterdir() if '.replaced-' in p.name)

    def test_reauthenticate_message_names_replace(self):
        self.assertIn('account add operations@example.test --long-lived --replace --name operations', self.enrolled())

    def test_replace_after_reauthenticate_installs_the_new_token_and_moves_the_old_aside(self):
        self.enrolled()
        old = long_lived.fingerprint(setup.credential(self.long_lived / 'operations.json'))
        new_file = long_lived_file(self.staging, 'new', token=fixture_token('b'), expires_ms=4_100_000_000_000)
        code, out = self.run_cli('--long-lived', '--replace', '--from-file', str(new_file), lookup=Mock(return_value=self.info()),
                                 overview=lambda: {'accounts': [{'name': 'operations', 'status': 'unknown'}]})
        self.assertEqual(code, 0)
        SecretWatch(fixture_token('a'), fixture_token('b')).text(out)
        new = long_lived.fingerprint(setup.credential(self.long_lived / 'operations.json'))
        self.assertNotEqual(new, old)
        self.assertFalse(new_file.exists())
        for directory in (self.long_lived, self.stores):
            with self.subTest(directory.name):
                self.assertEqual(long_lived.fingerprint(setup.credential(directory / 'operations.json')), new)
                [aside] = self.asides(directory)
                self.assertRegex(aside.name, r'^operations\.json\.replaced-\d{8}T\d{6}Z$')
                self.assertEqual(mode(aside), 0o600)
                self.assertEqual(long_lived.fingerprint(setup.credential(aside)), old)
        entry = registry.account(registry.load(self.reg), 'operations')
        self.assertEqual((entry['fingerprint'], entry['expiresAt']), (new, 4_100_000_000_000))

    def test_replace_with_a_fresh_login_uses_a_private_staging_folder(self):
        self.enrolled()
        seen = []
        def fake_login(binary, directory, name, label, mode='oauth'):
            directory = Path(directory)
            self.assertEqual((directory.parent, mode, directory.stat().st_mode & 0o777), (self.long_lived, 'setup-token', 0o700))
            seen.append(directory)
            long_lived_file(directory, name, token=fixture_token('b'))
        with patch.object(setup, 'login', side_effect=fake_login):
            code, out = self.run_cli('--long-lived', '--replace', lookup=Mock(return_value=self.info()))
        self.assertEqual(code, 0)
        self.assertFalse(seen[0].exists())
        self.assertEqual(long_lived.fingerprint(setup.credential(self.long_lived / 'operations.json')),
                         long_lived.fingerprint(setup.credential(self.stores / 'operations.json')))
        self.assertEqual(len(self.asides(self.long_lived)), 1)

    def files(self):
        return {p: p.read_bytes() for d in (self.long_lived, self.stores, self.cfg, self.runtime) for p in sorted(d.rglob('*')) if p.is_file()}

    def test_replace_with_another_identity_or_the_same_token_writes_nothing(self):
        self.enrolled()
        cases = [('other user', long_lived_file(self.staging, 'other-user', user='user-b', token=fixture_token('b')), 'another account'),
                 ('other org', long_lived_file(self.staging, 'other-org', org='team-b', token=fixture_token('c')), 'another account'),
                 ('same token', long_lived_file(self.staging, 'same', token=fixture_token('a')), 'same as the saved one')]
        for label, staged, message in cases:
            with self.subTest(label):
                before, source = self.files(), staged.read_bytes()
                lookup = Mock(return_value=self.info())
                with self.assertRaisesRegex(RuntimeError, message):
                    self.run_cli('--long-lived', '--replace', '--from-file', str(staged), lookup=lookup)
                self.assertEqual(self.files(), before)
                self.assertEqual(staged.read_bytes(), source)
                self.assertEqual(self.asides(self.long_lived), [])

    def test_replace_needs_a_saved_token_and_long_lived(self):
        with self.assertRaisesRegex(RuntimeError, 'No long-lived token is saved'):
            self.run_cli('--long-lived', '--replace', '--from-file', str(self.staged()))
        with self.assertRaisesRegex(RuntimeError, '--long-lived'):
            self.run_cli('--replace')

    def test_rerun_without_replace_reuses_the_saved_token(self):
        self.enrolled()
        before = (self.long_lived / 'operations.json').read_bytes()
        with patch.object(setup, 'login') as login:
            code, out = self.run_cli('--long-lived', lookup=Mock(return_value=self.info()))
        login.assert_not_called()
        self.assertIn('Using saved long-lived token', out)
        self.assertEqual((self.long_lived / 'operations.json').read_bytes(), before)
        self.assertEqual(self.asides(self.long_lived), [])
        with self.assertRaisesRegex(RuntimeError, '--replace'):
            self.run_cli('--long-lived', '--from-file', str(long_lived_file(self.staging, 'new', token=fixture_token('b'))))

    def test_from_file_needs_long_lived(self):
        staged = self.staged()
        with self.assertRaisesRegex(RuntimeError, '--long-lived'):
            self.run_cli('--from-file', str(staged))
        self.assertTrue(staged.exists())

    def test_local_paths_env_matches_the_backend_validation_inputs(self):
        source = self.root / 'source.toml'
        paths, env = setup.local_paths_env(self.runtime, self.stores, source, {'EXTRA': '1'})
        self.assertEqual(paths, [source, self.runtime / 'shunt.toml'])
        self.assertEqual(env['SHUNT_CLAUDE_ACCOUNTS_DIR'], str(self.stores))
        self.assertEqual(env['EXTRA'], '1')
        self.assertEqual(setup.local_paths_env(self.runtime, self.stores)[0], [self.runtime / 'shunt.toml'])

    def test_register_local_uses_the_installation_settings(self):
        with patch.object(setup, 'register') as register:
            setup.register_local({'name': 'operations'}, self.info())
        args = register.call_args.args
        self.assertEqual(args[:3], (settings.CONFIG, settings.STORES, settings.HERE / 'bin/shunt'))
        self.assertEqual(args[3], [settings.RUNTIME / 'shunt.toml'])
        self.assertEqual(args[6]['SHUNT_CLAUDE_ACCOUNTS_DIR'], str(settings.STORES))


if __name__ == '__main__': unittest.main()
