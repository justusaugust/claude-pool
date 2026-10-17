"""Doctor checks for long-lived tokens: expiry, fingerprint, modes, pending switches. Offline, fakes only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import launch
import long_lived
import registry
import settings
from fakes import SecretWatch, fixture_token, long_lived_file, refreshable_file

NOW = 1_800_000_000_000
DAY = 86_400_000


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stores = self.root / 'credentials'; self.stores.mkdir(mode=0o700)
        self.directory = self.root / 'claude-long-lived'; self.directory.mkdir(mode=0o700)

    def value(self, **row):
        base = registry.EMPTY()
        base['accounts'].append({'name': 'team', 'label': 'Team', 'alias': 'team', **row})
        return base

    def findings(self, days=200, seven_day='unused', value=None, name='team', **kwargs):
        long_lived_file(self.directory, name, expires_ms=None if days is None else NOW + int(days * DAY), **kwargs)
        return long_lived.health(self.stores, self.directory, value or self.value(), NOW, 'mac')

    def levels(self, **kwargs): return [f['level'] for f in self.findings(**kwargs)]

    def test_expiry_thresholds_30_and_7_days(self):
        for days, level in [(31, 'ok'), (30, 'warn'), (7, 'fail')]:
            with self.subTest(days=days):
                self.setUp(); self.assertEqual(self.levels(days=days), [level])

    def test_expired_token_fails_and_names_the_reenroll_fix(self):
        finding = self.findings(days=-1)[0]
        self.assertEqual(finding['level'], 'fail'); self.assertIn('account add', finding['fix']); self.assertIn('device sync', finding['fix'])

    def test_fingerprint_mismatch_fails_without_printing_either_token(self):
        a, b = fixture_token('a'), fixture_token('b')
        other = long_lived.fingerprint({'claudeAiOauth': {'accessToken': b}})
        found = self.findings(token=a, value=self.value(fingerprint=other))
        self.assertEqual(found[0]['level'], 'fail')
        SecretWatch(a, b).text(json.dumps(found))

    def test_switch_pending_is_derived_from_files(self):
        refreshable_file(self.stores, 'team', 'user-a', 'team-a')
        found = self.findings()
        self.assertEqual([f['message'] for f in found], ['switch pending'])
        self.assertEqual(found[0]['fix'], 'claude-pool device switch')

    def test_missing_seven_day_window_is_not_a_finding(self):
        self.assertEqual(self.levels(days=200, seven_day=None), ['ok'])

    def test_wrong_modes_and_symlinks_fail_with_a_chmod_fix(self):
        path = long_lived_file(self.directory, 'team', expires_ms=NOW + 200 * DAY); path.chmod(0o640)
        found = long_lived.health(self.stores, self.directory, self.value(), NOW, 'mac')
        self.assertEqual([f['level'] for f in found], ['fail']); self.assertEqual(found[0]['fix'], 'chmod 600 ' + str(path))
        self.directory.chmod(0o755)
        self.assertIn('chmod 700', [f for f in long_lived.health(self.stores, self.directory, self.value(), NOW, 'mac') if f['account'] == 'long-lived folder'][0]['fix'])

    def test_unreadable_file_fails_without_content(self):
        (self.directory / 'team.json').write_text('{not json'); (self.directory / 'team.json').chmod(0o600)
        found = long_lived.health(self.stores, self.directory, self.value(), NOW, 'mac')
        self.assertEqual((found[0]['level'], found[0]['message']), ('fail', 'unreadable'))

    def test_refreshable_stores_produce_no_findings(self):
        refreshable_file(self.stores, 'own', 'user-o', 'org-o')
        self.assertEqual(long_lived.health(self.stores, self.directory, registry.EMPTY(), NOW, 'mac'), [])

    def test_doctor_prints_findings_and_exits_nonzero_on_fail(self):
        long_lived_file(self.directory, 'team', expires_ms=NOW + 3 * DAY)
        diagnostics = json.dumps({'health': {'supervisor': 's', 'backend': 'up', 'activeRequests': 0, 'requestLimit': 4, 'storageError': False}, 'incidents': []})
        out = io.StringIO()
        with patch.object(launch, 'STORES', self.stores), patch.object(launch, 'LONG_LIVED', self.directory), \
             patch.object(launch.time, 'time', lambda: NOW / 1000), patch.object(launch, 'request', return_value=diagnostics), \
             patch.object(launch, 'ensure_pool'), patch.object(launch, 'token', return_value='fixture-client'), \
             patch.object(launch, 'RUNTIME', self.root / 'runtime'), patch.object(launch, 'NATIVE_CLAUDE', sys.executable), \
             patch.object(settings, 'REGISTRY', self.root / 'devices.json'), patch.object(settings, 'SELF', self.root / 'device.json'), \
             patch.object(launch.sys, 'argv', ['claude-pool', 'doctor']), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as result: launch.main()
        self.assertEqual(result.exception.code, 1)
        self.assertRegex(out.getvalue(), r'  fail team: expires in 3 days · fix: claude-pool account add .* --long-lived --name team')


if __name__ == '__main__': unittest.main()
