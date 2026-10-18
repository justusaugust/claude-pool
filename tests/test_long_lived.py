"""Long-lived (setup-token) store files are accepted without changing how refreshable ones are read."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import accounts_cli as setup
from account_metadata import load
import bridge
import long_lived
from native_auth import NativeAuth
import private_files
import settings
from fakes import fixture_token, long_lived_file, refreshable_file

NOW = 1_800_000_000_000
DAY = 86_400_000


def mode(path): return path.stat().st_mode & 0o777


class LongLivedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stores = self.root / 'credentials'; self.stores.mkdir(mode=0o700)
        self.long_lived = self.root / 'claude-long-lived'
        self.config = self.root / 'config.json'
        self.runtime_toml = self.root / 'shunt.toml'
        self.runtime_toml.write_text('[providers.anthropic]\nauth="claude_oauth"\naccounts=[]\n')
        self.binary = self.root / 'fake-shunt'
        self.binary.write_text('#!/bin/sh\nexit 0\n'); self.binary.chmod(0o700)

    def info(self, user, org):
        return {'email': 'x@example.test', 'organization': 'Example', 'plan': 'Team', 'userId': user, 'organizationId': org}

    def register(self, name, info):
        setup.register(self.config, self.stores, self.binary, [self.runtime_toml],
                       {'name': name, 'label': name.title(), 'alias': name}, info, {})

    def long_lived_value(self, **changes):
        value = json.loads(long_lived_file(self.root / 'variants', 'v').read_text())
        return value | changes

    def test_refreshable_store_is_read_exactly_as_before(self):
        path = refreshable_file(self.stores, 'operations', 'user-a', 'team-a')
        self.assertEqual(setup.credential(path), json.loads(path.read_text()))
        self.assertEqual(setup.refreshable(path), json.loads(path.read_text()))
        self.assertEqual(long_lived.kind(setup.credential(path)), 'refreshable')
        path.chmod(0o640)
        with self.assertRaisesRegex(RuntimeError, 'private 0600'): setup.credential(path)

    def test_long_lived_store_is_accepted_and_classified(self):
        self.assertEqual(long_lived.kind(setup.credential(long_lived_file(self.stores, 'team'))), 'long-lived')

    def test_setup_token_with_refresh_token_or_missing_uuids_is_rejected(self):
        base = self.long_lived_value()
        with_refresh_token = copy.deepcopy(base); with_refresh_token['claudeAiOauth']['refreshToken'] = 'fixture-refresh'
        without_account_uuid = copy.deepcopy(base); del without_account_uuid['shuntAccountUuid']
        without_org_uuid = copy.deepcopy(base); without_org_uuid['shuntOrganizationUuid'] = ''
        without_access_token = copy.deepcopy(base); del without_access_token['claudeAiOauth']['accessToken']
        for variant in [with_refresh_token, without_account_uuid, without_org_uuid, without_access_token]:
            with self.subTest(variant=sorted(variant)), self.assertRaisesRegex(RuntimeError, 'Unsupported credential format'):
                long_lived.kind(variant)

    def test_files_that_are_neither_kind_are_rejected(self):
        for oauth in [{}, {'accessToken': 'a'}, {'refreshToken': 'r'}, {'accessToken': '', 'refreshToken': 'r'}]:
            with self.subTest(oauth=sorted(oauth)), self.assertRaisesRegex(RuntimeError, 'Unsupported credential format'):
                long_lived.kind({'claudeAiOauth': oauth})
        with self.assertRaisesRegex(RuntimeError, 'Unsupported credential format'): long_lived.kind({'claudeAiOauth': []})

    def test_profile_still_requires_a_refreshable_login(self):
        with self.assertRaisesRegex(RuntimeError, 'refreshable'): setup.profile(long_lived_file(self.stores, 'team'))

    def test_register_with_one_long_lived_store_keeps_other_accounts_enrollable(self):
        long_lived_file(self.stores, 'team', user='user-t', org='org-t')
        self.config.write_text(json.dumps({'accounts': [{'name': 'team', 'label': 'Team'}]}))
        refreshable_file(self.stores, 'operations', 'user-a', 'team-a')
        self.register('operations', self.info('user-a', 'team-a'))
        self.assertEqual([a['name'] for a in load(self.config)['accounts']], ['team', 'operations'])
        self.assertIn('name = "operations"', self.runtime_toml.read_text())

    def test_register_rejects_a_second_alias_of_a_long_lived_identity(self):
        long_lived_file(self.stores, 'team', user='user-t', org='org-t')
        self.config.write_text(json.dumps({'accounts': [{'name': 'team', 'label': 'Team'}]}))
        refreshable_file(self.stores, 'team2', 'user-t', 'org-t')
        before = self.config.read_bytes(), self.runtime_toml.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'already connected'): self.register('team2', self.info('user-t', 'org-t'))
        self.assertEqual((self.config.read_bytes(), self.runtime_toml.read_bytes()), before)

    def test_native_auth_maps_long_lived_identities(self):  # regression guard: passes before and after
        long_lived_file(self.stores, 'team', user='user-t', org='org-t')
        self.assertEqual(NativeAuth(self.stores, ['team']).identities, {('user-t', 'org-t'): 'team'})

    def test_fingerprint_is_stable_short_and_not_a_substring_of_the_token(self):
        value = json.loads(long_lived_file(self.stores, 'team').read_text()); fp = long_lived.fingerprint(value)
        self.assertRegex(fp, r'^[0-9a-f]{12}$'); self.assertNotIn(fp, fixture_token()); self.assertEqual(fp, long_lived.fingerprint(value))
        other = json.loads(long_lived_file(self.stores, 'other', token=fixture_token('b')).read_text())
        self.assertNotEqual(long_lived.fingerprint(other), fp)

    def test_days_left_from_expires_at(self):
        value_expiring_in = lambda days: json.loads(long_lived_file(self.stores, 'team', expires_ms=NOW + days * DAY).read_text())
        value_without_expiry = json.loads(long_lived_file(self.stores, 'bare').read_text())
        self.assertEqual(long_lived.expires_at_ms(value_expiring_in(10)), NOW + 10 * DAY)
        self.assertTrue(9.99 < long_lived.days_left(value_expiring_in(10), NOW) <= 10)
        self.assertLess(long_lived.days_left(value_expiring_in(-2), NOW), 0)
        self.assertIsNone(long_lived.expires_at_ms(value_without_expiry))
        self.assertIsNone(long_lived.days_left(value_without_expiry, NOW))

    def test_write_private_refuses_a_shared_parent_and_writes_0600(self):
        open_dir = self.root / 'open'; open_dir.mkdir(); open_dir.chmod(0o755)
        with self.assertRaises(RuntimeError): private_files.write_private(open_dir / 'x.json', b'{}')
        self.assertFalse((open_dir / 'x.json').exists())
        private_files.write_private(self.long_lived / 'x.json', b'{}'); self.assertEqual(mode(self.long_lived / 'x.json'), 0o600)
        self.assertEqual(mode(self.long_lived), 0o700)
        self.assertEqual(private_files.read_private(self.long_lived / 'x.json'), {})
        private_files.write_private(self.long_lived / 'x.json', '{"a": 1}')
        self.assertEqual(private_files.read_private(self.long_lived / 'x.json'), {'a': 1})

    def test_bridge_atomic_write_is_the_shared_helper(self):
        self.assertIs(bridge.atomic_write, private_files.atomic_write)
        self.assertIs(settings.private_directory, private_files.private_directory)
        self.assertEqual(settings.LONG_LIVED, settings.STORES.parent / 'claude-long-lived')


if __name__ == '__main__': unittest.main()
