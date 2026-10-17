"""Cua between machines: one machine shares its desktop on its Tailscale address, the others register it as a Space.

Never downloads or installs Cua. The host token (the pairing secret) travels on stdin only into the cua process
environment and is never printed, logged or put in argv. `host setup` output can contain it, so it is discarded."""
import ipaddress
import json
import os
from pathlib import Path
import stat
import sys

import device_install
import device_sync
from device_report import Step
from remote import Local, require_pipe

PORT = 3211
DOCS_FIX = 'see docs/DEVICES.md, Cua'
ACCESSIBILITY_FIX = ('System Settings, Privacy & Security, Accessibility: switch on cua-spacesd, '
                     'then run claude-pool device add --cua again')
SPACES_ADD_TEMPLATE = ['spaces', 'add', '--embedded', '{url}', '--name', '{name}']
PATH_PREFIX = 'PATH=/opt/homebrew/bin:/usr/local/bin:"$HOME/.local/bin":$PATH; export PATH; '
TOKEN_WRAPPER = PATH_PREFIX + 'IFS= read -r CUA_ENV_TOKEN && export CUA_ENV_TOKEN && exec "$@"'
TAILSCALE_RANGE = ipaddress.ip_network('100.64.0.0/10')


def _checked(ip):
    try: address = ipaddress.IPv4Address(ip)
    except ValueError: raise ValueError('Not a Tailscale address') from None
    if address not in TAILSCALE_RANGE: raise ValueError('Not a Tailscale address')
    return str(address)


def host_setup_argv(ip, name):
    ip = _checked(ip)
    return ['cua', 'host', 'setup', '--embedded', '--direct', f'{ip}:{PORT}', '--name', name]


def _run(remote, argv, **options):
    """Run argv on the target with the usual tool directories on PATH."""
    return remote.call(['sh', '-c', PATH_PREFIX + 'exec "$@"', 'sh', *argv], **options)


def _cua(remote, *args, **options): return _run(remote, ['cua', *args], **options)


def _json(done):
    if done.returncode: return None
    try: value = json.loads((done.stdout or b'').decode('utf-8', 'replace'))
    except ValueError: return None
    return value if isinstance(value, dict) else None


def tailscale_ip(remote):
    done = _run(remote, ['tailscale', 'ip', '-4'])
    if done.returncode: return None
    for line in (done.stdout or b'').decode('utf-8', 'replace').splitlines():
        try: address = ipaddress.IPv4Address(line.strip())
        except ValueError: continue
        return str(address) if address in TAILSCALE_RANGE else None
    return None


def _has_cua(remote, **options):
    return remote.call(['sh', '-c', PATH_PREFIX + 'command -v cua'], **options).returncode == 0


def _host_status(remote, **options):
    return _json(_cua(remote, 'host', 'status', '--embedded', '--json', **options))


def _needs_accessibility(status):
    permissions = status.get('permissions') if status else None
    return isinstance(permissions, list) and any(isinstance(p, dict) and p.get('id') == 'accessibility' for p in permissions)


def _running(status):
    service = status.get('service') if status else None
    return bool(status and status.get('configured') is True and isinstance(service, dict) and service.get('running') is True)


def _at(status, ip):
    url = status.get('directUrl') if status else None
    return url in (f'{ip}:{PORT}', f'http://{ip}:{PORT}', f'https://{ip}:{PORT}')


def ensure_host(remote, name, ip):
    ip = _checked(ip)
    if not _has_cua(remote): return Step('cua', 'missing', 'Cua is not installed', DOCS_FIX)
    status = _host_status(remote)
    if not (_running(status) and _at(status, ip)):
        # The output can contain the pairing token: the remote captures it and it is never read.
        remote.call(['sh', '-c', PATH_PREFIX + 'exec "$@"', 'sh', *host_setup_argv(ip, name)], timeout=600)
        status = _host_status(remote)
    if _needs_accessibility(status): return Step('cua', 'manual', 'macOS Accessibility is not granted yet', ACCESSIBILITY_FIX)
    if _running(status) and _at(status, ip): return Step('cua', 'ok', 'host shares this desktop')
    return Step('cua', 'failed', 'Cua host did not start', DOCS_FIX)


def _token_path():
    status = _host_status(Local())
    given = status.get('envTokenPath') if status else None
    return Path(given) if isinstance(given, str) and given else Path.home() / '.cua/host/env-token'


def send_token(out=None, path=None):
    """Target-side `device cua-token`: write the host token to a pipe, never to a terminal."""
    out = out or sys.stdout.buffer
    require_pipe(out)
    path = Path(path) if path else _token_path()
    fixed = 'The Cua token must be your private 0600 file'
    try: fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError: raise RuntimeError(fixed) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077: raise RuntimeError(fixed)
        data = os.read(fd, 4096).strip()
    finally: os.close(fd)
    out.write(data); out.flush()


def host_token(remote):
    done = remote.device('cua-token')
    token = (done.stdout or b'').strip()
    if done.returncode or not token: raise RuntimeError('The other device did not send its Cua token')
    return token


def _space_ids(remote, **options):
    value = _json(_cua(remote, 'spaces', 'ls', '--json', **options))
    spaces = value.get('spaces') if value else None
    return [s['id'] for s in spaces if isinstance(s, dict) and isinstance(s.get('id'), str)] if isinstance(spaces, list) else None


def register(peer, host_token, host_ip, name):
    host_ip = _checked(host_ip)
    token = bytes(host_token).strip()
    if not token: return Step('cua', 'failed', 'no Cua token to register with', DOCS_FIX)
    if f'direct:{host_ip}:{PORT}' in (_space_ids(peer) or []): return Step('cua', 'ok', 'already registered')
    filled = [word.format(url=f'{host_ip}:{PORT}', name=name) for word in SPACES_ADD_TEMPLATE]
    done = peer.call(['sh', '-c', TOKEN_WRAPPER, 'sh', 'cua', *filled], stdin=token + b'\n')
    if done.returncode: return Step('cua', 'failed', 'could not register the Space', DOCS_FIX)
    return Step('cua', 'ok', 'registered')


def ensure_mcp(remote, native):
    if remote.call([native, 'mcp', 'get', 'cua']).returncode == 0: return Step('cua', 'ok', 'MCP server present')
    found = remote.call(['sh', '-c', PATH_PREFIX + 'command -v cua'])
    path = (found.stdout or b'').decode('utf-8', 'replace').strip().splitlines()
    if found.returncode or not path: return Step('cua', 'missing', 'Cua is not installed', DOCS_FIX)
    done = remote.call([native, 'mcp', 'add', '-s', 'user', 'cua', '--', path[0], 'mcp', '--embedded'])
    if done.returncode: return Step('cua', 'failed', 'could not add the MCP server', DOCS_FIX)
    return Step('cua', 'ok', 'MCP server added')


def _step(done, ok, bad): return Step('cua', 'ok', ok) if done.returncode == 0 else Step('cua', 'failed', bad)


def remove(device, peers, native=None):
    """Every part is checked first and counts as ok when already gone, so a second run changes nothing."""
    steps = []
    ip = tailscale_ip(device)
    for peer in peers:
        if ip is None: steps.append(Step('cua', 'skipped', 'no Tailscale address to forget')); break
        ids = _space_ids(peer)
        if ids is not None and f'direct:{ip}:{PORT}' not in ids: steps.append(Step('cua', 'ok', 'Space already forgotten')); continue
        steps.append(_step(_cua(peer, 'spaces', 'rm', f'direct:{ip}:{PORT}'), 'Space forgotten', 'could not forget the Space'))
    if not _has_cua(device) or not (_host_status(device) or {}).get('configured'): steps.append(Step('cua', 'ok', 'no host to remove'))
    else: steps.append(_step(_cua(device, 'host', 'remove', '--embedded'), 'host removed', 'could not remove the host'))
    native = native or device_install.check_native(device)[1]
    if native:
        if device.call([native, 'mcp', 'get', 'cua']).returncode: steps.append(Step('cua', 'ok', 'no MCP server to remove'))
        else: steps.append(_step(device.call([native, 'mcp', 'remove', 'cua', '-s', 'user']), 'MCP server removed', 'could not remove the MCP server'))
    return steps


class _Bounded:
    """Caps every call at 10 seconds, so `status` can never hang on a stuck cua."""
    def __init__(self, inner): self._inner, self.name = inner, inner.name

    def call(self, argv, stdin=None, timeout=10): return self._inner.call(argv, stdin=stdin, timeout=min(timeout, 10))


def _status():
    try:
        local = _Bounded(Local())
        if not _has_cua(local): return {'cua': None}
        native = device_install.check_native(local)[1]
        return {'cua': {'host': _running(_host_status(local)), 'spaces': _space_ids(local) or [],
                        'mcp': bool(native) and local.call([native, 'mcp', 'get', 'cua']).returncode == 0}}
    except Exception: return {'cua': None}


device_sync.STATUS_PARTS.append(_status)
