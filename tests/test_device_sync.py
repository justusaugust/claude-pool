"""Accounts and the registry flow between devices; a non-owner switches to long-lived tokens. Offline, fakes only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import device_sync
import diagnostics
import long_lived
import registry
import settings
from remote import Completed
from fakes import FakeRemote, SecretWatch, fixture_token, long_lived_file, refreshable_file

MAC = {'name': 'mac', 'ssh': 'alex@mac', 'platform': 'darwin', 'roles': [], 'addedAt': 1}
VPS = {'name': 'vps', 'ssh': 'alex@vps', 'platform': 'linux', 'roles': ['usage'], 'addedAt': 2}
BASE = ('[providers.anthropic]\nkind = "anthropic"\nauth = "claude_oauth"\n\n'
        '[[providers.anthropic.accounts]]\nname = "personal"\n')
ROW = {'name': 'team', 'label': 'Team', 'alias': 'team', 'plan': 'Team', 'email': 'team@x.de', 'organization': 'Team A',
       'userId': 'user-a', 'organizationId': 'team-a'}


def mode(path): return path.stat().st_mode & 0o777


def ok(value=b''): return lambda argv, stdin: Completed(0, value, b'')


class Pipe(io.BytesIO):
    def isatty(self): return False


class Tty(io.BytesIO):
    def isatty(self): return True


class DeviceSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stores = self.root / 'credentials'; self.stores.mkdir(mode=0o700)
        self.directory = self.root / 'claude-long-lived'; self.directory.mkdir(mode=0o700)
        self.runtime = self.root / 'runtime'; self.runtime.mkdir()
        self.cfg = self.root / 'cfg'; self.cfg.mkdir(mode=0o700)
        self.config = self.cfg / 'config.json'
        self.config.write_text(json.dumps({'accounts': [{'name': 'personal', 'label': 'Personal'}]}))
        self.reg = self.cfg / 'devices.json'; self.self_path = self.cfg / 'device.json'
        self.runtime_toml = self.runtime / 'shunt.toml'; self.runtime_toml.write_text(BASE)
        self.here = self.root / 'here'; (self.here / 'bin').mkdir(parents=True)
        shunt = self.here / 'bin/shunt'
        shunt.write_text('#!' + sys.executable + '\nimport sys\nsys.exit(0 if "check" in sys.argv else 3)\n'); shunt.chmod(0o700)
        # Fake service managers: running one would leave a marker.
        self.bin = self.root / 'bin'; self.bin.mkdir(); self.marker = self.root / 'service-called'
        for name in ['launchctl', 'systemctl']:
            (self.bin / name).write_text('#!/bin/sh\necho "$0" >> ' + str(self.marker) + '\nexit 0\n'); (self.bin / name).chmod(0o755)
        patches = [patch.object(settings, key, value) for key, value in
                   [('STORES', self.stores), ('LONG_LIVED', self.directory), ('RUNTIME', self.runtime), ('CONFIG', self.config),
                    ('REGISTRY', self.reg), ('SELF', self.self_path), ('HERE', self.here)]]
        patches += [patch.dict(os.environ, {'PATH': str(self.bin) + os.pathsep + os.environ['PATH']}),
                    patch.object(device_sync, 'default_native_identity', lambda: None),
                    patch.object(device_sync, 'STATUS_PARTS', []), patch.object(device_sync, 'INTEGRATION_HOOKS', [])]
        for item in patches: item.start(); self.addCleanup(item.stop)
        registry.set_self('mac', self.self_path)

    def devices(self, *rows, accounts=()):
        registry.update(lambda value: (value['devices'].extend(rows), value['accounts'].extend(accounts)), self.reg)

    def blob(self, name='team', **kwargs):
        path = long_lived_file(self.root / 'src', name, **{'user': 'user-a', 'org': 'team-a', **kwargs})
        return path.read_bytes()

    def config_names(self): return [r['name'] for r in json.loads(self.config.read_text())['accounts']]

    def test_build_version_is_the_source_version_so_mac_and_linux_builds_compare_equal(self):
        (self.root / 'build-info.json').write_text(json.dumps({'version': 'withbackend', 'sourceVersion': 'sourceonly'}))
        self.assertEqual(device_sync.build_version(), 'sourceonly')
        self.assertEqual(device_sync.status()['version'], 'sourceonly')

    def test_send_account_refuses_a_terminal_and_refreshable_files(self):
        long_lived_file(self.directory, 'team')
        refreshable_file(self.directory, 'ops-refreshable', 'user-o', 'org-o')
        with self.assertRaises(RuntimeError): device_sync.send_account('team', out=Tty())
        with self.assertRaises(RuntimeError): device_sync.send_account('ops-refreshable', out=Pipe())
        out = Pipe(); device_sync.send_account('team', out=out)
        self.assertEqual(out.getvalue(), (self.directory / 'team.json').read_bytes())

    def test_receive_account_writes_0600_and_registers_membership_on_a_non_owner(self):
        self.devices(MAC, VPS, accounts=[ROW])
        self.assertEqual(device_sync.receive_account('team', self.blob()), 'installed')
        self.assertIn('name = "team"', self.runtime_toml.read_text())
        self.assertIn('team', self.config_names())
        self.assertEqual(mode(self.stores / 'team.json'), 0o600); self.assertEqual(mode(self.directory / 'team.json'), 0o600)
        self.assertEqual(device_sync.receive_account('team', self.blob()), 'unchanged')

    def test_receive_account_applies_the_registry_reserve(self):
        self.devices(MAC, VPS, accounts=[{**ROW, 'reserveAt': 85}])
        self.assertEqual(device_sync.receive_account('team', self.blob()), 'installed')
        self.assertEqual(next(r for r in json.loads(self.config.read_text())['accounts'] if r['name'] == 'team')['reserveAt'], 85)
        self.assertIn('max_utilization = 0.85', self.runtime_toml.read_text())

    def test_a_rejected_reserve_is_reported_but_the_install_stands(self):
        self.devices(MAC, VPS, accounts=[{**ROW, 'reserveAt': 85}])
        shunt = self.here / 'bin/shunt'
        shunt.write_text('#!' + sys.executable + '\nimport sys\nsys.exit(1 if "max_utilization" in open(sys.argv[2]).read() else 0 if "check" in sys.argv else 3)\n')
        journal = Mock()
        result = device_sync.receive_account('team', self.blob(), journal=journal)
        self.assertEqual(result, 'installed')
        journal.record.assert_called_once_with('reserve_not_applied', account='team')
        self.assertIn('reserve_not_applied', diagnostics.CODES)
        self.assertIn('team', self.config_names())
        self.assertNotIn('max_utilization', self.runtime_toml.read_text())

    def test_a_failing_reserve_hook_is_reported_by_receive_registry(self):
        import accounts_cli
        self.devices(MAC, VPS, accounts=[{**ROW, 'reserveAt': 0}])
        accounts_cli.register_local({'name': 'team', 'label': 'Team'}, {'userId': 'u', 'organizationId': 'o'})
        shunt = self.here / 'bin/shunt'
        shunt.write_text('#!' + sys.executable + '\nimport sys\nsys.exit(1 if "max_utilization" in open(sys.argv[2]).read() else 0 if "check" in sys.argv else 3)\n')
        device_sync.INTEGRATION_HOOKS[:] = [accounts_cli.reserve_hook]
        report = device_sync.receive_registry(self.newer([{**ROW, 'reserveAt': 85}]))
        self.assertTrue(report['hooks']['reserve_hook'].startswith('failed'))

    def test_receive_account_without_a_usage_owner_installs(self):
        self.devices(MAC, accounts=[ROW])
        self.assertEqual(device_sync.receive_account('team', self.blob()), 'installed')

    def test_receive_account_on_the_usage_owner_only_keeps_the_replica(self):
        registry.set_self('vps', self.self_path)
        self.devices(MAC, VPS, accounts=[ROW])
        self.assertEqual(device_sync.receive_account('team', self.blob()), 'staged')
        self.assertFalse((self.stores / 'team.json').exists())
        self.assertEqual(mode(self.directory / 'team.json'), 0o600)
        self.assertNotIn('team', self.runtime_toml.read_text())

    def test_receive_account_never_replaces_a_refreshable_store_and_reports_switch_pending(self):
        self.devices(MAC, VPS, accounts=[{**ROW, 'name': 'ops'}])
        refreshable_file(self.stores, 'ops', 'user-a', 'team-a')
        before = (self.stores / 'ops.json').read_bytes()
        self.assertEqual(device_sync.receive_account('ops', self.blob('ops')), 'staged')
        self.assertEqual((self.stores / 'ops.json').read_bytes(), before)
        self.assertEqual(device_sync.status()['switchPending'], ['ops'])

    def test_receive_account_refuses_a_refreshable_or_oversized_payload(self):
        refreshable_file(self.root / 'src', 'ops', 'user-o', 'org-o')
        with self.assertRaises(RuntimeError): device_sync.receive_account('ops', (self.root / 'src/ops.json').read_bytes())
        with self.assertRaises(ValueError): device_sync.receive_account('team', b'{' + b' ' * 131072 + b'}')
        self.assertFalse((self.directory / 'ops.json').exists())

    def test_push_sends_tokens_only_on_stdin(self):
        self.devices(MAC, VPS, accounts=[{**ROW, 'fingerprint': long_lived.fingerprint(json.loads(self.blob()))}])
        long_lived_file(self.directory, 'team')
        empty = registry.canonical({**registry.EMPTY(), 'devices': registry.load(self.reg)['devices']})
        fake = FakeRemote('vps', lambda argv, stdin: Completed(0, json.dumps(empty if argv[2] == 'registry' else {'accounts': [], 'registryGeneration': 0}).encode(), b''))
        self.assertEqual(device_sync.push(fake), 'ok')
        SecretWatch(fixture_token()).argv(fake.calls)
        self.assertTrue(any(stdin and fixture_token().encode() in stdin for argv, stdin in fake.calls))
        self.assertIn(['.local/bin/claude-pool', 'device', 'receive-account', 'team'], [argv for argv, _ in fake.calls])

    def test_push_skips_accounts_the_peer_already_has(self):
        token = long_lived_file(self.directory, 'team'); fp = long_lived.fingerprint(json.loads(token.read_text()))
        self.devices(MAC, VPS, accounts=[{**ROW, 'fingerprint': fp}])
        status = {'accounts': [{'name': 'team', 'kind': 'long-lived', 'fingerprint': fp}], 'registryGeneration': 99,
                  'registryHash': registry.digest(registry.load(self.reg))}
        fake = FakeRemote('vps', ok(json.dumps(status).encode()))
        self.assertEqual(device_sync.push(fake), 'ok')
        self.assertEqual([argv[1:3] for argv, _ in fake.calls], [['device', 'status']])

    def test_pull_from_peer_installs_missing_accounts_and_merges_registry(self):
        self.devices(MAC, VPS)
        peer = {**registry.load(self.reg), 'generation': 7, 'accounts': [ROW]}
        blob = self.blob()
        status = {'accounts': [{'name': 'team', 'kind': 'long-lived', 'fingerprint': long_lived.fingerprint(json.loads(blob))}]}
        def handler(argv, stdin):
            verb = argv[2]
            if verb == 'status': return Completed(0, json.dumps(status).encode(), b'')
            if verb == 'registry': return Completed(0, json.dumps(peer).encode(), b'')
            if verb == 'send-account': return Completed(0, blob, b'')
            return Completed(1, b'', b'')
        self.assertEqual(device_sync.pull(FakeRemote('vps', handler)), 'ok')
        self.assertEqual(registry.load(self.reg)['generation'], 7)
        self.assertTrue((self.stores / 'team.json').exists())

    def test_pull_reports_a_failed_peer_without_its_output(self):
        self.devices(MAC, VPS)
        result = device_sync.pull(FakeRemote('vps', lambda argv, stdin: Completed(1, b'', fixture_token().encode())))
        self.assertTrue(result.startswith('failed: '))
        SecretWatch(fixture_token()).text(result)

    def test_unreachable_device_is_reported_and_other_devices_still_sync(self):
        self.devices({**MAC, 'name': 'mac', 'ssh': 'mac'}, {**VPS, 'name': 'vps', 'ssh': 'vps'}, {**MAC, 'name': 'me', 'ssh': 'me'})
        registry.set_self('me', self.self_path)
        status = json.dumps({'accounts': [], 'registryGeneration': 99}).encode()
        fakes = {'mac': FakeRemote('mac', lambda argv, stdin: Completed(255, b'', b'')),
                 'vps': FakeRemote('vps', lambda argv, stdin: Completed(0, status if argv[2] == 'status' else json.dumps(registry.load(self.reg)).encode(), b''))}
        self.assertEqual(device_sync.sync_all(remotes=lambda t: fakes[t]), {'mac': 'unreachable', 'vps': 'ok'})

    def test_a_device_without_ssh_is_unreachable(self):
        self.devices({**MAC, 'ssh': None, 'name': 'phone'}, {**MAC, 'name': 'me', 'ssh': 'me'})
        registry.set_self('me', self.self_path)
        self.assertEqual(device_sync.sync_all(remotes=Mock()), {'phone': 'skipped: no ssh target from this device (it syncs with this one itself)'})

    def test_a_refused_local_gateway_means_idle_but_other_failures_mean_busy(self):
        import urllib.error
        self.devices(MAC, VPS, accounts=[ROW]); device_sync.receive_account('team', self.blob())
        def down(): raise urllib.error.URLError(ConnectionRefusedError())
        def broken(): raise OSError('weird')
        report = device_sync.receive_registry(self.newer([]), overview=broken)
        self.assertEqual(report['kept_busy'], ['team'])
        report = device_sync.receive_registry(self.newer([]), overview=down)
        self.assertEqual(report['removed'], ['team'])

    def test_rotation_is_staged_and_switch_serves_the_new_token(self):
        self.devices(MAC, VPS, accounts=[ROW])
        device_sync.receive_account('team', self.blob())
        old = (self.stores / 'team.json').read_bytes()
        new = self.blob(token=fixture_token('b'))
        self.assertEqual(device_sync.receive_account('team', new), 'staged')
        row = next(r for r in device_sync.status()['accounts'] if r['name'] == 'team')
        self.assertEqual(row['servingFingerprint'], long_lived.fingerprint(json.loads(old)))
        self.assertEqual(row['fingerprint'], long_lived.fingerprint(json.loads(new)))
        self.assertEqual(device_sync.status()['switchPending'], ['team'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(device_sync.switch(False, lambda: {'sessions': []}, Mock(return_value=0)), 0)
        self.assertEqual((self.stores / 'team.json').read_bytes(), new)
        # The old replica (moved aside when the new one arrived) and the old served copy, never deleted.
        self.assertEqual([json.loads(p.read_bytes()) for p in (self.stores / '.retired').glob('team-*.json')], [json.loads(old)] * 2)

    def test_native_login_account_is_kept_when_the_registry_drops_it(self):
        self.devices(MAC, VPS, accounts=[ROW]); device_sync.receive_account('team', self.blob())
        report = device_sync.receive_registry(self.newer([]), native_identity=lambda: {'email': 'team@x.de', 'organization': 'Team A'})
        self.assertEqual(report['kept_native'], ['team']); self.assertIn('team', self.config_names())
        self.assertEqual(report['removed'], [])

    def test_an_unreadable_native_login_keeps_the_account(self):
        self.devices(MAC, VPS, accounts=[ROW]); device_sync.receive_account('team', self.blob())
        def boom(): raise RuntimeError('Could not read the Native login')
        report = device_sync.receive_registry(self.newer([]), native_identity=boom)
        self.assertIn('team', report['failed']); self.assertIn('team', self.config_names())

    def test_a_token_for_an_account_the_registry_does_not_list_is_only_staged(self):
        self.devices(MAC, VPS)
        self.assertEqual(device_sync.receive_account('team', self.blob()), 'staged')
        self.assertFalse((self.stores / 'team.json').exists()); self.assertNotIn('team', self.config_names())
        self.assertTrue((self.directory / 'team.json').exists())

    def test_a_device_that_is_behind_gets_the_removal_and_never_resurrects_the_account(self):
        # This device (B) is at an older generation and still has team; the peer (A) removed it at generation 6.
        self.devices(MAC, VPS, accounts=[ROW]); device_sync.receive_account('team', self.blob())
        peer = json.loads(self.newer([], generation=6))
        status = {'accounts': [], 'registryGeneration': 6, 'registryHash': registry.digest(registry.canonical(peer))}
        def handler(argv, stdin):
            return Completed(0, json.dumps(status if argv[2] == 'status' else peer).encode(), b'')
        fake = FakeRemote('vps', handler)
        self.assertEqual(device_sync.push(fake), 'ok')
        self.assertEqual(device_sync.pull(fake), 'ok')
        self.assertEqual([argv[2] for argv, _ in fake.calls if argv[2] != 'status' and argv[2] != 'registry'], [])
        self.assertNotIn('team', self.config_names()); self.assertEqual(registry.load(self.reg)['generation'], 6)

    def test_a_peer_with_the_same_registry_and_tokens_gets_nothing_pushed(self):
        token = long_lived_file(self.directory, 'team'); fp = long_lived.fingerprint(json.loads(token.read_text()))
        self.devices(MAC, VPS, accounts=[ROW])
        status = {'accounts': [{'name': 'team', 'kind': 'long-lived', 'fingerprint': fp}], 'registryGeneration': 99,
                  'registryHash': registry.digest(registry.load(self.reg))}
        fake = FakeRemote('vps', ok(json.dumps(status).encode()))
        device_sync.push(fake)
        self.assertEqual([argv[2] for argv, _ in fake.calls], ['status'])

    def test_switch_stop_failure_touches_nothing(self):
        self.switch_setup(); pool = Mock(return_value=1); before = (self.stores / 'team.json').read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertNotEqual(device_sync.switch(False, lambda: {'sessions': []}, pool), 0)
        pool.assert_called_once_with('--stop'); self.assertEqual((self.stores / 'team.json').read_bytes(), before)
        self.assertNotIn('Switched', out.getvalue()); self.assertFalse((self.stores / '.retired').exists())

    def test_switch_start_failure_prints_the_restart_command_and_no_success(self):
        self.switch_setup(); pool = Mock(side_effect=[0, 1])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertNotEqual(device_sync.switch(False, lambda: {'sessions': []}, pool), 0)
        self.assertIn('claude-pool --start', out.getvalue()); self.assertNotIn('Switched', out.getvalue())

    def test_a_waiting_removal_is_dropped_when_the_account_was_added_again_locally(self):
        self.devices(MAC, VPS, accounts=[ROW]); device_sync.receive_account('team', self.blob())
        removal = self.newer([])
        report = device_sync.receive_registry(removal, overview=lambda: {'sessions': [{'busy': True, 'account': 'team'}]})
        self.assertEqual(report['kept_busy'], ['team'])
        # Re-added locally with another token while the removal waited; the tombstone names the old token only.
        for folder in (self.stores, self.directory): (folder / 'team.json').unlink()
        long_lived_file(self.stores, 'team', token=fixture_token('c')); long_lived_file(self.directory, 'team', token=fixture_token('c'))
        report = device_sync.receive_registry(removal, overview=lambda: {'sessions': []})
        self.assertEqual(report['removed'], []); self.assertEqual(report['kept'], ['team']); self.assertIn('team', self.config_names())

    def test_a_held_add_lock_counts_as_busy(self):
        import fcntl
        self.devices(MAC, VPS, accounts=[ROW]); device_sync.receive_account('team', self.blob())
        with (self.stores / '.team.add.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            report = device_sync.receive_registry(self.newer([]), overview=lambda: {'sessions': []})
        self.assertEqual(report['kept_busy'], ['team']); self.assertIn('team', self.config_names())

    def test_a_device_without_a_name_syncs_nothing(self):
        self.self_path.unlink(); self.devices(MAC, VPS)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(device_sync.sync_all(remotes=Mock()), {})
        self.assertIn('no name', out.getvalue())

    def newer(self, accounts, generation=5, fingerprint=None):
        """The registry another device wrote after removing every account not in `accounts` (its tombstones name the
        fingerprint of the token this device holds, or `fingerprint`)."""
        value = registry.load(self.reg)
        kept = {row['name'] for row in accounts}
        tombs = [{'kind': 'account', 'name': row['name'], 'generation': generation,
                  'fingerprint': fingerprint or device_sync.local_fingerprint(row['name'])}
                 for row in value['accounts'] if row['name'] not in kept]
        out = {**value, 'generation': generation, 'accounts': [{**row, 'generation': generation} for row in accounts]}
        if tombs: out['removed'] = tombs
        return json.dumps(out).encode()

    def neu(self):
        row = {**ROW, 'name': 'neu', 'alias': 'neu', 'label': 'Neu', 'userId': 'user-n', 'organizationId': 'org-n'}
        blob = long_lived_file(self.root / 'src', 'neu', user='user-n', org='org-n', token=fixture_token('n')).read_bytes()
        return {**row, 'fingerprint': long_lived.fingerprint(json.loads(blob))}, blob

    def neu_files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*') if 'neu' in p.name and p.suffix == '.json'
                      and not str(p.relative_to(self.root)).startswith('src'))

    def test_a_partitioned_add_survives_a_peer_registry_that_moved_ahead(self):
        # The final review probe: the peer made two changes while this device enrolled neu; neu and its token stay.
        self.devices(MAC, VPS)
        base = registry.load(self.reg)
        row, blob = self.neu()
        registry.update(lambda v: v['accounts'].append(row), self.reg)
        self.assertEqual(device_sync.receive_account('neu', blob), 'installed')
        before = self.neu_files()
        report = device_sync.receive_registry(json.dumps({**base, 'generation': 3}).encode(), overview=lambda: {'sessions': []})
        self.assertEqual(report['removed'], [])
        self.assertEqual([r['name'] for r in registry.load(self.reg)['accounts']], ['neu'])
        self.assertEqual(registry.load(self.reg)['generation'], 3)
        self.assertEqual(self.neu_files(), before); self.assertIn('neu', self.config_names())

    def test_push_to_a_peer_that_is_ahead_merges_first_then_sends_the_merged_registry_and_the_token(self):
        self.devices(MAC, VPS)
        base = registry.load(self.reg)
        row, blob = self.neu()
        registry.update(lambda v: v['accounts'].append(row), self.reg)
        device_sync.receive_account('neu', blob)
        theirs = {**base, 'generation': 3}
        status = {'accounts': [], 'registryGeneration': 3, 'registryHash': registry.digest(theirs)}
        def handler(argv, stdin):
            if argv[2] == 'status': return Completed(0, json.dumps(status).encode(), b'')
            if argv[2] == 'registry': return Completed(0, json.dumps(theirs).encode(), b'')
            return Completed(0, b'{}', b'')
        fake = FakeRemote('vps', handler)
        self.assertEqual(device_sync.push(fake), 'ok')
        sent = {argv[2]: stdin for argv, stdin in fake.calls if argv[2] in ('receive-registry', 'receive-account')}
        self.assertEqual([r['name'] for r in json.loads(sent['receive-registry'])['accounts']], ['neu'])
        self.assertEqual(json.loads(sent['receive-registry'])['generation'], 3)
        self.assertEqual(sent['receive-account'], blob)
        SecretWatch(fixture_token('n')).argv(fake.calls)

    def test_a_tombstoned_account_is_moved_aside_never_unlinked(self):
        self.devices(MAC, VPS, accounts=[ROW]); device_sync.receive_account('team', self.blob())
        token = self.blob()
        report = device_sync.receive_registry(self.newer([]), overview=lambda: {'sessions': []})
        self.assertEqual(report['removed'], ['team']); self.assertNotIn('team', self.config_names())
        self.assertFalse((self.stores / 'team.json').exists()); self.assertFalse((self.directory / 'team.json').exists())
        aside = [p.read_bytes() for p in (self.stores / '.retired').glob('team-*.json')]
        self.assertEqual(len(aside), 2); self.assertTrue(all(json.loads(b) == json.loads(token) for b in aside))

    def test_a_tombstone_for_another_fingerprint_keeps_the_account_and_its_token(self):
        self.devices(MAC, VPS, accounts=[ROW]); device_sync.receive_account('team', self.blob())
        report = device_sync.receive_registry(self.newer([], fingerprint='ffffffffffff'), overview=lambda: {'sessions': []})
        self.assertEqual(report['removed'], []); self.assertIn('team', report['kept'])
        self.assertIn('team', self.config_names()); self.assertTrue((self.directory / 'team.json').exists())

    def test_a_tombstone_for_another_fingerprint_is_removed_when_the_peer_holds_this_token(self):
        self.devices(MAC, VPS, accounts=[ROW]); device_sync.receive_account('team', self.blob())
        held = {long_lived.fingerprint(json.loads(self.blob()))}
        report = device_sync.receive_registry(self.newer([], fingerprint='ffffffffffff'), overview=lambda: {'sessions': []}, elsewhere=held)
        self.assertEqual(report['removed'], ['team'])
        self.assertEqual(len(list((self.stores / '.retired').glob('team-*.json'))), 2)

    def test_an_account_the_registry_merely_does_not_list_is_never_removed(self):
        self.devices(MAC, VPS, accounts=[ROW]); device_sync.receive_account('team', self.blob())
        value = registry.load(self.reg)
        report = device_sync.receive_registry(json.dumps({**value, 'generation': 9, 'accounts': []}).encode(), overview=lambda: {'sessions': []})
        self.assertEqual(report['removed'], []); self.assertIn('team', self.config_names())
        self.assertEqual([r['name'] for r in registry.load(self.reg)['accounts']], ['team'])

    def test_account_removed_from_the_registry_is_removed_locally(self):
        self.devices(MAC, VPS, accounts=[ROW])
        device_sync.receive_account('team', self.blob())
        report = device_sync.receive_registry(self.newer([]))
        self.assertNotIn('team', self.config_names()); self.assertNotIn('name = "team"', self.runtime_toml.read_text())
        self.assertFalse((self.stores / 'team.json').exists()); self.assertFalse((self.directory / 'team.json').exists())
        self.assertEqual((report['result'], report['removed']), ('replaced', ['team']))

    def test_account_only_in_local_config_survives_receive_registry(self):
        self.devices(MAC, VPS, accounts=[ROW])
        device_sync.receive_registry(self.newer([]))
        self.assertIn('personal', self.config_names()); self.assertIn('name = "personal"', self.runtime_toml.read_text())

    def test_a_busy_account_is_kept_and_removed_on_the_next_sync(self):
        self.devices(MAC, VPS, accounts=[ROW])
        device_sync.receive_account('team', self.blob())
        busy = {'sessions': [{'busy': True, 'account': 'team'}]}
        report = device_sync.receive_registry(self.newer([]), overview=lambda: busy)
        self.assertEqual(report['kept_busy'], ['team']); self.assertIn('team', self.config_names())
        report = device_sync.receive_registry(self.newer([]), overview=lambda: {'sessions': []})
        self.assertEqual(report['removed'], ['team']); self.assertNotIn('team', self.config_names())

    def test_an_older_registry_changes_nothing(self):
        self.devices(MAC, VPS, accounts=[ROW])
        before = self.reg.read_bytes()
        self.assertEqual(device_sync.receive_registry(self.newer([], generation=0))['result'], 'unchanged')
        self.assertEqual(self.reg.read_bytes(), before)

    def test_receive_never_calls_service_controls(self):
        # Real code, real installation.json, fake launchctl/systemctl on PATH that leave a marker when run.
        (self.root / 'installation.json').write_text(json.dumps({'serviceLabel': 'io.example.pool'}))
        self.devices(MAC, VPS, accounts=[ROW])
        device_sync.receive_account('team', self.blob())
        device_sync.receive_registry(self.newer([]), native_identity=lambda: None)
        self.assertNotIn('team', self.config_names())
        self.assertFalse(self.marker.exists())

    def test_failing_integration_hook_is_reported_and_others_still_run(self):
        self.devices(MAC, VPS)
        boom, fine = Mock(side_effect=RuntimeError('no')), Mock()
        boom.__name__, fine.__name__ = 'boom', 'fine'
        device_sync.INTEGRATION_HOOKS[:] = [boom, fine]
        report = device_sync.receive_registry(self.newer([]))
        fine.assert_called_once()
        self.assertTrue(report['hooks']['boom'].startswith('failed')); self.assertEqual(report['hooks']['fine'], 'ok')

    def switch_setup(self):
        self.devices(MAC, VPS, accounts=[ROW])
        refreshable_file(self.stores, 'team', 'user-a', 'team-a')
        long_lived_file(self.directory, 'team', 'user-a', 'team-a')
        return (self.stores / 'team.json').read_bytes()

    def test_switch_refuses_while_a_session_is_in_flight_and_never_stops_the_service(self):
        self.switch_setup(); pool = Mock(return_value=0)
        busy = lambda: {'sessions': [{'busy': True, 'account': 'team'}]}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(device_sync.switch(False, busy, pool), 3)
        pool.assert_not_called()
        self.assertIn('Claude tasks are running here; run claude-pool device switch --wait or rerun later.', out.getvalue())

    def test_switch_when_idle_retires_refreshable_files_and_restarts_once(self):
        before = self.switch_setup(); pool = Mock(return_value=0)
        with contextlib.redirect_stdout(io.StringIO()): code = device_sync.switch(False, lambda: {'sessions': []}, pool)
        self.assertEqual(code, 0)
        self.assertEqual(pool.call_args_list, [call('--stop'), call('--start')])
        retired = list((self.stores / '.retired').glob('team-*.json'))
        self.assertEqual([p.read_bytes() for p in retired], [before])
        store = self.stores / 'team.json'
        self.assertEqual(long_lived.kind(json.loads(store.read_text())), 'long-lived'); self.assertEqual(mode(store), 0o600)
        self.assertEqual(device_sync.status()['switchPending'], [])

    def test_switch_with_nothing_pending_leaves_the_service_alone(self):
        pool = Mock(return_value=0)
        with contextlib.redirect_stdout(io.StringIO()): code = device_sync.switch(False, lambda: {'sessions': []}, pool)
        self.assertEqual(code, 0)
        pool.assert_not_called()

    def test_switch_pending_is_empty_on_the_usage_owner(self):
        self.switch_setup(); registry.set_self('vps', self.self_path)
        self.assertEqual(device_sync.switch_pending(self.stores, self.directory, registry.load(self.reg), 'vps'), [])
        self.assertEqual(long_lived.switch_pending(self.stores, self.directory, registry.load(self.reg), 'mac'), ['team'])

    def test_status_contains_fingerprints_and_parts_but_no_token(self):
        self.devices(MAC, VPS, accounts=[ROW]); long_lived_file(self.directory, 'team', 'user-a', 'team-a')
        device_sync.STATUS_PARTS[:] = [lambda: {'shell': True}]
        status = device_sync.status()
        self.assertTrue(status['shell'])
        team = next(row for row in status['accounts'] if row['name'] == 'team')
        self.assertEqual((team['kind'], team['active']), ('long-lived', False))
        self.assertEqual(len(team['fingerprint']), 12)
        self.assertIn(status['service'], ('running', 'stopped', 'missing'))
        self.assertEqual(team['servingFingerprint'], None)
        SecretWatch(fixture_token()).text(json.dumps(status))


class OnChangeTests(unittest.TestCase):
    def run_remove(self, on_change, name='team'):
        import accounts_cli
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as out:
            root = Path(folder)
            stores, directory, runtime, cfg = root / 'c', root / 'l', root / 'r', root / 'cfg'
            for path in (stores, directory, cfg): path.mkdir(mode=0o700)
            runtime.mkdir(); config = cfg / 'config.json'
            config.write_text(json.dumps({'accounts': [{'name': 'personal', 'label': 'P'}, {'name': 'team', 'label': 'T'}]}))
            (runtime / 'shunt.toml').write_text(BASE + '\n[[providers.anthropic.accounts]]\nname = "team"\n')
            binary = root / 'shunt'; binary.write_text('#!' + sys.executable + '\nimport sys\nsys.exit(0)\n'); binary.chmod(0o700)
            long_lived_file(stores, 'team'); long_lived_file(directory, 'team')
            code = accounts_cli.cli(['remove', name], config=config, stores=stores, binary=binary, runtime=runtime,
                                    long_lived=directory, registry_path=cfg / 'devices.json', on_change=on_change)
        return code, out.getvalue()

    def test_on_change_runs_after_a_successful_change_and_prints_one_line_per_device(self):
        code, out = self.run_remove(lambda: {'vps': 'ok', 'mac': 'unreachable'})
        self.assertEqual(code, 0); self.assertIn('  mac: unreachable\n  vps: ok\n', out)

    def test_a_failing_sync_never_fails_the_change(self):
        def boom(): raise OSError('x')
        code, out = self.run_remove(boom)
        self.assertEqual(code, 0); self.assertIn('Device sync failed', out)

    def test_on_change_is_not_called_when_the_change_fails(self):
        called = Mock()
        with self.assertRaises(RuntimeError): self.run_remove(called, name='nobody')
        called.assert_not_called()


if __name__ == '__main__': unittest.main()
