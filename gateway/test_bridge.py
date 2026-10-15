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
                account = self.headers.get('x-shunt-session-account', 'gmail')
                owner.preferred.append(account)
                if owner.hold:
                    owner.entered.set()
                    owner.release.wait(5)
                self.send_response(owner.http_status); self.send_header('Content-Type', 'text/event-stream'); self.send_header('x-shunt-account', account); self.end_headers()
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

    def test_state_restart_retains_pin_without_storing_credentials_or_prompts(self):
        self.call(body={'action':'pin','account':'team'})
        data=self.bridge.state_path.read_text()
        self.assertNotIn('fixture-client',data);self.assertNotIn('fixture-admin',data);self.assertNotIn('messages',data)
        second=PoolBridge(('127.0.0.1',0),self.upstream.server_port,'different','different',self.bridge.state_path)
        try:self.assertEqual(second.session('test-session')['pin'],'team')
        finally:second.server_close()


if __name__=='__main__': unittest.main()
