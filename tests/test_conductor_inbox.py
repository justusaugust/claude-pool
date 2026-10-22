"""The Conductor's inbox: wakers queue events, a hook in the Conductor's own session delivers them as one bundle and wakes it.
Everything runs on a temporary state directory; no real Conductor, hook, herdr or service is touched."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
sys.path.insert(0, str(ROOT / 'tests'))
import conductor
import conductor_inbox as inbox
import conductor_tools
import settings
import test_conductor

MARK = {inbox.MARK: '1'}


class InboxBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(settings, 'RUNTIME', Path(self.tmp.name) / 'state'); patcher.start(); self.addCleanup(patcher.stop)

    def waiter(self, environ=MARK, parent=lambda: 4242, **kw):
        """Runs one hook waiter in a thread; returns (thread, result dict with code and the text it delivered)."""
        result, out = {}, io.StringIO()
        def run():
            result['code'] = inbox.wait(environ=environ, parent=parent, poll=.02, settle=.05, out=out, err=out, **kw)
            result['text'] = out.getvalue()
        thread = threading.Thread(target=run, daemon=True); thread.start()
        return thread, result

    def registered(self, count=1):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if inbox.status()['generation'] >= count: return
            time.sleep(.01)
        self.fail('waiter did not register')


class InboxTests(InboxBase):
    def test_queued_events_wake_the_conductor_once_as_one_bundle(self):
        self.assertTrue(inbox.add('board antwort 1022-x: Ja'))
        self.assertTrue(inbox.add('memwatch: chromium beendet'))
        thread, result = self.waiter(); thread.join(3)
        self.assertEqual(result['code'], 2)
        self.assertIn('board antwort 1022-x: Ja', result['text']); self.assertIn('memwatch: chromium beendet', result['text'])
        self.assertEqual(inbox.status()['pending'], 0)
        thread, result = self.waiter(deadline=.3); thread.join(3)
        self.assertEqual(result, {'code': 0, 'text': ''})

    def test_an_event_that_arrives_later_wakes_the_waiting_hook(self):
        thread, result = self.waiter(); self.registered()
        self.assertTrue(thread.is_alive())
        inbox.add('board frage 1022-y neu'); thread.join(3)
        self.assertEqual(result['code'], 2); self.assertIn('1022-y', result['text'])

    def test_the_same_key_wakes_only_once_in_ten_minutes(self):
        self.assertTrue(inbox.add('Pool leer', key='pool-leer', now=1000))
        self.assertFalse(inbox.add('Pool leer', key='pool-leer', now=1000 + 599))
        self.assertTrue(inbox.add('Pool leer', key='pool-leer', now=1000 + 601))
        self.assertEqual(inbox.status()['pending'], 2)

    def test_only_the_newest_waiter_delivers(self):
        old, old_result = self.waiter(); self.registered(1)
        new, new_result = self.waiter(); self.registered(2)
        inbox.add('eine Nachricht')
        old.join(3); new.join(3)
        self.assertEqual(old_result, {'code': 0, 'text': ''})
        self.assertEqual(new_result['code'], 2); self.assertIn('eine Nachricht', new_result['text'])

    def test_sessions_without_the_conductor_mark_never_take_events(self):
        inbox.add('nur für Conductor')
        thread, result = self.waiter(environ={}); thread.join(3)
        self.assertEqual(result, {'code': 0, 'text': ''})
        self.assertEqual(inbox.status()['pending'], 1)

    def test_an_orphaned_waiter_leaves_the_events_for_the_next_session(self):
        parents = iter([4242] + [1] * 1000)
        thread, result = self.waiter(parent=lambda: next(parents)); self.registered()
        inbox.add('nach dem Neustart'); thread.join(3)
        self.assertEqual(result, {'code': 0, 'text': ''})
        self.assertEqual(inbox.status()['pending'], 1)

    def test_text_is_one_line_per_event_and_bounded(self):
        inbox.add('zeile eins\nzeile zwei')
        inbox.add('x' * 5000)
        thread, result = self.waiter(); thread.join(3)
        lines = [l for l in result['text'].splitlines() if l.startswith('- ')]
        self.assertEqual(lines[0], '- zeile eins zeile zwei')
        self.assertLessEqual(len(lines[1]), inbox.LINE_LIMIT + 2)


class InstallTests(InboxBase):
    def test_install_adds_both_hooks_keeps_other_settings_and_is_idempotent(self):
        home = Path(self.tmp.name) / 'conductor'
        (home / '.claude').mkdir(parents=True)
        (home / '.claude' / 'settings.local.json').write_text(json.dumps({'permissions': {'allow': ['Bash(ls)']},
                                                                           'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'say done'}]}]}}))
        for _ in range(2): inbox.install(home, '/usr/local/bin/claude-pool')
        value = json.loads((home / '.claude' / 'settings.local.json').read_text())
        self.assertEqual(value['permissions'], {'allow': ['Bash(ls)']})
        for event in ('SessionStart', 'Stop'):
            ours = [h for group in value['hooks'][event] for h in group['hooks'] if 'conductor inbox wait' in h['command']]
            self.assertEqual(len(ours), 1, event)
            self.assertTrue(ours[0]['asyncRewake']); self.assertGreaterEqual(ours[0]['timeout'], 86400)
            self.assertTrue(ours[0]['command'].startswith('/usr/local/bin/claude-pool '))
        self.assertIn({'type': 'command', 'command': 'say done'}, [h for g in value['hooks']['Stop'] for h in g['hooks']])


class WakerTests(InboxBase):
    def script(self, body):
        path = Path(self.tmp.name) / 'waker'
        path.write_text('#!/bin/sh\n' + body + '\n'); path.chmod(0o755)
        return [str(path)]

    def run_waker(self, command, rounds=1):
        stop, sleeps = threading.Event(), []
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= rounds: stop.set()
        inbox.waker_until(stop, command=command, sleep=sleep)
        return sleeps

    def test_the_waker_runs_only_when_switched_on_and_queues_what_it_prints(self):
        command = self.script('echo "board frage 1022-z neu"')
        self.run_waker(command)
        self.assertEqual(inbox.status()['pending'], 0)
        inbox.set_waker(True)
        self.run_waker(command)
        self.assertEqual(inbox.status()['pending'], 1)
        thread, result = self.waiter(); thread.join(3)
        self.assertIn('board frage 1022-z neu', result['text'])

    def test_a_waker_that_ends_silently_backs_off(self):
        inbox.set_waker(True)
        sleeps = self.run_waker(self.script('echo "läuft schon" >&2'), rounds=3)
        self.assertEqual(inbox.status()['pending'], 0)
        self.assertTrue(all(s >= inbox.WAKER_BACKOFF for s in sleeps))


class ServiceTests(test_conductor.ServeBase):
    def test_the_conductor_child_carries_the_mark_and_pool_agents_do_not(self):
        children = self.serve(([b'Connected \xc2\xb7 Capacity: 0/32\r\n'], 0), env=test_conductor.ENV_WITH_ALL)
        self.assertEqual(children[0].env[inbox.MARK], '1')
        self.assertNotIn(inbox.MARK, os.environ)


class CliTests(InboxBase):
    def test_add_and_status_from_the_command_line(self):
        with mock.patch('sys.stdout', new=io.StringIO()) as out:
            self.assertEqual(conductor_tools.cli(['inbox', 'add', 'Probe', 'von', 'testaudit']), 0)
            self.assertEqual(conductor_tools.cli(['inbox', 'status', '--json']), 0)
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1])['pending'], 1)


if __name__ == '__main__':
    unittest.main()
