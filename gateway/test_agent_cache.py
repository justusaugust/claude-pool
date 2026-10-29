"""Background agents keep their prompt cache: one-hour cache writes, and their account while it is ready."""
import json
import unittest
import agent_cache

NOW = 1900000000000
HOUR = {'type': 'ephemeral', 'ttl': '1h'}


def account(name, pct=20, status='ready', reserve=None):
    row = {'name': name, 'status': status, 'fiveHour': {'pct': pct, 'resetsAt': NOW + 3600000}, 'sevenDay': {'pct': 10, 'resetsAt': NOW + 86400000}}
    return {**row, 'reserveAt': reserve} if reserve else row


class OneHourCacheTests(unittest.TestCase):
    def test_every_cache_breakpoint_becomes_one_hour_and_keeps_its_other_keys(self):
        request = {'model': 'claude-opus-5-5', 'cache_control': {'type': 'ephemeral'},
                   'system': [{'type': 'text', 'text': 'fixture', 'cache_control': {'type': 'ephemeral', 'scope': 'global'}}],
                   'tools': [{'name': 'fixtureTool', 'input_schema': {'type': 'object', 'properties': {'cache_control': {'type': 'string'}}},
                              'cache_control': {'type': 'ephemeral', 'ttl': '5m'}}],
                   'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'go', 'cache_control': {'type': 'ephemeral'}}]},
                                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'fixtureTool', 'input': {'cache_control': 'kept'}}]},
                                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1',
                                                              'content': [{'type': 'text', 'text': 'ok', 'cache_control': {'type': 'ephemeral'}}]}]}]}
        before = json.loads(json.dumps(request))
        self.assertEqual(agent_cache.one_hour(request), 5)
        self.assertEqual(request['cache_control'], HOUR)
        self.assertEqual(request['system'][0]['cache_control'], {**HOUR, 'scope': 'global'})
        self.assertEqual(request['tools'][0]['cache_control'], HOUR)
        self.assertEqual(request['messages'][0]['content'][0]['cache_control'], HOUR)
        self.assertEqual(request['messages'][2]['content'][0]['content'][0]['cache_control'], HOUR)
        self.assertEqual(request['tools'][0]['input_schema'], before['tools'][0]['input_schema'], 'a schema property named cache_control is no breakpoint')
        self.assertEqual(request['messages'][1], before['messages'][1], 'tool input stays as the model wrote it')

    def test_a_request_without_five_minute_breakpoints_is_left_alone(self):
        for request in [{'model': 'm', 'system': 'plain', 'messages': [{'role': 'user', 'content': 'go'}]},
                        {'system': [{'type': 'text', 'text': 'x', 'cache_control': dict(HOUR)}]},
                        {'messages': 'malformed', 'tools': None, 'system': [None, 'x']}]:
            before = json.dumps(request)
            self.assertEqual(agent_cache.one_hour(request), 0)
            self.assertEqual(json.dumps(request), before)


class AgentAccountTests(unittest.TestCase):
    def test_an_agent_keeps_the_account_that_answered_it_while_that_account_is_ready(self):
        agents, accounts = agent_cache.AgentAccounts(), [account('gmail'), account('team')]
        self.assertIsNone(agents.preferred('a1', accounts), 'a new agent: Shunt chooses')
        agents.served('a1', 'team')
        self.assertEqual(agents.preferred('a1', accounts), 'team')
        self.assertIsNone(agents.preferred('a2', accounts))
        for unready in [account('team', status='limited'), account('team', status='cooling'), account('team', pct=96), account('team', pct=86, reserve=85)]:
            self.assertIsNone(agents.preferred('a1', [accounts[0], unready]), unready)
        self.assertIsNone(agents.preferred('a1', accounts[:1]), 'an account no longer listed')
        agents.served('a1', 'gmail')
        self.assertEqual(agents.preferred('a1', accounts), 'gmail', 'the account that answered becomes its account')
        agents.served('a3', None); agents.served('a4', 'Not An Account')
        self.assertEqual(set(agents.rows), {'a1'})

    def test_memory_is_bounded_and_keeps_the_agents_seen_last(self):
        agents = agent_cache.AgentAccounts(limit=8)
        for n in range(20): agents.served(f'a{n}', 'team')
        self.assertLessEqual(len(agents.rows), 8)
        self.assertEqual(agents.preferred('a19', [account('team')]), 'team')
        self.assertIsNone(agents.preferred('a0', [account('team')]))


if __name__ == '__main__':
    unittest.main()
