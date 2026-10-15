import json
import unittest
from unittest.mock import patch
from telemetry import enrich, fable_windows, coverage_note
from forecast import estimate


class TelemetryTests(unittest.TestCase):
    def data(self):
        return {'accounts': [{'id': 'org', 'provider': 'anthropic', 'name': 'team', 'label': 'Team',
                             'status': 'ready', 'seenAt': 1900000000000,
                             'fiveHour': {'pct': 12, 'resetsAt': 1900003600000}, 'sevenDay': None}],
                'forecast': {'seconds': 1000, 'partial': False}}

    def admin(self, pct=0):
        return {'providers': [{'provider': 'anthropic', 'auth': 'claude_oauth', 'accounts': [
            {'name': 'team', 'account_ref': 'org', 'utilization_7d_oi': pct, 'reset_7d_oi': 1900007200}]}]}

    def test_fable_zero_is_separate_and_missing_weekly_is_explicit(self):
        data = enrich(self.data(), self.admin())
        self.assertEqual(data['accounts'][0]['fableWeekly']['pct'], 0)
        self.assertIsNone(data['accounts'][0]['sevenDay'])
        self.assertEqual(data['forecast']['seconds'], 1000)
        self.assertTrue(data['forecast']['partial'])
        self.assertEqual(data['forecast']['missingWindows'], [{'name': 'team', 'label': 'Team', 'window': 'sevenDay'}])
        self.assertIn('Capacity may run out sooner', coverage_note(data['accounts']))

    def test_invalid_absent_and_other_organization_never_become_zero(self):
        for value in [None, True, -1, float('nan'), '0']:
            self.assertIsNone(next(iter(fable_windows(self.admin(value)).values())))
        data = self.data()
        data['accounts'][0]['id'] = 'other-org'
        self.assertNotIn('fableWeekly', enrich(data, self.admin())['accounts'][0])

    def test_fable_cannot_limit_or_replace_the_all_model_forecast(self):
        account = self.data()['accounts'][0]
        points = [{'at': account['seenAt'] - (2-i)*600000, 'fiveHour': {**account['fiveHour'], 'pct': pct}}
                  for i, pct in enumerate([2, 7, 12])]
        first = estimate([account], {'org': points}, account['seenAt'])
        account['fableWeekly'] = {'pct': 100, 'resetsAt': 1900007200000}
        self.assertEqual(first, estimate([account], {'org': points}, account['seenAt']))
        self.assertTrue(first['partial'])
        self.assertEqual(first['missingWindows'][0]['window'], 'sevenDay')

    def test_launcher_enriches_live_old_bridge_without_restart_and_survives_outage(self):
        import launch
        old = self.data()
        raw = json.dumps(old).encode()
        with patch.object(launch.http.client, 'HTTPConnection') as conn, patch.object(launch, 'token', return_value='fixture-admin'):
            response = conn.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = raw
            with patch('telemetry.read_admin', return_value=self.admin()):
                result = json.loads(launch.request('fixture-client', path='/pool/overview'))
            self.assertEqual(result['accounts'][0]['fableWeekly']['pct'], 0)
            with patch('telemetry.read_admin', side_effect=OSError()):
                result = json.loads(launch.request('fixture-client', path='/pool/overview'))
            self.assertEqual(result, old)


if __name__ == '__main__': unittest.main()


class CapacityTests(unittest.TestCase):
    def test_reset_requires_all_governing_windows_and_unknown_is_not_exhaustion(self):
        from telemetry import capacity
        def row(**kwargs): return {'status': 'limited', **kwargs}
        a = row(quotaLimits={'fiveHour': {'status': 'rejected', 'resetsAt': 1200}, 'sevenDay': {'status': 'rejected', 'resetsAt': 5000}})
        b = row(fiveHour={'pct': 100, 'resetsAt': 2000})
        self.assertEqual(capacity([a,b], 'claude-opus-5-5', 1000)['nextResetAt'], 2000)
        self.assertEqual(capacity([a,b], 'claude-opus-5-5', 2100)['state'], 'ready')
        self.assertEqual(capacity([a], 'claude-opus-5-5', 2100)['state'], 'quota_wait')
        self.assertEqual(capacity([a, row()], 'claude-opus-5-5', 1000)['state'], 'ready')
        self.assertEqual(capacity([{'status': 'paused'}], 'claude-opus-5-5', 1000)['state'], 'unavailable')
        self.assertEqual(capacity([row(quotaLimits={'sevenDay': {'status': 'rejected'}})], 'claude-opus-5-5', 1000)['nextResetAt'], None)

    def test_fable_limit_does_not_block_opus(self):
        from telemetry import capacity
        rows = [{'status': 'ready', 'quotaLimits': {'fableWeekly': {'status': 'rejected', 'resetsAt': 5000}}}]
        self.assertEqual(capacity(rows, 'claude-opus-5-5', 1000)['state'], 'ready')
        self.assertEqual(capacity(rows, 'claude-fable-5', 1000)['state'], 'quota_wait')
