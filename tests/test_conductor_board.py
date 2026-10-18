"""Conductor Board facts in `claude-pool conductor snapshot --json`: machines, accounts, quick links and the task queue,
plus `conductor tell --list | --cancel`. Fake herdr, preview, gh, git, ssh and service managers on a temp PATH
(`<fake>:/usr/bin:/bin`); other devices, the pool overview and the account files are injected fakes."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import conductor_board as cb
import conductor_tools as ct
from device_report import Step
import registry
from remote import Completed
import settings
from fakes import fixture_token
from test_conductor_tools import ToolsBase, base_state

NOW = 1_800_000_000.0
WANTED = 'src-1'
MAC = {'name': 'mac', 'platform': 'darwin', 'roles': [], 'addedAt': 1}
VPS = {'name': 'vps', 'ssh': 'alex@vps', 'platform': 'linux', 'roles': ['usage'], 'addedAt': 2}
PI = {'name': 'pi', 'ssh': 'alex@pi', 'platform': 'linux', 'roles': [], 'addedAt': 3}
MACHINE_KEYS = {'name', 'platform', 'self', 'reachable', 'poolRunning', 'versionMatches', 'light', 'code', 'reason', 'fix', 'checkedAt'}
CODE = r'^(ok|unreachable|pool-stopped|version-mismatch|never-checked|not-checkable|(failed|missing|manual):[a-z-]+)$'
ACCOUNT_KEYS = {'name', 'label', 'alias', 'registryName', 'fiveHourPct', 'weekPct', 'fableWeekPct', 'resetsAt', 'reserveAt', 'kind',
                'expiresAt', 'status'}

OVERVIEW = {
    'accounts': [
        {'name': 'team', 'label': 'AI Værk Team', 'status': 'ready', 'reserveAt': 85, 'seenAt': 1_799_999_990_000,
         'fiveHour': {'pct': 42.4, 'resetsAt': 1_800_009_000_000}, 'sevenDay': {'pct': 61.0, 'resetsAt': 1_800_400_000_000},
         'fableWeekly': {'pct': 12.0, 'resetsAt': None}},
        {'name': 'personal', 'label': 'Personal', 'status': 'limited', 'seenAt': 1_799_999_990_000,
         'fiveHour': {'pct': 100.0, 'resetsAt': 1_800_001_000_000}, 'sevenDay': None, 'fableWeekly': None}],
    'sessions': [{'account': 'team', 'busy': True}, {'account': 'team', 'busy': False}, {'account': 'personal', 'busy': True}],
    'health': {'activeRequests': 1}}
ROWS = [{'name': 'team', 'kind': 'long-lived', 'servingKind': 'long-lived', 'expiresAt': 1_830_000_000_000, 'active': True,
         'userId': 'user-a', 'organizationId': 'team-a', 'fingerprint': 'f' * 12},
        {'name': 'personal', 'kind': 'refreshable', 'servingKind': 'refreshable', 'expiresAt': 4_000_000_000_000, 'active': True,
         'userId': 'user-p', 'organizationId': 'org-p', 'fingerprint': 'e' * 12}]
CONFIG = {'accounts': [{'name': 'team', 'label': 'AI Værk Team', 'alias': 'team', 'reserveAt': 85},
                       {'name': 'personal', 'label': 'Personal', 'alias': 'p'}]}
REGISTRY_ACCOUNTS = [{'name': 'ai-vaerk-team-premium', 'label': 'AI Værk Team', 'userId': 'user-a', 'organizationId': 'team-a'},
                     {'name': 'personal', 'label': 'Personal', 'userId': 'user-p', 'organizationId': 'org-p'}]


class Peer:
    """Another device's `claude-pool device status` behind ssh; counts every call."""
    def __init__(self, service='running', version=WANTED, code=0, stderr=b''):
        self.service, self.version, self.code, self.stderr, self.calls = service, version, code, stderr, []

    def device(self, *args, stdin=None, timeout=120):
        self.calls.append((args, timeout))
        if self.code: return Completed(self.code, b'', self.stderr)
        return Completed(0, json.dumps({'name': 'x', 'version': self.version, 'service': self.service, 'accounts': []}).encode(), b'')


class BoardBase(ToolsBase):
    def setUp(self):
        super().setUp()
        self.cfg.mkdir(mode=0o700, parents=True, exist_ok=True)
        settings.CONFIG.write_text(json.dumps(CONFIG))
        self.peers = {'alex@vps': Peer(), 'alex@pi': Peer()}
        self.service = 'running'

    def devices(self, *rows, me='mac', accounts=(), conductor=None):
        def change(value):
            value['devices'].extend(dict(row) for row in rows); value['accounts'].extend(dict(row) for row in accounts)
            if conductor: value['conductor'] = conductor
        registry.update(change, settings.REGISTRY)
        registry.set_self(me, settings.SELF)

    def board(self, overview=OVERVIEW, rows=ROWS):
        def read():
            if isinstance(overview, Exception): raise overview
            return json.loads(json.dumps(overview))
        return cb.Board(remotes=lambda target: self.peers[target], service_state=lambda: self.service, version=lambda: WANTED,
                        overview=read, account_rows=lambda: [dict(row) for row in rows])

    def snap(self, now=NOW, board=None):
        return json.loads(ct.snapshot(ct.Tools(), as_json=True, now=now, queue_path=self.queue_path, board=board or self.board()))

    def machine(self, value, name): return next(row for row in value['machines'] if row['name'] == name)

    def ssh_count(self): return sum(len(peer.calls) for peer in self.peers.values())

    def checked(self, name, *steps, at=NOW):
        cb.record_checks({name: [Step(*step) for step in steps]}, now=at)


class ShapeTests(BoardBase):
    def test_old_keys_stay_and_the_board_keys_have_their_types(self):
        self.devices(MAC, VPS, conductor={'device': 'vps', 'email': 'c@x.de', 'organization': 'Team', 'cwd': '~/projects/conductor'})
        old = json.loads(ct.snapshot(ct.Tools(), as_json=True, now=NOW, queue_path=self.queue_path, board=self.board()))
        for key in ('sessions', 'previews', 'prs', 'orphaned'): self.assertIn(key, old)
        self.assertEqual(old['previews'], [{'port': 5173, 'label': 'web', 'link': 'https://example-vps.tail1.ts.net:8443/'}])
        value = self.snap()
        self.assertEqual(value['generatedAt'], int(NOW * 1000)); self.assertIsInstance(value['generatedAt'], int)
        self.assertEqual([row['name'] for row in value['machines']], ['mac', 'vps'])
        for row in value['machines']:
            self.assertEqual(set(row), MACHINE_KEYS)
            self.assertIsInstance(row['name'], str); self.assertIn(row['platform'], ('darwin', 'linux'))
            self.assertIsInstance(row['self'], bool); self.assertIn(row['light'], ('green', 'amber', 'red', 'grey'))
            self.assertRegex(row['code'], CODE)
            for key in ('reachable', 'poolRunning', 'versionMatches'): self.assertIn(type(row[key]), (bool, type(None)))
            self.assertIsInstance(row['reason'], str); self.assertIsInstance(row['fix'], str)
            self.assertIn(type(row['checkedAt']), (int, type(None)))
        self.assertTrue(self.machine(value, 'mac')['self']); self.assertFalse(self.machine(value, 'vps')['self'])
        for row in value['accounts']:
            self.assertLessEqual(ACCOUNT_KEYS, set(row))
            self.assertEqual(set(row['resetsAt']), {'fiveHour', 'week', 'fableWeek'})
            self.assertIn(row['kind'], ('long-lived', 'refreshable', None))
        self.assertIsInstance(value['quickAccess'], list); self.assertIsInstance(value['queue'], list)
        for row in value['quickAccess']: self.assertEqual(set(row), {'label', 'link', 'kind'})

    def test_herdr_down_still_reports_the_board_keys(self):
        (self.bin / 'herdr').unlink()
        self.devices(MAC, VPS)
        value = self.snap()
        self.assertEqual(value['error'], 'herdr is not available')
        self.assertEqual([row['name'] for row in value['machines']], ['mac', 'vps'])
        self.assertEqual(len(value['accounts']), 2)
        self.assertIn('herdr is not available', ct.snapshot(ct.Tools(), board=self.board()))

    def test_no_registry_means_no_machines_and_nothing_touches_ssh_or_the_service_manager(self):
        value = json.loads(ct.snapshot(ct.Tools(), as_json=True, now=NOW, queue_path=self.queue_path))  # the real Board, temp paths
        self.assertEqual(value['machines'], [])
        self.assertFalse([c for c in self.calls() if c[0] in ('ssh', 'launchctl', 'systemctl', 'claude-pool')])

    def test_text_snapshot_names_machines_and_accounts_in_a_few_lines(self):
        self.devices(MAC, VPS)
        self.peers['alex@vps'].code = 255
        text = ct.snapshot(ct.Tools(), now=NOW, queue_path=self.queue_path, board=self.board())
        self.assertIn('machines:', text); self.assertIn('vps red', text); self.assertIn('accounts:', text)
        self.assertIn('AI Værk Team', text); self.assertLessEqual(len(text.splitlines()), 60)


class LightTests(BoardBase):
    def setUp(self):
        super().setUp()
        self.devices(MAC, VPS)
        self.checked('mac', ('reachable', 'ok', 'this device'), ('service', 'ok', 'running'))
        self.checked('vps', ('reachable', 'ok', 'ssh works'), ('service', 'ok', 'running'), ('doctor', 'ok', 'no findings'))

    def test_all_good_is_green_with_no_reason(self):
        value = self.snap()
        for name in ('mac', 'vps'):
            row = self.machine(value, name)
            self.assertEqual((row['light'], row['code'], row['reason'], row['fix']), ('green', 'ok', '', ''), row)
            self.assertEqual((row['reachable'], row['poolRunning'], row['versionMatches']), (True, True, True))
            self.assertEqual(row['checkedAt'], int(NOW * 1000))

    def test_unreachable_is_red_with_the_ssh_fix(self):
        self.peers['alex@vps'].code = 255
        row = self.machine(self.snap(NOW + 700), 'vps')
        self.assertEqual((row['light'], row['code'], row['reachable']), ('red', 'unreachable', False))
        self.assertIn('not reachable', row['reason'].lower())
        self.assertEqual(row['fix'], 'ssh alex@vps true must work without a password prompt (BatchMode)')

    def test_pool_not_running_is_red(self):
        self.peers['alex@vps'].service = 'stopped'
        row = self.machine(self.snap(NOW + 700), 'vps')
        self.assertEqual((row['light'], row['code'], row['poolRunning']), ('red', 'pool-stopped', False))
        self.assertIn('pool is not running', row['reason'])
        self.assertEqual(row['fix'], 'claude-pool device add alex@vps')
        self.service = 'stopped'
        row = self.machine(self.snap(NOW + 700), 'mac')
        self.assertEqual((row['light'], row['poolRunning'], row['fix']), ('red', False, 'claude-pool --start'))

    def test_a_failed_step_is_red_and_names_its_fix(self):
        self.checked('vps', ('reachable', 'ok', 'ssh works'), ('doctor', 'failed', 'fail store mode', 'chmod 600 store'),
                     ('app', 'manual', 'add the SSH environment', 'open the Claude app'))
        row = self.machine(self.snap(), 'vps')
        self.assertEqual((row['light'], row['code']), ('red', 'failed:doctor')); self.assertIn('doctor', row['reason']); self.assertIn('fail store mode', row['reason'])
        self.assertEqual(row['fix'], 'chmod 600 store')

    def test_missing_or_manual_steps_are_amber(self):
        for status in ('missing', 'manual'):
            with self.subTest(status=status):
                self.checked('vps', ('reachable', 'ok', 'ssh works'), ('app', status, 'add the SSH environment', 'open the Claude app'))
                row = self.machine(self.snap(), 'vps')
                self.assertEqual((row['light'], row['code'], row['fix']), ('amber', status + ':app', 'open the Claude app'))
                self.assertIn('app', row['reason'])

    def test_live_facts_win_over_an_older_cached_check(self):
        self.checked('vps', ('reachable', 'unreachable', 'could not reach alex@vps over ssh', 'ssh alex@vps true'), at=NOW - 3600)
        row = self.machine(self.snap(), 'vps')
        self.assertEqual((row['light'], row['reachable']), ('green', True))

    def test_another_version_is_amber(self):
        self.peers['alex@vps'].version = 'src-0'
        row = self.machine(self.snap(NOW + 700), 'vps')
        self.assertEqual((row['light'], row['code'], row['versionMatches'], row['fix']),
                         ('amber', 'version-mismatch', False, 'claude-pool device add alex@vps'))

    def test_a_slow_answer_is_grey_not_unreachable(self):
        self.peers['alex@vps'].code, self.peers['alex@vps'].stderr = 255, b'timed out'
        row = self.machine(self.snap(NOW + 700), 'vps')
        self.assertEqual((row['light'], row['code'], row['reachable']), ('grey', 'not-checkable', None))
        self.assertIn('did not answer', row['reason'])

    def test_never_checked_is_grey_with_the_check_command(self):
        self.devices(PI)
        row = self.machine(self.snap(), 'pi')
        self.assertEqual((row['light'], row['code'], row['checkedAt'], row['fix']), ('grey', 'never-checked', None, 'claude-pool device check pi'))
        self.peers['alex@pi'].service = 'stopped'
        row = self.machine(self.snap(NOW + 700), 'pi')  # a live problem still outranks never checked
        self.assertEqual((row['light'], row['code']), ('red', 'pool-stopped'))

    def test_a_device_without_ssh_from_here_is_grey_and_never_probed(self):
        registry.set_self('vps', settings.SELF)
        (settings.RUNTIME / cb.CHECK_FILE).unlink()
        row = self.machine(self.snap(), 'mac')
        self.assertEqual((row['light'], row['code'], row['reachable']), ('grey', 'not-checkable', None))
        self.assertIn('no ssh', row['reason'].lower())
        self.assertEqual(self.ssh_count(), 0)

    def test_a_shared_result_lights_a_device_this_machine_cannot_reach(self):
        registry.set_self('vps', settings.SELF)
        (settings.RUNTIME / cb.CHECK_FILE).unlink()
        steps = [{'name': 'reachable', 'status': 'ok', 'detail': 'this device', 'fix': ''},
                 {'name': 'version', 'status': 'ok', 'detail': WANTED, 'fix': ''},
                 {'name': 'service', 'status': 'ok', 'detail': 'running', 'fix': ''},
                 {'name': 'app', 'status': 'manual', 'detail': 'add the SSH environment', 'fix': 'open the Claude app'}]
        self.assertEqual(cb.merge_checks({'devices': {'mac': {'checkedAt': int(NOW * 1000) - 5000, 'steps': steps}}}, now=NOW), ['mac'])
        row = self.machine(self.snap(), 'mac')
        self.assertEqual((row['light'], row['code'], row['reachable'], row['poolRunning'], row['versionMatches']),
                         ('amber', 'manual:app', True, True, True))
        self.assertEqual(row['checkedAt'], int(NOW * 1000) - 5000)
        steps[3]['status'] = 'ok'
        cb.merge_checks({'devices': {'mac': {'checkedAt': int(NOW * 1000), 'steps': steps}}}, now=NOW)
        row = self.machine(self.snap(), 'mac')
        self.assertEqual((row['light'], row['code']), ('green', 'ok'))
        self.assertEqual(self.ssh_count(), 0)

    def test_this_machines_own_row_stays_live_after_a_shared_result(self):
        steps = [{'name': 'reachable', 'status': 'ok', 'detail': 'ssh works', 'fix': ''},
                 {'name': 'service', 'status': 'missing', 'detail': 'service is stopped', 'fix': 'claude-pool device add mac'}]
        cb.merge_checks({'devices': {'mac': {'checkedAt': int(NOW * 1000) + 1000, 'steps': steps}}}, now=NOW)
        row = self.machine(self.snap(), 'mac')
        self.assertEqual((row['light'], row['code'], row['poolRunning']), ('green', 'ok', True))


class CacheTests(BoardBase):
    def test_at_most_one_ssh_per_ten_minutes_per_device_even_when_unreachable(self):
        self.devices(MAC, VPS, PI)
        self.peers['alex@pi'].code = 255
        for at in (NOW, NOW + 60, NOW + 300, NOW + 599):
            self.snap(at)
        self.assertEqual([len(self.peers[t].calls) for t in ('alex@vps', 'alex@pi')], [1, 1])
        self.snap(NOW + 600)
        self.assertEqual([len(self.peers[t].calls) for t in ('alex@vps', 'alex@pi')], [2, 2])
        args, timeout = self.peers['alex@vps'].calls[0]
        self.assertEqual(args, ('status',)); self.assertLessEqual(timeout, cb.PROBE_TIMEOUT)
        path = settings.RUNTIME / cb.PROBE_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_recent_device_check_counts_as_the_probe(self):
        self.devices(MAC, VPS)
        self.checked('vps', ('reachable', 'ok', 'ssh works'), ('version', 'ok', WANTED), ('service', 'ok', 'running'), at=NOW - 60)
        row = self.machine(self.snap(), 'vps')
        self.assertEqual(self.ssh_count(), 0)
        self.assertEqual((row['reachable'], row['poolRunning'], row['versionMatches'], row['light']), (True, True, True, 'green'))

    def test_check_cache_is_private_redacted_and_a_skipped_device_keeps_its_last_result(self):
        token = fixture_token()
        cb.record_checks({'vps': [Step('doctor', 'failed', 'leaked ' + token, 'fix ' + token)]}, now=NOW)
        cb.record_checks({'vps': [Step('reachable', 'skipped', 'no ssh target from this device')]}, now=NOW + 5)
        path = settings.RUNTIME / cb.CHECK_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = path.read_text(); self.assertNotIn(token, text)
        entry = json.loads(text)['devices']['vps']
        self.assertEqual(entry['checkedAt'], int(NOW * 1000)); self.assertEqual(entry['steps'][0]['status'], 'failed')

    def test_merge_keeps_the_newest_result_per_device_and_refuses_bad_input(self):
        self.devices(MAC, VPS)
        token = fixture_token()
        old = {'checkedAt': 1000, 'steps': [{'name': 'doctor', 'status': 'failed', 'detail': 'x', 'fix': 'y'}]}
        new = {'checkedAt': 2000, 'steps': [{'name': 'doctor', 'status': 'ok', 'detail': 'no findings ' + token, 'fix': ''}]}
        self.assertEqual(cb.merge_checks({'devices': {'vps': new}}), ['vps'])
        self.assertEqual(cb.merge_checks({'devices': {'vps': old}}), [])
        path = settings.RUNTIME / cb.CHECK_FILE
        entry = json.loads(path.read_text())['devices']['vps']
        self.assertEqual((entry['checkedAt'], entry['steps'][0]['status']), (2000, 'ok'))
        self.assertNotIn(token, path.read_text()); self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for bad in ({}, {'devices': []}, {'devices': {'Bad Name': new}}, {'devices': {'vps': {'checkedAt': 'x', 'steps': []}}},
                    {'devices': {'vps': {'checkedAt': 3000, 'steps': [{'name': 'doctor', 'status': 'great', 'detail': '', 'fix': ''}]}}},
                    {'devices': {'vps': {'checkedAt': 3000, 'steps': 'no'}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError): cb.merge_checks(bad)
        self.assertEqual(json.loads(path.read_text())['devices']['vps']['checkedAt'], 2000)

    def test_share_sends_the_cache_to_every_reachable_peer_once(self):
        self.devices(MAC, VPS, PI)
        self.checked('mac', ('reachable', 'ok', 'this device'))
        self.checked('vps', ('reachable', 'ok', 'ssh works'))
        sent = []
        class Receiver(Peer):
            def device(peer, *args, stdin=None, timeout=120):
                sent.append((args, json.loads(stdin), timeout)); return Completed(0, b'{}', b'')
        self.peers = {'alex@vps': Receiver(), 'alex@pi': Receiver()}
        results = cb.share_checks(lambda target: self.peers[target], skip=('pi',))
        self.assertEqual(results, {'vps': 'ok'})
        (args, payload, timeout), = sent
        self.assertEqual(args, ('receive-checks',)); self.assertEqual(set(payload['devices']), {'mac', 'vps'})
        self.assertLessEqual(timeout, cb.PROBE_TIMEOUT)
        self.peers['alex@vps'] = Peer(code=255)
        self.assertEqual(cb.share_checks(lambda target: self.peers[target]), {'vps': 'unreachable', 'pi': 'ok'})

    def test_budget_under_five_seconds_with_cached_machines(self):
        self.devices(MAC, VPS, PI)
        self.snap(NOW)  # fills the probe cache
        before = self.ssh_count()
        started = time.monotonic()
        self.snap(NOW + 60)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.ssh_count(), before)


class QuickAccessTests(BoardBase):
    PREVIEWS = [{'port': 5173, 'label': 'web', 'link': 'https://vps.tail1.ts.net:5173/', 'backend': 'https'},
                {'port': 18501, 'label': 'desk', 'link': 'https://vps.tail1.ts.net:18501/', 'kind': 'cua-space'},
                {'port': 18500, 'label': 'Cua desktops', 'link': 'https://vps.tail1.ts.net:18500/', 'kind': 'cua'},
                {'port': 18502, 'label': 'odd', 'link': 'https://vps.tail1.ts.net:18502/'},
                {'port': 8820, 'label': 'Board', 'link': 'https://vps.tail1.ts.net:8820/', 'kind': 'file', 'path': '/x'}]

    def test_cua_first_then_previews_never_space_links_then_the_app(self):
        self.set_state({**base_state(), 'previews': self.PREVIEWS})
        self.devices(MAC, VPS, conductor={'device': 'vps', 'email': 'c@x.de', 'organization': 'Team', 'cwd': '~/projects/conductor'})
        rows = self.snap()['quickAccess']
        self.assertEqual(rows, [{'label': 'Cua desktops', 'link': 'https://vps.tail1.ts.net:18500/', 'kind': 'cua'},
                                {'label': 'web', 'link': 'https://vps.tail1.ts.net:5173/', 'kind': 'preview'},
                                {'label': 'Board', 'link': 'https://vps.tail1.ts.net:8820/', 'kind': 'preview'},
                                {'label': 'Conductor in der Claude-App', 'link': 'https://claude.ai/code', 'kind': 'app'}])
        self.assertNotIn(':185', ' '.join(row['link'] for row in rows if row['kind'] != 'cua'))

    def test_no_conductor_no_app_link(self):
        self.devices(MAC, VPS)
        self.assertNotIn('app', [row['kind'] for row in self.snap()['quickAccess']])


class AccountTests(BoardBase):
    def setUp(self):
        super().setUp()
        self.devices(MAC, VPS, accounts=REGISTRY_ACCOUNTS)

    def test_accounts_come_from_the_usage_overview(self):
        team, personal = self.snap()['accounts']
        self.assertEqual(team, {'name': 'team', 'label': 'AI Værk Team', 'alias': 'team', 'registryName': 'ai-vaerk-team-premium',
                                'fiveHourPct': 42.4, 'weekPct': 61.0, 'fableWeekPct': 12.0,
                                'resetsAt': {'fiveHour': 1_800_009_000_000, 'week': 1_800_400_000_000, 'fableWeek': None},
                                'reserveAt': 85, 'kind': 'long-lived', 'expiresAt': 1_830_000_000_000, 'status': 'ready', 'agents': 2})
        self.assertEqual(personal, {'name': 'personal', 'label': 'Personal', 'alias': 'p', 'registryName': None,
                                    'fiveHourPct': 100.0, 'weekPct': None, 'fableWeekPct': None,
                                    'resetsAt': {'fiveHour': 1_800_001_000_000, 'week': None, 'fableWeek': None},
                                    'reserveAt': None, 'kind': 'refreshable', 'expiresAt': None, 'status': 'limited', 'agents': 1})

    def test_a_stopped_pool_still_lists_the_accounts_without_numbers(self):
        rows = self.snap(board=self.board(overview=ConnectionRefusedError()))['accounts']
        self.assertEqual([row['name'] for row in rows], ['team', 'personal'])
        team = rows[0]
        self.assertEqual((team['status'], team['fiveHourPct'], team['reserveAt'], team['kind']), ('unknown', None, 85, 'long-lived'))
        self.assertNotIn('agents', team)

    def test_accounts_never_carry_a_token_or_fingerprint(self):
        text = json.dumps(self.snap()['accounts'])
        self.assertNotIn('f' * 12, text); self.assertNotIn('user-a', text); self.assertNotIn('sk-', text)


class QueueTests(BoardBase):
    def queue(self, *items):
        self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        ct._write(self.queue_path, {'items': list(items)})

    def test_a_queued_task_gets_a_short_id_and_shows_in_snapshot_and_list(self):
        code, line = ct.tell('reviewer', 'Review PR 12 ' + 'x' * 300, tools=ct.Tools(), queue_path=self.queue_path, now=NOW)
        self.assertEqual(code, 0)
        item = json.loads(self.queue_path.read_text())['items'][0]
        self.assertRegex(item['id'], r'^[0-9a-f]{6}$')
        rows = self.snap(NOW + 60)['queue']
        self.assertEqual(rows, [{'id': item['id'], 'target': 'dashboard/reviewer', 'text': ('Review PR 12 ' + 'x' * 300)[:200],
                                 'queuedAt': int(NOW * 1000), 'attempts': 0}])
        self.assertEqual(json.loads(ct.tell_list(self.queue_path, as_json=True, now=NOW + 60)), rows)
        text = ct.tell_list(self.queue_path, now=NOW + 60)
        self.assertIn(item['id'], text); self.assertIn('dashboard/reviewer', text); self.assertIn('1m', text)

    def test_an_old_item_without_id_gets_one_on_first_read_and_keeps_it(self):
        self.queue({'session': 'dashboard', 'pane': 'w1:p1', 'agent': 'reviewer', 'task': 'Old', 'at': NOW, 'attempts': 2})
        first = json.loads(ct.tell_list(self.queue_path, as_json=True))
        self.assertRegex(first[0]['id'], r'^[0-9a-f]{6}$'); self.assertEqual(first[0]['attempts'], 2)
        self.assertEqual(json.loads(ct.tell_list(self.queue_path, as_json=True))[0]['id'], first[0]['id'])
        self.assertEqual(json.loads(self.queue_path.read_text())['items'][0]['id'], first[0]['id'])

    def test_events_still_deliver_an_old_item_without_id(self):
        self.tick(1000, first=True)
        self.queue({'session': 'dashboard', 'pane': 'w2:p1', 'agent': 'pooler', 'task': 'Old task', 'at': 990})
        self.assertIn('conductor-event delivered dashboard/pooler Old task', self.tick(1020))
        self.assertEqual(json.loads(self.queue_path.read_text())['items'], [])

    def test_cancel_removes_one_task_and_unknown_ids_fail(self):
        self.queue({'id': 'aaa111', 'session': 'dashboard', 'pane': 'w1:p1', 'agent': 'reviewer', 'task': 'One', 'at': NOW},
                   {'id': 'bbb222', 'session': 'dashboard', 'pane': 'w1:p1', 'agent': 'reviewer', 'task': 'Two', 'at': NOW})
        code, line = ct.tell_cancel('aaa111', self.queue_path)
        self.assertEqual(code, 0); self.assertIn('aaa111', line); self.assertIn('dashboard/reviewer', line); self.assertIn('One', line)
        self.assertEqual([i['id'] for i in json.loads(self.queue_path.read_text())['items']], ['bbb222'])
        self.assertEqual(stat.S_IMODE(self.queue_path.stat().st_mode), 0o600)
        code, line = ct.tell_cancel('nope', self.queue_path)
        self.assertEqual(code, 1); self.assertIn('no queued task with id nope', line)
        self.assertEqual(len(json.loads(self.queue_path.read_text())['items']), 1)

    def test_an_empty_queue_lists_plainly(self):
        self.assertEqual(ct.tell_list(self.queue_path), 'no queued tasks')
        self.assertEqual(json.loads(ct.tell_list(self.queue_path, as_json=True)), [])
        self.assertFalse(self.queue_path.exists())

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ct, 'QUEUE', self.queue_path), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try: code = ct.cli(list(argv))
            except SystemExit as done: code = done.code
        return code, out.getvalue(), err.getvalue()

    def test_cli_list_and_cancel(self):
        self.queue({'id': 'aaa111', 'session': 'dashboard', 'pane': 'w1:p1', 'agent': 'reviewer', 'task': 'One', 'at': NOW})
        code, out, err = self.run_cli('tell', '--list', '--json')
        self.assertEqual(code, 0, err); self.assertEqual(json.loads(out)[0]['id'], 'aaa111')
        code, out, err = self.run_cli('tell', '--list')
        self.assertEqual(code, 0, err); self.assertIn('aaa111', out)
        code, out, err = self.run_cli('tell', '--cancel', 'zzz')
        self.assertEqual(code, 1); self.assertIn('no queued task with id zzz', out)
        code, out, err = self.run_cli('tell', '--cancel', 'aaa111')
        self.assertEqual(code, 0, err); self.assertIn('cancelled aaa111', out)
        for argv in (['tell'], ['tell', 'reviewer'], ['tell', '--list', 'reviewer'], ['tell', '--json', 'reviewer', 'x'],
                     ['tell', '--list', '--cancel', 'aaa111']):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(*argv)[0], 2)

    def test_help_names_list_and_cancel(self):
        code, out, err = self.run_cli('tell', '--help')
        self.assertIn('--list', out); self.assertIn('--cancel', out)
        docs = (ROOT / 'docs/DEVICES.md').read_text()
        for words in ('tell --list [--json]', 'tell --cancel <id>', '`machines`', '`accounts`', '`quickAccess`', '`queue`', '`generatedAt`',
                      '`queued for <session>/<agent> (id <id>)`', '`grey`', '`never-checked`', '`not-checkable`', '`manual:<step>`',
                      '`device receive-checks`'):
            self.assertIn(words, docs)


class HardeningTests(BoardBase):
    """Review of 8c748c5: shared check results cannot poison the board, and one broken part never blanks the others."""
    DAY_MS = 86_400_000

    def entry(self, at, status='ok'):
        return {'checkedAt': at, 'steps': [{'name': 'doctor', 'status': status, 'detail': 'd', 'fix': ''}]}

    def test_checked_at_is_capped_to_one_day_ahead_and_absurd_values_are_refused(self):
        self.devices(MAC, VPS)
        now_ms = int(NOW * 1000)
        path = settings.RUNTIME / cb.CHECK_FILE
        for absurd in (10 ** 330, 2 ** 63, 4 * 10 ** 18):
            with self.subTest(absurd=absurd), self.assertRaises(ValueError): cb.merge_checks({'devices': {'vps': self.entry(absurd)}}, now=NOW)
        self.assertFalse(path.exists(), 'nothing is written for refused input')
        for raw in (b'{"devices": {"vps": {"checkedAt": Infinity, "steps": []}}}', b'{"devices": {"vps": {"checkedAt": NaN, "steps": []}}}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError): cb.merge_checks(json.loads(raw), now=NOW)
        # A far-future but finite value is kept at most one day ahead, so a later genuine result still replaces it.
        self.assertEqual(cb.merge_checks({'devices': {'vps': self.entry(now_ms + 30 * self.DAY_MS, 'failed')}}, now=NOW), ['vps'])
        self.assertEqual(json.loads(path.read_text())['devices']['vps']['checkedAt'], now_ms + self.DAY_MS)
        self.assertEqual(cb.merge_checks({'devices': {'vps': self.entry(now_ms + 2 * self.DAY_MS)}}, now=NOW + 2 * 86400), ['vps'])
        value = self.snap(NOW + 2 * 86400)
        self.assertEqual([row['name'] for row in value['machines']], ['mac', 'vps'])
        self.assertEqual(len(value['accounts']), 2)

    def test_an_oversized_checked_at_already_in_the_cache_never_blanks_the_board(self):
        self.devices(MAC, VPS)
        self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        steps = [{'name': name, 'status': 'ok', 'detail': '', 'fix': ''} for name in ('reachable', 'service', 'version')]
        (settings.RUNTIME / cb.CHECK_FILE).write_text(json.dumps({'devices': {'vps': {'checkedAt': 10 ** 330, 'steps': steps}}}))
        (settings.RUNTIME / cb.CHECK_FILE).chmod(0o600)
        value = self.snap()
        self.assertEqual(len(value['accounts']), 2, 'accounts survive a broken machines part')
        self.assertIsInstance(value['quickAccess'], list)

    def test_one_failing_part_does_not_blank_the_others(self):
        self.devices(MAC, VPS, conductor={'device': 'vps', 'email': 'c@x.de', 'organization': 'Team', 'cwd': '~/projects/conductor'})
        board = self.board()
        board.machines = mock.Mock(side_effect=RuntimeError('probe thread died'))
        value = self.snap(board=board)
        self.assertEqual(value['machines'], [])
        self.assertEqual(len(value['accounts']), 2)
        self.assertIn('app', [row['kind'] for row in value['quickAccess']])
        board = self.board()
        board.accounts = mock.Mock(side_effect=RuntimeError('overview broke'))
        value = self.snap(board=board)
        self.assertEqual(value['accounts'], []); self.assertEqual([row['name'] for row in value['machines']], ['mac', 'vps'])
        with mock.patch.object(cb, 'quick_access', side_effect=RuntimeError('preview list broke')):
            value = self.snap()
        self.assertEqual(value['quickAccess'], []); self.assertEqual(len(value['accounts']), 2)
        self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        ct._write(self.queue_path, {'items': [{'id': 'aaa111', 'session': 'dashboard', 'pane': 'w1:p1', 'agent': 'reviewer', 'task': 'Good', 'at': NOW},
                                              {'id': 'bbb222', 'session': 'dashboard', 'pane': 'w1:p1', 'task': 'Bad', 'at': 'yesterday'}]})
        value = self.snap()
        self.assertEqual([row['id'] for row in value['queue']], ['aaa111'], 'one bad queue item drops only itself')
        self.assertEqual([row['name'] for row in value['machines']], ['mac', 'vps']); self.assertEqual(len(value['accounts']), 2)

    def test_receive_checks_takes_only_registered_devices_and_prunes_the_rest(self):
        self.devices(MAC, VPS)
        cb.record_checks({'gone': [Step('doctor', 'ok', 'fine')]}, now=NOW)  # a device removed from the registry since
        merged = cb.merge_checks({'devices': {'vps': self.entry(int(NOW * 1000)), 'ghost': self.entry(int(NOW * 1000))}}, now=NOW)
        self.assertEqual(merged, ['vps'])
        self.assertEqual(set(json.loads((settings.RUNTIME / cb.CHECK_FILE).read_text())['devices']), {'vps'})
        # Many unknown names never grow the cache.
        flood = {'devices': {'ghost-' + str(n): self.entry(int(NOW * 1000)) for n in range(64)}}
        self.assertEqual(cb.merge_checks(flood, now=NOW), [])
        self.assertEqual(set(json.loads((settings.RUNTIME / cb.CHECK_FILE).read_text())['devices']), {'vps'})

    def test_a_failing_service_state_for_this_machine_is_grey_never_green(self):
        self.devices(MAC, VPS)
        def broken(): raise RuntimeError('launchctl unavailable')
        board = cb.Board(remotes=lambda target: self.peers[target], service_state=broken, version=lambda: WANTED,
                         overview=lambda: json.loads(json.dumps(OVERVIEW)), account_rows=lambda: [dict(row) for row in ROWS])
        row = self.machine(self.snap(board=board), 'mac')
        self.assertEqual((row['light'], row['code'], row['poolRunning']), ('grey', 'never-checked', None))
        self.checked('mac', ('reachable', 'ok', 'this device'), ('service', 'ok', 'running'))
        row = self.machine(self.snap(board=board), 'mac')
        self.assertEqual((row['light'], row['code'], row['poolRunning']), ('grey', 'pool-stopped', None))
        self.assertTrue(row['reason']); self.assertTrue(row['fix'])

    def test_tell_list_survives_a_corrupt_or_oversized_queue_file(self):
        self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        for content in ('{not json', '[1, 2]', json.dumps({'items': [{'id': 'x' * 10, 'task': 'y' * 100}] * 20000})):
            with self.subTest(size=len(content)):
                self.queue_path.write_text(content); self.queue_path.chmod(0o600)
                text = ct.tell_list(self.queue_path)
                self.assertIn('could not be read', text)
                self.assertEqual(json.loads(ct.tell_list(self.queue_path, as_json=True)), [])
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err): code = ct.cli(['tell', '--list'])
                self.assertEqual(code, 0, err.getvalue()); self.assertIn('could not be read', out.getvalue())
                self.assertEqual(self.queue_path.read_text(), content, 'an unreadable queue is never rewritten by a read')


if __name__ == '__main__': unittest.main()
