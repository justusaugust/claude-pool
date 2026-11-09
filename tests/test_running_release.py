"""Services that run another release than `current` are reported (never restarted): doctor, the Conductor check, update."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import conductor
import launch
import registry
import running_release
import settings
import update
from test_conductor import MAC, VPS, value_with

OLD, NEW = 'aaaaaaaaaaaa', 'bbbbbbbbbbbb'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        for patch in (mock.patch.object(settings, 'RUNTIME', self.runtime),
                      mock.patch.object(running_release, 'current', return_value=NEW),
                      mock.patch.object(running_release, '_alive', return_value=True)):
            patch.start(); self.addCleanup(patch.stop)

    def record(self, daemon=None, service=None):
        (self.runtime / 'owner.json').write_text(json.dumps({'daemon': 1, 'release': daemon}))
        (self.runtime / 'conductor.json').write_text(json.dumps({'servicePid': 2, 'version': service}))


class Stale(Fixture):
    def test_old_versions_name_the_service_and_the_restart(self):
        self.record(OLD, OLD)
        text = '\n'.join(running_release.line(unit) for unit in running_release.stale())
        self.assertIn('pool daemon runs version ' + OLD, text)
        self.assertIn('the current one is ' + NEW, text)
        self.assertIn('claude-pool --stop && claude-pool --start', text)
        self.assertIn('Conductor service runs version ' + OLD, text)

    def test_current_versions_and_dead_processes_report_nothing(self):
        self.record(NEW, NEW)
        self.assertEqual(running_release.stale(), [])
        self.record(OLD, OLD)
        with mock.patch.object(running_release, '_alive', return_value=False):
            self.assertEqual(running_release.stale(), [])

    def test_update_prints_the_hint_and_restarts_nothing(self):
        self.record(OLD, NEW)
        out = io.StringIO()
        with mock.patch.object(update.subprocess, 'run') as run, contextlib.redirect_stdout(out):
            update.report()
        self.assertIn('pool daemon runs version ' + OLD, out.getvalue())
        self.assertNotIn('Conductor', out.getvalue())
        run.assert_not_called()


class Doctor(Fixture):
    def run_doctor(self):
        diagnostics = json.dumps({'health': {'supervisor': 's', 'backend': 'up', 'activeRequests': 0, 'requestLimit': 4, 'storageError': False}, 'incidents': []})
        out, code = io.StringIO(), None
        with mock.patch.object(launch, 'request', return_value=diagnostics), mock.patch.object(launch, 'ensure_pool'), \
             mock.patch.object(launch, 'token', return_value='x'), mock.patch.object(launch, 'long_lived_report', return_value=False), \
             mock.patch.object(launch, 'NATIVE_CLAUDE', sys.executable), mock.patch.object(launch.sys, 'argv', ['claude-pool', 'doctor']), \
             contextlib.redirect_stdout(out):
            try: launch.main()
            except SystemExit as exit: code = exit.code
        return code, out.getvalue()

    def test_doctor_fails_on_a_stale_service(self):
        self.record(OLD, NEW)
        code, text = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertRegex(text, r'  fail pool daemon runs version a+, the current one is b+; restart needed: claude-pool --stop')

    def test_doctor_is_quiet_when_current(self):
        self.record(NEW, NEW)
        code, text = self.run_doctor()
        self.assertIsNone(code); self.assertNotIn('runs version', text)


class ConductorCheck(unittest.TestCase):
    def check(self, version, current):
        part = {'running': True, 'ready': True, 'version': version, 'currentVersion': current}
        return conductor.check({'example-vps': {'platform': 'linux', 'linger': True, 'conductor': part}}, value_with())

    def test_stale_conductor_unit_fails_with_the_restart(self):
        step = self.check(OLD, NEW)
        self.assertEqual(step.status, 'failed')
        self.assertIn('Conductor service runs version ' + OLD, step.detail)
        self.assertIn('device conductor install --restart', step.fix)

    def test_current_conductor_unit_is_ok(self):
        self.assertEqual(self.check(NEW, NEW).status, 'ok')


if __name__ == '__main__':
    unittest.main()
