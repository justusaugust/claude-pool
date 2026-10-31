"""Integration checks use synthetic streams and fake credentials only."""
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bridge import PoolBridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.paused = {"gmail": False, "team": False}
        self.preferred, self.inputs, self.headers = [], [], []
        self.complete = True
        self.stream_events = None
        self.event_interval = 0
        self.keep_open = False
        self.http_status = 200
        self.statuses = []
        self.default_account = 'gmail'
        self.hold = False
        self.entered, self.release = threading.Event(), threading.Event()
        owner = self
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                rows = [{"name": a, "account_ref": a, "has_state": True, "available": not owner.paused[a], "paused": owner.paused[a], "observed_at_5h": 1900000000, "utilization_5h": .2, "reset_5h": 2000000000} for a in owner.paused]
                body = json.dumps({"providers": [{"provider": "anthropic", "auth": "claude_oauth", "accounts": rows}]}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def do_PATCH(self):
                owner.paused[self.path.split('/')[-1]] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['paused']
                self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
            def do_POST(self):
                owner.inputs.append(self.rfile.read(int(self.headers['Content-Length'])))
                owner.headers.append(self.headers)
                account = self.headers.get('x-shunt-session-account', owner.default_account)
                owner.preferred.append(account)
                if owner.hold:
                    owner.entered.set()
                    owner.release.wait(5)
                self.send_response(owner.statuses.pop(0) if owner.statuses else owner.http_status); self.send_header('Content-Type', 'text/event-stream'); self.send_header('x-shunt-account', account); self.end_headers()
                events = [{'type': 'message_start', 'message': {'usage': {'input_tokens': 2}, 'input_transformations': []}}, {'type': 'message_delta', 'usage': {'output_tokens': 4}}]
                if owner.complete: events.append({'type': 'message_stop'})
                if owner.stream_events is not None: events = owner.stream_events
                for event in events:
                    try:
                        self.wfile.write(event if isinstance(event, bytes) else ('data: '+json.dumps(event)+'\n\n').encode()); self.wfile.flush()
                        if owner.event_interval: threading.Event().wait(owner.event_interval)
                    except OSError: break
                if owner.keep_open: owner.release.wait(5)
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.temp = tempfile.TemporaryDirectory()
        self.bridge = PoolBridge(('127.0.0.1', 0), self.upstream.server_port, 'fixture-client', 'fixture-admin', Path(self.temp.name)/'state.json')
        for server in [self.upstream, self.bridge]: threading.Thread(target=server.serve_forever, daemon=True).start()
    def tearDown(self):
        for server in [self.bridge, self.upstream]: server.shutdown(); server.server_close()
        self.temp.cleanup()
    def call(self, path='/cockpit/test-session', body=None, auth=True, extra_headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.bridge.server_port)
        headers = {'Authorization':'Bearer fixture-client'} if auth else {}
        headers['x-claude-code-session-id']='test-session'
        headers.update(extra_headers or {})
        conn.request('GET' if body is None else 'POST',path,body if isinstance(body,bytes) else json.dumps(body) if body else None,headers)
        try:
            response=conn.getresponse();data=response.read();return response.status,data
        finally:conn.close()
    def subagent(self, model='claude-opus-5-5', agent='fixture-agent'):
        body = {'model': model, 'messages': [{'role': 'user', 'content': 'x'}], 'stream': True, 'output_config': {'effort': 'xhigh'}}
        return self.call('/v1/messages', body, extra_headers={'x-claude-code-request-class': 'subagent', 'x-claude-code-agent-id': agent})
    def ledger(self):
        import time
        path = Path(self.temp.name) / 'usage-ledger' / (time.strftime('%Y-%m-%d') + '.jsonl')
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    def cached(self):
        return {'model': 'claude-opus-5-5', 'stream': True, 'system': [{'type': 'text', 'text': 'fixture', 'cache_control': {'type': 'ephemeral'}}],
                'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'go', 'cache_control': {'type': 'ephemeral', 'ttl': '5m'}}]}]}
    def test_while_the_brake_is_on_a_subagent_keeps_its_model_and_effort_and_is_logged(self):
        with self.bridge.lock: self.bridge.session('test-session')['project'] = 'pitch-riot'
        with patch.object(self.bridge, 'brake_info', return_value={'stage': 2}):
            status, _ = self.subagent()
        self.assertEqual(status, 200)
        sent = json.loads(self.inputs[-1])
        self.assertEqual((sent['model'], sent['output_config']['effort']), ('claude-opus-5-5', 'xhigh'))
        row = self.ledger()[-1]
        self.assertEqual((row['project'], row['class'], row['model'], row['braked']), ('pitch-riot', 'subagent', 'claude-opus-5-5', False))
        self.assertEqual((row['input_tokens'], row['output_tokens']), (2, 4))
    def test_subagent_and_workflow_requests_write_one_hour_cache_and_others_pass_byte_for_byte(self):
        for cls in ['subagent', 'workflow']:
            self.call('/v1/messages', self.cached(), extra_headers={'x-claude-code-request-class': cls, 'x-claude-code-agent-id': cls + '-agent'})
            sent = json.loads(self.inputs[-1])
            self.assertEqual([sent['system'][0]['cache_control'], sent['messages'][0]['content'][0]['cache_control']], [{'type': 'ephemeral', 'ttl': '1h'}] * 2, cls)
        raw = json.dumps(self.cached()).encode()
        self.call('/v1/messages', raw)
        self.assertEqual(self.inputs[-1], raw, 'main requests are forwarded as sent')
        sub = {'x-claude-code-request-class': 'subagent', 'x-claude-code-agent-id': 'subagent-agent'}
        self.call('/v1/messages/count_tokens', raw, extra_headers=sub)
        self.assertEqual(self.inputs[-1], raw, 'only Messages requests')
        plain = b'{"model": "claude-opus-5-5", "stream": true, "messages": [{"role": "user", "content": "x"}]}'
        self.call('/v1/messages', plain, extra_headers=sub)
        self.assertEqual(self.inputs[-1], plain, 'nothing to change: the bytes stay as sent')
    def test_a_refused_one_hour_cache_is_sent_once_more_as_the_agent_sent_it(self):
        self.statuses = [400]
        raw = json.dumps(self.cached()).encode()
        status, _ = self.call('/v1/messages', raw, extra_headers={'x-claude-code-request-class': 'subagent', 'x-claude-code-agent-id': 'refused'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(self.inputs[-2])['system'][0]['cache_control'], {'type': 'ephemeral', 'ttl': '1h'})
        self.assertEqual(self.inputs[-1], raw)
        self.assertIn('cache_fallback', [row['code'] for row in self.bridge.journal.snapshot()])
    def test_a_subagent_stays_on_the_account_it_was_first_routed_to_while_that_account_is_ready(self):
        self.default_account = 'team'      # Shunt's own choice for the agent's first request
        self.subagent(agent='steady')
        self.default_account = 'gmail'     # later Shunt would choose another account
        self.subagent(agent='steady'); self.subagent(agent='steady')
        self.assertEqual(self.preferred[-3:], ['team', 'team', 'team'])
        self.assertIsNone(self.headers[-3].get('x-shunt-session-account'), 'a new agent: Shunt chooses')
        self.subagent(agent='other')
        self.assertEqual(self.preferred[-1], 'gmail')
        self.paused['team'] = True; self.bridge.accounts()      # the next account sample shows team paused
        self.subagent(agent='steady')
        self.assertEqual(self.preferred[-1], 'gmail', 'its account is not ready: the normal choice applies')
        self.paused['team'] = False; self.bridge.accounts()
        self.subagent(agent='steady')
        self.assertEqual(self.preferred[-1], 'gmail', 'the account that answered became its account')
    def labeler(self, agent=None):
        """Claude Code's background-agent labeler: class auxiliary, the agent's own id, the agent's cached transcript."""
        return {'x-claude-code-request-class': 'auxiliary', **({'x-claude-code-agent-id': agent} if agent else {})}
    def test_a_labeler_request_goes_to_its_agents_account_and_is_sent_byte_for_byte(self):
        self.default_account = 'team'
        self.subagent(agent='steady')      # the agent's own request: team becomes its account
        self.default_account = 'gmail'     # Shunt alone would send the labeler elsewhere and write the transcript there again
        raw = json.dumps(self.cached()).encode()
        status, _ = self.call('/v1/messages', raw, extra_headers=self.labeler('steady'))
        self.assertEqual(status, 200)
        self.assertEqual(self.headers[-1].get('x-shunt-session-account'), 'team')
        self.assertEqual(self.preferred[-1], 'team')
        self.assertEqual(self.inputs[-1], raw, 'five-minute cache as sent: it reads the agent\'s one-hour entry')
        self.assertEqual(self.ledger()[-1]['class'], 'auxiliary')
    def test_a_labeler_request_neither_pins_nor_moves_its_agent(self):
        raw = json.dumps(self.cached()).encode()
        self.call('/v1/messages', raw, extra_headers=self.labeler('fresh'))    # before the agent's own first answer
        self.assertIsNone(self.headers[-1].get('x-shunt-session-account'), 'no account yet: Shunt chooses')
        self.assertEqual(self.inputs[-1], raw)
        self.assertNotIn('fresh', self.bridge.agents.rows)
        self.default_account = 'team'
        self.subagent(agent='fresh')
        self.assertIsNone(self.headers[-1].get('x-shunt-session-account'), 'the agent\'s own first request: Shunt chooses')
        self.assertEqual(self.bridge.agents.rows['fresh'], 'team')
        self.default_account = 'gmail'
        self.paused['team'] = True; self.bridge.accounts()
        self.call('/v1/messages', raw, extra_headers=self.labeler('fresh'))    # answered by gmail
        self.assertEqual(self.preferred[-1], 'gmail')
        self.assertEqual(self.bridge.agents.rows['fresh'], 'team', 'only the agent\'s own answers decide its account')
        self.paused['team'] = False; self.bridge.accounts()
        self.call('/v1/messages', raw, extra_headers=self.labeler('fresh')); self.subagent(agent='fresh')
        self.assertEqual(self.preferred[-2:], ['team', 'team'])
    def test_a_labeler_request_is_routed_normally_while_its_agents_account_is_not_ready(self):
        self.default_account = 'team'
        self.subagent(agent='steady')
        self.default_account = 'gmail'
        self.call('/v1/messages', self.cached(), extra_headers=self.labeler('steady'))
        self.assertEqual(self.preferred[-1], 'team')
        self.paused['team'] = True; self.bridge.accounts()      # the next account sample shows team paused
        raw = json.dumps(self.cached()).encode()
        self.call('/v1/messages', raw, extra_headers=self.labeler('steady'))
        self.assertIsNone(self.headers[-1].get('x-shunt-session-account'), 'its agent\'s account is not ready: the normal choice applies')
        self.assertEqual((self.preferred[-1], self.inputs[-1]), ('gmail', raw))
    def test_auxiliary_requests_without_an_agent_id_pass_as_before(self):
        self.default_account = 'team'
        self.subagent(agent='steady')
        self.default_account = 'gmail'
        raw = json.dumps(self.cached()).encode()
        status, _ = self.call('/v1/messages', raw, extra_headers=self.labeler())
        self.assertEqual(status, 200)
        self.assertIsNone(self.headers[-1].get('x-shunt-session-account'))
        self.assertEqual((self.preferred[-1], self.inputs[-1]), ('gmail', raw))
        self.assertEqual(self.bridge.agents.rows, {'steady': 'team'})
    def label_rows(self):
        import time
        path = Path(self.temp.name) / 'short-labels' / (time.strftime('%Y-%m-%d') + '.jsonl')
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    def labels(self):
        return [row['result'] for row in self.label_rows()]
    def test_a_labeler_request_goes_in_short_form_to_its_agents_account_and_is_marked_in_the_ledger(self):
        import short_label
        from test_short_label import labeler as labeler_request, SYSTEM
        self.default_account = 'team'
        self.subagent(agent='steady')
        self.default_account = 'gmail'
        status, _ = self.call('/v1/messages', labeler_request(), extra_headers=self.labeler('steady'))
        self.assertEqual(status, 200)
        self.assertEqual((self.headers[-1].get('x-shunt-session-account'), self.preferred[-1]), ('team', 'team'))
        sent = json.loads(self.inputs[-1])
        self.assertEqual((sent['model'], sent['max_tokens'], sent['system'][-1]['text']), ('claude-opus-5-5', 2108, SYSTEM))
        self.assertNotIn('tools', sent)
        self.assertEqual(sent, short_label.rewrite(labeler_request()))
        row = self.ledger()[-1]
        self.assertEqual((row['class'], row['short_label']), ('auxiliary', True))
        self.assertEqual(self.labels(), ['short'])
        self.assertEqual(self.bridge.agents.rows, {'steady': 'team'}, 'a label neither pins nor moves its agent')
    def test_a_refused_short_label_is_sent_once_more_as_claude_code_sent_it(self):
        from test_short_label import labeler as labeler_request
        self.statuses = [400]
        raw = json.dumps(labeler_request()).encode()
        status, _ = self.call('/v1/messages', raw, extra_headers=self.labeler('steady'))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(self.inputs[-2])['max_tokens'], 2108)
        self.assertEqual(self.inputs[-1], raw)
        self.assertIn('short_label_fallback', [row['code'] for row in self.bridge.journal.snapshot()])
        self.assertNotIn('short_label', self.ledger()[-1])
        self.assertEqual(self.labels(), ['short', 'refused'])
    def test_other_auxiliary_requests_of_an_agent_pass_byte_for_byte_and_are_counted(self):
        from test_short_label import labeler as labeler_request
        raw = json.dumps(self.cached()).encode()
        self.call('/v1/messages', raw, extra_headers=self.labeler('steady'))
        self.assertEqual(self.inputs[-1], raw)
        full = json.dumps(labeler_request()).encode()
        self.call('/v1/messages', full, extra_headers={**self.labeler('steady'), 'x-claude-code-agent-type': 'general-purpose'})
        self.assertEqual(self.inputs[-1], full)
        self.call('/v1/messages', full, extra_headers=self.labeler())          # no agent id: not counted
        self.assertEqual(self.inputs[-1], full)
        with patch('short_label.rewrite', side_effect=RuntimeError('fixture')):
            self.call('/v1/messages', full, extra_headers=self.labeler('steady'))
        self.assertEqual(self.inputs[-1], full, 'an error while rewriting: sent as Claude Code sent it')
        self.assertEqual(self.labels(), ['other', 'unmatched', 'kept'])
        self.assertEqual([row.get('form') for row in self.label_rows()], [None, 'agent type general-purpose', None], 'why unmatched')
        self.assertNotIn('short_label', json.dumps(self.ledger()))
    def test_most_labeler_requests_in_full_leave_a_warning_in_the_journal(self):
        from test_short_label import labeler as labeler_request
        full = json.dumps(labeler_request()).encode()
        with patch('short_label.MIN_LABELS', 2):
            for _ in range(2): self.call('/v1/messages', full, extra_headers={**self.labeler('steady'), 'x-claude-code-agent-type': 'x'})
        self.assertIn('short_label_missed', [row['code'] for row in self.bridge.journal.snapshot()])
    def test_without_brake_subagents_pass_unchanged_and_every_answer_is_logged(self):
        status, _ = self.subagent()
        self.assertEqual((status, json.loads(self.inputs[-1])['model']), (200, 'claude-opus-5-5'))
        self.call('/v1/messages', {'model': 'claude-opus-5-5', 'messages': [], 'stream': True})
        self.assertEqual([r['class'] for r in self.ledger()], ['subagent', 'main'])
        self.assertFalse(any(r['braked'] for r in self.ledger()))
    def test_every_upstream_answer_is_kept_with_its_status_and_answering_account(self):
        import time
        start = int(time.time() * 1000)
        self.http_status = 401
        self.call('/v1/messages', b'{"model":"claude-opus-5-5","messages":[]}')
        self.http_status = 200
        self.call('/v1/messages', b'{"model":"claude-opus-5-5","messages":[]}')
        self.assertEqual([(row['status'], row['account']) for row in self.bridge.outcomes_since(start)], [(401, 'gmail'), (200, 'gmail')])
        self.assertEqual(self.bridge.outcomes_since(int(time.time() * 1000) + 1000), [])

    def test_quota_rejection_waits_before_resending_identical_request(self):
        self.http_status = 429
        request = b'{"model":"claude-opus-5-5","output_config":{"effort":"high"},"thinking":{"type":"adaptive"},"stream":true,"messages":[]}'
        checks = []
        def capacity_fixture(accounts, model, now):
            checks.append(model)
            if len(checks) > 1: self.http_status = 200
            return {'state': 'quota_wait' if len(checks) == 1 else 'ready', 'nextResetAt': None, 'checkedAt': now}
        with patch('bridge.capacity', side_effect=capacity_fixture):
            code, body = self.call('/v1/messages', request)
        self.assertEqual(code, 200)
        self.assertIn(b'event: ping', body)
        self.assertIn(b'message_stop', body)
        self.assertEqual(self.inputs, [request, request])
        self.assertNotIn('quotaWaiting', self.bridge.session('test-session'))
        self.assertEqual(self.bridge.session('test-session')['profile']['model'], 'claude-opus-5-5')
        self.assertEqual(self.bridge.session('test-session')['profile']['effort'], 'high')

    def test_cancelled_quota_wait_releases_request_without_resending(self):
        self.http_status = 429
        def capacity_fixture(accounts, model, now): return {'state': 'quota_wait', 'nextResetAt': None, 'checkedAt': now}
        with patch('bridge.capacity', side_effect=capacity_fixture):
            conn = http.client.HTTPConnection('127.0.0.1', self.bridge.server_port)
            conn.request('POST', '/v1/messages', b'{"model":"claude-opus-5-5","stream":true}', {'Authorization':'Bearer fixture-client','x-claude-code-session-id':'test-session'})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'ping', response.read1(256))
            response.close(); conn.close()
            for _ in range(40):
                if not self.bridge.requests: break
                threading.Event().wait(.05)
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(self.bridge.requests, {})
        self.assertNotIn('quotaWaiting', self.bridge.session('test-session'))

    def test_quota_telemetry_failure_during_wait_returns_framed_error_without_replay(self):
        self.http_status = 429
        waiting = {'state': 'quota_wait', 'nextResetAt': None, 'checkedAt': 0}
        with patch('bridge.capacity', side_effect=[waiting, RuntimeError('fixture unavailable')]):
            code, body = self.call('/v1/messages', b'{"model":"claude-opus-5-5","stream":true}')
        self.assertEqual(code, 200)
        self.assertIn(b'event: error', body)
        self.assertNotIn(b'message_stop', body)
        self.assertEqual(len(self.inputs), 1)
        self.assertNotIn('quotaWaiting', self.bridge.session('test-session'))

    def test_invalid_model_quota_response_is_relayed_without_parking(self):
        self.http_status = 429
        with patch('bridge.capacity') as capacity:
            code, _ = self.call('/v1/messages', b'{"model":null,"stream":true}')
        self.assertEqual(code, 429)
        capacity.assert_not_called()

    def test_capacity_endpoint_checks_auth_and_model_without_admitting_inference(self):
        self.assertEqual(self.call('/pool/capacity?model=claude-opus-5-5', auth=False)[0], 401)
        self.assertEqual(self.call('/pool/capacity?model=bad%00')[0], 400)
        code, body = self.call('/pool/capacity?model=claude-opus-5-5')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['state'], 'ready')
        self.assertEqual(self.call('/pool/capacity?model=opus%5B1m%5D')[0], 200)
        self.assertEqual(self.call('/pool/capacity?model=custom-model')[0], 200)
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.bridge.requests, {})

    def test_controls_authentication_unknown_windows_and_session_isolation(self):
        self.assertEqual(self.call(auth=False)[0],401)
        status,data=self.call();self.assertEqual(status,200);self.assertIsNone(json.loads(data)['accounts'][0]['sevenDay'])
        self.assertEqual(self.call(body={'action':'pin','account':'missing'})[0],400)
        self.assertEqual(self.call(body={'action':'pin','account':'team'})[0],200)
        self.assertEqual(json.loads(self.call('/cockpit/other-session')[1])['mode'],'auto')
        self.assertEqual(self.call(body={'action':'pause','account':'team'})[0],200);self.assertTrue(self.paused['team'])
        self.assertEqual(self.call(body={'action':'resume','account':'team'})[0],200);self.assertFalse(self.paused['team'])
        self.assertEqual(self.call(body={'action':'wrap','enabled':False})[0],200)
        self.assertFalse(json.loads(self.call()[1])['wrap'])
        self.assertEqual(self.call(body={'action':'unpin'})[0],200)
        self.assertEqual(json.loads(self.call()[1])['mode'],'auto')
    def test_pin_reaches_shunt_without_rewriting_the_conversation(self):
        self.call(body={'action':'pin','account':'team'})
        body=b'{ "messages": [{"role":"user","content":"fixture"}], "stream":true }'
        self.assertEqual(self.call('/v1/messages',body)[0],200)
        self.assertEqual(self.inputs[-1],body);self.assertEqual(self.preferred[-1],'team')
        state=json.loads(self.call()[1]);self.assertEqual(state['active'],'team');self.assertEqual(state['usage']['output_tokens'],4)
        self.assertEqual(state['accounts'][0]['fiveHour']['pct'],20)
        self.assertEqual(state['accounts'][0]['seenAt'],1900000000000)

    def test_native_login_is_local_identity_and_never_reaches_shunt(self):
        self.bridge.native_auth = Mock()
        self.bridge.native_auth.authenticate.return_value = 'gmail'
        bearer = 'Bearer sk-ant-oat01-fixture-native-token-for-tests'
        status, data = self.call('/v1/messages', b'{"messages":[],"stream":true}', extra_headers={'Authorization':bearer})
        self.assertEqual(status,200)
        self.assertIn(b'message_stop',data)
        self.assertEqual(self.headers[-1]['Authorization'],'Bearer fixture-client')
        self.assertEqual(self.bridge.session('test-session')['connectorIdentity'],'gmail')
        self.assertNotIn(bearer,self.bridge.state_path.read_text())
        self.assertNotIn(bearer,(self.bridge.state_path.parent/'incidents.json').read_text() if (self.bridge.state_path.parent/'incidents.json').exists() else '')
        self.bridge.native_auth.authenticate.reset_mock()
        self.assertEqual(self.call('/v1/messages',b'{"messages":[]}',extra_headers={'Authorization':bearer,'authorization':bearer})[0],401)
        self.bridge.native_auth.authenticate.assert_not_called()
        self.bridge.native_auth.authenticate.return_value = None
        self.assertEqual(self.call('/pool/overview',extra_headers={'Authorization':bearer})[0],401)
        from native_auth import NativeAuthUnavailable
        self.bridge.native_auth.authenticate.side_effect = NativeAuthUnavailable()
        self.assertEqual(self.call('/v1/messages',b'{"messages":[]}',extra_headers={'Authorization':bearer})[0],503)
        self.assertEqual(self.call('/pool/overview')[0],200)

    def test_live_overlap_reserves_distinct_accounts_and_releases_leases(self):
        self.hold = True
        body = b'{"model":"claude-opus-5-5","output_config":{"effort":"high"},"messages":[],"stream":true}'
        first = threading.Thread(target=lambda: self.call('/v1/messages', body, extra_headers={'x-claude-code-session-id':'first'}))
        first.start(); self.assertTrue(self.entered.wait(3))
        state = json.loads(self.call('/cockpit/first')[1])
        self.assertTrue(state['inFlight']); self.assertEqual(state['routing']['account'], 'gmail')
        second = threading.Thread(target=lambda: self.call('/v1/messages', body, extra_headers={'x-claude-code-session-id':'second'}))
        second.start()
        for _ in range(100):
            if len(self.preferred) == 2: break
            threading.Event().wait(.01)
        self.assertEqual(self.preferred, ['gmail','team'])
        self.release.set(); first.join(3); second.join(3)
        for key in ['first','second']:
            self.assertFalse(self.bridge.session(key).get('inFlight'))
            self.assertNotIn('reserved', self.bridge.session(key))
        self.assertEqual(len(self.bridge.traffic), 2)
        self.assertEqual(self.bridge.traffic[0]['workload'], 'opus/high')
        self.assertIsNone(self.bridge.traffic[0]['thinkingTokens'])
        second_bridge = PoolBridge(('127.0.0.1',0),self.upstream.server_port,'fixture-client','fixture-admin',self.bridge.state_path)
        try:
            self.assertEqual(len(second_bridge.traffic),2)
            self.assertEqual(second_bridge.session('first')['profile']['model'],'claude-opus-5-5')
            self.assertEqual(second_bridge.session('second')['routing']['reason'],'shared capacity')
            self.assertNotIn('reserved', second_bridge.session('first'))
        finally: second_bridge.server_close()

    def test_completed_usage_retains_its_own_model_and_effort_after_overlap(self):
        self.assertEqual(json.loads(self.call('/pool/status')[1])['modelReporting'],'completed-request-v1')
        main = self.bridge.reserve('overlap', {'model':'claude-opus-5-5',
            'output_config':{'effort':'high'}, 'thinking':{'type':'adaptive'}})
        other = self.bridge.reserve('overlap', {'model':'claude-opus-4-8',
            'output_config':{'effort':'low'}})
        self.bridge.served('overlap','gmail',{'output_tokens':17,
            'output_tokens_details':{'thinking_tokens':7}},[],main)
        state = self.bridge.session('overlap')
        self.assertEqual(state['profile'], main['profile'])
        self.assertEqual(state['usage']['output_tokens_details']['thinking_tokens'],7)
        stored = json.loads(self.bridge.state_path.read_text())
        self.assertEqual(stored['sessions']['overlap']['profile'],main['profile'])
        self.bridge.served('overlap','team',{'output_tokens':3},[],other)
        self.assertEqual(state['profile'],other['profile'])
        self.assertEqual(state['usage']['output_tokens'],3)

    def test_session_limit_never_evicts_a_live_reservation(self):
        self.bridge.sessions = {str(i): {'at': 0, 'inFlight': 1, 'reserved': 'gmail'} for i in range(256)}
        with self.assertRaises(RuntimeError): self.bridge.session('overflow')
        self.assertEqual(len(self.bridge.sessions),256)
        self.assertEqual(self.bridge.session('0')['reserved'],'gmail')
        self.bridge.sessions['0'].pop('inFlight')
        self.assertEqual(self.bridge.session('new')['mode'],'auto')
        self.assertNotIn('0',self.bridge.sessions)
    def test_header_casing_preserves_betas_and_forces_one_identity_encoding(self):
        self.call('/v1/messages',b'{"messages":[],"stream":true}',extra_headers={'Anthropic-Beta':'existing-beta','Accept-Encoding':'gzip','x-cockpit-project':'must-stay-local'})
        headers=self.headers[-1]
        self.assertEqual(headers.get_all('accept-encoding'),['identity'])
        self.assertEqual(headers.get_all('anthropic-beta'),['existing-beta,thinking-binding-controls-2026-08-01'])
        self.assertIsNone(headers.get('x-cockpit-project'))

    def test_project_label_is_local_sanitized_and_durable_without_full_paths(self):
        self.call(extra_headers={'x-cockpit-project':'Caf%C3%A9%20Project'})
        self.assertEqual(json.loads(self.call()[1])['project'],'Café Project')
        self.call(extra_headers={'x-cockpit-project':'%2FUsers%2Fprivate%2Fsecret'})
        self.assertEqual(json.loads(self.call()[1])['project'],'Café Project')
        self.bridge.save()
        second=PoolBridge(('127.0.0.1',0),self.upstream.server_port,'fixture-client','fixture-admin',self.bridge.state_path)
        try:self.assertEqual(second.session('test-session')['project'],'Café Project')
        finally:second.server_close()

    def test_incomplete_stream_is_not_replayed_or_marked_complete(self):
        self.complete=False
        self.call('/v1/messages',b'{"messages":[],"stream":true}')
        self.assertEqual(len(self.inputs),1)
        self.assertIsNone(json.loads(self.call()[1])['active'])
    def test_malformed_state_does_not_break_startup(self):
        for data in ['[]', '{"bad":false}', '{"bad":{"at":"wrong"}}', 'not json']:
            self.bridge.state_path.write_text(data)
            second=PoolBridge(('127.0.0.1',0),self.upstream.server_port,'different','different',self.bridge.state_path)
            try:self.assertEqual(second.session('safe')['mode'],'auto')
            finally:second.server_close()
    def test_invalid_session_and_action_are_rejected(self):
        self.assertEqual(self.call('/cockpit/invalid_session')[0],400)
        self.assertEqual(self.call(body={'action':'wrap','enabled':'false'})[0],400)
        self.assertEqual(self.call(body={'action':'unknown'})[0],400)

    def test_switch_history_is_bounded_durable_and_contains_only_metadata(self):
        self.bridge.served('test-session','gmail',{'output_tokens':1,'prompt':'must-not-persist'},[])
        for index in range(60):
            self.bridge.served('test-session','team' if index%2==0 else 'gmail',{'cache_read_input_tokens':9,'cache_creation_input_tokens':3},[])
        state=json.loads(self.call()[1]);self.assertEqual(len(state['history']),50)
        self.assertEqual(state['history'][0]['cacheReadTokens'],9)
        self.assertNotIn('must-not-persist',self.bridge.state_path.read_text())
        second=PoolBridge(('127.0.0.1',0),self.upstream.server_port,'fixture-client','fixture-admin',self.bridge.state_path)
        try:self.assertEqual(len(second.session('test-session')['history']),50)
        finally:second.server_close()
    def test_backup_restores_preferences_pauses_and_marks_interrupted_stream(self):
        self.call(body={'action':'pin','account':'team'});self.call(body={'action':'pause','account':'team'})
        self.bridge.session('test-session')['inFlight']=True
        self.bridge.save();self.bridge.save()
        self.bridge.state_path.write_text('broken write')
        self.paused['team']=False
        second=PoolBridge(('127.0.0.1',0),self.upstream.server_port,'fixture-client','fixture-admin',self.bridge.state_path)
        try:
            state=second.session('test-session')
            self.assertEqual(state['pin'],'team');self.assertTrue(state['interrupted'])
            self.assertTrue(second.recovery['restoredBackup'])
            second.restore_pauses();self.assertTrue(self.paused['team'])
            second.save();json.loads(second.state_path.read_text())
        finally:second.server_close()
    def test_disk_error_does_not_truncate_a_completed_model_response(self):
        with patch('bridge.atomic_write',side_effect=OSError('fixture disk full')):
            status,body=self.call('/v1/messages',b'{"messages":[],"stream":true}')
        self.assertEqual(status,200);self.assertIn(b'message_stop',body)
        self.assertTrue(self.bridge.persistence_error)
        self.assertEqual(self.bridge.session('test-session')['active'],'gmail')
    def test_tool_changes_and_signed_compaction_blocks_are_forwarded_unchanged(self):
        payload={'system':'fixed','tools':[{'name':'fixtureTool','input_schema':{}}],
                 'messages':[{'role':'assistant','content':[{'type':'compaction','content':'fixture summary','signature':'opaque-fixture'},
                    {'type':'thinking','thinking':'','signature':'opaque-thinking-fixture'}]}], 'stream':True}
        body=json.dumps(payload).encode();self.call('/v1/messages',body)
        self.assertEqual(self.inputs[-1],body)
        self.assertNotIn('opaque-thinking-fixture',self.bridge.state_path.read_text())
    def test_overview_requires_auth_and_exposes_forecast_and_history(self):
        self.assertEqual(self.call('/pool/overview',auth=False)[0],401)
        data=json.loads(self.call('/pool/overview')[1])
        self.assertIn('forecast',data);self.assertIn('history',data)
        self.assertEqual(self.call('/pool/status')[0],200)

    def test_validation_requires_auth_and_collects_without_inference(self):
        self.assertEqual(self.call('/pool/validation',auth=False)[0],401)
        self.call('/pool/overview')
        data=json.loads(self.call('/pool/validation')[1])
        self.assertEqual(data['liveSnapshots'],1)
        self.assertEqual(data['completedPoolForecasts'],[])
        self.assertEqual(self.inputs,[])
        self.call('/pool/overview')
        self.assertEqual(json.loads(self.call('/pool/validation')[1])['liveSnapshots'],1)
        self.assertNotIn('fixture-client',next(self.bridge.history_path.glob('*.jsonl')).read_text())

    # --- background sessions wait while the brake holds ---------------------------------------------------------------
    NUDGE = 'Conductor (Board): Seit 14:05 kommt von dir nichts Neues. Wo stehst du? Mach weiter oder sag, was fehlt.'

    def turn(self, content, stream=True):
        """A main request whose newest user turn is `content` (a tool result turn says nothing about who is there)."""
        return json.dumps({'model': 'claude-opus-5-5', 'stream': stream, 'messages': [{'role': 'user', 'content': content}]}).encode()

    def tool_turn(self, stream=True):
        return self.turn([{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'ok'}], stream)

    def background(self, session='test-session'):
        """The pool saw only automatic turns in this session (a Board nudge), with the brake off."""
        self.call('/v1/messages', self.turn(self.NUDGE), extra_headers={'x-claude-code-session-id': session})
        self.inputs.clear(); self.headers.clear(); self.preferred.clear()

    def braking(self, stage=1):
        """The brake from here on: `self.stage` decides every sample; checks every 50 ms, calm releases after 0.2 s."""
        self.stage = stage
        self.bridge.brake_check, self.bridge.hold.release = .05, .2
        return patch.object(self.bridge, 'brake_info', side_effect=lambda: {'stage': self.stage, 'ratio': None, 'resetAt': None})

    def open_stream(self, body, session='test-session', extra=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.bridge.server_port, timeout=10)
        conn.request('POST', '/v1/messages', body, {'Authorization': 'Bearer fixture-client', 'x-claude-code-session-id': session, **(extra or {})})
        response = conn.getresponse()
        self.addCleanup(conn.close)
        self.stream_conn = conn
        return response

    def until(self, check, seconds=3):
        for _ in range(int(seconds / .02)):
            if check(): return True
            threading.Event().wait(.02)
        return False

    def waits(self):
        import time
        path = Path(self.temp.name) / 'brake-waits' / (time.strftime('%Y-%m-%d') + '.jsonl')
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_background_request_waits_with_pings_while_the_brake_holds_and_goes_once_calm_holds(self):
        self.background()
        with self.braking(1):
            response = self.open_stream(self.tool_turn())
            self.assertEqual(response.status, 200)
            self.assertIn(b'event: ping', response.read1(256))
            self.assertEqual(self.inputs, [], 'nothing goes upstream while it waits')
            overview = json.loads(self.call('/pool/overview')[1])
            row = next(r for r in overview['sessions'] if r['session'] == 'test-session')
            self.assertEqual((row['brakeWaiting']['reason'], row['brakeWaiting']['requests']), ('stage 1', 1))
            self.assertEqual((overview['brake']['hold'], overview['brake']['waiting']), (True, 1))
            health = self.bridge.diagnostics()['health']
            self.assertEqual((health['activeRequests'], health['waitingRequests']), (0, 1), 'a waiting request takes no request slot')
            self.stage = 0
            body = response.read()
        self.assertIn(b'message_stop', body)
        self.assertEqual(json.loads(self.inputs[-1])['messages'][0]['content'][0]['type'], 'tool_result', 'the request as sent')
        self.assertNotIn('brakeWaiting', self.bridge.session('test-session'))
        self.assertEqual(self.bridge.session('test-session')['active'], 'gmail')
        row = self.ledger()[-1]
        self.assertTrue(row['braked']); self.assertGreaterEqual(row['brakeWaitSeconds'], 0)
        wait = self.waits()[-1]
        self.assertEqual((wait['session'], wait['class'], wait['reason'], wait['end']), ('test-session', 'main', 'stage 1', 'released'))
        self.assertIn('brake_wait', [r['code'] for r in self.bridge.journal.snapshot()])
        self.assertNotIn(self.NUDGE, json.dumps(self.waits()) + json.dumps(self.ledger()) + self.bridge.state_path.read_text())

    def test_a_flicker_back_to_the_brake_restarts_the_calm(self):
        self.background()
        with self.braking(1):
            response = self.open_stream(self.tool_turn())
            response.read1(256)
            self.stage = 0
            self.assertTrue(self.until(lambda: self.bridge.hold.calm is not None))
            self.stage = 1
            self.assertTrue(self.until(lambda: self.bridge.hold.calm is None))
            self.assertEqual(self.inputs, [])
            self.stage = 0
            self.assertIn(b'message_stop', response.read())

    def test_a_subagent_waits_or_goes_with_its_main_session(self):
        self.background('quiet')
        with self.braking(2):
            sub = self.open_stream(json.dumps({'model': 'claude-opus-5-5', 'stream': True, 'messages': [{'role': 'user', 'content': 'x'}]}).encode(),
                                   'quiet', {'x-claude-code-request-class': 'subagent', 'x-claude-code-agent-id': 'agent-quiet'})
            self.assertIn(b'event: ping', sub.read1(256))
            status, body = self.subagent()    # a subagent of test-session: the pool never classified that session
            self.assertEqual(status, 200); self.assertNotIn(b'event: ping', body)
            self.assertEqual(len(self.inputs), 1)
            # The owner writes in the quiet session: its own request goes at once, and so does the agent that waited.
            status, body = self.call('/v1/messages', self.turn('Mach bitte erst mobil fertig.'), extra_headers={'x-claude-code-session-id': 'quiet'})
            self.assertEqual(status, 200); self.assertNotIn(b'event: ping', body)
            self.assertIn(b'message_stop', sub.read())
        self.assertEqual(len(self.inputs), 3)
        self.assertEqual(self.waits()[-1]['class'], 'subagent')

    def test_interactive_unknown_and_exempt_sessions_never_wait(self):
        self.call('/v1/messages', self.turn('Bau die Suche.'))       # the owner typed this turn
        with self.braking(2):
            status, body = self.call('/v1/messages', self.tool_turn())
            self.assertEqual(status, 200); self.assertNotIn(b'event: ping', body)
            status, body = self.call('/v1/messages', self.tool_turn(), extra_headers={'x-claude-code-session-id': 'never-seen'})
            self.assertNotIn(b'event: ping', body)
            with self.bridge.lock: self.bridge.session('board')['project'] = 'conductor'
            self.background('board')
            self.assertNotIn(b'event: ping', self.call('/v1/messages', self.tool_turn(), extra_headers={'x-claude-code-session-id': 'board'})[1])
        self.assertEqual(self.waits(), [])

    def test_an_urgent_mark_releases_a_waiting_session(self):
        import time
        self.background()
        with self.bridge.lock: self.bridge.session('test-session')['project'] = 'pitch-riot'
        with self.braking(1):
            response = self.open_stream(self.tool_turn())
            response.read1(256)
            (Path(self.temp.name) / 'brake.json').write_text(json.dumps({'urgent': {'pitch-riot': time.time() + 3600}}))
            self.assertIn(b'message_stop', response.read())
        self.assertEqual(self.waits()[-1]['end'], 'released')

    def test_non_streaming_and_token_counts_pass_while_the_brake_holds(self):
        self.background()
        with self.braking(2):
            self.assertNotIn(b'event: ping', self.call('/v1/messages', self.tool_turn(stream=False))[1])
            self.call('/v1/messages/count_tokens', self.tool_turn())
        self.assertEqual(len(self.inputs), 2)

    def test_draining_ends_the_wait_with_a_framed_error_and_sends_nothing(self):
        self.background()
        with self.braking(1):
            response = self.open_stream(self.tool_turn())
            response.read1(256)
            self.bridge.draining = True
            body = response.read()
        self.assertIn(b'event: error', body); self.assertNotIn(b'message_stop', body)
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.waits()[-1]['end'], 'draining')
        self.assertNotIn('brakeWaiting', self.bridge.session('test-session'))
        self.assertTrue(self.until(lambda: not self.bridge.requests))

    def test_a_closed_client_ends_the_wait_without_sending(self):
        self.background()
        with self.braking(1):
            response = self.open_stream(self.tool_turn())
            response.read1(256); response.close(); self.stream_conn.close()
            self.assertTrue(self.until(lambda: not self.bridge.requests))
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.waits()[-1]['end'], 'cancelled')

    def test_no_request_waits_longer_than_the_cap(self):
        self.background()
        with self.braking(2):
            self.bridge.brake_max_wait = .2
            status, body = self.call('/v1/messages', self.tool_turn())
        self.assertEqual(status, 200); self.assertIn(b'event: ping', body); self.assertIn(b'message_stop', body)
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(self.waits()[-1]['end'], 'capped')
        self.assertIn('brake_wait_capped', [r['code'] for r in self.bridge.journal.snapshot()])

    def test_the_owner_and_automatic_turns_and_the_hold_survive_a_restart(self):
        self.background()
        self.call('/v1/messages', self.turn('Weiter so.'), extra_headers={'x-claude-code-session-id': 'owned'})
        with self.braking(1): self.bridge.brake_hold()
        self.bridge.save()
        second = PoolBridge(('127.0.0.1', 0), self.upstream.server_port, 'fixture-client', 'fixture-admin', self.bridge.state_path)
        try:
            self.assertIn('autoAt', second.session('test-session')); self.assertNotIn('ownerAt', second.session('test-session'))
            self.assertIn('ownerAt', second.session('owned'))
            self.assertTrue(second.hold.on)
        finally: second.server_close()

    def test_a_full_waiting_room_sends_background_work_instead_of_holding_it(self):
        self.background()
        with self.braking(1):
            self.bridge.max_waiting = 0
            status, body = self.call('/v1/messages', self.tool_turn())
        self.assertNotIn(b'event: ping', body); self.assertEqual(len(self.inputs), 1)
        self.assertIn('brake_wait_full', [r['code'] for r in self.bridge.journal.snapshot()])

    # A message typed or relayed into a waiting session lands in its transcript as a queue "enqueue" line at once; Claude
    # Code sends it only after the waiting request. The wait reads what is appended and lets a person's message through.
    RELAY = 'Conductor (Board): Alex schreibt dir: Bitte zuerst die mobile Ansicht.'

    def transcript(self, session='test-session'):
        self.bridge.transcripts = Path(self.temp.name) / 'projects'
        path = self.bridge.transcripts / '-work-website' / (session + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'type': 'user', 'sessionId': session, 'message': {'role': 'user', 'content': 'Bau die Suche.'}}) + '\n')
        self.bridge.queue_check = .05
        return path

    def enqueue(self, path, content, session='test-session'):
        import time
        line = {'type': 'queue-operation', 'operation': 'enqueue', 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S.000Z'), 'sessionId': session}
        if content is not None: line['content'] = content
        with path.open('a') as f: f.write(json.dumps(line) + '\n')

    def test_a_message_queued_into_a_waiting_session_releases_all_its_waits_at_once(self):
        path = self.transcript()
        self.background()
        with self.braking(2):
            self.bridge.brake_check = 30         # only the transcript can release it within this test
            main = self.open_stream(self.tool_turn())
            self.assertIn(b'event: ping', main.read1(256))
            sub = self.open_stream(json.dumps({'model': 'claude-opus-5-5', 'stream': True, 'messages': [{'role': 'user', 'content': 'x'}]}).encode(),
                                   extra={'x-claude-code-request-class': 'subagent', 'x-claude-code-agent-id': 'agent-a'})
            self.assertIn(b'event: ping', sub.read1(256))
            self.assertTrue(self.until(lambda: self.bridge.brake_state()['waiting'] == 2))
            self.enqueue(path, None)                                  # some enqueues carry no content
            line = json.dumps({'type': 'queue-operation', 'operation': 'enqueue', 'sessionId': 'test-session', 'content': self.RELAY})
            with path.open('a') as f: f.write(line[:40])              # Claude Code is still writing the line
            threading.Event().wait(.2)
            self.assertEqual(self.inputs, [])
            with path.open('a') as f: f.write(line[40:] + '\n')
            self.assertIn(b'message_stop', main.read())
            self.assertIn(b'message_stop', sub.read())
        self.assertEqual(len(self.inputs), 2)
        self.assertIn('ownerAt', self.bridge.session('test-session'))
        self.assertEqual(sorted(w['end'] for w in self.waits()), ['owner_message', 'owner_message'])
        released = [r for r in self.bridge.journal.snapshot() if r['code'] == 'brake_wait_released']
        self.assertEqual([r.get('reason') for r in released], ['owner_message'])
        stored = self.bridge.state_path.read_text() + json.dumps(self.bridge.journal.snapshot()) + json.dumps(self.waits())
        self.assertNotIn('mobile Ansicht', stored, 'the message text is never kept')

    def test_an_automatic_enqueue_or_one_from_before_the_wait_releases_nothing(self):
        path = self.transcript()
        self.background()
        self.enqueue(path, self.RELAY)                                 # queued before the wait began
        with self.braking(1):
            response = self.open_stream(self.tool_turn())
            response.read1(256)
            self.enqueue(path, self.NUDGE)
            self.enqueue(path, '<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n</task-notification>')
            self.enqueue(path, self.RELAY, session='another-session')  # a line of another session
            threading.Event().wait(.4)
            self.assertEqual(self.inputs, [], 'still waiting')
            self.assertNotIn('ownerAt', self.bridge.session('test-session'))
            self.stage = 0
            self.assertIn(b'message_stop', response.read())
        self.assertEqual(self.waits()[-1]['end'], 'released')

    def test_a_missing_or_unreadable_transcript_is_harmless(self):
        self.bridge.transcripts = Path(self.temp.name) / 'no-such-folder'
        self.bridge.queue_check = .05
        self.background()
        with self.braking(1):
            response = self.open_stream(self.tool_turn())
            response.read1(256)
            threading.Event().wait(.2)
            path = self.transcript()
            path.chmod(0)
            threading.Event().wait(.2)
            self.assertEqual(self.inputs, [])
            self.stage = 0
            self.assertIn(b'message_stop', response.read())
        path.chmod(0o600)
        self.assertEqual(self.waits()[-1]['end'], 'released')
        self.assertNotIn('storage_failed', [r['code'] for r in self.bridge.journal.snapshot()])

    def test_state_restart_retains_pin_without_storing_credentials_or_prompts(self):
        self.call(body={'action':'pin','account':'team'})
        data=self.bridge.state_path.read_text()
        self.assertNotIn('fixture-client',data);self.assertNotIn('fixture-admin',data);self.assertNotIn('messages',data)
        second=PoolBridge(('127.0.0.1',0),self.upstream.server_port,'different','different',self.bridge.state_path)
        try:self.assertEqual(second.session('test-session')['pin'],'team')
        finally:second.server_close()


if __name__=='__main__': unittest.main()
