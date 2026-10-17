"""Install, service, linger and shell steps run through a fake SSH remote; nothing touches the real machine or the network."""
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
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
        self.assertEqual(step.status, 'failed'); self.assertEqual(step.detail, 'install failed: line4\nline5\nline6\nline7\nline8')

    def test_missing_native_claude_is_reported_with_its_fix(self):
        step, path = device_install.check_native(remote(lambda argv, stdin: answer(1)))
        self.assertEqual((step.name, step.status, path), ('native-claude', 'missing', None))
        self.assertIn('claude.ai/install.sh', step.fix)

    def test_native_claude_path_is_returned(self):
        step, path = device_install.check_native(remote(lambda argv, stdin: answer(0, '/opt/u/.local/bin/claude\n')))
        self.assertEqual((step.status, path), ('ok', '/opt/u/.local/bin/claude'))

    def test_missing_launcher_is_a_missing_step(self):
        self.assertEqual(device_install.check_install(remote(lambda argv, stdin: answer(10))), Step('install', 'missing', ANY, ANY))

    def test_old_launcher_without_device_support_is_never_called_with_device(self):
        fake = remote(lambda argv, stdin: answer(11))
        step = device_install.check_install(fake)
        self.assertEqual((step.status, step.detail), ('missing', 'older claude-pool without device support'))
        self.assertFalse([a for a, _ in fake.calls if a[:2] == ['.local/bin/claude-pool', 'device']])

    def launcher(self, folder='claude pool', current=True, support=True, quote=True):
        home = Path(tempfile.mkdtemp(prefix='launcher-')); self.addCleanup(shutil.rmtree, home)
        prefix = home / '.local/share' / folder; (home / '.local/bin').mkdir(parents=True); prefix.mkdir(parents=True)
        release = prefix / 'versions/abc' if current else prefix
        (release / 'gateway').mkdir(parents=True)
        if support: (release / 'gateway/devices_cli.py').write_text('')
        if current: (prefix / 'current').symlink_to(release)
        run = shlex.quote(str(prefix / 'run.py')) if quote else str(prefix / 'run.py')
        (home / '.local/bin/claude-pool').write_text('#!/bin/sh\n# managed by claude-pool installer\nexport X=1\nexec /usr/bin/python3 ' + run + ' "$@"\n')
        return subprocess.run(['sh', '-c', device_install.LAUNCHER_SCRIPT], env={'HOME': str(home), 'PATH': '/usr/bin:/bin'}, capture_output=True).returncode

    def test_launcher_script_reads_the_real_layouts(self):
        self.assertEqual(self.launcher(current=True), 0)
        self.assertEqual(self.launcher(current=False), 0)
        self.assertEqual(self.launcher(current=True, support=False), 11)
        self.assertEqual(self.launcher(current=False, support=False), 11)
        self.assertEqual(self.launcher(folder='plain', quote=False), 0)

    def test_launcher_script_without_a_wrapper_exits_10(self):
        home = Path(tempfile.mkdtemp(prefix='launcher-')); self.addCleanup(shutil.rmtree, home)
        done = subprocess.run(['sh', '-c', device_install.LAUNCHER_SCRIPT], env={'HOME': str(home), 'PATH': '/usr/bin:/bin'}, capture_output=True)
        self.assertEqual(done.returncode, 10)

    def test_launcher_with_device_support_reports_its_status(self):
        fake = remote(lambda argv, stdin: answer(0, '{}') )
        self.assertEqual(device_install.check_install(fake).status, 'ok')
        self.assertEqual(fake.calls[-1][0][:3], ['.local/bin/claude-pool', 'device', 'status'])

    def test_install_without_python_is_failed_with_the_fix(self):
        step = device_install.install(remote(lambda argv, stdin: answer(3, err='no python')), b'x', '/n')
        self.assertEqual((step.status, step.fix), ('failed', 'install Python 3.11 or newer on vps'))

    def test_scripts_prepend_homebrew_paths_and_pick_a_new_python(self):
        for script in (device_install.INSTALL_SCRIPT, device_install.NATIVE_SCRIPT):
            self.assertIn('/opt/homebrew/bin:/usr/local/bin', script)
        for name in ('python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3'): self.assertIn(name, device_install.INSTALL_SCRIPT)

    def test_install_script_runs_the_installer_with_a_spaced_argument_and_cleans_up(self):
        body = b'import sys,os; print(sys.argv[1:]); print(os.path.dirname(os.path.abspath(__file__)))\n'
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w:gz') as t:
            i = tarfile.TarInfo('claude-pool/install.py'); i.size = len(body); t.addfile(i, io.BytesIO(body))
        done = subprocess.run(['sh', '-c', device_install.INSTALL_SCRIPT, 'install', '/opt/my claude'], input=raw.getvalue(), capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.decode().splitlines()
        self.assertIn("'/opt/my claude'", lines[0]); self.assertFalse(os.path.exists(os.path.dirname(lines[1])))

    def test_source_archive_skips_files_an_installed_release_lacks(self):
        root = Path(tempfile.mkdtemp(prefix='release-')); self.addCleanup(shutil.rmtree, root)
        (root / 'scripts').mkdir(); (root / 'gateway').mkdir()
        shutil.copy(ROOT / 'scripts/package.py', root / 'scripts/package.py')
        (root / 'install.py').write_text('x'); (root / 'gateway/a.py').write_text('y')
        names = tar_names(device_install.source_archive(root))
        self.assertIn('claude-pool/install.py', names); self.assertIn('claude-pool/gateway/a.py', names)
        self.assertNotIn('claude-pool/install.sh', names)

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

    def test_missing_logind_skips_linger_with_the_reason(self):
        def handler(argv, stdin):
            if argv.startswith('uname'): return answer(0, 'Linux')
            return answer(1, err='Failed to connect to bus')
        step = device_install.check_linger(remote(handler))
        self.assertEqual(step.status, 'skipped'); self.assertIn('Failed to connect to bus', step.detail)

    def test_start_failure_fix_names_the_target(self):
        step = device_install.start_service(remote(lambda argv, stdin: answer(1, err='x')))
        self.assertEqual(step.fix, 'ssh vps claude-pool --start')

    def test_macos_skips_linger(self):
        self.assertEqual(device_install.check_linger(remote(lambda argv, stdin: answer(0, 'Darwin'))).status, 'skipped')


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='shell-test-')); self.addCleanup(shutil.rmtree, self.dir); self.rc = self.dir / '.zshrc'

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
            self.assertEqual(device_install._local_status(), {'linger': ANY, 'shell': None})


class ManagedBlockTests(unittest.TestCase):
    def test_managed_block_round_trip_keeps_surrounding_text(self):
        S, E = '# s', '# e'
        path = Path(tempfile.mkdtemp(prefix='block-test-')); self.addCleanup(shutil.rmtree, path); path /= 'rc'
        path.write_text('a\nb\n'); path.chmod(0o640)
        self.assertTrue(managed_block.write(path, S, E, 'x')); self.assertFalse(managed_block.write(path, S, E, 'x'))
        self.assertEqual(managed_block.read(path, S, E), 'x'); self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertTrue(managed_block.write(path, S, E, 'y')); self.assertEqual(managed_block.read(path, S, E), 'y')
        managed_block.write(path, S, E, None); self.assertEqual(path.read_text(), 'a\nb\n')

    def make(self, data):
        folder = Path(tempfile.mkdtemp(prefix='block-test-')); self.addCleanup(shutil.rmtree, folder)
        path = folder / 'rc'; path.write_bytes(data); return path

    def test_orphan_start_raises_and_keeps_every_user_line(self):
        raw = b'a\n# s\nuser 1\nuser 2\n'; path = self.make(raw)
        for op in (lambda: managed_block.read(path, '# s', '# e'), lambda: managed_block.write(path, '# s', '# e', 'x'), lambda: managed_block.write(path, '# s', '# e', None)):
            with self.assertRaises(ValueError) as caught: op()
            self.assertIn(str(path), str(caught.exception))
        self.assertEqual(path.read_bytes(), raw)

    def test_orphan_end_and_two_blocks_raise(self):
        for raw in (b'a\n# e\nb\n', b'# s\nx\n# e\nb\n# s\ny\n# e\n'):
            path = self.make(raw)
            with self.assertRaises(ValueError): managed_block.write(path, '# s', '# e', 'z')
            self.assertEqual(path.read_bytes(), raw)

    def test_crlf_and_non_utf8_bytes_survive_and_the_block_uses_the_files_line_ending(self):
        for raw in (b'a\r\nb\r\n', b'x=\xe9\n', b'x=\xe9\r\ny\r\n'):
            path = self.make(raw)
            self.assertTrue(managed_block.write(path, '# s', '# e', 'l1\nl2'))
            data = path.read_bytes(); self.assertTrue(data.startswith(raw))
            if b'\r\n' in raw: self.assertEqual(data.count(b'\r\n'), data.count(b'\n'))
            else: self.assertNotIn(b'\r', data)
            self.assertEqual(managed_block.read(path, '# s', '# e'), 'l1\nl2')
            self.assertTrue(managed_block.write(path, '# s', '# e', None)); self.assertEqual(path.read_bytes(), raw)

    def test_symlinked_file_is_written_through_and_stays_a_link(self):
        real = self.make(b'a\n'); real.chmod(0o640); link = real.parent / 'link'; link.symlink_to(real)
        self.assertTrue(managed_block.write(link, '# s', '# e', 'x'))
        self.assertTrue(link.is_symlink()); self.assertEqual(managed_block.read(real, '# s', '# e'), 'x')
        self.assertEqual(real.stat().st_mode & 0o777, 0o640)
        managed_block.write(link, '# s', '# e', None); self.assertEqual(real.read_bytes(), b'a\n')

    def test_no_trailing_newline_round_trips(self):
        path = self.make(b'export X=1')
        managed_block.write(path, '# s', '# e', 'x'); managed_block.write(path, '# s', '# e', None)
        self.assertEqual(path.read_bytes(), b'export X=1')

    def test_a_blank_line_before_someone_elses_block_is_kept(self):
        path = self.make(b'a\n\n# s\nx\n# e\nb\n')
        managed_block.write(path, '# s', '# e', None); self.assertEqual(path.read_bytes(), b'a\n\nb\n')

    def test_removing_from_a_missing_file_creates_nothing(self):
        path = Path(tempfile.mkdtemp(prefix='block-test-')); self.addCleanup(shutil.rmtree, path); path /= 'none'
        self.assertFalse(managed_block.write(path, '# s', '# e', None)); self.assertFalse(path.exists())


class ReportTests(unittest.TestCase):
    def test_render_and_ready_follow_the_pinned_format(self):
        out = render('vps', [Step('shell', 'missing', 'no function', 'claude-pool device add vps'), Step('app', 'manual', 'not listed', 'click …')])
        self.assertIn('\n  missing     shell         no function\n      fix: claude-pool device add vps', out)
        self.assertIn('\nNeeds you:\n  app: click …', out)
        self.assertTrue(ready([Step('app', 'manual', '', 'x'), Step('herdr', 'skipped', '', None)]))
        self.assertFalse(ready([Step('shell', 'missing', '', 'x')]))


if __name__ == '__main__': unittest.main()
