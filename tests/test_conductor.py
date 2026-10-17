"""Conductor Claude: one reboot-proof Native Remote Control conversation per pool. A fake child stands in for the pty,
fake launchctl/systemctl/ps on PATH stand in for the service manager; nothing touches the real Conductor, Native login,
launchd, systemd or herdr."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import accounts_cli
import conductor
import device_sync
import registry
import service
import settings
from fakes import FakeRemote
from remote import Completed

SCRUBBED = ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK',
            'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', 'DISABLE_GROWTHBOOK']
ENV_WITH_ALL = {**{key: 'x' for key in SCRUBBED}, 'HOME': '/srv/justus', 'LANG': 'C.UTF-8', 'PATH': '/usr/bin:/bin'}
MAC = {'name': 'mac', 'ssh': 'justus@mac', 'platform': 'darwin'}
VPS = {'name': 'personal-vps', 'ssh': 'personal-vps', 'platform': 'linux', 'herdrSession': 'conductor'}
IDENTITY = {'email': 'justus@ai-vaerk.de', 'organization': 'AI-Vaerk Team'}
SESSION = '01AbCdEf23'
URL_LINE = b'View it at https://claude.ai/code/session_' + SESSION.encode() + b'\r\n'
SERVER_ARGV = ['remote-control', '--name', 'Conductor Claude', '--spawn', 'same-dir', '--permission-mode', 'auto']


def value_with(device='personal-vps', cwd='/srv/justus/projects/conductor', devices=(MAC, VPS), accounts=()):
    return {**registry.EMPTY(), 'devices': [dict(d) for d in devices], 'accounts': list(accounts),
            'conductor': {'device': device, 'cwd': cwd, **IDENTITY}}


class Clock:
    def __init__(self): self.now = 1000.0
    def __call__(self): return self.now


class FakeChild:
    """Plays a script of output chunks. A callable item runs (e.g. to stop the supervisor) and reads as a timeout;
    an int item advances the fake clock. The script ends in EOF and the exit code."""
    def __init__(self, argv, env, cwd, script, code, clock, pid):
        self.argv, self.env, self.cwd, self.script, self.code, self.clock, self.pid = argv, env, cwd, list(script), code, clock, pid
        self.written, self.terminated = [], False

    def read(self, timeout):
        if self.terminated or not self.script: return b''
        item = self.script.pop(0)
        if callable(item): item(); return None
        if isinstance(item, (int, float)): self.clock.now += item; return None
        return item

    def write(self, data): self.written.append(data)
    def terminate(self): self.terminated = True
    def wait(self): return self.code


class Spawner:
    """Hands out one scripted child per start; after the last one it stops the supervisor."""
    def __init__(self, runs, stop, clock):
        self.runs, self.stop, self.clock, self.children = list(runs), stop, clock, []

    def __call__(self, argv, env, cwd):
        script, code = self.runs.pop(0) if self.runs else ([self.stop.set], 0)
        child = FakeChild(argv, env, cwd, script, code, self.clock, 4000 + len(self.children))
        self.children.append(child)
        if not self.runs: child.script.append(self.stop.set)
        return child


class ServeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state' / 'conductor.json'
        self.cwd = self.root / 'conductor'; self.cwd.mkdir()
        self.stop, self.clock, self.sleeps, self.states = threading.Event(), Clock(), [], []
        real = conductor.write_private
        def record(path, data):
            if path == self.state: self.states.append(json.loads(data))
            return real(path, data)
        patcher = mock.patch.object(conductor, 'write_private', side_effect=record); patcher.start(); self.addCleanup(patcher.stop)

    def serve(self, *runs, env=None):
        self.stop.clear()
        spawner = Spawner(runs, self.stop, self.clock)
        with mock.patch.dict(os.environ, env or {}, clear=False):
            conductor.serve('/opt/claude', self.cwd, state=self.state, spawn=spawner, sleep=self.sleeps.append, stop=self.stop,
                            clock=self.clock)
        return spawner.children


class ServeTests(ServeBase):
    def test_serve_runs_native_remote_control_without_pool_variables(self):
        children = self.serve(([b'Connected \xc2\xb7 Capacity: 0/32\r\n'], 0), env=ENV_WITH_ALL)
        spawned = children[0]
        self.assertEqual(conductor.ARGV, SERVER_ARGV)
        self.assertEqual(spawned.argv, ['/opt/claude', *conductor.ARGV])
        self.assertEqual(spawned.cwd, self.cwd)
        for key in SCRUBBED: self.assertNotIn(key, spawned.env)
        self.assertEqual(spawned.env['LANG'], 'C.UTF-8')

    def test_serve_restarts_with_backoff_and_reports_ready_only_after_the_ready_line(self):
        children = self.serve(([b'starting\r\n'], 1), ([b'starting\r\n'], 1), ([b'starting\r\n'], 1),
                              ([b'Conn', b'ected \xc2\xb7 Capacity: 0/32\r\n', self.stop.set], 0))
        self.assertEqual(self.sleeps[:3], [10, 20, 40])
        self.assertEqual(len(children), 4)
        self.assertFalse(self.states[0]['ready'])
        ready = [s for s in self.states if s['ready']]
        self.assertTrue(ready)
        self.assertEqual({s['pid'] for s in ready}, {children[3].pid})  # the ready line split over two reads still counts
        self.assertEqual(self.states[-1]['restarts'], 3)
        self.assertEqual(self.states[-1]['lastExit'], 1)
        self.assertEqual(set(self.states[0]) >= {'pid', 'startedAt', 'ready', 'restarts', 'lastExit'}, True)

    def test_backoff_caps_at_300_and_resets_after_ten_minutes_of_uptime(self):
        quick = [([], 1)] * 8
        self.serve(*quick)
        self.assertEqual(self.sleeps, [10, 20, 40, 80, 160, 300, 300])
        self.sleeps.clear(); self.stop.clear()
        self.serve(([], 1), ([], 1), ([601], 1), ([], 1))
        self.assertEqual(self.sleeps, [10, 20, 10])

    def test_state_file_is_private_and_keeps_the_session_id_but_never_the_url_or_output(self):
        self.serve(([b'Connected \xc2\xb7 Capacity: 0/32\r\n', URL_LINE, b'SECRET-OUTPUT\r\n'], 0))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        text = self.state.read_text()
        self.assertEqual(json.loads(text)['sessionId'], 'cse_' + SESSION)
        for word in ['claude.ai', 'https', 'SECRET-OUTPUT', 'View it']: self.assertNotIn(word, text)
        self.assertEqual(sorted(p.name for p in self.state.parent.iterdir()), ['conductor.json'])

    def test_recorded_session_is_reattached_and_an_exit_when_complete_restarts_it(self):
        self.serve(([URL_LINE], 1))
        self.stop.clear()
        children = self.serve(([b'Conductor Claude \xc2\xb7 /x \xc2\xb7 Single session \xc2\xb7 exits when complete\r\n', 31], 0),
                              ([b'Single session\r\n'], 0))
        resume = ['/opt/claude', 'remote-control', '--session-id', 'cse_' + SESSION, '--permission-mode', 'auto']
        self.assertEqual([c.argv for c in children], [resume, resume])
        self.assertEqual(self.sleeps[-1], 10)

    def test_a_session_that_cannot_be_reattached_falls_back_to_server_mode_once_and_is_marked_fresh(self):
        self.serve(([URL_LINE], 1))
        self.stop.clear()
        children = self.serve(([b'Session not found\r\n', 5], 1), ([b'Connected\r\n'], 0))
        self.assertEqual(children[0].argv[2:4], ['--session-id', 'cse_' + SESSION])
        self.assertEqual(children[1].argv, ['/opt/claude', *SERVER_ARGV])
        self.assertTrue(self.states[-1]['fresh'])
        self.assertNotIn('sessionId', self.states[-1])

    def test_only_the_configured_directory_trust_prompt_is_answered(self):
        prompt = ('\x1b[1mTrust ' + str(self.cwd) + '? [y/N]\x1b[0m ').encode()
        children = self.serve(([b'Trust /etc? [y/N] ', b'Allow network access? [y/N] ', prompt, b'Connected\r\n'], 0))
        self.assertEqual(children[0].written, [b'y\n'])
        children = self.serve(([('Trust ' + str(self.cwd) + '/sub? [y/N] ').encode()], 0))
        self.assertEqual(children[0].written, [])

    def test_stop_terminates_the_child_and_records_it_is_not_ready(self):
        children = self.serve(([b'Connected\r\n', self.stop.set, b'more'], 0))
        self.assertTrue(children[0].terminated)
        self.assertFalse(self.states[-1]['ready'])
        self.assertEqual(self.sleeps, [])

    def test_real_pty_child_gets_the_trust_answer_and_a_scrubbed_environment(self):
        native = self.root / 'native'
        native.write_text('#!/bin/sh\nprintf "%s" "${ANTHROPIC_BASE_URL-unset} $*" > seen.txt\n'
                          'printf "Trust %s? [y/N] " "$(pwd -P)"\nread answer\nprintf "%s" "$answer" > answer.txt\n'
                          'printf "Connected\\r\\n"\nexit 3\n')
        native.chmod(0o755)
        cwd = self.cwd.resolve()
        with mock.patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:1'}):
            conductor.serve(str(native), cwd, state=self.state, sleep=lambda seconds: self.stop.set(), stop=self.stop)
        self.assertEqual((cwd / 'answer.txt').read_text(), 'y')
        self.assertEqual((cwd / 'seen.txt').read_text(), 'unset ' + ' '.join(SERVER_ARGV))
        self.assertTrue(any(s['ready'] for s in self.states))
        self.assertEqual((self.states[-1]['lastExit'], self.states[-1]['ready']), (3, False))

    def test_output_ring_is_bounded_to_64_kib(self):
        ring = conductor.Ring()
        for _ in range(300): ring.add(b'x' * 1000)
        ring.add(b'tail')
        self.assertEqual(len(ring.data), 64 * 1024)
        self.assertTrue(bytes(ring.data).endswith(b'xtail'))


class DefinitionTests(unittest.TestCase):
    def test_unit_definition_restarts_on_failure_and_starts_at_login(self):
        cwd = Path('/srv/justus/projects/conductor')
        d = conductor.definition(ROOT, cwd)
        self.assertTrue(d['RunAtLoad']); self.assertEqual(d['KeepAlive'], {'SuccessfulExit': False})
        self.assertEqual(d['ProgramArguments'][1:], [str(ROOT / 'run.py'), 'device', 'conductor', 'serve'])
        self.assertEqual(d['WorkingDirectory'], str(cwd))
        self.assertEqual(d['EnvironmentVariables'], {'CLAUDE_POOL_CONFIG': str(settings.CONFIG)})
        self.assertTrue(conductor.LABEL.endswith('.conductor'))
        self.assertEqual(d['Label'], conductor.LABEL)
        with mock.patch.object(service, 'platform', return_value='linux'):
            unit = service.render(d).decode()
        self.assertIn('WantedBy=default.target', unit); self.assertIn('Restart=on-failure', unit)

    def test_phone_chat_environment_is_unchanged(self):
        import control_main
        seen = {}
        def call(argv, env): seen.update(env); return 0
        with mock.patch.dict(os.environ, ENV_WITH_ALL, clear=True), \
                mock.patch.object(control_main, 'prepare', return_value={'host': 'h', 'login': 'l'}), \
                mock.patch.object(control_main, 'start'), mock.patch('launch.configured_plugin', return_value=Path('/plugin')), \
                mock.patch.object(control_main.subprocess, 'call', side_effect=call), self.assertRaises(SystemExit):
            control_main.cli(['--chat'])
        self.assertEqual(seen, conductor.native_env(ENV_WITH_ALL))
        self.assertEqual(seen, {'HOME': '/srv/justus', 'LANG': 'C.UTF-8', 'PATH': '/usr/bin:/bin'})


PS = '''  PID ARGS
    1 /sbin/init
 {pid} /opt/native/claude {args}
'''
FAKE_PS = '#!/bin/sh\ncat "$FAKE_PS_OUT"\n'
FAKE_MANAGER = '''#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$FAKE_LOG"
case "$1 $2" in print*|"--user is-active"|"--user is-enabled") exit 1 ;; esac
exit 0
'''
SHUNT = '#!/bin/sh\n[ "$3" = check ] && exit 0\nexit 3\n'
TOML = ('[providers.anthropic]\nkind = "anthropic"\nauth = "claude_oauth"\n\n'
        '[[providers.anthropic.accounts]]\nname = "ops"\n\n'
        '[[providers.anthropic.accounts]]\nname = "team"\n\n[server]\nbind = "x"\n')


class ServiceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        for name, text in [('ps', FAKE_PS), ('launchctl', FAKE_MANAGER), ('systemctl', FAKE_MANAGER), ('shunt', SHUNT)]:
            (self.bin / name).write_text(text); (self.bin / name).chmod(0o755)
        self.log = self.root / 'manager.log'; self.log.write_text('')
        self.ps = self.root / 'ps.txt'; self.ps.write_text('  PID ARGS\n    1 /sbin/init\n')
        self.home = self.root / 'home'; self.home.mkdir()
        self.cfg = self.root / 'cfg'; self.cfg.mkdir(mode=0o700)
        self.config = self.cfg / 'config.json'
        self.config.write_text(json.dumps({'accounts': [{'name': 'ops'}, {'name': 'team'}]}))
        self.runtime = self.root / 'runtime'; self.runtime.mkdir(mode=0o700)
        self.toml = self.runtime / 'shunt.toml'; self.toml.write_text(TOML)
        self.cwd = self.root / 'projects' / 'conductor'
        env = {'PATH': str(self.bin) + ':/usr/bin:/bin', 'HOME': str(self.home), 'FAKE_LOG': str(self.log), 'FAKE_PS_OUT': str(self.ps)}
        patches = [mock.patch.dict(os.environ, env),
                   mock.patch.object(settings, 'REGISTRY', self.cfg / 'devices.json'),
                   mock.patch.object(settings, 'CONFIG', self.config),
                   mock.patch.object(settings, 'CONDUCTOR_STATE', self.runtime / 'conductor.json'),
                   mock.patch.object(accounts_cli, 'local_context',
                                     return_value=(self.config, self.bin / 'shunt', [self.toml], dict(os.environ))),
                   mock.patch.object(registry, 'self_name', return_value='personal-vps')]
        for item in patches: item.start(); self.addCleanup(item.stop)
        accounts = [{'name': 'team', 'email': 'Justus@AI-Vaerk.de', 'organization': 'AI-Vaerk Team'},
                    {'name': 'ops', 'email': 'ops@ai-vaerk.de', 'organization': 'AI-Vaerk Team'}]
        registry.update(lambda v: v.update(value_with(cwd=str(self.cwd), accounts=accounts), generation=v['generation']))

    def calls(self): return [line.split() for line in self.log.read_text().splitlines()]

    def assert_own_unit_only(self):
        self.assertTrue(self.calls())
        for argv in self.calls():
            if argv != ['systemctl', '--user', 'daemon-reload']: self.assertTrue(any(conductor.LABEL in word for word in argv), argv)
    def rows(self): return {row['name']: row for row in json.loads(self.config.read_text())['accounts']}
    def unit(self): return service.service_file(conductor.LABEL)

    def foreign(self, args, pid=4242): self.ps.write_text(PS.format(pid=pid, args=args))


class InstallTests(ServiceBase):
    def test_second_conductor_process_blocks_install(self):
        self.foreign('remote-control --name Conductor Claude --spawn same-dir --permission-mode auto')
        step = conductor.install()
        self.assertEqual((step.name, step.status), ('conductor', 'failed'))
        self.assertIn('herdr session', step.fix)
        self.assertEqual(step.fix, 'stop the Conductor in herdr session conductor first')
        self.assertEqual(self.calls(), []); self.assertFalse(self.unit().exists())
        self.assertNotIn('reserveAt', self.rows()['team'])

    def test_hand_started_conductor_with_the_adopted_session_blocks_install(self):
        self.foreign('remote-control --session-id cse_' + SESSION + ' --permission-mode auto')
        self.assertEqual(conductor.install(session_id='cse_' + SESSION).status, 'failed')
        self.assertEqual(json.loads(settings.CONDUCTOR_STATE.read_text())['sessionId'], 'cse_' + SESSION)
        self.assertEqual(conductor.install().status, 'failed')  # the recorded session still marks the hand-started one
        self.foreign('remote-control --session-id cse_other --permission-mode auto')
        self.assertEqual(conductor.install().status, 'ok')
        with self.assertRaises(ValueError): conductor.install(session_id='cse_x; rm -rf ~')

    def test_duplicates_exclude_our_own_child_and_other_remote_controls(self):
        ps = ('  PID ARGS\n 10 claude remote-control --name Conductor Claude --spawn same-dir\n'
              ' 11 claude remote-control --session-id cse_' + SESSION + ' --permission-mode auto\n'
              ' 12 claude remote-control --name Other --spawn same-dir\n 13 vim notes-about-remote-control --name Conductor Claude\n'
              ' 14 python3 run.py device conductor serve\n')
        self.assertEqual(conductor.duplicates(ps, None), [10])
        self.assertEqual(conductor.duplicates(ps, 10), [])
        self.assertEqual(conductor.duplicates(ps, None, session_id='cse_' + SESSION), [10, 11])

    def test_install_writes_only_its_own_unit_and_reserves_the_conductor_account_at_85_unless_set(self):
        pool_unit = service.service_file('io.github.claude-pool'); pool_unit.parent.mkdir(parents=True)
        pool_unit.write_bytes(b'pool unit'); before = pool_unit.stat().st_mtime_ns
        step = conductor.install()
        self.assertEqual(step.status, 'ok', step)
        self.assertTrue(self.cwd.is_dir())
        self.assertEqual(service.load(self.unit().read_bytes())['ProgramArguments'][-3:], ['device', 'conductor', 'serve'])
        self.assertEqual(pool_unit.read_bytes(), b'pool unit'); self.assertEqual(pool_unit.stat().st_mtime_ns, before)
        self.assert_own_unit_only()
        self.assertEqual(self.rows()['team']['reserveAt'], 85)
        self.assertNotIn('reserveAt', self.rows()['ops'])
        self.assertEqual(registry.account(registry.load(), 'team')['reserveAt'], 85)
        self.assertIn('max_utilization = 0.85', self.toml.read_text())

    def test_an_explicit_reserve_is_never_overridden(self):
        rows = json.loads(self.config.read_text()); rows['accounts'][1]['reserveAt'] = 0; self.config.write_text(json.dumps(rows))
        self.assertEqual(conductor.install().status, 'ok')
        self.assertEqual(self.rows()['team']['reserveAt'], 0)
        self.assertNotIn('max_utilization', self.toml.read_text())

    def test_uninstall_removes_only_its_own_unit(self):
        conductor.install(); self.log.write_text('')
        step = conductor.uninstall()
        self.assertEqual((step.name, step.status), ('conductor', 'ok'))
        self.assertFalse(self.unit().exists())
        self.assert_own_unit_only()

    def test_status_part_is_registered(self):
        self.assertIn(conductor.status_part, device_sync.STATUS_PARTS)
        with mock.patch.object(device_sync, 'STATUS_PARTS', [conductor.status_part]), \
                mock.patch.object(device_sync, 'account_rows', return_value=[]), \
                mock.patch.object(device_sync, 'switch_pending', return_value=[]):
            value = device_sync.status()
        self.assertIn('conductor', value)
        self.assertEqual(set(value['conductor']), {'installed', 'running', 'ready', 'fresh', 'duplicates'})
        self.assertFalse(value['conductor']['running'])


RUNNING = {'platform': 'linux', 'linger': True, 'conductor': {'installed': True, 'running': True, 'ready': True, 'fresh': False, 'duplicates': []}}
NO_LINGER = {**RUNNING, 'linger': False}
IDLE = {'platform': 'darwin', 'linger': None, 'conductor': {'installed': False, 'running': False, 'ready': False, 'fresh': False, 'duplicates': []}}


class CheckTests(unittest.TestCase):
    def test_check_is_not_ready_without_linger_or_with_two_conductors(self):
        value = value_with()
        self.assertEqual(conductor.check({'personal-vps': RUNNING, 'mac': IDLE}, value).status, 'ok')
        self.assertEqual(conductor.check({'personal-vps': NO_LINGER}, value).status, 'failed')
        self.assertEqual(conductor.check({'personal-vps': RUNNING, 'mac': {**RUNNING, 'platform': 'darwin'}}, value).status, 'failed')

    def test_check_names_wrong_device_not_ready_duplicates_and_fresh_conversation(self):
        value = value_with()
        wrong = conductor.check({'personal-vps': IDLE, 'mac': {**RUNNING, 'platform': 'darwin'}}, value)
        self.assertEqual(wrong.status, 'failed'); self.assertIn('move personal-vps', wrong.fix)
        busy = {**RUNNING, 'conductor': {**RUNNING['conductor'], 'ready': False}}
        self.assertEqual(conductor.check({'personal-vps': busy}, value).status, 'failed')
        dup = {**RUNNING, 'conductor': {**RUNNING['conductor'], 'duplicates': [77]}}
        self.assertIn('herdr session', conductor.check({'personal-vps': dup}, value).fix)
        fresh = {**RUNNING, 'conductor': {**RUNNING['conductor'], 'fresh': True}}
        self.assertEqual(conductor.check({'personal-vps': fresh}, value).status, 'manual')
        self.assertEqual(conductor.check({'mac': IDLE}, value).status, 'unreachable')
        self.assertEqual(conductor.check({}, {**value, 'conductor': None}).status, 'skipped')


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)

    def test_context_block_is_idempotent_and_keeps_hand_written_text(self):
        (self.cwd / 'CLAUDE.md').write_text('# Conductor\n\nhand-written role\n')
        self.assertTrue(conductor.write_context(self.cwd, value_with()))
        self.assertFalse(conductor.write_context(self.cwd, value_with()))
        text = (self.cwd / 'CLAUDE.md').read_text()
        self.assertTrue(text.startswith('# Conductor\n\nhand-written role\n'))
        self.assertIn('hand-written', text)
        block = text.split(conductor.START)[1].split(conductor.END)[0]
        for word in ['mac', 'justus@mac', 'personal-vps', 'herdr machine list', 'herdr session', 'through the pool']:
            self.assertIn(word, block)

    def test_malformed_markers_leave_the_file_alone(self):
        bad = conductor.START + '\nno end\n'
        (self.cwd / 'CLAUDE.md').write_text(bad)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(conductor.write_context(self.cwd, value_with()))
        self.assertEqual((self.cwd / 'CLAUDE.md').read_text(), bad)
        self.assertIn('CLAUDE.md', out.getvalue())

    def test_hook_refreshes_the_context_only_on_the_conductor_device(self):
        value = value_with(cwd=str(self.cwd))
        with mock.patch.object(registry, 'self_name', return_value='mac'): conductor.apply_hook(value)
        self.assertFalse((self.cwd / 'CLAUDE.md').exists())
        with mock.patch.object(registry, 'self_name', return_value='personal-vps'): conductor.apply_hook(value)
        self.assertIn(conductor.START, (self.cwd / 'CLAUDE.md').read_text())
        self.assertIn(conductor.apply_hook, device_sync.INTEGRATION_HOOKS)


def done(out=b'', code=0): return Completed(code, out if isinstance(out, bytes) else json.dumps(out).encode(), b'')


class MoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / 'cfg'; self.cfg.mkdir(mode=0o700)
        self.order, self.requested, self.ready_after = [], [], 2
        self.native = {'personal-vps': {'email': ' justus@AI-Vaerk.de', 'organization': 'AI Vaerk Team'}, 'mac': dict(IDENTITY)}
        self.linger = {'personal-vps': 'yes'}
        self.service = {'personal-vps': 'running', 'mac': 'running'}
        patches = [mock.patch.object(settings, 'REGISTRY', self.cfg / 'devices.json'),
                   mock.patch.object(registry, 'self_name', return_value='mac'),
                   mock.patch.object(conductor.device_sync, 'sync_all', side_effect=lambda remotes: self.order.append(('sync',)))]
        for item in patches: item.start(); self.addCleanup(item.stop)
        self.value = registry.update(lambda v: v.update(value_with(device='mac', cwd='~/projects/conductor'), generation=v['generation']))

    def fake(self, name):
        state = {'installed': False}
        def handler(argv, stdin):
            if argv[-3:] == ['device', 'status', '--json']:
                polls = self.order.count((name, 'status'))
                self.order.append((name, 'status'))
                ready = state['installed'] and polls >= self.ready_after
                return done({'platform': 'linux' if name == 'personal-vps' else 'darwin', 'service': self.service[name],
                             'nativeLogin': self.native[name], 'linger': self.linger.get(name) == 'yes',
                             'conductor': {'installed': state['installed'], 'running': state['installed'], 'ready': ready,
                                           'fresh': False, 'duplicates': []}})
            if argv == ['uname', '-s']: return done(b'Linux\n' if name == 'personal-vps' else b'Darwin\n')
            if argv[:2] == ['sh', '-c'] and 'loginctl' in argv[2]: return done((self.linger.get(name, 'no') + '\n').encode())
            if argv[-3:] == ['device', 'conductor', 'install']:
                state['installed'] = True; self.order.append((name, 'install')); return done()
            if argv[-3:] == ['device', 'conductor', 'uninstall']:
                self.order.append((name, 'uninstall')); return done()
            return done(code=1)
        return FakeRemote(name, handler)

    def remotes(self, ssh):
        self.requested.append(ssh)
        return self.fake({'personal-vps': 'personal-vps', 'justus@mac': 'mac'}[ssh])

    def move(self, target):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = conductor.move(target, self.value, remotes=self.remotes, local=self.fake('mac'), sleep=lambda s: None)
        return code, out.getvalue()

    def installs(self): return [item for item in self.order if item[1:] == ('install',)]

    def test_move_resolves_ssh_targets_and_installs_on_target_before_removing_from_source(self):
        code, out = self.move('personal-vps')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.requested, ['personal-vps'])
        self.assertLess(self.order.index(('personal-vps', 'install')), self.order.index(('mac', 'uninstall')))
        self.assertLess(self.order.index(('mac', 'uninstall')), self.order.index(('sync',)))
        self.assertEqual(registry.load()['conductor']['device'], 'personal-vps')

    def test_move_refuses_a_target_with_another_native_account(self):
        self.native['personal-vps'] = {'email': 'other@ai-vaerk.de', 'organization': 'AI-Vaerk Team'}
        code, out = self.move('personal-vps')
        self.assertNotEqual(code, 0); self.assertEqual(self.installs(), []); self.assertIn('Native login', out)
        self.native['personal-vps'] = {'email': 'justus@ai-vaerk.de', 'organization': 'Another Org'}
        self.assertNotEqual(self.move('personal-vps')[0], 0); self.assertEqual(self.installs(), [])
        self.native['personal-vps'] = None
        self.assertNotEqual(self.move('personal-vps')[0], 0); self.assertEqual(self.installs(), [])
        self.assertEqual(registry.load()['conductor']['device'], 'mac')

    def test_move_refuses_without_linger_or_a_running_pool(self):
        self.linger['personal-vps'] = 'no'
        self.assertNotEqual(self.move('personal-vps')[0], 0)
        self.linger['personal-vps'] = 'yes'; self.service['personal-vps'] = 'stopped'
        self.assertNotEqual(self.move('personal-vps')[0], 0)
        self.assertEqual(self.installs(), [])

    def test_move_that_never_gets_ready_leaves_the_old_conductor_running(self):
        self.ready_after = 10 ** 6
        code, _ = self.move('personal-vps')
        self.assertNotEqual(code, 0)
        self.assertIn(('personal-vps', 'uninstall'), self.order); self.assertNotIn(('mac', 'uninstall'), self.order)
        self.assertEqual(registry.load()['conductor']['device'], 'mac')


if __name__ == '__main__': unittest.main()
