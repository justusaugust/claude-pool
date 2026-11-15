"""A long-lived token that runs out wakes the Conductor in time.

The scenario: every account serves a one-year token. Nothing reminds anyone, so a year later all of them stop on the same
day. The pool says so when 30 days are left and again when 7 are left, once per account, and says when it was renewed.
"""
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from conductor_alerts import Alerts
import long_lived
from fakes import long_lived_file, refreshable_file

NOW = 1_900_000_000
DAY = 86400


class Clock:
    def __init__(self): self.now = NOW
    def __call__(self): return self.now


class ExpiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.stores, self.replicas = Path(self.tmp.name) / 'stores', Path(self.tmp.name) / 'long'
        self.stores.mkdir(); self.replicas.mkdir(mode=0o700)

    def at(self, days): return (NOW + int(days * DAY)) * 1000

    def test_it_lists_tokens_inside_the_window_and_takes_the_served_one_first(self):
        long_lived_file(self.replicas, 'soon', expires_ms=self.at(12))
        long_lived_file(self.replicas, 'later', expires_ms=self.at(200))
        long_lived_file(self.stores, 'served', expires_ms=self.at(3))
        long_lived_file(self.replicas, 'served', expires_ms=self.at(300))       # a newer copy that is not served yet
        refreshable_file(self.stores, 'login', 'user-b', 'team-a')
        rows = long_lived.expiring(self.stores, self.replicas, NOW * 1000)
        self.assertEqual([(r['name'], int(r['days']), r['expiresAt']) for r in rows], [('served', 3, self.at(3)), ('soon', 12, self.at(12))])

    def test_missing_folders_and_unreadable_files_list_nothing(self):
        (self.replicas / 'broken.json').write_text('{')
        self.assertEqual(long_lived.expiring(self.stores / 'none', self.replicas, NOW * 1000), [])


class ExpiryAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root, self.clock = Path(self.tmp.name), Clock()
        (self.root / 'conductor' / 'state').mkdir(parents=True)
        self.wake = self.root / 'conductor' / 'state' / 'wake.log'
        self.alerts = Alerts(self.wake, self.root / 'alerts.json', clock=self.clock)
        self.until = NOW + 40 * DAY

    def lines(self): return [l.split(' ', 1)[1] for l in self.wake.read_text().splitlines()] if self.wake.exists() else []

    def tick(self, days=0):
        self.clock.now += int(days * DAY)
        left = (self.until - self.clock.now) / DAY
        rows = [{'name': 'intern', 'expiresAt': self.until * 1000, 'days': left}] if left <= 30 else []
        self.alerts.expiry(rows, {'intern': 'Intern'})

    def test_a_year_runs_out_two_wakes_then_the_renewal(self):
        day = time.strftime('%d.%m.%Y', time.localtime(self.until))
        self.tick(); self.tick(5)
        self.assertEqual(self.lines(), [], 'more than 30 days left: silence')
        self.tick(6)                                                   # 29 days left
        self.assertEqual(self.lines(), ['pool ablauf an conductor: Konto Intern: Jahres-Token läuft am ' + day + ' ab, noch 29 Tage. '
                                        'Auf der Anmeldeseite erneuern'])
        for _ in range(20): self.tick(1)                               # 9 days left: nothing new, day after day
        self.assertEqual(len(self.lines()), 1)
        self.tick(3)                                                   # 6 days left
        self.assertEqual(self.lines()[1:], ['pool ablauf an conductor: Konto Intern: Jahres-Token läuft am ' + day + ' ab, noch 6 Tage. '
                                            'Auf der Anmeldeseite erneuern'])
        self.tick(1); self.tick(1)
        self.assertEqual(len(self.lines()), 2, 'the second stage wakes once, too')
        self.until = self.clock.now + 365 * DAY                        # renewed
        self.tick()
        self.assertEqual(self.lines()[2:], ['pool ablauf vorbei: Konto Intern: Jahres-Token erneuert'])
        self.tick(1)
        self.assertEqual(len(self.lines()), 3)

    def test_a_token_already_past_its_day_says_so(self):
        self.until = NOW - DAY
        self.tick()
        self.assertEqual([l.split('Jahres-Token ')[1] for l in self.lines()], ['ist abgelaufen. Auf der Anmeldeseite erneuern'])

    def test_each_account_has_its_own_episode(self):
        rows = [{'name': n, 'expiresAt': (NOW + 10 * DAY) * 1000, 'days': 10} for n in ('intern', 'sofi')]
        self.alerts.expiry(rows, {}); self.alerts.expiry(rows, {})
        self.assertEqual([l.split(':')[1].strip() for l in self.lines()], ['Konto intern', 'Konto sofi'])


if __name__ == '__main__': unittest.main()
