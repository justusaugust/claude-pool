"""Conductor tools for the brake: urgent projects and today's usage ledger, on a temporary state directory."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import conductor_tools as ct
from brake import ledger_line


class ConductorBrakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        patcher = mock.patch.object(ct.settings, 'RUNTIME', self.runtime)
        patcher.start(); self.addCleanup(patcher.stop); self.addCleanup(self.temp.cleanup)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): code = ct.cli(list(args))
        return code, out.getvalue()

    def test_urgent_marks_a_project_for_some_hours_and_can_be_lifted(self):
        code, line = self.run_cli('urgent', 'pitch-riot', '--hours', '2')
        self.assertEqual(code, 0); self.assertIn('pitch-riot', line)
        until = json.loads((self.runtime / 'brake.json').read_text())['urgent']['pitch-riot']
        self.assertAlmostEqual(until, time.time() + 7200, delta=5)
        self.assertIn('pitch-riot', self.run_cli('urgent', '--list')[1])
        self.assertEqual(self.run_cli('urgent', '--off', 'pitch-riot')[0], 0)
        self.assertEqual(json.loads((self.runtime / 'brake.json').read_text())['urgent'], {})

    def test_usage_reports_today_by_project_with_the_brake_stage(self):
        (self.runtime / 'usage-ledger').mkdir()
        rows = [ledger_line(1, 's', 'pitch-riot', 'subagent', 'claude-sonnet-5-5', 'intern', {'output_tokens': 1000000}, braked=True),
                ledger_line(2, 's', 'website', 'main', 'claude-opus-5-5', 'team', {'output_tokens': 100000})]
        (self.runtime / 'usage-ledger' / (time.strftime('%Y-%m-%d') + '.jsonl')).write_text(''.join(json.dumps(r) + '\n' for r in rows))
        (self.runtime / 'usage-history').mkdir()
        (self.runtime / 'usage-history' / (time.strftime('%Y-%m') + '.jsonl')).write_text(
            json.dumps({'at': 1, 'forecast': {'brake': {'stage': 1, 'ratio': .7, 'resetAt': 5}}}) + '\n')
        code, out = self.run_cli('usage', '--json')
        value = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([p['project'] for p in value['projects']], ['pitch-riot', 'website'])
        self.assertEqual((value['brake']['stage'], value['braked']), (1, 1))
        self.assertEqual([(r['session'], r['project'], round(r['usd'], 3)) for r in value['sessions']], [('s', 'pitch-riot', 12.0)])
        self.assertEqual(set(value['sessions'][0]['classes']), {'subagent', 'main'})
        text = self.run_cli('usage')[1]
        self.assertIn('pitch-riot', text); self.assertIn('Brake · stage 1: forecast ends before the next reset', text); self.assertNotIn('Sonnet', text)

    def test_usage_counts_todays_brake_waits(self):
        from brake import wait_line
        (self.runtime / 'brake-waits').mkdir()
        now = int(time.time() * 1000)
        rows = [wait_line(now, now - 35 * 60000, 's', 'pitch-riot', 'subagent', 'stage 1', 'released'),
                wait_line(now, now - 30 * 60000, 's', 'pitch-riot', 'main', 'stage 1', 'released')]
        (self.runtime / 'brake-waits' / (time.strftime('%Y-%m-%d') + '.jsonl')).write_text(''.join(json.dumps(r) + '\n' for r in rows))
        value = json.loads(self.run_cli('usage', '--json')[1])
        self.assertEqual((value['waits']['requests'], value['waits']['seconds']), (2, 65 * 60))
        self.assertIn('Background waits today · 2 requests · 1h 05m', self.run_cli('usage')[1])

    def test_usage_counts_todays_agent_labels(self):
        import short_label
        self.assertNotIn('Agent labels', self.run_cli('usage')[1], 'no label requests today: no line')
        (self.runtime / 'short-labels').mkdir()
        rows = [short_label.count_line(1, r) for r in ['short'] * 40 + ['native'] * 2 + ['kept', 'unmatched', 'refused', 'other', 'other']]
        (self.runtime / 'short-labels' / (time.strftime('%Y-%m-%d') + '.jsonl')).write_text(''.join(json.dumps(r) + '\n' for r in rows))
        value = json.loads(self.run_cli('usage', '--json')[1])
        self.assertEqual(value['labels'], {'short': 40, 'native': 2, 'kept': 1, 'unmatched': 1, 'other': 2, 'refused': 1})
        self.assertIn('Agent labels today · 41 short · 3 in full', self.run_cli('usage')[1])


if __name__ == '__main__':
    unittest.main()
