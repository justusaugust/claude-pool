"""Faults stay in isolated loopback servers with fixture credentials."""
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bridge import PoolBridge
from diagnostics import Journal
import test_bridge
from test_validation import snapshot, NOW
from validation import archive, evaluate, HistoryIndex
import launch


class FailureTests(unittest.TestCase):
    setUp, tearDown, call = test_bridge.BridgeTests.setUp, test_bridge.BridgeTests.tearDown, test_bridge.BridgeTests.call

    def wait_for(self, predicate):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if predicate(): return
            time.sleep(.02)
        self.fail('Condition did not become true')

    def test_request_limit_and_bytes_are_released_and_controls_stay_available(self):
        self.hold = True
        self.bridge.max_requests = 1
        worker = threading.Thread(target=lambda: self.call('/v1/messages', b'{"messages":[],"stream":true}'))
        worker.start(); self.assertTrue(self.entered.wait(2))
        self.assertEqual(self.call('/v1/messages', b'{"messages":[]}')[0], 503)
        self.assertEqual(self.call('/pool/diagnostics')[0], 200)
        self.release.set(); worker.join(3)
        self.wait_for(lambda: not self.bridge.requests)
        self.assertEqual(self.bridge.body_bytes, 0)
        self.bridge.max_body_bytes = 1
        self.assertEqual(self.call('/v1/messages', b'{"messages":[]}')[0], 503)
        self.assertEqual(len(self.inputs), 1)

    def test_closed_client_cancels_stalled_upstream_without_replay(self):
        self.hold = True
        conn = http.client.HTTPConnection('127.0.0.1', self.bridge.server_port)
        conn.request('POST', '/v1/messages', b'{"messages":[],"stream":true}',
                     {'Authorization':'Bearer fixture-client', 'x-claude-code-session-id':'cancel-test'})
        self.assertTrue(self.entered.wait(2)); conn.close()
        self.wait_for(lambda: not self.bridge.requests)
        self.assertTrue(self.bridge.sessions['cancel-test']['interrupted'])
        self.assertIn('client_disconnected', [r['code'] for r in self.bridge.journal.snapshot()])
        self.assertEqual(len(self.inputs), 1)
        self.release.set()

    def test_idle_timeout_closes_hung_request_and_releases_reservation(self):
        self.hold = True; self.bridge.idle_timeout = .15
        with self.assertRaises(http.client.RemoteDisconnected): self.call('/v1/messages', b'{"messages":[],"stream":true}')
        self.wait_for(lambda: not self.bridge.requests)
        self.assertFalse(self.bridge.sessions['test-session'].get('inFlight'))
        self.assertIn('request_idle_timeout', [r['code'] for r in self.bridge.journal.snapshot()])
        self.release.set()

    def test_auxiliary_saturation_keeps_main_request_capacity(self):
        self.bridge.max_requests, self.bridge.main_reserve = 3, 1
        clients = []
        try:
            for _ in range(2):
                client = socket.create_connection(self.bridge.server_address); clients.append(client)
                client.sendall(b'POST /v1/messages/count_tokens HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer fixture-client\r\nContent-Length: 10\r\n\r\n{')
            self.wait_for(lambda: len(self.bridge.requests) == 2)
            self.assertEqual(self.call('/v1/messages/count_tokens', b'{"messages":[]}')[0], 503)
            self.assertEqual(self.call('/v1/messages', b'{"messages":[],"stream":true}')[0], 200)
            self.assertEqual(self.bridge.diagnostics()['health']['mainRequests'], 0)
        finally:
            for client in clients: client.close()
            self.wait_for(lambda: not self.bridge.requests)

    def test_partial_upload_idle_timeout_releases_its_slot(self):
        self.bridge.idle_timeout = .15
        with socket.create_connection(self.bridge.server_address) as client:
            client.sendall(b'POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer fixture-client\r\nContent-Length: 100\r\n\r\n{')
            self.wait_for(lambda: len(self.bridge.requests) == 1)
            self.wait_for(lambda: not self.bridge.requests)
        self.assertEqual(self.bridge.body_bytes, 0)
        self.assertFalse(self.inputs)

    def test_terminal_sse_releases_main_and_auxiliary_slots_without_waiting_for_eof(self):
        self.keep_open = True
        try:
            for headers in ({}, {'x-claude-code-agent-id':'fixture-agent'}):
                self.assertEqual(self.call('/v1/messages', b'{"messages":[],"stream":true}', extra_headers=headers)[0], 200)
                self.wait_for(lambda: not self.bridge.requests)
        finally: self.release.set()

    def test_scheduler_pause_does_not_count_as_an_upstream_timeout(self):
        self.bridge.monitor_stop.set(); self.bridge.monitor.join(2)
        client, peer = socket.socketpair()
        try:
            row = {'client':client, 'progress':0, 'upstream':None}
            self.bridge.requests['pause'] = row
            self.bridge.idle_timeout = 1
            with patch.object(self.bridge.monitor_stop, 'wait', side_effect=[False,True]), patch('bridge.time.monotonic', return_value=10) as clock:
                clock.side_effect=[0,10,10]
                self.bridge.monitor_requests()
            self.assertEqual(row['progress'], 10); self.assertNotIn('cancelled', row)
            self.bridge.requests.clear()
        finally: client.close(); peer.close()

    def test_reader_consuming_bytes_before_monitor_peek_is_not_a_disconnect(self):
        self.bridge.monitor_stop.set();self.bridge.monitor.join(2)
        client=Mock();client.recv.side_effect=BlockingIOError()
        row={'client':client,'progress':time.monotonic(),'upstream':None}
        self.bridge.requests['fixture']=row
        try:
            with patch.object(self.bridge.monitor_stop,'wait',side_effect=[False,True]),patch('bridge.select.select',return_value=([client],[],[])):
                self.bridge.monitor_requests()
            self.assertNotIn('cancelled',row);client.shutdown.assert_not_called()
        finally:self.bridge.requests.clear()

    def test_long_thinking_heartbeats_extend_idle_deadline_without_replay(self):
        # Keep the timing ratio, with headroom for shared-runner scheduling.
        self.bridge.idle_timeout = 1
        self.stream_events = [{'type':'ping'}] * 8 + [{'type':'message_stop'}]
        self.event_interval = .4
        started = time.monotonic()
        status, payload = self.call('/v1/messages', b'{"messages":[],"stream":true}')
        self.assertGreater(time.monotonic() - started, self.bridge.idle_timeout)
        self.assertEqual(payload.count(b'"type": "ping"'), 8)
        self.assertEqual(status, 200); self.assertIn(b'message_stop', payload)
        self.assertNotIn('request_idle_timeout', [r['code'] for r in self.bridge.journal.snapshot()])
        self.assertEqual(len(self.inputs), 1)

    def test_stream_error_after_output_is_forwarded_and_not_retried_or_logged_verbatim(self):
        self.stream_events = [{'type':'content_block_delta','delta':{'text':'fixture partial answer'}},
                              {'type':'error','error':{'type':'overloaded_error','message':'fixture private provider detail'}}]
        status, payload = self.call('/v1/messages', b'{"messages":[],"stream":true}')
        self.assertEqual(status, 200); self.assertIn(b'fixture partial answer', payload)
        self.assertIn('stream_error', [r['code'] for r in self.bridge.journal.snapshot()])
        self.assertTrue(self.bridge.sessions['test-session']['interrupted'])
        self.assertEqual(len(self.inputs), 1)
        self.assertNotIn('fixture private provider detail', (Path(self.temp.name)/'incidents.json').read_text())

    def test_truncated_stream_reports_retryable_error_without_replay_or_completion(self):
        self.complete = False
        status, payload = self.call('/v1/messages', b'{"messages":[],"stream":true}')
        events = [json.loads(line[6:]) for line in payload.splitlines() if line.startswith(b'data: ')]
        self.assertEqual(status, 200)
        self.assertEqual(events[-1]['type'], 'error')
        self.assertEqual(events[-1]['error']['type'], 'api_error')
        self.assertNotIn(b'message_stop', payload)
        self.assertEqual(len(self.inputs), 1)
        self.assertTrue(self.bridge.sessions['test-session']['interrupted'])
        self.assertFalse(self.bridge.traffic)
        self.assertFalse(self.bridge.requests)

    def test_complete_stream_does_not_gain_an_error_or_change_signed_data(self):
        self.stream_events = [{'type':'message_start','message':{'usage':{}}},
            {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'fixture-signed-by-provider'}},
            {'type':'message_stop'}]
        status, payload = self.call('/v1/messages', b'{"messages":[],"stream":true}')
        expected = b''.join(('data: '+json.dumps(event)+'\n\n').encode() for event in self.stream_events)
        self.assertEqual(payload, expected)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.inputs), 1)

    def test_partial_json_frame_is_discarded_before_retryable_error(self):
        self.stream_events = [b'event: message_start\ndata: {"type":"message_start","message":{"usage":{}}}\n\n',
                              b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"partial_json":"unfinished']
        _, payload = self.call('/v1/messages', b'{"messages":[],"stream":true}')
        events = [json.loads(line[6:]) for line in payload.splitlines() if line.startswith(b'data: ')]
        self.assertEqual([v['type'] for v in events], ['message_start','error'])
        self.assertNotIn(b'unfinished', payload)
        self.assertEqual(len(self.inputs), 1)

    def test_fragmented_crlf_signature_frame_is_forwarded_byte_for_byte(self):
        payload = b'event: message_start\r\ndata: {"type":"message_start","message":{"usage":{}}}\r\n\r\nevent: message_stop\r\ndata: {"type":"message_stop"}\r\n\r\n'
        self.stream_events = [payload[:23], payload[23:77], payload[77:]]
        self.assertEqual(self.call('/v1/messages', b'{"messages":[],"stream":true}')[1], payload)
        self.assertFalse(self.bridge.sessions['test-session'].get('interrupted'))

    def test_oversized_unfinished_frame_fails_without_forwarding_or_replay(self):
        self.stream_events = [b'data: '+b'x'*(1024*1024+1)]
        _, payload = self.call('/v1/messages', b'{"messages":[],"stream":true}')
        self.assertLess(len(payload), 1024)
        self.assertIn(b'api_error', payload)
        self.assertEqual(len(self.inputs), 1)
        self.assertFalse(self.bridge.traffic)

    def test_expired_login_and_quota_http_failures_are_distinct_operational_metadata(self):
        for code in (401, 429, 529):
            self.http_status = code
            self.assertEqual(self.call('/v1/messages', b'{"messages":[],"stream":true}')[0], code)
        statuses = {r.get('status') for r in self.bridge.journal.snapshot() if r['code']=='upstream_http_error'}
        self.assertEqual(statuses, {401,429,529})

    def test_repeated_hang_defers_restart_while_active_then_restarts_only_idle(self):
        process = Mock()
        self.bridge.requests['fixture'] = {'client': None, 'progress': time.monotonic()}
        with patch.object(self.bridge, 'admin', side_effect=TimeoutError):
            for _ in range(3): self.assertFalse(self.bridge.check_backend())
        self.assertEqual(self.bridge.health['failures'], 3)
        self.assertFalse(launch.restart_idle(self.bridge, process)); process.terminate.assert_not_called()
        self.bridge.requests.clear()
        self.assertTrue(launch.restart_idle(self.bridge, process)); process.terminate.assert_called_once()
        with patch.object(self.bridge, 'admin', return_value={'providers': []}): self.assertTrue(self.bridge.check_backend())
        self.assertEqual(self.bridge.health['failures'], 0)
        self.assertIn('backend_recovered', [r['code'] for r in self.bridge.journal.snapshot()])

    def test_network_loss_shows_unknown_cached_accounts_and_diagnostics_remain_available(self):
        self.call()
        with patch.object(self.bridge, 'admin', side_effect=ConnectionRefusedError):
            status, payload = self.call('/pool/overview')
            value = json.loads(payload)
            self.assertEqual(status, 200); self.assertEqual(value['ready'], 0)
            self.assertTrue(all(a['status'] == 'unknown' for a in value['accounts']))
            self.assertEqual(value['health']['backend'], 'unresponsive')
            self.assertEqual(self.call('/v1/messages', b'{"messages":[]}')[0], 503)
            self.assertEqual(self.call('/pool/diagnostics')[0], 200)

    def test_disk_full_retains_successful_output_and_journal_stays_bounded(self):
        with patch('bridge.atomic_write', side_effect=OSError(28, 'fixture disk full')):
            status, payload = self.call('/v1/messages', b'{"messages":[],"stream":true}')
        self.assertEqual(status, 200); self.assertIn(b'message_stop', payload)
        self.assertTrue(self.bridge.persistence_error)
        self.assertIn('storage_failed', [r['code'] for r in self.bridge.journal.snapshot()])

    def test_drain_rejects_new_requests_and_existing_response_finishes(self):
        self.hold = True
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(self.call('/v1/messages', b'{"messages":[],"stream":true}')))
        worker.start(); self.assertTrue(self.entered.wait(2))
        self.bridge.draining = True
        self.assertEqual(self.call('/v1/messages', b'{"messages":[]}')[0], 503)
        self.release.set(); worker.join(3)
        self.assertEqual(outcome[0][0], 200); self.assertIn(b'message_stop', outcome[0][1])


class JournalTests(unittest.TestCase):
    def test_metadata_whitelist_bounding_recovery_and_disk_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'incidents.json'
            writer = lambda p, text: p.write_text(text)
            journal = Journal(path, writer)
            for status in range(300): journal.record('upstream_http_error', status=status, token='fixture-secret', message='private text', account='gmail')
            self.assertEqual(len(journal.rows), 256)
            self.assertNotIn('fixture-secret', path.read_text()); self.assertNotIn('private text', path.read_text())
            self.assertEqual(Journal(path, writer).snapshot(), journal.snapshot())
            with patch.object(journal, 'writer', side_effect=OSError(28, 'disk full')): journal.record('storage_failed')
            self.assertTrue(journal.error); self.assertEqual(journal.snapshot()[0]['code'], 'storage_failed')


class IndexTests(unittest.TestCase):
    def test_index_matches_batch_across_resets_reads_only_appends_and_handles_rewrites(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [snapshot(NOW, 90, NOW+1800000), snapshot(NOW+3600000, 5, NOW+19800000)]
            index = HistoryIndex(folder)
            for r in rows: archive(folder, r['accounts'], r['forecast'], r['at'])
            report = index.report(); batch = evaluate(rows)
            for key in ('snapshots','liveSnapshots','observedResets','poolOutcomes'): self.assertEqual(report[key], batch[key])
            self.assertEqual(sorted(report['groups'], key=str), sorted(batch['groups'], key=str))
            self.assertGreater(report['bytesRead'], 0)
            self.assertEqual(index.report()['bytesRead'], 0)
            extra = snapshot(NOW+7200000, 15, NOW+19800000)
            archive(folder, extra['accounts'], extra['forecast'], extra['at'])
            after = index.report(); self.assertEqual(after['snapshots'], 3)
            self.assertLess(after['bytesRead'], report['bytesRead'])
            path = next(Path(folder).glob('*.jsonl')); path.write_text(json.dumps(rows[0])+'\n')
            self.assertEqual(index.report()['snapshots'], 1)

    def test_corrupted_derived_index_is_preserved_and_rebuilt_without_losing_raw_history(self):
        with tempfile.TemporaryDirectory() as folder:
            row = snapshot(NOW, 30, NOW+3600000)
            archive(folder, row['accounts'], row['forecast'], row['at'])
            index=HistoryIndex(folder);self.assertEqual(index.report()['snapshots'],1)
            (Path(folder)/'validation.sqlite3').write_bytes(b'fixture corrupted database')
            recovered=index.report()
            self.assertTrue(recovered['indexRebuilt']);self.assertEqual(recovered['snapshots'],1)
            self.assertEqual(len(list(Path(folder).glob('validation.corrupt-*.sqlite3'))),1)
            self.assertEqual(len(list(Path(folder).glob('*.jsonl'))),1)

    def test_partial_line_is_pending_then_ingested_once_and_old_scores_are_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            row = snapshot(NOW, 90, NOW+1800000)
            path = Path(folder)/'fixture.jsonl'; payload=json.dumps(row)
            path.write_text(payload[:100])
            index=HistoryIndex(folder)
            self.assertEqual(index.report()['snapshots'], 0)
            with path.open('a') as f: f.write(payload[100:]+'\n')
            self.assertEqual(index.report()['snapshots'], 1)
            newer=snapshot(NOW+10*86400000, 30, NOW+11*86400000)
            archive(folder, newer['accounts'], newer['forecast'], newer['at'])
            self.assertEqual(index.report()['snapshots'], 2)
            self.assertEqual(index.report()['bytesRead'], 0)


class ServiceTests(unittest.TestCase):
    def test_owner_storage_failure_cleans_up_only_the_just_created_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'bin').mkdir();(root/'bin/shunt').touch()
            (root/'config.toml').write_text('ramp_initial_concurrency = 2')
            process=Mock();process.pid=12345
            with patch.object(launch,'backend_config',return_value='ramp_initial_concurrency = 2'),patch.object(launch,'HERE',root),patch.object(launch,'RUNTIME',root/'runtime'),patch.object(launch,'recover_orphan',return_value=False), \
                 patch.object(launch.subprocess,'Popen',return_value=process),patch.object(launch,'atomic_write',side_effect=OSError(28,'disk full')):
                with self.assertRaises(OSError): launch.daemon('fixture','fixture')
            process.terminate.assert_called_once();process.wait.assert_called_once_with(timeout=5)

    def test_explicit_start_returns_without_opening_a_claude_window(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(launch,'RUNTIME',Path(folder)),patch.object(launch,'token',return_value='fixture'), \
             patch.object(launch,'managed_start'),patch.object(launch,'ensure_pool'),patch.object(launch.sys,'argv',['claude-pool','--start']), \
             patch.object(launch.subprocess,'call') as native,patch('sys.stdout',new_callable=io.StringIO) as output:
            launch.main();native.assert_not_called();self.assertIn('pool started',output.getvalue())

    def test_launchd_definition_has_no_secrets_and_deliberate_stop_does_not_respawn(self):
        definition = launch.service_definition()
        self.assertEqual(definition['KeepAlive'], {'SuccessfulExit':False})
        self.assertEqual(definition['EnvironmentVariables'], {'POOL_MANAGED':'1', 'CLAUDE_POOL_CONFIG':str(launch.CONFIG)})
        self.assertNotIn('-k', definition['ProgramArguments'])
        with tempfile.TemporaryDirectory() as folder, patch.object(launch, 'RUNTIME', Path(folder)), patch.object(launch, 'managed_start') as start:
            (Path(folder)/'maintenance').write_text('stopped')
            with self.assertRaisesRegex(RuntimeError, 'deliberately stopped'): launch.ensure_pool('fixture')
            launch.daemon('fixture', 'fixture')
            start.assert_not_called()

    def test_systemd_unit_mirrors_the_launchd_definition_without_secrets(self):
        import service
        with patch.object(service.sys, 'platform', 'linux'):
            definition = launch.service_definition()
            text = service.render(definition).decode()
            self.assertIn('Restart=on-failure', text); self.assertIn('KillMode=mixed', text); self.assertIn('TimeoutStopSec=360', text)
            self.assertIn('Environment=POOL_MANAGED=1', text); self.assertIn('WantedBy=default.target', text)
            self.assertNotIn('TOKEN', text.upper().replace('CLAUDE_POOL_CONFIG', ''))
            self.assertEqual(service.load(text.encode()), definition)
            self.assertEqual(service.service_file('io.github.claude-pool', '/h'), Path('/h/.config/systemd/user/io.github.claude-pool.service'))
            self.assertEqual(service.unload_hint('x'), 'systemctl --user disable --now x.service')
            with tempfile.TemporaryDirectory() as folder, patch.object(launch, 'SERVICE_FILE', Path(folder) / 'u.service'), patch.object(launch, 'service_command') as control, \
                 patch.object(service, 'needs_reload', return_value=False) as needs:
                previous = os.umask(0o002)
                try: launch.managed_start()
                finally: os.umask(previous)
                self.assertEqual([c.args for c in control.call_args_list], [('daemon-reload',), ('enable', 'io.github.claude-pool.service'), ('start', 'io.github.claude-pool.service')])
                self.assertEqual((Path(folder) / 'u.service').stat().st_mode & 0o777, 0o644)
                self.assertEqual(service.load((Path(folder) / 'u.service').read_bytes()), definition)
                self.assertEqual(sorted(Path(folder).iterdir()), [Path(folder) / 'u.service'], 'no temporary file left behind')
                # An unchanged unit is not reloaded again; a changed one on disk is.
                control.reset_mock(); launch.managed_start()
                self.assertEqual([c.args for c in control.call_args_list], [('enable', 'io.github.claude-pool.service'), ('start', 'io.github.claude-pool.service')])
                needs.return_value = True; control.reset_mock(); launch.managed_start()
                self.assertEqual(control.call_args_list[0].args, ('daemon-reload',))
                (Path(folder) / 'u.service').write_text('[Service]\nExecStart=/elsewhere/python run.py --daemon\n')
                with self.assertRaisesRegex(RuntimeError, 'definition differs'): launch.install_service()
        for value, expected in [('"a b"', ['a b']), ('x "y\\"z" \'w v\'', ['x', 'y"z', 'w v']), ('50%%', ['50%'])]:
            self.assertEqual(service.words(value), expected)
        with patch.object(service.sys, 'platform', 'linux'):
            self.assertEqual(service.quote('/p/it\'s 100%'), '"/p/it\'s 100%%"')
            spaced = dict(definition, ProgramArguments=['/usr/bin/python3', '/srv/a b/$HOME/run.py', '--daemon'], WorkingDirectory='/srv/a b/50%')
            text = service.render(spaced).decode()
            self.assertIn('WorkingDirectory=/srv/a b/50%%\n', text, 'WorkingDirectory is never quoted')
            self.assertIn('ExecStart=/usr/bin/python3 "/srv/a b/$$HOME/run.py" --daemon', text, '$ must not expand in ExecStart')
            self.assertEqual(service.load(text.encode()), spaced)
            with self.assertRaisesRegex(ValueError, 'WorkingDirectory'): service.render(dict(definition, WorkingDirectory='/srv/new\nline'))
            self.assertTrue(service.needs_reload('x', run=lambda *a, **k: Mock(returncode=0, stdout='yes\n')))
            self.assertFalse(service.needs_reload('x', run=lambda *a, **k: Mock(returncode=0, stdout='no\n')))

    def test_orphan_detection_accepts_the_user_systemd_instance_on_linux(self):
        with patch.object(launch, 'process_command', return_value='/usr/lib/systemd/systemd --user'):
            self.assertTrue(launch.orphaned('1'))
            with patch.object(launch.service.sys, 'platform', 'linux'): self.assertTrue(launch.orphaned('900'))
            with patch.object(launch.service.sys, 'platform', 'darwin'): self.assertFalse(launch.orphaned('900'))
        with patch.object(launch, 'process_command', return_value='bash -l'), patch.object(launch.service.sys, 'platform', 'linux'):
            self.assertFalse(launch.orphaned('900'))

    def test_launcher_no_longer_spawns_a_per_window_watcher(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launch,'RUNTIME',Path(folder)), patch.object(launch,'token',return_value='fixture'), \
             patch.object(launch,'ensure_pool'), patch.object(launch.sys,'argv',['claude','--continue']), \
             patch.object(launch.threading,'Thread') as thread, patch.object(launch.subprocess,'call',return_value=0):
            with self.assertRaises(SystemExit): launch.main()
            thread.assert_not_called()


if __name__ == '__main__': unittest.main()
