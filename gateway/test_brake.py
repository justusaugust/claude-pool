import json, tempfile, unittest
from pathlib import Path
import brake
from brake import stage, usd, ledger_line, today_summary

NOW = 1900000000000
H = 3600000


def acct(name, pct, reset_in, status='ready', week=None):
    return {'name': name, 'status': status, 'fiveHour': {'pct': pct, 'resetsAt': NOW + reset_in}, 'sevenDay': week}


class StageTests(unittest.TestCase):
    def test_stage_compares_pooled_forecast_with_the_next_usable_reset(self):
        accounts = [acct('team', 60, 2 * H), acct('sofi', 50, 3 * H)]
        self.assertEqual(stage({'seconds': 3 * 3600}, accounts, NOW)['stage'], 0)
        self.assertEqual(stage({'seconds': 1.5 * 3600}, accounts, NOW)['stage'], 1)
        info = stage({'seconds': 0.8 * 3600}, accounts, NOW)
        self.assertEqual((info['stage'], round(info['ratio'], 2), info['resetAt']), (2, 0.4, NOW + 2 * H))
        self.assertEqual(stage({'seconds': None}, accounts, NOW)['stage'], 0)

    def test_a_weekly_blocked_account_reset_does_not_count_as_new_capacity(self):
        accounts = [acct('gmail', 5, H // 2, status='limited', week={'pct': 100, 'resetsAt': NOW + 90 * H}), acct('team', 60, 4 * H)]
        info = stage({'seconds': 2 * 3600}, accounts, NOW)
        self.assertEqual((info['stage'], info['resetAt']), (1, NOW + 4 * H))

    def test_an_account_whose_week_resets_soon_counts_as_returning_capacity(self):
        # 22.10. 00:20: The second Max account had a full week, an empty five-hour window and its week reset in 40 min.
        accounts = [acct('max', 0, 3 * H, status='limited', week={'pct': 100, 'resetsAt': NOW + 40 * 60000}), acct('team', 61, 4 * H)]
        info = stage({'seconds': 2.5 * 3600}, accounts, NOW)
        self.assertEqual((info['stage'], info['resetAt']), (0, NOW + 40 * 60000))
        accounts[0]['fiveHour']['pct'] = 100   # a full five-hour window returns only when both have reset
        self.assertEqual(stage({'seconds': 2.5 * 3600}, accounts, NOW)['resetAt'], NOW + 3 * H)


class ExemptionTests(unittest.TestCase):
    def test_the_conductor_and_urgent_projects_are_exempt_from_the_brake(self):
        # The Conductor runs as project conductor (COCKPIT_PROJECT is its folder name); conductor-wecker is its wake session.
        self.assertTrue(brake.exempt('conductor'))
        self.assertTrue(brake.exempt('conductor-wecker'))
        self.assertTrue(brake.exempt('pitch-riot', {'pitch-riot'}))
        self.assertFalse(brake.exempt('website', {'pitch-riot'}))
        self.assertFalse(brake.exempt(''))


class LedgerTests(unittest.TestCase):
    def test_usd_weighs_tokens_by_model_price(self):
        usage = {'input_tokens': 1000000, 'output_tokens': 100000, 'cache_read_input_tokens': 1000000, 'cache_creation_input_tokens': 0}
        self.assertAlmostEqual(usd('claude-opus-5-5', usage), 4 + 2 + 0.2)
        self.assertAlmostEqual(usd('claude-sonnet-5-5', usage), 2 + 1 + 0.2)
        self.assertAlmostEqual(usd('claude-fable-5-1', usage), 10 + 5 + 0.25)

    def test_usd_prices_one_hour_cache_writes_at_twice_input_and_five_minute_writes_at_one_and_a_quarter(self):
        usage = {'cache_creation_input_tokens': 3000000,
                 'cache_creation': {'ephemeral_5m_input_tokens': 1000000, 'ephemeral_1h_input_tokens': 2000000}}
        self.assertAlmostEqual(usd('claude-opus-5-5', usage), 1 * 4 * 1.25 + 2 * 4 * 2)
        self.assertAlmostEqual(usd('claude-opus-5-5', {'cache_creation_input_tokens': 3000000}), 3 * 4 * 1.25, msg='no breakdown: 5-minute price')
        partial = {'cache_creation_input_tokens': 3000000, 'cache_creation': {'ephemeral_1h_input_tokens': 1000000}}
        self.assertAlmostEqual(usd('claude-opus-5-5', partial), 1 * 4 * 2 + 2 * 4 * 1.25, msg='the rest is a 5-minute write')

    def test_the_ledger_keeps_cache_writes_by_ttl_when_usage_reports_them(self):
        usage = {'input_tokens': 3, 'cache_creation_input_tokens': 11683,
                 'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 11683}}
        row = ledger_line(NOW, 's1', 'website', 'subagent', 'claude-opus-5-5', 'team', usage)
        self.assertEqual((row['cache_creation_input_tokens'], row['ephemeral_5m_input_tokens'], row['ephemeral_1h_input_tokens']), (11683, 0, 11683))
        self.assertAlmostEqual(row['usd'], round((3 * 4 + 11683 * 4 * 2) / 1e6, 6))
        self.assertFalse(row['braked'])
        plain = ledger_line(NOW, 's1', 'website', 'main', 'claude-opus-5-5', 'team', {'cache_creation_input_tokens': 5})
        self.assertNotIn('ephemeral_1h_input_tokens', plain)

    def test_summary_groups_today_by_project_and_class(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'today.jsonl'
            rows = [ledger_line(NOW, 's1', 'pitch-riot', 'subagent', 'claude-sonnet-5-5', 'intern', {'input_tokens': 10, 'output_tokens': 1000000}, braked=True),
                    ledger_line(NOW, 's1', 'pitch-riot', 'main', 'claude-opus-5-5', 'intern', {'output_tokens': 100000}),
                    ledger_line(NOW, 's2', 'conductor-wecker', 'main', 'claude-opus-5-5', 'team', {'output_tokens': 50000})]
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows) + 'kaputt\n')
            s = today_summary(path)
            self.assertEqual([p['project'] for p in s['projects']], ['pitch-riot', 'conductor-wecker'])
            self.assertAlmostEqual(s['projects'][0]['usd'], 10 + 2, places=3)
            self.assertEqual(s['braked'], 1)
            self.assertAlmostEqual(s['classes']['subagent'], 10, places=3)
            self.assertEqual([(r['session'], r['project']) for r in s['sessions']], [('s1', 'pitch-riot'), ('s2', 'conductor-wecker')])
            self.assertAlmostEqual(s['sessions'][0]['usd'], 12, places=3)
            self.assertAlmostEqual(s['sessions'][0]['classes']['main'], 2, places=3)
            self.assertNotIn('content', json.dumps(rows), 'no prompt content in the ledger')


if __name__ == '__main__':
    unittest.main()
