import unittest
import tempfile
from pathlib import Path
from routing import choose, profile, session_rows
from forecast import estimate, renewal_scenario, workload_estimates
from validation import archive, load, evaluate

NOW = 1900000000000


class CoordinationTests(unittest.TestCase):
    def account(self, name, pct=20, status='ready'):
        return {'id': name, 'name': name, 'label': name, 'status': status, 'seenAt': NOW,
                'fiveHour': {'pct': pct, 'resetsAt': NOW + 3600000},
                'sevenDay': {'pct': pct, 'resetsAt': NOW + 8 * 86400000}}

    def test_atomic_reservations_spread_new_sessions_keep_affinity_and_ignore_idle(self):
        a, b = self.account('gmail'), self.account('team')
        f = {'accounts': [{'name': 'gmail', 'seconds': 1800}, {'name': 'team', 'seconds': 1800}]}
        sessions = {'one': {'requestAt': NOW / 1000, 'reserved': 'gmail', 'inFlight': 1}, 'two': {}}
        r = choose([a, b], sessions, 'two', f, NOW / 1000)
        self.assertEqual((r['account'], r['reason']), ('team', 'shared capacity'))
        sessions['two']['active'] = 'gmail'
        self.assertEqual(choose([a, b], sessions, 'two', f, NOW / 1000)['reason'], 'cache affinity')
        f['accounts'][0]['seconds'] = 600
        self.assertEqual(choose([a, b], sessions, 'two', f, NOW / 1000)['reason'], 'shared capacity')
        sessions['one'] = {'requestAt': NOW / 1000 - 301, 'active': 'gmail'}
        self.assertEqual(session_rows({'one': sessions['one']}, NOW / 1000), [])

    def test_pin_quota_reason_and_all_unavailable_remain_preferences(self):
        a, b = self.account('gmail', 100, 'limited'), self.account('team')
        f = estimate([a, b], {}, NOW)
        sessions = {'s': {'active': 'gmail', 'pin': 'gmail'}}
        r = choose([a, b], sessions, 's', f, NOW / 1000)
        self.assertEqual((r['account'], r['reason']), ('team', 'weekly quota exhausted'))
        sessions['s']['pin'] = 'team'
        self.assertEqual(choose([a, b], sessions, 's', f, NOW / 1000)['reason'], 'manual selection')
        b['status'] = 'paused'
        self.assertEqual(choose([a, b], sessions, 's', f, NOW / 1000)['reason'], 'no eligible account')

    def test_profile_never_contains_prompt_or_arbitrary_model_text(self):
        self.assertEqual(profile({'model': 'claude-opus-5-5', 'output_config': {'effort': 'high'}, 'messages': ['private']})['workload'], 'opus/high')
        self.assertEqual(profile({'model': 'secret\nvalue'})['model'], 'unknown')
        self.assertEqual(profile({'model': 'sk-ant-api-fixture'})['model'], 'unknown')
        self.assertEqual(profile({'output_config': {'effort': []}, 'thinking': {'type': {}}})['effort'], 'standard')
        self.assertEqual(profile({'model': 'claude-sonnet', 'thinking': {'type': 'enabled'}})['workload'], 'sonnet/thinking')

    def test_renewal_opens_capacity_only_after_known_reset_and_later_anchor_is_unknown(self):
        a = self.account('gmail', 100, 'limited'); a['sevenDay']['pct'] = 20
        f = {'threshold': 95, 'accounts': [{'name': 'gmail', 'windows': {'fiveHour': {'ratePerHour': 20}, 'sevenDay': {'ratePerHour': 1}}}]}
        r = renewal_scenario([a], f, NOW)
        self.assertEqual(r['segments'][0]['available'], [])
        at_reset = next(s for s in r['segments'] if s['at'] <= NOW + 3600000 < s['until'])
        self.assertEqual(at_reset['available'], ['gmail'])
        after_second = next(s for s in r['segments'] if s['at'] <= NOW + 6 * 3600000 < s['until'])
        self.assertEqual(after_second['uncertain'], ['gmail'])
        self.assertTrue(r['partial'])
        a['sevenDay'] = None
        r = renewal_scenario([a], f, NOW)
        self.assertEqual(r['coveredSeconds'], 0); self.assertGreater(r['possibleSeconds'], 0)

    def test_rejected_unknown_week_is_unavailable_until_reset_then_uncertain(self):
        a=self.account('team',20,'limited');a['sevenDay']=None
        a['quotaLimits']={'sevenDay':{'status':'rejected','resetsAt':NOW+7200000}}
        f={'threshold':95,'accounts':[{'name':'team','windows':{'fiveHour':{'ratePerHour':0}}}]}
        r=renewal_scenario([a],f,NOW)
        first=r['segments'][0]
        self.assertEqual(first['available'],[]);self.assertEqual(first['uncertain'],[])
        before=next(s for s in r['segments'] if s['at']<=NOW+3600000<s['until'])
        self.assertEqual(before['available'],[]);self.assertEqual(before['uncertain'],[])
        later=next(s for s in r['segments'] if s['at']<=NOW+7200000<s['until'])
        self.assertEqual(later['available'],[]);self.assertEqual(later['uncertain'],['team'])
        self.assertIn({'account':'team','window':'week','at':NOW+7200000},r['resets'])
        a['quotaLimits']['sevenDay']['resetsAt']=None
        r=renewal_scenario([a],f,NOW)
        self.assertEqual(r['segments'][0]['uncertain'],[])
        self.assertEqual(r['segments'][1]['uncertain'],['team'])

    def test_workload_learning_requires_tagged_same_window_span_and_reports_cache(self):
        a = self.account('gmail', 30)
        points = [{'at': NOW - 1200000 + i * 600000, 'fiveHour': {**a['fiveHour'], 'pct': v},
                   'sevenDay': a['sevenDay'], 'workload': 'opus/high'} for i, v in enumerate([10, 20, 30])]
        calls = [{'at': NOW, 'workload': 'opus/high', 'cacheReadTokens': 80, 'inputTokens': 20, 'thinkingTokens': 15, 'outputTokens': 30}]
        r = workload_estimates([a], {'gmail': points}, calls, NOW)[0]
        self.assertEqual(r['windows'][0]['ratePerHour'], 60)
        self.assertEqual(r['cacheReadShare'], .8); self.assertEqual(r['medianThinkingTokens'], 15)
        points[1]['workload'] = 'sonnet/standard'
        self.assertEqual(workload_estimates([a], {'gmail': points}, calls, NOW)[0]['windows'], [])

    def test_frozen_renewal_scenario_is_scored_after_reset_without_future_refitting(self):
        a = self.account('gmail', 100, 'limited'); a['fiveHour']['resetsAt'] = NOW + 1800000; a['sevenDay']['pct'] = 20
        f = {'seconds': None, 'partial': False, 'threshold': 95, 'accounts': [{'name': 'gmail', 'windows': {'fiveHour': {'ratePerHour': 10}, 'sevenDay': {'ratePerHour': 1}}}]}
        f['renewals'] = renewal_scenario([a], f, NOW)
        with tempfile.TemporaryDirectory() as directory:
            archive(directory, [a], f, NOW)
            later = {**a, 'status': 'ready', 'seenAt': NOW + 3600000,
                     'fiveHour': {'pct': 5, 'resetsAt': NOW + 6 * 3600000}}
            archive(directory, [later], estimate([later], {}, later['seenAt']), later['seenAt'])
            rows, bad = load(directory); self.assertEqual(bad, 0)
            checks = evaluate(rows)['renewalAccountChecks']
            self.assertEqual(len(checks), 1); self.assertTrue(checks[0]['correct']); self.assertTrue(checks[0]['crossedReset'])
            rows[-1]['accounts'][0]['fiveHour']['pct'] = 100
            self.assertFalse(evaluate(rows)['renewalAccountChecks'][0]['correct'])
            self.assertEqual(rows[0]['forecast']['renewals'], {'segments': f['renewals']['segments']})


if __name__ == '__main__': unittest.main()
