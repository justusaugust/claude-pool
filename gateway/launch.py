"""Start/reuse the local account pool, then run the user's ordinary Claude TUI."""
import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import threading
import tomllib
import uuid
import re
import tempfile
from views import usage, history

from bridge import PoolBridge, atomic_write
import readings
from native_auth import NativeAuth
import service
from validation import render as validation_view

from settings import HERE, RUNTIME, STORES, LONG_LIVED, NATIVE_CLAUDE, CONFIG, BASE_URL, GATEWAY_PORT, BACKEND_PORT, backend_config, connection_ts, SHARED_READINGS, ALIASES, private_directory
ROOT = HERE.parent
HEALTH_INTERVAL = 10
INSTALLATION = json.loads((ROOT / 'installation.json').read_text()) if (ROOT / 'installation.json').is_file() else {}
SERVICE_LABEL = INSTALLATION.get('serviceLabel', 'io.github.claude-pool')
if not isinstance(SERVICE_LABEL, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+', SERVICE_LABEL): raise RuntimeError('Invalid installed service label')
SERVICE_FILE = service.service_file(SERVICE_LABEL)
ACCOUNT_SHORTCUTS = ALIASES


def service_command(*args, check=True):
    manager = ['launchctl'] if service.platform() == 'darwin' else ['systemctl', '--user']
    result = subprocess.run([*manager, *args], capture_output=True, text=True)
    if check and result.returncode: raise RuntimeError('Background service control failed')
    return result.returncode


def service_definition():
    receipt = ROOT / 'installation.json'
    root = Path(json.loads(receipt.read_text()).get('root', ROOT)) if receipt.is_file() else ROOT
    entry = root / 'run.py' if (root / 'run.py').is_file() else HERE / 'launch.py'
    return {'Label': SERVICE_LABEL, 'ProgramArguments': ['/opt/homebrew/bin/python3' if Path('/opt/homebrew/bin/python3').exists() else sys.executable, str(entry), '--daemon'],
            'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10,
            'ExitTimeOut': 360, 'WorkingDirectory': str(root),
            'EnvironmentVariables': {'POOL_MANAGED': '1', 'CLAUDE_POOL_CONFIG': str(CONFIG)},
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}


def install_service():
    SERVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    definition = service_definition()
    if SERVICE_FILE.exists() and service.load(SERVICE_FILE.read_bytes()) != definition:
        raise RuntimeError('Existing pool service definition differs; review before replacing')
    written = not SERVICE_FILE.exists()
    if written: service.write(SERVICE_FILE, service.render(definition))
    if service.platform() == 'linux':
        # Reload only for our own new or changed unit; other user services stay untouched.
        if written or service.needs_reload(SERVICE_LABEL): service_command('daemon-reload')
        # Enabling mirrors RunAtLoad: the unit starts at login and, with linger, at boot.
        service_command('enable', SERVICE_LABEL + '.service')
        return SERVICE_LABEL + '.service'
    domain = f'gui/{os.getuid()}'
    if service_command('print', domain + '/' + SERVICE_LABEL, check=False):
        service_command('bootstrap', domain, str(SERVICE_FILE))
    return domain + '/' + SERVICE_LABEL


def managed_start():
    unit = install_service()
    service_command('start' if service.platform() == 'linux' else 'kickstart', unit)



def token(name):
    path = STORES / name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise RuntimeError("Pool credential file has unsafe permissions")
            value = f.read(256).strip()
            if len(value) < 32:
                raise RuntimeError("Pool credential file is incomplete")
            return value
    with os.fdopen(fd, "w") as f:
        value = secrets.token_urlsafe(32)
        f.write(value + "\n")
    return value


def request(client, method="GET", path="/health", body=None):
    conn = http.client.HTTPConnection("127.0.0.1", GATEWAY_PORT, timeout=30 if path == '/pool/validation' else 3)
    try:
        headers = {"Authorization": "Bearer " + client}
        if body is not None: headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = conn.getresponse()
        body = response.read()
        if response.status in {502, 503}:
            raise ConnectionError("Pool is starting")
        if response.status != 200:
            raise RuntimeError("Pool authentication or health check failed")
        # Keep display updates independent of the running inference owner.
        if method == 'GET' and (path == '/pool/overview' or path.startswith('/cockpit/')):
            from telemetry import enrich, read_admin
            data = json.loads(body)
            if any('fableWeekly' not in a for a in data.get('accounts', [])):
                try: enrich(data, read_admin(token('.pool-admin-token')))
                except (OSError, ValueError, RuntimeError, http.client.HTTPException): pass
            body = json.dumps(data).encode()
        if method == 'GET' and (path == '/pool/overview' or path.startswith('/cockpit/')):
            from account_metadata import decorate
            body = json.dumps(decorate(json.loads(body), CONFIG)).encode()
        return body
    finally:
        conn.close()


def daemon(client, admin):
    if (RUNTIME / 'maintenance').exists(): return
    private_directory(RUNTIME)
    config = backend_config(RUNTIME)
    cfg = RUNTIME / "shunt.toml"
    cfg.write_text(config)
    env = os.environ.copy()
    env.update(POOL_CLIENT_TOKENS="cockpit:" + client, POOL_ADMIN_TOKENS="cockpit:" + admin,
               SHUNT_CLAUDE_ACCOUNTS_DIR=str(STORES), RUST_LOG="error")
    binary = HERE / "bin/shunt"
    if not binary.exists():
        raise RuntimeError("The packaged Shunt binary is missing")
    def start_backend():
        process = subprocess.Popen([str(binary), "--config", str(cfg), "run"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            atomic_write(RUNTIME / "owner.json", json.dumps({"daemon": os.getpid(), "backend": process.pid, "managed": os.environ.get("POOL_MANAGED") == "1"}))
        except OSError:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            raise
        return process
    if recover_orphan(): raise RuntimeError("Pool is already owned by another daemon")
    gateway = start_backend()
    server, stop, watcher = None, threading.Event(), None
    try:
        names = [a['name'] for a in tomllib.loads(config).get('providers', {}).get('anthropic', {}).get('accounts', [])]
        server = PoolBridge(("127.0.0.1", GATEWAY_PORT), BACKEND_PORT, client, admin, RUNTIME / "sessions.json", native_auth=NativeAuth(STORES, names), shared_readings=SHARED_READINGS)
        readings.Puller(SHARED_READINGS, server.journal).start(stop)
        def drain(signum, frame):
            server.draining = True
            server.journal.record('service_draining', requests=len(server.requests))
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, drain)
        signal.signal(signal.SIGINT, drain)
        server.journal.record('service_started')
        def wait_ready(process):
            for _ in range(60):
                if stop.is_set() or process.poll() is not None: raise RuntimeError("Shunt exited before readiness")
                try:
                    server.restore_pauses()
                    return
                except (OSError, RuntimeError, ValueError, http.client.HTTPException):
                    stop.wait(.25)
            raise RuntimeError("Shunt did not become ready")
        wait_ready(gateway)
        def supervise():
            nonlocal gateway
            delay, next_probe, next_sample = .5, time.monotonic(), time.monotonic() + 30
            while not stop.wait(.5):
                now = time.monotonic()
                if gateway.poll() is None:
                    if now >= next_probe:
                        server.check_backend()
                        next_probe = time.monotonic() + HEALTH_INTERVAL
                    if server.health['failures'] < 3:
                        if now >= next_sample and server.health['backend'] == 'ready':
                            try: server.accounts()
                            except (OSError, RuntimeError, ValueError, http.client.HTTPException): pass
                            next_sample = time.monotonic() + 30
                        continue
                    if not restart_idle(server, gateway): continue
                else:
                    server.journal.record('backend_exited')
                server.health["backend"] = "starting"
                server.recovery.update(state="restarting", lastAt=int(time.time() * 1000))
                if stop.wait(delay): break
                try:
                    gateway = start_backend()
                    wait_ready(gateway)
                    server.check_backend()
                    server.recovery.update(state="ready", restarts=server.recovery["restarts"] + 1)
                    server.journal.record('backend_restarted', restarts=server.recovery['restarts'])
                    delay = .5
                except (OSError, RuntimeError, ValueError, http.client.HTTPException):
                    gateway.terminate()
                    try: gateway.wait(timeout=5)
                    except subprocess.TimeoutExpired: gateway.kill(); gateway.wait()
                    server.journal.record('restart_failed')
                    delay = min(30, delay * 2)
        watcher = threading.Thread(target=supervise, daemon=True)
        watcher.start()
        server.serve_forever()
    finally:
        stop.set()
        if watcher: watcher.join(timeout=16)
        if server: server.server_close()
        if gateway.poll() is None:
            gateway.terminate()
            try: gateway.wait(timeout=35)
            except subprocess.TimeoutExpired: gateway.kill(); gateway.wait()


def process_command(pid):
    if not isinstance(pid, int) or pid <= 1: return ""
    return subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True).stdout.strip()


def recover_orphan():
    try: owner = json.loads((RUNTIME / "owner.json").read_text())
    except (OSError, ValueError): return False
    if not isinstance(owner, dict): return False
    if process_command(owner.get("daemon")).endswith(str(Path(__file__).resolve()) + " --daemon"):
        return True
    pid = owner.get("backend")
    expected = f"{HERE / 'bin/shunt'} --config {RUNTIME / 'shunt.toml'} run"
    if process_command(pid) != expected: return False
    parent = subprocess.run(["ps", "-p", str(pid), "-o", "ppid="], capture_output=True, text=True).stdout.strip()
    if not orphaned(parent): return False
    try: os.kill(pid, signal.SIGTERM)
    except ProcessLookupError: return False
    for _ in range(30):
        if process_command(pid) != expected: return False
        time.sleep(.1)
    if process_command(pid) == expected: os.kill(pid, signal.SIGKILL)
    return False


def orphaned(parent):
    """An orphan is reparented to init, or on Linux to the user's own systemd instance."""
    if parent == "1": return True
    if service.platform() != 'linux' or not parent.isdigit(): return False
    command = process_command(int(parent)).split()
    return bool(command) and Path(command[0]).name == 'systemd' and '--user' in command


def restart_idle(server, process):
    # The check and admission gate share a lock; no newly admitted request can be killed.
    with server.lock:
        if server.requests:
            server.recovery['state'] = 'waiting for active responses'
            server.journal.record('restart_deferred', requests=len(server.requests))
            return False
        server.draining = True
    try:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        return True
    finally:
        server.draining = False


def ensure_pool(client):
    with (RUNTIME / "startup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (RUNTIME / 'maintenance').exists(): raise RuntimeError('Pool deliberately stopped; run claude-pool --start')
        try:
            request(client, path="/pool/status")
            return
        except OSError:
            managed_start()
            for _ in range(100):
                try:
                    request(client, path="/pool/status")
                    return
                except OSError: time.sleep(.25)
            raise RuntimeError("Local pool did not start; run claude pool doctor")


def show_usage(client, once=False):
    live = not once and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    if not live:
        data = json.loads(request(client, path="/pool/overview"))
        print(usage(data, width=shutil.get_terminal_size().columns, color=color))
        return

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    data, refreshed = None, None
    try:
        sys.stdout.write("\033[?1049h\033[2J\033[?25l")
        while True:
            offline = False
            try:
                data = json.loads(request(client, path="/pool/overview"))
                refreshed = time.strftime("%H:%M:%S")
            except (OSError, RuntimeError, ValueError):
                offline = True
                try: ensure_pool(client)
                except (OSError, RuntimeError): pass
            size = shutil.get_terminal_size()
            width, height = max(20, size.columns), max(4, size.lines)
            lines = usage(data, width=width, color=color).splitlines() if data is not None else ["  Pool unavailable", "  Retrying in 5s"]
            if offline and data is not None:
                lines.insert(0, "  Last known state; readings may be stale." if width >= 42 else "  Last known state")
            footer = ("Offline · retrying · Ctrl-C exit" if offline else f"Live · {refreshed} · 5s · Ctrl-C exit") if width >= 40 else ("Offline · Ctrl-C" if offline else "Live · Ctrl-C exit")
            while len(lines) + 1 > height:
                blanks = [i for i, line in enumerate(lines) if not line.strip()]
                if not blanks: break
                lines.pop(blanks[-1])
            if len(lines) + 1 > height:
                lines = lines[:height - 2] + ["  Enlarge terminal"]
            lines.append("  " + footer)
            sys.stdout.write("\033[H" + "\033[K\r\n".join(lines) + "\033[K\033[J")
            sys.stdout.flush()
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()
        signal.signal(signal.SIGTERM, previous)


def background_plugin(account, origin=None):
    source = HERE / 'background-bootstrap'
    version = json.loads((source / '.claude-plugin/plugin.json').read_text())['version']
    connection = connection_ts()
    folder = RUNTIME / 'background-plugins' / (version + '-' + (account or 'auto') + ('-' + origin if origin else '') + '-' + hashlib.sha256(connection.encode()).hexdigest()[:12])
    files = {name: (source / name).read_text() for name in ['.claude-plugin/plugin.json', 'hooks/hooks.json', 'hooks/register.ts', 'hooks/limits.ts']}
    files['hooks/connection.ts'] = connection
    files['hooks/account.ts'] = 'export const account: string | null = ' + json.dumps(account) + '\nexport const origin: string | null = ' + json.dumps(origin) + '\n'
    for name, content in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_text() != content: raise RuntimeError('Background bootstrap changed; update its version before starting new agents')
        else: atomic_write(path, content)
    return folder


def respawn_agent(arguments):
    if len(arguments) != 1 or not re.fullmatch(r'[a-f0-9]{8}', arguments[0]):
        raise RuntimeError('Use claude pool respawn <exact-agent-id> for one background task; list IDs with claude-native agents --json.')
    agent_id = arguments[0]
    def target():
        result = subprocess.run([NATIVE_CLAUDE, 'agents', '--json', '--all'], capture_output=True, text=True, timeout=20)
        try: rows = json.loads(result.stdout) if result.returncode == 0 else None
        except ValueError: rows = None
        if not isinstance(rows, list): raise RuntimeError('Unable to confirm the target agent; nothing was restarted.')
        matches = [r for r in rows if isinstance(r, dict) and r.get('id') == agent_id and r.get('kind') == 'background']
        if len(matches) != 1 or not isinstance(matches[0].get('sessionId'), str) or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', matches[0]['sessionId']):
            raise RuntimeError('Exact background agent identity unavailable; nothing was restarted.')
        return matches[0]['sessionId']
    private_directory(RUNTIME)
    with (RUNTIME / ('respawn-' + agent_id + '.lock')).open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('This agent already has a respawn in progress.') from None
        session = target()
        result = subprocess.run([NATIVE_CLAUDE, 'respawn', agent_id], capture_output=True, text=True, timeout=30)
        if result.returncode: raise RuntimeError('Targeted respawn failed; no continuation was sent.')
        try: current = target()
        except RuntimeError: raise RuntimeError('Agent restarted, but its identity could not be reconfirmed; no continuation was sent.') from None
        if current != session: raise RuntimeError('Agent identity changed after respawn; no continuation was sent.')
        with tempfile.TemporaryDirectory(prefix='pool-respawn-controller-') as tmp:
            receipt = Path(tmp) / 'result.json'
            env = os.environ.copy()
            for key in ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY']:
                env.pop(key, None)
            env.update(COCKPIT_RESPAWN_SESSION=session, COCKPIT_RESPAWN_RESULT=str(receipt))
            subprocess.run([NATIVE_CLAUDE, '-p', '--no-session-persistence', '--name', 'pool-respawn-control', '--setting-sources', '',
                            '--permission-mode', 'default', '--allowedTools', 'SendMessage',
                            '--tools', 'SendMessage', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                            '--plugin-dir', str(HERE / 'respawn-continuation'), '--', 'Continuation control only.'],
                           cwd=tmp, env=env, capture_output=True, text=True, timeout=30)
            try: delivered = json.loads(receipt.read_text())
            except (OSError, ValueError): delivered = None
            if not isinstance(delivered, dict) or delivered.get('delivered') is not True or delivered.get('session') != session:
                raise RuntimeError('Agent restarted, but continuation delivery is unconfirmed; no automatic resend. Attach to inspect it.')
        print('Agent ' + agent_id + ' respawned; continuation queued for its existing task.')


def configured_plugin(name):
    connection = connection_ts()
    version = json.loads((HERE / name / '.claude-plugin/plugin.json').read_text())['version']
    folder = RUNTIME / 'plugins' / (name + '-' + version + '-' + hashlib.sha256(connection.encode()).hexdigest()[:12])
    files = {str(f.relative_to(HERE / name)): f.read_text() for f in (HERE / name).rglob('*')
             if f.is_file() and '__pycache__' not in f.parts and '.claude-plugin/types' not in str(f)}
    files['hooks/connection.ts'] = connection
    if name == 'session-control':
        # Copied command hooks need the same retained release and config as the Mod.
        hooks = json.loads(files['hooks/hooks.json'])
        command = 'CLAUDE_POOL_CONFIG=' + shlex.quote(str(CONFIG)) + ' ' + shlex.join([sys.executable, str(HERE / 'control_hook.py'), 'hook'])
        for groups in hooks['hooks'].values():
            for group in groups:
                for hook in group['hooks']:
                    hook['command'] = command
        files['hooks/hooks.json'] = json.dumps(hooks) + '\n'
    for name, content in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text() != content: raise RuntimeError('Installed plugin changed; use a new package version before launching')
        if not path.exists(): atomic_write(path, content)
    return folder


def native_identity():
    """This machine's Native login ({'email', 'organization'}), or None when Native is signed out."""
    from native_login import parse_auth_status
    unreadable = RuntimeError('Could not read the Native login (claude-native auth status --json); nothing was changed')
    try: done = subprocess.run([NATIVE_CLAUDE, 'auth', 'status', '--json'], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired): raise unreadable from None
    try: native = parse_auth_status(done.stdout)
    except ValueError: raise unreadable from None
    # A signed-out Native may exit non-zero while still reporting loggedIn false; any other failure is unreadable.
    if native is not None and done.returncode: raise unreadable
    return native


def main():
    # The shell's claude adapter preserves Native commands; claude-pool owns pool commands.
    native_entry = sys.argv[1:2] == ['exec']
    if native_entry: del sys.argv[1]
    if sys.argv[1:2] == ['pool']:
        del sys.argv[1]
        native_entry = False
        if len(sys.argv) == 1: sys.argv.append('help')
    if native_entry:
        from account_metadata import NATIVE_COMMANDS
        if sys.argv[1:2] and sys.argv[1] in NATIVE_COMMANDS | {'--help', '-h', '--version'}:
            code = subprocess.call([NATIVE_CLAUDE, *sys.argv[1:]])
            if code == 0 and sys.argv[1:] in [['--help'], ['-h']]:
                print('\nPool: sessions use pooling automatically.\n  claude usage             Live account usage\n  claude pool update       Update Claude Pool\n  claude update            Update Claude Code\n  claude pool help         Pool commands')
            raise SystemExit(code)
    if len(sys.argv) > 1 and sys.argv[1] == 'update':
        from update import cli
        raise SystemExit(cli(sys.argv[2:], HERE))
    if len(sys.argv) > 1 and sys.argv[1] in {'account', 'login'}:
        from accounts_cli import cli
        argv = sys.argv[2:]
        if sys.argv[1] == 'login':
            if len(argv) != 1: raise RuntimeError('Usage: claude login <name>, or claude account add <email>')
            argv = ['add', '--name', argv[0]]
        def overview():
            return json.loads(request(token('.pool-client-token'), path='/pool/overview'))
        live = bool(argv) and argv[0] in ('add', 'remove') and '--help' not in argv and '-h' not in argv and (STORES / '.pool-client-token').is_file()
        try:
            if not live: raise OSError('No live readiness check needed')
            overview(); available = True
        except (OSError, RuntimeError, ValueError):
            # remove never skips its in-flight check while the service runs: an unanswered overview counts as busy.
            available = live and argv[0] == 'remove' and service.loaded(SERVICE_LABEL)
        on_change = None
        try:
            import registry
            me = registry.self_name()
            if me is not None and any(row['name'] != me for row in registry.load()['devices']):
                import device_sync, functools
                on_change = functools.partial(device_sync.sync_all)
        except ValueError: pass  # an unreadable registry never blocks adding or removing an account
        raise SystemExit(cli(argv, config=CONFIG, stores=STORES, binary=HERE/'bin/shunt', runtime=RUNTIME,
                             overview=overview if available else None, long_lived=LONG_LIVED, native_identity=native_identity,
                             on_change=on_change))
    if sys.argv[1:] in [['--help'], ['-h']]: sys.argv[1:] = ['help']
    if sys.argv[1:] == ['--version']:
        raise SystemExit(subprocess.call([NATIVE_CLAUDE, *sys.argv[1:]]))
    if sys.argv[1:] == ['help']:
        print('claude-pool [account-alias] [Claude arguments]\nCommands: update, usage [--once], history, forecast-check, doctor, account add <email>, account list, account remove <name> [--wait], account reserve <name> --at <pct>|--off, login <name>, phone, respawn <id>\nUpdates: claude update = Claude Code; claude-pool update or claude pool update = Claude Pool\nService: --start, --stop\nSetup: ' + str(ROOT / 'docs/SETUP.md'))
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'phone':
        from control_main import cli
        return cli(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == 'respawn':
        try: respawn_agent(sys.argv[2:])
        except subprocess.TimeoutExpired: raise RuntimeError('Respawn control timed out; outcome unknown. Inspect the target before retrying.') from None
        return
    if len(sys.argv) > 2 and sys.argv[1] in ACCOUNT_SHORTCUTS and sys.argv[2] == 'respawn':
        raise RuntimeError('Respawn preserves existing routing; use claude pool respawn <exact-agent-id>.')
    parser = argparse.ArgumentParser(add_help=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true")
    mode.add_argument("--stop", action="store_true")
    mode.add_argument("--start", action="store_true")
    args, claude_args = parser.parse_known_args()
    account = ACCOUNT_SHORTCUTS[claude_args.pop(0)] if claude_args and claude_args[0] in ACCOUNT_SHORTCUTS else None
    options = claude_args[:claude_args.index('--')] if '--' in claude_args else claude_args
    background = any(arg in {'--bg', '--background'} for arg in options)
    if background and any(arg in {'--safe-mode', '--bare'} for arg in options):
        raise SystemExit('Pool background sessions require the routing bootstrap; omit --safe-mode and --bare.')
    if account:
        if args.start or args.stop or args.daemon:
            raise SystemExit('Account shortcuts start sessions; they cannot control the pool service.')
        if any(arg == '--session-id' or arg.startswith('--session-id=') for arg in options):
            raise SystemExit('Account shortcuts allocate their own session ID; omit --session-id.')
        resuming = any(arg in {'--continue', '--resume'} or arg.startswith(('-c', '-r', '--resume=')) for arg in options)
        # A resumed window gets its own pin, even while the source remains open.
        if resuming and '--fork-session' not in options: claude_args = ['--fork-session', *claude_args]
    private_directory(RUNTIME)
    if not NATIVE_CLAUDE or not Path(NATIVE_CLAUDE).is_file(): raise RuntimeError("Install Native Claude Code, then set native_claude in the pool config")
    private_directory(STORES)
    if args.daemon:
        daemon(token(".pool-client-token"), token(".pool-admin-token"))
        return
    with (RUNTIME / "startup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        client, admin = token(".pool-client-token"), token(".pool-admin-token")
        if args.start:
            (RUNTIME / 'maintenance').unlink(missing_ok=True)
            managed_start()
        if args.stop:
            atomic_write(RUNTIME / 'maintenance', 'Deliberately stopped\n')
            try: request(client, "POST", "/pool/shutdown")
            except OSError:
                print('Local pool already stopped.'); return
            for _ in range(160):
                try:
                    request(client)
                except OSError:
                    # The daemon releases Shunt's socket after draining the bridge.
                    conn = http.client.HTTPConnection("127.0.0.1", BACKEND_PORT, timeout=1)
                    try:
                        conn.connect()
                    except OSError:
                        print("Local pool stopped.")
                        return
                    finally:
                        conn.close()
                time.sleep(.25)
            raise RuntimeError("Pool is still draining; retry after the active turn finishes")
    ensure_pool(client)
    if args.start:
        print("Local pool started.")
        return
    if not account and claude_args and claude_args[0] in {"usage", "history", "forecast-check", "doctor"}:
        if claude_args not in (["usage"], ["usage", "--once"], ["history"], ["forecast-check"], ["doctor"]):
            raise SystemExit("Usage: claude usage [--once] | claude history | claude forecast-check | claude pool doctor")
        if claude_args[0] == "usage":
            show_usage(client, once="--once" in claude_args)
        elif claude_args[0] == 'history':
            print(history(json.loads(request(client, path="/pool/overview"))))
        elif claude_args[0] == 'doctor':
            value = json.loads(request(client, path='/pool/diagnostics'))
            health = value['health']
            print(f"Claude pool · {health['supervisor']} · backend {health['backend']} · {health['activeRequests']}/{health['requestLimit']} requests")
            print('Metadata storage: ' + ('needs attention' if health['storageError'] else 'ready'))
            for incident in value['incidents'][:10]: print('  ' + incident['message'])
        else:
            print(validation_view(json.loads(request(client, path='/pool/validation'))))
        return

    env = os.environ.copy()
    for key in ['COCKPIT_CONTROL_TASK', 'COCKPIT_CONTROL_GENERATION', 'COCKPIT_CONTROL_SESSION', 'COCKPIT_CONTROL_SOURCE']:
        env.pop(key, None)
    for key in ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]:
        env.pop(key, None)
    env.update(ANTHROPIC_BASE_URL=BASE_URL,
               COCKPIT_BASE_URL=BASE_URL, COCKPIT_TOKEN_FILE=str(STORES / '.pool-client-token'),
               COCKPIT_PROJECT=Path.cwd().name, CLAUDE_CODE_GATEWAY_HINT_HEADERS="1")
    # Native inherits model choices from this environment when handing off or respawning.
    env.setdefault('ANTHROPIC_DEFAULT_OPUS_MODEL', 'claude-opus-5-5')
    # Native keeps signed prefixes stable only when discovery survives resume.
    env.setdefault('ENABLE_TOOL_SEARCH', 'force')
    env.setdefault('CLAUDE_CODE_INLINE_TOOLS', '1')
    env.setdefault('CLAUDE_CODE_FORCE_MID_CONVERSATION_SYSTEM', '1')
    for i, arg in enumerate(options):
        if arg == '--model' and i + 1 < len(options): model = options[i + 1]
        elif arg.startswith('--model='): model = arg.removeprefix('--model=')
        else: continue
        env['ANTHROPIC_MODEL'] = model
        if model.startswith('claude-opus-'): env['ANTHROPIC_DEFAULT_OPUS_MODEL'] = model
    for key in ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"]:
        env.pop(key, None)
    if background:
        claude_args = ['--plugin-dir', str(background_plugin(account)), *claude_args]
    else:
        if not any(arg in {'--safe-mode', '--bare'} for arg in options):
            claude_args = ['--plugin-dir', str(background_plugin(account, str(uuid.uuid4()))), *claude_args]
    if (RUNTIME / 'phone-config.json').exists() and not any(arg in {'--safe-mode', '--bare'} for arg in options):
        try:
            from control_main import start
            start()
            claude_args = ['--plugin-dir', str(configured_plugin('session-control')), *claude_args]
        except (OSError, RuntimeError, ValueError):
            print('Phone controls unavailable; this Claude task will run normally.', file=sys.stderr)
    if account and not background:
        session = str(uuid.uuid4())
        try: state = json.loads(request(client, 'POST', '/cockpit/' + session, {'action':'pin', 'account':account}))
        except ValueError: raise RuntimeError('Unable to read this session’s account pin; Claude was not started') from None
        if not isinstance(state, dict) or state.get('mode') != 'pinned' or state.get('pin') != account:
            raise RuntimeError('Unable to confirm this session’s account pin; Claude was not started')
        claude_args = ['--session-id', session, *claude_args]
    if not any(arg in {'--safe-mode', '--bare'} for arg in options):
        claude_args = ['--plugin-dir', str(configured_plugin('cockpit')), *claude_args]
    # The service manager owns recovery; a TUI never launches a second background watcher.
    raise SystemExit(subprocess.call([NATIVE_CLAUDE, *claude_args], env=env))


if __name__ == "__main__":
    try: main()
    except (OSError, RuntimeError, ValueError) as error: raise SystemExit(str(error))
