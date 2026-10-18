"""One subscription, a different local name on each device: devices match registry accounts by (userId, organizationId)
and fall back to the name only when a side has no UUIDs. Offline, fakes only; nothing reaches a real pool or tool."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import accounts_cli
import device_cua
import device_sync
import devices_cli
import long_lived
import readings
import registry
import settings
from remote import Completed
from fakes import FakeRemote, fixture_token, long_lived_file, refreshable_file
import test_devices_cli as cli_base

# The six subscriptions as the two machines were enrolled by hand before the devices feature existed.
MAC_NAMES = ['ai-vaerk-team-premium', 'intern', 'justus-ai-vaerk', 'justus-gmail', 'operations', 'sofi']
VPS_NAMES = ['team', 'intern', 'max', 'gmail', 'operations', 'sofi']
IDS = [('user-' + str(i), 'org-' + str(i)) for i in range(6)]
MAC_IDS, VPS_IDS = dict(zip(MAC_NAMES, IDS)), dict(zip(VPS_NAMES, IDS))
TEAM_ROW = {'name': 'ai-vaerk-team-premium', 'label': 'AI Værk Team', 'alias': 'team', 'plan': 'Team',
            'email': 'team@x.de', 'organization': 'Team A', 'userId': 'user-0', 'organizationId': 'org-0'}
BASE = ('[providers.anthropic]\nkind = "anthropic"\nauth = "claude_oauth"\n\n'
        '[[providers.anthropic.accounts]]\nname = "team"\n')


def local(name, ids=None, active=True):
    row = {'name': name, 'active': active}
    if ids: row.update(userId=ids[0], organizationId=ids[1])
    return row


class MatchTests(unittest.TestCase):
    def test_a_registry_row_finds_the_local_account_with_its_uuids(self):
        rows = [local(name, VPS_IDS[name]) for name in VPS_NAMES]
        self.assertEqual(registry.local_name(TEAM_ROW, rows), 'team')
        self.assertEqual(registry.local_name({'name': 'justus-gmail', 'userId': 'user-3', 'organizationId': 'org-3'}, rows), 'gmail')

    def test_the_name_counts_only_when_a_side_has_no_uuids(self):
        self.assertEqual(registry.local_name({'name': 'team'}, [local('team', ('x', 'y'))]), 'team')
        self.assertEqual(registry.local_name(TEAM_ROW, [local('ai-vaerk-team-premium')]), 'ai-vaerk-team-premium')
        # Same name, another subscription: not the same account.
        self.assertIsNone(registry.local_name(TEAM_ROW, [local('ai-vaerk-team-premium', ('other', 'org'))]))
        self.assertIsNone(registry.local_name(TEAM_ROW, []))

    def test_a_served_member_wins_over_a_stray_copy_of_the_same_subscription(self):
        rows = [local('ai-vaerk-team-premium', IDS[0], active=False), local('team', IDS[0])]
        self.assertEqual(registry.local_name(TEAM_ROW, rows), 'team')
        self.assertEqual(registry.local_name(TEAM_ROW, {row['name']: row for row in rows}), 'team')

    def test_messages_name_both_names_when_they_differ(self):
        self.assertEqual(registry.describe(TEAM_ROW, 'team'), 'team (AI Værk Team, registry: ai-vaerk-team-premium)')
        self.assertEqual(registry.describe(TEAM_ROW, 'ai-vaerk-team-premium'), 'ai-vaerk-team-premium')
        self.assertEqual(registry.describe(TEAM_ROW, None), 'ai-vaerk-team-premium')


class FileDevice(unittest.TestCase):
    """This device (the VPS) with real files in a temp dir: its own refreshable logins under its own names."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stores = self.root / 'credentials'; self.stores.mkdir(mode=0o700)
        self.directory = self.root / 'claude-long-lived'; self.directory.mkdir(mode=0o700)
        self.runtime = self.root / 'runtime'; self.runtime.mkdir()
        self.cfg = self.root / 'cfg'; self.cfg.mkdir(mode=0o700)
        self.config = self.cfg / 'config.json'
        self.config.write_text(json.dumps({'accounts': [{'name': 'team', 'label': 'AI Værk Team', 'alias': 'team'}]}))
        self.reg, self.self_path = self.cfg / 'devices.json', self.cfg / 'device.json'
        self.toml = self.runtime / 'shunt.toml'; self.toml.write_text(BASE)
        self.here = self.root / 'here'; (self.here / 'bin').mkdir(parents=True)
        shunt = self.here / 'bin/shunt'
        shunt.write_text('#!' + sys.executable + '\nimport sys\nsys.exit(0 if "check" in sys.argv else 3)\n'); shunt.chmod(0o700)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        for name in ['launchctl', 'systemctl', 'claude', 'claude-pool', 'herdr', 'tailscale']:
            (self.bin / name).write_text('#!/bin/sh\nexit 97\n'); (self.bin / name).chmod(0o755)
        patches = [patch.object(settings, key, value) for key, value in
                   [('STORES', self.stores), ('LONG_LIVED', self.directory), ('RUNTIME', self.runtime), ('CONFIG', self.config),
                    ('REGISTRY', self.reg), ('SELF', self.self_path), ('HERE', self.here)]]
        patches += [patch.dict(os.environ, {'PATH': str(self.bin) + ':/usr/bin:/bin'}),
                    patch.object(device_sync, 'default_native_identity', lambda: None),
                    patch.object(device_sync, 'STATUS_PARTS', []), patch.object(device_sync, 'INTEGRATION_HOOKS', [])]
        for item in patches: item.start(); self.addCleanup(item.stop)
        registry.set_self('vps', self.self_path)
        refreshable_file(self.stores, 'team', *IDS[0])
        mac = {'name': 'mac', 'ssh': 'justus@mac', 'platform': 'darwin', 'roles': [], 'addedAt': 1}
        vps = {'name': 'vps', 'ssh': 'justus@vps', 'platform': 'linux', 'roles': [], 'addedAt': 2}
        self.blob = long_lived_file(self.root / 'src', 'ai-vaerk-team-premium', *IDS[0]).read_bytes()
        self.fp = long_lived.fingerprint(json.loads(self.blob))
        registry.update(lambda value: (value['devices'].extend([mac, vps]), value['accounts'].append({**TEAM_ROW, 'fingerprint': self.fp})), self.reg)

    def config_names(self): return [row['name'] for row in json.loads(self.config.read_text())['accounts']]


class ReceiveTests(FileDevice):
    def test_status_reports_the_uuids_of_every_account(self):
        row = next(r for r in device_sync.status()['accounts'] if r['name'] == 'team')
        self.assertEqual((row['userId'], row['organizationId']), IDS[0])

    def test_the_long_lived_token_is_stored_for_the_local_name_and_switches_in_place(self):
        self.assertEqual(device_sync.receive_account('ai-vaerk-team-premium', self.blob), 'staged')
        self.assertEqual(long_lived.fingerprint(json.loads((self.directory / 'team.json').read_bytes())), self.fp)
        self.assertFalse((self.directory / 'ai-vaerk-team-premium.json').exists())
        self.assertFalse((self.stores / 'ai-vaerk-team-premium.json').exists())
        self.assertEqual(self.config_names(), ['team'])
        self.assertEqual(device_sync.status()['switchPending'], ['team'])
        self.assertEqual(device_sync.receive_account('ai-vaerk-team-premium', self.blob), 'unchanged')

    def test_a_stray_copy_under_the_registry_name_moves_to_the_local_name(self):
        # The state the VPS is in: the token arrived as a second name next to its own refreshable `team`.
        long_lived_file(self.directory, 'ai-vaerk-team-premium', *IDS[0])
        stray = long_lived_file(self.stores, 'ai-vaerk-team-premium', *IDS[0])
        self.assertEqual(device_sync.receive_account('ai-vaerk-team-premium', self.blob), 'unchanged')
        self.assertEqual(long_lived.fingerprint(json.loads((self.directory / 'team.json').read_bytes())), self.fp)
        self.assertFalse((self.directory / 'ai-vaerk-team-premium.json').exists())
        self.assertFalse(stray.exists())
        kept = list((self.stores / '.retired').glob('ai-vaerk-team-premium-*.json'))
        self.assertEqual([long_lived.fingerprint(json.loads(p.read_bytes())) for p in kept], [self.fp])  # moved aside, never deleted
        self.assertEqual(self.config_names(), ['team'])
        self.assertEqual([r['name'] for r in device_sync.status()['accounts']], ['team'])

    def test_a_stray_copy_stays_and_is_reported_when_the_local_name_already_holds_a_long_lived_token(self):
        other = long_lived_file(self.root / 'other', 'team', *IDS[0], token=fixture_token('o')).read_bytes()
        (self.directory / 'team.json').write_bytes(other); (self.directory / 'team.json').chmod(0o600)
        stray = long_lived_file(self.directory, 'ai-vaerk-team-premium', *IDS[0])
        device_sync.receive_account('ai-vaerk-team-premium', self.blob)
        self.assertTrue(stray.exists())
        row = next(r for r in device_sync.status()['accounts'] if r['name'] == 'ai-vaerk-team-premium')
        self.assertEqual(row['duplicateOf'], 'team')
        self.assertEqual(self.config_names(), ['team'])

    def test_a_non_owner_without_the_subscription_installs_it_under_the_registry_name(self):
        (self.stores / 'team.json').unlink()
        self.config.write_text(json.dumps({'accounts': [{'name': 'personal', 'label': 'P'}]}))
        refreshable_file(self.stores, 'personal', 'user-p', 'org-p')
        self.toml.write_text(BASE.replace('"team"', '"personal"'))
        self.assertEqual(device_sync.receive_account('ai-vaerk-team-premium', self.blob), 'installed')
        self.assertEqual(self.config_names(), ['personal', 'ai-vaerk-team-premium'])

    def test_no_receive_path_adds_a_second_config_row_for_a_served_subscription(self):
        # A long-lived copy kept only under another local name, no member yet: installed under that local name, never twice.
        (self.stores / 'team.json').unlink()
        self.config.write_text(json.dumps({'accounts': [{'name': 'personal', 'label': 'P'}]}))
        refreshable_file(self.stores, 'personal', 'user-p', 'org-p')
        self.toml.write_text(BASE.replace('"team"', '"personal"'))
        long_lived_file(self.directory, 'team', *IDS[0])
        device_sync.receive_account('ai-vaerk-team-premium', self.blob)
        device_sync.receive_account('ai-vaerk-team-premium', self.blob)
        self.assertEqual(sorted(self.config_names()), ['personal', 'team'])

    def test_pull_fetches_by_the_peer_name_and_keeps_the_local_name(self):
        status = {'accounts': [{'name': name, 'kind': 'long-lived' if name == 'ai-vaerk-team-premium' else 'refreshable',
                                'fingerprint': self.fp if name == 'ai-vaerk-team-premium' else '2' * 12, 'active': True,
                                'userId': MAC_IDS[name][0], 'organizationId': MAC_IDS[name][1]} for name in MAC_NAMES],
                  'registryHash': registry.digest(registry.load(self.reg))}
        def handler(argv, stdin):
            if argv[2] == 'status': return Completed(0, json.dumps(status).encode(), b'')
            if argv[2] == 'registry': return Completed(0, json.dumps(registry.load(self.reg)).encode(), b'')
            if argv[2:4] == ['send-account', 'ai-vaerk-team-premium']: return Completed(0, self.blob, b'')
            return Completed(1, b'', b'')
        fake = FakeRemote('justus@mac', handler)
        self.assertEqual(device_sync.pull(fake), 'ok')
        self.assertEqual(long_lived.fingerprint(json.loads((self.directory / 'team.json').read_bytes())), self.fp)
        self.assertFalse((self.directory / 'ai-vaerk-team-premium.json').exists())
        self.assertEqual(device_sync.pull(fake), 'ok')
        self.assertEqual(sum(argv[2] == 'send-account' for argv, _ in fake.calls), 1)

    def test_push_compares_with_the_peer_account_of_the_same_subscription(self):
        long_lived_file(self.directory, 'team', *IDS[0], token=fixture_token())
        status = {'accounts': [{'name': 'ai-vaerk-team-premium', 'kind': 'long-lived', 'fingerprint': self.fp, 'active': False,
                                'userId': IDS[0][0], 'organizationId': IDS[0][1]}],
                  'registryHash': registry.digest(registry.load(self.reg))}
        fake = FakeRemote('justus@mac', lambda argv, stdin: Completed(0, json.dumps(status).encode(), b''))
        self.assertEqual(device_sync.push(fake), 'ok')
        self.assertEqual([argv[2] for argv, _ in fake.calls], ['status'])

    def test_push_sends_the_local_copy_under_the_registry_name(self):
        long_lived_file(self.directory, 'team', *IDS[0], token=fixture_token())
        status = {'accounts': [], 'registryHash': registry.digest(registry.load(self.reg))}
        fake = FakeRemote('justus@mac', lambda argv, stdin: Completed(0, json.dumps(status).encode(), b''))
        self.assertEqual(device_sync.push(fake), 'ok')
        sent = [(argv[2:], stdin) for argv, stdin in fake.calls if argv[2] == 'receive-account']
        self.assertEqual([(args, long_lived.fingerprint(json.loads(data))) for args, data in sent],
                         [(['receive-account', 'ai-vaerk-team-premium'], self.fp)])

    def test_health_compares_a_local_name_with_its_registry_row(self):
        other = long_lived_file(self.root / 'other', 'team', *IDS[0], token=fixture_token('o')).read_bytes()
        (self.directory / 'team.json').write_bytes(other); (self.directory / 'team.json').chmod(0o600)
        found = long_lived.health(self.stores, self.directory, registry.load(self.reg), 0, 'vps')
        drift = [f for f in found if 'differs from the registry' in f['message']]
        self.assertEqual([f['account'] for f in drift], ['team (AI Værk Team, registry: ai-vaerk-team-premium)'])

    def test_remove_safety_finds_the_same_token_under_another_local_name(self):
        long_lived_file(self.directory, 'team', *IDS[0], token=fixture_token())
        theirs = {'ai-vaerk-team-premium': {'name': 'ai-vaerk-team-premium', 'kind': 'long-lived', 'fingerprint': self.fp,
                                            'userId': IDS[0][0], 'organizationId': IDS[0][1]}}
        self.assertEqual(devices_cli.unique_long_lived(theirs), [])
        theirs['ai-vaerk-team-premium']['fingerprint'] = '0' * 12
        self.assertEqual(devices_cli.unique_long_lived(theirs), ['ai-vaerk-team-premium'])


class RemovalTests(FileDevice):
    def removal(self, fingerprint):
        value = registry.load(self.reg)
        out = {**value, 'generation': value['generation'] + 1, 'accounts': [],
               'removed': [{'kind': 'account', 'name': 'ai-vaerk-team-premium', 'generation': value['generation'] + 1,
                            'fingerprint': fingerprint}]}
        return json.dumps(out).encode()

    def test_a_removed_registry_account_takes_only_its_long_lived_copy_kept_under_the_local_name(self):
        device_sync.receive_account('ai-vaerk-team-premium', self.blob)
        login = (self.stores / 'team.json').read_bytes()
        report = device_sync.receive_registry(self.removal(self.fp))
        self.assertFalse((self.directory / 'team.json').exists())  # a later switch can never serve the removed token
        kept = list((self.stores / '.retired').glob('team-*.json'))
        self.assertEqual([long_lived.fingerprint(json.loads(p.read_bytes())) for p in kept], [self.fp])  # moved aside
        self.assertEqual((self.stores / 'team.json').read_bytes(), login)  # this machine's own login stays
        self.assertEqual(self.config_names(), ['team'])
        self.assertEqual(device_sync.status()['switchPending'], [])
        self.assertIn('team', report['retired'])

    def test_another_token_under_the_local_name_stays(self):
        device_sync.receive_account('ai-vaerk-team-premium', self.blob)
        device_sync.receive_registry(self.removal('0' * 12))
        self.assertTrue((self.directory / 'team.json').exists())


class ReserveTests(FileDevice):
    def test_the_registry_reserve_reaches_the_local_name(self):
        value = registry.load(self.reg); value['accounts'][0]['reserveAt'] = 85
        accounts_cli.reserve_hook(value)
        self.assertEqual(json.loads(self.config.read_text())['accounts'][0]['reserveAt'], 85)
        self.assertIn('max_utilization = 0.85', self.toml.read_text())

    def test_a_local_reserve_follows_into_the_registry_row_of_the_subscription(self):
        config, binary, paths, env = accounts_cli.local_context()
        accounts_cli.reserve(config, binary, paths, env, 'team', 70)
        self.assertEqual(registry.account(registry.load(self.reg), 'ai-vaerk-team-premium')['reserveAt'], 70)
        self.assertIsNone(registry.account(registry.load(self.reg), 'team'))


class ReadingsTests(unittest.TestCase):
    NOW = 1_900_000_000_000

    def test_the_owner_exports_its_local_names_with_their_uuids(self):
        overview = {'accounts': [{'name': 'team', 'fiveHour': {'pct': 40, 'resetsAt': None}, 'seenAt': self.NOW - 10}]}
        value = readings.export(overview, 'vps', self.NOW, ids={'team': IDS[0]})
        self.assertEqual((value['accounts']['team']['userId'], value['accounts']['team']['organizationId']), IDS[0])
        self.assertEqual(readings.check(value)['accounts']['team']['userId'], 'user-0')

    def test_a_device_renames_shared_readings_to_its_own_names(self):
        overview = {'accounts': [{'name': name, 'fiveHour': {'pct': 10 + i, 'resetsAt': None}, 'seenAt': self.NOW - 10}
                                 for i, name in enumerate(VPS_NAMES)]}
        shared = readings.export(overview, 'vps', self.NOW, ids=VPS_IDS)
        mine = readings.localize(shared, [local(name, MAC_IDS[name]) for name in MAC_NAMES])
        self.assertEqual(sorted(mine['accounts']), sorted(MAC_NAMES))
        self.assertEqual(mine['accounts']['ai-vaerk-team-premium']['fiveHour']['pct'], 10)
        self.assertEqual(mine['accounts']['justus-gmail']['fiveHour']['pct'], 13)
        rows = [{'name': 'ai-vaerk-team-premium', 'seenAt': 0}]
        readings.merge(rows, mine, self.NOW)
        self.assertEqual(rows[0]['fiveHour']['pct'], 10)

    def test_readings_without_uuids_keep_their_names(self):
        shared = readings.export({'accounts': [{'name': 'team', 'seenAt': 1}]}, 'vps', self.NOW)
        self.assertEqual(readings.localize(shared, [local('team')])['accounts'].keys(), {'team'})

    def test_the_puller_writes_the_readings_under_the_local_names(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); cfg = root / 'cfg'; cfg.mkdir(mode=0o700)
        for key, value in [('REGISTRY', cfg / 'devices.json'), ('SELF', cfg / 'device.json')]:
            item = patch.object(settings, key, value); item.start(); self.addCleanup(item.stop)
        registry.set_self('mac')
        registry.update(lambda v: v['devices'].extend([{'name': 'mac', 'platform': 'darwin', 'roles': []},
                                                       {'name': 'vps', 'ssh': 'justus@vps', 'platform': 'linux', 'roles': ['usage']}]))
        overview = {'accounts': [{'name': 'team', 'fiveHour': {'pct': 40, 'resetsAt': None}, 'seenAt': int(time_ms()) - 10}]}
        reply = json.dumps(readings.export(overview, 'vps', int(time_ms()), ids={'team': IDS[0]})).encode()
        puller = readings.Puller(root / 'shared.json', quiet_journal(), lambda target: FakeRemote(target, lambda a, s: Completed(0, reply, b'')),
                                 local_accounts=lambda: [local('ai-vaerk-team-premium', IDS[0])])
        self.assertTrue(puller.run_once())
        self.assertEqual(list(readings.load_shared(root / 'shared.json')['accounts']), ['ai-vaerk-team-premium'])


def time_ms():
    import time
    return time.time() * 1000


def quiet_journal():
    class Journal:
        def record(self, code, **details): pass
    return Journal()


class TailscaleTests(unittest.TestCase):
    def test_a_mac_without_tailscale_on_path_uses_the_app_cli(self):
        app = '/Applications/Tailscale.app/Contents/MacOS/Tailscale'
        def handler(argv, stdin):
            if argv[4:] == [app, 'ip', '-4']: return Completed(0, b'100.64.0.1\n', b'')
            return Completed(127, b'', b'')
        fake = FakeRemote('local', handler)
        self.assertEqual(device_cua.tailscale_ip(fake), '100.64.0.1')
        self.assertEqual([argv[4:] for argv, _ in fake.calls], [['tailscale', 'ip', '-4'], [app, 'ip', '-4']])

    def test_tailscale_on_path_is_asked_first_and_only(self):
        fake = FakeRemote('local', lambda argv, stdin: Completed(0, b'100.64.0.2\n', b''))
        self.assertEqual(device_cua.tailscale_ip(fake), '100.64.0.2')
        self.assertEqual(len(fake.calls), 1)

    def test_no_tailscale_at_all_is_no_address(self):
        self.assertIsNone(device_cua.tailscale_ip(FakeRemote('local', lambda argv, stdin: Completed(127, b'', b''))))


class TwoDeviceTests(cli_base.OrchestrationBase):
    """The Mac (this device) and the VPS with the six subscriptions under their own names and shared UUIDs."""
    def setUp(self):
        super().setUp()
        # Replace the base fixture's `team` with the Mac's enrollment under its own name.
        self.team.unlink()
        registry.update(lambda value: value.update(accounts=[]), self.reg)
        self.token = long_lived_file(self.directory, 'ai-vaerk-team-premium', *IDS[0])
        self.fp = long_lived.fingerprint(json.loads(self.token.read_bytes()))
        registry.update(lambda value: (value['devices'].append(dict(cli_base.SELF_ROW)),
                                       value['accounts'].append({**TEAM_ROW, 'fingerprint': self.fp})), self.reg)
        self.vps = cli_base.Machine('justus@vps', self.log, refreshable=VPS_NAMES, uuids=VPS_IDS, shared_accounts=VPS_NAMES)
        self.machines['justus@vps'] = self.vps
        self.local.uuids, self.local.refreshable = dict(MAC_IDS), set(MAC_NAMES[1:])

    def test_add_usage_owner_succeeds_and_the_token_lands_under_team(self):
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps', '--usage-owner')
        self.assertEqual(code, 0, out + err)
        self.assertNotIn('serves no refreshable login', out)
        self.assertEqual(registry.owner(registry.load(self.reg))['name'], 'vps')
        self.assertEqual(self.vps.accounts['team']['fingerprint'], self.fp)
        self.assertNotIn('ai-vaerk-team-premium', self.vps.accounts)
        code, out, err = self.cli('check', 'vps')
        self.assertNotIn('fingerprint drift', out); self.assertNotIn('is not on vps', out)
        self.assertRegex(out, r'\n  ok +accounts ')

    def test_a_refused_usage_owner_names_both_names(self):
        self.vps.refreshable = set(VPS_NAMES[1:])  # `team` only long-lived there
        self.vps.accounts['team'] = {'kind': 'long-lived', 'fingerprint': self.fp}
        registry.update(lambda value: registry.device(value, 'mac').update(roles=['usage']), self.reg)
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps', '--usage-owner')
        self.assertIn('serves no refreshable login for team (AI Værk Team, registry: ai-vaerk-team-premium)', out)
        self.assertIn('account add team@x.de --name team,', out)

    def test_check_matches_readings_by_subscription(self):
        # The registry names the subscription as the VPS does; the Mac's readings carry its own name for it.
        registry.update(lambda value: (value['devices'].append({'name': 'vps', 'ssh': 'justus@vps', 'platform': 'linux', 'roles': ['usage']}),
                                       value.update(accounts=[{**TEAM_ROW, 'name': 'team', 'fingerprint': self.fp}])), self.reg)
        self.local.shared_accounts, self.local.refreshable = list(MAC_NAMES), set(MAC_NAMES)
        self.local.registry = registry.load(self.reg)
        t = devices_cli.Target(devices_cli.Session(self.remotes, self.local.remote), registry.device(registry.load(self.reg), 'mac'),
                               self.local.remote, True, None)
        t.supported = True
        step = devices_cli.check_readings(t)
        self.assertEqual(step.status, 'ok', step)


if __name__ == '__main__':
    unittest.main()
