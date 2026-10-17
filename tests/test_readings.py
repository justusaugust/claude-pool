"""Shared usage readings: export, merge, pull. Offline, fakes only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import diagnostics
import readings
import registry
import settings
import views
from bridge import PoolBridge
from private_files import write_private
from remote import Completed
from fakes import FakeRemote

NOW = 1_900_000_000_000
W = lambda pct: {'pct': pct, 'resetsAt': NOW + 3_600_000}
OVERVIEW = {'accounts': [{'name': 'team', 'label': 'Team', 'email': 'team@example.org', 'sessionKey': 'abc', 'status': 'ready',
                          'fiveHour': W(40), 'sevenDay': None, 'fableWeekly': W(10), 'seenAt': NOW - 1000}], 'sessions': [{'id': 's1'}]}
LOCAL_OLD = {'name': 'team', 'status': 'limited', 'paused': False, 'quotaLimits': {'fiveHour': {'status': 'rejected'}},
             'fiveHour': W(97), 'sevenDay': W(50), 'fableWeekly': None, 'seenAt': NOW - 600_000}
LOCAL_NO_7D = {**LOCAL_OLD, 'sevenDay': None}


def shared(at=NOW - 1000, seen=NOW - 1000, seven=None, device='vps'):
    return {'version': 1, 'device': device, 'at': at, 'accounts': {'team': {'fiveHour': W(20), 'sevenDay': seven, 'fableWeekly': W(5), 'seenAt': seen}}}


class MergeTests(unittest.TestCase):
    def test_export_contains_only_quota_fields(self):
        out = readings.export(OVERVIEW, 'vps', NOW)
        self.assertEqual(set(out['accounts']['team']), {'fiveHour', 'sevenDay', 'fableWeekly', 'seenAt'})
        self.assertNotIn('@', json.dumps(out)); self.assertNotIn('abc', json.dumps(out))
        self.assertEqual(set(out), {'version', 'device', 'at', 'accounts'})

    def test_merge_prefers_the_newer_reading_and_keeps_status(self):
        new = shared()
        row = readings.merge([dict(LOCAL_OLD)], new, NOW)[0]
        self.assertEqual(row['fiveHour'], new['accounts']['team']['fiveHour'])
        self.assertEqual(row['status'], LOCAL_OLD['status']); self.assertEqual(row['quotaLimits'], LOCAL_OLD['quotaLimits'])
        self.assertFalse(row['paused']); self.assertEqual(row['readingSource'], 'shared:vps')

    def test_older_shared_reading_does_not_win(self):
        row = readings.merge([dict(LOCAL_OLD)], shared(seen=NOW - 700_000), NOW)[0]
        self.assertEqual(row['fiveHour'], LOCAL_OLD['fiveHour']); self.assertNotIn('readingSource', row)

    def test_missing_seven_day_window_stays_not_reported(self):
        self.assertIsNone(readings.merge([dict(LOCAL_NO_7D)], shared(), NOW)[0]['sevenDay'])

    def test_unknown_accounts_in_shared_file_add_no_rows(self):
        data = shared(); data['accounts']['other'] = data['accounts']['team']
        self.assertEqual(len(readings.merge([dict(LOCAL_OLD)], data, NOW)), 1)

    def test_stale_or_malformed_shared_file_is_ignored(self):
        stale = shared(at=NOW - 2_000_000)
        self.assertEqual(readings.merge([dict(LOCAL_OLD)], stale, NOW), [LOCAL_OLD])
        self.assertEqual(readings.merge([dict(LOCAL_OLD)], {'version': 1}, NOW), [LOCAL_OLD])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'shared.json'
            write_private(path, 'not json'); self.assertIsNone(readings.load_shared(path))
            write_private(path, json.dumps({**shared(), 'extra': 1})); self.assertIsNone(readings.load_shared(path))
            write_private(path, json.dumps(shared())); self.assertEqual(readings.load_shared(path)['device'], 'vps')
            path.chmod(0o644); self.assertIsNone(readings.load_shared(path))
            self.assertIsNone(readings.load_shared(Path(tmp) / 'missing.json'))

    def test_importing_readings_does_not_import_device_sync(self):
        code = 'import sys; sys.path.insert(0, %r); import readings; print("device_sync" in sys.modules)' % str(Path(__file__).resolve().parents[1] / 'gateway')
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(out.stdout.strip(), 'False', out.stderr)

    def test_usage_names_the_reading_source(self):
        data = {'ready': 1, 'updatedAt': NOW, 'accounts': [{'label': 'Team', 'status': 'ready', 'plan': 'Team', 'seenAt': NOW, 'fiveHour': W(20),
                'sevenDay': None, 'readingSource': 'shared:vps'}],
                'forecast': {'seconds': None, 'eligibleAccounts': 0, 'estimatedAccounts': 0, 'partial': False, 'threshold': 95, 'nextReset': None}, 'history': []}
        self.assertIn('reading from vps', views.usage(data))


class Journal:
    def __init__(self): self.codes = []
    def record(self, code, **details): self.codes.append(code)


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfg = root / 'cfg'; self.cfg.mkdir(mode=0o700)
        self.reg, self.me, self.path = self.cfg / 'devices.json', self.cfg / 'device.json', root / 'shared.json'
        for key, value in [('REGISTRY', self.reg), ('SELF', self.me)]:
            item = patch.object(settings, key, value); item.start(); self.addCleanup(item.stop)
        registry.set_self('mac', self.me)
        self.journal, self.reply, self.remotes = Journal(), None, {}
        self.owner_row = {'name': 'vps', 'ssh': 'justus@vps', 'platform': 'linux', 'roles': ['usage'], 'addedAt': 1}

    def add(self, *rows): registry.update(lambda v: v['devices'].extend(rows), self.reg)

    def make(self, handler=None):
        handler = handler or (lambda argv, stdin: Completed(0, json.dumps(shared()).encode(), b''))
        def remotes(target):
            self.remotes[target] = self.remotes.get(target) or FakeRemote(target, handler)
            return self.remotes[target]
        return readings.Puller(self.path, self.journal, remotes)

    def test_successful_pull_writes_a_private_file(self):
        self.add({'name': 'mac', 'ssh': None, 'platform': 'darwin', 'roles': [], 'addedAt': 0}, self.owner_row)
        puller = self.make()
        self.assertTrue(puller.run_once())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.remotes['justus@vps'].calls[0][0], ['.local/bin/claude-pool', 'device', 'readings'])
        self.assertEqual(readings.load_shared(self.path)['device'], 'vps')

    def test_pull_failure_records_an_incident_once_and_overview_still_answers(self):
        self.add({'name': 'mac', 'ssh': None, 'platform': 'darwin', 'roles': [], 'addedAt': 0}, self.owner_row)
        puller = self.make(lambda argv, stdin: Completed(255, b'', b'boom'))
        self.assertFalse(puller.run_once()); self.assertFalse(puller.run_once())
        self.assertEqual(self.journal.codes, ['readings_sync_failed'])
        self.assertIn('readings_sync_failed', diagnostics.CODES)
        self.assertFalse(self.path.exists())
        self.assertIn('accounts', self.fake_bridge_overview())

    def fake_bridge_overview(self):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                rows = [{'name': 'team', 'account_ref': 'team', 'has_state': True, 'observed_at_5h': 1900000000, 'utilization_5h': .2, 'reset_5h': 2000000000}]
                body = json.dumps({'providers': [{'provider': 'anthropic', 'auth': 'claude_oauth', 'accounts': rows}]}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        bridge = PoolBridge(('127.0.0.1', 0), upstream.server_port, 'c', 'a', Path(self.tmp.name) / 'state.json', shared_readings=self.path)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try: return bridge.overview()
        finally:
            bridge.monitor_stop.set(); bridge.server_close(); upstream.shutdown(); upstream.server_close()

    def test_bridge_merges_a_newer_shared_reading(self):
        write_private(self.path, json.dumps(shared(at=int(__import__('time').time() * 1000), seen=1_999_999_999_000)))
        row = self.fake_bridge_overview()['accounts'][0]
        self.assertEqual(row['readingSource'], 'shared:vps'); self.assertEqual(row['fiveHour']['pct'], 20)

    def test_owner_and_unregistered_devices_pull_nothing(self):
        self.add({'name': 'mac', 'ssh': None, 'platform': 'darwin', 'roles': ['usage'], 'addedAt': 0}, {**self.owner_row, 'roles': []})
        puller = self.make()
        self.assertFalse(puller.run_once()); self.assertEqual(self.remotes, {}); self.assertFalse(self.path.exists())
        self.me.unlink(); registry.update(lambda v: v['devices'][0].update(roles=[]) or v['devices'][1].update(roles=['usage']), self.reg)
        self.assertFalse(puller.run_once()); self.assertEqual(self.remotes, {}); self.assertEqual(self.journal.codes, [])

    def test_puller_picks_up_a_new_owner_without_restart(self):
        self.add({'name': 'mac', 'ssh': None, 'platform': 'darwin', 'roles': [], 'addedAt': 0})
        puller = self.make()
        self.assertFalse(puller.run_once())
        self.add({**self.owner_row})
        self.assertTrue(puller.run_once())

    def test_every_third_success_also_syncs_the_registry(self):
        self.add({'name': 'mac', 'ssh': None, 'platform': 'darwin', 'roles': [], 'addedAt': 0}, self.owner_row)
        puller = self.make()
        with patch('device_sync.pull', return_value='ok') as pull:
            for _ in range(3): puller.run_once()
        self.assertEqual(pull.call_count, 1)

    def test_start_stops_when_the_event_is_set(self):
        stop = threading.Event(); stop.set()
        self.make().start(stop).join(5)


if __name__ == '__main__': unittest.main()
