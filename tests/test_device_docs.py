"""The device help and docs must name every verb, check and human step, and never carry a token shape."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import conductor
import device_cua
import device_install
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
        for word in ['conductor snapshot', 'preview file', 'account reserve', 'forget-accounts', '--forget',
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

    def test_fix_column_is_tied_to_the_real_fix_constants(self):
        table = DEVICES.read_text()
        pieces = {'native-claude': [device_install.NATIVE_FIX], 'cua': [device_cua.DOCS_FIX, 'device add <ssh-target> --cua'],
                  'linger': ['sudo loginctl enable-linger'], 'readings': ['BatchMode'], 'service': ['claude-pool --start'],
                  'install': ['Python 3.11'], 'doctor': ['ssh <device>'],
                  'conductor': ['ssh <device> .local/bin/claude-pool device conductor install', conductor.adopt_fix('abc').split(',')[0]]}
        for name, words in pieces.items():
            row = next(line for line in table.splitlines() if line.startswith('| `' + name + '`'))
            for word in words:
                self.assertIn(word, row.replace('\\|', '|'), name)
                self.assertIn(word, devices_cli.CHECK_HELP[name][1], name)

    def test_help_and_docs_cover_prerequisites_and_conductor_details(self):
        text = DEVICES.read_text() + devices_cli.DEVICE_HELP
        for word in ['Python 3.11', 'same tailnet', '[a-z0-9-]{1,40}', '--from-file', '--herdr-session', '--remote-session',
                     '--replace', 'first enrolled', 'conductor install', '--fresh', 'Conductor Claude', 'outdated launcher (no device support)',
                     'preview stop --all', 'preview doctor', 'preview cua', 'github.com/trycua/cua', 'cua --version', 'exit code 3', 'enable-linger',
                     'ssh-keygen', 'ssh-copy-id']:
            self.assertIn(word, text)
        self.assertNotIn('exit 86', text)

    def test_docs_and_help_have_no_em_dashes(self):
        for text in [devices_cli.DEVICE_HELP, *(path.read_text() for path in DOCS + [DEVICES] if path.exists())]:
            self.assertNotIn('—', text)

    def test_docs_never_contain_a_token_shape(self):
        for path in DOCS + [DEVICES]:
            if path.exists(): self.assertNotRegex(path.read_text(), r'sk-' + r'ant-[a-z]{3}\d\d-[A-Za-z0-9_-]{20}', str(path))
        self.assertNotIn('sk-' + 'ant-', devices_cli.DEVICE_HELP)


if __name__ == '__main__': unittest.main()
