"""Cua steps against fake targets; nothing real is run and the token never leaves stdin."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway')); sys.path.insert(0, str(ROOT / 'tests'))
import device_cua
import device_sync
from fakes import FakeRemote, SecretWatch, fixture_token
from remote import Completed

IP = '100.101.102.103'
TOKEN = fixture_token().encode()
ABS = '/opt/tools/bin/cua'
OK = Completed(0, b'', b'')
NO = Completed(1, b'', b'')


def has(argv, *words): return all(w in argv for w in words)


def host(configured=True, running=True, permissions=(), url=IP + ':3211'):
    return json.dumps({'configured': configured, 'directUrl': url, 'service': {'running': running}, 'permissions': [{'id': p} for p in permissions]}).encode()


class Target:
    """A fake machine: cua presence, host state and registered spaces."""
    def __init__(self, cua=True, setup_state=None, state=None, spaces=(), mcp=False, ip=IP, fail=()):
        self.fail = set(fail)
        self.cua, self.state, self.setup_state, self.spaces, self.mcp, self.ip = cua, state or host(False, False), setup_state, list(spaces), mcp, ip
        self.remote = FakeRemote('t', self.handle)

    def handle(self, argv, stdin):
        if any(w in argv for w in self.fail): return NO
        if argv[:2] == ['sh', '-c'] and 'managed by claude-pool installer' in argv[2]: return Completed(0, b'/opt/claude\n', b'')
        if argv[:2] == ['sh', '-c'] and 'command -v cua' in argv[2]: return Completed(0, ABS.encode(), b'') if self.cua else NO
        if has(argv, 'tailscale'): return Completed(0, self.ip.encode() + b'\n', b'')
        if has(argv, 'setup'):
            if self.setup_state is not None: self.state = self.setup_state
            return Completed(0, b'pair with token ' + TOKEN, b'also ' + TOKEN)
        if has(argv, 'host', 'status'): return Completed(0, self.state, b'')
        if has(argv, 'spaces', 'ls'): return Completed(0, json.dumps({'spaces': [{'id': s} for s in self.spaces]}).encode(), b'')
        if has(argv, 'spaces', 'add'): self.spaces.append(f'direct:{IP}:3211'); return OK
        if has(argv, 'spaces', 'rm'): self.spaces = [x for x in self.spaces if x not in argv]; return OK
        if has(argv, 'host', 'remove'): self.state = host(False, False); return OK
        if has(argv, 'mcp', 'remove'): self.mcp = False; return OK
        if has(argv, 'mcp', 'get'): return OK if self.mcp else NO
        if has(argv, 'mcp', 'add'): self.mcp = True; return OK
        return OK

    def calls(self, *words): return [a for a, _ in self.remote.calls if has(a, *words)]


class CuaTests(unittest.TestCase):
    def test_host_setup_output_is_never_printed(self):
        target = Target(setup_state=host())
        buf = io.StringIO()
        with redirect_stdout(buf): step = device_cua.ensure_host(target.remote, 'mac', IP)
        self.assertEqual(step.status, 'ok')
        SecretWatch(fixture_token()).text(buf.getvalue(), repr(step), step.detail)
        self.assertEqual(target.calls('setup')[0][-6:], ['setup', '--embedded', '--direct', f'{IP}:3211', '--name', 'mac'])

    def test_peer_registration_passes_the_token_on_stdin_only(self):
        peer = Target()
        step = device_cua.register(peer.remote, TOKEN, IP, 'mac')
        self.assertEqual(step.status, 'ok')
        SecretWatch(fixture_token()).argv(peer.remote.calls)
        self.assertEqual(peer.remote.calls[-1][1], TOKEN + b'\n')
        self.assertEqual(peer.remote.calls[-1][0][3:], ['sh', 'cua', 'spaces', 'add', '--embedded', f'{IP}:3211', '--name', 'mac'])
        self.assertIn('CUA_ENV_TOKEN', peer.remote.calls[-1][0][2])

    def test_non_tailscale_address_is_refused(self):
        self.assertIsNone(device_cua.tailscale_ip(Target(ip='192.168.1.5').remote))
        self.assertEqual(device_cua.tailscale_ip(Target().remote), IP)

    def test_missing_cua_is_reported_with_install_steps_not_installed(self):
        target = Target(cua=False)
        step = device_cua.ensure_host(target.remote, 'vps', IP)
        self.assertEqual((step.status, step.fix), ('missing', 'see docs/DEVICES.md, Cua'))
        self.assertEqual(len(target.remote.calls), 1); self.assertIn('command -v cua', target.remote.calls[0][0][2])

    def test_missing_accessibility_is_a_manual_step_naming_cua_spacesd(self):
        step = device_cua.ensure_host(Target(setup_state=host(True, True, ['accessibility'])).remote, 'mac', IP)
        self.assertEqual(step.status, 'manual'); self.assertIn('cua-spacesd', step.fix)

    def test_present_host_is_kept(self):
        target = Target(state=host())
        self.assertEqual(device_cua.ensure_host(target.remote, 'mac', IP).status, 'ok')
        self.assertEqual(target.calls('setup'), [])

    def test_existing_registration_is_kept(self):
        peer = Target(spaces=[f'direct:{IP}:3211'])
        self.assertEqual(device_cua.register(peer.remote, TOKEN, IP, 'mac').status, 'ok')
        self.assertEqual(peer.calls('spaces', 'add'), [])

    def test_mcp_added_once_at_user_scope(self):
        target = Target()
        device_cua.ensure_mcp(target.remote, '/opt/claude'); device_cua.ensure_mcp(target.remote, '/opt/claude')
        self.assertEqual(target.calls('mcp', 'add'), [['/opt/claude', 'mcp', 'add', '-s', 'user', 'cua', '--', ABS, 'mcp', '--embedded']])

    def test_remove_forgets_space_host_and_mcp(self):
        device, peer = Target(state=host(), mcp=True), Target(spaces=[f'direct:{IP}:3211'])
        steps = device_cua.remove(device.remote, [peer.remote], native='/opt/claude')
        self.assertTrue(all(s.status == 'ok' for s in steps), steps)
        self.assertEqual(len(peer.calls('spaces', 'rm', f'direct:{IP}:3211')), 1)
        self.assertEqual(len(device.calls('host', 'remove')), 1)
        self.assertEqual(device.calls('mcp', 'remove'), [['/opt/claude', 'mcp', 'remove', 'cua', '-s', 'user']])

    def test_token_goes_only_to_a_private_pipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'env-token'; path.write_bytes(TOKEN + b'\n'); path.chmod(0o600)
            r, w = os.pipe()
            with os.fdopen(w, 'wb') as out: device_cua.send_token(out, path)
            self.assertEqual(os.read(r, 100), TOKEN); os.close(r)
            path.chmod(0o644)
            with self.assertRaises(RuntimeError), os.fdopen(os.open(os.devnull, os.O_WRONLY), 'wb') as out: device_cua.send_token(out, path)

    def test_status_part_registered(self):
        self.assertIn(device_cua._status, device_sync.STATUS_PARTS)

    # fix round 1
    def sendtoken(self, path, out=None):
        r, w = os.pipe()
        try:
            with os.fdopen(w, 'wb') as pipe: device_cua.send_token(out or pipe, path)
        finally: os.close(r)

    def test_send_token_refuses_symlink_foreign_owner_and_non_regular(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / 'real'; real.write_bytes(TOKEN); real.chmod(0o600)
            link = Path(tmp) / 'link'; link.symlink_to(real)
            with self.assertRaisesRegex(RuntimeError, 'private 0600 file'): self.sendtoken(link)
            with self.assertRaisesRegex(RuntimeError, 'private 0600 file'): self.sendtoken(Path(tmp))
            with self.assertRaisesRegex(RuntimeError, 'private 0600 file'): self.sendtoken(Path(tmp) / 'absent')
            real_fstat = os.fstat
            def foreign(fd):
                info = real_fstat(fd)
                return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, os.getuid() + 1, info.st_gid, info.st_size, 0, 0, 0))
            with mock.patch('device_cua.os.fstat', foreign), self.assertRaisesRegex(RuntimeError, 'private 0600 file'): self.sendtoken(real)

    def test_send_token_refuses_a_terminal(self):
        class Tty(io.BytesIO):
            def isatty(self): return True
        out = Tty()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 't'; path.write_bytes(TOKEN); path.chmod(0o600)
            with self.assertRaises(RuntimeError): device_cua.send_token(out, path)
        self.assertEqual(out.getvalue(), b'')

    def test_host_token_refuses_empty_and_failed(self):
        for done in (Completed(0, b'\n', b''), Completed(1, TOKEN, b'')):
            with self.assertRaises(RuntimeError): device_cua.host_token(FakeRemote('t', lambda a, s, d=done: d))
        self.assertEqual(device_cua.host_token(FakeRemote('t', lambda a, s: Completed(0, TOKEN + b'\n', b''))), TOKEN)

    def test_register_refuses_an_empty_token(self):
        peer = Target()
        self.assertEqual(device_cua.register(peer.remote, b' \n', IP, 'mac').status, 'failed')
        self.assertEqual(peer.calls('spaces', 'add'), [])

    def test_addresses_outside_tailscale_raise(self):
        for bad in ('0.0.0.0', '192.168.1.5', 'nope'):
            with self.assertRaises(ValueError): device_cua.host_setup_argv(bad, 'mac')
            with self.assertRaises(ValueError): device_cua.register(Target().remote, TOKEN, bad, 'mac')

    def test_host_with_another_address_is_set_up_again(self):
        for url in ('100.64.0.9:3211', None):
            target = Target(state=host(url=url), setup_state=host())
            self.assertEqual(device_cua.ensure_host(target.remote, 'mac', IP).status, 'ok')
            self.assertEqual(len(target.calls('setup')), 1)
        for url in ('http://' + IP + ':3211', 'https://' + IP + ':3211'):
            target = Target(state=host(url=url))
            device_cua.ensure_host(target.remote, 'mac', IP); self.assertEqual(target.calls('setup'), [])

    def test_failed_branches(self):
        self.assertEqual(device_cua.ensure_host(Target().remote, 'mac', IP).status, 'failed')
        self.assertEqual(device_cua.register(Target(fail=['add']).remote, TOKEN, IP, 'mac').status, 'failed')
        self.assertEqual(device_cua.ensure_mcp(Target(fail=['add']).remote, '/opt/claude').status, 'failed')
        self.assertEqual(device_cua.ensure_mcp(Target(cua=False).remote, '/opt/claude').status, 'missing')

    def test_remove_is_idempotent_and_precheck_only(self):
        device, peer = Target(state=host(), mcp=True, spaces=[]), Target(spaces=[f'direct:{IP}:3211'])
        for _ in range(2):
            steps = device_cua.remove(device.remote, [peer.remote], native='/opt/claude')
            self.assertTrue(all(s.status == 'ok' for s in steps), steps)
        self.assertEqual(len(peer.calls('spaces', 'rm')), 1)
        self.assertEqual(len(device.calls('host', 'remove')), 1)
        self.assertEqual(len(device.calls('mcp', 'remove')), 1)

    def test_remove_finds_native_itself(self):
        device = Target(mcp=True)
        device_cua.remove(device.remote, [])
        self.assertEqual(device.calls('mcp', 'remove'), [['/opt/claude', 'mcp', 'remove', 'cua', '-s', 'user']])

    def test_status_part_shape_and_timeouts(self):
        target = Target(state=host(), spaces=['direct:x:1'], mcp=True)
        timeouts = []
        class Rec:
            name = 'local'
            def call(self, argv, stdin=None, timeout=120): timeouts.append(timeout); return target.handle(list(argv), stdin)
        tunnels = mock.Mock(return_value={'vps': {'port': 32345, 'running': True}})
        with mock.patch('device_cua.Local', Rec), mock.patch('device_cua.tunnels', tunnels):
            self.assertEqual(device_cua._status(), {'cua': {'host': True, 'spaces': ['direct:x:1'], 'mcp': True,
                                                            'tunnels': {'vps': {'port': 32345, 'running': True}}}})
            self.assertEqual(set(timeouts), {10})
            target.cua = False
            self.assertEqual(device_cua._status(), {'cua': None})


# SSH tunnel when the peer's Cua port is blocked ----------------------------------------------------------------------------

SSH = 'justus@vps'
PEER = '100.108.161.69'
CONNECT_ERROR = Completed(5, b'', b'Error: tcp connect error (deadline has elapsed)\n')
AUTH_ERROR = Completed(5, b'', b'Error: 401 Unauthorized: invalid token\n')


def exact_argv(port, ip=PEER, ssh=SSH):
    return ['/usr/bin/ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=20',
            '-o', 'ServerAliveCountMax=3', '-o', 'ForwardAgent=no', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
            '-L', f'127.0.0.1:{port}:{ip}:3211', ssh]


class Manager:
    """launchctl and systemctl --user as a fake: which of our units are loaded and whether they run."""
    def __init__(self, running=True, fail=(), lingers=0):
        self.calls, self.loaded, self.running, self.fail = [], set(), running, set(fail)
        self.lingers, self.leaving = lingers, {}  # launchd: a booted-out label is still listed for `lingers` prints

    def call(self, argv, stdin=None, timeout=120):
        argv = list(argv); self.calls.append(argv)
        if any(word in self.fail for word in argv): return NO
        if argv[:2] == ['launchctl', 'print']:
            label = argv[2].rsplit('/', 1)[1]
            if self.leaving.get(label):
                self.leaving[label] -= 1; return Completed(0, b'state = exiting\n', b'')
            if label not in self.loaded: return Completed(113, b'', b'Could not find service')
            return Completed(0, b'state = ' + (b'running' if self.running else b'waiting') + b'\n', b'')
        if argv[:2] == ['launchctl', 'bootstrap']:
            label = Path(argv[3]).stem
            if self.leaving.get(label): return Completed(5, b'', b'Bootstrap failed: 5: Input/output error')
            self.loaded.add(label); return OK
        if argv[:2] == ['launchctl', 'bootout']:
            label = argv[2].rsplit('/', 1)[1]
            self.loaded.discard(label)
            if self.lingers: self.leaving[label] = self.lingers
            return OK
        if argv[:2] == ['systemctl', '--user']:
            verb = argv[2:]
            if verb[:2] == ['enable', '--now']: self.loaded.add(verb[2]); return OK
            if verb[:2] == ['disable', '--now']: self.loaded.discard(verb[2]); return OK
            if verb[:2] == ['is-active', '--quiet']: return OK if verb[2] in self.loaded and self.running else NO
            return OK
        return Completed(127, b'', b'')

    def managed(self, *words): return [a for a in self.calls if has(a, *words)]


class TunnelDevice:
    """The registering device: cua spaces (with a blocked address) and the `device cua-tunnel` verb backed by tunnel_add."""
    def __init__(self, home, blocked=(), add_error=CONNECT_ERROR, listening=(), system='darwin', manager=None, spaces=()):
        self.home, self.blocked, self.add_error, self.system = home, set(blocked), add_error, system
        self.manager, self.spaces, self.busy = manager or Manager(), list(spaces), set(listening)
        self.remote = FakeRemote('local', self.handle)

    def listening(self, port): return port in self.busy or (self.manager.running and port in self.open_ports())

    def open_ports(self):
        return [entry['port'] for entry in device_cua.tunnels(run=self.manager, system=self.system, home=self.home,
                                                               listening=lambda port: True).values()]

    def handle(self, argv, stdin):
        if argv[:2] == ['sh', '-c'] and argv[2] == device_cua.TOKEN_WRAPPER:
            url = argv[argv.index('--embedded') + 1]
            if url in self.blocked: return self.add_error
            self.spaces.append('direct:' + url); return OK
        if has(argv, 'spaces', 'ls'): return Completed(0, json.dumps({'spaces': [{'id': s} for s in self.spaces]}).encode(), b'')
        if has(argv, 'spaces', 'rm'): self.spaces = [s for s in self.spaces if s not in argv]; return OK
        if 'cua-tunnel' in argv:
            verb = argv[argv.index('cua-tunnel') + 1:]
            if verb[0] == 'add':
                value = device_cua.tunnel_add(*verb[1:], run=self.manager, system=self.system, home=self.home,
                                              listening=self.listening, sleep=lambda s: None)
                return Completed(0, json.dumps(value).encode(), b'')
            if verb[0] == 'remove':
                device_cua.tunnel_remove(verb[1], run=self.manager, system=self.system, home=self.home)
                return OK
        return Completed(127, b'', b'')

    def adds(self): return [(a, s) for a, s in self.remote.calls if a[:3] == ['sh', '-c', device_cua.TOKEN_WRAPPER]]


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def agent(self, name='vps'): return self.home / 'Library/LaunchAgents' / ('io.github.claude-pool.cua-tunnel.' + name + '.plist')

    def unit(self, name='vps'): return self.home / '.config/systemd/user' / ('claude-pool-cua-tunnel-' + name + '.service')

    def up(self, manager):
        """Our ssh listens once its unit is loaded and runs."""
        return lambda port: manager.running and bool(manager.loaded & {'io.github.claude-pool.cua-tunnel.vps', 'claude-pool-cua-tunnel-vps.service'})

    def register(self, device, port=None):
        tunnel = device_cua.Tunnel(device.remote, 'vps', PEER, SSH, port)
        return device_cua.register(device.remote, TOKEN, PEER, 'vps', tunnel=tunnel), tunnel

    def test_a_connect_failure_registers_the_peer_through_an_ssh_tunnel_on_loopback(self):
        device = TunnelDevice(self.home, blocked=[PEER + ':3211'])
        step, tunnel = self.register(device)
        self.assertEqual(step.status, 'ok', step); self.assertIn('SSH tunnel', step.detail)
        port = tunnel.port
        self.assertIn(port, device_cua.TUNNEL_PORTS)
        verbs = [a for a, _ in device.remote.calls if 'cua-tunnel' in a]
        self.assertEqual(verbs[0][-5:], ['cua-tunnel', 'add', 'vps', PEER, SSH])
        adds = device.adds()
        self.assertEqual([a[a.index('--embedded') + 1:] for a, _ in adds],
                         [[PEER + ':3211', '--name', 'vps'], [f'127.0.0.1:{port}', '--name', 'vps']])
        self.assertTrue(all(s == TOKEN + b'\n' for _, s in adds))
        SecretWatch(fixture_token()).argv(device.remote.calls)
        self.assertIn(f'direct:127.0.0.1:{port}', device.spaces)
        import plistlib
        agent = plistlib.loads(self.agent().read_bytes())
        self.assertEqual(agent['ProgramArguments'], exact_argv(port))
        self.assertEqual(agent['Label'], 'io.github.claude-pool.cua-tunnel.vps')
        self.assertIs(agent['KeepAlive'], True); self.assertIs(agent['RunAtLoad'], True)
        self.assertNotIn('0.0.0.0', self.agent().read_text())
        self.assertEqual(device.manager.managed('launchctl', 'bootstrap'),
                         [['launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(self.agent())]])
        self.assertEqual(self.agent().stat().st_mode & 0o777, 0o644)

    def test_an_auth_failure_creates_no_tunnel(self):
        device = TunnelDevice(self.home, blocked=[PEER + ':3211'], add_error=AUTH_ERROR)
        step, tunnel = self.register(device)
        self.assertEqual(step.status, 'failed'); self.assertIsNone(tunnel.port)
        self.assertEqual([a for a, _ in device.remote.calls if 'cua-tunnel' in a], [])
        self.assertFalse(self.agent().exists()); self.assertEqual(device.manager.calls, [])

    def test_unreachable_tells_connect_failures_from_auth_failures(self):
        for text in (b'tcp connect error (deadline has elapsed)', b'connect: Connection refused', b'Connection timed out',
                     b'No route to host'):
            self.assertTrue(device_cua.unreachable(Completed(5, b'', text)), text)
        for done in (AUTH_ERROR, Completed(5, b'', b'403 Forbidden after tcp connect error'), Completed(0, b'', b''),
                     Completed(1, b'', b'something else')):
            self.assertFalse(device_cua.unreachable(done), done)

    def test_a_recorded_tunnel_is_reused_and_nothing_is_registered_twice(self):
        device = TunnelDevice(self.home, blocked=[PEER + ':3211'])
        _, first = self.register(device)
        device.remote.calls.clear(); device.manager.calls.clear()
        step, again = self.register(device, first.port)
        self.assertEqual(step.status, 'ok', step); self.assertEqual(again.port, first.port)
        self.assertEqual(device.adds(), [])
        self.assertEqual(device.manager.managed('bootstrap'), []); self.assertEqual(device.manager.managed('bootout'), [])

    def test_a_tunnel_that_does_not_come_up_fails_with_the_ssh_fix(self):
        device = TunnelDevice(self.home, blocked=[PEER + ':3211'], manager=Manager(running=False))
        step, tunnel = self.register(device)
        self.assertEqual(step.status, 'failed'); self.assertIn('SSH tunnel', step.detail)
        self.assertIn('ssh ' + SSH + ' true', step.fix)
        self.assertEqual(len(device.adds()), 1)
        # a fresh tunnel that never came up is taken away again: nothing relaunches ssh every 10 s, no port to record
        self.assertFalse(self.agent().exists()); self.assertEqual(device.manager.loaded, set()); self.assertIsNone(tunnel.port)
        self.assertEqual(len(device.manager.managed('bootout')), 1)

    def test_a_fresh_tunnel_that_fails_to_load_is_removed_too(self):
        manager = Manager(fail=['bootstrap'])
        with self.assertRaises(RuntimeError):
            device_cua.tunnel_add('vps', PEER, SSH, run=manager, system='darwin', home=self.home, listening=self.up(manager),
                                  sleep=lambda s: None)
        self.assertFalse(self.agent().exists())
        manager = Manager(running=False)
        value = device_cua.tunnel_add('vps', PEER, SSH, run=manager, system='linux', home=self.home, listening=self.up(manager),
                                      sleep=lambda s: None)
        self.assertEqual(value, {'port': None, 'running': False}); self.assertFalse(self.unit().exists())
        self.assertEqual(manager.managed('disable', '--now'), [['systemctl', '--user', 'disable', '--now', 'claude-pool-cua-tunnel-vps.service']])

    def test_an_existing_tunnel_that_does_not_run_now_is_left_in_place(self):
        manager = Manager()
        first = device_cua.tunnel_add('vps', PEER, SSH, run=manager, system='darwin', home=self.home, listening=self.up(manager),
                                      sleep=lambda s: None)
        manager.running = False; manager.calls.clear()
        value = device_cua.tunnel_add('vps', PEER, SSH, run=manager, system='darwin', home=self.home, listening=self.up(manager),
                                      sleep=lambda s: None)
        self.assertEqual(value, {'port': first['port'], 'running': False})
        self.assertTrue(self.agent().exists()); self.assertEqual(manager.managed('bootout'), [])

    def test_a_reload_waits_for_launchd_to_let_go_of_the_label(self):
        manager = Manager(lingers=3)
        first = device_cua.tunnel_add('vps', PEER, SSH, run=manager, system='darwin', home=self.home, listening=self.up(manager),
                                      sleep=lambda s: None)
        manager.calls.clear()
        value = device_cua.tunnel_add('vps', '100.64.0.7', SSH, run=manager, system='darwin', home=self.home,
                                      listening=self.up(manager), sleep=lambda s: None)
        self.assertEqual(value, {'port': first['port'], 'running': True})
        verbs = [a[1] for a in manager.calls if a[0] == 'launchctl']
        out = verbs.index('bootout')
        self.assertGreaterEqual(verbs[out + 1:verbs.index('bootstrap')].count('print'), 4)
        self.assertEqual(verbs.count('bootstrap'), 1)

    def test_a_failed_bootstrap_after_a_reload_is_retried_once(self):
        manager = Manager(lingers=30)  # launchd keeps the label past the 5 s wait, then lets go
        first = device_cua.tunnel_add('vps', PEER, SSH, run=manager, system='darwin', home=self.home, listening=self.up(manager),
                                      sleep=lambda s: None)
        def release(seconds):
            if any(a[:2] == ['launchctl', 'bootstrap'] for a in manager.calls): manager.leaving.clear()
        manager.calls.clear()
        value = device_cua.tunnel_add('vps', '100.64.0.7', SSH, run=manager, system='darwin', home=self.home,
                                      listening=self.up(manager), sleep=release)
        self.assertEqual(len(manager.managed('bootstrap')), 2)
        self.assertEqual(value['port'], first['port'])

    def test_the_port_is_deterministic_per_name_and_skips_listening_ports(self):
        first = device_cua.tunnel_port('vps', listening=lambda port: False)
        self.assertEqual(first, device_cua.tunnel_port('vps', listening=lambda port: False))
        self.assertIn(first, device_cua.TUNNEL_PORTS)
        self.assertNotEqual(first, device_cua.tunnel_port('lap', listening=lambda port: False))
        skipped = device_cua.tunnel_port('vps', listening=lambda port: port == first)
        self.assertIn(skipped, device_cua.TUNNEL_PORTS); self.assertNotEqual(skipped, first)
        self.assertNotEqual(device_cua.tunnel_port('vps', listening=lambda port: False, taken={first}), first)
        with self.assertRaises(RuntimeError): device_cua.tunnel_port('vps', listening=lambda port: True)

    def test_linux_writes_a_systemd_user_unit_that_restarts_always(self):
        manager = Manager()
        value = device_cua.tunnel_add('vps', PEER, SSH, run=manager, system='linux', home=self.home,
                                      listening=lambda port: manager.running and 'claude-pool-cua-tunnel-vps.service' in manager.loaded,
                                      sleep=lambda s: None)
        self.assertTrue(value['running']); port = value['port']
        text = self.unit().read_text()
        import service
        exec_line = next(line for line in text.splitlines() if line.startswith('ExecStart='))
        self.assertEqual(service.words(exec_line[len('ExecStart='):], exec=True), exact_argv(port))
        self.assertIn('\nRestart=always\n', text); self.assertIn('\nWantedBy=default.target\n', text)
        self.assertNotIn('0.0.0.0', text)
        self.assertEqual(manager.managed('enable', '--now'), [['systemctl', '--user', 'enable', '--now', 'claude-pool-cua-tunnel-vps.service']])
        self.assertTrue(manager.managed('daemon-reload'))
        self.assertFalse(self.agent().exists())
        self.assertEqual(device_cua.tunnels(run=manager, system='linux', home=self.home, listening=lambda port: True),
                         {'vps': {'port': port, 'running': True}})

    def test_remove_deletes_only_our_unit_and_is_idempotent(self):
        manager = Manager()
        value = device_cua.tunnel_add('vps', PEER, SSH, run=manager, system='darwin', home=self.home, listening=self.up(manager),
                                      sleep=lambda s: None)
        agents = self.agent().parent
        hand_made = agents / 'de.cua.personal-vps-tunnel.plist'; hand_made.write_bytes(b'theirs')
        pool = agents / 'io.github.claude-pool.plist'; pool.write_bytes(b'pool')
        manager.loaded |= {'de.cua.personal-vps-tunnel', 'io.github.claude-pool'}
        self.assertEqual(device_cua.tunnels(run=manager, system='darwin', home=self.home, listening=lambda port: True),
                         {'vps': {'port': value['port'], 'running': True}})
        for _ in range(2): self.assertTrue(device_cua.tunnel_remove('vps', run=manager, system='darwin', home=self.home))
        self.assertFalse(self.agent().exists())
        self.assertEqual(hand_made.read_bytes(), b'theirs'); self.assertEqual(pool.read_bytes(), b'pool')
        self.assertEqual({'de.cua.personal-vps-tunnel', 'io.github.claude-pool'}, manager.loaded)
        self.assertTrue(all(a[-1].endswith('io.github.claude-pool.cua-tunnel.vps') for a in manager.managed('bootout')))
        for bad in ('personal-vps-tunnel.x', '../vps', '', 'VPS'):
            with self.assertRaises(ValueError): device_cua.tunnel_remove(bad, run=manager, system='darwin', home=self.home)

    def test_a_stopped_tunnel_reports_not_running(self):
        manager = Manager()
        device_cua.tunnel_add('vps', PEER, SSH, run=manager, system='darwin', home=self.home, listening=self.up(manager),
                              sleep=lambda s: None)
        manager.running = False
        self.assertEqual(device_cua.tunnels(run=manager, system='darwin', home=self.home, listening=lambda port: True)['vps']['running'], False)
        manager.loaded.clear()
        self.assertEqual(device_cua.tunnels(run=manager, system='darwin', home=self.home, listening=lambda port: True)['vps']['running'], False)

    def test_a_changed_address_rewrites_the_unit_on_the_same_port(self):
        manager = Manager()
        first = device_cua.tunnel_add('vps', PEER, SSH, run=manager, system='darwin', home=self.home, listening=self.up(manager),
                                      sleep=lambda s: None)
        second = device_cua.tunnel_add('vps', '100.64.0.7', SSH, run=manager, system='darwin', home=self.home,
                                       listening=self.up(manager), sleep=lambda s: None)
        self.assertEqual(first['port'], second['port'])
        import plistlib
        self.assertEqual(plistlib.loads(self.agent().read_bytes())['ProgramArguments'], exact_argv(first['port'], ip='100.64.0.7'))
        self.assertEqual(len(manager.managed('bootout')), 1); self.assertEqual(len(manager.managed('bootstrap')), 2)

    def test_tunnel_inputs_are_checked(self):
        manager = Manager()
        for args in [('vps', '192.168.1.5', SSH), ('vps', PEER, '-oProxyCommand=x'), ('../vps', PEER, SSH), ('vps', PEER, 'a b')]:
            with self.assertRaises(ValueError): device_cua.tunnel_add(*args, run=manager, system='darwin', home=self.home,
                                                                      listening=lambda port: False, sleep=lambda s: None)
        self.assertEqual(manager.calls, []); self.assertFalse((self.home / 'Library').exists())

    def test_forget_tunnel_forgets_the_space_and_removes_the_unit(self):
        device = TunnelDevice(self.home, blocked=[PEER + ':3211'])
        _, tunnel = self.register(device)
        device.spaces.append('direct:' + PEER + ':3211-other')
        for _ in range(2):
            steps = device_cua.forget_tunnel(device.remote, 'vps', tunnel.port)
            self.assertTrue(all(s.status == 'ok' for s in steps), steps)
        self.assertNotIn(f'direct:127.0.0.1:{tunnel.port}', device.spaces)
        self.assertFalse(self.agent().exists())
        self.assertEqual(len([a for a, _ in device.remote.calls if has(a, 'spaces', 'rm')]), 1)


if __name__ == '__main__': unittest.main()
