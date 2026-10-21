import json, tempfile, unittest
from pathlib import Path
from brake import Brake, stage, usd, ledger_line, today_summary, SONNET

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


class BrakeTests(unittest.TestCase):
    def setUp(self):
        self.brake = Brake()
        self.opus = {'model': 'claude-opus-5-5', 'messages': [{'role': 'user', 'content': 'go'}], 'output_config': {'effort': 'xhigh'},
                     'thinking': {'type': 'adaptive'}, 'speed': 'fast', 'stream': True}

    def decide(self, request, level, cls='subagent', agent='a1', project='website', urgent=()):
        headers = {'x-claude-code-request-class': cls, 'x-claude-code-agent-id': agent}
        return self.brake.decide(headers, request, {'stage': level}, project, urgent)

    def test_no_brake_leaves_requests_alone(self):
        self.assertIsNone(self.decide(dict(self.opus), 0)[0])

    def test_a_new_subagent_starts_on_sonnet_and_stays_there(self):
        new, decision = self.decide(dict(self.opus), 1)
        self.assertEqual((new['model'], decision['model']), (SONNET, SONNET))
        self.assertNotIn('speed', new)
        self.assertEqual(new['output_config']['effort'], 'xhigh')
        later = dict(self.opus, messages=[{'role': 'user', 'content': 'go'}] * 7)
        self.assertEqual(self.decide(later, 0)[0]['model'], SONNET, 'no switch back mid-conversation')

    def test_a_conversation_already_running_keeps_its_model(self):
        running = dict(self.opus, messages=[{'role': 'user', 'content': 'go'}] * 5)
        self.assertIsNone(self.decide(running, 2, agent='seen-mid')[0])
        self.assertIsNone(self.decide(dict(self.opus), 2, agent='seen-mid')[0], 'sticky keep')

    def test_conductor_urgent_projects_main_sessions_and_small_models_are_never_braked(self):
        self.assertIsNone(self.decide(dict(self.opus), 2, project='conductor-wecker')[0])
        self.assertIsNone(self.decide(dict(self.opus), 2, agent='a2', project='pitch-riot', urgent={'pitch-riot'})[0])
        self.assertIsNone(self.decide(dict(self.opus), 2, cls='main', agent='')[0])
        self.assertIsNone(self.decide(dict(self.opus), 2, cls='compaction', agent='a3')[0])
        self.assertIsNone(self.decide(dict(self.opus, model='claude-haiku-5-5'), 2, agent='a4')[0])

    def test_workflow_agents_brake_too_and_stage_two_caps_effort(self):
        new, _ = self.decide(dict(self.opus), 2, cls='workflow', agent='w1')
        self.assertEqual((new['model'], new['output_config']['effort']), (SONNET, 'high'))


class LedgerTests(unittest.TestCase):
    def test_usd_weighs_tokens_by_model_price(self):
        usage = {'input_tokens': 1000000, 'output_tokens': 100000, 'cache_read_input_tokens': 1000000, 'cache_creation_input_tokens': 0}
        self.assertAlmostEqual(usd('claude-opus-5-5', usage), 4 + 2 + 0.2)
        self.assertAlmostEqual(usd('claude-sonnet-5-5', usage), 2 + 1 + 0.2)
        self.assertAlmostEqual(usd('claude-fable-5-1', usage), 10 + 5 + 0.25)

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
            self.assertNotIn('content', json.dumps(rows), 'no prompt content in the ledger')


if __name__ == '__main__':
    unittest.main()
