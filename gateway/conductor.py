"""Conductor Claude: one permanent Native Remote Control conversation per pool, kept alive as a service on one device.

It runs Native Claude with this machine's own Native login, never through the pool (Remote Control does not work through a
custom gateway). Facts from the live Conductor (2026-10-17, controller, read-only):
- server mode `claude remote-control --name "Conductor Claude" --spawn same-dir --permission-mode auto` prints a line with
  `Connected` and `Capacity: n/32`; on stop it keeps the environment so a restart reconnects existing sessions;
- `claude remote-control --session-id cse_<id> --permission-mode auto` reattaches that one conversation and prints
  `Conductor Claude · <dir> · ...` and `Single session · exits when complete`;
- the output names the conversation as `https://claude.ai/code/session_<id>`; its `cse_` id has the same `<id>`;
- an untrusted directory prompts `Trust <dir>? [y/N]`.
The supervisor keeps the child's output only in a bounded in-memory ring and stores only the `cse_` id (never the URL)."""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time

import accounts_cli
import device_sync
import managed_block
from private_files import read_private, write_private
import registry
from remote import Local, Remote
import service
import settings
from device_report import Step

STEP = 'conductor'
NAME = 'Conductor Claude'
ARGV = ['remote-control', '--name', NAME, '--spawn', 'same-dir', '--permission-mode', 'auto']
READY = re.compile(r'Connected|Single session')
SESSION_URL = re.compile(r'claude\.ai/code/session_([A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*)')
SESSION_ID = re.compile(r'cse_[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*')
TRUST = re.compile(r'Trust ([^\r\n]+?)\? \[y/N\]')
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]')
SCRUB = ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK',
         'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', 'DISABLE_GROWTHBOOK']
RING = 64 * 1024
PROMPT_WINDOW = 4096
BACKOFF_START, BACKOFF_MAX, BACKOFF_RESET = 10, 300, 600
REATTACH_GRACE = 30
MOVE_WAIT, MOVE_POLL = 120, 2
RESERVE_DEFAULT = 85
HERDR_FIX = 'stop the Conductor in herdr session conductor first'
START, END = '<!-- claude-pool:devices start -->', '<!-- claude-pool:devices end -->'


LABEL = service.installed_label(service.receipt(settings.HERE.parent)) + '.conductor'
WINDOW = (50, 300)  # rows, columns of the child's terminal: wide enough that no line of interest wraps
REATTACH_TRIES = 3
DELETE = object()


def native_env(environ):
    """Native Claude's own environment: nothing that would point it at the pool or another provider."""
    env = dict(environ)
    for key in SCRUB: env.pop(key, None)
    return env


class Ring:
    """The last 64 KiB of the child's output, in memory only."""
    def __init__(self, size=RING):
        self.size, self.data = size, bytearray()

    def add(self, chunk):
        self.data += chunk
        if len(self.data) > self.size: del self.data[:len(self.data) - self.size]

    def text(self): return ANSI.sub('', bytes(self.data).decode('utf-8', 'replace'))


class PtyChild:
    """Native Claude in its own pty and session."""
    def __init__(self, argv, env, cwd):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack('HHHH', *WINDOW, 0, 0))
                os.chdir(cwd)
                os.execve(argv[0], argv, env)
            finally: os._exit(127)
        self.pid, self.fd = pid, fd

    def read(self, timeout):
        """Bytes, None on timeout, b'' at end of output."""
        try: ready, _, _ = select.select([self.fd], [], [], timeout)
        except OSError: return b''
        if not ready: return None
        try: return os.read(self.fd, 4096)
        except OSError: return b''  # EIO once the child closed the terminal (Linux)

    def write(self, data):
        try: os.write(self.fd, data)
        except OSError: pass

    def terminate(self, grace=10):
        try: os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError: return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if os.waitpid(self.pid, os.WNOHANG)[0]: return
            time.sleep(.2)
        try: os.killpg(self.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError): pass

    def wait(self):
        try: _, status = os.waitpid(self.pid, 0); code = os.waitstatus_to_exitcode(status)
        except ChildProcessError: code = None  # already reaped in terminate()
        try: os.close(self.fd)
        except OSError: pass
        return code


def read_state(path=None):
    """The supervisor state; {} when there is none yet. A file that is not private 0600 JSON raises, so nothing overwrites it."""
    try: value = read_private(path or settings.CONDUCTOR_STATE, 16384)
    except FileNotFoundError: return {}
    for key in ('sessionId', 'previousSessionId'):
        if not SESSION_ID.fullmatch(str(value.get(key, ''))): value.pop(key, None)
    return value


def _update(path, **changes):
    """Merge `changes` (DELETE removes a key) into the state file under its lock; install and serve both write it."""
    path = Path(path)
    with registry.locked(path):
        value = read_state(path)
        for key, item in changes.items():
            if item is DELETE: value.pop(key, None)
            else: value[key] = item
        write_private(path, json.dumps(value, sort_keys=True) + '\n')
    return value


def _command(native, session):
    if session: return [native, 'remote-control', '--session-id', session, '--permission-mode', 'auto']
    return [native, *ARGV]


def serve(native, cwd, state=None, spawn=PtyChild, sleep=None, stop=None, clock=time.monotonic):
    """Supervise Conductor Claude until `stop` is set: reattach the recorded conversation, restart with backoff.
    The recorded session is read from the state file before every start, so `install --session-id` takes effect at the next one.
    Only after three quick reattach failures in a row (under 30 s, never ready) does a new conversation start; the old id is kept
    as `previousSessionId` and `fresh` is set until a later reattach of the new id is ready."""
    state = Path(state or settings.CONDUCTOR_STATE)
    stop = stop or threading.Event()
    sleep = sleep or stop.wait
    cwd = Path(cwd)
    backoff, failures, restarts, last = BACKOFF_START, 0, 0, read_state(state).get('lastExit')
    while not stop.is_set():
        session = read_state(state).get('sessionId')
        child = spawn(_command(native, session), native_env(os.environ), cwd)
        started, ready, known = clock(), False, session
        _update(state, pid=child.pid, startedAt=int(time.time()), ready=False, restarts=restarts, lastExit=last)
        ring, prompt = Ring(), ''
        while True:
            chunk = child.read(1.0)
            if stop.is_set():
                child.terminate(); child.wait()
                _update(state, ready=False, pid=None)
                return
            if chunk is None: continue
            if not chunk: break
            ring.add(chunk)
            text = ring.text()
            changes = {}
            if not ready and READY.search(text):
                ready, failures, changes['ready'] = True, 0, True
                if session: changes['fresh'] = DELETE
            found = SESSION_URL.findall(text)
            if found and known != 'cse_' + found[-1]:
                known = changes['sessionId'] = 'cse_' + found[-1]
            prompt = (prompt + ANSI.sub('', chunk.decode('utf-8', 'replace')))[-PROMPT_WINDOW:]
            if any(match == str(cwd) for match in TRUST.findall(prompt)):
                child.write(b'y\n'); prompt = ''
            if changes: _update(state, **changes)
        code = child.wait()
        uptime = clock() - started
        changes = {}
        if session and not ready and uptime < REATTACH_GRACE:
            failures += 1
            if failures >= REATTACH_TRIES:
                # The conversation could not be reattached three times in a row: start a new one; device check reports it.
                changes = {'sessionId': DELETE, 'previousSessionId': session, 'fresh': True}
                failures = 0
        else: failures = 0
        restarts, last = restarts + 1, code
        _update(state, ready=False, pid=None, lastExit=code, restarts=restarts, **changes)
        if uptime >= BACKOFF_RESET: backoff = BACKOFF_START
        sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX)


def serve_service():
    """The unit's entry point: supervise in the unit's working directory until the service manager stops it."""
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT): signal.signal(number, lambda *_: stop.set())
    serve(settings.NATIVE_CLAUDE, Path.cwd(), stop=stop)
    return 0


def unit_path():
    """PATH for the Conductor: the user's tools (herdr, ssh, claude-pool, preview, gh) and the system."""
    return ':'.join([str(Path.home() / '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin'])


def definition(root, cwd):
    return {'Label': LABEL, 'ProgramArguments': [service.python(), str(Path(root) / 'run.py'), 'device', 'conductor', 'serve'],
            'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10, 'ExitTimeOut': 30,
            'WorkingDirectory': str(cwd), 'EnvironmentVariables': {'CLAUDE_POOL_CONFIG': str(settings.CONFIG), 'PATH': unit_path()},
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}


def _run(argv, timeout=30):
    try: return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired): return subprocess.CompletedProcess(argv, 255, '', '')


def processes():
    return _run(['ps', '-axww', '-o', 'pid=,ppid=,args=']).stdout or ''


def duplicates(ps_output, own_pid, session_id=None):
    """Conductor processes other than our own child and its descendants (`pid ppid args` lines): Remote Control named
    Conductor Claude, or attached to our recorded session."""
    rows = []
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit(): continue
        rows.append((int(parts[0]), int(parts[1]), parts[2:]))
    ours = {own_pid} if own_pid else set()
    grew = True
    while grew:
        grew = False
        for pid, parent, _ in rows:
            if parent in ours and pid not in ours: ours.add(pid); grew = True
    found = []
    for pid, _, words in rows:
        if 'remote-control' not in words or pid in ours: continue
        named = any(words[i:i + 3] == ['--name', 'Conductor', 'Claude'] and (i + 3 == len(words) or words[i + 3].startswith('-'))
                    for i in range(len(words)))
        attached = bool(session_id) and any(words[i:i + 2] == ['--session-id', session_id] for i in range(len(words)))
        if named or attached: found.append(pid)
    return found


def adoptable(ps_output, pids):
    """The `--session-id` of a hand-started Conductor among `pids` (its conversation can be adopted), or None."""
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit() or int(parts[0]) not in pids: continue
        words = parts[2:]
        for i in range(len(words) - 1):
            if words[i] == '--session-id' and SESSION_ID.fullmatch(words[i + 1]): return words[i + 1]
    return None


def adopt_fix(session_id, prefix=''):
    """How Justus hands a hand-started Conductor to the service; `prefix` is `ssh <target> ` when shown from another device."""
    if not session_id: return HERDR_FIX
    return ('stop the hand-started Conductor (herdr session conductor), then run: ' + prefix +
            'claude-pool device conductor install --session-id ' + session_id)


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0: return False
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False
    except PermissionError: return True


def _own_child(record):
    pid = record.get('pid')
    return pid if _alive(pid) and service.loaded(LABEL) else None


def _manager(*args):
    manager = ['launchctl'] if service.platform() == 'darwin' else ['systemctl', '--user']
    return _run([*manager, *args]).returncode


def _identity_accounts(entry):
    """Pool accounts whose identity is the Conductor's (email, and organization when both name one)."""
    known, native = registry.load(), {'email': entry['email'], 'organization': entry['organization']}
    from account_metadata import load
    rows = load(settings.CONFIG).get('accounts', [])
    matches = []
    for row in rows:
        try:
            if accounts_cli.matches_native(native, row['name'], known, settings.STORES): matches.append(row)
        except RuntimeError: continue
    return matches


def reserve_default(entry):
    """Reserve the Conductor's pool account from 85 % unless Justus set a reserve (any `reserveAt`, even 0)."""
    done, failed = [], []
    for row in _identity_accounts(entry):
        if 'reserveAt' in row: continue
        try:
            config, binary, paths, env = accounts_cli.local_context()
            accounts_cli.reserve(config, binary, paths, env, row['name'], RESERVE_DEFAULT)
            done.append(row['name'])
        except Exception: failed.append(row['name'])
    return done, failed


def install(session_id=None, fresh=False, moving=False):
    """Install and start the Conductor service on this device. `session_id` adopts a conversation started by hand; `fresh`
    starts a new conversation (move); `moving` (internal, from move) allows a device the registry does not name yet."""
    if session_id is not None and not SESSION_ID.fullmatch(session_id): raise ValueError('Invalid Conductor session id')
    entry = registry.load().get('conductor')
    if not entry: return Step(STEP, 'failed', 'the registry names no Conductor', 'claude-pool device add <ssh-target> --usage-owner')
    me = registry.self_name()
    if not moving and entry['device'] != me:
        return Step(STEP, 'failed', 'the registry names ' + entry['device'] + ' as the Conductor device, not this one',
                    'claude-pool device conductor move ' + str(me or '<this device>'))
    state = settings.CONDUCTOR_STATE
    record = read_state(state)
    changes = {}
    if session_id and record.get('sessionId') != session_id: changes = {'sessionId': session_id, 'fresh': DELETE}
    elif fresh:
        changes = {'fresh': True, 'sessionId': DELETE}
        if record.get('sessionId'): changes['previousSessionId'] = record['sessionId']
    if changes: record = _update(state, **changes)
    listing = processes()
    found = duplicates(listing, _own_child(record), record.get('sessionId'))
    if found: return Step(STEP, 'failed', 'another Conductor is running (pid ' + ', '.join(map(str, found)) + ')',
                          adopt_fix(adoptable(listing, found)))
    cwd = Path(entry['cwd']).expanduser()
    if not cwd.is_absolute(): return Step(STEP, 'failed', 'the Conductor folder must be an absolute path', None)
    cwd.mkdir(parents=True, exist_ok=True)
    wanted = definition(service.install_root(settings.HERE.parent), cwd)
    unit = service.service_file(LABEL)
    unit.parent.mkdir(parents=True, exist_ok=True)
    changed = not unit.exists() or service.load(unit.read_bytes()) != service.load(service.render(wanted))
    fix = 'claude-pool device conductor install'
    if service.platform() == 'darwin':
        target = 'gui/' + str(os.getuid()) + '/' + LABEL
        loaded = _manager('print', target) == 0
        if changed and loaded: _manager('bootout', target); loaded = False
        if changed: service.write(unit, service.render(wanted))
        if not loaded:
            if _manager('bootstrap', 'gui/' + str(os.getuid()), str(unit)): return Step(STEP, 'failed', 'launchd did not load the Conductor', fix)
        elif changes: _manager('kickstart', '-k', target)  # reattach the newly recorded conversation now
    else:
        active = _manager('is-active', '--quiet', LABEL + '.service') == 0
        if changed:
            service.write(unit, service.render(wanted))
            _manager('daemon-reload')
        if _manager('enable', '--now', LABEL + '.service'): return Step(STEP, 'failed', 'systemd did not start the Conductor', fix)
        if active and (changed or changes): _manager('restart', LABEL + '.service')
    reserved, failed = reserve_default(entry)
    detail = 'installed in ' + str(cwd)
    if reserved: detail += '; reserved ' + ', '.join(reserved) + ' from ' + str(RESERVE_DEFAULT) + ' %'
    if failed:
        # The service runs; only the reserve is missing, and Justus can apply it himself.
        return Step(STEP, 'manual', detail + '; reserve not applied for ' + ', '.join(failed),
                    'claude-pool account reserve ' + failed[0] + ' ' + str(RESERVE_DEFAULT))
    return Step(STEP, 'ok', detail)


def uninstall():
    """Stop and remove the Conductor service of this device. The pool service is never touched."""
    unit = service.service_file(LABEL)
    if service.platform() == 'darwin':
        _manager('bootout', 'gui/' + str(os.getuid()) + '/' + LABEL)
    else:
        _manager('disable', '--now', LABEL + '.service')
    unit.unlink(missing_ok=True)
    if service.platform() == 'linux': _manager('daemon-reload')
    if service.loaded(LABEL): return Step(STEP, 'failed', 'the Conductor service is still loaded', service.unload_hint(LABEL))
    return Step(STEP, 'ok', 'removed')


def status():
    try:
        record = read_state()
        installed = service.service_file(LABEL).exists()
        own = _own_child(record) if installed else None
        listing = processes()
        found = duplicates(listing, own, record.get('sessionId'))
        return {'installed': installed, 'running': own is not None, 'ready': own is not None and bool(record.get('ready')),
                'fresh': bool(record.get('fresh')), 'duplicates': found, 'adoptSessionId': adoptable(listing, found)}
    except Exception as error:
        return {'error': type(error).__name__}


def status_part(): return {'conductor': status()}


def _target(value, name):
    row = registry.device(value, name)
    return row.get('ssh') if row else None


def check(statuses, value):
    entry = value.get('conductor')
    if not entry: return Step(STEP, 'skipped', 'no Conductor configured')
    device = entry['device']
    parts = {name: (s.get('conductor') or {}) for name, s in statuses.items() if isinstance(s, dict)}
    running = sorted(name for name, part in parts.items() if part.get('running'))
    broken = sorted(name for name, part in parts.items() if part.get('error'))
    if broken:
        return Step(STEP, 'failed', '; '.join('status error on ' + name + ': ' + str(parts[name]['error']) for name in broken), None)
    extra = {name: part.get('duplicates') for name, part in parts.items() if part.get('duplicates')}
    if extra:
        first = sorted(extra)[0]
        session = parts[first].get('adoptSessionId')
        session = session if isinstance(session, str) and SESSION_ID.fullmatch(session) else None
        prefix = '' if first == registry.self_name() else 'ssh ' + str(_target(value, first) or first) + ' '
        return Step(STEP, 'failed', 'another Conductor runs on ' + ', '.join(sorted(extra)), adopt_fix(session, prefix))
    if len(running) > 1:
        others = [name for name in running if name != device] or running[1:]
        return Step(STEP, 'failed', 'the Conductor runs on ' + ', '.join(running), 'ssh ' + str(_target(value, others[0]) or others[0]) +
                    ' claude-pool device conductor uninstall')
    if running and running[0] != device:
        return Step(STEP, 'failed', 'the Conductor runs on ' + running[0] + ', the registry names ' + device,
                    'claude-pool device conductor move ' + device)
    if device not in statuses: return Step(STEP, 'unreachable', device + ' did not report its status', None)
    if not running:
        return Step(STEP, 'failed', 'not running on ' + device, 'ssh ' + str(_target(value, device) or device) + ' claude-pool device conductor install')
    here = statuses[device]
    if not parts[device].get('ready'): return Step(STEP, 'failed', 'running on ' + device + ' but not connected to Remote Control', None)
    if here.get('platform') == 'linux' and here.get('linger') is False:
        return Step(STEP, 'failed', 'stops when ' + device + ' logs out (linger is off)', 'claude-pool device add ' + str(_target(value, device) or device))
    if parts[device].get('fresh'):
        return Step(STEP, 'manual', 'ready on ' + device + ' in a new conversation; the old one could not be reattached',
                    'Open the new Conductor Claude conversation in the Claude app (Code, environment of ' + device + ').')
    return Step(STEP, 'ok', 'ready on ' + device)


def context_text(value):
    entry = value.get('conductor') or {}
    lines = ['## Devices (managed by claude-pool, rewritten on every device sync)', '',
             'Work runs in herdr sessions on these machines, through the pool (`claude` there is `claude-pool exec`). '
             'This conversation coordinates; start and continue work in a herdr session, never in this process.',
             'Never keep a static list of herdr sessions; derive them live: on the Mac, `herdr machine list` shows one '
             'profile per remote herdr session, labelled with the device name.', '']
    for row in value.get('devices', []):
        where = ['ssh `' + row['ssh'] + '`'] if row.get('ssh') else ['no ssh target']
        where.append('herdr profile `' + row['name'] + '` (open it with `herdr --machine ' + row['name'] + '`)')
        if row.get('herdrSession'): where.append('remote herdr session `' + row['herdrSession'] + '`')
        mark = ' (this Conductor runs here)' if row['name'] == entry.get('device') else ''
        lines.append('- ' + row['name'] + mark + ', ' + {'darwin': 'macOS', 'linux': 'Linux'}.get(row.get('platform'), 'unknown') +
                     ': ' + '; '.join(where))
    return '\n'.join(lines)


def write_context(cwd, value):
    """Refresh the managed device block in `cwd/CLAUDE.md`; every byte outside it stays. True when the file changed."""
    path = Path(cwd).expanduser() / 'CLAUDE.md'
    try: return managed_block.write(path, START, END, context_text(value))
    except ValueError as error:
        print('Left ' + str(path) + ' unchanged: ' + str(error))
        return False


def apply_hook(value):
    entry = value.get('conductor')
    if entry and entry['device'] == registry.self_name() and Path(entry['cwd']).expanduser().is_dir(): write_context(entry['cwd'], value)


def _status_of(remote):
    done = remote.device('status', '--json')
    if done.returncode: return None
    try: value = json.loads(done.stdout)
    except ValueError: return None
    return value if isinstance(value, dict) else None


def move(target, value, remotes=Remote, local=None, sleep=time.sleep):
    """Move the Conductor to `target`: install there, wait until it is ready, then remove it from the old device."""
    from device_install import check_linger
    local = local or Local()
    entry = value.get('conductor')
    if not entry: print('The registry names no Conductor.'); return 2
    source, me = entry['device'], registry.self_name()
    if target == source: print('The Conductor already runs on ' + target + '.'); return 0
    if registry.device(value, target) is None: print('No device named ' + target + ' (see claude-pool device list).'); return 2
    def reach(name):
        if name == me: return local
        ssh = _target(value, name)
        return remotes(ssh) if ssh else None
    there = reach(target)
    if there is None: print(target + ' has no ssh target; run claude-pool device add <ssh-target> --name ' + target + '.'); return 2
    found = _status_of(there)
    if found is None: print('Could not read the status of ' + target + '; nothing was changed.'); return 2
    native = found.get('nativeLogin')
    same = isinstance(native, dict) and all(accounts_cli.normalized(str(native.get(key) or '')) == accounts_cli.normalized(entry[key])
                                           for key in ('email', 'organization'))
    if not same:
        print('The Native login on ' + target + ' is not the Conductor account (' + entry['email'] + ', ' + entry['organization'] +
              '); sign Native in there with that account first. Nothing was changed.')
        return 1
    if check_linger(there).status not in ('ok', 'skipped'):
        print(target + ' stops its services at logout; run claude-pool device add ' + str(_target(value, target)) + ' first. Nothing was changed.')
        return 1
    if found.get('service') != 'running':
        print('The pool service on ' + target + ' is not running; start it first (claude-pool --start). Nothing was changed.')
        return 1
    done = there.device('conductor', 'install', '--move', '--fresh')
    if done.returncode:
        there.device('conductor', 'uninstall')  # it may have started before failing; never leave a second Conductor
        print('Installing the Conductor on ' + target + ' failed; the Conductor stays on ' + source + '.')
        return 1
    for _ in range(MOVE_WAIT // MOVE_POLL):
        part = (_status_of(there) or {}).get('conductor') or {}
        if part.get('ready'): break
        sleep(MOVE_POLL)
    else:
        there.device('conductor', 'uninstall')
        print('The Conductor on ' + target + ' did not connect within ' + str(MOVE_WAIT) + ' s; it stays on ' + source + '.')
        return 1
    old = reach(source)
    gone = old.device('conductor', 'uninstall') if old is not None else None
    if gone is None or gone.returncode == 255:
        print(source + ' is unreachable; its old Conductor is removed there later (claude-pool device check reports it).')
    elif gone.returncode:
        print('Could not remove the old Conductor on ' + source + '; run there: claude-pool device conductor uninstall')
    def note(current):
        if current.get('conductor'): current['conductor']['device'] = target
    registry.update(note)
    device_sync.sync_all(remotes)
    print('The Conductor now runs on ' + target + ' in a new conversation: open Conductor Claude in the Claude app '
          '(Code, environment of ' + target + ').')
    return 0


device_sync.STATUS_PARTS.append(status_part)
device_sync.INTEGRATION_HOOKS.append(apply_hook)
