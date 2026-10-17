"""Native login per machine. A fake ssh runs the remote command locally against a fake Native; nothing touches the
real Native login, the real ssh config, a real machine or the network."""
import contextlib
import inspect
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
import device_sync
import native_login
from device_report import Step
from fakes import FakeRemote, SecretWatch
from remote import Completed

ANY = mock.ANY
ACCOUNTS = [{'name': 'team-a', 'email': 'ops@x.de', 'organization': 'Team A'},
            {'name': 'solo', 'email': 'solo@x.de'}]

FAKE_SSH = '''#!{python}
import json, os, shlex, sys
argv = sys.argv[1:]
with open(os.environ['FAKE_SSH_LOG'], 'a') as log: log.write(json.dumps(argv) + '\\n')
if '-M' in argv:
    socket = argv[argv.index('-S') + 1]
    with open(os.environ['FAKE_SSH_LOG'] + '.mode', 'w') as out: out.write(oct(os.stat(os.path.dirname(socket)).st_mode & 0o777))
    sys.exit(int(os.environ.get('FAKE_SSH_MASTER_CODE', '0')))
if '-O' in argv: sys.exit(int(os.environ.get('FAKE_SSH_FORWARD_CODE', '0')) if 'forward' in argv else 0)
# A session: the last word is the command the remote shell would run. "The remote machine" has Native at /opt/claude.
words = [os.environ['FAKE_NATIVE'] if word == '/opt/claude' else word for word in shlex.split(argv[-1])]
os.execvp(words[0], words)
'''

FAKE_NATIVE = '''#!/bin/sh
[ "$1 $2" = "auth login" ] || exit 64
printf '%s\\n' "$BROWSER" > "$FAKE_BROWSER_SEEN"
printf '%s\\n' "${DISPLAY-unset}" > "$FAKE_DISPLAY_SEEN"
echo "Opening browser to sign in. CHILD-SECRET"
echo "If the browser did not open, visit: https://claude.com/cai/oauth/authorize?code=true&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=CHILD-SECRET"
case "$FAKE_NATIVE_MODE" in
  loopback|fail) "$BROWSER" "https://claude.com/cai/oauth/authorize?code=true&redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback&state=fixture-state" ;;
  platform) "$BROWSER" "https://claude.com/cai/oauth/authorize?code=true&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=fixture-state" ;;
esac
case "$FAKE_NATIVE_MODE" in
  loopback|fail)
    i=0
    while [ ! -e "$FAKE_CLICKED" ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done
    echo "Login successful CHILD-SECRET"
    [ "$FAKE_NATIVE_MODE" = fail ] && exit 1
    exit 0 ;;
  *) printf 'Paste code here if prompted > '; sleep 30 ;;
esac
'''


def fake_status(email, organization, logged_in=True):
    value = {'loggedIn': logged_in, 'email': email, 'orgName': organization, 'authMethod': 'claude.ai'}
    return FakeRemote('vps', lambda argv, stdin: Completed(0, json.dumps(value).encode(), b''))


@contextlib.contextmanager
def captured_output():
    """Everything written to stdout or stderr, by Python or by any child process."""
    box, sink, saved = {}, tempfile.TemporaryFile(), (os.dup(1), os.dup(2))
    sys.stdout.flush(); sys.stderr.flush()
    os.dup2(sink.fileno(), 1); os.dup2(sink.fileno(), 2)
    text = io.StringIO()
    try:
        with contextlib.redirect_stdout(text), contextlib.redirect_stderr(text): yield box
    finally:
        sys.stdout.flush(); sys.stderr.flush()
        os.dup2(saved[0], 1); os.dup2(saved[1], 2); os.close(saved[0]); os.close(saved[1])
        sink.seek(0); box['text'] = sink.read().decode('utf-8', 'replace') + text.getvalue(); sink.close()


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix='native-login-test-')); self.addCleanup(shutil.rmtree, self.folder, True)
        self.ssh = self.folder / 'ssh'
        self.ssh.write_text(FAKE_SSH.format(python=sys.executable)); self.ssh.chmod(0o755)
        native = self.folder / 'native'
        native.write_text(FAKE_NATIVE); native.chmod(0o755)
        self.log, self.clicked = self.folder / 'ssh.log', self.folder / 'clicked'
        self.env = {'FAKE_SSH_LOG': str(self.log), 'FAKE_NATIVE': str(native), 'FAKE_CLICKED': str(self.clicked),
                    'FAKE_BROWSER_SEEN': str(self.folder / 'browser'), 'FAKE_DISPLAY_SEEN': str(self.folder / 'display'),
                    'DISPLAY': ':0'}
        self.opener = mock.Mock(side_effect=lambda url: self.clicked.touch() or True)

    def login(self, mode, **env):
        with mock.patch.dict(os.environ, {**self.env, 'FAKE_NATIVE_MODE': mode, **env}), \
                mock.patch.object(native_login, 'MARKER_WAIT', 2), captured_output() as out:
            step = native_login.login('vps', '/opt/claude', opener=self.opener, ssh=str(self.ssh), timeout=30)
        self.output = out['text']
        return step

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_loopback_login_forwards_the_callback_port_and_needs_only_the_click(self):
        step = self.login('loopback')
        forwarded = [argv[2:6] for argv in self.calls()]
        self.assertIn(['-O', 'forward', '-L', '54545:127.0.0.1:54545'], forwarded)
        self.opener.assert_called_once()
        self.assertIn('redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback', self.opener.call_args.args[0])
        self.assertEqual(step, Step('native-login', 'ok', ANY, None))

    def test_the_platform_callback_url_native_prints_is_never_opened(self):
        self.login('loopback')
        self.assertNotIn('platform.claude.com', self.opener.call_args.args[0])

    def test_non_loopback_login_is_manual_with_exact_instructions_and_reads_no_code(self):
        step = self.login('platform')
        self.assertEqual(step.status, 'manual'); self.assertIn('ssh -t vps /opt/claude auth login', step.fix)
        self.assertEqual(step.fix, 'Native login on vps needs you once: in a terminal on your Mac run ssh -t vps /opt/claude auth login, '
                                   'open the link it prints, click Authorize and finish there. Then rerun claude-pool device add vps.')
        self.opener.assert_not_called(); self.assertNotIn('code_reader', inspect.signature(native_login.login).parameters)
        self.assertFalse([argv for argv in self.calls() if 'forward' in argv])
        self.assertFalse(Path((self.folder / 'browser').read_text().strip()).parent.exists())

    def test_no_browser_handoff_within_the_wait_is_manual(self):
        step = self.login('silent')
        self.assertEqual(step.status, 'manual'); self.opener.assert_not_called()

    def test_control_master_is_closed_on_every_path(self):
        for mode in ['loopback', 'platform', 'fail']:
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True); self.clicked.unlink(missing_ok=True)
                self.login(mode)
                calls = self.calls()
                master = calls[0]
                self.assertEqual(master[:4], ['-M', '-S', master[2], '-fN'])
                self.assertEqual(calls[-1], ['-S', master[2], '-O', 'exit', 'vps'])
                self.assertFalse(Path(master[2]).parent.exists())

    def test_control_socket_lives_in_a_private_directory(self):
        self.login('loopback')
        self.assertEqual((self.log.parent / (self.log.name + '.mode')).read_text(), '0o700')

    def test_child_that_fails_after_the_click_is_failed(self):
        step = self.login('fail')
        self.assertEqual(step.status, 'failed'); self.assertIn('claude-pool device add vps', step.fix)

    def test_unreachable_target_starts_no_login_and_still_closes(self):
        step = self.login('loopback', FAKE_SSH_MASTER_CODE='255')
        self.assertEqual(step.status, 'unreachable'); self.opener.assert_not_called()
        calls = self.calls()
        self.assertEqual(len(calls), 2); self.assertEqual(calls[-1][2:], ['-O', 'exit', 'vps'])

    def test_a_forward_that_fails_opens_nothing_and_is_manual(self):
        step = self.login('loopback', FAKE_SSH_FORWARD_CODE='255')
        self.assertEqual(step.status, 'manual'); self.opener.assert_not_called()

    def test_child_output_is_never_echoed(self):
        for mode in ['loopback', 'platform', 'fail']:
            with self.subTest(mode=mode):
                self.clicked.unlink(missing_ok=True)
                step = self.login(mode)
                SecretWatch('CHILD-SECRET', 'claude-pool-open', 'fixture-state').text(self.output, step.detail, step.fix or '')

    def test_native_path_is_a_positional_argument_of_the_remote_helper(self):
        self.login('loopback')
        session = next(argv for argv in self.calls() if '-tt' in argv)
        words = shlex.split(session[-1])
        self.assertEqual(words[:2], ['sh', '-c']); self.assertEqual(words[-1], '/opt/claude')
        self.assertNotIn('/opt/claude', words[2])

    def test_remote_helper_hands_the_browser_to_a_private_temporary_helper_and_removes_it(self):
        self.login('loopback')
        helper = Path((self.folder / 'browser').read_text().strip())
        self.assertNotEqual(str(helper), ''); self.assertFalse(helper.parent.exists())
        self.assertEqual((self.folder / 'display').read_text().strip(), 'unset')

    def test_an_invalid_target_is_refused_before_any_ssh(self):
        with mock.patch.dict(os.environ, self.env):
            with self.assertRaises(ValueError): native_login.login('-oProxyCommand=x', '/opt/claude', opener=self.opener, ssh=str(self.ssh))
        self.assertFalse(self.log.exists())


class LoopbackTests(unittest.TestCase):
    def test_only_a_localhost_http_redirect_with_a_port_is_loopback(self):
        base = 'https://claude.com/cai/oauth/authorize?code=true&redirect_uri='
        self.assertEqual(native_login.loopback_port(base + 'http%3A%2F%2Flocalhost%3A54545%2Fcallback'), 54545)
        self.assertEqual(native_login.loopback_port(base + 'http%3A%2F%2F127.0.0.1%3A4000%2Fcallback'), 4000)
        for redirect in ['https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback', 'http%3A%2F%2Flocalhost%2Fcallback',
                         'http%3A%2F%2Fevil.example%3A54545%2Fcallback', 'http%3A%2F%2Flocalhost%3Axyz%2F']:
            with self.subTest(redirect=redirect): self.assertIsNone(native_login.loopback_port(base + redirect))
        self.assertIsNone(native_login.loopback_port('file:///etc/passwd?redirect_uri=http%3A%2F%2Flocalhost%3A1%2F'))


class CheckTests(unittest.TestCase):
    def test_native_login_not_among_pool_accounts_is_reported_with_fix(self):
        step = native_login.check(fake_status('other@x.de', 'Other'), '/opt/claude', ACCOUNTS)
        self.assertEqual(step.status, 'failed'); self.assertIn('claude-native auth login', step.fix)
        self.assertEqual(step.fix, 'log in on vps with an account the pool serves: claude-native auth login')

    def test_matching_native_login_is_ok_regardless_of_case_and_spacing(self):
        self.assertEqual(native_login.check(fake_status('Ops@X.de', ' Team  A '), '/opt/claude', ACCOUNTS).status, 'ok')

    def test_same_email_in_another_organization_does_not_match(self):
        self.assertEqual(native_login.check(fake_status('ops@x.de', 'Other'), '/opt/claude', ACCOUNTS).status, 'failed')

    def test_account_without_organization_matches_on_email(self):
        self.assertEqual(native_login.check(fake_status('solo@x.de', 'Anything'), '/opt/claude', ACCOUNTS).status, 'ok')

    def test_signed_out_native_is_missing_with_the_add_fix(self):
        step = native_login.check(fake_status('', '', logged_in=False), '/opt/claude', ACCOUNTS)
        self.assertEqual(step, Step('native-login', 'missing', ANY, 'claude-pool device add vps'))

    def test_status_is_asked_from_the_given_native(self):
        fake = fake_status('ops@x.de', 'Team A')
        native_login.check(fake, '/opt/claude', ACCOUNTS)
        self.assertEqual(fake.calls[0][0], ['/opt/claude', 'auth', 'status', '--json'])

    def test_unreadable_status_is_failed_without_its_output(self):
        fake = FakeRemote('vps', lambda argv, stdin: Completed(1, b'boom CHILD-SECRET', b'CHILD-SECRET'))
        step = native_login.check(fake, '/opt/claude', ACCOUNTS)
        self.assertEqual(step.status, 'failed'); self.assertIn('claude-native auth status', step.fix)
        SecretWatch('CHILD-SECRET').text(step.detail, step.fix)


class StatusPartTests(unittest.TestCase):
    def test_status_part_is_registered(self):
        self.assertIn(native_login.local_status, device_sync.STATUS_PARTS)

    def test_status_part_reports_the_local_native_login(self):
        with mock.patch.object(device_sync, 'default_native_identity', return_value={'email': 'ops@x.de', 'organization': 'Team A'}):
            self.assertEqual(native_login.local_status(), {'nativeLogin': {'email': 'ops@x.de', 'organization': 'Team A'}})

    def test_status_part_is_none_when_signed_out_or_unreadable(self):
        with mock.patch.object(device_sync, 'default_native_identity', return_value=None):
            self.assertEqual(native_login.local_status(), {'nativeLogin': None})
        with mock.patch.object(device_sync, 'default_native_identity', side_effect=RuntimeError('x')):
            self.assertEqual(native_login.local_status(), {'nativeLogin': None})


if __name__ == '__main__': unittest.main()
