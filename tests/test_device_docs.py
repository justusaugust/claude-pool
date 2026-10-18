"""The device help and docs must name every verb, check and human step, and never carry a token shape."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import device_report
import devices_cli

DEVICES = ROOT / 'docs' / 'DEVICES.md'
DOCS = [ROOT / 'README.md', *sorted((ROOT / 'docs').glob('*.md'))]


class DeviceDocs(unittest.TestCase):
    def test_device_help_names_every_command_check_and_fix(self):
        words = ['device ' + verb for verb in devices_cli.USER_VERBS] + list(device_report.CHECK_NAMES)
        words += ['Authorize', 'Accessibility', 'linger', 'long-lived', 'account reserve', 'preview file', 'conductor move',
                  '--force-registry', '--forget', 'reauthenticate', 'docs/DEVICES.md']
        for word in words: self.assertIn(word, devices_cli.DEVICE_HELP)
        for verb in devices_cli.INTERNAL_VERBS: self.assertIn(verb.split()[0], devices_cli.DEVICE_HELP)

    def test_help_device_prints_without_pool_access(self):
        import launch
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['claude-pool', 'help', 'device']), mock.patch.object(launch, 'token') as token, \
                contextlib.redirect_stdout(out):
            launch.main()
        token.assert_not_called()
        self.assertEqual(out.getvalue(), devices_cli.DEVICE_HELP + '\n')

    def test_docs_cover_every_check_and_link_from_readme(self):
        text = DEVICES.read_text()
        for name in device_report.CHECK_NAMES: self.assertIn('| `' + name + '`', text)
        self.assertIn('docs/DEVICES.md', (ROOT / 'README.md').read_text())
        for word in ['conductor snapshot', 'preview file', 'account reserve', 'forget-accounts', '--forget', 'exit 86',
                     'one Native Authorize click', 'cua-spacesd', 'tailscale set --operator']:
            self.assertIn(word.lower(), text.lower())

    def test_other_docs_carry_their_device_parts(self):
        wanted = {'docs/SETUP.md': ['only by `device` commands'], 'docs/SECURITY.md': ['fingerprint', 'one-year'],
                  'docs/USAGE.md': ['device add', 'account reserve', 'preview file', 'account remove', '--long-lived', 'conductor snapshot'],
                  'docs/PHONE.md': ['Conductor Claude', 'Remote Control', '85%'],
                  'docs/TROUBLESHOOTING.md': ['reauthenticate', 'BatchMode', 'linger', 'Conductor', 'diverged', 'HTTP']}
        for name, words in wanted.items():
            text = (ROOT / name).read_text()
            for word in words: self.assertIn(word, text, name)

    def test_docs_and_help_have_no_em_dashes(self):
        for text in [devices_cli.DEVICE_HELP, *(path.read_text() for path in DOCS + [DEVICES] if path.exists())]:
            self.assertNotIn('—', text)

    def test_docs_never_contain_a_token_shape(self):
        for path in DOCS + [DEVICES]:
            if path.exists(): self.assertNotRegex(path.read_text(), r'sk-' + r'ant-[a-z]{3}\d\d-[A-Za-z0-9_-]{20}', str(path))
        self.assertNotIn('sk-' + 'ant-', devices_cli.DEVICE_HELP)


if __name__ == '__main__': unittest.main()
