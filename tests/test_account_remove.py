"""Removing an account: one validated write path, no interrupted request, no lost grant, never the Native login."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import account_metadata
import accounts_cli as setup
import native_login
import registry
import routing
from fakes import fixture_token, long_lived_file, refreshable_file

BASE = ('[providers.anthropic]\nkind = "anthropic"\nauth = "claude_oauth"\n\n'
        '[[providers.anthropic.accounts]]\nname = "operations"\n\n'
        '[[providers.anthropic.accounts]]\nname = "team"\n')

# Only the key names of `claude-native auth status --json` are copied; every value is a fixture.
AUTH_STATUS = {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'analyticsDisabled': False,
               'projectsDirectory': '/fixture/projects', 'configDirectory': '/fixture/config', 'email': 'ops@x.de',
               'orgId': 'org-fixture', 'orgName': 'Team A', 'subscriptionType': 'team'}


def shunt(code):
    return '#!' + sys.executable + '\nimport sys\nsys.exit(' + str(code) + ' if "check" in sys.argv else 3)\n'


class MembershipTests(unittest.TestCase):
    def test_remove_membership_round_trips_with_add_membership(self):
        self.assertEqual(setup.remove_membership(setup.add_membership(BASE, 'x'), 'x').strip(), BASE.strip())

    def test_remove_membership_keeps_every_other_account_and_setting(self):
        text = BASE + '\n[server]\nbind = "127.0.0.1:1"\n'
        value = setup.tomllib.loads(setup.remove_membership(text, 'operations'))
        self.assertEqual(value['providers']['anthropic']['accounts'], [{'name': 'team'}])
        self.assertEqual(value['server'], {'bind': '127.0.0.1:1'})
        self.assertEqual(value['providers']['anthropic']['auth'], 'claude_oauth')

    def test_remove_membership_of_an_unknown_name_changes_nothing(self):
        self.assertEqual(setup.remove_membership(BASE, 'nobody'), BASE)

    def test_removing_the_last_member_leaves_an_empty_list_that_add_can_extend(self):
        empty = setup.remove_membership(setup.remove_membership(BASE, 'operations'), 'team')
        self.assertEqual(setup.tomllib.loads(empty)['providers']['anthropic']['accounts'], [])
        again = setup.tomllib.loads(setup.add_membership(empty, 'x'))
        self.assertEqual(again['providers']['anthropic']['accounts'], [{'name': 'x'}])

    def test_remove_membership_takes_the_members_own_sub_tables_with_it(self):
        text = BASE + '[providers.anthropic.accounts.limits]\nweekly = 1\n\n[server]\nbind = "x"\n'
        value = setup.tomllib.loads(setup.remove_membership(text, 'team'))
        self.assertEqual(value['providers']['anthropic']['accounts'], [{'name': 'operations'}])
        self.assertEqual(value['server'], {'bind': 'x'})


class RoutingTests(unittest.TestCase):
    def test_in_flight_reads_busy_rows_and_active_requests(self):
        self.assertTrue(routing.in_flight({'health': {'activeRequests': 0}, 'sessions': [{'busy': True, 'account': 'ops'}]}, 'ops'))
        self.assertFalse(routing.in_flight({'health': {'activeRequests': 0}, 'sessions': [{'busy': True, 'account': 'ops'}]}, 'team'))
        self.assertTrue(routing.in_flight({'health': {'activeRequests': 2}, 'sessions': []}))
        self.assertFalse(routing.in_flight({'health': {'activeRequests': 0}, 'sessions': []}, 'ops'))
        self.assertFalse(routing.in_flight({'sessions': [{'busy': False, 'account': 'ops'}]}))

    def test_untracked_requests_count_as_busy_for_every_account(self):
        # Only main requests with a session id become busy rows; the rest show only in activeRequests.
        self.assertTrue(routing.in_flight({'health': {'activeRequests': 1}, 'sessions': []}, 'team'))
        self.assertFalse(routing.in_flight({'health': {'activeRequests': 1}, 'sessions': [{'busy': True, 'account': 'gmail'}]}, 'team'))
        self.assertTrue(routing.in_flight({'health': {'activeRequests': 2}, 'sessions': [{'busy': True, 'account': 'gmail'}]}, 'team'))

    def test_wait_idle_polls_until_idle_or_the_limit(self):
        busy = {'sessions': [{'busy': True, 'account': 'ops'}]}
        answers = iter([busy, {'sessions': []}])
        sleeps, sleeps2, sleeps3 = [], [], []
        self.assertTrue(routing.wait_idle(lambda: next(answers), 'ops', True, sleep=sleeps.append, clock=lambda: 0))
        self.assertEqual(sleeps, [5])
        self.assertFalse(routing.wait_idle(lambda: busy, 'ops', False, sleep=sleeps2.append))
        self.assertEqual(sleeps2, [])
        ticks = iter(range(0, 10000, 600))
        self.assertFalse(routing.wait_idle(lambda: busy, 'ops', True, sleep=sleeps3.append, clock=lambda: next(ticks)))
        self.assertEqual(sleeps3, [5, 5])  # clock 600 and 1200 sleep, 1800 hits the limit

    def test_an_unreadable_overview_never_counts_as_idle(self):
        def broken(): raise OSError('pool did not answer')
        self.assertFalse(routing.wait_idle(broken, 'ops', False))


class NativeStatusTests(unittest.TestCase):
    def test_parse_auth_status_reads_email_and_organization(self):
        self.assertEqual(native_login.parse_auth_status(json.dumps(AUTH_STATUS)), {'email': 'ops@x.de', 'organization': 'Team A'})

    def test_parse_auth_status_without_a_login_is_none(self):
        self.assertIsNone(native_login.parse_auth_status(json.dumps({**AUTH_STATUS, 'loggedIn': False})))
        self.assertIsNone(native_login.parse_auth_status(json.dumps({'loggedIn': False})))

    def test_parse_auth_status_refuses_output_it_cannot_read(self):
        for text in [json.dumps({'loggedIn': True}), json.dumps({'email': 'a@b.de'}), '', 'not json', '[]']:
            with self.subTest(text=text):
                with self.assertRaises(ValueError): native_login.parse_auth_status(text)


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stores = self.root / 'credentials'; self.stores.mkdir(mode=0o700)
        self.long_lived = self.root / 'claude-long-lived'; self.long_lived.mkdir(mode=0o700)
        self.runtime = self.root / 'runtime'; self.runtime.mkdir()
        self.cfg = self.root / 'cfg'; self.cfg.mkdir(mode=0o700)
        self.config = self.cfg / 'config.json'
        self.config.write_text(json.dumps({'accounts': [{'name': 'operations', 'label': 'Operations', 'alias': 'ops'},
                                                        {'name': 'team', 'label': 'Team'},
                                                        {'name': 'spare', 'label': 'Spare'}]}))
        self.reg = self.cfg / 'devices.json'
        self.backend = self.runtime / 'shunt.toml'
        self.backend.write_text(BASE + '\n[[providers.anthropic.accounts]]\nname = "spare"\n')
        self.binary = self.root / 'fake-shunt'
        self.binary.write_text(shunt(0)); self.binary.chmod(0o700)
        refreshable_file(self.stores, 'operations', 'user-o', 'org-o')
        refreshable_file(self.stores, 'spare', 'user-s', 'org-s')
        long_lived_file(self.stores, 'team', 'user-t', 'org-t')
        long_lived_file(self.long_lived, 'team', 'user-t', 'org-t')
        registry.update(lambda value: value['accounts'].extend([
            {'name': 'operations', 'email': 'ops@x.de', 'organization': 'Team A', 'userId': 'user-o', 'organizationId': 'org-o'},
            {'name': 'team', 'email': 'team@x.de', 'organization': 'Team B', 'userId': 'user-t', 'organizationId': 'org-t'}]), self.reg)

    def remove(self, name='operations', overview=None, native_identity=lambda: None, wait=False):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = setup.cli(['remove', name, *(['--wait'] if wait else [])], config=self.config, stores=self.stores,
                             binary=self.binary, runtime=self.runtime, overview=overview, long_lived=self.long_lived,
                             registry_path=self.reg, native_identity=native_identity)
        return code, out.getvalue()

    def snapshot(self):
        files = [self.config, self.backend, self.reg, *sorted(self.stores.rglob('*.json')), *sorted(self.long_lived.rglob('*.json'))]
        return {str(path): path.read_bytes() for path in files}

    def members(self):
        return [row['name'] for row in setup.tomllib.loads(self.backend.read_text())['providers']['anthropic']['accounts']]

    def test_remove_drops_membership_config_and_registry_entry(self):
        code, out = self.remove()
        self.assertEqual(code, 0)
        self.assertEqual(self.members(), ['team', 'spare'])
        self.assertEqual([row['name'] for row in json.loads(self.config.read_text())['accounts']], ['team', 'spare'])
        self.assertIsNone(registry.account(registry.load(self.reg), 'operations'))
        self.assertIsNotNone(registry.account(registry.load(self.reg), 'team'))
        self.assertIn('Removed Operations', out)
        self.assertNotIn(fixture_token('r'), out)

    def test_remove_refuses_while_a_request_is_served_by_the_account(self):
        overview = lambda: {'health': {'activeRequests': 1}, 'accounts': [], 'sessions': [{'busy': True, 'account': 'operations'}]}
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'still serving'): self.remove(overview=overview)
        self.assertEqual(self.snapshot(), before)

    def test_remove_ignores_requests_on_other_accounts(self):
        overview = lambda: {'health': {'activeRequests': 1}, 'accounts': [], 'sessions': [{'busy': True, 'account': 'team'}]}
        self.assertEqual(self.remove(overview=overview)[0], 0)

    def test_remove_with_wait_waits_for_the_account_to_go_idle(self):
        overview = lambda: {'sessions': [{'busy': True, 'account': 'operations'}]}
        with patch.object(setup.routing, 'wait_idle', return_value=True) as wait:
            self.assertEqual(self.remove(overview=overview, wait=True)[0], 0)
        wait.assert_called_once_with(overview, 'operations', True)
        before = self.snapshot()
        with patch.object(setup.routing, 'wait_idle', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'still serving'): self.remove('spare', overview=overview, wait=True)
        self.assertEqual(self.snapshot(), before)

    def test_remove_refuses_the_native_login_identity(self):
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'Native login'):
            self.remove(native_identity=lambda: {'email': 'Ops@X.de', 'organization': 'Team A'})
        self.assertEqual(self.snapshot(), before)

    def test_another_native_login_does_not_block_removal(self):
        self.assertEqual(self.remove(native_identity=lambda: {'email': 'ops@x.de', 'organization': 'Team B'})[0], 0)

    def test_native_check_uses_the_local_login_for_an_account_without_registry_entry(self):
        info = {'email': 'spare@x.de', 'organization': 'Spare Org', 'plan': 'Max', 'userId': 'user-s', 'organizationId': 'org-s'}
        with patch.object(setup, 'profile', return_value=info) as profile:
            with self.assertRaisesRegex(RuntimeError, 'Native login'):
                self.remove('spare', native_identity=lambda: {'email': 'spare@x.de', 'organization': 'Spare Org'})
        profile.assert_called_once_with(self.stores / 'spare.json')

    def test_a_long_lived_account_without_entry_never_matches_the_native_login(self):
        registry.update(lambda value: value.update(accounts=[r for r in value['accounts'] if r['name'] != 'team']), self.reg)
        with patch.object(setup, 'profile') as profile:
            self.assertFalse(setup.matches_native({'email': 'team@x.de', 'organization': 'Team B'}, 'team',
                                                  registry.load(self.reg), self.stores))
        profile.assert_not_called()

    def test_remove_keeps_refreshable_grant_aside_and_deletes_long_lived_replica(self):
        original_bytes = (self.stores / 'operations.json').read_bytes()
        self.assertEqual(self.remove('operations')[0], 0)
        retired = list((self.stores / '.retired').glob('operations-*.json'))
        self.assertEqual(len(retired), 1)
        retired_bytes = retired[0].read_bytes()
        self.assertEqual(retired_bytes, original_bytes)
        self.assertEqual((self.stores / '.retired').stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.stores / 'operations.json').exists())
        self.assertEqual(self.remove('team')[0], 0)
        self.assertFalse((self.long_lived / 'team.json').exists())
        self.assertFalse((self.stores / 'team.json').exists())
        self.assertEqual(self.members(), ['spare'])

    def test_replaced_tokens_are_kept_and_named(self):
        aside = long_lived_file(self.long_lived, 'team.json.replaced-20261017T000000Z', 'user-t', 'org-t')
        code, out = self.remove('team')
        self.assertEqual(code, 0)
        self.assertTrue(aside.exists())
        self.assertIn('team.json.replaced-20261017T000000Z', out)

    def test_backend_rejection_changes_nothing(self):
        self.binary.write_text(shunt(1))
        before = self.snapshot()
        with self.assertRaises(RuntimeError) as error: self.remove()
        self.assertEqual(str(error.exception), 'Backend rejected the removal; live membership is unchanged')
        self.assertEqual(self.snapshot(), before)

    def test_register_keeps_its_rejection_text(self):
        self.binary.write_text(shunt(1))
        paths, env = setup.local_paths_env(self.runtime, self.stores)
        info = {'email': 'f@x.de', 'organization': 'F', 'plan': 'Max', 'userId': 'user-f', 'organizationId': 'org-f'}
        with self.assertRaises(RuntimeError) as error:
            setup.register(self.config, self.stores, self.binary, paths, {'name': 'fresh', 'label': 'Fresh', 'alias': 'fresh'}, info, env)
        self.assertEqual(str(error.exception), 'Backend rejected enrollment; live membership is unchanged')

    def test_an_unanswered_overview_refuses_and_changes_nothing(self):
        def overview(): raise OSError('pool did not answer')
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'did not answer'): self.remove(overview=overview)
        self.assertEqual(self.snapshot(), before)

    def test_retire_never_unlinks_a_file_replaced_while_it_was_moved(self):
        store = self.stores / 'operations.json'
        original = store.read_bytes()
        def swap(path, target):
            # Another process installs a new login at the same path between link and unlink.
            fresh = path.with_name('fresh.tmp'); fresh.write_bytes(b'replacement'); fresh.chmod(0o600); fresh.replace(path)
        with self.assertRaisesRegex(RuntimeError, 'changed while'):
            setup.retire(self.stores, self.long_lived, 'operations', after_link=swap)
        self.assertEqual(store.read_bytes(), b'replacement')
        self.assertEqual(list((self.stores / '.retired').glob('*')), [])
        self.assertEqual(original[:1], b'{')

    def test_retire_retries_after_a_single_replacement(self):
        store = self.stores / 'operations.json'
        calls = []
        def swap_once(path, target):
            calls.append(1)
            if len(calls) == 1:
                fresh = path.with_name('fresh.tmp'); fresh.write_bytes(store.read_bytes()); fresh.chmod(0o600); fresh.replace(path)
        setup.retire(self.stores, self.long_lived, 'operations', after_link=swap_once)
        self.assertEqual(len(calls), 2)
        self.assertFalse(store.exists())
        self.assertEqual(len(list((self.stores / '.retired').glob('operations-*.json'))), 1)

    def test_unknown_account_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'No account named'): self.remove('nobody')

    def test_the_last_account_cannot_be_removed(self):
        self.assertEqual(self.remove('operations')[0], 0)
        self.assertEqual(self.remove('team')[0], 0)
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'only connected account'): self.remove('spare')
        self.assertEqual(self.snapshot(), before)

    def test_an_interrupted_removal_resumes(self):
        # Membership is gone but the files and registry entry remain: rerunning finishes the job.
        paths, env = setup.local_paths_env(self.runtime, self.stores)
        setup.unregister(self.config, self.stores, self.binary, paths, 'operations', env)
        self.assertEqual(self.remove('operations')[0], 0)
        self.assertFalse((self.stores / 'operations.json').exists())
        self.assertIsNone(registry.account(registry.load(self.reg), 'operations'))

    def test_register_still_goes_through_apply_change(self):
        self.backend.write_text(BASE)
        paths, env = setup.local_paths_env(self.runtime, self.stores)
        entry = {'name': 'fresh', 'label': 'Fresh', 'alias': 'fresh'}
        info = {'email': 'f@x.de', 'organization': 'F', 'plan': 'Max', 'userId': 'user-f', 'organizationId': 'org-f'}
        with patch.object(setup, 'apply_change', wraps=setup.apply_change) as spy:
            setup.register(self.config, self.stores, self.binary, paths, entry, info, env)
        spy.assert_called_once()
        self.assertIn('fresh', self.members())
        self.assertIn(entry, json.loads(self.config.read_text())['accounts'])

    def test_native_match_by_email_when_an_organization_is_missing(self):
        registry.update(lambda value: value['accounts'].append({'name': 'spare', 'email': 'spare@x.de'}), self.reg)
        known = registry.load(self.reg)
        with patch.object(setup, 'profile') as profile:
            self.assertTrue(setup.matches_native({'email': 'Spare@x.de', 'organization': 'Any Org'}, 'spare', known, self.stores))
            self.assertFalse(setup.matches_native({'email': 'other@x.de', 'organization': 'Any Org'}, 'spare', known, self.stores))
            # Native without an organization matches on email alone.
            self.assertTrue(setup.matches_native({'email': 'ops@x.de', 'organization': ''}, 'operations', known, self.stores))
        profile.assert_not_called()

    def test_native_check_on_an_unreadable_store_refuses(self):
        (self.stores / 'spare.json').chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'Could not read'):
            setup.matches_native({'email': 'x@x.de', 'organization': 'X'}, 'spare', registry.load(self.reg), self.stores)
        (self.stores / 'spare.json').write_text('not json')
        (self.stores / 'spare.json').chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, 'Could not read'):
            setup.matches_native({'email': 'x@x.de', 'organization': 'X'}, 'spare', registry.load(self.reg), self.stores)

    def test_device_is_a_reserved_shortcut(self):
        self.assertIn('device', account_metadata.RESERVED)


class LaunchTests(unittest.TestCase):
    def test_launch_builds_the_overview_for_remove(self):
        import launch
        with tempfile.TemporaryDirectory() as folder:
            stores = Path(folder)
            token = stores / '.pool-client-token'
            fd = os.open(token, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'w') as file: file.write('fixture-client-token-' + 'x' * 32 + '\n')
            with patch.object(launch.sys, 'argv', ['claude-pool', 'account', 'remove', 'x']), \
                 patch.object(launch, 'STORES', stores), patch.object(launch, 'request', return_value='{"sessions": []}'), \
                 patch.object(setup, 'cli', return_value=0) as cli:
                with self.assertRaises(SystemExit) as result: launch.main()
                kwargs = cli.call_args.kwargs
                self.assertIsNotNone(kwargs['overview'])
                self.assertEqual(kwargs['overview'](), {'sessions': []})
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(cli.call_args.args[0], ['remove', 'x'])
        self.assertTrue(callable(kwargs['native_identity']))

    def launch_remove(self, request, loaded, client_token=True):
        import launch
        with tempfile.TemporaryDirectory() as folder:
            stores = Path(folder)
            if client_token:
                fd = os.open(stores / '.pool-client-token', os.O_WRONLY | os.O_CREAT, 0o600)
                with os.fdopen(fd, 'w') as file: file.write('fixture-client-token-' + 'x' * 32 + '\n')
            with patch.object(launch.sys, 'argv', ['claude-pool', 'account', 'remove', 'x']), \
                 patch.object(launch, 'STORES', stores), patch.object(launch, 'request', side_effect=request), \
                 patch.object(launch.service, 'loaded', return_value=loaded), \
                 patch.object(setup, 'cli', return_value=0) as cli:
                with self.assertRaises(SystemExit): launch.main()
                overview = cli.call_args.kwargs['overview']
                # evaluate while `request` is still patched, so the check never reaches a real pool
                return overview and routing.wait_idle(overview, 'x', False)

    def test_launch_keeps_the_check_when_the_running_pool_does_not_answer(self):
        idle = self.launch_remove(OSError('no answer'), loaded=True)
        self.assertIs(idle, False)

    def test_launch_keeps_the_check_when_the_running_pool_has_no_client_token(self):
        self.assertIs(self.launch_remove(OSError('no answer'), loaded=True, client_token=False), False)
        self.assertIsNone(self.launch_remove(OSError('no answer'), loaded=False, client_token=False))

    def test_launch_skips_the_check_only_when_the_pool_is_not_running(self):
        self.assertIsNone(self.launch_remove(OSError('no answer'), loaded=False))

    def test_launch_native_identity_refuses_unreadable_status(self):
        import launch
        for done in [Mock(returncode=1, stdout=''), Mock(returncode=0, stdout='not json'), Mock(returncode=2, stdout=json.dumps(AUTH_STATUS))]:
            with self.subTest(done=done), patch.object(launch.subprocess, 'run', return_value=done):
                with self.assertRaisesRegex(RuntimeError, 'Native login'): launch.native_identity()
        logged_out = Mock(returncode=1, stdout=json.dumps({**AUTH_STATUS, 'loggedIn': False}))
        with patch.object(launch.subprocess, 'run', return_value=logged_out):
            self.assertIsNone(launch.native_identity())

    def test_launch_native_identity_reads_native_auth_status(self):
        import launch
        with patch.object(launch.subprocess, 'run', return_value=Mock(returncode=0, stdout=json.dumps(AUTH_STATUS))) as run:
            self.assertEqual(launch.native_identity(), {'email': 'ops@x.de', 'organization': 'Team A'})
        self.assertEqual(run.call_args.args[0][1:], ['auth', 'status', '--json'])
        with patch.object(launch.subprocess, 'run', side_effect=OSError('missing')):
            with self.assertRaisesRegex(RuntimeError, 'Native login'): launch.native_identity()


if __name__ == '__main__': unittest.main()
