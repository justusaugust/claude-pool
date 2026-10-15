import http.client
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from control import Controls, ControlServer, Conflict
import control_hook


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = 1000.
        self.c = Controls(Path(self.tmp.name) / 'state.json', now=lambda: self.clock)
        self.task, self.session = str(uuid.uuid4()), str(uuid.uuid4())
        self.registration = {'task': self.task, 'session': self.session, 'pid': 123, 'instance': str(uuid.uuid4()), 'project': 'Public QA'}
        self.worker = self.c.register(self.registration)

    def request(self, kind='permission'):
        data = {'questions': [{'question': 'Which public marker?', 'options': [{'label': 'A'}]}]} if kind == 'question' else {'command': 'public-qa'}
        self.c.worker({**self.worker, 'operation': 'prepare', 'tool': 'Bash', 'input': data})
        return {**self.worker, **self.c.challenge({**self.worker, 'tool': 'AskUserQuestion' if kind == 'question' else 'Bash', 'input': data, 'kind': kind})}

    def test_decisions_are_exact_once_and_keep_the_native_input_unchanged(self):
        r = self.request()
        with self.assertRaises(Conflict): self.c.decision({**r, 'digest': 'changed', 'action': 'allow'})
        self.c.decision({**r, 'action': 'allow'})
        self.assertTrue(self.c.decision({**r, 'action': 'allow'})['duplicate'])
        with self.assertRaises(Conflict): self.c.decision({**r, 'action': 'deny'})
        self.assertEqual(self.c.result(r), {'status': 'allow'})
        self.assertEqual(self.c.result(r), {'status': 'consumed'})
        self.assertNotIn('public-qa', self.c.path.read_text())

    def test_expiry_local_takeover_and_worker_replacement_cannot_approve(self):
        for change in ['expired', 'local', 'respawn']:
            r = self.request()
            if change == 'expired': self.clock += 121
            if change == 'local': self.c.result({**r, 'cancel': True})
            if change == 'respawn': self.c.register({**self.registration, 'instance': str(uuid.uuid4())})
            with self.assertRaises(Conflict): self.c.decision({**r, 'action': 'allow'})
        with self.assertRaises(Conflict): self.c.register(self.registration)

    def test_question_requires_every_answer_and_has_no_permission_side_effect(self):
        r = self.request('question')
        with self.assertRaises(ValueError): self.c.decision({**r, 'action': 'allow', 'answers': {}})
        answers = {'Which public marker?': 'A'}
        self.c.decision({**r, 'action': 'allow', 'answers': answers})
        self.assertEqual(self.c.result(r), {'status': 'allow', 'answers': answers})

    def test_instruction_id_is_idempotent_and_unknown_delivery_never_replays(self):
        message = {**self.worker, 'id': str(uuid.uuid4()), 'text': 'PUBLIC-TEXT'}
        self.c.message(message)
        self.assertEqual(self.c.message(message)['status'], 'queued')
        with self.assertRaises(Conflict): self.c.message({**message, 'text': 'OTHER'})
        leased = self.c.worker({**self.worker, 'operation': 'poll'})['command']
        self.assertEqual(leased['id'], message['id'])
        self.assertIsNone(self.c.worker({**self.worker, 'operation': 'poll'})['command'])
        restarted = Controls(self.c.path, now=lambda: self.clock)
        self.assertEqual(restarted.commands[message['id']]['status'], 'unknown')
        self.assertNotIn('PUBLIC-TEXT', self.c.path.read_text())
        self.assertEqual(restarted.tasks[self.task]['events'], [])

    def test_two_tasks_do_not_share_messages_and_stale_targets_are_refused(self):
        second = self.c.register({'task': str(uuid.uuid4()), 'session': str(uuid.uuid4()), 'pid': 222, 'instance': str(uuid.uuid4())})
        self.c.message({**self.worker, 'id': str(uuid.uuid4()), 'text': 'PUBLIC'})
        self.assertIsNone(self.c.worker({**second, 'operation': 'poll'})['command'])
        self.clock += 21
        with self.assertRaises(Conflict): self.c.message({**second, 'id': str(uuid.uuid4()), 'text': 'PUBLIC'})

    def test_activity_is_bounded_and_only_metadata_persists(self):
        for n in range(60): self.c.worker({**self.worker, 'operation': 'event', 'id': str(n), 'kind': 'assistant', 'text': 'PUBLIC-ACTIVITY'})
        self.c.save()
        self.assertEqual(len(self.c.view(self.task)['tasks'][0]['events']), 48)
        self.assertNotIn('PUBLIC-ACTIVITY', self.c.path.read_text())

    def test_partial_or_changed_tool_input_cannot_create_a_remote_approval(self):
        self.c.worker({**self.worker, 'operation': 'prepare', 'tool': 'Bash', 'input': {'command': 'public complete action'}})
        with self.assertRaises(Conflict):
            self.c.challenge({**self.worker, 'tool': 'Bash', 'input': {'command': 'public'}, 'kind': 'permission'})

    def test_disconnected_worker_cannot_leave_a_live_approval(self):
        r = self.request()
        self.c.worker({**self.worker, 'operation': 'status', 'status': 'offline'})
        self.assertEqual(self.c.view()['pending'], [])
        with self.assertRaises(Conflict): self.c.decision({**r, 'action': 'allow'})

    def test_recovery_preserves_confirmed_receipts_and_corrupt_originals(self):
        message = {**self.worker, 'id': str(uuid.uuid4()), 'text': 'PUBLIC'}
        self.c.message(message)
        self.c.worker({**self.worker, 'operation': 'poll'})
        self.c.worker({**self.worker, 'operation': 'receipt', 'id': message['id'], 'accepted': True})
        self.assertEqual(Controls(self.c.path).commands[message['id']]['status'], 'accepted')
        self.c.path.write_text('public corrupt fixture')
        with self.assertRaises(RuntimeError): Controls(self.c.path)
        self.assertEqual(self.c.path.read_text(), 'public corrupt fixture')

    def test_respawn_without_environment_keeps_the_logical_handoff_task(self):
        native = str(uuid.uuid4())
        handoff = self.c.register({**self.registration, 'session': native, 'instance': str(uuid.uuid4())})
        respawn = self.c.register({**self.registration, 'task': native, 'session': native, 'instance': str(uuid.uuid4())})
        self.assertEqual(respawn['task'], self.task)
        self.assertNotEqual(handoff['generation'], respawn['generation'])
        self.assertEqual(len(self.c.tasks), 1)


class HookTests(unittest.TestCase):
    def test_offline_unrelated_and_unsupported_hooks_never_grant_permission(self):
        with patch.dict(control_hook.os.environ, {}, clear=True):
            self.assertIsNone(control_hook.handle({'hook_event_name': 'PermissionRequest'}, call=lambda *a: self.fail()))

    def test_approval_has_no_input_rewrite_or_saved_permission_rule(self):
        env = {'COCKPIT_CONTROL_TASK': 'task', 'COCKPIT_CONTROL_SESSION': 'native', 'COCKPIT_CONTROL_GENERATION': 'generation'}
        for action in ['allow', 'deny', 'expired', 'local']:
            calls = []
            def rpc(path, body):
                calls.append((path, body))
                if path == 'challenge': return {'id': 'request'}
                return {'status': action}
            with patch.dict(control_hook.os.environ, env):
                answer = control_hook.handle({'session_id': 'native', 'hook_event_name': 'PermissionRequest', 'tool_name': 'Bash', 'tool_input': {'command': 'public'}}, call=rpc)
            if action in ['allow', 'deny']:
                decision = answer['hookSpecificOutput']['decision']
                self.assertEqual(decision['behavior'], action)
                self.assertNotIn('updatedPermissions', decision)
                self.assertNotIn('updatedInput', decision)
            else: self.assertIsNone(answer)
            self.assertTrue(calls[-1][1]['cancel'])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Controls(Path(self.tmp.name) / 'state.json')
        self.server = ControlServer(('127.0.0.1', 0), self.c, 'worker-token', 'phone.test:8443', 'owner@test', lambda: {'accounts': []})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request(method, path, json.dumps(body) if body is not None else None, headers or {})
        response = conn.getresponse(); value = response.read(); conn.close()
        return response.status, value

    def test_worker_credential_cannot_decide_and_wrong_identity_is_refused(self):
        self.assertEqual(self.request('POST', '/api/decision', {}, {'Authorization': 'Bearer worker-token'})[0], 403)
        self.assertEqual(self.request('GET', '/api/state', headers={'Host': 'phone.test:8443', 'Tailscale-User-Login': 'other@test'})[0], 403)
        self.assertEqual(self.request('GET', '/api/state')[0], 403)

    def test_packaged_phone_assets_are_served_to_the_authenticated_owner(self):
        headers = {'Host':'phone.test:8443', 'Tailscale-User-Login':'owner@test'}
        for path in ['/', '/app.js', '/style.css']:
            status, body = self.request('GET', path, headers=headers)
            self.assertEqual(status, 200); self.assertGreater(len(body), 100)
            self.assertEqual(self.request('GET', path)[0], 403)

    def test_usage_telemetry_is_authenticated_read_only_and_has_no_task_data(self):
        self.server.overview = lambda: {'accounts': [{'name': 'team', 'fableWeekly': {'pct': 0}}], 'updatedAt': 123, 'sessions': ['private']}
        self.assertEqual(self.request('GET', '/internal/usage')[0], 401)
        status, raw = self.request('GET', '/internal/usage', headers={'Authorization': 'Bearer worker-token'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {'accounts': [{'name': 'team', 'fableWeekly': {'pct': 0}}], 'updatedAt': 123})
        self.assertEqual(self.request('POST', '/internal/usage', {}, {'Authorization': 'Bearer worker-token', 'Content-Type': 'application/json'})[0], 404)
        with patch.object(self.server, 'overview', side_effect=http.client.BadStatusLine('fixture')):
            self.assertEqual(self.request('GET', '/internal/usage', headers={'Authorization': 'Bearer worker-token'})[0], 503)

    def test_human_write_requires_origin_cookie_and_csrf(self):
        headers = {'Host': 'phone.test:8443', 'Tailscale-User-Login': 'owner@test', 'Content-Type': 'application/json'}
        self.assertEqual(self.request('GET', '/api/state', headers=headers)[0], 200)
        self.assertEqual(self.request('POST', '/api/decision', {}, headers)[0], 403)
        full = {**headers, 'Origin': 'https://phone.test:8443', 'Cookie': '__Host-cockpit=' + self.server.cookie, 'X-Cockpit-CSRF': self.server.csrf}
        self.assertEqual(self.request('POST', '/api/decision', {}, full)[0], 409)
        self.assertEqual(self.request('POST', '/api/decision', {}, {**full, 'Origin': 'https://attacker.test'})[0], 403)


if __name__ == '__main__': unittest.main()
