"""The `device` command: verb routing, the internal device protocol and the non-orchestration user verbs. Offline, fakes only;
secrets travel only on stdin and stdout pipes."""
import contextlib
import http.server
import shlex
import socket
import subprocess
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import accounts_cli
import conductor
import device_cua
import device_install
import device_integrations
import device_preview
from device_report import ADD_ORDER, CHECK_NAMES, Step
import device_sync
import devices_cli
import launch
import long_lived
import native_login
import readings
import remote
import registry
import service
import settings
from remote import POOL, Completed, Local, Remote
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

    def test_add_check_and_remove_report_usage_errors(self):
        for argv in (['add'], ['remove'], ['check', '--frobnicate']):
            code, out, err = self.run_cli(argv)
            self.assertEqual(code, 2, argv); self.assertIn('help device', out + err)


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
            self.assertEqual(value['registryHash'], registry.digest(registry.load(self.reg)))
            self.assertIn(value['service'], ('running', 'stopped', 'missing'))
            team = next(r for r in value['accounts'] if r['name'] == 'team')
            self.assertEqual(len(team['fingerprint']), 12); self.assertIn('servingFingerprint', team)
            self.assertIsNone(team['servingFingerprint'])
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

    def pool_stopped(self):
        (self.runtime / 'maintenance').write_text('Deliberately stopped\n')
        for key in ('GATEWAY_PORT', 'BACKEND_PORT', 'CONTROL_PORT'):
            probe = socket.socket(); probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]; probe.close()
            item = patch.object(settings, key, port); item.start(); self.addCleanup(item.stop)

    def strict_shunt(self):
        """Like the real backend: `check` rejects an empty member list. Every run leaves a marker."""
        shunt = self.here / 'bin/shunt'
        shunt.write_text('#!' + sys.executable + '\nimport sys, tomllib\nopen(' + repr(str(self.root / 'shunt-ran')) + ', "a").write("x")\n'
                         'a = sys.argv\nc = tomllib.load(open(a[a.index("--config") + 1], "rb"))\n'
                         'sys.exit(0 if c["providers"]["anthropic"].get("accounts") else 1)\n')
        shunt.chmod(0o700)

    def tree(self):
        return {str(p): (p.read_bytes() if p.is_file() else None) for p in self.root.rglob('*') if 'home' not in p.parts}

    def test_forget_accounts_on_a_device_with_only_long_lived_accounts_empties_the_membership(self):
        self.pool_stopped(); self.strict_shunt()
        self.config.write_text(json.dumps({'accounts': [{'name': 'team', 'label': 'Team'}]}))
        (self.runtime / 'shunt.toml').write_text('[providers.anthropic]\nkind = "anthropic"\nauth = "claude_oauth"\n\n'
                                                 '[[providers.anthropic.accounts]]\nname = "team"\n')
        long_lived_file(self.stores, 'team'); long_lived_file(self.directory, 'team')
        code, out, err = self.run_cli(['forget-accounts'])
        self.assertEqual(code, 0, out + err); self.assertEqual(json.loads(out)['removed'], ['team'])
        self.assertFalse((self.stores / 'team.json').exists()); self.assertFalse((self.directory / 'team.json').exists())
        self.assertEqual(json.loads(self.config.read_text())['accounts'], [])
        import tomllib
        self.assertEqual(tomllib.loads((self.runtime / 'shunt.toml').read_text())['providers']['anthropic']['accounts'], [])
        self.assertEqual(list(self.stores.glob('.*.lock')), [])
        self.assertFalse((self.root / 'shunt-ran').exists())  # the live-backend check is skipped, nothing else is
        SecretWatch(fixture_token()).text(out, err)
        self.assertEqual(self.run_cli(['forget-accounts'])[0], 0)  # idempotent

    def test_forget_accounts_refuses_while_the_pool_runs_and_changes_nothing(self):
        self.config.write_text(json.dumps({'accounts': [{'name': 'team', 'label': 'Team'}]}))
        long_lived_file(self.stores, 'team'); long_lived_file(self.directory, 'team')
        before = self.tree()
        code, out, err = self.run_cli(['forget-accounts'])  # no maintenance marker: the pool counts as running
        self.assertEqual(code, 1); self.assertIn('stop', out + err); self.assertEqual(self.tree(), before)
        (self.runtime / 'maintenance').write_text('Deliberately stopped\n'); before = self.tree()
        listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(); self.addCleanup(listener.close)
        with patch.object(settings, 'GATEWAY_PORT', listener.getsockname()[1]):
            code, out, err = self.run_cli(['forget-accounts'])  # marker present, but something still answers the port
        self.assertEqual(code, 1); self.assertEqual(self.tree(), before)

    def test_forget_accounts_refuses_while_the_backend_or_the_control_port_answers(self):
        self.pool_stopped()
        self.config.write_text(json.dumps({'accounts': [{'name': 'team', 'label': 'Team'}]}))
        long_lived_file(self.stores, 'team'); long_lived_file(self.directory, 'team')
        for key in ('BACKEND_PORT', 'CONTROL_PORT'):
            with self.subTest(port=key):
                before = self.tree()
                listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(); self.addCleanup(listener.close)
                with patch.object(settings, key, listener.getsockname()[1]):
                    code, out, err = self.run_cli(['forget-accounts'])
                self.assertEqual(code, 1); self.assertIn('stop', out + err); self.assertEqual(self.tree(), before)

    def test_forget_accounts_never_touches_a_refreshable_file(self):
        self.pool_stopped()
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
        self.assertNotIn('"team"', (self.runtime / 'shunt.toml').read_text()); self.assertIn('"personal"', (self.runtime / 'shunt.toml').read_text())
        SecretWatch(fixture_token(), fixture_token('r')).text(out, err)

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
            install.assert_called_once_with(session, True, True, False)
            self.run_cli(['conductor', 'install'])
            install.assert_called_with(None, False, False, False)
            self.run_cli(['conductor', 'install', '--restart'])
            install.assert_called_with(None, False, False, True)
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

    def test_check_from_another_device_reports_the_device_that_added_it_as_skipped(self):
        # The device that ran add (here: mac) is registered with no ssh key; checking it from the VPS must not crash.
        registry.set_self('vps', self.self_path)
        self.devices({'name': 'mac', 'platform': 'darwin', 'roles': [], 'addedAt': 1}, {**VPS, 'ssh': None})
        def remotes(target): raise AssertionError('no remote may be called, got ' + repr(target))
        for argv in (['check', 'mac'], ['check']):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(argv, remotes=remotes)
                self.assertEqual(code if argv == ['check', 'mac'] else 0, 0, out + err)
                self.assertRegex(out, r'skipped +reachable +no ssh target from this device')
        with patch.object(device_sync, 'sync_all', wraps=device_sync.sync_all):
            code, out, err = self.run_cli(['sync'], remotes=remotes)
        self.assertEqual(code, 0, out + err); self.assertIn('mac: skipped', out)

    def test_force_registry_reads_every_peer_list_and_tombstones_what_only_the_peer_has(self):
        self.devices(MAC, VPS)
        theirs = {**registry.load(self.reg), 'generation': 7,
                  'accounts': [{'name': 'ops', 'label': 'Ops', 'fingerprint': 'aaaaaaaaaaaa', 'generation': 7}]}
        fake = FakeRemote('vps', lambda argv, data: answer(0, json.dumps(theirs).encode()) if argv[2:3] == ['registry'] else answer(255))
        with patch.object(device_sync, 'sync_all', return_value={'vps': 'ok'}):
            code, out, _ = self.run_cli(['sync', '--force-registry'], remotes=lambda target: fake)
        self.assertEqual(code, 0)
        value = registry.load(self.reg)
        self.assertEqual(value['generation'], 8)
        self.assertIn({'kind': 'account', 'name': 'ops', 'generation': 8, 'fingerprint': 'aaaaaaaaaaaa'}, value['removed'])

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
            if argv[1:3] == ['device', 'status']: return answer(0, json.dumps(vps_status).encode())
            return answer(97)
        fakes = {'justus@vps': FakeRemote('justus@vps', vps),
                 'justus@old': FakeRemote('justus@old', lambda argv, data: answer(97), device_exit=86)}
        with patch.object(device_sync, 'status', return_value={'name': 'mac', 'conductor': {'installed': False, 'running': False}}):
            code, out, _ = self.run_cli(['conductor', 'status'], remotes=fakes.__getitem__)
        self.assertEqual(code, 0, out); self.assertIn('ready on vps', out)
        self.assertIn('outdated launcher on old', out); self.assertIn('claude-pool device add justus@old', out)
        self.assertEqual(fakes['justus@old'].calls, [])

    def test_conductor_status_names_an_outdated_conductor_device(self):
        self.devices(MAC, VPS, conductor_entry={'device': 'vps', 'email': 'team@x.de', 'organization': 'Team A', 'cwd': '/srv/c'})
        fakes = {'justus@vps': FakeRemote('justus@vps', lambda argv, data: answer(97), device_exit=86)}
        with patch.object(device_sync, 'status', return_value={'name': 'mac', 'conductor': {}}):
            code, out, _ = self.run_cli(['conductor', 'status'], remotes=fakes.__getitem__)
        self.assertEqual(code, 2); self.assertIn('outdated launcher', out); self.assertRegex(out, r'fix: claude-pool device add justus@vps')

    def test_conductor_status_without_a_conductor(self):
        self.devices(MAC)
        code, out, _ = self.run_cli(['conductor', 'status'])
        self.assertEqual(code, 0); self.assertIn('no Conductor', out)



def layout(home, current=True, support=True, wrapper=True):
    """A target HOME as install.py lays it out; the wrapper logs every argv it is run with."""
    prefix = home / '.local/share/claude pool'; (home / '.local/bin').mkdir(parents=True); prefix.mkdir(parents=True)
    release = prefix / 'versions/abc' if current else prefix
    (release / 'gateway').mkdir(parents=True)
    if support: (release / 'gateway/devices_cli.py').write_text('')
    if current: (prefix / 'current').symlink_to(release)
    run = prefix / 'run.py'
    run.write_text('import sys\nopen(sys.argv[1] if False else ' + repr(str(home / 'launcher.log')) + ', "a").write(" ".join(sys.argv[1:]) + "\\n")\n')
    if wrapper:
        (home / '.local/bin/claude-pool').write_text('#!/bin/sh\n# managed by claude-pool installer\nexec ' + shlex.quote(sys.executable) +
                                                     ' ' + shlex.quote(str(run)) + ' "$@"\n')
        (home / '.local/bin/claude-pool').chmod(0o755)
    return home / 'launcher.log'


def over_ssh(home):
    """Stands in for ssh: runs the remote command line with `sh` in `home`, exactly as the far side would."""
    def run(argv, **options):
        assert argv[0] == 'ssh' and '-T' in argv
        return subprocess.run(['sh', '-c', argv[-1]], cwd=home, env={'HOME': str(home), 'PATH': '/usr/bin:/bin'}, **options)
    return run


class DeviceScriptTests(DeviceCliBase):
    def setUp(self):
        super().setUp(); self.parts_off()

    def target(self, **kwargs):
        home = Path(tempfile.mkdtemp(prefix='target-', dir=self.root))
        return home, layout(home, **kwargs)

    def test_the_device_script_runs_only_a_launcher_with_device_support(self):
        for kwargs, code in [({'current': True}, 0), ({'current': False}, 0), ({'current': True, 'support': False}, 86),
                             ({'current': False, 'support': False}, 86), ({'wrapper': False}, 87)]:
            with self.subTest(**kwargs):
                home, log = self.target(**kwargs)
                done = Remote('justus@vps', run=over_ssh(home)).device('status', '--json', 'a b; touch pwned')
                self.assertEqual(done.returncode, code, done.stderr)
                self.assertFalse((home / 'pwned').exists())
                self.assertEqual(log.read_text() if log.exists() else '', 'device status --json a b; touch pwned\n' if code == 0 else '')

    def test_the_device_script_resolves_the_release_like_the_install_check(self):
        lines = lambda script: script.split('\n')[2:10]  # from `line=$(grep ...` to the devices_cli.py lookup
        self.assertEqual(lines(remote.DEVICE_SCRIPT), lines(device_install.LAUNCHER_SCRIPT))

    def test_local_device_uses_the_launcher_from_the_receipt(self):
        home, log = self.target()
        receipt = self.root / 'installation.json'
        receipt.write_text(json.dumps({'launchers': {str(home / '.local/bin/claude-pool'): 'x'}}))
        def run(argv, **options): return subprocess.run(argv, env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'}, **options)
        self.assertEqual(Local(run=run, receipt=receipt).device('registry').returncode, 0)
        self.assertEqual(log.read_text(), 'device registry\n')

    def old_vps(self):
        self.devices(MAC, VPS)
        home, log = self.target(support=False)
        return (lambda target: Remote(target, run=over_ssh(home))), log

    def test_sync_never_hands_device_words_to_an_old_launcher(self):
        remotes, log = self.old_vps()
        code, out, _ = self.run_cli(['sync'], remotes=remotes)
        self.assertEqual(code, 1); self.assertIn('vps: outdated: run claude-pool device add justus@vps', out)
        self.assertFalse(log.exists())

    def test_owner_lookup_never_hands_device_words_to_an_old_launcher(self):
        remotes, log = self.old_vps()
        self.assertIsNone(accounts_cli.owner_lookup('user-a', 'team-a', remotes=remotes))
        self.assertFalse(log.exists())

    def test_the_readings_puller_journals_an_outdated_owner_once(self):
        remotes, log = self.old_vps()
        journal = Mock()
        puller = readings.Puller(self.root / 'shared.json', journal, remotes)
        self.assertFalse(puller.run_once()); self.assertFalse(puller.run_once())
        journal.record.assert_called_once_with('readings_sync_failed', reason='outdated')
        self.assertFalse(log.exists())

    def test_peer_results_name_outdated_and_missing_launchers(self):
        self.devices(MAC, VPS)
        for code, result in [(86, 'outdated: run claude-pool device add justus@vps'), (87, 'missing')]:
            fake = FakeRemote('justus@vps', lambda argv, data: answer(97), device_exit=code)
            self.assertEqual(device_sync.sync_all(lambda target: fake), {'vps': result})
            self.assertEqual(fake.calls, [])


# Orchestration: device add, check and remove ----------------------------------------------------------------------------


CUA_FIXTURE_TOKEN = 'cua-pairing-' + 'k' * 24
WANTED = '1.2.3'
NATIVE_PATH = '/opt/claude/bin/claude'
SELF_ROW = {'name': 'mac', 'platform': 'darwin', 'roles': [], 'addedAt': 1}
TEAM_LOGIN = {'loggedIn': True, 'email': 'team@x.de', 'orgName': 'Team A'}


def ok(stdout=b''): return answer(0, stdout if isinstance(stdout, bytes) else stdout.encode())


class Machine:
    """A whole machine behind FakeRemote: answers every call the device steps make, keeps its state and logs every call."""
    def __init__(self, ssh, log, *, platform='linux', reachable=True, launcher='current', native=NATIVE_PATH, version=WANTED,
                 linger='yes', shell=True, self_name=None, registry_value=None, accounts=None, service='running',
                 doctor=True, login=TEAM_LOGIN, cua=False, ip=None, preview='ours', rules=True, https=True,
                 overview=None, shared_at='fresh', conductor_part=None, switch_pending=(), herdr=None, label='io.github.claude-pool'):
        self.ssh, self.log = ssh, log
        self.platform, self.reachable, self.launcher, self.native, self.version = platform, reachable, launcher, native, version
        self.linger, self.shell, self.self_name, self.registry = linger, shell, self_name, registry_value
        self.accounts = dict(accounts or {})
        self.service, self.doctor, self.login = service, doctor, login
        self.cua, self.ip, self.host, self.spaces, self.mcp = cua, ip, False, [], False
        self.preview, self.rules, self.https = preview, rules, https
        self.overview = overview if overview is not None else {'health': {'activeRequests': 0}, 'sessions': [], 'accounts': []}
        self.shared_at = int(time.time() * 1000) - 60_000 if shared_at == 'fresh' else shared_at
        self.conductor = conductor_part or {'installed': False, 'running': False, 'ready': False, 'fresh': False, 'duplicates': []}
        self.switch_pending, self.herdr = list(switch_pending), herdr
        self.label, self.unit_loaded = label, True
        self.fail = set()  # e.g. {'shell add'}
        self.calls = []
        machine = self

        class Remote_(FakeRemote):
            def device(self, *args, stdin=None, timeout=120):
                if machine.reachable and machine.launcher != 'current':
                    return answer(86 if machine.launcher == 'old' else 87)
                return FakeRemote.device(self, *args, stdin=stdin, timeout=timeout)

            def reachable(self): return self.call(['true']).returncode == 0

        self.remote = Remote_(ssh, self.handle)

    def status(self):
        rows = [{'name': n, 'kind': r.get('kind', 'long-lived'), 'fingerprint': r['fingerprint'], 'expiresAt': None,
                 'active': r.get('active', True), 'servingFingerprint': r['fingerprint']} for n, r in sorted(self.accounts.items())]
        known = self.registry or registry.EMPTY()
        return {'name': self.self_name, 'version': self.version, 'platform': self.platform, 'service': self.service,
                'accounts': rows, 'switchPending': self.switch_pending, 'registryGeneration': known['generation'],
                'registryHash': registry.digest(known), 'linger': self.linger == 'yes', 'shell': self.shell,
                'nativeLogin': {'email': self.login['email'], 'organization': self.login['orgName']} if self.login else None,
                'herdrProfiles': [], 'conductor': self.conductor,
                'cua': {'host': self.host, 'spaces': list(self.spaces), 'mcp': self.mcp} if self.cua else None,
                'preview': {'installed': self.preview == 'ours', 'https': self.https}, 'overview': self.overview,
                'sharedReadingsAt': self.shared_at, 'serviceLabel': self.label}

    def event(self, label): self.log.append((self.ssh, label))

    def handle(self, argv, data):
        self.calls.append((argv, data))
        if not self.reachable: return answer(255)
        if argv == ['true']: return ok()
        if argv == ['uname', '-s']: return ok({'linux': 'Linux', 'darwin': 'Darwin'}[self.platform])
        if argv == ['id', '-un']: return ok('justus')
        if argv == ['loginctl', 'enable-linger']:
            if self.linger == 'no': self.linger = 'yes'
            return ok()
        if self.native and argv[:1] == [self.native]: return self.native_call(argv[1:])
        if argv[:1] == ['herdr']: return self.herdr_call(argv[1:])
        if argv[:2] == ['sh', '-c']: return self.script(argv[2], argv[3:], data)
        if argv[:1] == [POOL]: return self.pool(argv[1:], data)
        return answer(127)

    def native_call(self, args):
        if args == ['auth', 'status', '--json']:
            return ok(json.dumps(self.login or {'loggedIn': False}))
        if args == ['mcp', 'get', 'cua']: return answer(0 if self.mcp else 1)
        if args[:3] == ['mcp', 'add', '-s']: self.mcp = True; return ok()
        if args[:2] == ['mcp', 'remove']: self.event('cua'); self.mcp = False; return ok()
        return answer(1)

    def herdr_call(self, args):
        if self.herdr is None: return answer(127)
        if args == ['machine', 'list', '--json']: return ok(json.dumps(self.herdr))
        if args[:2] == ['machine', 'add']:
            self.herdr.append({'id': 'p' + str(len(self.herdr)), 'label': args[args.index('--label') + 1], 'target': args[2]})
            return ok()
        if args[:2] == ['machine', 'remove']:
            self.event('herdr'); self.herdr[:] = [r for r in self.herdr if r['id'] != args[2]]; return ok()
        return answer(1)

    def script(self, script, rest, data):
        if script == device_install.NATIVE_SCRIPT: return ok(self.native + '\n') if self.native else answer(1)
        if script == device_install.LAUNCHER_SCRIPT: return answer({'current': 0, 'old': 11, 'none': 10}[self.launcher])
        if script == device_install.INSTALL_SCRIPT:
            if 'install' in self.fail: return answer(1)
            self.launcher, self.version = 'current', WANTED
            return ok()
        if 'loginctl show-user' in script: return ok(self.linger)
        if script == devices_cli.UNINSTALL_SCRIPT: self.event('uninstall'); self.launcher = 'none'; return ok()
        if 'systemctl --user disable' in script or 'launchctl bootout' in script:
            self.event('unit')
            if 'unit' in self.fail: return answer(1)
            self.unit_loaded = False; return ok()
        if 'is-active' in script or 'launchctl print' in script: return answer(0 if self.unit_loaded else 1)
        if script == device_preview._SHA_SCRIPT:
            return ok({'ours': device_preview._tool_sha(), 'absent': 'absent', 'other': 'f' * 64,
                       'previous': sorted(device_preview.KNOWN_PREVIOUS)[0]}[self.preview])
        if script == device_preview._UPDATE_SCRIPT: self.updates = getattr(self, 'updates', 0) + 1; self.preview = 'ours'; return ok()
        if script == device_preview._INSTALL_SCRIPT: self.preview = 'ours'; return ok()
        if script == device_preview._UNLINK_SCRIPT: self.event('preview'); self.preview = 'absent'; return ok()
        if script == device_preview._RUN_SCRIPT:
            if self.preview not in ('ours', 'previous'): return answer(127)
            if rest[1:] == ['stop', '--all'] and 'preview' in self.fail: return answer(1)
            if rest[1:] == ['doctor']: return ok('backend ' + ('https' if self.https else 'http') + '\n')
            self.event('preview'); return ok()
        if script.startswith(device_cua.PATH_PREFIX): return self.cua_call(script, rest, data)
        return answer(127)

    def cua_call(self, script, rest, data):
        if script.endswith('command -v cua'): return ok('/usr/local/bin/cua\n') if self.cua else answer(1)
        args = rest[1:]
        if script == device_cua.TOKEN_WRAPPER:
            if not data or not data.strip(): return answer(1)
            self.spaces.append('direct:' + args[args.index('--embedded') + 1]); return ok()
        if args[:3] == ['tailscale', 'ip', '-4']: return ok(self.ip + '\n') if self.ip else answer(127)
        if not self.cua or args[:1] != ['cua']: return answer(127)
        if args[1:] == ['host', 'status', '--embedded', '--json']:
            return ok(json.dumps({'configured': self.host, 'service': {'running': self.host}, 'directUrl': f'{self.ip}:3211'}))
        if args[1:3] == ['host', 'setup']: self.host = True; return ok('pairing ' + CUA_FIXTURE_TOKEN)
        if args[1:] == ['host', 'remove', '--embedded']: self.event('cua'); self.host = False; return ok()
        if args[1:] == ['spaces', 'ls', '--json']: return ok(json.dumps({'spaces': [{'id': s} for s in self.spaces]}))
        if args[1:3] == ['spaces', 'rm']: self.event('cua'); self.spaces.remove(args[3]); return ok()
        return answer(1)

    def pool(self, args, data):
        if args[:1] == ['device']: return self.device_verb(args[1:], data)
        if args == ['--start']: self.service = 'running'; return ok()
        if args == ['--stop']: self.event('--stop'); self.service = 'stopped'; return ok()
        if args == ['doctor']: return ok() if self.doctor else answer(1, 'Doctor\n  fail tokens · fix: claude-pool account add x\n'.encode())
        if args[1:2] == ['-p']: return ok('OK')
        return answer(97)

    def device_verb(self, args, data):
        if args[:1] == ['status']: return ok(json.dumps(self.status()))
        if args == ['registry']: return ok(json.dumps(self.registry or registry.EMPTY()))
        if args == ['receive-registry']:
            incoming = json.loads(data)
            if self.registry is None or incoming['generation'] > self.registry['generation']: self.registry = incoming
            return ok(json.dumps({'result': 'replaced'}))
        if args[:1] == ['receive-account']:
            value = json.loads(data)
            self.accounts[args[1]] = {'kind': 'long-lived', 'fingerprint': long_lived.fingerprint(value)}
            return ok('installed')
        if args[:1] == ['self']: self.self_name = args[1]; return ok()
        if args[:1] == ['shell']:
            if args[1] == 'check': return answer(0 if self.shell else 1)
            if args[1] == 'add':
                if 'shell add' in self.fail: return answer(1)
                self.shell = True; return ok()
            if 'shell remove' in self.fail: return answer(1)
            self.event('shell'); self.shell = False; return ok()
        if args[:1] == ['preview-rules']:
            if args[1] == 'check': return answer(0 if self.rules else 1)
            if args[1] == 'remove':
                if 'preview' in self.fail: return answer(1)
                self.event('preview')
            self.rules = args[1] == 'add'; return ok()
        if args == ['conductor', 'install']:
            self.conductor_installs = getattr(self, 'conductor_installs', 0) + 1
            if self.conductor.get('duplicates') or 'conductor install' in self.fail:
                self.conductor = dict(self.conductor, **getattr(self, 'after_failed_install', {}))
                return answer(1, b'vps\n  failed      conductor     another Conductor is running\n')
            self.conductor = {'installed': True, 'running': True, 'ready': True, 'fresh': False, 'duplicates': []}; return ok()
        if args == ['cua-token']: return ok(CUA_FIXTURE_TOKEN) if self.host else answer(1)
        if args == ['forget-accounts']:
            if self.service != 'stopped': return answer(1)
            self.event('forget-accounts')
            self.accounts = {n: r for n, r in self.accounts.items() if r.get('kind') != 'long-lived'}
            return ok(json.dumps({'removed': [], 'kept': [], 'failed': {}}))
        return answer(2)


def step_names(text): return re.findall(r'^  (?:ok|missing|manual|skipped|failed|unreachable) +(\S+)', text, re.M)
def step_of(text, name): return re.search(r'^  (\S+) +' + re.escape(name) + r' +(.*)$', text, re.M)


class OrchestrationBase(DeviceCliBase):
    def setUp(self):
        super().setUp(); self.parts_off()
        self.log = []
        self.team = long_lived_file(self.directory, 'team')
        self.team_fp = long_lived.fingerprint(json.loads(self.team.read_bytes()))
        registry.update(lambda value: value['accounts'].append(dict(TEAM)), self.reg)
        self.app = self.root / 'app'; self.app.mkdir()
        (self.app / 'ssh_configs.json').write_text(json.dumps({'configs': [{'name': 'vps', 'sshHost': 'justus@vps', 'id': '1'}],
                                                              'trustedHosts': ['justus@vps']}))
        self.local = Machine('local', self.log, platform='darwin', self_name='mac', herdr=[], ip='100.64.0.1')
        self.vps = Machine('justus@vps', self.log)
        self.machines = {'justus@vps': self.vps}
        self.login = Mock(return_value=Step('native-login', 'ok', 'logged in'))
        patches = [patch.object(device_install, 'source_archive', lambda root: b'ARCHIVE'),
                   patch.object(native_login, 'login', self.login),
                   patch.object(devices_cli, 'local_platform', lambda: 'darwin'),
                   patch.object(device_integrations, 'APP_DIR', self.app),
                   patch.object(device_sync, 'build_version', lambda: WANTED),
                   patch.object(devices_cli, '_sleep', lambda seconds: None)]
        for item in patches: item.start(); self.addCleanup(item.stop)

    def remotes(self, target): return self.machines[target].remote

    def cli(self, *argv):
        return self.run_cli(list(argv), remotes=self.remotes, local=self.local.remote)

    def all_calls(self): return [call for m in [self.local, *self.machines.values()] for call in m.calls]

    def count(self, machine, words):
        words = words.split()
        def has(argv): return any(argv[i:i + len(words)] == words for i in range(len(argv)))
        script = {'install': device_install.INSTALL_SCRIPT}
        return sum(1 for argv, _ in machine.calls if has(argv) or (words[0] in script and script[words[0]] in argv))




class AddTests(OrchestrationBase):
    def fresh_vps(self, **kwargs):
        options = dict(launcher='none', version=None, linger='no', shell=False, service='stopped', preview='absent', rules=False)
        options.update(kwargs)
        self.vps = Machine('justus@vps', self.log, **options); self.machines['justus@vps'] = self.vps
        return self.vps

    def test_add_runs_every_step_in_order_and_ends_with_a_health_report(self):
        self.fresh_vps()
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps', '--usage-owner')
        self.assertEqual(code, 0, out + err); self.assertEqual(step_names(out), list(ADD_ORDER))
        value = registry.load(self.reg)
        self.assertEqual({d['name'] for d in value['devices']}, {'mac', 'vps'})
        self.assertEqual(registry.owner(value)['name'], 'vps')
        self.assertEqual(value['conductor']['device'], 'vps'); self.assertEqual(value['conductor']['email'], 'team@x.de')
        # The Conductor's home (CLAUDE.md, bin/, board/, journal.md, state/ on personal-vps) is its working directory.
        self.assertEqual(value['conductor']['cwd'], '~/projects/conductor')
        self.assertEqual(self.vps.self_name, 'vps'); self.assertEqual(self.vps.accounts['team']['fingerprint'], self.team_fp)
        self.assertEqual(self.vps.registry['generation'], value['generation'])
        self.assertTrue(self.vps.shell); self.assertEqual(self.vps.linger, 'yes'); self.assertEqual(self.vps.service, 'running')
        self.assertTrue(self.vps.rules); self.assertEqual(self.vps.preview, 'ours')
        self.assertEqual([r['target'] for r in self.local.herdr], ['justus@vps'])

    def test_add_checks_the_launcher_before_any_device_verb(self):
        self.fresh_vps()
        self.cli('add', 'justus@vps', '--name', 'vps')
        order = [argv for argv, _ in self.vps.calls]
        first_device = next(i for i, argv in enumerate(order) if argv[1:2] == ['device'])
        first_install = next(i for i, argv in enumerate(order) if device_install.INSTALL_SCRIPT in argv)
        self.assertLess(first_install, first_device)
        self.assertEqual(order[0], ['true'])

    def test_add_is_idempotent_and_resumes_a_half_finished_machine(self):
        self.fresh_vps().fail.add('shell add')
        code, out1, err = self.cli('add', 'justus@vps', '--name', 'vps')
        self.assertEqual(code, 2, out1 + err); self.assertRegex(out1, r'shell .*\n      fix: \S')
        self.vps.fail.clear(); self.vps.calls.clear(); self.local.calls.clear()
        code, out2, err = self.cli('add', 'justus@vps', '--name', 'vps')
        self.assertEqual(code, 0, out2 + err)
        for verb in ['install', 'shell add', 'receive-account', 'preview-rules add']:
            self.assertLessEqual(self.count(self.vps, verb), 1, verb)
        self.assertLessEqual(self.count(self.local, 'machine add'), 1)
        generation = registry.load(self.reg)['generation']
        self.vps.calls.clear(); self.local.calls.clear()
        self.assertEqual(self.cli('add', 'justus@vps', '--name', 'vps')[0], 0)
        self.assertEqual(registry.load(self.reg)['generation'], generation)
        for verb in ['install', 'shell add', 'receive-account', 'preview-rules add', 'receive-registry', '--start', 'enable-linger']:
            self.assertEqual(self.count(self.vps, verb), 0, verb)
        self.assertEqual(self.count(self.local, 'machine add'), 0)

    def test_manual_steps_do_not_block_and_are_listed_under_needs_you(self):
        (self.app / 'ssh_configs.json').unlink()
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps')
        self.assertEqual(code, 0, out + err); self.assertIn('\nNeeds you:\n  app: ', out)

    def test_add_never_leaks_a_token_anywhere(self):
        self.fresh_vps(cua=True, ip='100.64.0.2'); self.local.cua = True
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps', '--cua', '--usage-owner')
        self.assertEqual(code, 0, out + err)
        self.assertIn('direct:100.64.0.2:3211', self.local.spaces); self.assertIn('direct:100.64.0.1:3211', self.vps.spaces)
        watch = SecretWatch(fixture_token(), CUA_FIXTURE_TOKEN)
        watch.text(out, err); watch.argv(self.all_calls())
        watch.tree(self.root, allowed=[self.team])
        receive = [data for argv, data in self.vps.calls if 'receive-account' in argv]
        self.assertEqual(receive, [self.team.read_bytes()])

    def snapshot(self):
        return {str(p): (p.read_bytes() if p.is_file() else None) for p in self.root.rglob('*')}

    def test_add_on_an_unreachable_target_writes_nothing(self):
        (self.self_path).unlink()  # this device has no name yet either: still nothing is written
        self.vps.reachable = False
        before = self.snapshot()
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps')
        self.assertEqual(code, 2); self.assertEqual(self.snapshot(), before)
        self.assertRegex(out, r'unreachable +reachable .*\n      fix: ssh justus@vps true must work without a password prompt \(BatchMode\)')
        self.assertEqual([argv for argv, _ in self.vps.calls], [['true']]); self.assertEqual(self.local.calls, [])

    def test_add_names_this_device_from_its_hostname_when_it_has_none(self):
        self.self_path.unlink()
        with patch.object(devices_cli.socket, 'gethostname', return_value='Justus-MacBook.local'):
            self.assertEqual(self.cli('add', 'justus@vps', '--name', 'vps')[0], 0)
        self.assertEqual(registry.self_name(self.self_path), 'justus-macbook')
        self.assertEqual({d['name'] for d in registry.load(self.reg)['devices']}, {'justus-macbook', 'vps'})

    def test_add_skips_and_reports_steps_a_machine_cannot_do(self):
        self.local.herdr, self.local.ip, self.vps.ip = None, None, None
        with patch.object(devices_cli, 'local_platform', lambda: 'linux'):
            code, out, err = self.cli('add', 'justus@vps', '--name', 'vps', '--cua')
        self.assertEqual(code, 0, out + err)
        for name in ['herdr', 'app', 'cua']:
            found = step_of(out, name)
            self.assertEqual(found.group(1), 'skipped', name); self.assertTrue(found.group(2).strip(), name)

    def test_existing_native_login_is_not_repeated(self):
        self.assertEqual(self.cli('add', 'justus@vps', '--name', 'vps')[0], 0)
        self.login.assert_not_called()

    def test_a_missing_native_login_runs_the_one_click_login_once(self):
        self.vps.login = None
        def login(target, native):
            self.vps.login = TEAM_LOGIN
            return Step('native-login', 'ok', 'logged in on ' + target)
        self.login.side_effect = login
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps')
        self.assertEqual(code, 0, out + err); self.login.assert_called_once_with('justus@vps', NATIVE_PATH)

    def test_an_outdated_launcher_is_reinstalled_and_never_handed_device_words(self):
        self.vps.launcher = 'old'
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps')
        self.assertEqual(code, 0, out + err)
        installed = next(i for i, (argv, _) in enumerate(self.vps.calls) if device_install.INSTALL_SCRIPT in argv)
        self.assertFalse([argv for argv, _ in self.vps.calls[:installed] if argv[1:2] == ['device']])

    def test_add_tells_every_other_device_about_the_new_one(self):
        registry.update(lambda value: value['devices'].append({'name': 'lap', 'ssh': 'justus@lap', 'platform': 'darwin', 'roles': []}), self.reg)
        lap = Machine('justus@lap', self.log, platform='darwin', self_name='lap', registry_value=registry.load(self.reg))
        self.machines['justus@lap'] = lap
        self.assertEqual(self.cli('add', 'justus@vps', '--name', 'vps')[0], 0)
        self.assertEqual({d['name'] for d in lap.registry['devices']}, {'mac', 'vps', 'lap'})
        self.assertFalse([argv for argv, _ in lap.calls if '--start' in argv or '--stop' in argv])

    def test_add_refuses_a_name_that_belongs_to_another_target(self):
        registry.update(lambda value: value['devices'].append({'name': 'vps', 'ssh': 'justus@other', 'platform': 'linux', 'roles': []}), self.reg)
        before = registry.load(self.reg)
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps')
        self.assertEqual(code, 2); self.assertIn('justus@other', out + err); self.assertEqual(registry.load(self.reg), before)
        self.assertEqual(self.vps.calls, [])


class CheckTests(OrchestrationBase):
    def setUp(self):
        super().setUp()
        registry.update(lambda value: value['devices'].extend([dict(SELF_ROW), dict(VPS, ssh='justus@vps', roles=[])]), self.reg)
        known = registry.load(self.reg)
        self.vps.registry, self.vps.self_name = known, 'vps'
        self.vps.accounts = {'team': {'kind': 'long-lived', 'fingerprint': self.team_fp}}
        self.local.herdr = [{'id': 'p1', 'label': 'vps', 'target': 'justus@vps'}]
        self.local.accounts = dict(self.vps.accounts); self.local.registry = known

    def test_a_healthy_device_is_ready(self):
        code, out, err = self.cli('check', 'vps')
        self.assertEqual(code, 0, out + err); self.assertEqual(step_names(out), list(CHECK_NAMES))
        self.assertEqual(self.count(self.vps, '-p'), 0)

    def test_check_never_changes_the_target(self):
        self.vps.shell = False; self.vps.service = 'stopped'; self.vps.linger = 'no'; self.vps.rules = False
        self.cli('check', 'vps')
        for verb in ['shell add', '--start', 'enable-linger', 'preview-rules add', 'receive-account', 'receive-registry', 'self']:
            self.assertEqual(self.count(self.vps, verb), 0, verb)
        self.assertEqual(self.count(self.local, 'machine add'), 0)

    def test_check_lists_each_missing_item_with_its_exact_fix(self):
        registry.update(lambda value: (registry.device(value, 'vps').update(cua=True), value.update(
            conductor={'device': 'vps', 'email': 'team@x.de', 'organization': 'Team A', 'cwd': '/srv/c'})), self.reg)
        self.vps.native, self.vps.version, self.vps.linger, self.vps.shell = None, '0.9', 'no', False
        self.vps.accounts, self.vps.service, self.vps.doctor = {}, 'stopped', False
        self.vps.cua, self.vps.ip, self.vps.preview, self.vps.rules = False, '100.64.0.2', 'absent', False
        self.vps.shared_at = None; self.local.herdr = []
        registry.update(lambda value: registry.device(value, 'vps')['roles'].clear() or registry.device(value, 'mac')['roles'].append('usage'), self.reg)
        (self.app / 'ssh_configs.json').unlink()
        code, out, err = self.cli('check', 'vps')
        self.assertEqual(code, 2, out + err)
        for name in CHECK_NAMES[1:]:
            if name == 'install': continue  # the launcher is current; every other step is broken
            self.assertRegex(out, r'\n  \S+ +' + re.escape(name) + r' .*\n      fix: \S', name)
        self.assertNotRegex(out, r'\n  ok +(?!reachable|install)\S')
        self.assertIn('fix: claude-pool account add x', out)

    def test_an_outdated_launcher_fails_install_and_every_step_that_needs_it_names_the_fix(self):
        self.vps.launcher, self.vps.native, self.vps.ip = 'old', None, '100.64.0.2'
        self.vps.linger = 'no'; self.local.herdr = []; (self.app / 'ssh_configs.json').unlink()
        registry.update(lambda value: registry.device(value, 'mac')['roles'].append('usage'), self.reg)
        registry.update(lambda value: (registry.device(value, 'vps').update(cua=True), value.update(
            conductor={'device': 'vps', 'email': 'team@x.de', 'organization': 'Team A', 'cwd': '/srv/c'})), self.reg)
        code, out, err = self.cli('check', 'vps')
        self.assertEqual(code, 2)
        self.assertRegex(out, r'\n  failed +install +outdated launcher.*\n      fix: claude-pool device add justus@vps')
        for name in CHECK_NAMES[1:]:
            self.assertRegex(out, r'\n  \S+ +' + re.escape(name) + r' .*\n      fix: \S', name)
        self.assertFalse([argv for argv, _ in self.vps.calls if argv[1:2] == ['device']])

    def test_check_reports_reauthenticate_from_the_overview_without_probe(self):
        self.vps.overview = {'health': {'activeRequests': 0}, 'sessions': [], 'accounts': [{'name': 'team', 'status': 'reauthenticate'}]}
        code, out, _ = self.cli('check', 'vps')
        self.assertEqual(code, 2)
        self.assertRegex(out, r'accounts .*reauthenticate.*\n      fix: .*account add team@x\.de --long-lived --replace --name team.*first enrolled.*browser')
        self.assertEqual(self.count(self.vps, '-p'), 0)

    def test_check_detects_fingerprint_drift_and_registry_divergence(self):
        self.vps.accounts['team']['fingerprint'] = '0' * 12
        diverged = json.loads(json.dumps(self.vps.registry)); diverged['devices'][0]['addedAt'] = 99
        self.vps.registry = diverged
        code, out, _ = self.cli('check', 'vps')
        self.assertEqual(code, 2); self.assertIn('fingerprint', out); self.assertIn('registry diverged', out)
        self.assertIn('claude-pool device sync --force-registry', out)

    def test_probe_runs_one_real_claude_request_per_long_lived_account(self):
        self.vps.accounts['personal'] = {'kind': 'refreshable', 'fingerprint': '1' * 12}
        code, out, err = self.cli('check', 'vps', '--probe')
        self.assertEqual(code, 0, out + err)
        probes = [argv for argv, _ in self.vps.calls if '-p' in argv]
        self.assertEqual(probes, [['.local/bin/claude-pool', 'team', '-p', 'Reply with OK.', '--model', 'haiku']])

    def test_check_derives_switch_pending_conductor_fresh_and_error(self):
        registry.update(lambda value: value.update(conductor={'device': 'vps', 'email': 'team@x.de', 'organization': 'Team A',
                                                             'cwd': '/srv/c'}), self.reg)
        self.vps.registry = self.local.registry = registry.load(self.reg)
        self.vps.switch_pending = ['team']
        self.vps.conductor = {'installed': True, 'running': True, 'ready': True, 'fresh': True, 'duplicates': []}
        code, out, err = self.cli('check', 'vps')
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r'\n  manual +accounts .*switch.*\n      fix: ssh justus@vps claude-pool device switch')
        self.assertRegex(out, r'\n  manual +conductor ')
        self.vps.conductor = {'error': 'OSError'}
        code, out, _ = self.cli('check', 'vps')
        self.assertEqual(code, 2); self.assertIn('status error on vps: OSError', out)

    def test_check_applies_a_differing_registry_reserve_once_and_names_the_fix(self):
        registry.update(lambda value: registry.account(value, 'team').update(reserveAt=70), self.reg)
        self.config.write_text(json.dumps({'accounts': [{'name': 'team', 'label': 'Team', 'reserveAt': 50}]}))
        self.local.registry = self.vps.registry = registry.load(self.reg)
        with patch.object(accounts_cli, 'reserve_hook') as hook:
            code, out, _ = self.cli('check', 'mac')
        hook.assert_called_once_with(registry.load(self.reg))
        self.assertEqual(code, 2)
        self.assertRegex(out, r'\n  failed +accounts .*reserve.*\n      fix: claude-pool account reserve team --at 70')

        def apply(value): self.config.write_text(json.dumps({'accounts': [{'name': 'team', 'label': 'Team', 'reserveAt': 70}]}))
        with patch.object(accounts_cli, 'reserve_hook', side_effect=apply) as hook:
            code, out, err = self.cli('check', 'mac')
        hook.assert_called_once(); self.assertNotRegex(out, r'failed +accounts')

    def test_check_json_reports_every_device(self):
        code, out, err = self.cli('check', '--json')
        value = json.loads(out)
        self.assertEqual(set(value['devices']), {'mac', 'vps'}); self.assertEqual(code, 0 if value['ready'] else 2)
        self.assertEqual([s['name'] for s in value['devices']['vps']], list(CHECK_NAMES))

    def test_check_of_an_unknown_device_says_so(self):
        code, out, err = self.cli('check', 'nope')
        self.assertEqual(code, 2); self.assertIn('device list', out + err)


class RemoveTests(OrchestrationBase):
    def setUp(self):
        super().setUp()
        self.local.herdr = [{'id': 'p1', 'label': 'vps', 'target': 'justus@vps'}]
        registry.update(lambda value: value['devices'].extend([dict(SELF_ROW, roles=['usage']),
                                                               dict(VPS, ssh='justus@vps', roles=[], cua=True)]), self.reg)
        self.vps.registry, self.vps.self_name = registry.load(self.reg), 'vps'
        self.vps.accounts = {'team': {'kind': 'long-lived', 'fingerprint': self.team_fp},
                             'personal': {'kind': 'refreshable', 'fingerprint': '1' * 12}}
        self.vps.cua, self.vps.ip, self.vps.host, self.vps.mcp = True, '100.64.0.2', True, True
        self.local.cua, self.local.spaces = True, ['direct:100.64.0.2:3211']
        self.updates = []
        real = registry.update
        def update(change, path=None):
            self.log.append(('local', 'registry')); return real(change, path)
        item = patch.object(registry, 'update', update); item.start(); self.addCleanup(item.stop)

    def order(self):
        seen = []
        for _, label in self.log:
            if label not in seen: seen.append(label)
        return seen

    def test_remove_undoes_preview_shell_service_accounts_cua_herdr_and_registry(self):
        code, out, err = self.cli('remove', 'vps')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.order(), ['preview', 'shell', '--stop', 'unit', 'forget-accounts', 'cua', 'herdr', 'registry'])
        self.assertEqual(self.vps.accounts, {'personal': {'kind': 'refreshable', 'fingerprint': '1' * 12}})
        self.assertIsNone(registry.device(registry.load(self.reg), 'vps'))
        self.assertEqual(self.local.herdr, []); self.assertEqual(self.local.spaces, [])
        unit = [argv for argv, _ in self.vps.calls if 'systemctl' in ' '.join(argv)]
        self.assertEqual(unit, [['sh', '-c', service.unload_hint(service.DEFAULT_LABEL, 'linux')]])
        self.assertIn('remove the justus@vps environment', out)
        self.assertEqual(self.count(self.vps, 'uninstall'), 0)

    def test_remove_with_uninstall_runs_the_target_uninstaller_last(self):
        code, out, err = self.cli('remove', 'vps', '--uninstall')
        self.assertEqual(code, 0, out + err); self.assertEqual(self.order()[-1], 'uninstall')

    def mutating(self):
        reads = (['true'], ['uname', '-s'])
        return [argv for argv, _ in self.vps.calls + self.local.calls
                if tuple(argv) not in {tuple(r) for r in reads} and argv[1:3] != ['device', 'status']]

    def test_remove_refuses_with_running_tasks_the_conductor_or_the_usage_owner(self):
        before = registry.load(self.reg)
        cases = {
            'busy': lambda: setattr(self.vps, 'overview', {'health': {'activeRequests': 1}, 'sessions': [], 'accounts': []}),
            'conductor': lambda: registry.update(lambda value: value.update(conductor={'device': 'vps', 'email': 'team@x.de',
                                                                                      'organization': 'Team A', 'cwd': '/c'}), self.reg),
            'owner': lambda: registry.update(lambda value: (registry.device(value, 'mac')['roles'].clear(),
                                                            registry.device(value, 'vps')['roles'].append('usage')), self.reg),
            'only copy': lambda: self.team.unlink()}
        fixes = {'busy': '--wait', 'conductor': 'claude-pool device conductor move mac', 'owner': '--usage-owner', 'only copy': 'claude-pool device sync'}
        for case, prepare in cases.items():
            with self.subTest(case=case):
                prepare(); self.vps.calls.clear(); self.local.calls.clear(); self.log.clear()
                state = registry.load(self.reg)
                code, out, err = self.cli('remove', 'vps')
                self.assertNotEqual(code, 0); self.assertIn(fixes[case], out + err)
                self.assertEqual(self.mutating(), []); self.assertEqual(registry.load(self.reg), state)
                self.assertEqual(self.vps.service, 'running')
                # undo for the next case
                registry.write(registry.canonical(before), self.reg)
                self.vps.overview = {'health': {'activeRequests': 0}, 'sessions': [], 'accounts': []}
                if not self.team.exists(): self.team = long_lived_file(self.directory, 'team')

    def test_remove_refuses_this_device(self):
        code, out, err = self.cli('remove', 'mac')
        self.assertNotEqual(code, 0); self.assertEqual(self.local.calls, [])

    def test_remove_waits_with_wait(self):
        busy = {'n': 2}
        overview = self.vps.overview
        def status():
            busy['n'] -= 1
            return {'health': {'activeRequests': 1 if busy['n'] > 0 else 0}, 'sessions': [], 'accounts': []}
        original = self.vps.status
        self.vps.status = lambda: dict(original(), overview=status())
        code, out, err = self.cli('remove', 'vps', '--wait')
        self.assertEqual(code, 0, out + err); self.assertEqual(busy['n'], 0)  # busy on the first read, idle on the second
        self.assertIn('--stop', [argv[-1] for argv, _ in self.vps.calls])


class LocalPoolTests(OrchestrationBase):
    def test_device_commands_never_start_or_stop_the_local_pool(self):
        registry.update(lambda value: value['devices'].append(dict(SELF_ROW)), self.reg)
        with patch.object(launch, 'managed_start') as start, patch.object(launch, 'service_command') as command:
            self.assertEqual(self.cli('add', 'justus@vps', '--name', 'vps')[0], 0)
            self.cli('check')
            self.assertEqual(self.cli('remove', 'vps')[0], 0)
        start.assert_not_called(); command.assert_not_called()
        self.assertFalse([argv for argv, _ in self.local.calls if '--start' in argv or '--stop' in argv])
        self.assertIn('--stop', [argv[-1] for argv, _ in self.vps.calls])


SESSION_FIXTURE = 'cse_' + 'adopt123'
ADOPT_FIX = ('stop the hand-started Conductor (herdr session conductor), then run: '
             'ssh justus@vps claude-pool device conductor install --session-id ' + SESSION_FIXTURE)
HAND_STARTED = {'installed': False, 'running': False, 'ready': False, 'fresh': False, 'duplicates': [4242],
                'adoptSessionId': SESSION_FIXTURE}
IDLE_CONDUCTOR = {'installed': False, 'running': False, 'ready': False, 'fresh': False, 'duplicates': []}


class ConductorAddTests(OrchestrationBase):
    def test_a_hand_started_conductor_is_adopted_by_hand_then_the_rerun_installs(self):
        self.vps.conductor = dict(HAND_STARTED)
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps', '--usage-owner')
        self.assertEqual(getattr(self.vps, 'conductor_installs', 0), 0)  # never installs next to a running Conductor
        self.assertRegex(out, r'\n  manual +conductor .*\n      fix: ' + re.escape(ADOPT_FIX))
        self.assertIn('\nNeeds you:\n', out); self.assertIn('  conductor: ' + ADOPT_FIX, out)
        self.assertEqual(registry.load(self.reg)['conductor']['device'], 'vps')
        self.vps.conductor = dict(IDLE_CONDUCTOR)  # Justus stops the hand-started one
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps', '--usage-owner')
        self.assertEqual(code, 0, out + err); self.assertEqual(self.vps.conductor_installs, 1)
        self.assertRegex(out, r'\n  ok +conductor +ready on vps')

    def test_a_registered_conductor_that_is_not_running_is_installed_again(self):
        registry.update(lambda value: (value['devices'].extend([dict(SELF_ROW), dict(VPS, ssh='justus@vps', roles=[])]), value.update(
            conductor={'device': 'vps', 'email': 'team@x.de', 'organization': 'Team A', 'cwd': '/srv/c'})), self.reg)
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps')
        self.assertEqual(code, 0, out + err); self.assertEqual(self.vps.conductor_installs, 1)

    def test_a_failed_install_reports_the_real_conductor_step_not_a_generic_one(self):
        self.vps.after_failed_install = dict(HAND_STARTED)  # someone started one by hand meanwhile
        self.vps.fail.add('conductor install')
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps', '--usage-owner')
        self.assertEqual(self.vps.conductor_installs, 1)
        self.assertRegex(out, r'\n  manual +conductor .*\n      fix: ' + re.escape(ADOPT_FIX))
        self.assertNotIn('install failed', out); self.assertNotIn('installing the Conductor', out)


class ConductorAdoptTests(unittest.TestCase):
    def test_status_reports_the_session_id_of_a_hand_started_conductor(self):
        ps = ('  10     1 /opt/claude remote-control --session-id ' + SESSION_FIXTURE + ' --name Conductor Claude --permission-mode auto\n'
              '  11     1 /opt/claude remote-control --name Other\n')
        self.assertEqual(conductor.adoptable(ps, [10]), SESSION_FIXTURE)
        self.assertIsNone(conductor.adoptable(ps.replace(SESSION_FIXTURE, 'not-a-session'), [10]))
        self.assertIsNone(conductor.adoptable(ps, []))

    def test_check_names_the_adopt_command_for_the_device_it_runs_on(self):
        value = {'devices': [dict(VPS, roles=[]), MAC], 'accounts': [],
                 'conductor': {'device': 'vps', 'email': 'team@x.de', 'organization': 'Team A', 'cwd': '/c'}}
        step = conductor.check({'vps': {'platform': 'linux', 'linger': True, 'conductor': dict(HAND_STARTED)}}, value)
        self.assertEqual(step.fix, ADOPT_FIX)
        with patch.object(registry, 'self_name', return_value='vps'):
            step = conductor.check({'vps': {'platform': 'linux', 'linger': True, 'conductor': dict(HAND_STARTED)}}, value)
        self.assertEqual(step.fix, ADOPT_FIX.replace('ssh justus@vps ', ''))


class FixRoundCheckTests(OrchestrationBase):
    def setUp(self):
        super().setUp()
        registry.update(lambda value: value['devices'].extend([dict(SELF_ROW, roles=['usage']), dict(VPS, ssh='justus@vps', roles=[])]), self.reg)
        known = registry.load(self.reg)
        self.vps.registry, self.vps.self_name = known, 'vps'
        self.vps.accounts = {'team': {'kind': 'long-lived', 'fingerprint': self.team_fp}}
        self.local.herdr = [{'id': 'p1', 'label': 'vps', 'target': 'justus@vps'}]
        self.local.registry = known

    def test_readings_not_yet_arrived_are_manual_and_stale_readings_are_missing(self):
        fix = 'first readings arrive within 5 min; needs ssh from justus@vps to mac without a password prompt'
        self.vps.shared_at = None
        code, out, _ = self.cli('check', 'vps')
        self.assertEqual(code, 0, out); self.assertRegex(out, r'\n  manual +readings .*\n      fix: ' + re.escape(fix))
        self.vps.shared_at = int(time.time() * 1000) - 20 * 60_000
        code, out, _ = self.cli('check', 'vps')
        self.assertEqual(code, 2); self.assertRegex(out, r'\n  missing +readings ')

    def test_doctor_fixes_are_run_on_the_device(self):
        self.vps.doctor = False
        code, out, _ = self.cli('check', 'vps')
        self.assertRegex(out, r'\n  failed +doctor +fail tokens.*\n      fix: ssh justus@vps claude-pool account add x')

    def test_a_peer_whose_registry_is_ahead_is_reported(self):
        ahead = json.loads(json.dumps(self.vps.registry)); ahead['generation'] += 3
        self.vps.registry = ahead
        code, out, _ = self.cli('check', 'vps')
        self.assertEqual(code, 2); self.assertRegex(out, r'accounts .*ahead.*\n      fix: claude-pool device sync')


class FixRoundAddTests(OrchestrationBase):
    def test_add_waits_for_readings_then_leaves_a_manual_step(self):
        registry.update(lambda value: value['devices'].append(dict(SELF_ROW, roles=['usage'])), self.reg)
        self.vps.shared_at = None
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps')
        self.assertEqual(code, 0, out + err)
        self.assertIn('  readings: first readings arrive within 5 min; needs ssh from justus@vps to mac without a password prompt', out)

    def test_add_updates_an_earlier_preview_tool_even_when_it_answers(self):
        self.vps.preview = 'previous'
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps')
        self.assertEqual(code, 0, out + err); self.assertEqual(self.vps.updates, 1); self.assertEqual(self.vps.preview, 'ours')

    def test_add_prints_each_devices_sync_result_and_records_the_tailscale_address(self):
        self.vps.cua, self.vps.ip, self.local.cua = True, '100.64.0.2', True
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps', '--cua')
        self.assertEqual(code, 0, out + err); self.assertIn('sync vps: ok', out)
        value = registry.load(self.reg)
        self.assertEqual(registry.device(value, 'vps')['tailscaleIp'], '100.64.0.2')
        self.assertEqual(registry.device(value, 'mac')['tailscaleIp'], '100.64.0.1')


class FixRoundRemoveTests(OrchestrationBase):
    def setUp(self):
        super().setUp()
        self.local.herdr = [{'id': 'p1', 'label': 'vps', 'target': 'justus@vps'}, {'id': 'p2', 'label': 'vps', 'target': 'justus@other'}]
        registry.update(lambda value: value['devices'].extend([dict(SELF_ROW, roles=['usage']),
                                                               dict(VPS, ssh='justus@vps', roles=[], cua=True, tailscaleIp='100.64.0.2')]), self.reg)
        self.vps.registry, self.vps.self_name = registry.load(self.reg), 'vps'
        self.vps.accounts = {'team': {'kind': 'long-lived', 'fingerprint': self.team_fp}}
        self.vps.cua, self.vps.ip, self.vps.host, self.vps.mcp = True, '100.64.0.2', True, True
        self.local.cua, self.local.spaces = True, ['direct:100.64.0.2:3211', 'direct:100.64.0.9:3211']

    def test_a_failed_preview_or_shell_removal_stops_before_anything_irreversible_and_a_rerun_completes(self):
        for part, fix in [('preview', 'claude-pool device remove vps'), ('shell remove', 'ssh justus@vps claude-pool device shell remove')]:
            with self.subTest(part=part):
                self.vps.fail = {part}; self.vps.calls.clear()
                code, out, err = self.cli('remove', 'vps')
                self.assertNotEqual(code, 0); self.assertIn('fix: ' + fix, out)
                self.assertEqual([argv for argv, _ in self.vps.calls if '--stop' in argv or 'forget-accounts' in argv], [])
                self.assertIsNotNone(registry.device(registry.load(self.reg), 'vps'))
        self.vps.fail = set()
        code, out, err = self.cli('remove', 'vps')
        self.assertEqual(code, 0, out + err); self.assertIsNone(registry.device(registry.load(self.reg), 'vps'))

    def test_the_unit_is_disabled_by_the_installed_label_and_a_still_loaded_unit_fails(self):
        self.vps.label = 'io.example.pool'
        code, out, err = self.cli('remove', 'vps')
        self.assertEqual(code, 0, out + err)
        self.assertIn(['sh', '-c', service.unload_hint('io.example.pool', 'linux')], [argv for argv, _ in self.vps.calls])

    def test_a_unit_that_stays_loaded_is_a_failed_step(self):
        self.vps.fail = {'unit'}
        code, out, err = self.cli('remove', 'vps')
        self.assertNotEqual(code, 0); self.assertRegex(out, r'\n  failed +service .*\n      fix: ')
        self.assertIsNotNone(registry.device(registry.load(self.reg), 'vps'))

    def test_a_unit_that_was_not_loaded_is_fine(self):
        self.vps.fail = {'unit'}; self.vps.unit_loaded = False
        code, out, err = self.cli('remove', 'vps')
        self.assertEqual(code, 0, out + err)

    def test_remove_prints_each_devices_sync_result(self):
        registry.update(lambda value: value['devices'].append({'name': 'lap', 'ssh': 'justus@lap', 'platform': 'darwin', 'roles': []}), self.reg)
        self.machines['justus@lap'] = Machine('justus@lap', self.log, platform='darwin', self_name='lap', registry_value=registry.load(self.reg))
        code, out, err = self.cli('remove', 'vps')
        self.assertEqual(code, 0, out + err); self.assertIn('sync lap: ok', out)

    def test_forget_drops_an_unreachable_device_here_without_any_remote_call(self):
        self.vps.reachable = False
        code, out, err = self.cli('remove', 'vps', '--forget')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.vps.calls, [])
        self.assertIsNone(registry.device(registry.load(self.reg), 'vps'))
        self.assertEqual(self.local.herdr, [{'id': 'p2', 'label': 'vps', 'target': 'justus@other'}])
        self.assertEqual(self.local.spaces, ['direct:100.64.0.9:3211'])
        self.assertIn('tokens copied to vps remain', out); self.assertIn('revoke', out)
        self.assertIn('--long-lived --replace --name <name> on the device that first enrolled it', out)

    def test_forget_refuses_the_conductor_device_and_the_usage_owner(self):
        for prepare in [lambda value: value.update(conductor={'device': 'vps', 'email': 'team@x.de', 'organization': 'Team A', 'cwd': '/c'}),
                        lambda value: (registry.device(value, 'mac')['roles'].clear(), registry.device(value, 'vps')['roles'].append('usage'))]:
            with self.subTest():
                registry.update(prepare, self.reg); state = registry.load(self.reg)
                self.vps.calls.clear(); self.local.calls.clear()
                code, out, err = self.cli('remove', 'vps', '--forget')
                self.assertNotEqual(code, 0); self.assertEqual(registry.load(self.reg), state)
                self.assertEqual(self.vps.calls + self.local.calls, [])
                registry.update(lambda value: (value.update(conductor=None), registry.device(value, 'vps')['roles'].clear(),
                                               registry.device(value, 'mac').update(roles=['usage'])), self.reg)


class StrayLongLivedTests(DeviceCliBase):
    def setUp(self):
        super().setUp(); self.parts_off()

    def test_status_lists_a_long_lived_store_file_that_has_no_member_and_no_replica(self):
        long_lived_file(self.stores, 'stray')
        code, out, _ = self.run_cli(['status'])
        row = next(r for r in json.loads(out)['accounts'] if r['name'] == 'stray')
        self.assertEqual((row['kind'], row['active']), ('long-lived', False))

    def test_forget_accounts_moves_a_stray_long_lived_file_aside_and_never_deletes_it(self):
        (self.runtime / 'maintenance').write_text('Deliberately stopped\n')
        for key in ('GATEWAY_PORT', 'BACKEND_PORT', 'CONTROL_PORT'):
            probe = socket.socket(); probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]; probe.close()
            item = patch.object(settings, key, port); item.start(); self.addCleanup(item.stop)
        data = long_lived_file(self.stores, 'stray').read_bytes()
        code, out, err = self.run_cli(['forget-accounts'])
        self.assertEqual(code, 0, out + err)
        self.assertFalse((self.stores / 'stray.json').exists())
        moved = list((self.stores / '.retired').glob('stray-*.json'))
        self.assertEqual([path.read_bytes() for path in moved], [data])


class DoctorOutputTests(unittest.TestCase):
    def test_a_failing_first_line_keeps_its_fix(self):
        fake = FakeRemote('justus@vps', lambda argv, data: answer(1, '  fail tokens · fix: claude-pool account add x\n'.encode()))
        step = device_install.check_doctor(fake)
        self.assertEqual((step.status, step.fix), ('failed', 'claude-pool account add x'))


class FixRoundTwoTests(OrchestrationBase):
    def test_the_real_status_reports_the_installed_service_label_and_remove_uses_it(self):
        (self.root / 'installation.json').write_text(json.dumps({'serviceLabel': 'io.example.pool'}))
        status = device_sync.status()
        self.assertEqual(status['serviceLabel'], 'io.example.pool')
        self.assertEqual(devices_cli._service_label(status), 'io.example.pool')
        (self.root / 'installation.json').write_text(json.dumps({'serviceLabel': 'bad label; rm'}))
        self.assertEqual(device_sync.status()['serviceLabel'], service.DEFAULT_LABEL)
        (self.root / 'installation.json').unlink()
        self.assertEqual(device_sync.status()['serviceLabel'], service.DEFAULT_LABEL)

    def test_remove_disables_the_label_the_real_status_reports(self):
        (self.root / 'installation.json').write_text(json.dumps({'serviceLabel': 'io.example.pool'}))
        real = device_sync.status()
        registry.update(lambda value: value['devices'].extend([dict(SELF_ROW, roles=['usage']), dict(VPS, ssh='justus@vps', roles=[])]), self.reg)
        self.vps.registry, self.vps.self_name = registry.load(self.reg), 'vps'
        self.vps.label = real['serviceLabel']
        code, out, err = self.cli('remove', 'vps')
        self.assertEqual(code, 0, out + err)
        self.assertIn(['sh', '-c', service.unload_hint('io.example.pool', 'linux')], [argv for argv, _ in self.vps.calls])

    def test_a_symlinked_earlier_preview_tool_is_reported_and_never_replaced(self):
        old = sorted(device_preview.KNOWN_PREVIOUS)[0]
        self.vps.preview = 'previous'
        original = self.vps.script
        def script(text, rest, data):
            if text == device_preview._SHA_SCRIPT: return ok('link ' + old)
            return original(text, rest, data)
        self.vps.script = script
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps')
        self.assertEqual(code, 2)
        self.assertRegex(out, r'\n  failed +preview .*\n      fix: remove or rename ~/.local/bin/preview on justus@vps')
        self.assertEqual(getattr(self.vps, 'updates', 0), 0)
        self.assertEqual(sum(1 for argv, _ in self.vps.calls if device_preview._SHA_SCRIPT in argv), 3)  # check, install, recheck

    def test_the_tailscale_address_is_recorded_only_after_both_hosts_run(self):
        self.vps.cua, self.vps.ip, self.local.cua = True, '100.64.0.2', True
        original = self.vps.cua_call
        def cua_call(script, rest, data):
            if rest[2:4] == ['host', 'setup']: return answer(1)
            return original(script, rest, data)
        self.vps.cua_call = cua_call
        code, out, err = self.cli('add', 'justus@vps', '--name', 'vps', '--cua')
        self.assertEqual(code, 2)
        value = registry.load(self.reg)
        self.assertNotIn('tailscaleIp', registry.device(value, 'vps')); self.assertNotIn('tailscaleIp', registry.device(value, 'mac'))


if __name__ == '__main__':
    unittest.main()
