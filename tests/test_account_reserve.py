"""Account reserve: last choice below its limit, no new pool work at or above it, one validated write path."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import account_metadata
import accounts_cli as setup
import readings
import registry
import routing
import settings
import views

NOW = 1900000000
BASE = ('[providers.anthropic]\nkind = "anthropic"\nauth = "claude_oauth"\n\n'
        '[[providers.anthropic.accounts]]\nname = "ops"\n\n'
        '[[providers.anthropic.accounts]]\nname = "team"\n\n[server]\nbind = "x"\n')


def shunt(code):
    return '#!' + sys.executable + '\nimport sys\nsys.exit(' + str(code) + ' if "check" in sys.argv else 3)\n'


def account(name, five=None, week=None, fable=None, reserve=None, status='ready'):
    window = lambda pct: None if pct is None else {'pct': pct, 'resetsAt': None}
    row = {'name': name, 'label': name.title(), 'status': status, 'fiveHour': window(five), 'sevenDay': window(week), 'fableWeekly': window(fable)}
    if reserve: row['reserveAt'] = reserve
    return row


def capacity(**seconds): return {'accounts': [{'name': n, 'seconds': s} for n, s in seconds.items()]}


def decide(accounts, previous=None, pin=None, seconds=None):
    sessions = {'s1': {'active': previous, 'pin': pin}}
    return routing.choose(accounts, sessions, 's1', capacity(**(seconds or {a['name']: 1000 for a in accounts})), NOW)


class RoutingTests(unittest.TestCase):
    def test_reserved_account_is_last_choice_below_its_limit(self):
        big = account('team', five=10, reserve=85)
        self.assertEqual(decide([big, account('other', five=60)], seconds={'team': 99999, 'other': 10})['account'], 'other')
        self.assertEqual(decide([big, account('other', five=97)])['account'], 'team')

    def test_reserved_account_ranks_after_a_busier_unreserved_account(self):
        sessions = {'s1': {}, 's2': {'serving': 'other', 'inFlight': 1, 'requestAt': NOW}}
        rows = [account('team', five=1, reserve=85), account('other', five=50)]
        self.assertEqual(routing.choose(rows, sessions, 's1', capacity(team=9999, other=1), NOW)['account'], 'other')

    def test_reserved_account_gets_no_new_work_at_or_above_its_limit(self):
        for window in ['fiveHour', 'sevenDay', 'fableWeekly']:
            with self.subTest(window=window):
                self.assertTrue(routing.reserve_blocked({'name': 'team', 'reserveAt': 85, window: {'pct': 85, 'resetsAt': None}}))
        self.assertFalse(routing.reserve_blocked({'name': 'team', 'reserveAt': 85, 'fiveHour': {'pct': 84.9, 'resetsAt': None}}))
        self.assertFalse(routing.reserve_blocked({'name': 'team', 'fiveHour': {'pct': 99, 'resetsAt': None}}))
        self.assertFalse(routing.reserve_blocked({'name': 'team', 'reserveAt': 85, 'fiveHour': None}))
        decision = decide([account('team', five=86, reserve=85), account('other', five=97)])
        self.assertEqual(decision['reason'], 'no eligible account')
        rows = [account('team', five=86, reserve=85), account('other', five=97)]
        for kwargs in ({'previous': 'team'}, {'pin': 'team'}):
            with self.subTest(**kwargs):
                decision = decide(rows, **kwargs)
                self.assertEqual((decision['account'], decision['reason']), (None, 'no eligible account'))
        self.assertEqual(decide([account('team', five=10, reserve=85), account('other', five=97)], previous='team')['account'], 'team')

    def test_a_blocked_previous_account_names_its_reserve_as_the_reason(self):
        self.assertIn('reserved account', routing.REASONS)
        self.assertEqual(routing.unavailable(account('team', five=86, reserve=85)), 'reserved account')
        self.assertEqual(routing.unavailable(account('team', five=96, reserve=85)), 'five-hour quota exhausted')
        decision = decide([account('team', five=86, reserve=85), account('other', five=40)], previous='team')
        self.assertEqual((decision['account'], decision['reason']), ('other', 'reserved account'))

    def test_session_on_reserved_account_moves_at_its_next_request(self):
        decision = decide([account('team', five=86, reserve=85), account('other', five=40)], previous='team')
        self.assertEqual(decision['account'], 'other')
        self.assertNotEqual(decision['reason'], 'cache affinity')
        pinned = decide([account('team', week=90, reserve=85), account('other', five=40)], pin='team')
        self.assertEqual(pinned['account'], 'other')

    def test_cache_affinity_never_keeps_a_session_on_a_reserved_account(self):
        rows = [account('team', five=10, reserve=85), account('other', five=40)]
        decision = decide(rows, previous='team')
        self.assertEqual(decision['account'], 'other')
        self.assertNotEqual(decision['reason'], 'cache affinity')
        # Nothing else ready: the reserved account keeps its session.
        alone = decide([account('team', five=10, reserve=85), account('other', five=97)], previous='team')
        self.assertEqual((alone['account'], alone['reason']), ('team', 'cache affinity'))
        # A pin below the limit is still honored.
        self.assertEqual(decide(rows, pin='team')['reason'], 'manual selection')

    def test_shared_reading_counts_toward_the_limit(self):
        rows = [account('team', five=10, reserve=85)]
        shared = {'version': 1, 'device': 'mac', 'at': NOW * 1000, 'accounts': {'team': {
            'fiveHour': {'pct': 90, 'resetsAt': None}, 'sevenDay': None, 'fableWeekly': None, 'seenAt': NOW * 1000}}}
        rows[0]['seenAt'] = 1
        readings.merge(rows, shared, NOW * 1000)
        self.assertTrue(routing.reserve_blocked(rows[0]))


class QuotedKeyTests(unittest.TestCase):
    def test_a_quoted_key_is_removed_or_refused_never_left_in_place(self):
        text = BASE.replace('name = "team"\n', 'name = "team"\n"max_utilization" = 0.5\n')
        try: out = setup.set_account_option(text, 'team', 'max_utilization', None)
        except RuntimeError: return
        self.assertNotIn('max_utilization', out)
        self.assertEqual(out.replace('"max_utilization" = 0.5\n', ''), text.replace('"max_utilization" = 0.5\n', ''))
        set_out = setup.set_account_option(text, 'team', 'max_utilization', 0.7)
        self.assertEqual(setup.tomllib.loads(set_out)['providers']['anthropic']['accounts'][1]['max_utilization'], 0.7)


class MetadataTests(unittest.TestCase):
    def load(self, rows):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'accounts': rows}))
            return account_metadata.load(path), account_metadata.decorate({'accounts': [{'name': 'team'}, {'name': 'ops'}]}, path)

    def test_reserve_at_is_accepted_from_0_to_99_and_copied_when_positive(self):
        value, data = self.load([{'name': 'team', 'reserveAt': 85}, {'name': 'ops', 'reserveAt': 0}])
        self.assertEqual(value['accounts'][0]['reserveAt'], 85)
        self.assertEqual(data['accounts'], [{'name': 'team', 'reserveAt': 85}, {'name': 'ops'}])

    def test_reserve_at_outside_its_range_or_not_an_integer_is_refused(self):
        for bad in [100, -1, 85.5, '85', True, None]:
            with self.subTest(bad=bad), self.assertRaises(ValueError): self.load([{'name': 'team', 'reserveAt': bad}])


class BackendConfigTests(unittest.TestCase):
    def config(self, accounts):
        with patch.object(settings, 'ACCOUNTS', accounts), patch.object(settings, 'BACKEND_PORT', 18340):
            return settings.backend_config(Path('/state'))

    def test_backend_config_is_unchanged_without_reserves_and_caps_reserved_accounts(self):
        plain = self.config([{'name': 'ops'}, {'name': 'team'}])
        self.assertTrue(plain.endswith('[[providers.anthropic.accounts]]\nname = "ops"\n\n[[providers.anthropic.accounts]]\nname = "team"\n'))
        self.assertNotIn('max_utilization', plain)
        self.assertEqual(self.config([{'name': 'ops', 'reserveAt': 0}, {'name': 'team'}]), plain)
        capped = self.config([{'name': 'ops'}, {'name': 'team', 'reserveAt': 85}])
        self.assertIn('name = "team"\nmax_utilization = 0.85', capped)
        self.assertEqual(capped.replace('\nmax_utilization = 0.85', ''), plain)
        self.assertEqual(settings.backend_config.__defaults__, (settings.RUNTIME,))


class TomlEditTests(unittest.TestCase):
    def table(self, text, name):
        return next(r for r in setup.tomllib.loads(text)['providers']['anthropic']['accounts'] if r['name'] == name)

    def test_set_and_remove_touch_only_the_named_account(self):
        out = setup.set_account_option(BASE, 'team', 'max_utilization', 0.85)
        self.assertEqual(self.table(out, 'team')['max_utilization'], 0.85)
        self.assertEqual(self.table(out, 'ops'), {'name': 'ops'})
        self.assertEqual(out.replace('max_utilization = 0.85\n', ''), BASE)
        again = setup.set_account_option(out, 'team', 'max_utilization', 0.5)
        self.assertEqual(self.table(again, 'team')['max_utilization'], 0.5)
        self.assertEqual(setup.set_account_option(again, 'team', 'max_utilization', None), BASE)
        self.assertEqual(setup.set_account_option(BASE, 'team', 'max_utilization', None), BASE)

    def test_sub_tables_and_other_settings_stay_byte_identical(self):
        text = BASE.replace('[server]', '[providers.anthropic.accounts.limits]\nweekly = 1\n\n[server]')
        out = setup.set_account_option(text, 'team', 'max_utilization', 0.9)
        self.assertEqual(out.replace('max_utilization = 0.9\n', ''), text)
        self.assertEqual(setup.tomllib.loads(out)['server'], {'bind': 'x'})


class ReserveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / 'cfg'; self.cfg.mkdir(mode=0o700)
        self.runtime = self.root / 'runtime'; self.runtime.mkdir()
        self.stores = self.root / 'stores'; self.stores.mkdir(mode=0o700)
        self.config = self.cfg / 'config.json'
        self.config.write_text(json.dumps({'accounts': [{'name': 'ops', 'label': 'Ops'}, {'name': 'team', 'label': 'AI Værk Team'}]}))
        self.reg = self.cfg / 'devices.json'
        self.toml = self.runtime / 'shunt.toml'; self.toml.write_text(BASE)
        self.binary = self.root / 'fake-shunt'; self.binary.write_text(shunt(0)); self.binary.chmod(0o700)

    def reserve(self, name='team', pct=85, binary=None):
        paths, env = setup.local_paths_env(self.runtime, self.stores)
        setup.reserve(self.config, binary or self.binary, paths, env, name, pct, registry_path=self.reg)

    def run_cli(self, *argv, on_change=None):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = setup.cli(list(argv), config=self.config, stores=self.stores, binary=self.binary, runtime=self.runtime,
                             registry_path=self.reg, on_change=on_change)
        return code, out.getvalue()

    def row(self, name): return next(r for r in json.loads(self.config.read_text())['accounts'] if r['name'] == name)

    def block(self, name):
        return next(r for r in setup.tomllib.loads(self.toml.read_text())['providers']['anthropic']['accounts'] if r['name'] == name)

    def test_reserve_writes_the_cap_through_the_backend_check(self):
        self.reserve()
        self.assertIn('max_utilization = 0.85', self.toml.read_text())
        self.assertEqual(self.block('team')['max_utilization'], 0.85)
        self.assertEqual(self.block('ops'), {'name': 'ops'})
        self.assertEqual(self.row('team')['reserveAt'], 85)
        self.assertNotIn('reserveAt', self.row('ops'))

    def test_backend_rejection_changes_nothing(self):
        bad = self.root / 'bad-shunt'; bad.write_text(shunt(1)); bad.chmod(0o700)
        before = (self.config.read_bytes(), self.toml.read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'rejected'): self.reserve(binary=bad)
        self.assertEqual((self.config.read_bytes(), self.toml.read_bytes()), before)

    def test_off_removes_the_cap_and_records_explicit_off(self):
        self.reserve(); self.reserve(pct=None)
        self.assertNotIn('max_utilization', self.block('team'))
        self.assertEqual(self.toml.read_text(), BASE)
        self.assertEqual(self.row('team')['reserveAt'], 0)

    def test_an_account_that_is_not_connected_is_refused_without_writing(self):
        before = (self.config.read_bytes(), self.toml.read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'No account named nobody'): self.reserve('nobody')
        self.assertEqual((self.config.read_bytes(), self.toml.read_bytes()), before)

    def test_registry_entry_follows_when_the_account_has_one(self):
        registry.update(lambda v: v['accounts'].append({'name': 'team', 'email': 'a@b.de'}), self.reg)
        self.reserve()
        self.assertEqual(registry.account(registry.load(self.reg), 'team')['reserveAt'], 85)
        self.reserve(pct=None)
        self.assertEqual(registry.account(registry.load(self.reg), 'team')['reserveAt'], 0)
        self.reserve('ops')  # no registry entry: nothing created
        self.assertIsNone(registry.account(registry.load(self.reg), 'ops'))

    def test_at_must_be_1_to_99(self):
        for bad in ['0', '100', 'x', '-5']:
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): self.run_cli('reserve', 'team', '--at', bad)
        self.assertNotIn('reserveAt', self.row('team'))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): self.run_cli('reserve', 'team')

    def test_cli_messages_and_sync(self):
        calls = []
        code, out = self.run_cli('reserve', 'team', '--at', '85', on_change=lambda: calls.append(1) or {})
        self.assertEqual(code, 0)
        self.assertEqual(out, 'Reserved AI Værk Team from 85%: last choice, and no new pool work at or above 85% of its 5-hour or weekly limit.\n')
        code, out = self.run_cli('reserve', 'team', '--off', on_change=lambda: calls.append(1) or {})
        self.assertEqual(out, 'AI Værk Team is no longer reserved.\n')
        self.assertEqual(len(calls), 2)

    def test_reserve_syncs_through_the_registry(self):
        value = registry.load(self.reg)
        value['accounts'] = [{'name': 'team', 'email': 'a@b.de', 'reserveAt': 85}, {'name': 'ops', 'reserveAt': 0}, {'name': 'gone', 'reserveAt': 50}, {'name': 'new'}]
        paths, env = setup.local_paths_env(self.runtime, self.stores)
        context = (self.config, self.binary, paths, env)
        with patch.object(setup.registry, 'update') as update:  # the hook applies, it never writes the registry
            setup.reserve_hook(value, context)
            update.assert_not_called()
        self.assertEqual(self.row('team')['reserveAt'], 85)
        self.assertEqual(self.block('team')['max_utilization'], 0.85)
        self.assertNotIn('reserveAt', self.row('ops'))
        value['accounts'][0]['reserveAt'] = 0
        setup.reserve_hook(value, context)
        self.assertEqual(self.row('team')['reserveAt'], 0)
        self.assertNotIn('max_utilization', self.block('team'))

    def test_hook_attempts_every_account_then_raises_one_error_naming_the_failures(self):
        picky = self.root / 'picky-shunt'
        picky.write_text('#!' + sys.executable + '\nimport sys\ntext = open(sys.argv[2]).read()\nsys.exit(1 if "max_utilization = 0.7" in text else 0)\n')
        picky.chmod(0o700)
        paths, env = setup.local_paths_env(self.runtime, self.stores)
        value = {'accounts': [{'name': 'ops', 'reserveAt': 70}, {'name': 'team', 'reserveAt': 85}]}
        with self.assertRaisesRegex(RuntimeError, 'ops') as raised: setup.reserve_hook(value, (self.config, picky, paths, env))
        self.assertNotIn('team', str(raised.exception))
        self.assertEqual(self.row('team')['reserveAt'], 85)
        self.assertNotIn('reserveAt', self.row('ops'))

    def test_hook_leaves_state_alone_on_rejection_and_is_registered_for_device_sync(self):
        bad = self.root / 'bad-shunt'; bad.write_text(shunt(1)); bad.chmod(0o700)
        paths, env = setup.local_paths_env(self.runtime, self.stores)
        with self.assertRaises(RuntimeError): setup.reserve_hook({'accounts': [{'name': 'team', 'reserveAt': 70}]}, (self.config, bad, paths, env))
        self.assertNotIn('reserveAt', self.row('team'))
        import device_sync
        self.assertIn(setup.reserve_hook, device_sync.INTEGRATION_HOOKS)


class UsageViewTests(unittest.TestCase):
    def test_usage_shows_reserved_from_pct(self):
        now = 1900000000000
        row = {'label': 'Team', 'status': 'ready', 'plan': 'Team', 'seenAt': now - 60000, 'reserveAt': 85,
               'fiveHour': {'pct': 3, 'resetsAt': now + 7320000}, 'sevenDay': None, 'readingSource': 'shared:mac'}
        data = {'ready': 1, 'updatedAt': now, 'accounts': [row], 'forecast': {'seconds': 27600, 'eligibleAccounts': 1, 'estimatedAccounts': 1, 'partial': False, 'threshold': 95, 'nextReset': None}, 'history': []}
        text = views.usage(data)
        self.assertIn('Team · ready · reserved from 85%', text)
        self.assertRegex(text, r'reserved from 85% · reading from mac')
        del row['reserveAt']
        self.assertNotIn('reserved', views.usage(data))


if __name__ == '__main__': unittest.main()
