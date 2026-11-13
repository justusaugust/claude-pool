"""Pool states that need the Conductor become one wake.log line each; files live in a temporary folder."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import conductor_alerts
from conductor_alerts import Alerts, LINE
from remote import Completed

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
        self.blocked = {}

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

    def test_an_account_anthropic_refuses_wakes_the_conductor_with_its_label_and_next_try(self):
        self.server.blocked = {'intern': {'until': (NOW + 6 * H) * 1000, 'code': 'oauth_not_allowed_for_organization'}}
        self.alerts.check(self.server)
        self.clock.now += 30; self.alerts.check(self.server)
        self.server.blocked = {}
        self.clock.now += 30; self.alerts.check(self.server)
        start, end = self.lines()
        clock = __import__('time').strftime('%H:%M', __import__('time').localtime(NOW + 6 * H))
        self.assertRegex(start, r' pool gesperrt an conductor: Konto Intern: Anthropic lässt .*oauth_not_allowed_for_organization.* pausiert bis ' + clock)
        self.assertRegex(end, r' pool gesperrt vorbei: ')
        self.assertTrue(LINE.fullmatch(start) and LINE.fullmatch(end))
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
        self.assertIn('Gmail', text); self.assertIn('Stufe 2', text); self.assertNotIn('Sonnet', text)
        self.server.last_accounts = [self.server.account('Intern'), self.server.account('Team')]
        self.server.stage = {'stage': 0, 'ratio': None, 'resetAt': None}
        self.clock.now += 30; self.alerts.check(self.server)
        ends = [l for l in self.lines() if ' vorbei: ' in l and ' backend ' not in l]
        self.assertEqual(len(ends), 3)
        self.assertIn('Intern', next(l for l in ends if 'konten-limit' in l))


if __name__ == '__main__':
    unittest.main()


DAY = 86400


class Journal:
    def __init__(self): self.rows = []
    def record(self, code, **details): self.rows.append((code, details))


class SignInTests(unittest.TestCase):
    """Sign-in alerts are one episode per account; a device that is not the Conductor's delivers every line to it."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock, self.server, self.journal = Clock(), FakeServer(), Journal()
        self.sent, self.up = [], True
        self.alerts = Alerts(self.root / 'conductor' / 'state' / 'wake.log', self.root / 'alerts.json', clock=self.clock,
                             device=lambda: 'laptop', route=lambda: self.send, journal=self.journal)

    def send(self, text, key):
        if not self.up: return False
        self.sent.append((text, key)); return True

    def fail(self, *labels, status='reauthenticate'):
        names = {l.lower(): status for l in labels}
        self.server.last_accounts = [self.server.account(l, status=names.get(l.lower(), 'ready')) for l in ['Intern', 'Team', 'Gmail', 'Ops']]

    def test_replay_one_account_fails_and_two_days_later_three_more_each_line_reaches_the_conductor(self):
        self.fail('Intern'); self.alerts.check(self.server)
        self.clock.now += 2 * DAY
        self.fail('Intern', 'Team', 'Gmail', 'Ops'); self.alerts.check(self.server)
        self.clock.now += 30; self.alerts.check(self.server)
        texts = [t for t, _ in self.sent if ' pool anmeldung an conductor: ' in t]
        self.assertEqual(len(texts), 4)
        for text, label, name in zip(texts, ['Intern', 'Team', 'Gmail', 'Ops'], ['intern', 'team', 'gmail', 'ops']):
            self.assertTrue(LINE.fullmatch(text), text)
            for part in ['laptop', 'Konto ' + label, 'claude-pool login ' + name]: self.assertIn(part, text)
        self.assertEqual(len({key for _, key in self.sent}), len(self.sent))

    def test_each_account_has_its_own_quiet_window_and_its_own_end_line(self):
        self.fail('Intern'); self.alerts.check(self.server)
        self.clock.now += 600; self.fail('Intern', 'Team'); self.alerts.check(self.server)
        self.assertEqual(sum(' pool anmeldung an conductor: ' in t for t, _ in self.sent), 2)
        self.clock.now += 600; self.fail('Team'); self.alerts.check(self.server)
        ends = [t for t, _ in self.sent if ' pool anmeldung vorbei: ' in t]
        self.assertEqual(len(ends), 1); self.assertIn('Intern', ends[0]); self.assertIn('laptop', ends[0])

    def test_a_failed_delivery_is_retried_while_the_alert_is_active_and_journaled(self):
        self.up = False
        self.fail('Intern'); self.alerts.check(self.server)
        self.assertEqual(self.sent, [])
        self.assertEqual([c for c, _ in self.journal.rows if c == 'alert_forward_failed'], ['alert_forward_failed'])
        self.up = True
        self.clock.now += 3600; self.alerts.check(self.server)
        self.assertEqual(len(self.sent), 1)
        self.clock.now += 3600; self.alerts.check(self.server)
        self.assertEqual(len(self.sent), 1)

    def test_every_kind_is_delivered_with_the_device_name(self):
        self.alerts.update('bremse', True, 'Bremse Stufe 2', 'Bremse unter Stufe 2')
        (text, _), = self.sent
        self.assertTrue(LINE.fullmatch(text)); self.assertIn('pool bremse an conductor: ', text); self.assertIn('laptop', text)
        self.assertFalse((self.root / 'conductor').exists())

    def test_the_journal_records_when_an_account_needs_sign_in_and_when_it_is_valid_again(self):
        self.fail('Team'); self.alerts.check(self.server)
        self.fail(); self.clock.now += 30; self.alerts.check(self.server)
        self.assertEqual([r for r in self.journal.rows if r[0].startswith('account_')],
                         [('account_needs_login', {'account': 'team'}), ('account_login_valid', {'account': 'team'})])

    def test_nothing_is_delivered_without_a_route_and_the_line_goes_to_the_local_file_as_before(self):
        (self.root / 'conductor' / 'state').mkdir(parents=True)
        alerts = Alerts(self.root / 'conductor' / 'state' / 'wake.log', self.root / 'a2.json', clock=self.clock, route=lambda: None)
        alerts.update('bremse', True, 'Bremse Stufe 2', 'Bremse unter Stufe 2')
        self.assertEqual(self.sent, [])
        self.assertEqual(len((self.root / 'conductor' / 'state' / 'wake.log').read_text().splitlines()), 1)


class RouteTests(unittest.TestCase):
    """The route is the Conductor's inbox over the registry's ssh target; it exists only on another device."""
    def setUp(self):
        self.calls, self.result = [], Completed(0, b'queued', b'')
        outer = self
        class Fake:
            def __init__(self, target): self.target = target
            def pool(self, *args, **kw): outer.calls.append((self.target, args, kw)); return outer.result
        self.fake = Fake
        self.value = {'devices': [{'name': 'laptop', 'ssh': 'me@laptop'}, {'name': 'vps', 'ssh': 'me@vps'}], 'accounts': [],
                      'conductor': {'device': 'laptop'}}

    def route(self, me='vps', value='default'):
        value = self.value if value == 'default' else value
        with patch('registry.load', return_value=value), patch('registry.self_name', return_value=me):
            return conductor_alerts.conductor_route(remote=self.fake)()

    def test_another_device_sends_through_the_conductors_inbox_with_a_bounded_timeout(self):
        send = self.route()
        self.assertTrue(send('line text', 'pool-anmeldung-x'))
        (target, args, kw), = self.calls
        self.assertEqual((target, args), ('me@laptop', ('conductor', 'inbox', 'add', '--key', 'pool-anmeldung-x', 'line text')))
        self.assertLessEqual(kw['timeout'], 30)

    def test_a_failed_or_crashing_transport_returns_false(self):
        send = self.route(); self.result = Completed(255, b'', b'timed out')
        self.assertFalse(send('t', 'k'))
        self.fake.pool = lambda *a, **k: (_ for _ in ()).throw(ValueError('x'))
        self.assertFalse(send('t', 'k'))

    def test_the_conductor_device_itself_a_missing_conductor_or_registry_has_no_route(self):
        self.assertIsNone(self.route(me='laptop'))
        self.assertIsNone(self.route(value={**self.value, 'conductor': None}))
        self.assertIsNone(self.route(me=None))
        with patch('registry.load', side_effect=ValueError('x')), patch('registry.self_name', return_value='vps'):
            self.assertIsNone(conductor_alerts.conductor_route(remote=self.fake)())
