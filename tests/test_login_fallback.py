"""The usage owner keeps an account in the pool when Claude ends its login.

The scenario: the usage owner serves every account from its own refreshable login, because only that login reads usage.
Claude ends such a login about four weeks after the sign-in. Until someone signed in again the account was gone from
the pool, and an account that got no request stayed "ready" for a day, because only a request finds the dead login.
The long-lived token of the same subscription lies on the owner the whole time. Now the owner serves that token as soon
as the login is known to be dead, says so once, and takes the login back when someone signs in again.
"""
import fcntl
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from conductor_alerts import Alerts
import fallback
import long_lived
from private_files import read_private
from fakes import long_lived_file, refreshable_file

NOW = 1_900_000_000_000
MINUTE = 60_000
REGISTRY = {'version': 1, 'generation': 3, 'updatedAt': 0, 'conductor': None, 'accounts': [],
            'devices': [{'name': 'owner', 'platform': 'linux', 'roles': ['usage']}, {'name': 'laptop', 'platform': 'darwin', 'roles': []}]}


def row(name, status='ready', seen=NOW - MINUTE): return {'name': name, 'label': name.title(), 'status': status, 'seenAt': seen}


class Folders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.stores, self.replicas = Path(self.tmp.name) / 'stores', Path(self.tmp.name) / 'long'
        self.stores.mkdir(mode=0o700); self.replicas.mkdir(mode=0o700)

    def login(self, name, ended_minutes_ago, user='user-a'):
        return refreshable_file(self.stores, name, user, 'team-a', expires_ms=NOW - ended_minutes_ago * MINUTE)

    def token(self, name, user='user-a', days=300):
        return long_lived_file(self.replicas, name, user=user, expires_ms=NOW + days * 86_400_000)

    def due(self, rows, me='owner'): return fallback.due(rows, self.stores, self.replicas, REGISTRY, me, NOW)
    def run_once(self, rows, me='owner', record=None): return fallback.run(rows, self.stores, self.replicas, REGISTRY, me, NOW, record)
    def kind(self, name): return long_lived.kind(read_private(self.stores / (name + '.json')))


class DueTests(Folders):
    def test_a_login_the_backend_marked_is_due_ten_minutes_after_its_access_token_ended(self):
        self.login('ops', 11); self.token('ops')
        self.assertEqual(self.due([row('ops', 'reauthenticate')]), ['ops'])

    def test_a_marked_login_whose_access_token_still_holds_is_left_alone(self):
        # The backend also marks an account when one served request came back 401. The login may be fine then.
        self.login('ops', -120); self.token('ops')
        self.assertEqual(self.due([row('ops', 'reauthenticate')]), [])
        self.login('ops', 5)
        self.assertEqual(self.due([row('ops', 'reauthenticate')]), [])

    def test_an_idle_login_is_due_after_an_hour_without_renewal_while_the_usage_poll_reads_another_account(self):
        # No request went to the account, so the backend never marked it; its reading stopped when the token ended.
        self.login('team', 61); self.token('team')
        self.login('ops', -120, user='user-b')
        self.assertEqual(self.due([row('team', seen=NOW - 66 * MINUTE), row('ops')]), ['team'])

    def test_an_idle_login_waits_when_nothing_shows_that_the_usage_poll_runs(self):
        self.login('team', 61); self.token('team')
        self.login('ops', -120, user='user-b')
        self.assertEqual(self.due([row('team', seen=NOW - 66 * MINUTE), row('ops', seen=NOW - 40 * MINUTE)]), [])  # every reading is old
        self.assertEqual(self.due([row('team', seen=NOW - 66 * MINUTE)]), [])                                      # the only account
        self.assertEqual(self.due([row('team', seen=NOW - MINUTE), row('ops')]), [])                               # its own reading is fresh
        self.login('team', 30)
        self.assertEqual(self.due([row('team', seen=NOW - 35 * MINUTE), row('ops')]), [])                          # under an hour

    def test_only_the_usage_owner_falls_back(self):
        self.login('ops', 11); self.token('ops')
        self.assertEqual(self.due([row('ops', 'reauthenticate')], me='laptop'), [])
        self.assertEqual(self.due([row('ops', 'reauthenticate')], me=None), [])

    def test_it_needs_a_valid_long_lived_token_of_the_same_subscription(self):
        self.login('ops', 11)
        marked = [row('ops', 'reauthenticate')]
        self.assertEqual(self.due(marked), [])                      # no token on this device
        self.token('ops', user='user-other')
        self.assertEqual(self.due(marked), [])                      # another subscription under the same name
        self.token('ops', days=-1)
        self.assertEqual(self.due(marked), [])                      # the token itself ran out
        refreshable_file(self.replicas, 'ops', 'user-a', 'team-a')
        self.assertEqual(self.due(marked), [])                      # not a long-lived token
        self.token('ops')
        self.assertEqual(self.due(marked), ['ops'])

    def test_an_account_that_already_serves_its_token_and_a_symlinked_store_are_never_due(self):
        long_lived_file(self.stores, 'ops'); self.token('ops')
        self.assertEqual(self.due([row('ops', 'reauthenticate')]), [])
        (self.stores / 'ops.json').unlink()
        target = refreshable_file(Path(self.tmp.name) / 'elsewhere', 'ops', 'user-a', 'team-a', expires_ms=NOW - 11 * MINUTE)
        (self.stores / 'ops.json').symlink_to(target)
        self.assertEqual(self.due([row('ops', 'reauthenticate')]), [])


class RunTests(Folders):
    def test_the_owner_serves_the_token_and_keeps_the_ended_login(self):
        old = self.login('ops', 11).read_bytes(); copy = self.token('ops').read_bytes()
        seen = []
        self.assertEqual(self.run_once([row('ops', 'reauthenticate')], record=seen.append), ['ops'])
        self.assertEqual(seen, ['ops'])
        self.assertEqual((self.stores / 'ops.json').read_bytes(), copy)
        self.assertEqual(self.kind('ops'), 'long-lived')
        self.assertEqual((self.stores / 'ops.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.read_bytes() for p in (self.stores / '.retired').glob('ops-*.json')], [old])
        self.assertEqual((self.replicas / 'ops.json').read_bytes(), copy)
        self.assertEqual(fallback.active(self.stores, REGISTRY, 'owner'), ['ops'])
        self.assertEqual(self.run_once([row('ops', 'reauthenticate')]), [])       # nothing left to do

    def test_a_failed_write_puts_the_login_back(self):
        old = self.login('ops', 11).read_bytes(); self.token('ops')
        with patch.object(fallback, 'write_private', side_effect=OSError('disk full')):
            self.assertEqual(self.run_once([row('ops', 'reauthenticate')]), [])
        self.assertEqual((self.stores / 'ops.json').read_bytes(), old)
        self.assertEqual(list((self.stores / '.retired').glob('ops-*.json')), [])

    def test_a_sign_in_that_is_running_for_the_account_is_not_disturbed(self):
        old = self.login('ops', 11).read_bytes(); self.token('ops')
        with (self.stores / '.ops.add.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_once([row('ops', 'reauthenticate')]), [])
        self.assertEqual((self.stores / 'ops.json').read_bytes(), old)

    def test_it_never_raises_on_unreadable_folders_or_rows(self):
        self.assertEqual(fallback.run([{'name': 7}, None, {'status': 'reauthenticate'}], self.stores / 'none', self.replicas / 'none', REGISTRY, 'owner', NOW), [])
        self.assertEqual(fallback.run([row('ops', 'reauthenticate')], self.stores, self.replicas, {'devices': 'broken'}, 'owner', NOW), [])

    def test_active_and_possible_are_read_from_the_files(self):
        long_lived_file(self.stores, 'ops'); self.token('ops')
        self.login('team', -120, user='user-b'); self.token('team', user='user-b')
        self.login('solo', -120, user='user-c')
        self.assertEqual(fallback.active(self.stores, REGISTRY, 'owner'), ['ops'])
        self.assertEqual(fallback.active(self.stores, REGISTRY, 'laptop'), [])     # another device serves long-lived tokens by design
        self.assertEqual(fallback.possible(self.stores, self.replicas, REGISTRY, 'owner', NOW), ['team'])
        self.assertEqual(fallback.possible(self.stores, self.replicas, REGISTRY, 'laptop', NOW), [])


class Clock:
    def __init__(self): self.now = NOW / 1000
    def __call__(self): return self.now


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'conductor' / 'state').mkdir(parents=True)
        self.wake, self.clock = root / 'conductor' / 'state' / 'wake.log', Clock()
        self.alerts = Alerts(self.wake, root / 'alerts.json', clock=self.clock, device=lambda: 'owner')

    def lines(self): return [l.split(' ', 1)[1] for l in self.wake.read_text().splitlines()] if self.wake.exists() else []

    def test_a_fallback_is_told_once_and_ends_with_the_new_sign_in(self):
        ready = [row('ops')]
        self.alerts.sign_ins(ready, fallback=['ops'])
        self.alerts.sign_ins(ready, fallback=['ops'])
        self.assertEqual(self.lines(), ['pool rueckfall an conductor: Konto Ops läuft auf Gerät owner mit dem Jahres-Token weiter, weil Claude die '
                                        'Anmeldung beendet hat. Für die Verbrauchszahlen neu anmelden, eilt nicht: claude-pool login ops dort ausführen'])
        self.alerts.sign_ins(ready)
        self.assertEqual(self.lines()[1:], ['pool rueckfall vorbei: Konto Ops auf Gerät owner wieder angemeldet'])

    def test_a_login_that_can_fall_back_is_only_reported_when_the_fallback_did_not_come(self):
        marked = [row('ops', 'reauthenticate')]
        self.alerts.sign_ins(marked, possible=['ops'])
        self.clock.now += 600
        self.alerts.sign_ins(marked, possible=['ops'])
        self.assertEqual(self.lines(), [])
        self.clock.now += 700
        self.alerts.sign_ins(marked, possible=['ops'])
        self.assertEqual(len(self.lines()), 1)
        self.assertIn('pool anmeldung an conductor: Konto Ops braucht eine neue Anmeldung auf Gerät owner', self.lines()[0])

    def test_the_regular_check_reads_the_fallback_state_it_was_given(self):
        class Server:
            health, last_accounts, blocked = {'failures': 0}, [row('ops')], {}
            def brake_info(self): return {}
        alerts = Alerts(self.wake, Path(self.tmp.name) / 'other.json', clock=self.clock, device=lambda: 'owner', fallback=lambda: (['ops'], []))
        alerts.check(Server())
        self.assertEqual([l.split(':')[0] for l in self.lines()], ['pool rueckfall an conductor'])

    def test_the_mark_that_outlives_the_fallback_is_not_reported_as_a_missing_sign_in(self):
        # The backend keeps "needs sign-in" until the account served a request with its token.
        marked = [row('ops', 'reauthenticate')]
        self.alerts.sign_ins(marked, possible=['ops'])
        self.clock.now += 900
        self.alerts.sign_ins(marked, fallback=['ops'])
        self.clock.now += 3600
        self.alerts.sign_ins(marked, fallback=['ops'])
        self.assertEqual([l.split(':')[0] for l in self.lines()], ['pool rueckfall an conductor'])

    def test_a_login_without_a_fallback_is_reported_at_once_as_before(self):
        self.alerts.sign_ins([row('ops', 'reauthenticate')])
        self.assertEqual(len(self.lines()), 1)

    def test_the_end_of_a_sign_in_episode_says_what_ended_it(self):
        marked, ready = [row('ops', 'reauthenticate'), row('max', 'reauthenticate')], [row('ops')]
        self.alerts.sign_ins(marked)
        self.alerts.sign_ins(ready, fallback=['ops'])      # ops now serves its token; max was removed from this device
        ends = [l for l in self.lines() if ' vorbei: ' in l]
        self.assertEqual(sorted(ends), ['pool anmeldung vorbei: Konto Ops läuft auf Gerät owner mit dem Jahres-Token weiter',
                                        'pool anmeldung vorbei: Konto max auf Gerät owner entfernt'])


if __name__ == '__main__':
    unittest.main()
