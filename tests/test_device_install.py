"""Install, service, linger and shell steps run through a fake SSH remote; nothing touches the real machine or the network."""
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import device_install
import device_sync
import managed_block
from device_report import Step, ready, render
from fakes import FakeRemote
from remote import Completed

ANY = mock.ANY


def tar_names(data):
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive: return archive.getnames()


def answer(code=0, out='', err=''): return Completed(code, out.encode(), err.encode())


def remote(handler): return FakeRemote('vps', lambda argv, stdin: handler(' '.join(argv), stdin))


class InstallTests(unittest.TestCase):
    def test_source_archive_has_no_runtime_state_or_credentials(self):
        names = tar_names(device_install.source_archive(ROOT))
        self.assertIn('claude-pool/install.py', names)
        self.assertFalse([n for n in names if 'gateway/bin/' in n or n.endswith('.pool-client-token')])

    def test_install_streams_the_archive_on_stdin_and_never_on_argv(self):
        fake = remote(lambda argv, stdin: answer())
        step = device_install.install(fake, b'ARCHIVE', '/opt/claude')
        argv, stdin = fake.calls[-1]
        self.assertEqual(stdin, b'ARCHIVE'); self.assertNotIn('ARCHIVE', ' '.join(argv))
        self.assertEqual(argv[-2:], ['install', '/opt/claude']); self.assertEqual(step.status, 'ok')

    def test_failed_install_shows_only_the_last_error_lines(self):
        fake = remote(lambda argv, stdin: answer(1, err='\n'.join('line%d' % i for i in range(9))))
        step = device_install.install(fake, b'x', '/n')
        self.assertEqual(step.status, 'failed'); self.assertIn('line8', step.detail); self.assertNotIn('line1\n', step.detail)

    def test_missing_native_claude_is_reported_with_its_fix(self):
        step, path = device_install.check_native(remote(lambda argv, stdin: answer(1)))
        self.assertEqual((step.name, step.status, path), ('native-claude', 'missing', None))
        self.assertIn('claude.ai/install.sh', step.fix)

    def test_native_claude_path_is_returned(self):
        step, path = device_install.check_native(remote(lambda argv, stdin: answer(0, '/opt/u/.local/bin/claude\n')))
        self.assertEqual((step.status, path), ('ok', '/opt/u/.local/bin/claude'))

    def test_missing_launcher_is_a_missing_step(self):
        self.assertEqual(device_install.check_install(remote(lambda argv, stdin: answer(127))), Step('install', 'missing', ANY, ANY))

    def test_version_difference_is_missing(self):
        fake = remote(lambda argv, stdin: answer(0, json.dumps({'version': 'old'})))
        self.assertEqual(device_install.check_version(fake, 'new').status, 'missing')
        self.assertEqual(device_install.check_version(fake, 'old').status, 'ok')

    def test_service_is_read_from_status_and_started_with_start(self):
        fake = remote(lambda argv, stdin: answer(0, json.dumps({'service': 'stopped'})))
        self.assertEqual(device_install.check_service(fake).status, 'missing')
        device_install.start_service(fake)
        self.assertIn('--start', fake.calls[-2][0])

    def test_doctor_failure_carries_the_first_failing_fix(self):
        out = '  warn a: soon\n  fail b: token expired · fix: claude-pool account add --long-lived\n'
        step = device_install.check_doctor(remote(lambda argv, stdin: answer(1, out)))
        self.assertEqual((step.status, step.fix), ('failed', 'claude-pool account add --long-lived'))

    def test_linger_failure_reports_the_one_root_command_without_running_sudo(self):
        def denied(argv, stdin):
            if argv.startswith('uname'): return answer(0, 'Linux')
            if argv.startswith('id'): return answer(0, 'justus')
            if argv.startswith('loginctl enable-linger'): return answer(1, err='denied')
            return answer(0, 'no')
        fake = remote(denied)
        step = device_install.enable_linger(fake)
        self.assertEqual(step.status, 'failed'); self.assertIn('sudo loginctl enable-linger justus', step.fix)
        self.assertFalse([a for a, _ in fake.calls if 'sudo' in ' '.join(a)])

    def test_macos_skips_linger(self):
        self.assertEqual(device_install.check_linger(remote(lambda argv, stdin: answer(0, 'Darwin'))).status, 'skipped')


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='shell-test-')); self.rc = self.dir / '.zshrc'

    def test_shell_rc_follows_the_login_shell(self):
        self.assertEqual(device_install.shell_rc({'HOME': '/h', 'SHELL': '/bin/zsh'}), Path('/h/.zshrc'))
        self.assertEqual(device_install.shell_rc({'HOME': '/h'}), Path('/h/.bashrc'))

    def test_shell_block_is_added_once_and_legacy_line_counts_as_present(self):
        self.rc.write_text('export X=1\nclaude() { command claude-pool exec "$@"; }\n')
        self.assertTrue(device_install.shell_check(self.rc)); self.assertFalse(device_install.shell_add(self.rc))
        self.assertEqual(self.rc.read_text().count('claude-pool exec'), 1)

    def test_shell_add_then_remove_deletes_only_the_managed_block(self):
        self.rc.write_text('export X=1\n')
        self.assertTrue(device_install.shell_add(self.rc)); self.assertFalse(device_install.shell_add(self.rc))
        self.assertTrue(device_install.shell_remove(self.rc)); self.assertEqual(self.rc.read_text(), 'export X=1\n')

    def test_remove_leaves_a_legacy_line_alone(self):
        self.rc.write_text('claude() { command claude-pool exec "$@"; }\n')
        self.assertFalse(device_install.shell_remove(self.rc)); self.assertTrue(device_install.shell_check(self.rc))

    def test_status_part_is_registered_and_never_raises(self):
        self.assertIn(device_install._local_status, device_sync.STATUS_PARTS)
        with mock.patch.object(device_install, 'shell_rc', side_effect=RuntimeError):
            self.assertEqual(device_install._local_status()['shell'], False)


class ManagedBlockTests(unittest.TestCase):
    def test_managed_block_round_trip_keeps_surrounding_text(self):
        S, E = '# s', '# e'
        path = Path(tempfile.mkdtemp(prefix='block-test-')) / 'rc'
        path.write_text('a\nb\n'); path.chmod(0o640)
        self.assertTrue(managed_block.write(path, S, E, 'x')); self.assertFalse(managed_block.write(path, S, E, 'x'))
        self.assertEqual(managed_block.read(path, S, E), 'x'); self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertTrue(managed_block.write(path, S, E, 'y')); self.assertEqual(managed_block.read(path, S, E), 'y')
        managed_block.write(path, S, E, None); self.assertEqual(path.read_text(), 'a\nb\n')

    def test_removing_from_a_missing_file_creates_nothing(self):
        path = Path(tempfile.mkdtemp(prefix='block-test-')) / 'none'
        self.assertFalse(managed_block.write(path, '# s', '# e', None)); self.assertFalse(path.exists())


class ReportTests(unittest.TestCase):
    def test_render_and_ready_follow_the_pinned_format(self):
        out = render('vps', [Step('shell', 'missing', 'no function', 'claude-pool device add vps'), Step('app', 'manual', 'not listed', 'click …')])
        self.assertIn('\n  missing     shell         no function\n      fix: claude-pool device add vps', out)
        self.assertIn('\nNeeds you:\n  app: click …', out)
        self.assertTrue(ready([Step('app', 'manual', '', 'x'), Step('herdr', 'skipped', '', None)]))
        self.assertFalse(ready([Step('shell', 'missing', '', 'x')]))


if __name__ == '__main__': unittest.main()
