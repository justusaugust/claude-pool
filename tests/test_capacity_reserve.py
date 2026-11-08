"""An account at its hard reserve takes no work: capacity, the brake and the Conductor alert agree with Shunt.

The scenario: every account without a reserve has a full week, two reserved accounts sit at their caps below 100 %,
one account is paused. Shunt chooses no account, so the pool is out of quota until the earliest full week resets.
"""
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import brake
import test_bridge
from conductor_alerts import Alerts
from telemetry import capacity

NOW = 1900000000  # seconds
H = 3600
# name, status, five-hour (pct, reset in s from now), week (pct, reset in s from now), reserve, Shunt flags
SCENARIO = [
    ('reserve-high', 'limited', None, (99, 47 * H), {'reserveAt': 99}, {'capped': True}),
    ('reserve-low', 'limited', (3, 3 * H), (77, 81 * H), {'reserveAt': 75}, {'capped': True}),
    ('paused', 'paused', None, None, {}, {'paused': True}),
    ('spent-a', 'limited', (0, 5 * H), (100, 102 * H), {}, {'near_quota': True, 'status_7d': 'rejected'}),
    ('spent-soft', 'limited', (0, 5 * H), (100, 111 * H), {'reserveAt': 90, 'reserveSoft': True}, {'near_quota': True, 'status_7d': 'rejected'}),
    ('spent-b', 'limited', (5, 1 * H), (100, 19 * H), {}, {'near_quota': True, 'status_7d': 'rejected'}),
]
FIRST_WEEK_RESET = NOW + 19 * H  # spent-b: the first moment Shunt can choose an account again


def rows(now):
    """The accounts as bridge.read_accounts and account_metadata.decorate give them at `now` (seconds)."""
    out = []
    for name, status, five, week, reserve, flags in SCENARIO:
        window = lambda w: {'pct': w[0], 'resetsAt': (now + w[1]) * 1000} if w else None
        out.append({'id': name, 'name': name, 'label': name, 'provider': 'anthropic', 'status': status,
                    'fiveHour': window(five), 'sevenDay': window(week), 'fableWeekly': None, 'seenAt': now * 1000,
                    'quotaLimits': {'fiveHour': {'status': None, 'resetsAt': None},
                                    'sevenDay': {'status': flags.get('status_7d'), 'resetsAt': (now + week[1]) * 1000 if week else None},
                                    'fableWeekly': {'status': None, 'resetsAt': None}},
                    **reserve})
    return out


def shunt_admin(now):
    """Shunt's admin answer (GET /admin/api/pool) for the same scenario at `now` (seconds)."""
    accounts = []
    for name, _, five, week, _, flags in SCENARIO:
        row = {'name': name, 'account_ref': name, 'has_state': True, 'observed_at_5h': now, **flags}
        if five: row.update(utilization_5h=five[0] / 100, reset_5h=now + five[1])
        if week: row.update(utilization_7d=week[0] / 100, reset_7d=now + week[1])
        accounts.append(row)
    return {'providers': [{'provider': 'anthropic', 'auth': 'claude_oauth', 'accounts': accounts}]}


class ReserveCapTests(unittest.TestCase):
    def test_capacity_waits_when_only_accounts_at_their_hard_reserve_remain(self):
        accounts = rows(NOW)
        self.assertTrue(brake.standstill(accounts))
        quota = capacity(accounts, 'claude-opus-5-5', NOW * 1000)
        self.assertEqual((quota['state'], quota['nextResetAt']), ('quota_wait', FIRST_WEEK_RESET * 1000))

    def test_shunts_capped_flag_alone_holds_an_account(self):
        account = {'name': 'capped', 'status': 'limited', 'capped': True, 'fiveHour': {'pct': 40, 'resetsAt': (NOW + H) * 1000},
                   'sevenDay': {'pct': 60, 'resetsAt': (NOW + 50 * H) * 1000}}
        self.assertEqual(capacity([account], 'claude-opus-5-5', NOW * 1000)['state'], 'quota_wait')
        self.assertTrue(brake.standstill([account]))

    def test_brake_returns_a_reserve_capped_account_at_the_reset_of_its_capping_window(self):
        info = brake.stage({'seconds': H}, rows(NOW), NOW * 1000)
        self.assertEqual(info['resetAt'], FIRST_WEEK_RESET * 1000)
        self.assertEqual(brake.returns_at(rows(NOW)[1], NOW * 1000), (NOW + 81 * H) * 1000)

    def test_conductor_alert_reports_that_every_account_is_at_its_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            wake = Path(folder) / 'state' / 'wake.log'
            wake.parent.mkdir()
            alerts = Alerts(wake, Path(folder) / 'alerts.json', clock=lambda: NOW)
            server = types.SimpleNamespace(health={'failures': 0}, last_accounts=rows(NOW), blocked={}, brake_info=lambda: {})
            alerts.check(server)
            text = wake.read_text() if wake.exists() else ''
        self.assertIn('pool konten-limit an conductor', text)


class ReserveCapGatewayTests(unittest.TestCase):
    setUp, tearDown = test_bridge.BridgeTests.setUp, test_bridge.BridgeTests.tearDown
    open_stream, until, MESSAGE = test_bridge.BridgeTests.open_stream, test_bridge.BridgeTests.until, test_bridge.BridgeTests.MESSAGE

    def scenario(self):
        """Shunt reports the scenario, and the config adds each account's reserve as account_metadata.decorate does."""
        real = self.bridge.admin
        def admin(path='/admin/api/pool', body=None, timeout=3):
            if body is None and path == '/admin/api/pool': return shunt_admin(int(time.time()))
            return real(path, body, timeout)
        reserves = {name: reserve for name, _, _, _, reserve, _ in SCENARIO}
        def decorate(data, path, now=None):
            for account in data.get('accounts', []): account.update(reserves.get(account.get('name'), {}))
            return data
        for patcher in [patch.object(self.bridge, 'admin', side_effect=admin), patch('account_metadata.decorate', side_effect=decorate)]:
            patcher.start(); self.addCleanup(patcher.stop)

    def test_a_weekly_limit_429_waits_instead_of_reaching_an_interactive_session(self):
        # Shunt tried every account it may choose and relays the last 429; the reserved accounts sit at their caps.
        self.scenario()
        self.default_account = 'spent-b'
        self.statuses = [429]
        self.bodies = [json.dumps({'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'rate limit'}}).encode()]
        response = self.open_stream(self.MESSAGE, session='interactive')
        status = response.status
        response.close(); self.stream_conn.close()
        self.until(lambda: not self.bridge.requests)
        self.assertEqual(status, 200, 'the request waits for quota in an event stream instead of relaying the 429')


if __name__ == '__main__': unittest.main()
