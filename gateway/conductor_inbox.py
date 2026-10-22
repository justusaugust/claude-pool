"""The Conductor's inbox: wakers queue events here, and a hook in the Conductor's own session delivers them.

Conductor Claude runs as a Native Remote Control conversation outside herdr, so `conductor tell` cannot reach it. Instead
`install` puts two hooks into the Conductor folder's local settings: SessionStart and Stop start `conductor inbox wait` with
`asyncRewake`. The waiter runs in the background, and when events are queued it prints them as one bundle and exits 2, which
wakes the model (a turn in progress finishes first). Every Stop starts a new waiter; only the newest one delivers. The
Conductor service marks its child with MARK, so other sessions in the same folder (board agents) end their waiter at once.
`waker_until` runs the Conductor's event watcher in the service and queues what it prints, once `set_waker(True)` was called.
State: one 0600 JSON file next to the other pool state; event text only, never secrets."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from private_files import private_directory, read_private, write_private
import settings

MARK = 'CLAUDE_POOL_CONDUCTOR'    # set only in the Conductor service's child, so only the Conductor's waiter takes events
LINE_LIMIT = 1500                 # characters per event; the bundle stays a short reminder
KEY_WINDOW = 600                  # seconds in which the same key wakes only once
KEEP = 200                        # delivered events kept for `status`
WAIT_TIMEOUT = 7 * 86400          # hook timeout in seconds: a waiter outlives a quiet week
WAKER_IDLE, WAKER_BACKOFF, WAKER_PAUSE = 30, 60, 1
STATE_LIMIT = 1 << 20


def _path():
    return settings.RUNTIME / 'conductor-inbox.json'


@contextlib.contextmanager
def _locked():
    path = _path()
    private_directory(path.parent)
    fd = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX); yield
    finally: os.close(fd)


def _read():
    try: value = read_private(_path(), STATE_LIMIT)
    except FileNotFoundError: value = {}
    value.setdefault('items', []); value.setdefault('keys', {})
    for key in ('seq', 'delivered', 'generation'): value.setdefault(key, 0)
    return value


def _write(value):
    value['items'] = [i for i in value['items'] if i['seq'] > value['delivered']] + \
                     [i for i in value['items'] if i['seq'] <= value['delivered']][-KEEP:]
    value['items'].sort(key=lambda i: i['seq'])
    write_private(_path(), json.dumps(value, ensure_ascii=False, sort_keys=True))


def _pending(value):
    return [i for i in value['items'] if i['seq'] > value['delivered']]


def add(text, key=None, now=None):
    """Queue one event (one line, bounded). The same `key` within KEY_WINDOW seconds is dropped. True when queued."""
    text, now = ' '.join(str(text).split())[:LINE_LIMIT], time.time() if now is None else now
    if not text: return False
    with _locked():
        value = _read()
        if key and now - value['keys'].get(key, -KEY_WINDOW) < KEY_WINDOW: return False
        if key: value['keys'] = {k: at for k, at in value['keys'].items() if now - at < KEY_WINDOW} | {key: now}
        value['seq'] += 1
        value['items'].append({'seq': value['seq'], 'at': now, 'text': text, **({'key': key} if key else {})})
        _write(value)
    return True


def status():
    value = _read()
    return {'pending': len(_pending(value)), 'generation': value['generation'], 'waker': bool(value.get('waker')),
            'delivered': value['delivered'], 'last': [i['text'][:200] for i in value['items'][-5:]]}


def set_waker(on):
    with _locked():
        value = _read(); value['waker'] = bool(on); _write(value)


def _bundle(items):
    head = 'Conductor-Eingang: ' + (str(len(items)) + ' Ereignisse' if len(items) > 1 else 'ein Ereignis') + \
           ' (claude-pool conductor inbox). Prüf sie wie eine Meldung deines Ereignis-Wächters:'
    return '\n'.join([head] + ['- ' + time.strftime('%H:%M', time.localtime(i['at'])) + ' ' + i['text'] for i in items])


def wait(environ=os.environ, parent=os.getppid, poll=1.0, settle=5.0, deadline=None, out=sys.stdout, err=sys.stderr,
         clock=time.monotonic, sleep=time.sleep):
    """The hook: 2 after printing queued events (wakes the model), 0 when it is not the Conductor, a newer waiter took over,
    its session ended (parent changed) or `deadline` seconds passed. `settle` gathers events that arrive close together."""
    if environ.get(MARK) != '1': return 0
    origin, start = parent(), clock()
    with _locked():
        value = _read(); value['generation'] += 1; mine = value['generation']; _write(value)
    found = None
    while True:
        if deadline is not None and clock() - start >= deadline: return 0
        if parent() != origin: return 0
        value = _read()
        if value['generation'] != mine: return 0
        if _pending(value):
            if found is None: found = clock()
            if clock() - found >= settle:
                with _locked():
                    value = _read()
                    if value['generation'] != mine or parent() != origin: return 0
                    items = _pending(value)
                    value['delivered'] = value['seq']; _write(value)
                print(_bundle(items), file=err, flush=True)
                return 2
        sleep(poll)


def install(home, entry):
    """Add the SessionStart and Stop waiters to `home`/.claude/settings.local.json; other settings and hooks stay. Idempotent."""
    home = Path(home).expanduser()
    path = home / '.claude' / 'settings.local.json'
    try: value = json.loads(path.read_text())
    except FileNotFoundError: value = {}
    if not isinstance(value, dict): raise RuntimeError('Not a settings object: ' + str(path))
    hooks = value.setdefault('hooks', {})
    ours = {'type': 'command', 'command': 'exec ' + str(entry) + ' conductor inbox wait', 'asyncRewake': True, 'timeout': WAIT_TIMEOUT}
    for event in ('SessionStart', 'Stop'):
        groups = []
        for group in hooks.get(event, []):
            kept = [h for h in group.get('hooks', []) if 'conductor inbox wait' not in str(h.get('command', ''))]
            if kept: groups.append({**group, 'hooks': kept})
        hooks[event] = groups + [{'hooks': [dict(ours)]}]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n'); os.replace(temporary, path)
    exclude = home / '.git' / 'info' / 'exclude'
    if exclude.parent.is_dir():
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        if '.claude/settings.local.json' not in lines: exclude.write_text('\n'.join(lines + ['.claude/settings.local.json']) + '\n')
    return path


def default_waker():
    command = Path(settings.CONDUCTOR_HOME).expanduser() / 'bin' / 'conductor-events'
    return [str(command), '--bis-wichtig'] if command.is_file() else None


def waker_until(stop, command=None, sleep=None):
    """Service thread: while switched on, run the event watcher (it ends after an important bundle) and queue each line it
    prints. A watcher that ends silently (another instance holds its lock) waits WAKER_BACKOFF seconds."""
    sleep = sleep or stop.wait
    while not stop.is_set():
        argv = command or default_waker()
        if not _read().get('waker') or not argv:
            sleep(WAKER_IDLE); continue
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                     cwd=str(Path(argv[0]).parent))
        except OSError:
            sleep(WAKER_BACKOFF); continue
        output = ''
        while True:
            try:
                output = child.communicate(timeout=1)[0] or ''; break
            except subprocess.TimeoutExpired:
                if stop.is_set():
                    child.terminate()
                    try: child.wait(5)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()
                    return
        lines = [line for line in output.splitlines() if line.strip()]
        for line in lines: add(line)
        sleep(WAKER_PAUSE if lines else WAKER_BACKOFF)
