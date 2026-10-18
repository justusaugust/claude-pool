import importlib.util
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tarfile
import threading
import tomllib
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import settings
import launch

spec = importlib.util.spec_from_file_location('pool_installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ReleaseTests(unittest.TestCase):
    def test_config_validates_accounts_labels_aliases_ports_and_timezone(self):
        valid = {'accounts': [{'name': 'one', 'alias': 'personal', 'label': 'Personal'}], 'gateway_port': 28141, 'backend_port': 28140, 'control_port': 28142, 'timezone': 'UTC'}
        invalid = [{'accounts': [{'name': '../escape'}]}, {'accounts': [{'name': 'one', 'alias': 'usage'}]},
                   {'accounts': [{'name': 'one'}, {'name': 'one'}]}, {'gateway_port': True}, {'gateway_port': 18340},
                   {'accounts': [{'name': 'one', 'label': '\x1b[2J'}]}, {'timezone': 'not/a/zone'}, {'typo': 1}]
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'config.json'
            file.write_text(json.dumps(valid)); self.assertEqual(settings.load(file), valid)
            for value in invalid:
                file.write_text(json.dumps(value))
                with self.assertRaises(ValueError): settings.load(file)

    def test_generated_backend_is_parseable_and_keeps_account_identity(self):
        accounts = [{'name': 'personal', 'label': 'Personal'}, {'name': 'work', 'label': 'Work'}]
        with patch.object(settings, 'ACCOUNTS', accounts):
            value = tomllib.loads(settings.backend_config(Path('/tmp/state with "quotes"')))
        self.assertEqual([a['name'] for a in value['providers']['anthropic']['accounts']], ['personal', 'work'])
        self.assertEqual(value['server']['pool']['state_path'], '/tmp/state with "quotes"/quota.json')
        self.assertEqual(value['providers']['anthropic']['auth'], 'claude_oauth')

    def test_runtime_plugins_get_local_connection_paths_and_are_immutable(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launch, 'RUNTIME', Path(folder)), patch.object(launch, 'connection_ts', return_value='export const tokenFile = "/tmp/fixture-token"\n'):
            first = launch.configured_plugin('cockpit')
            self.assertEqual(first, launch.configured_plugin('cockpit'))
            self.assertIn('/tmp/fixture-token', (first / 'hooks/connection.ts').read_text())
            source = first / 'hooks/register.tsx'; source.write_text('changed')
            with self.assertRaisesRegex(RuntimeError, 'plugin changed'): launch.configured_plugin('cockpit')
            self.assertEqual(source.read_text(), 'changed')

    def test_private_directory_refuses_broad_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); target = root / 'private'
            settings.private_directory(target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            target.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'mode 0700'): settings.private_directory(target)
            link = root / 'link'; link.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, 'symlink'): settings.private_directory(link)

    def test_background_snapshots_survive_release_and_connection_changes(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launch, 'RUNTIME', Path(folder)), patch.object(launch, 'connection_ts') as connection:
            connection.return_value = 'export const hookPath = "/old/release/control_hook.py"\n'
            first = launch.background_plugin('work', 'same-origin')
            before = {str(p.relative_to(first)): p.read_bytes() for p in first.rglob('*') if p.is_file()}
            self.assertEqual(first, launch.background_plugin('work', 'same-origin'))
            connection.return_value = 'export const hookPath = "/new/release/control_hook.py"\n'
            second = launch.background_plugin('work', 'same-origin')
            self.assertNotEqual(first, second)
            self.assertIn('/new/release/', (second / 'hooks/connection.ts').read_text())
            self.assertEqual(before, {str(p.relative_to(first)): p.read_bytes() for p in first.rglob('*') if p.is_file()})

    def test_generated_control_hooks_execute_after_relocation_and_keep_native_permissions(self):
        calls, status = [], ['expired']
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                if self.headers.get('Authorization') != 'Bearer fixture-token':
                    self.send_response(403); self.end_headers(); return
                calls.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                body = {'id': 'fixture-request'} if self.path == '/internal/challenge' else {'status': status[0]}
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(body).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="pool hook ' $ ") as folder:
            root = Path(folder); source = root / 'relocated gateway'; source.mkdir()
            shutil.copytree(launch.HERE / 'session-control', source / 'session-control')
            for name in ['control_hook.py', 'settings.py', 'account_metadata.py', 'private_files.py']:
                shutil.copyfile(launch.HERE / name, source / name)
            stores = root / 'credentials'; stores.mkdir(); (stores / '.pool-client-token').write_text('fixture-token')
            config = root / "config ' $ .json"
            # Distinct ephemeral port; no requests can reach the user's real controls.
            ports = [server.server_port] + [p for p in range(28000, 28003) if p != server.server_port][:2]
            config.write_text(json.dumps({'credential_dir': str(stores), 'control_port': ports[0], 'gateway_port': ports[1], 'backend_port': ports[2]}))
            with patch.object(launch, 'HERE', source), patch.object(launch, 'RUNTIME', root / 'state'), patch.object(launch, 'CONFIG', config):
                plugin = launch.configured_plugin('session-control')
                self.assertEqual(plugin, launch.configured_plugin('session-control'))
            manifest = json.loads((plugin / 'hooks/hooks.json').read_text())
            commands = [hook['command'] for groups in manifest['hooks'].values() for group in groups for hook in group['hooks']]
            self.assertEqual(len(commands), 3)
            self.assertEqual(len(set(commands)), 1)
            self.assertNotIn('CLAUDE_PLUGIN_ROOT', commands[0])
            env = {k: v for k, v in os.environ.items() if not k.startswith('COCKPIT_CONTROL_')}
            env.update(CLAUDE_POOL_CONFIG='/missing/inherited/config', COCKPIT_CONTROL_TASK='task', COCKPIT_CONTROL_GENERATION='generation', COCKPIT_CONTROL_SESSION='native')
            def run(event, **values):
                result = subprocess.run(commands[0], shell=True, cwd=root, env=env,
                    input=json.dumps({'session_id': 'native', 'hook_event_name': event, 'tool_name': 'Bash', 'tool_input': {'command': 'public'}, **values}),
                    text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, '')
                return json.loads(result.stdout) if result.stdout else None
            self.assertIsNone(run('PreToolUse'))
            self.assertEqual(calls[-1][1]['operation'], 'prepare')
            for action in ['allow', 'deny', 'expired', 'local']:
                status[0] = action
                result = run('PermissionRequest')
                if action in ['allow', 'deny']:
                    decision = result['hookSpecificOutput']['decision']
                    self.assertEqual(decision['behavior'], action)
                    self.assertNotIn('updatedInput', decision); self.assertNotIn('updatedPermissions', decision)
                else: self.assertIsNone(result)
            self.assertIsNone(run('Notification', notification_type='permission_prompt'))
            self.assertEqual(calls[-1][1]['status'], 'needs-native')
            count = len(calls)
            self.assertIsNone(run('PermissionRequest', session_id='unrelated'))
            self.assertEqual(len(calls), count)
            (stores / '.pool-client-token').unlink()
            self.assertIsNone(run('PermissionRequest'))
            self.assertEqual(len(calls), count)

    def test_export_excludes_generated_sdk_binaries_and_runtime(self):
        pack_spec = importlib.util.spec_from_file_location('pool_packager', ROOT / 'scripts/package.py')
        packager = importlib.util.module_from_spec(pack_spec); pack_spec.loader.exec_module(packager)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ['gateway/main.py', 'gateway/bin/shunt', 'gateway/cockpit/.claude-plugin/types/secret.json', 'gateway/__pycache__/compiled.pyc', 'runtime/tokens.json', 'backend/upstream.json']:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('fixture')
            names = packager.source_files(root)
            self.assertIn('gateway/main.py', names); self.assertIn('backend/upstream.json', names)
            self.assertFalse(any('secret' in name or '/bin/' in name or 'runtime/' in name or '__pycache__' in name for name in names))

    def test_real_source_export_contains_phone_assets_and_verified_manifest(self):
        pack_spec = importlib.util.spec_from_file_location('pool_packager_actual', ROOT / 'scripts/package.py')
        packager = importlib.util.module_from_spec(pack_spec); pack_spec.loader.exec_module(packager)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'source.tar.gz'; packager.package(ROOT, output)
            with tarfile.open(output) as archive:
                for module in ['account_login.py', 'account_metadata.py', 'accounts_cli.py']:
                    self.assertGreater(len(archive.extractfile('claude-pool/gateway/' + module).read()), 100)
                for name in ['index.html', 'app.js', 'style.css']:
                    self.assertGreater(len(archive.extractfile('claude-pool/gateway/phone/' + name).read()), 100)
                manifest = json.load(archive.extractfile('claude-pool/SOURCE-MANIFEST.json'))
                for name, expected in manifest.items():
                    self.assertEqual(hashlib.sha256(archive.extractfile('claude-pool/' + name).read()).hexdigest(), expected)

    def test_backend_builder_checks_pin_and_uses_two_jobs_and_ui(self):
        build_spec = importlib.util.spec_from_file_location('pool_builder', ROOT / 'scripts/build_backend.py')
        builder = importlib.util.module_from_spec(build_spec); build_spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'backend').mkdir(); (root / 'gateway').mkdir()
            patch_file = root / 'backend/shunt.patch'; patch_file.write_text('fixture patch')
            pin = {'repository':'https://example.invalid/upstream.git', 'revision':'a' * 40, 'rustToolchain':'1.96.1', 'patchSha256':hashlib.sha256(patch_file.read_bytes()).hexdigest()}
            (root / 'backend/upstream.json').write_text(json.dumps(pin))
            cached = root / 'cache/release/shunt'; cached.parent.mkdir(parents=True); cached.write_text('stale backend')
            commands = []
            def run(argv, **kwargs):
                commands.append(list(argv))
                if argv[0] == 'cargo':
                    output = Path(kwargs['env']['CARGO_TARGET_DIR']) / 'release/shunt'
                    if 'clean' in argv: output.unlink(missing_ok=True)
                    else:
                        self.assertFalse(output.exists(), 'Cached application must be removed before building')
                        output.parent.mkdir(parents=True, exist_ok=True); output.write_text('fixture binary')
                    self.assertEqual(kwargs['env']['CARGO_BUILD_JOBS'], '2')
                return Mock(returncode=0)
            with patch.object(builder, 'ROOT', root), patch.dict(os.environ, {'CARGO_TARGET_DIR':str(root / 'cache')}), patch.object(builder.subprocess, 'run', side_effect=run): builder.main()
            self.assertIn(['git', 'fetch', '--depth', '1', 'origin', pin['revision']], commands)
            self.assertIn(['cargo', '+1.96.1', 'build', '--release', '--locked', '--features', 'ui', '-j', '2'], commands)
            clean = ['cargo', '+1.96.1', 'clean', '--release', '--locked', '--package', 'shunt-gateway']
            self.assertEqual([cmd for cmd in commands if cmd[0] == 'cargo'], [clean, ['cargo', '+1.96.1', 'build', '--release', '--locked', '--features', 'ui', '-j', '2'], clean])
            self.assertFalse(cached.exists(), 'Shared cache must not retain the application')
            self.assertEqual((root / 'gateway/bin/shunt').read_text(), 'fixture binary')
            (root / 'gateway/bin/shunt').unlink()
            cached.write_text('stale backend')
            with patch.object(builder, 'ROOT', root), patch.dict(os.environ, {'CARGO_TARGET_DIR':str(root / 'cache')}), patch.object(builder.subprocess, 'run', return_value=Mock(returncode=0)):
                with self.assertRaisesRegex(RuntimeError, 'Backend survived Cargo cleanup'): builder.main()
            self.assertFalse((root / 'gateway/bin/shunt').exists())
            patch_file.write_text('modified without review')
            with patch.object(builder, 'ROOT', root), patch.object(builder.subprocess, 'run') as network:
                with self.assertRaisesRegex(RuntimeError, 'digest changed'): builder.main()
                network.assert_not_called()

    def install_fixture(self, root):
        native = root / 'native-claude'; native.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "2.1.300 (Claude Code)"; else printf "%s\\n" "$@"; fi\n'); native.chmod(0o755)
        app, binaries, config = root / 'app', root / 'bin', root / 'private/config.json'
        with patch.object(installer.sys, 'platform', 'darwin'):
            installer.install(app, binaries, config, native, skip_backend=True)
        return app, binaries, config, native

    def test_install_help_and_uninstall_preserve_config_and_native(self):
        with tempfile.TemporaryDirectory(prefix='pool fresh home ') as folder:
            root = Path(folder); app, binaries, config, native = self.install_fixture(root)
            result = subprocess.run([str(binaries / 'claude-pool'), 'help'], text=True, capture_output=True, env=dict(os.environ, HOME=str(root)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage [--once]', result.stdout)
            self.assertFalse((root / '.local/state').exists()); self.assertFalse((root / '.shunt').exists())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(config.read_text())['accounts'], [])
            result = subprocess.run([str(binaries / 'claude-pool'), 'account', '--help'], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('add', result.stdout)
            result = subprocess.run([str(binaries / 'claude-native'), '--resume', 'public-fixture'], text=True, capture_output=True)
            self.assertEqual(result.stdout.splitlines(), ['--resume', 'public-fixture'])
            before = native.read_bytes(); saved = config.read_bytes()
            with patch.object(installer.subprocess, 'run', return_value=Mock(returncode=1)):
                installer.uninstall(app)
            self.assertFalse(app.exists()); self.assertFalse((binaries / 'claude-pool').exists())
            self.assertEqual(config.read_bytes(), saved); self.assertEqual(native.read_bytes(), before)

    def test_uninstall_refuses_loaded_service_or_modified_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            app, binaries, config, native = self.install_fixture(Path(folder))
            with patch.object(installer.subprocess, 'run', return_value=Mock(returncode=0)):
                with self.assertRaisesRegex(RuntimeError, 'service is loaded'): installer.uninstall(app)
            launcher = binaries / 'claude-pool'; launcher.write_text('#!/bin/sh\n# user edit\n')
            with patch.object(installer.subprocess, 'run', return_value=Mock(returncode=1)):
                with self.assertRaisesRegex(RuntimeError, 'Launcher changed'): installer.uninstall(app)
            self.assertTrue(app.exists()); self.assertIn('user edit', launcher.read_text())

    def test_install_refuses_existing_launchers_before_creating_app(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); native = root / 'native'; native.write_text('#!/bin/sh\nexit 0\n'); native.chmod(0o755)
            binaries = root / 'bin'; binaries.mkdir(); (binaries / 'claude-pool').write_text('user-owned')
            with self.assertRaisesRegex(RuntimeError, 'Refusing to replace'):
                installer.install(root / 'app', binaries, root / 'config.json', native, skip_backend=True)
            self.assertFalse((root / 'app').exists()); self.assertEqual((binaries / 'claude-pool').read_text(), 'user-owned')

    def test_copy_failure_leaves_no_application_or_launchers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(installer.shutil, 'copytree', side_effect=OSError('fixture disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'): self.install_fixture(root)
            self.assertFalse((root / 'app').exists())
            self.assertFalse((root / 'bin/claude-pool').exists())
            self.assertTrue((root / 'private/config.json').exists())

    def test_old_native_version_is_rejected_before_installing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); native = root / 'native'; native.write_text('#!/bin/sh\necho "2.1.250 (Claude Code)"\n'); native.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, '2.1.300'):
                installer.install(root / 'app', root / 'bin', root / 'config.json', native, skip_backend=True)
            self.assertFalse((root / 'app').exists()); self.assertFalse((root / 'config.json').exists())

    def test_native_updater_symlink_is_preserved_for_both_launchers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, version in [('first', '2.1.300'), ('second', '2.1.301')]:
                file = root / name
                file.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "' + version + ' (Claude Code)"; else echo "' + name + '"; fi\n'); file.chmod(0o755)
            link = root / 'claude'; link.symlink_to(root / 'first')
            app, binaries, config = root / 'app', root / 'bin', root / 'config.json'
            installer.install(app, binaries, config, link, skip_backend=True)
            self.assertEqual(json.loads(config.read_text())['native_claude'], str(link))
            link.unlink(); link.symlink_to(root / 'second')
            escape = subprocess.run([str(binaries / 'claude-native')], capture_output=True, text=True)
            version = subprocess.run([str(binaries / 'claude-pool'), '--version'], capture_output=True, text=True)
            self.assertEqual(escape.stdout.strip(), 'second')
            self.assertEqual(version.stdout.strip(), '2.1.301 (Claude Code)')

    def test_uninstall_removes_only_its_own_unloaded_launchd_definition(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); app, binaries, config, native = self.install_fixture(root)
            plist = root / 'Library/LaunchAgents/io.github.claude-pool.plist'
            plist.parent.mkdir(parents=True)
            definition = {'Label':'io.github.claude-pool', 'ProgramArguments':['python3','/another/app/launch.py','--daemon']}
            plist.write_bytes(plistlib.dumps(definition))
            with patch.object(installer.sys, 'platform', 'darwin'), patch.object(installer.Path, 'home', return_value=root), patch.object(installer.subprocess, 'run', return_value=Mock(returncode=1)):
                with self.assertRaisesRegex(RuntimeError, 'another installation'): installer.uninstall(app)
                self.assertTrue(app.exists()); self.assertTrue((binaries / 'claude-pool').exists())
                definition['ProgramArguments'][1] = str(app / 'gateway/launch.py')
                plist.write_bytes(plistlib.dumps(definition))
                installer.uninstall(app)
            self.assertFalse(plist.exists()); self.assertTrue(config.exists()); self.assertTrue(native.exists())

    def test_linux_install_uses_systemd_unit_paths_and_removes_only_its_own_unit(self):
        with tempfile.TemporaryDirectory(prefix='pool linux home ') as folder:
            root = Path(folder); native = root / 'native-claude'
            native.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "2.1.300 (Claude Code)"; fi\n'); native.chmod(0o755)
            app, binaries, config = root / 'app', root / 'bin', root / 'private/config.json'
            with patch.object(installer.sys, 'platform', 'linux'):
                self.assertEqual(installer.service_path({'serviceLabel': 'io.github.claude-pool'})[1].name, 'io.github.claude-pool.service')
                self.assertIn('.config/systemd/user', str(installer.service_path({})[1]))
                previous = os.umask(0o002)
                try: installer.install(app, binaries, config, native, skip_backend=True)
                finally: os.umask(previous)
                self.assertEqual(app.stat().st_mode & 0o777, 0o755)
                self.assertFalse((root / 'Library').exists()); self.assertFalse((root / '.config/systemd').exists())
                unit = root / '.config/systemd/user/io.github.claude-pool.service'; unit.parent.mkdir(parents=True)
                unit.write_bytes(installer.service.render({'Label': 'io.github.claude-pool', 'ProgramArguments': ['/usr/bin/python3', '/another/app/launch.py', '--daemon']}))
                with patch.object(installer.Path, 'home', return_value=root), patch.object(installer.subprocess, 'run', return_value=Mock(returncode=1)):
                    with self.assertRaisesRegex(RuntimeError, 'another installation'): installer.uninstall(app)
                    self.assertTrue(app.exists())
                    unit.write_bytes(installer.service.render({'Label': 'io.github.claude-pool', 'ProgramArguments': ['/usr/bin/python3', str(app / 'run.py'), '--daemon']}))
                    self.assertIn('"', unit.read_text(), 'paths with spaces are quoted')
                    with patch.object(installer.subprocess, 'run', return_value=Mock(returncode=0)) as run:
                        with self.assertRaisesRegex(RuntimeError, 'systemctl --user disable --now io.github.claude-pool.service'): installer.uninstall(app)
                        self.assertEqual(run.call_args_list[0].args[0][:3], ['systemctl', '--user', 'is-active'])
                    # Stopped but still enabled (the state after claude-pool --stop) must block too.
                    def enabled_only(argv, **kwargs): return Mock(returncode=0 if argv[2] == 'is-enabled' else 1)
                    with patch.object(installer.subprocess, 'run', side_effect=enabled_only):
                        with self.assertRaisesRegex(RuntimeError, 'service is loaded'): installer.uninstall(app)
                    self.assertTrue(app.exists())
                    with patch.object(installer.subprocess, 'run', return_value=Mock(returncode=1)) as run: installer.uninstall(app)
                    self.assertIn(['systemctl', '--user', 'daemon-reload'], [c.args[0] for c in run.call_args_list])
            self.assertFalse(unit.exists()); self.assertFalse(app.exists()); self.assertTrue(config.exists())

    def test_unsupported_platform_is_refused_before_any_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); native = root / 'native'; native.write_text('#!/bin/sh\necho "2.1.300 (Claude Code)"\n'); native.chmod(0o755)
            with patch.object(installer.sys, 'platform', 'win32'):
                with self.assertRaisesRegex(RuntimeError, 'macOS and Linux'):
                    installer.install(root / 'app', root / 'bin', root / 'config.json', native, skip_backend=True)
            self.assertFalse((root / 'app').exists()); self.assertFalse((root / 'config.json').exists())

    def test_source_version_ignores_the_platform_built_backend_and_files_the_source_archive_skips(self):
        # Final review probe: identical sources with a darwin and a linux backend gave two versions, so device add never matched.
        infos = []
        for binary, checkout in ((b'MACHO-darwin-arm64', True), (b'ELF-linux-x86_64', False)):
            release = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, release)
            for name, text in [('gateway/x.py', 'same source\n'), ('gateway/preview', '#!/usr/bin/env python3\n'), ('docs/a.md', 'doc\n'),
                               ('backend/upstream.json', '{"pin": 1}'), ('run.py', 'run\n'), ('README.md', 'readme\n')]:
                (release / name).parent.mkdir(parents=True, exist_ok=True); (release / name).write_text(text)
            (release / 'gateway/bin').mkdir(); (release / 'gateway/bin/shunt').write_bytes(binary)
            if checkout:  # a copy_release from a checkout carries files the source archive never sends
                (release / 'gateway/notes.txt').write_text('scratch'); (release / 'gateway/__pycache__').mkdir()
                (release / 'gateway/__pycache__/x.cpython-314.pyc').write_bytes(b'pyc')
            infos.append(installer.build_info(release))
        self.assertNotEqual(infos[0]['version'], infos[1]['version'])
        self.assertEqual(infos[0]['sourceVersion'], infos[1]['sourceVersion'])
        changed = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, changed)
        (changed / 'gateway').mkdir(); (changed / 'gateway/x.py').write_text('other source\n')
        (changed / 'backend').mkdir(); (changed / 'backend/upstream.json').write_text('{"pin": 1}')
        self.assertNotEqual(installer.build_info(changed)['sourceVersion'], infos[0]['sourceVersion'])

    def test_no_machine_home_paths_or_private_keys_in_source(self):
        forbidden = ['/Us' + 'ers/', '/ho' + 'me/', '-----BEGIN ' + 'PRIVATE KEY-----']
        for path in ROOT.rglob('*'):
            if not path.is_file() or path.suffix not in {'.py', '.ts', '.tsx', '.json', '.md', '.patch'}: continue
            if '__pycache__' in path.parts or '.scratch' in path.parts or '.claude-plugin/types' in str(path): continue
            for text in forbidden: self.assertNotIn(text, path.read_text(), str(path.relative_to(ROOT)))

    def test_no_personal_identifiers_in_source(self):
        """Tests and docs use neutral fixtures (100.64.0.x, example-vps, alex@vps), never a real machine or person.
        The repository URL is the one allowed use of the owner's account name."""
        forbidden = ['100.116.' + '84.79', '100.108.' + '161.69', 'personal' + '-vps', 'tail69' + 'b084', 'jus' + 'tus', 'aug' + 'ust']
        allowed = ['github.com/' + 'jus' + 'tusau' + 'gust/claude-pool', 'github.com/' + 'jus' + 'tusau' + 'gust)',
                   'raw.githubusercontent.com/' + 'jus' + 'tusau' + 'gust/claude-pool']
        skip = {'.git', '.superpowers', '__pycache__', '.scratch', 'node_modules'}
        hits = []
        for path in ROOT.rglob('*'):
            if not path.is_file() or skip & set(path.relative_to(ROOT).parts): continue
            try: text = path.read_text()
            except (UnicodeDecodeError, OSError): continue
            for line_number, line in enumerate(text.splitlines(), 1):
                low = line.lower()
                for keep in allowed: low = low.replace(keep, '')
                hits += [f'{path.relative_to(ROOT)}:{line_number}: {word}' for word in forbidden if word in low]
        self.assertEqual(hits, [])


if __name__ == '__main__': unittest.main()
