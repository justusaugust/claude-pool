"""A quota wait says which account is held by which limit until when.

The scenario: a device where most accounts are signed out. The only signed-in account with quota is reserved, and its
Fable week is past the reserve; one more account has a full week. A Fable session waits, an Opus session does not. The
wait used to say only "Pool quota exhausted", next to a status line that showed the account at 1 % and 33 %.
"""
from datetime import datetime
import json
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import test_bridge
import test_capacity_reserve
from telemetry import capacity, wait_text

ZONE = 'Europe/Berlin'
NOW = int(datetime(2030, 3, 15, 23, 40, tzinfo=ZoneInfo(ZONE)).timestamp())  # a Friday, seconds
H = 3600
WEEK_FULL_UNTIL = int(datetime(2030, 3, 16, 20, 0, tzinfo=ZoneInfo(ZONE)).timestamp())      # Saturday
FABLE_WEEK_UNTIL = int(datetime(2030, 3, 19, 10, 0, tzinfo=ZoneInfo(ZONE)).timestamp())     # Tuesday
FIVE_HOUR_UNTIL = int(datetime(2030, 3, 15, 23, 55, tzinfo=ZoneInfo(ZONE)).timestamp())     # today


def account(name, status, five=None, week=None, fable=None, rejected=None, **more):
    window = lambda w: {'pct': w[0], 'resetsAt': w[1] * 1000} if w else None
    limits = {key: {'status': None, 'resetsAt': None} for key in ('fiveHour', 'sevenDay', 'fableWeekly')}
    if rejected: limits[rejected[0]] = {'status': 'rejected', 'resetsAt': rejected[1] * 1000}
    return {'id': name, 'name': name, 'label': name.title(), 'provider': 'anthropic', 'status': status,
            'fiveHour': window(five), 'sevenDay': window(week), 'fableWeekly': window(fable), 'quotaLimits': limits, **more}


def device():
    """Four accounts need sign-in, one is reserved from 40 % with its Fable week at 47 %, one has a full week."""
    return [account('private', 'reauthenticate', week=(51, NOW + 48 * H), fable=(76, NOW + 48 * H), reserveAt=99),
            account('team', 'ready', five=(1, NOW + 4 * H), week=(33, FABLE_WEEK_UNTIL), fable=(47, FABLE_WEEK_UNTIL), reserveAt=40),
            account('second', 'reauthenticate', reserveAt=1),
            account('third', 'reauthenticate', five=(11, NOW + 4 * H), week=(70, NOW + 103 * H), fable=(81, NOW + 103 * H)),
            account('fourth', 'reauthenticate', week=(13, NOW + 112 * H), fable=(24, NOW + 112 * H)),
            account('spent', 'limited', week=(100, WEEK_FULL_UNTIL), fable=(15, WEEK_FULL_UNTIL), rejected=('sevenDay', WEEK_FULL_UNTIL))]


class WaitReasonTests(unittest.TestCase):
    def test_a_fable_wait_names_each_held_account_its_window_and_its_reset(self):
        quota = capacity(device(), 'claude-fable-5-2', NOW * 1000)
        self.assertEqual((quota['state'], quota['nextResetAt']), ('quota_wait', WEEK_FULL_UNTIL * 1000))
        self.assertEqual(quota['holds'], [
            {'label': 'Spent', 'window': 'sevenDay', 'pct': 100, 'reserveAt': None, 'resetsAt': WEEK_FULL_UNTIL * 1000},
            {'label': 'Team', 'window': 'fableWeekly', 'pct': 47, 'reserveAt': 40, 'resetsAt': FABLE_WEEK_UNTIL * 1000}],
            'earliest reset first; a reserve hold carries the reserve percent')
        self.assertEqual((quota['signIn'], quota['paused'], quota['otherModels']), (4, 0, True))

    def test_the_same_accounts_take_an_opus_request(self):
        quota = capacity(device(), 'claude-opus-5-5', NOW * 1000)
        self.assertEqual(quota['state'], 'ready')
        self.assertEqual(wait_text(quota, 'claude-opus-5-5', NOW * 1000, ZONE), None)

    def test_the_wait_text_says_account_limit_and_time(self):
        quota = capacity(device(), 'claude-fable-5-2', NOW * 1000)
        self.assertEqual(wait_text(quota, 'claude-fable-5-2', NOW * 1000, ZONE),
                         'Pool waits for Fable quota · Spent week limit reached until Sat 20:00 · '
                         'Team Fable week 47% (reserved from 40%) until Tue 10:00 · 4 accounts need sign-in · '
                         'other models have quota (/model)')

    def test_a_reset_today_shows_only_the_time_and_one_account_is_singular(self):
        accounts = [account('solo', 'limited', five=(100, FIVE_HOUR_UNTIL), week=(20, NOW + 90 * H)),
                    account('away', 'reauthenticate'), account('held', 'paused')]
        quota = capacity(accounts, 'claude-opus-5-5', NOW * 1000)
        self.assertEqual(quota['otherModels'], False)
        self.assertEqual(wait_text(quota, 'claude-opus-5-5', NOW * 1000, ZONE),
                         'Pool waits for quota · Solo 5h limit reached until 23:55 · 1 account needs sign-in · 1 account paused')

    def test_shunts_capped_flag_without_a_full_window_has_no_time(self):
        accounts = [account('capped', 'limited', five=(40, NOW + H), week=(60, NOW + 50 * H), capped=True)]
        quota = capacity(accounts, 'claude-opus-5-5', NOW * 1000)
        self.assertEqual(quota['holds'], [{'label': 'Capped', 'window': None, 'pct': None, 'reserveAt': None, 'resetsAt': None}])
        self.assertEqual(wait_text(quota, 'claude-opus-5-5', NOW * 1000, ZONE), 'Pool waits for quota · Capped at its limit')

    def test_more_than_three_held_accounts_are_counted(self):
        accounts = [account('a%d' % i, 'limited', week=(100, NOW + (i + 1) * 24 * H)) for i in range(5)]
        text = wait_text(capacity(accounts, 'claude-opus-5-5', NOW * 1000), 'claude-opus-5-5', NOW * 1000, ZONE)
        self.assertEqual(text.count('limit reached'), 3)
        self.assertTrue(text.endswith(' · 2 more accounts held'), text)

    def test_no_candidate_account_is_unavailable_and_says_why(self):
        quota = capacity([account('away', 'reauthenticate'), account('held', 'paused')], 'claude-opus-5-5', NOW * 1000)
        self.assertEqual((quota['state'], quota['holds'], quota['signIn'], quota['paused']), ('unavailable', [], 1, 1))


class WaitReasonGatewayTests(unittest.TestCase):
    setUp, tearDown = test_bridge.BridgeTests.setUp, test_bridge.BridgeTests.tearDown
    call = test_bridge.BridgeTests.call
    scenario = test_capacity_reserve.ReserveCapGatewayTests.scenario

    def test_the_capacity_endpoint_carries_the_wait_text(self):
        self.scenario()
        code, body = self.call('/pool/capacity?model=claude-opus-5-5')
        answer = json.loads(body)
        self.assertEqual((code, answer['state']), (200, 'quota_wait'))
        self.assertTrue(answer['message'].startswith('Pool waits for quota · spent-b week limit reached until '), answer['message'])
        self.assertEqual([hold['label'] for hold in answer['holds']][:1], ['spent-b'])

    def test_a_ready_pool_sends_no_wait_text(self):
        code, body = self.call('/pool/capacity?model=claude-opus-5-5')
        self.assertEqual((code, json.loads(body).get('message')), (200, None))


if __name__ == '__main__': unittest.main()
