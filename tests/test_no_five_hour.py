"""An account without a five-hour limit is not an account with a missing reading.

Since November 2026 Claude sends the largest Max plan only weekly windows: every response carries the week and the Fable
week, none a five-hour window. The pool took the time of a reading from the five-hour window alone, so such an account
read as never checked: its week reading counted as stale, the forecast left the account out, and the status warned of a
missing five-hour reading.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from bridge import PoolBridge
import conductor_board
from forecast import estimate, renewal_scenario
import routing
from telemetry import missing_windows
import validation
from validation import archive, evaluate, load
from views import usage

NOW = 1900000000000
H, DAY = 3600000, 86400000
NONE = {'status': None, 'resetsAt': None}


def account(name, five=20, week=20, seen=NOW, **more):
    limits = {'fiveHour': {'status': 'allowed', 'resetsAt': NOW + H} if five is not None else dict(NONE),
              'sevenDay': {'status': 'allowed', 'resetsAt': NOW + 5 * DAY} if week is not None else dict(NONE), 'fableWeekly': dict(NONE)}
    return {'id': name, 'name': name, 'label': name.title(), 'provider': 'anthropic', 'status': 'ready', 'plan': 'Max', 'seenAt': seen,
            'fiveHour': {'pct': five, 'resetsAt': NOW + H} if five is not None else None,
            'sevenDay': {'pct': week, 'resetsAt': NOW + 5 * DAY} if week is not None else None, 'quotaLimits': limits, **more}


def week_points(start, per_hour, minutes=50):
    """Week readings of the last `minutes`, one every ten minutes, rising by `per_hour` points an hour."""
    return [{'at': NOW - (minutes - m) * 60000, 'fiveHour': None,
             'sevenDay': {'pct': start + per_hour * m / 60, 'resetsAt': NOW + 5 * DAY}} for m in range(0, minutes + 1, 10)]


class NoFiveHourTests(unittest.TestCase):
    def test_a_week_reading_without_a_five_hour_window_means_no_five_hour_limit(self):
        self.assertTrue(routing.no_five_hour(account('ultra', five=None)))
        self.assertFalse(routing.no_five_hour(account('team')))
        self.assertFalse(routing.no_five_hour(account('fresh', five=None, week=None)), 'no reading at all says nothing yet')
        rejected = account('held', five=None)
        rejected['quotaLimits']['fiveHour'] = {'status': 'rejected', 'resetsAt': NOW + H}
        self.assertFalse(routing.no_five_hour(rejected), 'a five-hour status is a five-hour window')
        self.assertTrue(routing.no_five_hour({'name': 'archived', 'fiveHour': None, 'sevenDay': {'pct': 5, 'resetsAt': NOW + DAY}}))

    def test_it_is_not_a_missing_window(self):
        rows = [account('ultra', five=None), account('team'), account('fresh', five=None, week=None)]
        self.assertEqual(missing_windows(rows), [{'name': 'fresh', 'label': 'Fresh', 'window': 'fiveHour'},
                                                 {'name': 'fresh', 'label': 'Fresh', 'window': 'sevenDay'}])

    def test_the_forecast_counts_it_in_full(self):
        ultra = account('ultra', five=None, week=40)
        forecast = estimate([ultra], {'ultra': week_points(30, 12)}, NOW)
        self.assertEqual((forecast['estimatedAccounts'], forecast['partial']), (1, False))
        self.assertAlmostEqual(forecast['seconds'], 55 / 12 * 3600, delta=60)
        team = account('team', week=40)
        self.assertTrue(estimate([team], {'team': week_points(30, 12)}, NOW)['partial'], 'a five-hour window without pace stays partial')

    def test_the_outlook_does_not_wait_for_a_five_hour_reading(self):
        ultra = account('ultra', five=None, week=40)
        forecast = estimate([ultra], {'ultra': week_points(30, 12)}, NOW)
        outlook = renewal_scenario([ultra], forecast, NOW)
        self.assertEqual((outlook['partial'], outlook['segments'][0]['available']), (False, ['ultra']))

    def test_the_status_says_no_limit_and_does_not_warn(self):
        rows = [account('ultra', five=None), account('team')]
        data = {'accounts': rows, 'updatedAt': NOW, 'ready': 2, 'sessions': [], 'history': [], 'forecast': estimate(rows, {}, NOW)}
        text = usage(data, width=80)
        line = next(line for line in text.splitlines() if line.strip().startswith('5h') and 'no limit' in line)
        self.assertNotIn('not reported', line)
        self.assertNotIn('5h readings missing', text)
        data['accounts'].append(account('fresh', five=None, week=None))
        data['forecast'] = estimate(data['accounts'], {}, NOW)
        self.assertIn('5h readings missing', usage(data, width=80), 'an account without any reading still warns')

    def test_the_board_snapshot_marks_it(self):
        rows = [account('ultra', five=None), account('team')]
        marks = {row['name']: row['noFiveHour'] for row in conductor_board.Board(overview=lambda: {'accounts': rows, 'sessions': []},
                                                                                account_rows=lambda: {}).accounts(NOW / 1000)}
        self.assertEqual(marks, {'ultra': True, 'team': False})

    def test_the_forecast_check_scores_an_account_that_has_only_a_week(self):
        ultra = account('ultra', five=None, week=40)
        self.assertEqual(validation.present(ultra), ['sevenDay'])
        self.assertEqual(validation.present(account('team')), ['fiveHour', 'sevenDay'])
        forecast = estimate([ultra], {'ultra': week_points(30, 12)}, NOW)
        with tempfile.TemporaryDirectory() as directory:
            archive(directory, [ultra], forecast, NOW)
            for step, pct in ((1, 60), (2, 96)):
                at = NOW + step * 300000
                later = {**ultra, 'seenAt': at, 'sevenDay': {'pct': pct, 'resetsAt': NOW + 5 * DAY}}
                archive(directory, [later], estimate([later], {}, at), at)
            rows, bad = load(directory)
            outcomes = evaluate(rows)['poolOutcomes']
        self.assertEqual((bad, outcomes.get('observed depletion'), outcomes.get('incomplete readings')), (0, 1, None))


class ReadingTimeTests(unittest.TestCase):
    def overview(self, row):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                body = json.dumps({'providers': [{'provider': 'anthropic', 'auth': 'claude_oauth', 'accounts': [row]}]}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as folder:
            bridge = PoolBridge(('127.0.0.1', 0), upstream.server_port, 'c', 'a', Path(folder) / 'state.json')
            try: return bridge.overview(), dict(bridge.observations)
            finally:
                bridge.monitor_stop.set(); bridge.server_close(); upstream.shutdown(); upstream.server_close()

    def test_a_week_only_account_is_seen_when_its_week_was_read(self):
        seen = int(time.time()) - 30
        data, observations = self.overview({'name': 'ultra', 'account_ref': 'ultra', 'has_state': True, 'observed_at_5h': None,
                                            'observed_at_7d': seen, 'utilization_7d': .4, 'reset_7d': seen + 400000})
        row = data['accounts'][0]
        self.assertEqual((row['seenAt'], row['fiveHour'], row['sevenDay']['pct']), (seen * 1000, None, 40))
        self.assertEqual([point['at'] for point in observations['ultra']], [seen * 1000], 'the reading feeds the pace')

    def test_a_five_hour_reading_keeps_its_own_time(self):
        seen = int(time.time()) - 30
        data, _ = self.overview({'name': 'team', 'account_ref': 'team', 'has_state': True, 'observed_at_5h': seen - 600,
                                 'observed_at_7d': seen, 'utilization_5h': .2, 'reset_5h': seen + 9000,
                                 'utilization_7d': .4, 'reset_7d': seen + 400000})
        self.assertEqual(data['accounts'][0]['seenAt'], (seen - 600) * 1000)

    def test_a_backend_without_the_week_time_reads_as_before(self):
        data, _ = self.overview({'name': 'ultra', 'account_ref': 'ultra', 'has_state': True, 'observed_at_5h': None,
                                 'utilization_7d': .4, 'reset_7d': int(time.time()) + 400000})
        self.assertEqual(data['accounts'][0]['seenAt'], 0)


if __name__ == '__main__': unittest.main()
