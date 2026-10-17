"""The SSH transport keeps secrets on stdin and builds one fixed, quoted command."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from remote import Completed, Local, Remote, redact, require_pipe
from fakes import FakeRemote, SecretWatch, fixture_token


def ok(*a, **k): return subprocess.CompletedProcess(a, 0, b'out', b'')


class RemoteTests(unittest.TestCase):
    def setUp(self): self.run = mock.Mock(side_effect=ok)

    def test_ssh_argv_is_batch_mode_and_command_is_one_quoted_word(self):
        Remote('personal-vps', run=self.run).call(['echo', 'a b'])
        self.assertEqual(self.run.call_args.args[0], ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-T', 'personal-vps', "echo 'a b'"])

    def test_targets_that_look_like_options_or_contain_spaces_are_refused(self):
        for target in ['-oProxyCommand=x', 'a b', '', 'x;y']:
            with self.assertRaises(ValueError): Remote(target)

    def test_stdin_bytes_reach_the_process_and_never_the_argv(self):
        Remote('vps', run=self.run).call(['cat'], stdin=fixture_token().encode())
        self.assertEqual(self.run.call_args.kwargs['input'], fixture_token().encode())
        SecretWatch(fixture_token()).argv([(self.run.call_args.args[0], None)])

    def test_pool_calls_use_the_home_relative_launcher(self):
        Remote('vps', run=self.run).pool('device', 'status', '--json')
        self.assertTrue(self.run.call_args.args[0][-1].startswith('.local/bin/claude-pool device status'))

    def test_unreachable_target_returns_false_without_raising(self):
        def returns_255(*a, **k): return subprocess.CompletedProcess(a, 255, b'', b'')
        def raises_timeout(*a, **k): raise subprocess.TimeoutExpired('ssh', 1)
        self.assertFalse(Remote('vps', run=returns_255).reachable())
        self.assertFalse(Remote('vps', run=raises_timeout).reachable())
        self.assertTrue(Remote('vps', run=self.run).reachable())

    def test_timeout_and_os_errors_become_255_results(self):
        for error in [subprocess.TimeoutExpired('ssh', 1), FileNotFoundError('ssh')]:
            done = Remote('vps', run=mock.Mock(side_effect=error)).call(['true'])
            self.assertEqual(done.returncode, 255)

    def test_redact_replaces_every_secret_occurrence(self):
        self.assertEqual(redact('a SECRET b SECRET', ['SECRET']), 'a [redacted] b [redacted]')

    def test_require_pipe_refuses_a_terminal(self):
        tty = mock.Mock(); tty.isatty.return_value = True
        with self.assertRaises(RuntimeError): require_pipe(tty)
        self.assertIsNone(require_pipe(io.BytesIO()))

    def test_fake_remote_records_pool_calls_with_the_launcher(self):
        fake = FakeRemote('vps', lambda argv, stdin: Completed(0, b'', b''))
        fake.pool('device', 'registry')
        self.assertEqual(fake.calls[-1][0], ['.local/bin/claude-pool', 'device', 'registry'])

    def test_local_runs_argv_directly_with_the_installed_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            receipt = Path(folder) / 'installation.json'
            receipt.write_text(json.dumps({'launchers': {'/x/bin/claude-native': 'h', '/x/bin/claude-pool': 'h'}}))
            local = Local(run=self.run, receipt=receipt)
            local.call(['echo', 'a b'])
            self.assertEqual(self.run.call_args.args[0], ['echo', 'a b'])
            local.pool('device', 'status')
            self.assertEqual(self.run.call_args.args[0], ['/x/bin/claude-pool', 'device', 'status'])
            self.assertTrue(local.reachable())

    def test_secret_watch_detects_leaks(self):
        watch = SecretWatch('SECRET')
        with self.assertRaises(AssertionError): watch.argv([(['x', 'aSECRETb'], None)])
        with self.assertRaises(AssertionError): watch.text('SECRET')
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'f').write_text('SECRET')
            with self.assertRaises(AssertionError): watch.tree(Path(folder), set())
            watch.tree(Path(folder), {Path(folder) / 'f'})


if __name__ == '__main__': unittest.main()
