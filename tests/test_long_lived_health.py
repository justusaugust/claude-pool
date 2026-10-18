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
        # Without --replace a device that holds the expiring token reuses it and reports Connected; the fix would loop.
        self.assertIn('--long-lived --replace --name team', finding['fix']); self.assertIn('first enrolled', finding['fix'])
        self.assertIn('--replace', self.findings(days=20)[0]['fix'])

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

    def run_doctor(self):
        diagnostics = json.dumps({'health': {'supervisor': 's', 'backend': 'up', 'activeRequests': 0, 'requestLimit': 4, 'storageError': False}, 'incidents': []})
        out = io.StringIO(); code = None
        with patch.object(launch, 'STORES', self.stores), patch.object(launch, 'LONG_LIVED', self.directory), \
             patch.object(launch.time, 'time', lambda: NOW / 1000), patch.object(launch, 'request', return_value=diagnostics), \
             patch.object(launch, 'ensure_pool'), patch.object(launch, 'token', return_value='fixture-client'), \
             patch.object(launch, 'RUNTIME', self.root / 'runtime'), patch.object(launch, 'NATIVE_CLAUDE', sys.executable), \
             patch.object(settings, 'REGISTRY', self.root / 'devices.json'), patch.object(settings, 'SELF', self.root / 'device.json'), \
             patch.object(launch.sys, 'argv', ['claude-pool', 'doctor']), contextlib.redirect_stdout(out):
            try: launch.main()
            except SystemExit as exit: code = exit.code
        return code, out.getvalue()

    def test_doctor_prints_findings_and_exits_nonzero_on_fail(self):
        long_lived_file(self.directory, 'team', expires_ms=NOW + 3 * DAY)
        code, text = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertRegex(text, r'  fail team: expires in 3 days · fix: claude-pool account add .* --long-lived --replace --name team')

    def test_a_rejected_probe_fails_doctor_with_the_replace_fix(self):
        import readings
        path = long_lived_file(self.stores, 'team', expires_ms=NOW + 200 * DAY)
        probes = self.root / 'runtime' / 'long-lived-probes.json'
        from private_files import write_private
        write_private(probes, json.dumps({'team': {'at': NOW, 'fingerprint': long_lived.fingerprint(json.loads(path.read_text())), 'result': 'rejected'}}))
        with patch.object(settings, 'STORES', self.stores), patch.object(launch, 'LONG_LIVED_PROBES', probes):
            code, text = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertRegex(text, r'  fail team: needs reauthenticate .*· fix: claude-pool account add .* --long-lived --replace --name team')

    def test_doctor_without_long_lived_tokens_prints_nothing_extra_and_keeps_its_exit(self):
        refreshable_file(self.stores, 'own', 'user-o', 'org-o')
        code, text = self.run_doctor()
        self.assertIsNone(code); self.assertNotIn('long-lived', text)

    def test_doctor_healthy_prints_the_summary_line(self):
        long_lived_file(self.directory, 'team', expires_ms=NOW + 200 * DAY)
        long_lived_file(self.directory, 'other', expires_ms=NOW + 200 * DAY)
        code, text = self.run_doctor()
        self.assertIsNone(code); self.assertIn('  ok long-lived tokens: 2 checked\n', text)

    def test_store_files_with_wrong_mode_or_symlink_fail_whatever_their_kind(self):
        a = long_lived_file(self.stores, 'll', expires_ms=NOW + 200 * DAY); a.chmod(0o640)
        b = refreshable_file(self.stores, 'rf', 'user-r', 'org-r'); b.chmod(0o640)
        real = long_lived_file(self.root / 'elsewhere', 'real', expires_ms=NOW + 200 * DAY)
        (self.stores / 'link.json').symlink_to(real)
        found = {f['account']: f for f in long_lived.health(self.stores, self.directory, registry.EMPTY(), NOW, 'mac')}
        for name, path in (('ll', a), ('rf', b)):
            self.assertEqual((found[name]['level'], found[name]['message'], found[name]['fix']), ('fail', 'store file mode 640', 'chmod 600 ' + str(path)))
        self.assertEqual((found['link']['level'], found['link']['message']), ('fail', 'store file is a symlink'))
        self.assertEqual(found['link']['fix'], 'replace ' + str(self.stores / 'link.json') + ' with the real file')

    def test_a_symlinked_replica_fails(self):
        real = long_lived_file(self.root / 'elsewhere', 'team', expires_ms=NOW + 200 * DAY)
        (self.directory / 'team.json').symlink_to(real)
        found = long_lived.health(self.stores, self.directory, registry.EMPTY(), NOW, 'mac')
        self.assertEqual([f['level'] for f in found], ['fail']); self.assertIn('symlink', found[0]['message'])

    def test_a_refreshable_file_in_the_long_lived_folder_warns_without_content(self):
        path = refreshable_file(self.directory, 'team', 'user-a', 'team-a')
        found = long_lived.health(self.stores, self.directory, registry.EMPTY(), NOW, 'mac')
        self.assertEqual([(f['level'], f['message'], f['fix']) for f in found],
                         [('warn', 'not a long-lived token', 'move ' + str(path) + ' out of ' + str(self.directory))])
        SecretWatch(fixture_token('r')).text(json.dumps(found))


if __name__ == '__main__': unittest.main()
