"""Reserve window: an account reserved only during set hours of the day in its own time zone, and a soft reserve that
still takes work once no other account can (2.11.: Intern from 06:00 to 24:00 only up to 90 %)."""
import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import account_metadata
import accounts_cli as setup
import forecast
import registry
import routing
import settings
import views

ZONE = 'Europe/Stockholm'
WINDOW = {'reserveAt': 90, 'reserveHours': [6, 24], 'reserveTimezone': ZONE, 'reserveSoft': True}
BASE = ('[providers.anthropic]\nkind = "anthropic"\nauth = "claude_oauth"\n\n'
        '[[providers.anthropic.accounts]]\nname = "ops"\n\n'
        '[[providers.anthropic.accounts]]\nname = "intern"\n\n'
        '[[providers.anthropic.accounts]]\nname = "team"\nmax_utilization = 0.75\npriority = 125\n\n[server]\nbind = "x"\n')


def at(text):
    """Wall time in Stockholm as epoch seconds."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(ZONE)).timestamp()


DAY, NIGHT = at('2026-11-03T10:00'), at('2026-11-03T05:30')


def account(name, five=None, week=None, status='ready', **extra):
    window = lambda pct: None if pct is None else {'pct': pct, 'resetsAt': None}
    return {'name': name, 'label': name.title(), 'status': status, 'fiveHour': window(five), 'sevenDay': window(week),
            'fableWeekly': None, **extra}


def intern(five=None, **extra): return account('intern', five, **{**WINDOW, **extra})


def decide(accounts, now, previous=None, pin=None, seconds=None):
    seconds = seconds or {a['name']: 1000 for a in accounts}
    return routing.choose(accounts, {'s1': {'active': previous, 'pin': pin}}, 's1',
                          {'accounts': [{'name': n, 's': 0, 'seconds': s} for n, s in seconds.items()]}, now)


def shunt(code):
    return '#!' + sys.executable + '\nimport sys\nsys.exit(' + str(code) + ' if "check" in sys.argv else 3)\n'


class WindowTests(unittest.TestCase):
    def test_the_reserve_holds_from_six_to_midnight_in_its_own_time_zone(self):
        row = intern()
        for when, limit in (('2026-11-03T05:59:59', 0), ('2026-11-03T06:00', 90), ('2026-11-03T23:59:59', 90),
                            ('2026-11-04T00:00', 0), ('2026-07-01T06:30', 90), ('2026-07-01T05:30', 0)):
            with self.subTest(when=when):
                self.assertEqual(routing.reserve_limit(row, at(when)), limit)
        # 05:30 UTC is 06:30 in Stockholm in winter (UTC+1): the hours are Stockholm's, not the machine's.
        self.assertEqual(routing.reserve_limit(row, datetime.fromisoformat('2026-11-03T05:30+00:00').timestamp()), 90)

    def test_reserves_without_hours_hold_all_day(self):
        for when in (NIGHT, DAY):
            self.assertEqual(routing.reserve_limit(account('team', reserveAt=75), when), 75)
            self.assertEqual(routing.reserve_limit(account('ops'), when), 0)
            self.assertEqual(routing.reserve_limit(account('ops', reserveAt=0), when), 0)

    def test_before_six_there_is_no_reserve(self):
        self.assertFalse(routing.reserve_blocked(intern(93), NIGHT))
        self.assertTrue(routing.eligible(intern(93), NIGHT))
        # Not the last choice either: at night Intern competes like any account.
        rows = [intern(10), account('ops', 60)]
        self.assertEqual(decide(rows, NIGHT, seconds={'intern': 99999, 'ops': 10})['account'], 'intern')
        self.assertEqual(decide([intern(93), account('ops', 40)], NIGHT, previous='intern')['account'], 'intern')

    def test_from_six_to_midnight_it_stops_at_90_while_another_account_has_room(self):
        self.assertTrue(routing.reserve_blocked(intern(90), DAY))
        self.assertFalse(routing.reserve_blocked(intern(89.9), DAY))
        rows = [intern(90), account('ops', 40)]
        for kwargs in ({}, {'previous': 'intern'}, {'pin': 'intern'}):
            with self.subTest(**kwargs):
                self.assertEqual(decide(rows, DAY, **kwargs)['account'], 'ops')
        self.assertEqual(decide(rows, DAY, previous='intern')['reason'], 'reserved account')
        self.assertEqual(routing.unavailable(intern(90), DAY), 'reserved account')
        # Below 90 % it is the last choice during the day, like every reserved account.
        self.assertEqual(decide([intern(10), account('ops', 60)], DAY, seconds={'intern': 99999, 'ops': 10})['account'], 'ops')

    def test_when_every_other_account_is_full_intern_may_continue(self):
        rows = [intern(92), account('ops', 96), account('team', 80, reserveAt=75), account('sofi', status='paused')]
        for kwargs in ({}, {'previous': 'intern'}, {'pin': 'intern'}):
            with self.subTest(**kwargs):
                self.assertEqual(decide(rows, DAY, **kwargs)['account'], 'intern')
        # Its quota still ends it, and so does the hard reserve of another account.
        rows[0] = intern(96)
        self.assertIsNone(decide(rows, DAY)['account'])
        self.assertEqual(decide(rows, DAY)['reason'], 'no eligible account')

    def test_other_reserves_stay_hard(self):
        # Team (all day, 75 %) and a hard window reserve never take work above their limit, even when nothing else can.
        self.assertIsNone(decide([account('team', 80, reserveAt=75), account('ops', 97)], NIGHT, previous='team')['account'])
        hard = account('gmail', 85, reserveAt=80, reserveHours=[6, 24], reserveTimezone=ZONE)
        self.assertIsNone(decide([hard, account('ops', 97)], DAY, previous='gmail')['account'])
        self.assertEqual(decide([hard, account('ops', 97)], NIGHT, previous='gmail')['account'], 'gmail')

    def test_the_status_text_names_the_window(self):
        self.assertEqual(routing.reserve_text(intern(), DAY), 'reserved from 90% until 24:00')
        self.assertEqual(routing.reserve_text(intern(), NIGHT), 'reserved from 90% from 06:00')
        self.assertEqual(routing.reserve_text(account('team', reserveAt=75), DAY), 'reserved from 75%')
        self.assertEqual(routing.reserve_text(account('ops'), DAY), '')


class ShuntConfigTests(unittest.TestCase):
    """Shunt has no clock: the pool writes each window's options for the current hour and switches them at its edges."""

    def config(self, accounts, now):
        with patch.object(settings, 'ACCOUNTS', accounts), patch.object(settings, 'BACKEND_PORT', 18340):
            return settings.backend_config(Path('/state'), now=now)

    def blocks(self, text):
        return {r['name']: r for r in setup.tomllib.loads(text)['providers']['anthropic']['accounts']}

    def test_backend_config_writes_the_window_for_the_current_hour(self):
        rows = [{'name': 'ops'}, {'name': 'intern', **WINDOW}, {'name': 'team', 'reserveAt': 75},
                {'name': 'gmail', 'reserveAt': 80, 'reserveHours': [6, 24], 'reserveTimezone': ZONE}]
        day, night = self.blocks(self.config(rows, DAY)), self.blocks(self.config(rows, NIGHT))
        # Soft: only the order (Shunt uses it once every other account is out), no cap, so the pool may still send work.
        self.assertEqual(day['intern'], {'name': 'intern', 'priority': 110})
        self.assertEqual(night['intern'], {'name': 'intern'})
        self.assertEqual(day['gmail'], {'name': 'gmail', 'max_utilization': 0.8, 'priority': 120})
        self.assertEqual(night['gmail'], {'name': 'gmail'})
        for blocks in (day, night):
            self.assertEqual(blocks['team'], {'name': 'team', 'max_utilization': 0.75, 'priority': 125})
            self.assertEqual(blocks['ops'], {'name': 'ops'})
        self.assertEqual(settings.backend_config.__defaults__, (settings.RUNTIME,))

    def test_the_window_switches_at_its_edges_and_touches_nothing_else(self):
        accounts = [{'name': 'ops'}, {'name': 'intern', **WINDOW}, {'name': 'team', 'reserveAt': 75}]
        self.assertEqual(setup.reserve_window_toml(BASE, accounts, at('2026-11-03T05:59')), BASE)
        day = setup.reserve_window_toml(BASE, accounts, at('2026-11-03T06:00'))
        self.assertEqual(day, BASE.replace('name = "intern"\n', 'name = "intern"\npriority = 110\n'))
        self.assertEqual(setup.reserve_window_toml(day, accounts, at('2026-11-03T23:00')), day)
        self.assertEqual(setup.reserve_window_toml(day, accounts, at('2026-11-04T00:00')), BASE)

    def test_apply_reserve_windows_rewrites_the_runtime_file_only_when_the_hour_changes_it(self):
        with tempfile.TemporaryDirectory() as folder:
            config, toml = Path(folder) / 'config.json', Path(folder) / 'shunt.toml'
            config.write_text(json.dumps({'accounts': [{'name': 'ops'}, {'name': 'intern', **WINDOW},
                                                       {'name': 'team', 'reserveAt': 75}]}))
            toml.write_text(BASE)
            self.assertFalse(setup.apply_reserve_windows(config, [toml], NIGHT))
            self.assertEqual(toml.read_text(), BASE)
            self.assertTrue(setup.apply_reserve_windows(config, [toml], DAY))
            self.assertIn('name = "intern"\npriority = 110\n', toml.read_text())
            self.assertFalse(setup.apply_reserve_windows(config, [toml], DAY + 60))
            self.assertTrue(setup.apply_reserve_windows(config, [toml], at('2026-11-04T00:00')))
            self.assertEqual(toml.read_text(), BASE)


class ConfigTests(unittest.TestCase):
    def load(self, rows):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'accounts': rows}))
            return account_metadata.load(path), account_metadata.decorate({'accounts': [{'name': r['name']} for r in rows]}, path, now=DAY)

    def test_a_window_is_accepted_and_reaches_the_overview(self):
        value, data = self.load([{'name': 'intern', **WINDOW}, {'name': 'team', 'reserveAt': 75}, {'name': 'ops'}])
        self.assertEqual(value['accounts'][0]['reserveHours'], [6, 24])
        self.assertEqual(data['accounts'], [{'name': 'intern', **WINDOW, 'reserveText': 'reserved from 90% until 24:00'},
                                            {'name': 'team', 'reserveAt': 75},
                                            {'name': 'ops'}])

    def test_a_bad_window_is_refused(self):
        bad = [{'reserveHours': [6]}, {'reserveHours': [24, 6]}, {'reserveHours': [6, 6]}, {'reserveHours': [6, 25]},
               {'reserveHours': [-1, 6]}, {'reserveHours': ['6', '24']}, {'reserveHours': [6.0, 24]}, {'reserveHours': '6-24'},
               {'reserveTimezone': 'Mars/Olympus'}, {'reserveTimezone': 7}, {'reserveTimezone': None},
               {'reserveSoft': 'yes'}, {'reserveSoft': 1}]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load([{'name': 'intern', **WINDOW, **change}])
        for missing in ({'reserveHours': [6, 24]}, {'reserveTimezone': ZONE}):  # hours need a zone, a zone needs hours
            with self.subTest(only=missing), self.assertRaises(ValueError):
                self.load([{'name': 'intern', 'reserveAt': 90, **missing}])

    def test_the_registry_carries_the_window(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'devices.json'
            registry.update(lambda v: v['accounts'].append({'name': 'intern', 'email': 'a@b.de', **WINDOW}), path)
            self.assertEqual({k: registry.account(registry.load(path), 'intern')[k] for k in WINDOW}, WINDOW)
            for change in ({'reserveHours': [24, 6]}, {'reserveTimezone': 'Mars/Olympus'}, {'reserveSoft': 'yes'}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    registry.update(lambda v: registry.account(v, 'intern').update(change), path)


class ForecastTests(unittest.TestCase):
    def estimate(self, now):
        ms = int(now * 1000)
        row = {'id': 'intern', 'name': 'intern', 'label': 'Intern', 'seenAt': ms, 'status': 'ready', **WINDOW,
               'fiveHour': {'pct': 59, 'resetsAt': ms + 3600000}, 'sevenDay': None}
        points = [{'at': ms - 1200000 + i * 600000, 'fiveHour': {'pct': v, 'resetsAt': ms + 3600000}, 'sevenDay': None}
                  for i, v in enumerate([55, 57, 59])]
        return forecast.estimate([row], {'intern': points}, ms)['accounts'][0]['seconds']

    def test_the_forecast_counts_to_the_reserve_only_inside_the_window(self):
        self.assertEqual(self.estimate(DAY), round((90 - 59) * 3600 / 12))
        self.assertEqual(self.estimate(NIGHT), round((95 - 59) * 3600 / 12))


class StatusLineTests(unittest.TestCase):
    def text(self, now, **reserve):
        row = {'id': 'intern', 'name': 'intern', 'label': 'Intern', 'status': 'ready', 'seenAt': int(now * 1000), 'plan': 'Max',
               'fiveHour': {'pct': 40, 'resetsAt': None}, 'sevenDay': None, 'fableWeekly': None, **reserve}
        ms = int(now * 1000)
        data = {'updatedAt': ms, 'ready': 1, 'accounts': [row], 'forecast': forecast.estimate([row], {}, ms), 'history': []}
        return views.usage(data, width=96)

    def test_the_usage_view_shows_the_window(self):
        self.assertIn('Intern · ready · reserved from 90% until 24:00', self.text(DAY, **WINDOW))
        self.assertIn('Intern · ready · reserved from 90% from 06:00', self.text(NIGHT, **WINDOW))
        self.assertIn('Intern · ready · reserved from 75%', self.text(DAY, reserveAt=75))


class ReserveCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfg = root / 'cfg'; self.cfg.mkdir(mode=0o700)
        self.runtime = root / 'runtime'; self.runtime.mkdir()
        self.stores = root / 'stores'; self.stores.mkdir(mode=0o700)
        self.config = self.cfg / 'config.json'
        self.config.write_text(json.dumps({'accounts': [{'name': 'ops'}, {'name': 'intern', 'label': 'Intern'},
                                                        {'name': 'team', 'reserveAt': 75}]}))
        self.reg = self.cfg / 'devices.json'
        registry.update(lambda v: v['accounts'].append({'name': 'intern', 'email': 'a@b.de'}), self.reg)
        self.toml = self.runtime / 'shunt.toml'; self.toml.write_text(BASE)
        self.binary = root / 'fake-shunt'; self.binary.write_text(shunt(0)); self.binary.chmod(0o700)

    def run_cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, patch('time.time', return_value=DAY):
            code = setup.cli(list(argv), config=self.config, stores=self.stores, binary=self.binary, runtime=self.runtime,
                             registry_path=self.reg, on_change=lambda: {})
        return code, out.getvalue()

    def row(self, name): return next(r for r in json.loads(self.config.read_text())['accounts'] if r['name'] == name)

    def test_reserve_sets_the_window_writes_this_hour_and_follows_in_the_registry(self):
        code, _ = self.run_cli('reserve', 'intern', '--at', '90', '--hours', '6-24', '--timezone', ZONE, '--soft')
        self.assertEqual(code, 0)
        self.assertEqual({k: self.row('intern')[k] for k in WINDOW}, WINDOW)
        self.assertIn('name = "intern"\npriority = 110\n', self.toml.read_text())
        self.assertEqual(self.row('team'), {'name': 'team', 'reserveAt': 75})
        self.assertEqual({k: registry.account(registry.load(self.reg), 'intern')[k] for k in WINDOW}, WINDOW)

    def test_a_plain_reserve_clears_an_earlier_window(self):
        self.run_cli('reserve', 'intern', '--at', '90', '--hours', '6-24', '--timezone', ZONE, '--soft')
        self.run_cli('reserve', 'intern', '--at', '85')
        self.assertEqual({k: v for k, v in self.row('intern').items() if k.startswith('reserve')}, {'reserveAt': 85})
        self.assertIn('name = "intern"\nmax_utilization = 0.85\npriority = 115\n', self.toml.read_text())

    def test_bad_window_arguments_are_refused_without_writing(self):
        before = (self.config.read_bytes(), self.toml.read_bytes())
        for argv in (['--hours', '24-6', '--timezone', ZONE], ['--hours', '6-24'], ['--hours', '6-24', '--timezone', 'Mars/Olympus'],
                     ['--timezone', ZONE], ['--hours', 'morgens', '--timezone', ZONE]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): self.run_cli('reserve', 'intern', '--at', '90', *argv)
        self.assertEqual((self.config.read_bytes(), self.toml.read_bytes()), before)

    def test_device_sync_applies_a_window_from_the_registry(self):
        def note(value): registry.account(value, 'intern').update(WINDOW)
        registry.update(note, self.reg)
        paths, env = setup.local_paths_env(self.runtime, self.stores)
        with patch('time.time', return_value=DAY):
            setup.reserve_hook(registry.load(self.reg), (self.config, self.binary, paths, env))
        self.assertEqual({k: self.row('intern')[k] for k in WINDOW}, WINDOW)
        self.assertIn('name = "intern"\npriority = 110\n', self.toml.read_text())


if __name__ == '__main__':
    unittest.main()
