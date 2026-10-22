"""Pool states that need the Conductor become one wake.log line each; files live in a temporary folder."""
import re
import tempfile
import unittest
from pathlib import Path

from conductor_alerts import Alerts, LINE

NOW = 1900000000
H = 3600


class Clock:
    def __init__(self): self.now = NOW
    def __call__(self): return self.now


class FakeServer:
    def __init__(self):
        self.health = {'failures': 0}
        self.last_accounts = [self.account('Intern'), self.account('Team')]
        self.stage = {'stage': 0, 'ratio': None, 'resetAt': None}

    def account(self, label, status='ready', pct=20):
        return {'name': label.lower(), 'label': label, 'status': status, 'fiveHour': {'pct': pct, 'resetsAt': (NOW + 2 * H) * 1000},
                'sevenDay': {'pct': 10, 'resetsAt': (NOW + 90 * H) * 1000}, 'quotaLimits': {}}

    def brake_info(self): return self.stage


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'conductor' / 'state').mkdir(parents=True)
        self.wake = self.root / 'conductor' / 'state' / 'wake.log'
        self.clock, self.server = Clock(), FakeServer()
        self.alerts = self.fresh()

    def fresh(self):
        return Alerts(self.wake, self.root / 'pool' / 'conductor-alerts.json', clock=self.clock)

    def lines(self):
        return self.wake.read_text().splitlines() if self.wake.exists() else []

    def test_a_state_wakes_once_and_its_end_is_routine(self):
        self.alerts.update('backend', True, 'Backend gestört', 'Backend antwortet wieder')
        self.clock.now += 60
        self.alerts.update('backend', True, 'Backend gestört', 'Backend antwortet wieder')
        self.clock.now += 60
        self.alerts.update('backend', False, 'Backend gestört', 'Backend antwortet wieder')
        start, end = self.lines()
        self.assertRegex(start, r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} pool backend an conductor: Backend gestört$')
        self.assertRegex(end, r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} pool backend vorbei: Backend antwortet wieder$')
        self.assertTrue(LINE.fullmatch(start))

    def test_a_second_episode_within_the_hour_stays_quiet_and_later_ones_wake_again(self):
        for at, active in [(0, True), (600, False), (1200, True), (1800, False), (H + 60, True)]:
            self.clock.now = NOW + at
            self.alerts.update('bremse', active, 'Bremse Stufe 2', 'Bremse unter Stufe 2')
        self.assertEqual([l.split(' ', 1)[1] for l in self.lines()],
                         ['pool bremse an conductor: Bremse Stufe 2', 'pool bremse vorbei: Bremse unter Stufe 2',
                          'pool bremse an conductor: Bremse Stufe 2'])

    def test_text_is_one_line_of_at_most_300_characters(self):
        self.alerts.update('anmeldung', True, 'zeile\neins ' + 'x' * 500, 'ok')
        sentence = self.lines()[0].split(': ', 1)[1]
        self.assertNotIn('\n', sentence); self.assertLessEqual(len(sentence), 300)

    def test_a_daemon_restart_continues_the_episode_without_a_second_wake(self):
        self.alerts.update('konten-limit', True, 'Alle Konten am Limit', 'Konten wieder frei')
        self.clock.now += 120
        self.fresh().update('konten-limit', True, 'Alle Konten am Limit', 'Konten wieder frei')
        self.assertEqual(len(self.lines()), 1)

    def test_without_a_conductor_folder_nothing_is_written(self):
        alerts = Alerts(self.root / 'missing' / 'state' / 'wake.log', self.root / 'pool' / 'other.json', clock=self.clock)
        alerts.update('backend', True, 'Backend gestört', 'Backend antwortet wieder')
        self.assertFalse((self.root / 'missing').exists())

    def test_check_reads_backend_accounts_and_brake_from_the_pool(self):
        self.server.health['failures'] = 3
        self.alerts.check(self.server)
        self.server.health['failures'] = 0
        self.server.last_accounts = [self.server.account('Intern', pct=100), self.server.account('Team', pct=100),
                                     self.server.account('Gmail', status='reauthenticate')]
        self.server.stage = {'stage': 2, 'ratio': .3, 'resetAt': (NOW + 2 * H) * 1000}
        self.clock.now += 30; self.alerts.check(self.server)
        kinds = [re.match(r'\S+ pool (\S+) (an conductor|vorbei):', l).group(1, 2) for l in self.lines()]
        self.assertEqual(kinds, [('backend', 'an conductor'), ('backend', 'vorbei'), ('konten-limit', 'an conductor'),
                                 ('anmeldung', 'an conductor'), ('bremse', 'an conductor')])
        text = '\n'.join(self.lines())
        self.assertIn('Gmail', text); self.assertIn('Stufe 2', text)
        self.server.last_accounts = [self.server.account('Intern'), self.server.account('Team')]
        self.server.stage = {'stage': 0, 'ratio': None, 'resetAt': None}
        self.clock.now += 30; self.alerts.check(self.server)
        ends = [l for l in self.lines() if ' vorbei: ' in l and ' backend ' not in l]
        self.assertEqual(len(ends), 3)
        self.assertIn('Intern', next(l for l in ends if 'konten-limit' in l))


if __name__ == '__main__':
    unittest.main()
