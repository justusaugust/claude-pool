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

    def test_usage_reads_only_the_tail_of_a_large_monthly_history(self):
        history = self.runtime / 'usage-history'; history.mkdir()
        path = history / (time.strftime('%Y-%m') + '.jsonl')
        with path.open('w') as file:
            for at in range(5000):  # earlier days of the month: about 10 MB of rows with a brake
                file.write(json.dumps({'at': at, 'forecast': {'brake': {'stage': at % 3, 'ratio': .5}, 'pad': 'y' * 1900}}) + '\n')
            file.write(json.dumps({'at': 5000, 'forecast': {'brake': {'stage': 2, 'ratio': .9, 'resetAt': 7}}}) + '\n')
            for at in range(100):  # 200 KB of newer rows without one: several blocks back from the end
                file.write(json.dumps({'at': 5001 + at, 'forecast': {'pad': 'z' * 1900}}) + '\n')
        full = next((json.loads(l).get('forecast', {}).get('brake') for l in reversed(path.read_text().splitlines()) if '"brake"' in l), None)
        read, real = [], io.open
        class Counted:
            def __init__(self, inner): self.inner = inner
            def read(self, *args):
                data = self.inner.read(*args); read.append(len(data)); return data
            def __enter__(self): return self
            def __exit__(self, *error): self.inner.close()
            def __getattr__(self, name): return getattr(self.inner, name)
        def counting(file, *args, **kwargs):
            handle = real(file, *args, **kwargs)
            return Counted(handle) if str(file) == str(path) else handle
        with mock.patch('builtins.open', counting), mock.patch('io.open', counting):
            value = json.loads(self.run_cli('usage', '--json')[1])
        self.assertEqual(value['brake'], full)
        self.assertEqual(value['brake'], {'stage': 2, 'ratio': .9, 'resetAt': 7})
        self.assertGreater(path.stat().st_size, 8 << 20)
        self.assertLess(sum(read), 1 << 20, 'read %d bytes of a %d byte file' % (sum(read), path.stat().st_size))

    def test_the_tail_read_finds_the_line_a_full_read_finds(self):
        text = ('{"brake": 1}\n\n{"a": "ü€ brake"}\r\n{"brake": "\\u00fc", "x": "ä"}\r{"no": 1}\n{"brake": 3}\n'
                '{"x": "brake ' + 'ö' * 50 + '"}\n{"brake": 4, "y": "' + 'é' * 30 + '"}\n{"z": 1}')
        folder = Path(self.temp.name)
        for name, content in [('mixed', text), ('trailing', text + '\n'), ('partial', text + '\n{"brake": 9'), ('empty', ''),
                              ('none', '{"a": 1}\n{"b": 2}\n'), ('only', '{"brake": 7}'), ('blank', '\n\n\n')]:
            path = folder / (name + '.jsonl'); path.write_bytes(content.encode())
            for needle in ('"brake"', 'brake', '"x"', 'ü', '{', 'missing'):
                lines = path.read_text().splitlines()
                full = next((line for line in reversed(lines) if needle in line), None)
                for block in (1, 2, 3, 5, 8, 13, 64, 1 << 16):
                    with self.subTest(name=name, needle=needle, block=block):
                        self.assertEqual(ct.last_line_with(path, needle, block), full)
        with self.assertRaises(FileNotFoundError): ct.last_line_with(folder / 'gone.jsonl', '"brake"')

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
