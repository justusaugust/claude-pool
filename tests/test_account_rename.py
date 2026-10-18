"""`account rename <old> <new>`: one machine's local name for a subscription changes everywhere it is keyed, nothing is
lost, a failure puts everything back. Offline: temp dirs, a fake backend binary and a fake pool; nothing real is touched."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import accounts_cli as setup
import device_sync
import routing
import long_lived
import registry
import settings
from fakes import fixture_token, long_lived_file, refreshable_file
from test_subscription_names import FileDevice, IDS, TEAM_ROW

OLD, NEW = 'team', 'ai-vaerk-team-premium'
BASE = ('[providers.anthropic]\nkind = "anthropic"\nauth = "claude_oauth"\n\n'
        '[[providers.anthropic.accounts]]\nname = "operations"\n\n'
        '[[providers.anthropic.accounts]]\nname = "team"\nmax_utilization = 0.85\n')


def shunt(code):
    return '#!' + sys.executable + '\nimport sys\nsys.exit(' + str(code) + ' if "check" in sys.argv else 3)\n'


def fp(path): return long_lived.fingerprint(json.loads(Path(path).read_bytes()))


class RenameBase(unittest.TestCase):
    """The VPS before the rename: its own refreshable `team`, the registry (enrolled on the Mac) calls it NEW."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stores = self.root / 'credentials'; self.stores.mkdir(mode=0o700)
        self.directory = self.root / 'claude-long-lived'; self.directory.mkdir(mode=0o700)
        self.runtime = self.root / 'runtime'; self.runtime.mkdir()
        self.cfg = self.root / 'cfg'; self.cfg.mkdir(mode=0o700)
        self.config = self.cfg / 'config.json'
        self.config.write_text(json.dumps({'accounts': [
            {'name': 'operations', 'label': 'Operations', 'alias': 'ops'},
            {'name': OLD, 'label': 'AI Værk Team', 'alias': 'team', 'reserveAt': 85}]}))
        self.reg = self.cfg / 'devices.json'
        self.backend = self.runtime / 'shunt.toml'; self.backend.write_text(BASE)
        self.binary = self.root / 'fake-shunt'; self.binary.write_text(shunt(0)); self.binary.chmod(0o700)
        # The fake backend is the only command these tests run; nothing real is reachable on PATH.
        fake_bin = self.root / 'bin'; fake_bin.mkdir()
        for name in ['launchctl', 'systemctl', 'claude', 'claude-pool', 'herdr', 'tailscale', 'shunt']:
            (fake_bin / name).write_text('#!/bin/sh\nexit 97\n'); (fake_bin / name).chmod(0o755)
        guard = patch.dict(os.environ, {'PATH': str(fake_bin) + ':/usr/bin:/bin'}); guard.start(); self.addCleanup(guard.stop)
        refreshable_file(self.stores, 'operations', 'user-o', 'org-o')
        self.grant = refreshable_file(self.stores, OLD, *IDS[0]).read_bytes()
        self.token = long_lived_file(self.root / 'src', NEW, *IDS[0]).read_bytes()
        self.fp = long_lived.fingerprint(json.loads(self.token))
        registry.update(lambda value: value['accounts'].append({**TEAM_ROW, 'fingerprint': self.fp, 'reserveAt': 85}), self.reg)
        self.pool_calls = []

    def put(self, folder, name, data):
        path = folder / (name + '.json')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as file: file.write(data)
        return path

    def pool(self, flag):
        self.pool_calls.append((flag, (self.stores / (OLD + '.json')).exists()))
        return 0

    def rename(self, old=OLD, new=NEW, overview=None, wait=False, pool=None):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = setup.cli(['rename', old, new, *(['--wait'] if wait else [])], config=self.config, stores=self.stores,
                             binary=self.binary, runtime=self.runtime, overview=overview, long_lived=self.directory,
                             registry_path=self.reg, pool=pool or self.pool)
        return code, out.getvalue()

    def snapshot(self):
        files = [self.config, self.backend, self.reg, *sorted(self.runtime.rglob('*.json')),
                 *sorted(self.stores.rglob('*.json')), *sorted(self.directory.rglob('*.json'))]
        return {str(path.relative_to(self.root)): path.read_bytes() for path in files}

    def members(self):
        return setup.tomllib.loads(self.backend.read_text())['providers']['anthropic']['accounts']

    def names(self): return [row['name'] for row in json.loads(self.config.read_text())['accounts']]

    def copies_of(self, fingerprint):
        return sorted(str(p.relative_to(self.root)) for folder in (self.stores, self.directory) for p in folder.rglob('*.json')
                      if setup.stored_kind(p) is not None and fp(p) == fingerprint)


class RenameTests(RenameBase):
    def test_a_stopped_pool_renames_the_files_the_config_the_runtime_config_and_the_state(self):
        (self.runtime / 'long-lived-probes.json').write_text(json.dumps({OLD: {'at': 1, 'fingerprint': 'abc', 'result': 'ok'}}))
        (self.runtime / 'shared-readings.json').write_text(json.dumps(
            {'version': 1, 'device': 'mac', 'at': 5, 'accounts': {OLD: {'fiveHour': None, 'sevenDay': None, 'fableWeekly': None, 'seenAt': 4}}}))
        (self.runtime / 'sessions.json').write_text(json.dumps({'version': 1, 'paused': [OLD], 'traffic': [{'at': 1, 'account': OLD, 'workload': 'opus/high'}],
            'observations': {OLD: [{'at': 1}]}, 'sessions': {'s1': {'at': 1, 'pin': OLD, 'active': OLD, 'mode': 'pinned',
            'routing': {'account': OLD, 'reason': 'manual selection', 'peers': 0}, 'history': [{'from': 'operations', 'to': OLD, 'at': 1, 'reason': 'manual selection'}]}}}))
        code, out = self.rename()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.stores / (NEW + '.json')).read_bytes(), self.grant)
        self.assertFalse((self.stores / (OLD + '.json')).exists())
        rows = json.loads(self.config.read_text())['accounts']
        self.assertEqual(rows[1], {'name': NEW, 'label': 'AI Værk Team', 'alias': 'team', 'reserveAt': 85})
        self.assertEqual(self.members(), [{'name': 'operations'}, {'name': NEW, 'max_utilization': 0.85}])
        self.assertEqual(list(json.loads((self.runtime / 'long-lived-probes.json').read_text())), [NEW])
        self.assertEqual(list(json.loads((self.runtime / 'shared-readings.json').read_text())['accounts']), [NEW])
        sessions = json.loads((self.runtime / 'sessions.json').read_text())
        self.assertEqual((sessions['paused'], sessions['traffic'][0]['account'], list(sessions['observations'])), ([NEW], NEW, [NEW]))
        one = sessions['sessions']['s1']
        self.assertEqual((one['pin'], one['active'], one['routing']['account'], one['history'][0]['to'], one['history'][0]['from']),
                         (NEW, NEW, NEW, NEW, 'operations'))
        for path in self.runtime.glob('*.json'): self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
        self.assertEqual(self.pool_calls, [])  # a stopped pool is just renamed
        self.assertEqual(registry.account(registry.load(self.reg), NEW)['fingerprint'], self.fp)  # already the registry name
        self.assertIn('Renamed team to ' + NEW, out); self.assertIn('registry already', out)
        self.assertNotIn(fixture_token(), out); self.assertNotIn(fixture_token('r'), out)

    def test_the_stray_copy_before_a_sync_ends_as_one_long_lived_token_under_the_new_name(self):
        # The VPS today: the synced token sits under NEW in both folders, next to its own refreshable `team`.
        self.put(self.directory, NEW, self.token); self.put(self.stores, NEW, self.token)
        code, out = self.rename()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.stores / (NEW + '.json')).read_bytes(), self.grant)
        self.assertEqual((self.directory / (NEW + '.json')).read_bytes(), self.token)
        self.assertEqual(self.copies_of(self.fp), ['claude-long-lived/' + NEW + '.json'])  # the duplicate went, one copy stays
        self.assertEqual(sorted(p.name for p in self.stores.glob('*.json')), [NEW + '.json', 'operations.json'])
        self.assertEqual(self.names(), ['operations', NEW])

    def test_the_synced_replica_after_a_sync_follows_the_rename(self):
        self.put(self.directory, OLD, self.token)
        code, out = self.rename()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.directory / (NEW + '.json')).read_bytes(), self.token)
        self.assertFalse((self.directory / (OLD + '.json')).exists())
        self.assertEqual(self.copies_of(self.fp), ['claude-long-lived/' + NEW + '.json'])

    def test_the_only_copy_of_a_token_is_moved_aside_never_removed(self):
        self.put(self.directory, OLD, self.token)
        other = long_lived_file(self.stores, NEW, *IDS[0], token=fixture_token('b'))
        other_fp = fp(other)
        code, out = self.rename()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.directory / (NEW + '.json')).read_bytes(), self.token)
        self.assertEqual((self.stores / (NEW + '.json')).read_bytes(), self.grant)
        kept = self.copies_of(other_fp)
        self.assertEqual(len(kept), 1); self.assertTrue(kept[0].startswith('credentials/.retired/'), kept)
        self.assertIn('.retired', out)

    def test_refusals_change_nothing(self):
        cases = {
            'a connected account': ('operations', 'already'),
            'not a valid name': ('Team Premium', 'lowercase'),
            'the same name': (OLD, 'already'),
        }
        before = self.snapshot()
        for label, (new, message) in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(RuntimeError, message): self.rename(new=new)
                self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(RuntimeError, 'No account named'): self.rename(old='nobody', new='x')
        self.assertEqual(self.snapshot(), before)

    def test_a_token_of_another_subscription_under_the_new_name_refuses(self):
        for folder in (self.stores, self.directory):
            with self.subTest(str(folder.name)):
                path = long_lived_file(folder, NEW, 'user-x', 'org-x')
                before = self.snapshot()
                with self.assertRaisesRegex(RuntimeError, 'another subscription'): self.rename()
                self.assertEqual(self.snapshot(), before)
                path.unlink()

    def test_the_registry_name_of_another_subscription_refuses(self):
        registry.update(lambda value: value['accounts'].append({'name': 'other', 'userId': 'user-x', 'organizationId': 'org-x'}), self.reg)
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'another subscription'): self.rename(new='other')
        self.assertEqual(self.snapshot(), before)

    def test_a_running_pool_refuses_while_the_account_serves_a_request(self):
        overview = lambda: {'health': {'activeRequests': 1}, 'sessions': [{'busy': True, 'account': OLD}]}
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'still serving'): self.rename(overview=overview)
        self.assertEqual(self.snapshot(), before); self.assertEqual(self.pool_calls, [])

    def test_wait_waits_for_the_account_and_then_for_the_pool(self):
        overview = lambda: {'health': {'activeRequests': 0}, 'sessions': []}
        with patch.object(setup.routing, 'wait_idle', return_value=True) as wait:
            self.assertEqual(self.rename(overview=overview, wait=True)[0], 0)
        self.assertEqual([c.args for c in wait.call_args_list], [(overview, OLD, True), (overview, None, True)])

    def test_a_running_pool_is_stopped_renamed_and_started_again(self):
        overview = lambda: {'health': {'activeRequests': 0}, 'sessions': []}
        code, out = self.rename(overview=overview)
        self.assertEqual(code, 0, out)
        # Stopped while the old files were still there, started once they were renamed.
        self.assertEqual(self.pool_calls, [('--stop', True), ('--start', False)])
        self.assertIn('stopped the pool', out.lower()); self.assertIn('started it again', out.lower())

    def test_a_pool_that_does_not_stop_changes_nothing(self):
        overview = lambda: {'health': {'activeRequests': 0}, 'sessions': []}
        before = self.snapshot()
        code, out = self.rename(overview=overview, pool=lambda flag: (self.pool_calls.append(flag), 1)[1])
        self.assertEqual(code, 1); self.assertIn('did not stop', out)
        self.assertEqual(self.snapshot(), before); self.assertEqual(self.pool_calls, ['--stop'])

    def test_a_failure_after_the_first_move_puts_everything_back(self):
        self.put(self.directory, NEW, self.token); self.put(self.stores, NEW, self.token)
        (self.runtime / 'long-lived-probes.json').write_text(json.dumps({OLD: {'at': 1, 'fingerprint': 'abc', 'result': 'ok'}}))
        before = self.snapshot()
        for step in ('apply_change', '_rename_state'):
            with self.subTest(step):
                overview = lambda: {'health': {'activeRequests': 0}, 'sessions': []}
                self.pool_calls = []
                with patch.object(setup, step, side_effect=RuntimeError('boom')):
                    with self.assertRaisesRegex(RuntimeError, 'boom.*put back'): self.rename(overview=overview)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual([flag for flag, _ in self.pool_calls], ['--stop', '--start'])
                self.assertEqual(list(self.stores.glob('.rename-*')) + list(self.directory.glob('.rename-*')), [])


    def test_a_new_name_with_a_removal_marker_refuses(self):
        registry.update(lambda value: value['accounts'].append({'name': 'jamie-gmail', 'userId': 'user-g', 'organizationId': 'org-g',
                                                               'fingerprint': 'f00d00000000'}), self.reg)
        registry.update(lambda value: value.update(accounts=[r for r in value['accounts'] if r['name'] != 'jamie-gmail']), self.reg)
        before = self.snapshot()
        with self.assertRaises(RuntimeError) as caught: self.rename(new='jamie-gmail')
        self.assertIn('removal marker for jamie-gmail', str(caught.exception)); self.assertIn('f00d00000000', str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_an_interrupt_inside_the_rename_puts_everything_back_and_restarts_the_pool(self):
        self.put(self.directory, NEW, self.token); self.put(self.stores, NEW, self.token)
        overview = lambda: {'health': {'activeRequests': 0}, 'sessions': []}
        before = self.snapshot()
        for error in (KeyboardInterrupt, SystemExit):
            with self.subTest(error.__name__):
                self.pool_calls = []
                with patch.object(setup, '_rename_state', side_effect=error()):
                    with self.assertRaises(error): self.rename(overview=overview)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual([flag for flag, _ in self.pool_calls], ['--stop', '--start'])
                self.assertEqual(list(self.stores.glob('.rename-*')), [])

class RegistryRenameTests(RenameBase):
    """The Mac: it enrolled the subscription, so the registry row carries its local name and is renamed with it."""
    def setUp(self):
        super().setUp()
        registry.update(lambda value: value.update(accounts=[]), self.reg)
        (self.stores / (OLD + '.json')).unlink()
        self.put(self.stores, OLD, self.token); self.put(self.directory, OLD, self.token)
        registry.update(lambda value: value['accounts'].append({**TEAM_ROW, 'name': OLD, 'fingerprint': self.fp, 'reserveAt': 85}), self.reg)

    def test_the_registry_row_follows_and_leaves_a_tombstone(self):
        generation = registry.load(self.reg)['generation']
        code, out = self.rename(new='team-premium')
        self.assertEqual(code, 0, out)
        value = registry.load(self.reg)
        self.assertIsNone(registry.account(value, OLD))
        row = registry.account(value, 'team-premium')
        self.assertEqual((row['label'], row['alias'], row['fingerprint'], row['reserveAt']), ('AI Værk Team', 'team', self.fp, 85))
        self.assertEqual(value['generation'], generation + 1)
        self.assertIn({'kind': 'account', 'name': OLD, 'generation': generation + 1, 'fingerprint': self.fp}, value['removed'])
        self.assertIn('registry', out)

    def test_a_registry_failure_puts_everything_back(self):
        before = self.snapshot()
        with patch.object(setup.registry, 'update', side_effect=ValueError('registry is locked')):
            with self.assertRaisesRegex(RuntimeError, 'put back'): self.rename(new='team-premium')
        self.assertEqual(self.snapshot(), before)


class PeerTests(FileDevice):
    """A peer that holds the subscription under the name the registry used before the rename keeps it: a rename is not a
    removal, and every device keeps its own local name."""
    def test_a_renamed_registry_row_never_removes_the_account_on_a_peer(self):
        # This device holds it as `ai-vaerk-team-premium` (the old registry name), served from the synced long-lived token.
        (settings.STORES / 'team.json').unlink()
        self.config.write_text(json.dumps({'accounts': [{'name': 'operations', 'label': 'Operations'},
                                                        {'name': NEW, 'label': 'AI Værk Team', 'alias': 'team'}]}))
        refreshable_file(settings.STORES, 'operations', 'user-o', 'org-o')
        for folder in (settings.STORES, settings.LONG_LIVED):
            path = folder / (NEW + '.json')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as file: file.write(self.blob)
        # The enrolling device renamed its row: tombstone for NEW, the same subscription and token under `team-premium`.
        theirs = registry.load(self.reg)
        mac = self.cfg / 'mac-devices.json'
        mac.write_text(json.dumps(theirs)); mac.chmod(0o600)
        registry.update(lambda value: registry.account(value, NEW).update(name='team-premium'), mac)
        report = device_sync.receive_registry(mac.read_bytes(), overview=lambda: {})
        self.assertEqual(report['removed'], []); self.assertIn(NEW, report['kept'])
        self.assertEqual(self.config_names(), ['operations', NEW])
        self.assertTrue((settings.STORES / (NEW + '.json')).exists()); self.assertTrue((settings.LONG_LIVED / (NEW + '.json')).exists())
        # The rename's tombstone stays (a peer that missed the rename must not bring the old row back), but once this
        # device has it, every later sync is quiet about it.
        again = device_sync.receive_registry(mac.read_bytes(), overview=lambda: {})
        self.assertEqual((again['result'], again['removed'], again['kept']), ('unchanged', [], []))
        self.assertEqual(self.config_names(), ['operations', NEW])


class EnrollAfterRenameTests(RenameBase):
    """A long-lived enrollment on a machine whose local name differs from the registry's: refused with the rename command,
    and working under the registry name after the rename, even with the enrolling machine gone."""
    INFO = {'email': 'team@x.de', 'organization': 'Team A', 'plan': 'Team', 'userId': IDS[0][0], 'organizationId': IDS[0][1]}

    def add(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out, patch.object(setup, 'profile', return_value=dict(self.INFO)), \
                patch.object(setup, 'login', side_effect=AssertionError('no browser in this test')):
            code = setup.cli(['add', 'team@x.de', '--long-lived', *args], config=self.config, stores=self.stores, binary=self.binary,
                             runtime=self.runtime, long_lived=self.directory, registry_path=self.reg,
                             lookup=lambda *ids: None)  # the enrolling machine is gone: nobody answers
        return code, out.getvalue()

    def test_the_local_name_refuses_with_the_registry_name_and_the_rename_command(self):
        self.put(self.directory, OLD, self.token)
        with self.assertRaises(RuntimeError) as caught: self.add('--name', OLD)
        self.assertIn(NEW, str(caught.exception))
        self.assertIn('claude-pool account rename team ' + NEW, str(caught.exception))

    def test_the_registry_name_before_the_rename_refuses_with_the_rename_command(self):
        self.put(self.directory, NEW, self.token)
        with self.assertRaises(RuntimeError) as caught: self.add('--name', NEW)
        self.assertIn('claude-pool account rename team ' + NEW, str(caught.exception))

    def test_after_the_rename_the_registry_name_enrolls_without_the_enrolling_machine(self):
        self.put(self.directory, OLD, self.token)
        self.assertEqual(self.rename()[0], 0)
        code, out = self.add('--name', NEW)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.names(), ['operations', NEW])
        new = long_lived_file(self.root / 'staged', 'new', *IDS[0], token=fixture_token('n'))
        code, out = self.add('--replace', '--from-file', str(new), '--name', NEW)
        self.assertEqual(code, 0, out)
        self.assertEqual(fp(self.directory / (NEW + '.json')), registry.account(registry.load(self.reg), NEW)['fingerprint'])
        self.assertNotEqual(fp(self.directory / (NEW + '.json')), self.fp)
        self.assertEqual((self.stores / (NEW + '.json')).read_bytes(), self.grant)  # its own refreshable login stays its store
        self.assertNotIn(fixture_token('n'), out)


class RemoveUnderLocalNameTests(RenameBase):
    """`account remove` of a subscription this machine holds under its own name finds the registry row by UUIDs."""
    def test_removing_the_local_name_removes_the_registry_row_of_the_subscription(self):
        self.put(self.directory, OLD, self.token)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = setup.cli(['remove', OLD], config=self.config, stores=self.stores, binary=self.binary, runtime=self.runtime,
                             long_lived=self.directory, registry_path=self.reg, native_identity=lambda: None)
        self.assertEqual(code, 0, out.getvalue())
        value = registry.load(self.reg)
        self.assertIsNone(registry.account(value, NEW))
        self.assertIn({'kind': 'account', 'name': NEW, 'generation': value['generation'], 'fingerprint': self.fp}, value['removed'])
        self.assertEqual(self.names(), ['operations'])
        self.assertIn('Other devices drop it', out.getvalue()); self.assertIn('registry: ' + NEW, out.getvalue())


class LaunchTests(unittest.TestCase):
    def launch_rename(self, request, loaded, argv=('rename', 'team', NEW), marker=False, client_token=True):
        import launch, devices_cli
        with tempfile.TemporaryDirectory() as folder:
            stores = Path(folder) / 'stores'; stores.mkdir()
            runtime = Path(folder) / 'runtime'; runtime.mkdir()
            if marker: (runtime / 'maintenance').write_text('Deliberately stopped\n')  # what `claude-pool --stop` leaves
            if client_token:
                fd = os.open(stores / '.pool-client-token', os.O_WRONLY | os.O_CREAT, 0o600)
                with os.fdopen(fd, 'w') as file: file.write('fixture-client-token-' + 'x' * 32 + '\n')
            def refused(*args, **kwargs): raise ConnectionRefusedError()  # no pool port answers; the real ports are never asked
            with patch.object(launch.sys, 'argv', ['claude-pool', 'account', *argv]), \
                 patch.object(launch, 'STORES', stores), patch.object(launch, 'request', side_effect=request), \
                 patch.object(settings, 'RUNTIME', runtime), patch.object(devices_cli.socket, 'create_connection', refused), \
                 patch.object(launch.service, 'loaded', loaded if callable(loaded) else (lambda label: loaded)), \
                 patch.object(setup, 'cli', return_value=0) as cli:
                with self.assertRaises(SystemExit): launch.main()
                kwargs = cli.call_args.kwargs
                overview = kwargs['overview']
                idle = overview and routing.wait_idle(overview, 'team', False)
                self.assertEqual((stores / '.pool-client-token').exists(), client_token)  # the check never mints a token
                return kwargs, idle

    def test_a_pool_stopped_with_stop_is_stopped_although_its_service_is_loaded(self):
        import service
        real = service.loaded
        answers = {'darwin': lambda argv: argv[:2] == ['launchctl', 'print'],          # the job stays bootstrapped
                   'linux': lambda argv: argv[:3] == ['systemctl', '--user', 'is-enabled']}  # enabled, not active
        for platform, ok in answers.items():
            with self.subTest(platform):
                calls = []
                def fake(argv, **kwargs):
                    calls.append(argv)
                    return type('Done', (), {'returncode': 0 if ok(argv) else 3})()
                with patch.object(service, 'platform', return_value=platform):
                    loaded = lambda label: real(label, run=fake)
                    self.assertTrue(loaded('io.github.claude-pool'))
                    self.assertIsNone(self.launch_rename(OSError('no answer'), loaded, marker=True)[1])  # just renamed
                    # Without the marker the same loaded service that does not answer still counts as busy.
                    self.assertIs(self.launch_rename(OSError('no answer'), loaded)[1], False)

    def test_launch_passes_the_overview_and_the_restart_path(self):
        kwargs, idle = self.launch_rename(lambda *a, **k: '{"sessions": []}', loaded=True)
        self.assertTrue(idle); self.assertTrue(callable(kwargs['pool']))

    def test_a_running_pool_that_does_not_answer_counts_as_busy(self):
        self.assertIs(self.launch_rename(OSError('no answer'), loaded=True)[1], False)

    def test_a_running_pool_without_a_client_token_counts_as_busy(self):
        # The service manager says the pool runs: without its token nobody can ask it, so files never move under it.
        self.assertIs(self.launch_rename(OSError('no answer'), loaded=True, client_token=False)[1], False)
        self.assertIsNone(self.launch_rename(OSError('no answer'), loaded=False, client_token=False)[1])

    def test_a_stopped_pool_has_no_overview(self):
        self.assertIsNone(self.launch_rename(OSError('no answer'), loaded=False)[1])

    def test_help_account_names_rename(self):
        import launch
        out = io.StringIO()
        with patch.object(sys, 'argv', ['claude-pool', 'help', 'account']), patch.object(launch, 'token') as token, \
                contextlib.redirect_stdout(out):
            launch.main()
        token.assert_not_called()
        self.assertEqual(out.getvalue(), setup.ACCOUNT_HELP + '\n')
        for words in ('account rename <old> <new> [--wait]', 'account add', 'account remove', 'account reserve', 'label and shortcut stay'):
            self.assertIn(words, setup.ACCOUNT_HELP)
        usage = (Path(__file__).resolve().parents[1] / 'docs/USAGE.md').read_text()
        self.assertIn('claude-pool account rename <old> <new> [--wait]', usage)
        self.assertIn('account rename', (Path(__file__).resolve().parents[1] / 'docs/DEVICES.md').read_text())


if __name__ == '__main__': unittest.main()
