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
        # The Conductor runs as project conductor (COCKPIT_PROJECT is its folder name); conductor-wecker is its watcher.
        self.assertTrue(brake.exempt('conductor'))
        self.assertTrue(brake.exempt('conductor-wecker'))
        self.assertTrue(brake.exempt('pitch-riot', {'pitch-riot'}))
        self.assertFalse(brake.exempt('website', {'pitch-riot'}))
        self.assertFalse(brake.exempt(''))


class HoldTests(unittest.TestCase):
    """Background work waits from the first sample with the brake on; it goes again after 15 minutes of calm in a row."""
    def test_stage_zero_must_hold_fifteen_minutes_before_the_hold_lets_go(self):
        hold = brake.Hold()
        self.assertFalse(hold.on)
        self.assertEqual(hold.update(True, 0), 'on')
        self.assertIsNone(hold.update(True, 60))
        for t in (120, 600, 1019):
            self.assertIsNone(hold.update(False, t)); self.assertTrue(hold.on, t)
        self.assertEqual(hold.update(False, 120 + 15 * 60), 'off')
        self.assertFalse(hold.on)
        self.assertIsNone(hold.update(False, 5000), 'off stays off without a change')

    def test_one_braking_sample_starts_the_calm_over(self):
        hold = brake.Hold()
        hold.update(True, 0)
        hold.update(False, 60)
        hold.update(True, 14 * 60)                 # the stage flickers back
        self.assertIsNone(hold.update(False, 15 * 60))
        self.assertIsNone(hold.update(False, 16 * 60), 'fifteen minutes after the first calm sample is not enough')
        self.assertTrue(hold.on)
        self.assertEqual(hold.update(False, 30 * 60), 'off')

    def test_the_hold_survives_a_restart_with_its_calm(self):
        hold = brake.Hold()
        hold.update(True, 0); hold.update(False, 100)
        again = brake.Hold.restore(json.loads(json.dumps(hold.state())))
        self.assertTrue(again.on)
        self.assertEqual(again.update(False, 100 + 15 * 60), 'off')
        for bad in (None, [], {'on': 'yes'}, {'on': True, 'calmSince': 'later'}):
            self.assertIn(brake.Hold.restore(bad).on, (False, True))
        self.assertFalse(brake.Hold.restore(None).on)

    def test_braking_means_stage_one_or_a_confirmed_standstill(self):
        self.assertTrue(brake.braking({'stage': 1}))
        self.assertTrue(brake.braking({'stage': 0, 'standstill': True}))
        self.assertFalse(brake.braking({'stage': 0, 'standstill': False}))
        self.assertFalse(brake.braking({}))
        ready, limited, unknown = {'status': 'ready'}, {'status': 'limited'}, {'status': 'unknown'}
        self.assertTrue(brake.standstill([limited, unknown]))
        self.assertFalse(brake.standstill([limited, ready]))
        self.assertFalse(brake.standstill([unknown, unknown]), 'no reading yet (a fresh start) is no standstill')
        self.assertFalse(brake.standstill([]))


class SessionClassTests(unittest.TestCase):
    """A session is interactive for 90 minutes after a message from a person; one the pool never classified, too."""
    def test_a_recent_owner_message_makes_the_session_interactive(self):
        now = 10 * 3600
        self.assertTrue(brake.interactive({'ownerAt': now - 89 * 60, 'autoAt': now - 60}, now))
        self.assertFalse(brake.interactive({'ownerAt': now - 91 * 60, 'autoAt': now - 60}, now))
        self.assertFalse(brake.interactive({'ownerAt': now - 91 * 60}, now))
        self.assertFalse(brake.interactive({'autoAt': now - 60}, now))

    def test_a_session_the_pool_never_classified_counts_as_interactive(self):
        self.assertTrue(brake.interactive({}, 100))
        self.assertTrue(brake.interactive(None, 100))
        self.assertTrue(brake.interactive({'mode': 'auto', 'ownerAt': 'yesterday'}, 100))


class LedgerTests(unittest.TestCase):
    def test_usd_weighs_tokens_by_model_price(self):
        usage = {'input_tokens': 1000000, 'output_tokens': 100000, 'cache_read_input_tokens': 1000000, 'cache_creation_input_tokens': 0}
        self.assertAlmostEqual(usd('claude-opus-5-5', usage), 4 + 2 + 0.2)
        self.assertAlmostEqual(usd('claude-sonnet-5-5', usage), 2 + 1 + 0.1, msg='cache reads 0.05x input since 7 Oct 2026')
        self.assertAlmostEqual(usd('claude-sonnet-5', usage), 2 + 1 + 0.2)
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

    def test_an_answer_that_waited_for_the_brake_carries_its_wait(self):
        row = ledger_line(NOW, 's1', 'website', 'subagent', 'claude-opus-5-5', 'team', {'output_tokens': 1}, waited=125.4)
        self.assertEqual((row['braked'], row['brakeWaitSeconds']), (True, 125))
        plain = ledger_line(NOW, 's1', 'website', 'main', 'claude-opus-5-5', 'team', {'output_tokens': 1})
        self.assertFalse(plain['braked']); self.assertNotIn('brakeWaitSeconds', plain)

    def test_waits_per_day_count_requests_and_seconds_without_content(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'today.jsonl'
            rows = [brake.wait_line(NOW, NOW - 600000, 's1', 'pitch-riot', 'subagent', 'stage 1', 'released'),
                    brake.wait_line(NOW, NOW - 3 * H, 's2', 'website', 'main', 'stage 2', 'capped'),
                    brake.wait_line(NOW, NOW - 5000, 's1', 'pitch-riot', 'main', 'stage 1', 'cancelled')]
            self.assertEqual(set(rows[0]), {'at', 'since', 'seconds', 'session', 'project', 'class', 'reason', 'end'})
            self.assertEqual(rows[0]['seconds'], 600)
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows) + 'kaputt\n')
            s = brake.wait_summary(path)
            self.assertEqual((s['requests'], s['seconds']), (3, 600 + 3 * 3600 + 5))
            self.assertEqual(s['ends'], {'released': 1, 'capped': 1, 'cancelled': 1})
            self.assertEqual(s['projects'], {'pitch-riot': 605, 'website': 3 * 3600})
            self.assertEqual(brake.wait_summary(Path(d) / 'missing.jsonl')['requests'], 0)


if __name__ == '__main__':
    unittest.main()
