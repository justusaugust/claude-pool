"""Preview-link install steps run against a fake remote; no ssh, tailscale or systemd is touched."""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
sys.path.insert(0, str(ROOT / 'tests'))
import device_install
import device_preview
import device_sync
from fakes import FakeRemote
from remote import Completed

TOOL_SHA = '8cba203c1f2566ec033d47dc39189c7dbf0643fc68fd619720f8a19c1bc4c0d0'
PREVIOUS_SHA = 'b3e645e7ba9750057b68c12f7dd518d19ff792a98062eeea857115b194358d94'  # the copy live on personal-vps
FIRST_FILE_SHARE_SHA = '89c880b1411939c7996239273e28aa51399a1dca1cd56e387754e82e235faabe'  # Task 16's first release
BEFORE_HOST_CHECK_SHA = '7894d83a893ab63cc07ec07244b910ae8fce1949b080ba355a3b08f6e3903efd'  # the release before the Host check
BEFORE_CUA_SHA = 'fc7f0f3162eadf48ca4f1b942aef8b25f6446fba8fdc1e64bafc959c718a5a11'  # the release before `preview cua`
FIRST_CUA_SHA = 'ce9f107b5620bea133df4972781e0630bc880b2b2d933f6761e3d84f855bd90b'  # first `preview cua`, on the VPS
VIEWER_COOKIE_SHA = '27adcbcf4e189571c911730040e606e5a961a81debe3080d62bf9d8551b489aa'  # `cua viewer` with its cookie
BEFORE_REBOOT_SHA = 'c4aaac57cf0bb23e4a64d91e2435d638d87bab1dfb66f14e2102a51a079198f5'  # one Cua link; transient units
OTHER_SHA = '0' * 64
RUN = 'cd "$HOME" && exec .local/bin/preview "$@"'
DOCTOR_HTTPS = 'tailnet name   box.ts.net\nbackend        https (Tailscale Serve is allowed for this user)\n'
PERSISTENT_YES = 'persistent     yes (every preview comes back after a reboot)\n'
PERSISTENT_NO = 'persistent     no (8820, 18500 would not come back after a reboot); fix: preview restore\n'
DOCTOR_HTTP = 'tailnet name   box.ts.net\nbackend        http (Tailscale Serve not allowed for this user; links look like http://x:<port>)\n'


def done(out='', code=0): return Completed(code, out.encode(), b'')


def _run_sh(script, name, *args, home, stdin=None):
    """Runs one of the device scripts for real in a temp HOME. PATH is a stub folder (real preview, systemctl and
    tailscale can never be reached) followed by /usr/bin:/bin for sh and the coreutils the scripts use."""
    import subprocess
    stubs = Path(home) / 'stub-bin'
    if not stubs.exists():
        stubs.mkdir()
        for tool in ('preview', 'systemctl', 'tailscale', 'claude-pool'):
            (stubs / tool).write_text('#!/bin/sh\nexit 97\n'); (stubs / tool).chmod(0o755)
    return subprocess.run(['/bin/sh', '-c', script, name, *args], input=stdin or b'', capture_output=True,
                          env={'HOME': home, 'PATH': f'{stubs}:/usr/bin:/bin'}, timeout=30)


def _gnu_ln():
    import subprocess
    try:
        done = subprocess.run(['ln', '--help'], capture_output=True, env={'PATH': '/usr/bin:/bin'}, timeout=10)
    except OSError:
        return False
    return b'--no-target-directory' in done.stdout


GNU_LN = _gnu_ln()  # the device scripts use `ln -T` and `mv -T` (GNU coreutils; devices are Linux)
needs_gnu = unittest.skipUnless(GNU_LN, 'needs GNU ln/mv -T (Linux); the script text is checked separately')


def sh(script, name, *args, home, stdin=None): return _run_sh(script, name, *args, home=home, stdin=stdin).returncode
def sh_out(script, name, *args, home): return _run_sh(script, name, *args, home=home).stdout.decode().strip()


def load_tool():
    loader = importlib.machinery.SourceFileLoader('preview_tool_under_test', str(ROOT / 'gateway/preview'))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def device(system='Linux', target_sha=None, doctor=DOCTOR_HTTPS, doctor_code=0):
    """A fake Linux (or macOS) machine. target_sha None means no preview file yet."""
    def handler(argv, stdin):
        if argv == ['uname', '-s']: return done(system + '\n')
        if argv[0] == 'sh' and 'sha256' in argv[2]: return done('absent\n' if target_sha is None else target_sha + '\n')
        if argv[0] == 'sh' and 'mktemp' in argv[2]: return done()
        if argv[-1] == 'doctor': return done(doctor, doctor_code)
        return done()
    return FakeRemote('box', handler)


def argvs(remote): return [argv for argv, _ in remote.calls]
def remove_on(remote):
    device_preview.remove(remote)
    return remote


def runs(remote): return [argv[4:] for argv in argvs(remote) if argv[:4] == ['sh', '-c', RUN, 'preview']]
def installs(remote): return [(a, s) for a, s in remote.calls if a[0] == 'sh' and 'mktemp' in a[2]]


class InstallTests(unittest.TestCase):
    def test_tool_is_the_proven_standalone_copy(self):
        self.assertEqual(hashlib.sha256(device_preview.TOOL.read_bytes()).hexdigest(), TOOL_SHA)

    def test_install_adopts_identical_file(self):
        remote = device(target_sha=TOOL_SHA)
        self.assertEqual(device_preview.install(remote).status, 'ok')
        self.assertEqual(installs(remote), [])

    def test_install_refuses_to_overwrite_a_different_preview(self):
        remote = device(target_sha=OTHER_SHA)
        step = device_preview.install(remote)
        self.assertEqual(step.status, 'failed')
        self.assertIn('rename', step.fix)
        self.assertIn('claude-pool device add box', step.fix)
        self.assertEqual(installs(remote), [])

    def test_install_streams_the_tool_on_stdin(self):
        remote = device()
        step = device_preview.install(remote)
        self.assertEqual(step.status, 'ok')
        argv, stdin = remote.calls[-1]
        self.assertEqual(stdin, device_preview.TOOL.read_bytes())
        script = argv[2]
        self.assertIn('0755', script)
        self.assertIn('ln -T ', script)  # a hard link fails when anything appeared meanwhile; nothing is overwritten
        self.assertNotIn('mv ', script)
        self.assertNotIn(TOOL_SHA, ' '.join(argv))

    def test_known_previous_copy_is_updated_in_place(self):
        self.assertEqual(device_preview.KNOWN_PREVIOUS, {PREVIOUS_SHA, FIRST_FILE_SHARE_SHA, BEFORE_HOST_CHECK_SHA, BEFORE_CUA_SHA, FIRST_CUA_SHA,
                                                         VIEWER_COOKIE_SHA, BEFORE_REBOOT_SHA})
        remote = device(target_sha=PREVIOUS_SHA)
        step = device_preview.install(remote)
        self.assertEqual((step.status, step.detail), ('ok', 'updated'))
        [(argv, stdin)] = installs(remote)
        self.assertEqual(stdin, device_preview.TOOL.read_bytes())
        script = argv[2]
        self.assertIn('0755', script); self.assertIn('mv -f -T ', script)
        # The device re-checks the file it replaces within the same shell: only the packaged or a known previous copy.
        self.assertIn(TOOL_SHA, argv[3:]); self.assertIn(PREVIOUS_SHA, argv[3:])
        self.assertIn('-L', script)

    def test_first_file_share_release_is_updated_too(self):
        self.assertEqual(device_preview.install(device(target_sha=FIRST_FILE_SHARE_SHA)).detail, 'updated')

    def test_the_release_with_transient_units_is_updated_too(self):
        self.assertNotEqual(TOOL_SHA, BEFORE_REBOOT_SHA)
        self.assertEqual(device_preview.install(device(target_sha=BEFORE_REBOOT_SHA)).detail, 'updated')

    @needs_gnu
    def test_update_script_replaces_only_a_known_file(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / '.local/bin/preview'; target.parent.mkdir(parents=True)
            run = lambda: sh(device_preview._UPDATE_SCRIPT, 'update', device_preview.TARGET,
                                              *device_preview._accepted(), home=home, stdin=b'new tool')
            target.write_bytes(b'someone else'); target.chmod(0o755)
            self.assertNotEqual(run(), 0)
            self.assertEqual(target.read_bytes(), b'someone else')
            with mock.patch.object(device_preview, 'KNOWN_PREVIOUS', {hashlib.sha256(b'someone else').hexdigest()}):
                self.assertEqual(run(), 0)
            self.assertEqual(target.read_bytes(), b'new tool')
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ['preview'])

    @needs_gnu
    def test_scripts_never_write_into_a_directory_at_the_target(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / '.local/bin/preview'; target.mkdir(parents=True)
            self.assertNotEqual(sh(device_preview._INSTALL_SCRIPT, 'install', device_preview.TARGET, home=home, stdin=b't'), 0)
            with mock.patch.object(device_preview, 'KNOWN_PREVIOUS', {hashlib.sha256(b'').hexdigest()}):
                self.assertNotEqual(sh(device_preview._UPDATE_SCRIPT, 'update', device_preview.TARGET,
                                       *device_preview._accepted(), home=home, stdin=b't'), 0)
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ['preview'])

    def test_install_and_update_scripts_use_no_target_directory(self):
        self.assertIn('ln -T "$t" "$1"', device_preview._INSTALL_SCRIPT)
        self.assertIn('mv -f -T "$t" "$f"', device_preview._UPDATE_SCRIPT)
        self.assertNotRegex(device_preview._INSTALL_SCRIPT + device_preview._UPDATE_SCRIPT, r'\b(ln|mv)(?! -)')

    def test_a_directory_at_the_target_is_refused(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / '.local/bin/preview'; target.mkdir(parents=True)
            self.assertEqual(sh_out(device_preview._SHA_SCRIPT, 'sha', device_preview.TARGET, home=home), 'notfile')
            target.rmdir(); target.symlink_to(Path(home))
            self.assertEqual(sh_out(device_preview._SHA_SCRIPT, 'sha', device_preview.TARGET, home=home), 'notfile')
        remote = device(target_sha='notfile')
        step = device_preview.install(remote)
        self.assertEqual((step.status, step.detail), ('failed', 'not a regular file'))
        self.assertEqual(installs(remote), [])
        remote = device(target_sha='notfile')
        device_preview.remove(remote)
        self.assertEqual(runs(remote), [])

    @needs_gnu
    def test_install_script_never_replaces_a_file_that_appeared(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / '.local/bin/preview'; target.parent.mkdir(parents=True)
            target.write_bytes(b'raced in')
            code = sh(device_preview._INSTALL_SCRIPT, 'install', device_preview.TARGET, home=home, stdin=b'tool')
            self.assertNotEqual(code, 0)
            self.assertEqual(target.read_bytes(), b'raced in')
            self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ['preview'])
            target.unlink(); target.symlink_to(Path(home) / 'gone')
            self.assertNotEqual(sh(device_preview._INSTALL_SCRIPT, 'install', device_preview.TARGET,
                                                   home=home, stdin=b'tool'), 0)
            self.assertTrue(target.is_symlink())
            target.unlink()
            self.assertEqual(sh(device_preview._INSTALL_SCRIPT, 'install', device_preview.TARGET,
                                                home=home, stdin=b'tool'), 0)
            self.assertEqual((target.read_bytes(), target.stat().st_mode & 0o777), (b'tool', 0o755))
            self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ['preview'])

    def test_sha_script_reports_broken_and_linked_files(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / '.local/bin/preview'; target.parent.mkdir(parents=True)
            sha = lambda: sh_out(device_preview._SHA_SCRIPT, 'sha', device_preview.TARGET, home=home)
            self.assertEqual(sha(), 'absent')
            target.symlink_to(Path(home) / 'gone')
            self.assertEqual(sha(), 'broken')
            (Path(home) / 'gone').write_bytes(b'x')
            self.assertEqual(sha(), 'link ' + hashlib.sha256(b'x').hexdigest())
            target.unlink(); target.write_bytes(b'y')
            self.assertEqual(sha(), hashlib.sha256(b'y').hexdigest())

    def test_broken_symlink_is_refused_not_replaced(self):
        remote = device(target_sha='broken')
        step = device_preview.install(remote)
        self.assertEqual(step.status, 'failed')
        self.assertIn('symlink', step.detail)
        self.assertEqual(installs(remote), [])

    def test_a_symlinked_previous_copy_is_refused(self):
        remote = device(target_sha='link ' + PREVIOUS_SHA)
        self.assertEqual(device_preview.install(remote).status, 'failed')
        self.assertEqual(installs(remote), [])
        self.assertEqual(device_preview.install(device(target_sha='link ' + TOOL_SHA)).status, 'ok')

    def test_a_failed_install_is_reported(self):
        remote = FakeRemote('box', lambda argv, stdin: done('Linux\n') if argv[0] == 'uname' else
                            done('absent\n') if 'sha256' in argv[2] else Completed(1, b'', b'disk full'))
        self.assertEqual(device_preview.install(remote).status, 'failed')

    def test_macos_skips_preview(self):
        remote = device(system='Darwin')
        step = device_preview.install(remote)
        self.assertEqual(step.status, 'skipped')
        self.assertIn('Linux only', step.detail)
        self.assertEqual(installs(remote), [])


class CheckTests(unittest.TestCase):
    def test_check_reports_http_only_with_operator_fix(self):
        step = device_preview.check(device(doctor=DOCTOR_HTTP), 'justus')
        self.assertEqual((step.status, step.detail, step.fix), ('manual', 'http-only', 'sudo tailscale set --operator=justus'))

    def test_check_ok_when_serve_is_permitted(self):
        self.assertEqual(device_preview.check(device(), 'justus').status, 'ok')

    def test_check_missing_when_the_tool_is_absent(self):
        step = device_preview.check(device(doctor='', doctor_code=127), 'justus')
        self.assertEqual(step.status, 'missing')
        self.assertIn('claude-pool device add box', step.fix)

    def test_check_failed_when_doctor_is_unreadable(self):
        self.assertEqual(device_preview.check(device(doctor='nonsense\n'), 'justus').status, 'failed')

    def test_check_reports_previews_that_would_not_survive_a_reboot(self):
        for backend in (DOCTOR_HTTPS, DOCTOR_HTTP):
            doctor = backend + PERSISTENT_NO
            step = device_preview.check(device(doctor=doctor), 'justus')
            self.assertEqual((step.status, step.fix), ('missing', 'ssh box .local/bin/preview restore'))
            self.assertIn('reboot', step.detail)

    def test_check_ok_when_every_preview_is_persistent_or_the_tool_is_older(self):
        self.assertEqual(device_preview.check(device(doctor=DOCTOR_HTTPS + PERSISTENT_YES), 'justus').status, 'ok')
        self.assertEqual(device_preview.check(device(doctor=DOCTOR_HTTPS), 'justus').status, 'ok')

    def test_check_reads_the_real_doctor_wording(self):
        tool = load_tool()
        for transient, word in ((False, 'yes'), (True, 'no')):
            out = io.StringIO()
            with mock.patch.object(tool, 'transient_previews', return_value=[{'port': 8820}] if transient else []):
                tool.persistence_line(tool.System(), out)
            self.assertEqual(device_preview._persistent(out.getvalue()), word)

    def test_check_runs_the_tool_from_home(self):
        remote = device()
        device_preview.check(remote, 'justus')
        self.assertEqual(argvs(remote), [['sh', '-c', RUN, 'preview', 'doctor']])


class RulesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / '.claude' / 'CLAUDE.md'

    def verb(self, action):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): code = device_preview.rules_verb(action, self.file)
        return code

    def test_rules_block_is_idempotent_and_keeps_the_rest_of_claude_md(self):
        self.file.parent.mkdir(); original = b'# Mine\r\n\r\nkeep this \xff exactly\r\n'
        self.file.write_bytes(original)
        self.assertEqual(self.verb('check'), 1)
        self.assertEqual(self.verb('add'), 0)
        once = self.file.read_bytes()
        self.assertEqual(self.verb('add'), 0)
        self.assertEqual(self.file.read_bytes(), once)
        self.assertEqual(self.verb('check'), 0)
        text = once.decode('utf-8', 'replace')
        self.assertEqual(text.count(device_preview.RULES_START), 1)
        for word in ('preview <port>', 'preview file <path>', 'preview stop'): self.assertIn(word, text)
        self.assertTrue(once.startswith(original))
        self.assertEqual(self.verb('remove'), 0)
        self.assertEqual(self.file.read_bytes(), original)

    def test_add_creates_the_file_and_remove_deletes_only_the_block(self):
        self.assertEqual(self.verb('add'), 0)
        self.assertTrue(self.file.exists())
        self.assertEqual(self.verb('remove'), 0)
        self.assertNotIn(device_preview.RULES_START, self.file.read_text())

    def test_malformed_markers_are_reported_and_not_rewritten(self):
        self.file.parent.mkdir(); broken = (device_preview.RULES_START + '\nno end\n').encode()
        self.file.write_bytes(broken)
        self.assertEqual(self.verb('add'), 1)
        self.assertEqual(self.file.read_bytes(), broken)

    def test_unknown_action_is_an_error(self):
        self.assertEqual(self.verb('explode'), 2)

    def test_remove_on_a_missing_claude_md_changes_nothing(self):
        self.assertEqual(self.verb('remove'), 0)
        self.assertFalse(self.file.exists())
        self.assertFalse(self.file.parent.exists())


class RemoveTests(unittest.TestCase):
    def test_remove_stops_only_previews_it_created(self):
        remote = device(target_sha=TOOL_SHA)
        step = device_preview.remove(remote)
        self.assertEqual(step.status, 'ok')
        self.assertIn(['stop', '--all'], runs(remote))
        self.assertNotIn('systemctl', ' '.join(' '.join(map(str, argv)) for argv in argvs(remote)))
        self.assertIn(['.local/bin/claude-pool', 'device', 'preview-rules', 'remove'], argvs(remote))

    def test_remove_unlinks_only_an_identical_tool(self):
        remote = device(target_sha=TOOL_SHA)
        device_preview.remove(remote)
        unlink = [a for a in argvs(remote) if a[0] == 'sh' and 'rm -f' in a[2]][0]
        self.assertIn(TOOL_SHA, unlink)  # the identity check happens on the device, against the packaged hash

    def test_remove_stops_and_unlinks_a_known_previous_copy(self):
        remote = device(target_sha=PREVIOUS_SHA)
        self.assertEqual(device_preview.remove(remote).status, 'ok')
        self.assertIn(['stop', '--all'], runs(remote))
        unlink = [a for a in argvs(remote) if a[0] == 'sh' and 'rm -f' in a[2]][0]
        self.assertIn(PREVIOUS_SHA, unlink)

    def test_remove_stops_previews_of_a_symlinked_copy_of_the_tool(self):
        self.assertEqual(runs(remove_on(device(target_sha='link ' + PREVIOUS_SHA))), [['stop', '--all']])

    def test_remove_leaves_a_different_tool_alone(self):
        for target_sha in [OTHER_SHA, 'link ' + OTHER_SHA, 'broken', None]:
            remote = device(target_sha=target_sha)
            step = device_preview.remove(remote)
            self.assertEqual(runs(remote), [], target_sha)
            self.assertEqual(step.status, 'ok', target_sha)
            self.assertIn(['.local/bin/claude-pool', 'device', 'preview-rules', 'remove'], argvs(remote))
            if target_sha == OTHER_SHA: self.assertIn('not stopped', step.detail)

    def test_unlink_script_removes_only_known_files(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / '.local/bin/preview'; target.parent.mkdir(parents=True)
            target.write_bytes(b'mine')
            known = hashlib.sha256(b'mine').hexdigest()
            sh(device_preview._UNLINK_SCRIPT, 'unlink', device_preview.TARGET, OTHER_SHA, home=home)
            self.assertTrue(target.exists())
            sh(device_preview._UNLINK_SCRIPT, 'unlink', device_preview.TARGET, OTHER_SHA, known, home=home)
            self.assertFalse(target.exists())

    def test_remove_skips_macos(self):
        self.assertEqual(device_preview.remove(device(system='Darwin')).status, 'skipped')


class ToolSafetyTests(unittest.TestCase):
    def test_proxy_binds_only_to_the_tailscale_ip(self):
        tool = load_tool()

        class Stand(tool.System):
            def __init__(self): super().__init__(); self.started = []
            def tailnet(self): return '100.101.102.103', 'box.ts.net'
            def operator_ok(self): return False
            def serve_ports(self): return set()
            def listeners(self, port): return []
            def unit_state(self, unit): return 'inactive'
            def unit_start(self, unit, desc, argv): self.started.append(argv[argv.index('--listen') + 1]); return True, ''
            def unit_stop(self, unit): pass
            def unit_log(self, unit, lines=5): return ''
            def can_connect(self, host, port, timeout=0.5): return True
            def sleep(self, s): pass
            def firewall_tailnet_ports(self, path=None): return None

        stand = Stand()
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {'PREVIEW_STATE_DIR': state, 'PREVIEW_BACKEND': 'http'}):
            code = tool.cmd_start(stand, 5173, 'x', out=io.StringIO(), err=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(stand.started, ['100.101.102.103:5173'])
        for bad in ['0.0.0.0', '::', '8.8.8.8']:
            with self.assertRaises(tool.UnsafeListenAddress): tool.check_listen_host(bad)
        self.assertEqual(tool.check_listen_host('127.0.0.1'), '127.0.0.1')
        self.assertEqual(tool.check_listen_host('100.101.102.103'), '100.101.102.103')


class PackageTests(unittest.TestCase):
    def package(self):
        spec = importlib.util.spec_from_file_location('claude_pool_package', ROOT / 'scripts/package.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_package_includes_the_preview_tool(self):
        self.assertIn('gateway/preview', set(self.package().source_files(ROOT)))

    def test_release_archive_carries_the_tool_executable(self):
        import io as _io, tarfile
        data = device_install.source_archive(ROOT)
        with tarfile.open(fileobj=_io.BytesIO(data), mode='r:gz') as archive:
            info = archive.getmember('claude-pool/gateway/preview')
            self.assertEqual(info.mode, 0o755)
            self.assertEqual(archive.extractfile(info).read(), device_preview.TOOL.read_bytes())
            self.assertEqual(archive.getmember('claude-pool/gateway/remote.py').mode, 0o644)


class StatusTests(unittest.TestCase):
    def test_status_part_reports_installed_and_https(self):
        with tempfile.TemporaryDirectory() as home:
            tool = Path(home) / '.local/bin/preview'; tool.parent.mkdir(parents=True)
            tool.write_text('#!/bin/sh\nprintf "backend        https (ok)\\n"\n'); tool.chmod(0o755)
            with mock.patch.object(Path, 'home', return_value=Path(home)):
                self.assertEqual(device_preview._status(), {'preview': {'installed': True, 'https': True}})

    def test_status_part_when_not_installed(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(Path, 'home', return_value=Path(home)):
            self.assertEqual(device_preview._status(), {'preview': {'installed': False, 'https': None}})

    def test_status_part_is_registered(self):
        self.assertIn(device_preview._status, device_sync.STATUS_PARTS)


if __name__ == '__main__': unittest.main()
