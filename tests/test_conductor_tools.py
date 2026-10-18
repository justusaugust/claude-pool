"""Conductor tools: snapshot, events, tell and journal. Fake herdr, preview, gh and git scripts on a temp PATH
(`<fake>:/usr/bin:/bin`) stand in for the real tools; nothing reaches the network, a real herdr session, gh or a real repo."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import conductor_tools as ct
from fakes import fixture_token

FAKE_HERDR = r'''
import json, os, sys
state = json.load(open(os.environ['FAKE_STATE']))
with open(os.environ['FAKE_LOG'], 'a') as log: log.write(json.dumps(['herdr'] + sys.argv[1:]) + '\n')
args = sys.argv[1:]
session = None
if args[:1] == ['--session']: session, args = args[1], args[2:]
def out(value): print(json.dumps(value)); sys.exit(0)
if args == ['session', 'list', '--json']:
    if state.get('herdr_down'): sys.exit(1)
    out({'sessions': state['sessions']})
if session is None or session not in state.get('agents', {}): sys.exit(3)
if args == ['agent', 'list']:
    if session in state.get('agent_list_fails', []): sys.exit(1)
    out({'id': 'cli:agent:list', 'result': {'agents': state['agents'][session]}})
if args == ['workspace', 'list']:
    out({'result': {'type': 'workspace_list', 'workspaces': state.get('workspaces', {}).get(session, [])}})
if args[:2] == ['agent', 'read']:
    text = state.get('reads', {}).get(args[2], '')
    if state.get('raw_reads'): print(text); sys.exit(0)
    out({'result': {'type': 'pane_read', 'read': {'text': text}}})
if args[:2] == ['agent', 'prompt']:
    if state.get('prompt_fails'): sys.exit(1)
    if state.get('prompt_error'): out({'id': 'cli:agent:prompt', 'error': {'code': 'agent_blocked', 'message': 'blocked'}})
    out({'result': {'type': 'agent_prompted', 'agent': {}}})
if args[:2] == ['workspace', 'create']:
    out({'result': {'type': 'workspace_created', 'workspace': {'workspace_id': 'w9'}, 'tab': {'tab_id': 'w9:t1'},
                    'root_pane': {'pane_id': 'w9:p1'}}})
if args[:2] == ['agent', 'start']:
    out({'result': {'type': 'agent_started', 'agent': {'name': args[2], 'pane_id': args[6]}, 'argv': ['claude']}})
if args[:2] == ['pane', 'process-info']:
    out({'result': {'type': 'pane_process_info', 'process_info': {'pane_id': args[3], 'foreground_processes': state['processes']}}})
if args[:2] == ['workspace', 'close']: out({'result': {'type': 'ok'}})
sys.exit(2)
'''

FAKE_PREVIEW = r'''
import json, os, sys
state = json.load(open(os.environ['FAKE_STATE']))
with open(os.environ['FAKE_LOG'], 'a') as log: log.write(json.dumps(['preview'] + sys.argv[1:]) + '\n')
if sys.argv[1:] == ['list', '--json']: print(json.dumps(state.get('previews', []))); sys.exit(0)
sys.exit(2)
'''

FAKE_GH = r'''
import json, os, sys
state = json.load(open(os.environ['FAKE_STATE']))
with open(os.environ['FAKE_LOG'], 'a') as log: log.write(json.dumps(['gh'] + sys.argv[1:]) + '\n')
args = sys.argv[1:]
if state.get('gh_unauthenticated'): print('To get started with GitHub CLI, please run:  gh auth login', file=sys.stderr); sys.exit(4)
if args[:2] == ['pr', 'list']:
    repo = args[args.index('--repo') + 1]
    rows = state.get('prs', {}).get(repo, [])
    wanted = args[args.index('--state') + 1].upper() if '--state' in args else 'OPEN'
    rows = [row for row in rows if row['state'] == wanted]
    print(json.dumps(rows)); sys.exit(0)
sys.exit(2)
'''

FAKE_GIT = r'''
import json, os, sys
state = json.load(open(os.environ['FAKE_STATE']))
with open(os.environ['FAKE_LOG'], 'a') as log: log.write(json.dumps(['git'] + sys.argv[1:]) + '\n')
args = sys.argv[1:]
repo = state.get('git', {}).get(args[1]) if args[:1] == ['-C'] else None
if repo is None: sys.exit(128)
if args[2:] == ['rev-parse', '--show-toplevel']: print(repo['top']); sys.exit(0)
if args[2:] == ['remote', 'get-url', 'origin'] and repo.get('origin'): print(repo['origin']); sys.exit(0)
sys.exit(2)
'''

ESC = '\x1b'


def agent(pane, status, cwd='/srv/justus/dev/website', name=None):
    row = {'agent': 'claude', 'agent_status': status, 'cwd': cwd, 'pane_id': pane, 'workspace_id': pane.split(':')[0],
           'tab_id': pane.split(':')[0] + ':t1', 'state_change_seq': 1, 'terminal_title_stripped': 'claude'}
    if name: row['name'] = name
    return row


def base_state():
    return {
        'sessions': [{'name': 'dashboard', 'running': True, 'default': True, 'session_dir': '/x', 'socket_path': '/x/s'},
                     {'name': 'old', 'running': False, 'default': False, 'session_dir': '/y', 'socket_path': '/y/s'}],
        'agents': {'dashboard': [agent('w1:p1', 'working', name='reviewer'),
                                 agent('w2:p1', 'idle', cwd='/srv/justus/dev/claude-pool', name='pooler')]},
        'workspaces': {'dashboard': [{'workspace_id': 'w1', 'label': 'api'}, {'workspace_id': 'w2', 'label': 'pool'}]},
        'reads': {'w1:p1': ESC + '[32mRunning tests 3/9' + ESC + '[0m\n\n? for shortcuts\n',
                  'w2:p1': 'Committed. token ' + fixture_token() + '\n> \n'},
        'previews': [{'port': 5173, 'label': 'web', 'link': 'https://personal-vps.tail1.ts.net:8443/'}],
        'prs': {'ai-vaerk/website': [{'number': 12, 'title': 'Über uns: neu', 'state': 'OPEN', 'url': 'https://github.com/ai-vaerk/website/pull/12',
                                      'headRefName': 'about'}]},
        'git': {'/srv/justus/dev/website': {'top': '/srv/justus/dev/website', 'origin': 'git@github.com:ai-vaerk/website.git'},
                '/srv/justus/dev/claude-pool': {'top': '/srv/justus/dev/claude-pool',
                                                'origin': 'https://user:' + fixture_token('g') + '@example.com/pool.git'}},
        'processes': [{'pid': 10, 'name': 'python3', 'argv': ['/usr/bin/python3', '/srv/justus/.local/share/claude-pool/current/gateway/launch.py', 'exec'],
                       'cmdline': 'python3 /srv/justus/.local/share/claude-pool/current/gateway/launch.py exec'},
                      {'pid': 11, 'name': 'claude', 'argv': ['/srv/justus/.local/bin/claude']}],
    }


class ToolsBase(unittest.TestCase):
    """A temp dir with fake tools on PATH, a state file the fakes read and a log of every call they get."""
    tools = ('herdr', 'preview', 'gh', 'git')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / 'bin'; self.bin.mkdir()
        sources = {'herdr': FAKE_HERDR, 'preview': FAKE_PREVIEW, 'gh': FAKE_GH, 'git': FAKE_GIT}
        for name in self.tools:
            path = self.bin / name
            path.write_text('#!' + sys.executable + '\n' + sources[name]); path.chmod(0o755)
        self.state_file, self.log = self.dir / 'fake.json', self.dir / 'calls.log'
        self.log.write_text('')
        self.set_state(base_state())
        self.env = {'PATH': str(self.bin) + ':/usr/bin:/bin', 'FAKE_STATE': str(self.state_file), 'FAKE_LOG': str(self.log)}
        # The board facts (machines, accounts) read the registry, the pool and the service manager: point every path at the
        # temp dir and shadow ssh and the service managers, so a snapshot can never reach a real device, pool or service.
        for name in ('ssh', 'launchctl', 'systemctl', 'tailscale', 'cua', 'claude-pool'):
            path = self.bin / name
            path.write_text('#!' + sys.executable + '\nimport json, os, sys\nopen(os.environ["FAKE_LOG"], "a").write(json.dumps(['
                            + repr(name) + '] + sys.argv[1:]) + "\\n")\nsys.exit(97)\n'); path.chmod(0o755)
        patcher = mock.patch.dict(os.environ, self.env); patcher.start(); self.addCleanup(patcher.stop)
        self.runtime = self.dir / 'state'
        self.cfg = self.dir / 'cfg'
        import settings
        for key, value in [('RUNTIME', self.runtime), ('REGISTRY', self.cfg / 'devices.json'), ('SELF', self.cfg / 'device.json'),
                           ('CONFIG', self.cfg / 'config.json'), ('STORES', self.dir / 'credentials'), ('LONG_LIVED', self.dir / 'long-lived'),
                           ('LONG_LIVED_PROBES', self.runtime / 'long-lived-probes.json'),
                           ('SHARED_READINGS', self.runtime / 'shared-readings.json'), ('HERE', self.dir / 'release' / 'gateway')]:
            patcher = mock.patch.object(settings, key, value); patcher.start(); self.addCleanup(patcher.stop)
        self.events_path, self.queue_path = self.runtime / 'conductor-events.json', self.runtime / 'conductor-queue.json'
        for key, value in [('EVENTS', self.events_path), ('QUEUE', self.queue_path)]:  # the real queue is never read or rewritten
            patcher = mock.patch.object(ct, key, value); patcher.start(); self.addCleanup(patcher.stop)

    def set_state(self, value):
        self.value = value
        self.state_file.write_text(json.dumps(value))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def tick(self, now, first=False, with_prs=True):
        return ct.tick(self.events_path, self.queue_path, ct.Tools(), now, first=first, with_prs=with_prs)


class GuardTests(unittest.TestCase):
    def test_fake_tools_never_inherit_the_real_path(self):
        base = ToolsBase('setUp'); base.setUp()
        try:
            parts = os.environ['PATH'].split(':')
            self.assertEqual(parts[1:], ['/usr/bin', '/bin'])
            for name in ('herdr', 'gh', 'preview', 'git', 'ssh', 'launchctl', 'systemctl', 'claude-pool'):
                self.assertTrue(str(ct.shutil.which(name)).startswith(str(base.bin)), name)
        finally: base.doCleanups()


class CleanTests(unittest.TestCase):
    def test_clean_strips_ansi_and_redacts_secrets_but_keeps_paths_and_words(self):
        token = fixture_token()
        text = ESC + '[1mbold' + ESC + '[0m ' + token + ' Authorization: Bearer abc.def ' + 'a1' * 25 + ' ' + 'f' * 40
        cleaned = ct.clean(text)
        self.assertNotIn(ESC, cleaned); self.assertNotIn(token, cleaned); self.assertNotIn('abc.def', cleaned)
        self.assertNotIn('a1' * 25, cleaned); self.assertNotIn('f' * 40, cleaned)
        self.assertIn('bold', cleaned); self.assertIn('<redacted>', cleaned)
        keep = '/srv/justus/dev/claude-pool-devices/gateway/conductor_tools.py test_no_machine_home_paths_or_private_keys_in_source'
        self.assertEqual(ct.clean(keep), keep)
        self.assertEqual(ct.clean('a\tb\x07c\rd'), 'a b c d')
        self.assertEqual(len(ct.clean('x' * 500, limit=160)), 160)
        self.assertEqual(ct.clean('\x1b(Bplain\x1b)0 x'), 'plain x')

    def test_standard_base64_with_slashes_is_redacted_but_plain_paths_survive(self):
        aws = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
        self.assertEqual(len(aws), 40)
        run = 'aB3/' * 15
        self.assertEqual(ct.clean('secret ' + aws), 'secret <redacted>')
        self.assertEqual(ct.clean('key ' + run + '=='), 'key <redacted>')
        path = '/srv/justus/dev/claude-pool-devices/gateway/Conductor2Tools/Module3'
        self.assertEqual(ct.clean('open ' + path), 'open ' + path)

    def test_last_line_skips_chrome_and_blank_lines(self):
        text = 'Done: committed abc\n\n───────────\n> \n? for shortcuts\n⏵⏵ auto mode on (shift+tab to cycle)\n'
        self.assertEqual(ct.last_line(text), 'Done: committed abc')
        self.assertEqual(ct.last_line('│ Reading files │\n'), 'Reading files')
        self.assertEqual(ct.last_line(''), '')


class HerdrCallTests(unittest.TestCase):
    def test_success_is_exit_zero_without_an_error_key(self):
        def runner(code, out):
            return lambda argv, timeout: subprocess.CompletedProcess(argv, code, out, '')
        self.assertTrue(ct.Tools(runner(0, 'ok\n')).prompt('s', 'w1:p1', 'task'))
        self.assertTrue(ct.Tools(runner(0, json.dumps({'result': {'type': 'agent_prompted'}}))).prompt('s', 'w1:p1', 'task'))
        self.assertFalse(ct.Tools(runner(0, json.dumps({'error': {'code': 'agent_blocked'}}))).prompt('s', 'w1:p1', 'task'))
        self.assertFalse(ct.Tools(runner(1, '')).prompt('s', 'w1:p1', 'task'))


class SnapshotTests(ToolsBase):
    def test_snapshot_lists_sessions_agents_last_lines_previews_and_open_prs(self):
        text = ct.snapshot(ct.Tools())
        self.assertIn('dashboard', text); self.assertIn('old', text)
        self.assertIn('reviewer', text); self.assertIn('working', text); self.assertIn('[api]', text)
        self.assertIn('Running tests 3/9', text); self.assertIn('ai-vaerk/website', text)
        self.assertIn('Committed. token <redacted>', text); self.assertNotIn(fixture_token(), text)
        self.assertNotIn(fixture_token('g'), text); self.assertNotIn('example.com', text)
        self.assertIn('https://personal-vps.tail1.ts.net:8443/', text)
        self.assertIn('#12', text); self.assertIn('https://github.com/ai-vaerk/website/pull/12', text)
        self.assertNotIn(ESC, text); self.assertNotIn('? for shortcuts', text)
        value = json.loads(ct.snapshot(ct.Tools(), as_json=True))
        dashboard = next(s for s in value['sessions'] if s['name'] == 'dashboard')
        self.assertTrue(dashboard['running'])
        rows = [a for w in dashboard['workspaces'] for a in w['agents']]
        reviewer = next(a for a in rows if a['name'] == 'reviewer')
        self.assertEqual((reviewer['status'], reviewer['repo'], reviewer['last']), ('working', 'ai-vaerk/website', 'Running tests 3/9'))
        self.assertEqual(value['previews'][0], {'port': 5173, 'label': 'web', 'link': 'https://personal-vps.tail1.ts.net:8443/'})
        self.assertEqual(value['prs']['ai-vaerk/website'][0]['number'], 12)
        stopped = next(s for s in value['sessions'] if s['name'] == 'old')
        self.assertEqual(stopped['workspaces'], [])
        # A stopped session is never queried (asking could start its server).
        self.assertFalse([c for c in self.calls() if c[:3] == ['herdr', '--session', 'old']])

    def test_snapshot_is_bounded(self):
        value = base_state()
        value['agents']['dashboard'] = [agent('w%d:p1' % i, 'working', name='a%d' % i) for i in range(200)]
        value['reads'] = {'w%d:p1' % i: 'y' * 900 + '\n' for i in range(200)}
        self.set_state(value)
        text = ct.snapshot(ct.Tools())
        self.assertLessEqual(len(text.splitlines()), 60); self.assertLessEqual(len(text.encode()), 6 * 1024)
        self.assertIn('160 more agents', text)
        with mock.patch.object(ct, 'MAX_AGENTS', 500):
            text = ct.snapshot(ct.Tools())
        self.assertLessEqual(len(text.splitlines()), 60); self.assertLessEqual(len(text.encode()), 6 * 1024)
        self.assertIn('more lines (use --json)', text.splitlines()[-1])
        data = json.loads(ct.snapshot(ct.Tools(), as_json=True))
        lasts = [a['last'] for s in data['sessions'] for w in s['workspaces'] for a in w['agents']]
        self.assertTrue(lasts); self.assertTrue(all(len(last) <= 160 for last in lasts))
        self.assertLessEqual(len(lasts), ct.MAX_AGENTS)

    def test_missing_preview_and_unauthenticated_gh_are_skipped_quietly(self):
        (self.bin / 'preview').unlink()
        self.set_state({**base_state(), 'gh_unauthenticated': True})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            text = ct.snapshot(ct.Tools()); value = json.loads(ct.snapshot(ct.Tools(), as_json=True))
        self.assertEqual(err.getvalue(), '')
        self.assertNotIn('preview', text.lower()); self.assertNotIn('pull request', text.lower())
        self.assertEqual((value['previews'], value['prs']), ([], {}))

    def test_agent_read_plain_text_output_is_used_as_is(self):
        self.set_state({**base_state(), 'raw_reads': True})
        value = json.loads(ct.snapshot(ct.Tools(), as_json=True))
        rows = [a for s in value['sessions'] for w in s['workspaces'] for a in w['agents']]
        self.assertEqual(next(a for a in rows if a['name'] == 'reviewer')['last'], 'Running tests 3/9')

    def test_missing_herdr_is_one_line_not_an_error(self):
        (self.bin / 'herdr').unlink()
        self.assertIn('herdr is not available', ct.snapshot(ct.Tools()))


class EventsTests(ToolsBase):
    def test_first_tick_only_seeds_a_private_state_file(self):
        self.assertEqual(self.tick(1000, first=True), [])
        self.assertEqual(stat.S_IMODE(self.events_path.stat().st_mode), 0o600)
        self.assertEqual(self.tick(1020), [])

    def test_important_changes_print_at_once_one_line_each(self):
        self.tick(1000, first=True)
        value = base_state()
        value['agents']['dashboard'] = [agent('w1:p1', 'idle', name='reviewer'), agent('w2:p1', 'blocked', cwd='/srv/justus/dev/claude-pool', name='pooler'),
                                        agent('w3:p1', 'done', name='writer')]
        value['agents']['old'] = []; value['sessions'][1]['running'] = True
        value['reads']['w1:p1'] = 'Ready for review\n'
        value['previews'] = [{'port': 3000, 'label': 'docs', 'link': 'https://personal-vps.tail1.ts.net:9443/'}]
        value['prs']['ai-vaerk/website'] = [{'number': 12, 'title': 'Über uns: neu', 'state': 'MERGED', 'url': 'u12', 'headRefName': 'about'},
                                            {'number': 13, 'title': 'Kontakt', 'state': 'OPEN', 'url': 'u13', 'headRefName': 'k'}]
        self.set_state(value)
        lines = self.tick(1020)
        self.assertTrue(all(line.startswith('conductor-event ') for line in lines))
        kinds = sorted(line.split()[1] for line in lines)
        self.assertEqual(kinds, sorted(['agent-waiting', 'agent-blocked', 'agent-done', 'session-started', 'pr-merged', 'pr-opened',
                                        'preview-started', 'preview-stopped']))
        self.assertIn('conductor-event agent-waiting dashboard/reviewer website: Ready for review', lines)
        self.assertIn('agent-blocked dashboard/pooler', '\n'.join(lines))
        self.assertIn('conductor-event session-started old', lines)
        self.assertIn('conductor-event pr-merged ai-vaerk/website#12 Über uns: neu u12', lines)
        self.assertIn('conductor-event pr-opened ai-vaerk/website#13 Kontakt u13', lines)
        self.assertIn('conductor-event preview-started 3000 docs https://personal-vps.tail1.ts.net:9443/', lines)
        self.assertIn('conductor-event preview-stopped 5173 web https://personal-vps.tail1.ts.net:8443/', lines)
        # A new agent appearing as done is important; nothing unimportant prints before the batch window.
        self.assertIn('agent-done dashboard/writer', '\n'.join(lines))
        self.assertFalse([line for line in lines if ' batch ' in line])
        value['sessions'][1]['running'] = False
        self.set_state(value)
        self.assertEqual(self.tick(1040), ['conductor-event session-ended old'])

    def test_unimportant_changes_batch_into_one_line_per_ten_minutes_and_churn_never_counts(self):
        self.tick(1000, first=True)
        self.assertEqual(self.tick(1020), [])  # working -> working: nothing, not even a batch count
        value = base_state()
        value['agents']['dashboard'][1]['agent_status'] = 'working'  # idle -> working
        value['agents']['dashboard'].append(agent('w4:p1', 'working', name='fresh'))  # new agent appearing as working
        self.set_state(value)
        self.assertEqual(self.tick(1040), [])
        self.assertEqual(self.tick(1300), [])
        self.assertEqual(self.tick(1600), ['conductor-event batch 2 changes'])
        self.assertEqual(self.tick(1620), [])
        value['agents']['dashboard'][2]['agent_status'] = 'idle'  # working -> idle is important
        value['agents']['dashboard'][1]['agent_status'] = 'unknown'
        self.set_state(value)
        lines = self.tick(1640)
        self.assertEqual([line.split()[1] for line in lines], ['agent-waiting'])
        self.assertEqual(self.tick(2100), [])
        self.assertEqual(self.tick(2200), ['conductor-event batch 1 changes'])

    def test_herdr_failure_announces_nothing_and_keeps_the_last_state(self):
        self.tick(1000, first=True)
        self.set_state({**base_state(), 'herdr_down': True})
        self.assertEqual(self.tick(1020), [])
        value = base_state(); value['agent_list_fails'] = ['dashboard']
        self.set_state(value)
        self.assertEqual(self.tick(1040), [])
        (self.bin / 'preview').unlink(); self.set_state({**base_state(), 'gh_unauthenticated': True})
        self.assertEqual(self.tick(1060), [])
        self.set_state(base_state())
        self.assertEqual(self.tick(1080), [])

    def test_restart_reseeds_silently_and_keeps_since_and_batch_clock(self):
        self.tick(1000, first=True)
        before = json.loads(self.events_path.read_text())
        value = base_state()
        value['agents']['dashboard'][0]['agent_status'] = 'done'
        self.set_state(value)
        self.assertEqual(self.tick(5000, first=True), [])
        after = json.loads(self.events_path.read_text())
        self.assertEqual(after['agents']['dashboard/w2:p1']['since'], before['agents']['dashboard/w2:p1']['since'])
        self.assertEqual(after['agents']['dashboard/w1:p1']['since'], 5000)
        value['agents']['dashboard'][0]['agent_status'] = 'blocked'
        self.set_state(value)
        self.assertEqual([line.split()[1] for line in self.tick(5020)], ['agent-blocked'])

    def test_prs_are_only_asked_when_due_and_seed_silently(self):
        self.tick(1000, first=True, with_prs=False)
        self.assertFalse([c for c in self.calls() if c[0] == 'gh'])
        self.assertEqual(self.tick(1020, with_prs=True), [])  # first sight of a repo seeds it
        gh = [c for c in self.calls() if c[0] == 'gh']
        fields = ['--json', 'number,title,state,url,headRefName', '--limit', '20']
        self.assertEqual(gh, [['gh', 'pr', 'list', '--repo', 'ai-vaerk/website', *fields],
                              ['gh', 'pr', 'list', '--repo', 'ai-vaerk/website', *fields, '--state', 'merged',
                               '--search', 'merged:>=' + time.strftime('%Y-%m-%d', time.gmtime(1020 - 86400))]])

    def test_a_merge_older_than_the_last_twenty_prs_is_not_missed(self):
        value = base_state()
        value['prs']['ai-vaerk/website'] = [{'number': n, 'title': 'p%d' % n, 'state': 'CLOSED', 'url': 'u', 'headRefName': 'b'}
                                            for n in range(100, 140)] + value['prs']['ai-vaerk/website']
        self.set_state(value)
        self.tick(1000, first=True)
        value['prs']['ai-vaerk/website'][-1]['state'] = 'MERGED'
        self.set_state(value)
        self.assertIn('pr-merged ai-vaerk/website#12', '\n'.join(self.tick(1020)))

    def test_unreadable_state_file_reseeds_silently_and_then_reports_again(self):
        for spoil in ('{not json', 'mode', 'big'):
            with self.subTest(spoil=spoil):
                self.set_state(base_state())
                self.tick(1000, first=True)
                if spoil == 'mode': self.events_path.chmod(0o644)
                else:
                    self.events_path.write_text(spoil if spoil != 'big' else json.dumps({'x': 'y' * (2 << 20)}))
                    self.events_path.chmod(0o600)
                value = base_state(); value['agents']['dashboard'][0]['agent_status'] = 'blocked'
                self.set_state(value)
                self.assertEqual(self.tick(1020), [])
                self.assertEqual(stat.S_IMODE(self.events_path.stat().st_mode), 0o600)
                value['agents']['dashboard'][0]['agent_status'] = 'idle'
                self.set_state(value)
                self.assertEqual([line.split()[1] for line in self.tick(1040)], ['agent-waiting'])

    def test_a_second_watcher_exits_at_once_with_one_line(self):
        self.runtime.mkdir(mode=0o700)
        lines = []
        with ct._watcher_lock(self.events_path) as held:
            self.assertTrue(held)
            code = ct.events(self.events_path, self.queue_path, interval=0.01, tools=ct.Tools(), write=lines.append)
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1); self.assertIn('already running', lines[0])
        self.assertFalse(self.events_path.exists())

    def test_events_command_exits_cleanly_on_sigterm_and_prints_nothing_at_start(self):
        config = self.dir / 'config.json'
        config.write_text(json.dumps({'state_dir': str(self.runtime), 'credential_dir': str(self.dir / 'credentials'),
                                      'native_claude': '/bin/false'}))
        env = {**self.env, 'HOME': str(self.dir), 'CLAUDE_POOL_CONFIG': str(config), 'PYTHONDONTWRITEBYTECODE': '1'}
        child = subprocess.Popen([sys.executable, str(ROOT / 'gateway/launch.py'), 'conductor', 'events', '--interval', '0.2'],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 20
            while not self.events_path.exists() and time.time() < deadline: time.sleep(0.05)
            self.assertTrue(self.events_path.exists())
            child.send_signal(signal.SIGTERM)
            out, err = child.communicate(timeout=20)
        finally:
            if child.poll() is None: child.kill(); child.wait()
        self.assertEqual((child.returncode, out, err), (0, '', ''))


class TellTests(ToolsBase):
    def herdr_calls(self):
        return [c for c in self.calls() if c[0] == 'herdr']

    def assert_never_creates_sessions(self):
        for call in self.herdr_calls():
            if call[1:2] == ['session']: self.assertEqual(call, ['herdr', 'session', 'list', '--json'])
            if call[1:2] == ['--session']: self.assertEqual(call[2], 'dashboard')

    def test_idle_agent_is_prompted_by_name_session_project_or_session_slash_agent(self):
        for target in ['pooler', 'dashboard/pooler', 'dashboard/w2:p1', 'claude-pool']:
            with self.subTest(target=target):
                self.log.write_text('')
                code, line = ct.tell(target, 'Fix the flaky test', tools=ct.Tools(), queue_path=self.queue_path)
                self.assertEqual((code, line), (0, 'prompted dashboard/pooler'))
                self.assertIn(['herdr', '--session', 'dashboard', 'agent', 'prompt', 'w2:p1', 'Fix the flaky test'], self.herdr_calls())
        # A session with several agents is ambiguous even when only one of them is idle.
        code, line = ct.tell('dashboard', 'x', tools=ct.Tools(), queue_path=self.queue_path)
        self.assertEqual(code, 1); self.assertIn('dashboard/reviewer (working)', line); self.assertIn('dashboard/pooler (idle)', line)
        self.assert_never_creates_sessions()

    def test_ambiguous_or_unknown_target_fails_with_candidates(self):
        value = base_state(); value['agents']['dashboard'][1]['agent_status'] = 'working'
        self.set_state(value)
        code, line = ct.tell('dashboard', 'x', tools=ct.Tools(), queue_path=self.queue_path)
        self.assertEqual(code, 1)
        self.assertIn('dashboard/reviewer (working)', line); self.assertIn('dashboard/pooler (working)', line)
        code, line = ct.tell('nowhere', 'x', tools=ct.Tools(), queue_path=self.queue_path)
        self.assertEqual(code, 1); self.assertIn('no agent', line)
        self.assertFalse([c for c in self.herdr_calls() if 'prompt' in c])
        self.assertFalse(self.queue_path.exists())
        self.assert_never_creates_sessions()

    def test_busy_agent_is_queued_and_events_deliver_it_when_idle(self):
        self.tick(1000, first=True)
        code, line = ct.tell('reviewer', 'Review PR 12', tools=ct.Tools(), queue_path=self.queue_path, now=1010)
        self.assertEqual(code, 0); self.assertRegex(line, r'^queued for dashboard/reviewer \(id [0-9a-f]{6}\)$')
        self.assertIn(json.loads(self.queue_path.read_text())['items'][0]['id'], line)  # the id `tell --cancel` takes
        self.assertEqual(stat.S_IMODE(self.queue_path.stat().st_mode), 0o600)
        self.assertFalse([c for c in self.herdr_calls() if 'prompt' in c])
        self.assertEqual(self.tick(1020), [])  # still working: nothing delivered
        value = base_state(); value['agents']['dashboard'][0]['agent_status'] = 'idle'
        self.set_state(value)
        lines = self.tick(1040)
        self.assertIn('conductor-event delivered dashboard/reviewer Review PR 12', lines)
        self.assertIn(['herdr', '--session', 'dashboard', 'agent', 'prompt', 'w1:p1', 'Review PR 12'], self.herdr_calls())
        self.assertEqual(json.loads(self.queue_path.read_text())['items'], [])
        self.assertNotIn('delivered', ' '.join(self.tick(1060)))
        self.assert_never_creates_sessions()

    def test_failed_prompt_keeps_the_queued_task(self):
        ct.tell('reviewer', 'Later', tools=ct.Tools(), queue_path=self.queue_path, now=1010)
        self.tick(1000, first=True)
        value = base_state(); value['agents']['dashboard'][0]['agent_status'] = 'idle'; value['prompt_fails'] = True
        self.set_state(value)
        self.assertNotIn('delivered', ' '.join(self.tick(1040)))
        self.assertEqual(len(json.loads(self.queue_path.read_text())['items']), 1)
        self.assertEqual(self.tick(1060), [])
        self.assertEqual(self.tick(1080), ['conductor-event undeliverable dashboard/reviewer Later'])
        self.assertEqual(json.loads(self.queue_path.read_text())['items'], [])
        prompts = [c for c in self.herdr_calls() if c[4:5] == ['prompt']]
        self.assertEqual(len(prompts), 3)
        self.assertEqual(self.tick(1100), [])

    def test_herdr_error_reply_counts_as_a_failed_delivery(self):
        ct.tell('reviewer', 'Later', tools=ct.Tools(), queue_path=self.queue_path, now=1010)
        self.tick(1000, first=True)
        value = base_state(); value['agents']['dashboard'][0]['agent_status'] = 'idle'; value['prompt_error'] = True
        self.set_state(value)
        self.assertNotIn('delivered', ' '.join(self.tick(1040)))
        self.assertEqual(json.loads(self.queue_path.read_text())['items'][0]['attempts'], 1)

    def orphan(self):
        self.tick(1000, first=True)
        ct.tell('reviewer', 'Review PR 12', tools=ct.Tools(), queue_path=self.queue_path, now=1010)
        value = base_state(); del value['agents']['dashboard'][0]
        self.set_state(value)
        self.assertEqual(self.tick(1020), ['conductor-event orphaned dashboard/reviewer Review PR 12'])
        self.assertIn('dashboard/reviewer Review PR 12', ct.snapshot(ct.Tools(), queue_path=self.queue_path))
        orphaned = json.loads(ct.snapshot(ct.Tools(), as_json=True, queue_path=self.queue_path))['orphaned']
        self.assertEqual(orphaned, [{'agent': 'dashboard/reviewer', 'task': 'Review PR 12'}])

    def test_orphaned_queue_item_is_reported_once_and_delivered_when_the_agent_comes_back(self):
        self.orphan()
        for poll in range(10): self.assertEqual(self.tick(1040 + 20 * poll), [])
        self.assertEqual(len(json.loads(self.queue_path.read_text())['items']), 1)
        value = base_state(); value['agents']['dashboard'][0]['agent_status'] = 'idle'
        self.set_state(value)
        self.assertIn('conductor-event delivered dashboard/reviewer Review PR 12', self.tick(1300))
        self.assertEqual(json.loads(self.queue_path.read_text())['items'], [])
        self.assertEqual(len([c for c in self.herdr_calls() if c[4:5] == ['prompt']]), 1)

    def test_orphaned_queue_item_is_dropped_with_one_line_after_a_day(self):
        self.orphan()
        # The agent vanishing is one unimportant change; its batch line is due by now.
        self.assertEqual(self.tick(1010 + 86400 - 20), ['conductor-event batch 1 changes'])
        self.assertEqual(self.tick(1010 + 86400), ['conductor-event undeliverable dashboard/reviewer Review PR 12'])
        self.assertEqual(json.loads(self.queue_path.read_text())['items'], [])
        self.assertEqual(self.tick(1010 + 86420), [])
        self.assertFalse([c for c in self.herdr_calls() if c[4:5] == ['prompt']])

    def test_parallel_starts_a_pool_agent_in_a_new_workspace_of_the_same_session(self):
        code, line = ct.tell('reviewer', 'Write the docs', parallel=True, tools=ct.Tools(), queue_path=self.queue_path, sleep=lambda s: None)
        self.assertEqual(code, 0); self.assertEqual(line, 'started dashboard/reviewer-2 in a new workspace through the pool and prompted it')
        calls = self.herdr_calls()
        self.assertIn(['herdr', '--session', 'dashboard', 'workspace', 'create', '--cwd', '/srv/justus/dev/website', '--label', 'reviewer-2',
                       '--no-focus'], calls)
        self.assertIn(['herdr', '--session', 'dashboard', 'agent', 'start', 'reviewer-2', '--kind', 'claude', '--pane', 'w9:p1'], calls)
        self.assertIn(['herdr', '--session', 'dashboard', 'pane', 'process-info', '--pane', 'w9:p1'], calls)
        self.assertEqual(calls[-1], ['herdr', '--session', 'dashboard', 'agent', 'prompt', 'w9:p1', 'Write the docs'])
        self.assertFalse(self.queue_path.exists())
        self.assert_never_creates_sessions()

    def test_parallel_agent_outside_the_pool_is_stopped_and_reported(self):
        value = base_state(); value['processes'] = [{'pid': 11, 'name': 'claude', 'argv': ['/srv/justus/.local/bin/claude']}]
        self.set_state(value)
        code, line = ct.tell('reviewer', 'Write the docs', parallel=True, tools=ct.Tools(), queue_path=self.queue_path, sleep=lambda s: None)
        self.assertEqual(code, 1); self.assertIn('launch.py exec', line); self.assertIn('stopped', line)
        calls = self.herdr_calls()
        self.assertEqual(calls[-1], ['herdr', '--session', 'dashboard', 'workspace', 'close', 'w9'])
        self.assertFalse([c for c in calls if c[4:5] == ['prompt']])

    def test_tell_lines_are_cleaned(self):
        value = base_state(); value['agents']['dashboard'][1]['name'] = 'pool' + ESC + '[31mer'
        value['agents']['dashboard'][0]['agent_status'] = 'idle'
        self.set_state(value)
        code, line = ct.tell('dashboard', 'x', tools=ct.Tools(), queue_path=self.queue_path)
        self.assertEqual(code, 1); self.assertNotIn(ESC, line)

    def test_busy_agent_without_parallel_never_creates_a_workspace(self):
        ct.tell('reviewer', 'x', tools=ct.Tools(), queue_path=self.queue_path)
        self.assertFalse([c for c in self.herdr_calls() if 'create' in c or 'start' in c])


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'conductor' / 'journal.md'

    def test_add_show_and_prune_keep_fourteen_days_and_the_file_mode(self):
        day = 86400
        now = time.mktime((2026, 10, 18, 12, 0, 0, 0, 0, -1))
        ct.journal_add(self.path, 'dashboard: old work', now=now - 20 * day)
        self.path.chmod(0o640)
        ct.journal_add(self.path, 'dashboard: reviewer done\nsecond line ' + fixture_token(), now=now - 2 * 3600)
        ct.journal_add(self.path, 'pool: PR #12 merged', now=now - 2 * day)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1], '- 2026-10-18 10:00 dashboard: reviewer done second line <redacted>')
        self.assertEqual(ct.journal_show(self.path, ct.duration('12h'), now=now), lines[1])
        self.assertEqual(ct.journal_show(self.path, ct.duration('14d'), now=now).splitlines(), [lines[1], lines[2]])
        self.assertEqual(ct.journal_prune(self.path, now=now), 1)
        self.assertEqual(self.path.read_text().splitlines(), lines[1:])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_new_journal_is_private_and_non_entry_lines_survive_prune(self):
        self.path.parent.mkdir()
        self.path.write_text('# Conductor journal\n')
        self.path.chmod(0o600)
        ct.journal_add(self.path, 'x', now=time.time())
        self.assertEqual(ct.journal_prune(self.path, now=time.time()), 0)
        self.assertTrue(self.path.read_text().startswith('# Conductor journal\n- '))
        other = Path(self.tmp.name) / 'new' / 'journal.md'
        ct.journal_add(other, 'y', now=time.time())
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o600)

    def test_durations(self):
        self.assertEqual([ct.duration(v) for v in ('30m', '12h', '1d', '14d')], [1800, 43200, 86400, 14 * 86400])
        with self.assertRaises(ValueError): ct.duration('soon')


class CliTests(ToolsBase):
    def test_help_lists_conductor(self):
        import launch
        with mock.patch.object(launch.sys, 'argv', ['claude-pool', '--help']), contextlib.redirect_stdout(io.StringIO()) as out:
            launch.main()
        self.assertIn('conductor snapshot', out.getvalue())

    def test_cli_dispatch_runs_journal_snapshot_and_tell(self):
        import launch
        journal = self.dir / 'journal.md'
        for argv, expected in [(['conductor', 'journal', 'add', 'hello', 'world', '--file', str(journal)], ''),
                               (['conductor', 'journal', 'show', '--since', '1d', '--file', str(journal)], 'hello world'),
                               (['conductor', 'snapshot'], 'reviewer'),
                               (['conductor', 'tell', 'pooler', 'Fix', 'it'], 'prompted dashboard/pooler')]:
            with self.subTest(argv=argv), mock.patch.object(launch.sys, 'argv', ['claude-pool', *argv]), \
                    mock.patch.object(ct, 'QUEUE', self.queue_path), contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as done: launch.main()
                self.assertEqual(done.exception.code, 0)
                self.assertIn(expected, out.getvalue())
        self.assertIn(['herdr', '--session', 'dashboard', 'agent', 'prompt', 'w2:p1', 'Fix it'], self.calls())


if __name__ == '__main__': unittest.main()
