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


def host(configured=True, running=True, permissions=()):
    return json.dumps({'configured': configured, 'service': {'running': running}, 'permissions': [{'id': p} for p in permissions]}).encode()


class Target:
    """A fake machine: cua presence, host state and registered spaces."""
    def __init__(self, cua=True, setup_state=None, state=None, spaces=(), mcp=False, ip=IP):
        self.cua, self.state, self.setup_state, self.spaces, self.mcp, self.ip = cua, state or host(False, False), setup_state, list(spaces), mcp, ip
        self.remote = FakeRemote('t', self.handle)

    def handle(self, argv, stdin):
        if argv[:2] == ['sh', '-c'] and 'command -v cua' in argv[2]: return Completed(0, ABS.encode(), b'') if self.cua else NO
        if has(argv, 'tailscale'): return Completed(0, self.ip.encode() + b'\n', b'')
        if has(argv, 'setup'):
            if self.setup_state is not None: self.state = self.setup_state
            return Completed(0, b'pair with token ' + TOKEN, b'also ' + TOKEN)
        if has(argv, 'host', 'status'): return Completed(0, self.state, b'')
        if has(argv, 'spaces', 'ls'): return Completed(0, json.dumps({'spaces': [{'id': s} for s in self.spaces]}).encode(), b'')
        if has(argv, 'spaces', 'add'): self.spaces.append(f'direct:{IP}:3211'); return OK
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
        self.assertFalse([a for a, _ in target.remote.calls if any('curl' in w or 'install' in w for w in a)])

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
        device, peer = Target(mcp=True), Target()
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


if __name__ == '__main__': unittest.main()
