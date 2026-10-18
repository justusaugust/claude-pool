"""herdr session units run against a fake machine: herdr, systemctl, /proc and signals are all simulated in Python.
Nothing real is started, stopped or read outside a temp folder."""
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import device_herdr
import device_sync
from remote import Completed

LISTEN = '00010000'


def ok(value=None): return Completed(0, json.dumps(value).encode() if value is not None else b'', b'')
def fail(code=1): return Completed(code, b'', b'')


def session_json(agents):
    """A version-3 shaped session.json: workspaces with panes, each Claude pane naming its conversation id."""
    panes = [{'pane_id': pane, 'cwd': '/srv/u/projects/x', 'agent_name': name, 'managed_agent_kind': kind,
              'agent_session': {'source': 'herdr:' + kind, 'kind': 'id', 'value': conversation}}
             for pane, name, kind, conversation in agents]
    return {'version': 3, 'workspaces': [{'workspace_id': 'w1', 'tabs': [{'layout': {'panes': panes}}]}]}


class Machine:
    """A Linux user's herdr sessions, systemd user manager and process table."""
    def __init__(self, root):
        self.root = root
        self.home = root / 'home'; (self.home / '.local/bin').mkdir(parents=True)
        self.herdr_path = self.home / '.local/bin/herdr'
        self.herdr_path.write_text('#!/bin/sh\nexit 97\n'); self.herdr_path.chmod(0o755)  # never run: calls go to handle()
        self.units = self.home / '.config/systemd/user'; self.units.mkdir(parents=True)
        self.proc = root / 'proc'; (self.proc / 'net').mkdir(parents=True)
        self.sessions, self.enabled, self.active, self.calls, self.killed = {}, set(), set(), [], []
        self.next_pid, self.inode = 4000, 7000
        self.restore = 'same'  # what a server started by a unit brings back: same, other-workspaces, other-id, no-agents
        self.list_fails = False

    def add(self, name, running=True, agents=(), workspaces=None, statuses=None, saved=True):
        folder = self.home / '.config/herdr/sessions' / name; folder.mkdir(parents=True)
        if saved: (folder / 'session.json').write_text(json.dumps(session_json(agents)))
        entry = {'running': False, 'agents': list(agents), 'statuses': dict(statuses or {}),
                 'workspaces': workspaces if workspaces is not None else [{'workspace_id': 'w1', 'label': 'main'}],
                 'socket': str(self.root / 'run' / (name + '.sock')), 'pid': None, 'folder': folder}
        self.sessions[name] = entry
        if running: self.serve(name)
        return entry

    def serve(self, name, parent=1):
        """A herdr server for `name`: a process that holds the listening socket."""
        entry = self.sessions[name]
        pid, self.inode = self.next_pid, self.inode + 1
        self.next_pid += 1
        folder = self.proc / str(pid); (folder / 'fd').mkdir(parents=True)
        (folder / 'stat').write_text(f'{pid} (herdr) S {parent} 1 1 0 -1\n')
        (folder / 'cmdline').write_bytes(b'/srv/u/.local/bin/herdr\0server\0')
        os.symlink('socket:[' + str(self.inode) + ']', folder / 'fd' / '3')
        os.symlink('/dev/null', folder / 'fd' / '0')
        with (self.proc / 'net/unix').open('a') as table:
            table.write(f'0000000000000000: 00000002 00000000 {LISTEN} 0001 01 {self.inode} {entry["socket"]}\n')
        entry.update(running=True, pid=pid)
        return pid

    def write_table_header(self):
        (self.proc / 'net/unix').write_text('Num       RefCount Protocol Flags    Type St Inode Path\n')

    def kill(self, pid, sig):
        self.killed.append((pid, sig))
        if sig != signal.SIGTERM: return
        shutil.rmtree(self.proc / str(pid), ignore_errors=True)
        for entry in self.sessions.values():
            if entry['pid'] == pid: entry.update(running=False, pid=None)

    def unit_started(self, name):
        entry = self.sessions[name]
        if entry['running']: return 1  # the socket is taken: the unit fails
        self.serve(name, parent=999)
        if self.restore == 'other-workspaces': entry['workspaces'] = [{'workspace_id': 'w9', 'label': 'new'}]
        if self.restore == 'other-id':
            agents = [(pane, agent, kind, 'f' * 8) for pane, agent, kind, _ in entry['agents']]
            (entry['folder'] / 'session.json').write_text(json.dumps(session_json(agents)))
        if self.restore == 'no-agents': entry['statuses'] = {}; entry['gone'] = True
        return 0

    def handle(self, argv, stdin=None, timeout=None):
        self.calls.append(list(argv))
        if argv[0] == str(self.herdr_path): return self.herdr(argv[1:])
        if argv[:2] == ['systemctl', '--user']: return self.systemctl(argv[2:])
        return fail(127)

    def herdr(self, args):
        if args == ['session', 'list', '--json']:
            if self.list_fails: return fail()
            return ok({'sessions': [{'name': n, 'running': e['running'], 'default': False, 'session_dir': str(e['folder']),
                                     'socket_path': e['socket']} for n, e in self.sessions.items()]})
        if args[:1] == ['--session'] and args[1] in self.sessions:
            entry = self.sessions[args[1]]
            if not entry['running']: return fail()
            if args[2:] == ['agent', 'list']:
                rows = [] if entry.get('gone') else [
                    {'agent': kind, 'name': agent, 'pane_id': pane, 'workspace_id': 'w1', 'agent_status': entry['statuses'].get(agent, 'idle')}
                    for pane, agent, kind, _ in entry['agents']]
                return ok({'result': {'agents': rows}})
            if args[2:] == ['workspace', 'list']: return ok({'result': {'workspaces': entry['workspaces']}})
        return fail()

    def systemctl(self, args):
        quiet = [a for a in args if a != '--quiet']
        if quiet == ['daemon-reload']: return ok()
        verb, unit = quiet[0], quiet[-1]
        name = unit[len('claude-pool-herdr@'):-len('.service')] if unit.startswith('claude-pool-herdr@') else None
        if verb == 'is-enabled': return ok() if unit in self.enabled else fail()
        if verb == 'is-active': return ok() if unit in self.active else fail(3)
        if verb == 'enable':
            if not (self.units / 'claude-pool-herdr@.service').exists(): return fail()
            self.enabled.add(unit)
            wants = self.units / 'default.target.wants'; wants.mkdir(exist_ok=True)
            link = wants / unit
            if not link.is_symlink(): os.symlink(self.units / 'claude-pool-herdr@.service', link)
            return ok()
        if verb == 'disable':
            self.enabled.discard(unit); (self.units / 'default.target.wants' / unit).unlink(missing_ok=True); return ok()
        if verb == 'start':
            if name is None or name not in self.sessions or self.unit_started(name): return fail()
            self.active.add(unit); return ok()
        return fail()


class FakeLocal:
    def __init__(self, machine): self.machine = machine
    def call(self, argv, stdin=None, timeout=120): return self.machine.handle(argv, stdin, timeout)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / 'bin'; self.bin.mkdir()  # no tools at all: every command goes through the fake machine
        env = mock.patch.dict(os.environ, {'PATH': str(self.bin) + ':/usr/bin:/bin', 'HOME': str(root / 'home')})
        env.start(); self.addCleanup(env.stop)
        self.m = Machine(root); self.m.write_table_header()
        self.clock = [0.0]
        linux = mock.patch.object(device_herdr.service, 'platform', return_value='linux'); linux.start(); self.addCleanup(linux.stop)

    def host(self):
        def sleep(seconds): self.clock[0] += seconds
        return device_herdr.Host(home=self.m.home, local=FakeLocal(self.m), proc=self.m.proc, kill=self.m.kill, sleep=sleep,
                                 clock=lambda: self.clock[0])

    def apply(self, listed=(), adopt=False):
        return device_herdr.apply(self.host(), list(listed), adopt=adopt)

    def by_name(self, steps): return {step.name: step for step in steps}

    def starts(self): return [c for c in self.m.calls if c[2:3] == ['start']]


CLAUDE = [('w1:p1', 'werkbank', 'claude', 'c90c7a70'), ('w1:p2', 'helper', 'claude', 'd819a980')]


class UnitFileTests(Base):
    def test_the_template_unit_starts_one_herdr_server_per_instance(self):
        text = device_herdr.template(str(self.m.herdr_path), self.m.home)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith(device_herdr.MARK))
        self.assertIn('ExecStart=' + str(self.m.herdr_path) + ' --session %i server', lines)
        self.assertIn('Environment=PATH=' + str(self.m.home) + '/.local/bin:/usr/local/bin:/usr/bin:/bin', lines)
        for line in ['Restart=on-failure', 'RestartSec=3', '[Install]', 'WantedBy=default.target']: self.assertIn(line, lines)
        self.assertNotIn('$HOME', text)

    def test_apply_writes_the_template_once_and_reloads_only_when_it_changed(self):
        self.m.add('dashboard', running=False)
        self.apply(['dashboard'])
        path = self.m.units / device_herdr.TEMPLATE
        self.assertEqual(path.read_text(), device_herdr.template(str(self.m.herdr_path), self.m.home))
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o644')
        reloads = lambda: [c for c in self.m.calls if c[2:] == ['daemon-reload']]
        self.assertEqual(len(reloads()), 1)
        self.apply(['dashboard'])
        self.assertEqual(len(reloads()), 1)

    def test_a_template_claude_pool_did_not_write_is_never_overwritten(self):
        self.m.add('dashboard')
        path = self.m.units / device_herdr.TEMPLATE; path.write_text('[Service]\nExecStart=/bin/true\n')
        steps = self.apply()
        self.assertEqual(steps[0].status, 'failed'); self.assertIn('did not write', steps[0].detail)
        self.assertEqual(path.read_text(), '[Service]\nExecStart=/bin/true\n')
        self.assertFalse([c for c in self.m.calls if c[2:3] == ['enable']])


class ApplyTests(Base):
    def test_a_running_session_is_enabled_but_never_started_or_stopped(self):
        self.m.add('dashboard', agents=CLAUDE)
        pid = self.m.sessions['dashboard']['pid']
        steps = self.by_name(self.apply())
        self.assertEqual(steps['dashboard'].status, 'ok'); self.assertIn('next boot', steps['dashboard'].detail)
        self.assertIn('claude-pool-herdr@dashboard.service', self.m.enabled)
        self.assertEqual(self.starts(), []); self.assertEqual(self.m.killed, [])
        self.assertTrue((self.m.proc / str(pid)).exists())

    def test_a_registered_session_that_is_not_running_is_enabled_and_started(self):
        self.m.add('objekt', running=False)
        steps = self.by_name(self.apply(['objekt']))
        self.assertEqual(steps['objekt'].status, 'ok')
        self.assertIn('claude-pool-herdr@objekt.service', self.m.active)

    def test_sessions_neither_running_nor_registered_or_never_saved_are_left_alone(self):
        self.m.add('leitstand', running=False); self.m.add('fresh', saved=False)
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.m.enabled, set())

    def test_a_session_served_by_a_unit_claude_pool_did_not_write_counts_as_served(self):
        self.m.add('trial', agents=())
        (self.m.units / 'herdr-trial.service').write_text(
            '[Service]\nExecStart=/srv/u/.local/bin/herdr --session trial server\nRestart=on-failure\n')
        steps = self.by_name(self.apply(['trial']))
        self.assertEqual((steps['trial'].status, steps['trial'].detail), ('ok', 'served by herdr-trial.service'))
        self.assertEqual(self.m.enabled, set()); self.assertEqual(self.starts(), [])
        self.assertEqual((self.m.units / 'herdr-trial.service').read_text().count('trial'), 1)

    def test_a_foreign_template_instance_and_a_herdr_session_environment_also_count(self):
        self.m.add('conductor'); self.m.add('pr-coord')
        (self.m.units / 'herdr@.service').write_text('[Service]\nExecStart=/usr/bin/herdr --session %i server\n')
        wants = self.m.units / 'default.target.wants'; wants.mkdir()
        os.symlink(self.m.units / 'herdr@.service', wants / 'herdr@conductor.service')
        (self.m.units / 'pr.service').write_text('[Service]\nEnvironment=HERDR_SESSION=pr-coord\nExecStart=-herdr server\n')
        steps = self.by_name(self.apply())
        self.assertEqual(steps['conductor'].detail, 'served by herdr@conductor.service')
        self.assertEqual(steps['pr-coord'].detail, 'served by pr.service')

    def test_herdr_missing_or_not_linux_is_skipped_and_an_unreadable_list_fails(self):
        self.m.herdr_path.unlink()
        self.assertEqual(self.apply()[0].status, 'skipped')
        self.m.herdr_path.write_text('#!/bin/sh\n'); self.m.herdr_path.chmod(0o755)
        self.m.list_fails = True
        self.assertEqual(self.apply()[0].status, 'failed')
        with mock.patch.object(device_herdr.service, 'platform', return_value='darwin'):
            self.assertEqual(self.apply()[0].status, 'skipped')


class AdoptTests(Base):
    def test_adopt_refuses_while_an_agent_works_or_waits_and_changes_nothing(self):
        for status in ['working', 'blocked']:
            with self.subTest(status):
                self.setUp()
                self.m.add('dashboard', agents=CLAUDE, statuses={'helper': status})
                pid = self.m.sessions['dashboard']['pid']
                step = self.by_name(self.apply(adopt=True))['dashboard']
                self.assertEqual(step.status, 'manual'); self.assertIn('helper', step.detail); self.assertIn(status, step.detail)
                self.assertIn('--adopt', step.fix)
                self.assertEqual(self.m.killed, []); self.assertEqual(self.starts(), [])
                self.assertTrue((self.m.proc / str(pid)).exists())
                self.assertIn('claude-pool-herdr@dashboard.service', self.m.enabled)  # the reboot is covered anyway

    def test_adopt_stops_only_the_socket_owner_and_starts_the_unit(self):
        self.m.add('dashboard', agents=CLAUDE)
        self.m.add('objekt', agents=[('w2:p1', 'objekt-claude', 'claude', 'fa1247de')])
        pid, other = self.m.sessions['dashboard']['pid'], self.m.sessions['objekt']['pid']
        # a pane process that inherited the socket stays a child of the server, so only the server is the owner
        child = self.m.next_pid; self.m.next_pid += 1
        (self.m.proc / str(child) / 'fd').mkdir(parents=True)
        (self.m.proc / str(child) / 'stat').write_text(f'{child} (bash) S {pid} 1 1 0 -1\n')
        os.symlink(os.readlink(self.m.proc / str(pid) / 'fd/3'), self.m.proc / str(child) / 'fd/7')
        steps = self.by_name(device_herdr.apply(self.host(), [], adopt=True, only=['dashboard']))
        self.assertEqual(steps['dashboard'].status, 'ok', steps['dashboard']); self.assertIn('adopted', steps['dashboard'].detail)
        self.assertEqual(self.m.killed, [(pid, signal.SIGTERM)])
        self.assertIn('claude-pool-herdr@dashboard.service', self.m.active)
        self.assertTrue((self.m.proc / str(other)).exists()); self.assertNotIn('objekt', steps)

    def test_adopt_refuses_when_no_single_process_owns_the_socket(self):
        self.m.add('dashboard', agents=CLAUDE)
        (self.m.proc / 'net/unix').write_text('Num       RefCount Protocol Flags    Type St Inode Path\n')
        step = self.by_name(self.apply(adopt=True))['dashboard']
        self.assertEqual(step.status, 'manual'); self.assertIn('socket', step.detail)
        self.assertEqual(self.m.killed, [])

    def test_adopt_refuses_a_socket_owner_that_is_not_herdr(self):
        self.m.add('dashboard', agents=CLAUDE)
        pid = self.m.sessions['dashboard']['pid']
        (self.m.proc / str(pid) / 'cmdline').write_bytes(b'/usr/bin/python3\0x.py\0')
        step = self.by_name(self.apply(adopt=True))['dashboard']
        self.assertEqual(step.status, 'manual'); self.assertEqual(self.m.killed, [])

    def test_adopt_verifies_workspaces_and_each_conversation_id(self):
        cases = {'same': 'ok', 'other-workspaces': 'failed', 'other-id': 'failed', 'no-agents': 'failed'}
        for restore, wanted in cases.items():
            with self.subTest(restore):
                self.setUp()
                self.m.add('dashboard', agents=CLAUDE, workspaces=[{'workspace_id': 'w1', 'label': 'werkbank'}])
                self.m.restore = restore
                step = self.by_name(self.apply(adopt=True))['dashboard']
                self.assertEqual(step.status, wanted, step)
                if wanted == 'failed':
                    self.assertIn('herdr --session dashboard', step.fix)
                    self.assertNotIn('c90c7a70', step.detail + step.fix)  # conversation ids are named by agent, never printed
                    self.assertGreaterEqual(self.clock[0], device_herdr.VERIFY_WAIT)
                    self.assertIn('workspaces' if restore == 'other-workspaces' else 'werkbank', step.detail)

    def test_adopt_reports_a_server_that_does_not_stop(self):
        self.m.add('dashboard', agents=CLAUDE)
        host = self.host(); host.kill = lambda pid, sig: self.m.killed.append((pid, sig))
        step = self.by_name(device_herdr.apply(host, [], adopt=True))['dashboard']
        self.assertEqual(step.status, 'failed'); self.assertIn('did not stop', step.detail)
        self.assertEqual(self.starts(), [])

    def test_a_session_already_served_by_its_unit_is_not_adopted_again(self):
        self.m.add('dashboard', agents=CLAUDE, running=False)
        self.apply(['dashboard'])
        self.m.calls.clear()
        step = self.by_name(self.apply(['dashboard'], adopt=True))['dashboard']
        self.assertEqual(step.status, 'ok'); self.assertEqual(self.m.killed, []); self.assertEqual(self.starts(), [])


class CheckTests(Base):
    FIX = 'ssh vps .local/bin/claude-pool device herdr-units'

    def check(self, listed=()):
        return device_herdr.check(device_herdr.report(self.host()), list(listed), self.FIX)

    def test_missing_names_the_sessions_and_the_internal_verb(self):
        self.m.add('dashboard'); self.m.add('objekt', running=False); self.m.add('leitstand', running=False)
        step = self.check(['objekt'])
        self.assertEqual((step.status, step.fix), ('missing', self.FIX))
        self.assertIn('dashboard', step.detail); self.assertIn('objekt', step.detail); self.assertNotIn('leitstand', step.detail)

    def test_ok_once_every_session_is_enabled_or_served_by_another_unit(self):
        self.m.add('dashboard'); self.m.add('trial')
        (self.m.units / 'herdr-trial.service').write_text('[Service]\nExecStart=/x/herdr --session trial server\n')
        self.apply()
        step = self.check()
        self.assertEqual(step.status, 'ok', step); self.assertIn('herdr-trial.service', step.detail)

    def test_an_outdated_template_is_missing_again(self):
        self.m.add('dashboard'); self.apply()
        (self.m.units / device_herdr.TEMPLATE).write_text(device_herdr.MARK + '\n[Service]\nExecStart=/old/herdr --session %i server\n')
        self.assertEqual(self.check().status, 'missing')

    def test_status_part_is_registered_and_reports_nothing_off_linux(self):
        self.assertIn(device_herdr.status_part, device_sync.STATUS_PARTS)
        with mock.patch.object(device_herdr.service, 'platform', return_value='darwin'):
            self.assertEqual(device_herdr.status_part()['herdrUnits']['available'], False)
        self.assertEqual(self.m.calls, [])
        self.assertEqual(device_herdr.check({'available': False, 'reason': 'x'}, [], self.FIX).status, 'skipped')
        self.assertEqual(device_herdr.check(None, [], self.FIX).status, 'failed')


class RemoveTests(Base):
    def test_remove_disables_only_our_instances_and_deletes_only_our_template(self):
        self.m.add('dashboard'); self.m.add('trial')
        (self.m.units / 'herdr-trial.service').write_text('[Service]\nExecStart=/x/herdr --session trial server\n')
        self.m.enabled.add('herdr-trial.service')
        self.apply(['dashboard'])
        step = device_herdr.remove(self.host())
        self.assertEqual(step.status, 'ok', step)
        self.assertFalse((self.m.units / device_herdr.TEMPLATE).exists())
        self.assertTrue((self.m.units / 'herdr-trial.service').exists())
        self.assertEqual(self.m.enabled, {'herdr-trial.service'})
        disabled = [c[-1] for c in self.m.calls if c[2:3] == ['disable']]
        self.assertEqual(disabled, ['claude-pool-herdr@dashboard.service'])
        self.assertEqual(self.m.killed, [])  # running servers keep running until the next reboot

    def test_remove_keeps_a_template_it_did_not_write(self):
        (self.m.units / device_herdr.TEMPLATE).write_text('[Service]\nExecStart=/bin/true\n')
        self.assertEqual(device_herdr.remove(self.host()).status, 'ok')
        self.assertTrue((self.m.units / device_herdr.TEMPLATE).exists())


if __name__ == '__main__': unittest.main()
