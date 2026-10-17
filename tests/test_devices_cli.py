"""The `device` command: verb routing, the internal device protocol and the non-orchestration user verbs. Offline, fakes only;
secrets travel only on stdin and stdout pipes."""
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import accounts_cli
import conductor
import device_cua
import device_install
import device_integrations
import device_preview
from device_report import Step
import device_sync
import devices_cli
import launch
import registry
import settings
from remote import Completed
from fakes import FakeRemote, SecretWatch, fixture_token, long_lived_file, refreshable_file

MAC = {'name': 'mac', 'ssh': 'justus@mac', 'platform': 'darwin', 'roles': [], 'addedAt': 1}
VPS = {'name': 'vps', 'ssh': 'justus@vps', 'platform': 'linux', 'roles': ['usage'], 'addedAt': 2}
BASE = ('[providers.anthropic]\nkind = "anthropic"\nauth = "claude_oauth"\n\n'
        '[[providers.anthropic.accounts]]\nname = "personal"\n')
TEAM = {'name': 'team', 'label': 'Team', 'alias': 'team', 'plan': 'Team', 'email': 'team@x.de', 'organization': 'Team A',
        'userId': 'user-a', 'organizationId': 'team-a'}


class Pipe(io.BytesIO):
    def isatty(self): return False


class Tty(io.BytesIO):
    def isatty(self): return True


def answer(code=0, stdout=b''): return Completed(code, stdout, b'')


class DeviceCliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'; self.home.mkdir()
        self.stores = self.root / 'credentials'; self.stores.mkdir(mode=0o700)
        self.directory = self.root / 'claude-long-lived'; self.directory.mkdir(mode=0o700)
        self.runtime = self.root / 'runtime'; self.runtime.mkdir()
        self.cfg = self.root / 'cfg'; self.cfg.mkdir(mode=0o700)
        self.config = self.cfg / 'config.json'
        self.config.write_text(json.dumps({'accounts': [{'name': 'personal', 'label': 'Personal'}]}))
        self.reg, self.self_path = self.cfg / 'devices.json', self.cfg / 'device.json'
        (self.runtime / 'shunt.toml').write_text(BASE)
        self.here = self.root / 'here'; (self.here / 'bin').mkdir(parents=True)
        shunt = self.here / 'bin/shunt'
        shunt.write_text('#!' + sys.executable + '\nimport sys\nsys.exit(0 if "check" in sys.argv else 3)\n'); shunt.chmod(0o700)
        # Only fake tools and the system directories: no real herdr, claude, claude-pool, cua, tailscale or preview.
        self.bin = self.root / 'bin'; self.bin.mkdir(); self.marker = self.root / 'tool-called'
        for name in ['launchctl', 'systemctl', 'herdr', 'claude', 'claude-pool', 'cua', 'tailscale', 'preview']:
            (self.bin / name).write_text('#!/bin/sh\necho "$0" >> ' + str(self.marker) + '\nexit 97\n'); (self.bin / name).chmod(0o755)
        for name in ['claude-native', 'loginctl']:  # read-only status reads may reach these; they answer nothing
            (self.bin / name).write_text('#!/bin/sh\nexit 97\n'); (self.bin / name).chmod(0o755)
        native = self.bin / 'claude-native'
        patches = [patch.object(settings, key, value) for key, value in
                   [('STORES', self.stores), ('LONG_LIVED', self.directory), ('RUNTIME', self.runtime), ('CONFIG', self.config),
                    ('REGISTRY', self.reg), ('SELF', self.self_path), ('HERE', self.here), ('NATIVE_CLAUDE', str(native)),
                    ('CONDUCTOR_STATE', self.runtime / 'conductor.json')]]
        patches += [patch.dict(os.environ, {'PATH': str(self.bin) + ':/usr/bin:/bin', 'HOME': str(self.home), 'SHELL': '/bin/zsh'}),
                    patch.object(device_sync, 'default_native_identity', lambda: None)]
        for item in patches: item.start(); self.addCleanup(item.stop)
        registry.set_self('mac', self.self_path)

    def parts_off(self):
        for item in [patch.object(device_sync, 'STATUS_PARTS', []), patch.object(device_sync, 'INTEGRATION_HOOKS', [])]:
            item.start(); self.addCleanup(item.stop)

    def devices(self, *rows, accounts=(), conductor_entry=None):
        def change(value):
            value['devices'].extend(rows); value['accounts'].extend(accounts)
            if conductor_entry: value['conductor'] = conductor_entry
        registry.update(change, self.reg)

    def run_cli(self, argv, stdin=b'', remotes=None, local=None, tty=False):
        raw = Tty() if tty else Pipe()
        out = io.TextIOWrapper(raw, encoding='utf-8', write_through=True)
        err = io.StringIO()
        remotes = remotes or (lambda target: FakeRemote(target, lambda argv, data: answer(255)))
        local = local or FakeRemote('local', lambda argv, data: answer(97))
        with patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(stdin))), contextlib.redirect_stderr(err):
            code = devices_cli.cli(argv, remotes=remotes, local=local, out=out)
        out.flush()
        self.raw = raw.getvalue()
        return code, self.raw.decode('utf-8', 'replace'), err.getvalue()


class RoutingTests(DeviceCliBase):
    def setUp(self):
        super().setUp(); self.parts_off()

    def test_help_stub_names_every_user_verb(self):
        for verb in devices_cli.USER_VERBS: self.assertIn('device ' + verb, devices_cli.DEVICE_HELP)
        self.assertIn('Internal verbs (called over SSH): ' + ', '.join(devices_cli.INTERNAL_VERBS), devices_cli.DEVICE_HELP)

    def test_verb_lists_use_the_conductor_names(self):
        self.assertEqual(devices_cli.USER_VERBS, ['add', 'check', 'remove', 'list', 'sync', 'switch', 'conductor move', 'conductor status'])
        self.assertEqual(devices_cli.INTERNAL_VERBS, ['status', 'registry', 'receive-registry', 'receive-account', 'send-account',
                                                      'identity', 'readings', 'shell', 'cua-token', 'conductor serve',
                                                      'conductor install', 'conductor uninstall', 'forget-accounts', 'self',
                                                      'preview-rules'])

    def test_help_and_unknown_verbs(self):
        code, out, _ = self.run_cli(['--help'])
        self.assertEqual((code, out.strip()), (0, devices_cli.DEVICE_HELP.strip()))
        code, out, err = self.run_cli(['frobnicate'])
        self.assertEqual(code, 2); self.assertIn('help device', out + err)
        self.assertEqual(self.run_cli([])[0], 2)

    def test_add_check_and_remove_are_not_implemented_yet(self):
        for verb in ['add', 'check', 'remove']:
            code, out, err = self.run_cli([verb, 'justus@vps'])
            self.assertEqual(code, 2); self.assertIn('not implemented yet', out + err)


class RegistrationTests(DeviceCliBase):
    def test_every_status_part_is_registered(self):
        quiet = lambda *a, **k: FakeRemote('local', lambda argv, data: answer(127))
        for module in (device_cua, device_integrations, device_preview):
            item = patch.object(module, 'Local', quiet); item.start(); self.addCleanup(item.stop)
        status = device_sync.status()
        self.assertTrue({'linger', 'shell', 'nativeLogin', 'herdrProfiles', 'conductor', 'cua', 'preview'} <= set(status))
        self.assertIn(conductor.apply_hook, device_sync.INTEGRATION_HOOKS)
        self.assertIn(device_integrations.apply_hook, device_sync.INTEGRATION_HOOKS)
        self.assertFalse(self.marker.exists())


class LaunchTests(unittest.TestCase):
    def test_device_dispatches_before_pool_start(self):
        with patch.object(launch.sys, 'argv', ['claude-pool', 'device', 'list']), patch('devices_cli.cli', return_value=0) as cli, \
             patch.object(launch, 'token') as token, patch.object(launch, 'ensure_pool') as pool, \
             patch.object(launch, 'managed_start') as start, patch.object(launch.subprocess, 'call') as native:
            with self.assertRaises(SystemExit) as result: launch.main()
        self.assertEqual(result.exception.code, 0)
        cli.assert_called_once_with(['list'])
        token.assert_not_called(); pool.assert_not_called(); start.assert_not_called(); native.assert_not_called()

    def test_pool_device_also_dispatches(self):
        with patch.object(launch.sys, 'argv', ['claude-pool', 'pool', 'device', 'status', '--json']), \
             patch('devices_cli.cli', return_value=3) as cli, patch.object(launch, 'token') as token:
            with self.assertRaises(SystemExit) as result: launch.main()
        self.assertEqual(result.exception.code, 3); cli.assert_called_once_with(['status', '--json']); token.assert_not_called()

    def test_help_names_the_device_commands_and_help_device_prints_the_device_help(self):
        with patch.object(launch.sys, 'argv', ['claude-pool', '--help']), contextlib.redirect_stdout(io.StringIO()) as out:
            launch.main()
        self.assertIn('device add|check|remove|list', out.getvalue())
        with patch.object(launch.sys, 'argv', ['claude-pool', 'help', 'device']), contextlib.redirect_stdout(io.StringIO()) as out, \
             patch.object(launch, 'token') as token:
            launch.main()
        self.assertEqual(out.getvalue().strip(), devices_cli.DEVICE_HELP.strip()); token.assert_not_called()


class InternalVerbTests(DeviceCliBase):
    def setUp(self):
        super().setUp(); self.parts_off()

    def test_status_prints_non_secret_json_with_or_without_the_flag(self):
        self.devices(MAC, VPS, accounts=[TEAM]); long_lived_file(self.directory, 'team')
        for argv in (['status', '--json'], ['status']):
            code, out, _ = self.run_cli(argv)
            value = json.loads(out)
            self.assertEqual(code, 0); self.assertEqual(value['name'], 'mac')
            self.assertEqual(value['registryGeneration'], registry.load(self.reg)['generation'])
            self.assertEqual(len(next(r for r in value['accounts'] if r['name'] == 'team')['fingerprint']), 12)
            SecretWatch(fixture_token()).text(out)

    def test_registry_prints_the_registry(self):
        self.devices(MAC, VPS, accounts=[TEAM])
        code, out, _ = self.run_cli(['registry'])
        self.assertEqual((code, json.loads(out)), (0, registry.load(self.reg)))

    def test_receive_registry_reads_stdin_and_reports_as_json(self):
        self.devices(MAC)
        incoming = registry.load(self.reg); incoming['devices'].append(VPS); incoming['generation'] += 5
        code, out, _ = self.run_cli(['receive-registry'], stdin=json.dumps(incoming).encode())
        self.assertEqual(code, 0); self.assertEqual(json.loads(out)['result'], 'replaced')
        self.assertEqual({d['name'] for d in registry.load(self.reg)['devices']}, {'mac', 'vps'})
        code, out, err = self.run_cli(['receive-registry'], stdin=b'not json')
        self.assertNotEqual(code, 0)

    def test_receive_account_takes_the_token_on_stdin_only(self):
        data = long_lived_file(self.root / 'src', 'team').read_bytes()
        argv = ['receive-account', 'team']
        code, out, err = self.run_cli(argv, stdin=data)
        self.assertEqual((code, out.strip()), (0, 'staged'))
        self.assertEqual((self.directory / 'team.json').read_bytes(), data)
        self.assertEqual((self.directory / 'team.json').stat().st_mode & 0o777, 0o600)
        SecretWatch(fixture_token()).argv([(argv, None)]); SecretWatch(fixture_token()).text(out, err)
        self.assertEqual(self.run_cli(['receive-account', 'Bad Name'], stdin=data)[0], 2)

    def test_send_account_writes_the_token_to_the_stdout_pipe_and_never_to_a_terminal(self):
        data = long_lived_file(self.directory, 'team').read_bytes()
        code, _, err = self.run_cli(['send-account', 'team'])
        self.assertEqual((code, self.raw), (0, data)); SecretWatch(fixture_token()).text(err)
        code, _, err = self.run_cli(['send-account', 'team'], tty=True)
        self.assertNotEqual(code, 0); self.assertEqual(self.raw, b''); SecretWatch(fixture_token()).text(err)
        refreshable_file(self.directory, 'ops', 'user-b', 'team-b')
        code, _, err = self.run_cli(['send-account', 'ops'])
        self.assertNotEqual(code, 0); self.assertEqual(self.raw, b'')

    def test_identity_prints_the_local_grant_identity_and_never_a_token(self):
        refreshable_file(self.stores, 'operations', 'user-a', 'team-a')
        info = {'email': 'ops@x.de', 'organization': 'Team A', 'plan': 'Team', 'userId': 'user-a', 'organizationId': 'team-a'}
        with patch.object(accounts_cli, 'profile', return_value=info) as profile:
            code, out, err = self.run_cli(['identity', 'user-a', 'team-a'])
        self.assertEqual((code, json.loads(out)), (0, info)); profile.assert_called_once_with(self.stores / 'operations.json')
        SecretWatch(fixture_token('r'), 'fixture-refresh-operations').text(out, err)
        with patch.object(accounts_cli, 'profile', return_value=info):
            code, out, _ = self.run_cli(['identity', 'user-z', 'team-z'])
        self.assertNotEqual(code, 0); self.assertEqual(out.strip(), '')

    def test_readings_exports_only_on_the_usage_owner(self):
        self.devices(MAC, VPS)
        overview = {'accounts': [{'name': 'team', 'fiveHour': {'pct': 40, 'resetsAt': 1}, 'seenAt': 5}]}
        with patch.object(device_sync, 'pool_overview', return_value=overview) as read:
            code, out, err = self.run_cli(['readings'])
            self.assertNotEqual(code, 0); self.assertIn('usage owner', out + err); read.assert_not_called()
            registry.set_self('vps', self.self_path)
            code, out, _ = self.run_cli(['readings'])
        value = json.loads(out)
        self.assertEqual((code, value['device'], value['accounts']['team']['fiveHour']['pct']), (0, 'vps', 40))

    def test_shell_check_add_remove_edit_the_rc_file(self):
        rc = self.home / '.zshrc'
        self.assertEqual(self.run_cli(['shell', 'check'])[0], 1)
        self.assertEqual(self.run_cli(['shell', 'add'])[0], 0); self.assertIn('claude-pool exec', rc.read_text())
        self.assertEqual(self.run_cli(['shell', 'check'])[0], 0)
        self.assertEqual(self.run_cli(['shell', 'remove'])[0], 0); self.assertEqual(self.run_cli(['shell', 'check'])[0], 1)
        self.assertEqual(self.run_cli(['shell', 'wipe'])[0], 2)

    def test_cua_token_goes_to_the_stdout_pipe_only(self):
        token = fixture_token('c').encode()
        path = self.home / 'env-token'; path.write_bytes(token + b'\n'); path.chmod(0o600)
        with patch.object(device_cua, '_token_path', return_value=path):
            code, _, err = self.run_cli(['cua-token'])
            self.assertEqual((code, self.raw), (0, token)); SecretWatch(token.decode()).text(err)
            code, _, err = self.run_cli(['cua-token'], tty=True)
        self.assertNotEqual(code, 0); self.assertEqual(self.raw, b''); SecretWatch(token.decode()).text(err)

    def test_preview_rules_verb_manages_the_rules_block(self):
        rules = self.home / '.claude' / 'CLAUDE.md'
        self.assertEqual(self.run_cli(['preview-rules', 'check'])[0], 1)
        self.assertEqual(self.run_cli(['preview-rules', 'add'])[0], 0); self.assertIn(device_preview.RULES_START, rules.read_text())
        self.assertEqual(self.run_cli(['preview-rules', 'check'])[0], 0)
        self.assertEqual(self.run_cli(['preview-rules', 'remove'])[0], 0)
        self.assertEqual(self.run_cli(['preview-rules', 'check'])[0], 1)

    def test_forget_accounts_drops_long_lived_copies_and_never_a_refreshable_file(self):
        self.config.write_text(json.dumps({'accounts': [{'name': 'personal', 'label': 'Personal'}, {'name': 'team', 'label': 'Team'}]}))
        (self.runtime / 'shunt.toml').write_text(BASE + '\n[[providers.anthropic.accounts]]\nname = "team"\n')
        personal = refreshable_file(self.stores, 'personal', 'user-p', 'org-p'); before = personal.read_bytes()
        long_lived_file(self.stores, 'team'); long_lived_file(self.directory, 'team')
        long_lived_file(self.directory, 'personal', 'user-p', 'org-p')  # a synced copy next to the refreshable grant
        code, out, err = self.run_cli(['forget-accounts'])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)['removed'], ['personal', 'team'])
        self.assertFalse((self.stores / 'team.json').exists()); self.assertFalse((self.directory / 'team.json').exists())
        self.assertFalse((self.directory / 'personal.json').exists())
        self.assertEqual(personal.read_bytes(), before); self.assertFalse((self.stores / '.retired').exists())
        self.assertEqual([r['name'] for r in json.loads(self.config.read_text())['accounts']], ['personal'])
        self.assertNotIn('"team"', (self.runtime / 'shunt.toml').read_text())
        SecretWatch(fixture_token(), fixture_token('r')).text(out, err)
        self.assertEqual(self.run_cli(['forget-accounts'])[0], 0)  # idempotent

    def test_self_names_this_device(self):
        self.assertEqual(self.run_cli(['self', 'vps'])[0], 0); self.assertEqual(registry.self_name(self.self_path), 'vps')
        code, out, err = self.run_cli(['self', 'Not Valid'])
        self.assertEqual(code, 2); self.assertEqual(registry.self_name(self.self_path), 'vps')

    def test_conductor_internal_verbs_call_the_conductor_module(self):
        with patch.object(conductor, 'serve_service', return_value=0) as serve:
            self.assertEqual(self.run_cli(['conductor', 'serve'])[0], 0)
        serve.assert_called_once_with()
        session = 'cse_' + 'abc123'
        with patch.object(conductor, 'install', return_value=Step('conductor', 'ok', 'installed')) as install:
            code, out, _ = self.run_cli(['conductor', 'install', '--session-id', session, '--fresh', '--move'])
            self.assertEqual(code, 0); self.assertIn('installed', out)
            install.assert_called_once_with(session, True, True)
            self.run_cli(['conductor', 'install'])
            install.assert_called_with(None, False, False)
        with patch.object(conductor, 'uninstall', return_value=Step('conductor', 'failed', 'still loaded', 'launchctl bootout x')) as gone:
            code, out, _ = self.run_cli(['conductor', 'uninstall'])
        self.assertEqual(code, 1); self.assertIn('fix: launchctl bootout x', out); gone.assert_called_once_with()
        with patch.object(conductor, 'install', side_effect=ValueError('Invalid Conductor session id')):
            code, out, err = self.run_cli(['conductor', 'install', '--session-id', 'x'])
        self.assertEqual(code, 2); self.assertIn('Invalid Conductor session id', out + err)


class PoolOverviewTests(DeviceCliBase):
    def test_pool_overview_reads_the_local_gateway_with_the_client_token(self):
        token = 'client-token-fixture'
        (self.stores / '.pool-client-token').write_text(token + '\n')
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen['path'], seen['auth'] = self.path, self.headers.get('Authorization')
                body = json.dumps({'sessions': [], 'health': {'activeRequests': 0}}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args): pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        with patch.object(settings, 'BASE_URL', 'http://127.0.0.1:' + str(server.server_address[1])):
            value = device_sync.pool_overview()
        self.assertEqual(value, {'sessions': [], 'health': {'activeRequests': 0}})
        self.assertEqual((seen['path'], seen['auth']), ('/pool/overview', 'Bearer ' + token))


class UserVerbTests(DeviceCliBase):
    def setUp(self):
        super().setUp(); self.parts_off()

    def test_list_shows_devices_roles_and_the_conductor(self):
        self.devices(MAC, VPS, conductor_entry={'device': 'vps', 'email': 'team@x.de', 'organization': 'Team A', 'cwd': '/srv/c'})
        code, out, _ = self.run_cli(['list'])
        self.assertEqual(code, 0)
        mac = next(line for line in out.splitlines() if line.split()[:1] == ['mac'])
        vps = next(line for line in out.splitlines() if line.split()[:1] == ['vps'])
        self.assertIn('justus@mac', mac); self.assertIn('this device', mac)
        self.assertIn('usage owner', vps); self.assertIn('Conductor', vps)

    def test_list_without_devices_says_how_to_add_one(self):
        code, out, _ = self.run_cli(['list'])
        self.assertEqual(code, 0); self.assertIn('claude-pool device add', out)

    def test_sync_runs_sync_all_and_force_registry_bumps_the_generation_first(self):
        self.devices(MAC, VPS)
        remotes = Mock()
        before = registry.load(self.reg)['generation']
        with patch.object(device_sync, 'sync_all', return_value={'vps': 'ok'}) as sync:
            code, out, _ = self.run_cli(['sync'], remotes=remotes)
            self.assertEqual(code, 0); self.assertIn('vps: ok', out)
            self.assertEqual(registry.load(self.reg)['generation'], before)
            code, out, _ = self.run_cli(['sync', '--force-registry'], remotes=remotes)
        self.assertEqual(code, 0); self.assertEqual(registry.load(self.reg)['generation'], before + 1)
        self.assertEqual(sync.call_count, 2); sync.assert_called_with(remotes)
        with patch.object(device_sync, 'sync_all', return_value={'vps': 'unreachable'}):
            code, out, _ = self.run_cli(['sync'], remotes=remotes)
        self.assertEqual(code, 1); self.assertIn('vps: unreachable', out)

    def test_switch_pool_callable_runs_the_local_launcher_and_returns_its_exit_code(self):
        local = FakeRemote('local', lambda argv, data: answer(1 if '--stop' in argv else 0))
        seen = {}

        def fake_switch(wait, overview, pool):
            seen['wait'], seen['overview'] = wait, overview
            return pool('--stop')
        with patch.object(device_sync, 'switch', side_effect=fake_switch):
            code, _, _ = self.run_cli(['switch', '--wait'], local=local)
        self.assertEqual(code, 1); self.assertTrue(seen['wait']); self.assertTrue(callable(seen['overview']))
        self.assertEqual([argv for argv, _ in local.calls], [['.local/bin/claude-pool', '--stop']])
        with patch.object(device_sync, 'switch', side_effect=lambda wait, overview, pool: pool('--start')):
            code, _, _ = self.run_cli(['switch'], local=local)
        self.assertEqual(code, 0)

    def test_conductor_move_calls_conductor_move_with_the_registry(self):
        self.devices(MAC, VPS, conductor_entry={'device': 'vps', 'email': 'team@x.de', 'organization': 'Team A', 'cwd': '/srv/c'})
        remotes, local = Mock(), FakeRemote('local', lambda argv, data: answer())
        with patch.object(conductor, 'move', return_value=0) as move:
            self.assertEqual(self.run_cli(['conductor', 'move', 'mac'], remotes=remotes, local=local)[0], 0)
        move.assert_called_once_with('mac', registry.load(self.reg), remotes=remotes, local=local)
        self.assertEqual(self.run_cli(['conductor', 'move'])[0], 2)

    def test_conductor_status_asks_each_device_after_confirming_device_support(self):
        self.devices(MAC, VPS, {'name': 'old', 'ssh': 'justus@old', 'platform': 'linux', 'roles': [], 'addedAt': 3},
                     conductor_entry={'device': 'vps', 'email': 'team@x.de', 'organization': 'Team A', 'cwd': '/srv/c'})
        vps_status = {'name': 'vps', 'platform': 'linux', 'linger': True,
                      'conductor': {'installed': True, 'running': True, 'ready': True, 'fresh': False, 'duplicates': []}}

        def vps(argv, data):
            if argv[:2] == ['sh', '-c'] and argv[2] == device_install.LAUNCHER_SCRIPT: return answer(0)
            if argv[1:] == ['device', 'status', '--json']: return answer(0, json.dumps(vps_status).encode())
            return answer(97)
        fakes = {'justus@vps': FakeRemote('justus@vps', vps),
                 'justus@old': FakeRemote('justus@old', lambda argv, data: answer(11))}
        with patch.object(device_sync, 'status', return_value={'name': 'mac', 'conductor': {'installed': False, 'running': False}}):
            code, out, _ = self.run_cli(['conductor', 'status'], remotes=fakes.__getitem__)
        self.assertEqual(code, 0, out); self.assertIn('ready on vps', out)
        self.assertFalse([a for a, _ in fakes['justus@old'].calls if 'device' in a])

    def test_conductor_status_without_a_conductor(self):
        self.devices(MAC)
        code, out, _ = self.run_cli(['conductor', 'status'])
        self.assertEqual(code, 0); self.assertIn('no Conductor', out)


if __name__ == '__main__':
    unittest.main()
