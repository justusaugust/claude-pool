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

    def test_export_clamps_and_round_trips_through_check(self):
        over = {'accounts': [{'name': 'team', 'fiveHour': W(101.5), 'sevenDay': {'pct': float('nan'), 'resetsAt': None}, 'fableWeekly': None, 'seenAt': NOW},
                             {'name': 'b', 'fiveHour': W(5), 'sevenDay': None, 'fableWeekly': None, 'seenAt': NOW}]}
        out = readings.check(readings.export(over, 'vps', NOW))
        self.assertEqual(out['accounts']['team']['fiveHour']['pct'], 100)
        self.assertIsNone(out['accounts']['team']['sevenDay']); self.assertIn('b', out['accounts'])

    def test_check_nulls_one_bad_window_not_the_file(self):
        data = shared(); data['accounts']['team']['fiveHour'] = {'pct': 500, 'resetsAt': None}
        out = readings.check(data)
        self.assertIsNone(out['accounts']['team']['fiveHour']); self.assertEqual(out['accounts']['team']['fableWeekly']['pct'], 5)

    def test_export_accepts_a_float_seen_at(self):
        over = {'accounts': [{'name': 'team', 'fiveHour': None, 'seenAt': 1234.7}]}
        self.assertEqual(readings.export(over, 'vps', NOW)['accounts']['team']['seenAt'], 1234)

    def test_entry_seen_at_after_file_time_is_clamped(self):
        out = readings.check(shared(at=NOW - 5000, seen=NOW + 10_000_000))
        self.assertEqual(out['accounts']['team']['seenAt'], NOW - 5000)

    def test_small_clock_skew_is_accepted_large_is_not(self):
        self.assertEqual(readings.merge([dict(LOCAL_OLD)], shared(at=NOW + 50_000, seen=NOW + 50_000), NOW)[0].get('readingSource'), 'shared:vps')
        self.assertEqual(readings.merge([dict(LOCAL_OLD)], shared(at=NOW + 70_000, seen=NOW + 70_000), NOW), [LOCAL_OLD])

    def test_reset_is_an_integer_or_none(self):
        data = shared(); data['accounts']['team']['fiveHour'] = {'pct': 3, 'resetsAt': 1.5e12 + 0.5}
        data['accounts']['team']['sevenDay'] = {'pct': 3, 'resetsAt': -4.0}
        out = readings.check(data)['accounts']['team']
        self.assertIs(type(out['fiveHour']['resetsAt']), int); self.assertIsNone(out['sevenDay']['resetsAt'])

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
    def __init__(self): self.codes, self.rows = [], []
    def record(self, code, **details): self.codes.append(code); self.rows.append((code, details))


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
        self.assertEqual(self.journal.rows[0][1], {'reason': 'pull failed'})
        self.assertIn('readings_sync_failed', diagnostics.CODES)
        self.assertFalse(self.path.exists())
        overview = self.fake_bridge_overview()
        self.assertIn('accounts', overview); self.assertNotEqual(overview['accounts'][0]['status'], 'unknown')
        self.assertNotEqual(self.last_bridge.health['backend'], 'unresponsive')

    def fake_bridge_overview(self):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                rows = [{'name': 'team', 'account_ref': 'team', 'has_state': True, 'observed_at_5h': 1000000000, 'utilization_5h': .2, 'reset_5h': 2000000000}]
                body = json.dumps({'providers': [{'provider': 'anthropic', 'auth': 'claude_oauth', 'accounts': rows}]}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        bridge = PoolBridge(('127.0.0.1', 0), upstream.server_port, 'c', 'a', Path(self.tmp.name) / 'state.json', shared_readings=self.path)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.last_bridge = bridge
        try: return bridge.overview()
        finally:
            bridge.monitor_stop.set(); bridge.server_close(); upstream.shutdown(); upstream.server_close()

    def test_bridge_merges_a_newer_shared_reading(self):
        write_private(self.path, json.dumps(shared(at=int(__import__('time').time() * 1000), seen=int(__import__('time').time() * 1000))))
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
        thread = self.make().start(stop); thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_loop_survives_a_raising_run_once(self):
        puller = self.make(); puller.interval = 0.01
        runs, stop = [], threading.Event()
        def boom():
            runs.append(1)
            if len(runs) >= 3: stop.set()
            raise OSError('x')
        puller.run_once = boom
        thread = puller.start(stop); thread.join(5)
        self.assertGreaterEqual(len(runs), 3); self.assertFalse(thread.is_alive())
        self.assertEqual(self.journal.codes, ['readings_sync_failed'])

    def test_reason_survives_the_real_journal(self):
        j = diagnostics.Journal(None, None); j.record('readings_sync_failed', reason='pull failed')
        self.assertEqual(j.snapshot()[0]['reason'], 'pull failed')

    def test_owner_removes_a_leftover_shared_file(self):
        self.add({'name': 'mac', 'ssh': None, 'platform': 'darwin', 'roles': ['usage'], 'addedAt': 0})
        write_private(self.path, json.dumps(shared()))
        self.assertFalse(self.make().run_once()); self.assertFalse(self.path.exists())


if __name__ == '__main__': unittest.main()


class AuthProbeTests(unittest.TestCase):
    """A long-lived account that Shunt only cools after a 401 is probed, at most every 15 minutes, by the device that serves it."""
    def setUp(self):
        from fakes import long_lived_file, refreshable_file
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.stores = root / 'credentials'; self.stores.mkdir(mode=0o700)
        self.path = root / 'state' / 'long-lived-probes.json'
        self.config = root / 'config.json'
        self.config.write_text(json.dumps({'accounts': [{'name': 'team', 'alias': 'teamx'}, {'name': 'ops'}]}))
        long_lived_file(self.stores, 'team'); refreshable_file(self.stores, 'ops', 'u', 'o')
        for item in [patch.object(settings, 'STORES', self.stores), patch.object(settings, 'CONFIG', self.config)]:
            item.start(); self.addCleanup(item.stop)
        self.now, self.calls, self.answer = NOW, [], Completed(1, b'', b'API Error: 401')
        self.status = {'team': 'cooling', 'ops': 'cooling'}

    def probe(self):
        def run(alias):
            self.calls.append(alias); return self.answer
        overview = lambda: {'accounts': [{'name': n, 'status': s} for n, s in self.status.items()]}
        return readings.AuthProbe(self.path, overview=overview, run=run, clock=lambda: self.now).run_once()

    def test_a_cooling_long_lived_account_is_probed_once_per_15_minutes_and_stored_without_its_token(self):
        from fakes import fixture_token
        self.probe()
        self.assertEqual(self.calls, ['teamx'])  # the refreshable account is never probed
        self.assertEqual(readings.reauthenticate(self.path), {'team'})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(fixture_token(), self.path.read_text())
        self.now += 14 * 60_000; self.probe(); self.assertEqual(self.calls, ['teamx'])
        self.now += 2 * 60_000; self.answer = Completed(0, b'OK', b''); self.probe()
        self.assertEqual(self.calls, ['teamx', 'teamx']); self.assertEqual(readings.reauthenticate(self.path), set())

    def test_nothing_is_probed_while_the_account_serves_and_a_ready_account_clears_its_mark(self):
        self.probe(); self.assertEqual(readings.reauthenticate(self.path), {'team'})
        self.status = {'team': 'ready', 'ops': 'ready'}
        self.now += 60 * 60_000; self.probe()
        self.assertEqual(self.calls, ['teamx']); self.assertEqual(readings.reauthenticate(self.path), set())

    def test_a_replaced_token_is_not_marked_by_the_old_probe(self):
        from fakes import long_lived_file, fixture_token
        self.probe()
        (self.stores / 'team.json').unlink(); long_lived_file(self.stores, 'team', token=fixture_token('z'))
        self.assertEqual(readings.reauthenticate(self.path), set())

    def test_an_unreadable_overview_probes_nothing(self):
        def boom(): raise OSError('down')
        readings.AuthProbe(self.path, overview=boom, run=lambda alias: self.calls.append(alias)).run_once()
        self.assertEqual(self.calls, []); self.assertFalse(self.path.exists())
