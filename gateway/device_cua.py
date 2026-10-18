"""Cua between machines: one machine shares its desktop on its Tailscale address, the others register it as a Space.

Never downloads or installs Cua. The host token (the pairing secret) travels on stdin only into the cua process
environment and is never printed, logged or put in argv. `host setup` output can contain it, so it is discarded.

When a firewall drops port 3211 on the way to a peer (only ssh gets through), the registering device reaches the peer's host
through an SSH tunnel that claude-pool owns there: a LaunchAgent or systemd user unit bound to 127.0.0.1 only, named by our
label prefix, so a tunnel claude-pool did not create is never touched. No root is needed on either machine."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import stat
import sys
import time

import device_install
import device_sync
from device_report import Step
import registry
from private_files import private_directory
from remote import _TARGET, Local, require_pipe
import service

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


# The Mac App Store and standalone Tailscale apps put no `tailscale` on PATH; the CLI ships inside the app.
MAC_TAILSCALE = '/Applications/Tailscale.app/Contents/MacOS/Tailscale'


def tailscale_ip(remote):
    done = _run(remote, ['tailscale', 'ip', '-4'])
    if done.returncode == 127: done = _run(remote, [MAC_TAILSCALE, 'ip', '-4'])  # not on PATH: the macOS app's CLI
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


def _add_space(peer, token, url, name):
    filled = [word.format(url=url, name=name) for word in SPACES_ADD_TEMPLATE]
    return peer.call(['sh', '-c', TOKEN_WRAPPER, 'sh', 'cua', *filled], stdin=token + b'\n')


# What `cua spaces add` prints when the address cannot be reached, and when the host answered but refused the token.
CONNECT_ERRORS = ('tcp connect error', 'deadline has elapsed', 'connection refused', 'timed out', 'no route to host',
                  'network is unreachable', 'host is unreachable', 'connect timeout')
AUTH_ERRORS = ('401', '403', 'unauthori', 'forbidden', 'token', 'authenticat', 'permission denied')


def unreachable(done):
    """True when a failed `cua spaces add` could not connect at all; an answer that refused the token is never unreachable.
    The output is only matched here, never printed."""
    if done.returncode == 0: return False
    text = ((done.stdout or b'') + b'\n' + (done.stderr or b'')).decode('utf-8', 'replace').lower()
    if any(word in text for word in AUTH_ERRORS): return False
    return any(word in text for word in CONNECT_ERRORS)


def register(peer, host_token, host_ip, name, tunnel=None):
    """Register the host at host_ip as Space `name` on `peer`. With a Tunnel (a tunnel this device may own to that host),
    a connect failure, or a tunnel recorded earlier, registers it at the tunnel's loopback port instead."""
    host_ip = _checked(host_ip)
    token = bytes(host_token).strip()
    if not token: return Step('cua', 'failed', 'no Cua token to register with', DOCS_FIX)
    ids = _space_ids(peer) or []
    if f'direct:{host_ip}:{PORT}' in ids: return Step('cua', 'ok', 'already registered')
    if tunnel is None or tunnel.port is None:
        done = _add_space(peer, token, f'{host_ip}:{PORT}', name)
        if done.returncode == 0: return Step('cua', 'ok', 'registered')
        if not unreachable(done): return Step('cua', 'failed', 'could not register the Space', DOCS_FIX)
        if tunnel is None:
            return Step('cua', 'failed', f'{name} cannot be reached on port {PORT} from {peer.name} (a firewall drops it)', DOCS_FIX)
    if not tunnel.open():
        return Step('cua', 'failed', 'the SSH tunnel to ' + name + ' did not come up',
                    'ssh ' + tunnel.ssh + ' true must work without a password prompt (BatchMode), then claude-pool device add '
                    + tunnel.ssh + ' --cua')
    if tunnel.space in ids: return Step('cua', 'ok', 'already registered through an SSH tunnel')
    done = _add_space(peer, token, f'{LOOPBACK}:{tunnel.port}', name)
    if done.returncode: return Step('cua', 'failed', 'could not register the Space through the SSH tunnel', DOCS_FIX)
    return Step('cua', 'ok', 'registered through an SSH tunnel')


# SSH tunnel to a peer's host ------------------------------------------------------------------------------------------------

LOOPBACK = '127.0.0.1'
TUNNEL_PORTS = range(32300, 32400)
TUNNEL_LABEL = 'io.github.claude-pool.cua-tunnel.'  # launchd label prefix; the systemd user unit is TUNNEL_UNIT<name>.service
TUNNEL_UNIT = 'claude-pool-cua-tunnel-'
TUNNEL_WAIT = 15  # seconds a new tunnel gets to log in and listen
BOOTOUT_WAIT = 5  # seconds launchd gets to let go of a booted-out label before it is loaded again
TUNNEL_BACKOFF = 60  # seconds between ssh attempts: a changed host key or a refused key never loops quickly
LOG_KEEP = 64 * 1024  # bytes of ssh errors kept per tunnel; a longer log starts again when the tunnel is written
_PORT_IN = re.compile(rb'127\.0\.0\.1:([0-9]{4,5}):')


def _peer(name):
    if not isinstance(name, str) or not registry.NAME.fullmatch(name): raise ValueError('Invalid device name')
    return name


def _ssh(target):
    if not isinstance(target, str) or not _TARGET.fullmatch(target) or target.startswith('-'): raise ValueError('Invalid ssh target')
    return target


def tunnel_label(peer, system): return (TUNNEL_LABEL if system == 'darwin' else TUNNEL_UNIT) + _peer(peer)


def tunnel_file(peer, system, home):
    if system == 'darwin': return Path(home) / 'Library/LaunchAgents' / (tunnel_label(peer, system) + '.plist')
    return Path(home) / '.config/systemd/user' / (tunnel_label(peer, system) + '.service')


def tunnel_argv(port, peer_ip, ssh):
    """The ssh that forwards 127.0.0.1:<port> on this device to the peer's host. Never a 0.0.0.0 bind, no agent, no master."""
    if port not in TUNNEL_PORTS: raise ValueError('Tunnel port out of range')
    return ['/usr/bin/ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=20',
            '-o', 'ServerAliveCountMax=3', '-o', 'ForwardAgent=no', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
            '-L', f'{LOOPBACK}:{port}:{_checked(peer_ip)}:{PORT}', _ssh(ssh)]


def tunnel_log(peer, home):
    """The 0600 file the tunnel's ssh writes its errors to, in this device's runtime folder (0700)."""
    if Path(home) == Path.home():
        import settings
        root = settings.RUNTIME
    else: root = Path(home) / '.local/state/claude-pool'  # a home given by a test: everything stays inside it
    return root / ('cua-tunnel-' + _peer(peer) + '.log')


def _prepare_log(path):
    """Create the log owner-only before the unit runs (launchd and systemd would create it with the umask's mode)."""
    private_directory(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        if os.fstat(fd).st_size > LOG_KEEP: os.ftruncate(fd, 0)
    finally: os.close(fd)


def _last_error(path):
    """The last non-empty line of a tunnel's log, printable and short, or None."""
    try:
        with open(path, 'rb') as file:
            file.seek(max(0, os.fstat(file.fileno()).st_size - 4096)); data = file.read()
    except OSError: return None
    lines = [line for line in data.decode('utf-8', 'replace').splitlines() if line.strip()]
    if not lines: return None
    text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', lines[-1])
    return ''.join(char for char in text if char.isprintable()).strip()[:200] or None


def tunnel_unit(peer, port, peer_ip, ssh, system, log=None):
    """ssh's errors go to `log` (else nowhere); a failing ssh is started again only every TUNNEL_BACKOFF seconds."""
    argv, label = tunnel_argv(port, peer_ip, ssh), tunnel_label(peer, system)
    if system == 'darwin':
        return plistlib.dumps({'Label': label, 'ProgramArguments': argv, 'KeepAlive': True, 'RunAtLoad': True,
                               'ThrottleInterval': TUNNEL_BACKOFF, 'ProcessType': 'Background', 'Umask': 0o077,
                               'StandardOutPath': '/dev/null', 'StandardErrorPath': str(log) if log else '/dev/null'})
    target = str(log) if log and not re.search(r'\s', str(log)) else None  # append: takes a bare path
    lines = ['[Unit]', 'Description=claude-pool Cua tunnel to ' + peer, '', '[Service]', 'Type=simple',
             'ExecStart=' + ' '.join(service.exec_quote(word) for word in argv), 'Restart=always', 'RestartSec=' + str(TUNNEL_BACKOFF),
             'UMask=0077', 'StandardOutput=null', 'StandardError=' + ('append:' + target.replace('%', '%%') if target else 'null'),
             '', '[Install]', 'WantedBy=default.target']
    return ('\n'.join(lines) + '\n').encode()


def _listening(port):
    try:
        with socket.create_connection((LOOPBACK, port), timeout=0.5): return True
    except OSError: return False


def tunnel_port(peer, listening=None, taken=()):
    """The first free port from a start fixed by the device name: the same name gets the same port while it is free."""
    listening = listening or _listening
    start = int(hashlib.sha256(_peer(peer).encode()).hexdigest(), 16) % len(TUNNEL_PORTS)
    for step in range(len(TUNNEL_PORTS)):
        port = TUNNEL_PORTS[(start + step) % len(TUNNEL_PORTS)]
        if port not in taken and not listening(port): return port
    raise RuntimeError(f'No free port for a Cua tunnel between {TUNNEL_PORTS.start} and {TUNNEL_PORTS.stop - 1}')


def _recorded_port(path):
    try: found = _PORT_IN.search(path.read_bytes())
    except OSError: return None
    port = int(found.group(1)) if found else None
    return port if port in TUNNEL_PORTS else None


def _ours(system, home):
    """Our tunnel files by peer name: only files with our prefix and a valid device name; nobody else's units."""
    folder = tunnel_file('x', system, home).parent
    prefix, suffix = (TUNNEL_LABEL, '.plist') if system == 'darwin' else (TUNNEL_UNIT, '.service')
    try: names = sorted(os.listdir(folder))
    except OSError: return {}
    found = {}
    for entry in names:
        peer = entry[len(prefix):-len(suffix)] if entry.startswith(prefix) and entry.endswith(suffix) else ''
        if registry.NAME.fullmatch(peer): found[peer] = folder / entry
    return found


def _gui(): return 'gui/' + str(os.getuid())


def _loaded(run, label, system):
    if system == 'darwin': return run.call(['launchctl', 'print', _gui() + '/' + label]).returncode == 0
    return run.call(['systemctl', '--user', 'is-active', '--quiet', label + '.service']).returncode == 0


def _unit_runs(run, label, system):
    if system == 'darwin':
        done = run.call(['launchctl', 'print', _gui() + '/' + label])
        return done.returncode == 0 and b'state = running' in (done.stdout or b'')
    return run.call(['systemctl', '--user', 'is-active', '--quiet', label + '.service']).returncode == 0


def _gone(run, label, sleep, wait=BOOTOUT_WAIT):
    """launchctl bootout returns before launchd lets go of the label; a bootstrap until then fails (Input/output error)."""
    for _ in range(int(wait * 2)):
        if not _loaded(run, label, 'darwin'): return True
        sleep(0.5)
    return not _loaded(run, label, 'darwin')


def _bootstrap(run, path, label, sleep):
    """Load the agent; one retry after waiting for launchd again."""
    for attempt in range(2):
        if run.call(['launchctl', 'bootstrap', _gui(), str(path)]).returncode == 0: return True
        if attempt == 0: _gone(run, label, sleep)
    return False


def _defaults(run, system, home, listening=None):
    return (run or Local(), system or service.platform(), Path(home) if home else Path.home(), listening or _listening)


def tunnel_add(peer, peer_ip, ssh, *, run=None, system=None, home=None, listening=None, sleep=time.sleep, wait=TUNNEL_WAIT):
    """This device's tunnel to `peer`'s host: written, loaded and given `wait` seconds to listen. Idempotent; keeps its port.
    A tunnel this call created that fails to load or never comes up is removed again ({'port': None, 'running': False}), so
    no ssh relaunches every 10 s; a tunnel from an earlier run that worked stays in place."""
    peer, peer_ip, ssh = _peer(peer), _checked(peer_ip), _ssh(ssh)
    run, system, home, listening = _defaults(run, system, home, listening)
    fresh = peer not in _ours(system, home)
    try: value = _tunnel_up(peer, peer_ip, ssh, run, system, home, listening, sleep, wait)
    except BaseException:
        if fresh:
            try: tunnel_remove(peer, run=run, system=system, home=home)
            except Exception: pass
        raise
    if fresh and not value['running']:
        tunnel_remove(peer, run=run, system=system, home=home)
        return {'port': None, 'running': False}
    return value


def _tunnel_up(peer, peer_ip, ssh, run, system, home, listening, sleep, wait):
    label, path = tunnel_label(peer, system), tunnel_file(peer, system, home)
    ours = _ours(system, home)
    taken = {port for name, other in ours.items() if name != peer for port in [_recorded_port(other)] if port}
    port = _recorded_port(path) if peer in ours else None
    if port is not None and (port in taken or (listening(port) and not _unit_runs(run, label, system))): port = None
    if port is None: port = tunnel_port(peer, listening, taken)
    log = tunnel_log(peer, home)
    _prepare_log(log)
    data = tunnel_unit(peer, port, peer_ip, ssh, system, log)
    try: changed = path.read_bytes() != data
    except OSError: changed = True
    if system == 'darwin':
        target = _gui() + '/' + label
        loaded = _loaded(run, label, system)
        if changed and loaded:
            run.call(['launchctl', 'bootout', target]); _gone(run, label, sleep); loaded = False
        if changed: path.parent.mkdir(parents=True, exist_ok=True); service.write(path, data)
        if not loaded and not _bootstrap(run, path, label, sleep):
            raise RuntimeError('launchd did not load the Cua tunnel to ' + peer)
    else:
        unit = label + '.service'
        active = _loaded(run, label, system)
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True); service.write(path, data)
            run.call(['systemctl', '--user', 'daemon-reload'])
        if run.call(['systemctl', '--user', 'enable', '--now', unit]).returncode:
            raise RuntimeError('systemd did not start the Cua tunnel to ' + peer)
        if changed and active: run.call(['systemctl', '--user', 'restart', unit])
    for _ in range(int(wait * 2) + 1):
        if _unit_runs(run, label, system) and listening(port): return {'port': port, 'running': True}
        sleep(0.5)
    return {'port': port, 'running': False}


def tunnel_remove(peer, *, run=None, system=None, home=None):
    """Stop and delete this device's tunnel to `peer`; only our own unit, and ok when it is already gone."""
    peer = _peer(peer)
    run, system, home, _ = _defaults(run, system, home)
    label, path = tunnel_label(peer, system), tunnel_file(peer, system, home)
    existed = path.exists()
    if system == 'darwin':
        if _loaded(run, label, system): run.call(['launchctl', 'bootout', _gui() + '/' + label])
    elif existed or _loaded(run, label, system): run.call(['systemctl', '--user', 'disable', '--now', label + '.service'])
    path.unlink(missing_ok=True)
    tunnel_log(peer, home).unlink(missing_ok=True)
    if existed and system != 'darwin': run.call(['systemctl', '--user', 'daemon-reload'])
    if _loaded(run, label, system): raise RuntimeError('the Cua tunnel to ' + peer + ' is still loaded')
    return True


def tunnels(*, run=None, system=None, home=None, listening=None):
    """{peer: {'port', 'running'}} for every tunnel claude-pool made on this device; running = the unit runs and listens.
    A tunnel that does not run also carries 'error': the last line its ssh wrote, when there is one."""
    run, system, home, listening = _defaults(run, system, home, listening)
    found = {}
    for peer, path in _ours(system, home).items():
        port = _recorded_port(path)
        found[peer] = {'port': port, 'running': bool(port) and _unit_runs(run, tunnel_label(peer, system), system) and listening(port)}
        error = None if found[peer]['running'] else _last_error(tunnel_log(peer, home))
        if error: found[peer]['error'] = error
    return found


class Tunnel:
    """The tunnel the registering device may own to a peer's host. `device cua-tunnel` writes the unit there, so it works the
    same for this machine and for one reached over ssh."""
    def __init__(self, device, peer, peer_ip, ssh, port=None):
        self.device, self.peer, self.peer_ip, self.ssh, self.port = device, _peer(peer), _checked(peer_ip), _ssh(ssh), port

    @property
    def space(self): return f'direct:{LOOPBACK}:{self.port}'

    def open(self):
        """True when the tunnel runs; sets `port` to the one the device chose."""
        value = _json(self.device.device('cua-tunnel', 'add', self.peer, self.peer_ip, self.ssh, timeout=120))
        port = value.get('port') if value else None
        if type(port) is not int or port not in TUNNEL_PORTS: return False
        self.port = port
        return value.get('running') is True


def forget_tunnel(device, peer, port):
    """Forget the tunnel Space for `peer` on `device` and delete the tunnel unit; ok when either is already gone."""
    steps, space = [], f'direct:{LOOPBACK}:{port}'
    ids = _space_ids(device)
    if ids is not None and space not in ids: steps.append(Step('cua', 'ok', 'tunnel Space already forgotten'))
    else: steps.append(_step(_cua(device, 'spaces', 'rm', space), 'tunnel Space forgotten', 'could not forget the tunnel Space'))
    steps.append(_step(device.device('cua-tunnel', 'remove', peer), 'SSH tunnel to ' + peer + ' removed',
                       'could not remove the SSH tunnel to ' + peer))
    return steps


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
        try: found = tunnels(run=local)
        except Exception: found = {}
        return {'cua': {'host': _running(_host_status(local)), 'spaces': _space_ids(local) or [],
                        'mcp': bool(native) and local.call([native, 'mcp', 'get', 'cua']).returncode == 0, 'tunnels': found}}
    except Exception: return {'cua': None}


device_sync.STATUS_PARTS.append(_status)
