"""herdr session servers that come back after a reboot (Linux devices).

herdr keeps each session's workspaces, cwd and every Claude pane's conversation id in ~/.config/herdr/sessions/<s>/session.json
and resumes the agents (`claude --resume <id>`) when its server starts again; something has to start the server at boot.
claude-pool writes one systemd user template unit, claude-pool-herdr@.service, and enables one instance per session. Enabling
never starts a unit next to a running server, so nothing running is interrupted: the unit takes over at the next boot.
`--adopt` moves a running server under its unit now, only while none of its agents is working or waiting, and only by the pid
that owns the session's socket. A unit claude-pool did not write (herdr-trial.service, a hand-made herdr@.service) is never
touched; a session it serves counts as served."""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import time

from device_report import Step
import device_sync
import registry
from remote import Local
import service

STEP = 'herdr-sessions'
TEMPLATE = 'claude-pool-herdr@.service'
PREFIX = 'claude-pool-herdr@'
MARK = '# Written by claude-pool (device herdr-units): one herdr session server per instance, started at boot.'
SESSION = registry.HERDR_SESSION
BUSY = ('working', 'blocked')
LINUX_ONLY = 'herdr sessions come back through systemd user units; Linux devices only'
VERB = 'claude-pool device herdr-units'
TIMEOUT = 15
STOP_WAIT, VERIFY_WAIT, POLL = 30, 180, 2
LISTENING = 0x10000  # __SO_ACCEPTCON in /proc/net/unix


def unit(name): return PREFIX + name + '.service'


def template(herdr, home):
    """The template unit; `%i` is the session name. systemd never expands $HOME in Environment=, so the path is written out."""
    path = 'PATH=' + str(home) + '/.local/bin:/usr/local/bin:/usr/bin:/bin'
    return '\n'.join([MARK, '[Unit]', 'Description=herdr session %i (claude-pool)', 'After=' + service.DEFAULT_LABEL + '.service', '',
                      '[Service]', 'ExecStart=' + service.exec_quote(herdr) + ' --session %i server', 'Environment=' + service.quote(path),
                      'Restart=on-failure', 'RestartSec=3', '', '[Install]', 'WantedBy=default.target']) + '\n'


def _json(data):
    try: return json.loads(data)
    except ValueError: return None


def conversations(path):
    """{agent name: conversation id} for every Claude pane saved in a session.json; {} when it cannot be read."""
    try: value = json.loads(Path(path).read_text())
    except (OSError, ValueError): return {}
    found = {}
    def walk(item):
        if isinstance(item, dict):
            ref = item.get('agent_session')
            if (item.get('managed_agent_kind') == 'claude' and isinstance(item.get('agent_name'), str) and isinstance(ref, dict)
                    and ref.get('kind') == 'id' and isinstance(ref.get('value'), str)):
                found[item['agent_name']] = ref['value']
            for child in item.values(): walk(child)
        elif isinstance(item, list):
            for child in item: walk(child)
    walk(value)
    return found


def _exec_words(text):
    """(argv, HERDR_SESSION) from a unit file's [Service] section."""
    argv, env, section = [], {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('['): section = line; continue
        if section != '[Service]' or '=' not in line: continue
        key, _, value = line.partition('=')
        try:
            if key.strip() == 'ExecStart': argv = service.words(value.strip().lstrip('-@:+!'), exec=True)
            elif key.strip() == 'Environment':
                for word in service.words(value.strip()):
                    name, _, content = word.partition('='); env[name] = content
        except ValueError: continue
    return argv, env.get('HERDR_SESSION')


def _serves(argv, env_session):
    """The session a herdr server command line serves, or None."""
    if not any(Path(word).name == 'herdr' for word in argv[:1]) or 'server' not in argv: return None
    for i, word in enumerate(argv):
        if word == '--session' and i + 1 < len(argv): return argv[i + 1]
        if word.startswith('--session='): return word.split('=', 1)[1]
    return env_session


class Host:
    """This machine: its herdr, systemd user manager, /proc and signals. Every part can be replaced in tests."""
    def __init__(self, home=None, local=None, proc='/proc', kill=os.kill, sleep=time.sleep, clock=time.monotonic):
        self.home = Path(home) if home else Path.home()
        self.local = local or Local()
        self.proc, self.kill, self.sleep, self.clock = Path(proc), kill, sleep, clock
        self.units = self.home / '.config/systemd/user'
        self.saved = self.home / '.config/herdr/sessions'
        self._herdr = None

    def herdr(self):
        """herdr's absolute path: ~/.local/bin first (not on PATH over non-interactive ssh), then PATH."""
        own = self.home / '.local/bin/herdr'
        if own.is_file() and os.access(own, os.X_OK): return str(own)
        found = shutil.which('herdr')
        return str(Path(found).absolute()) if found else None

    def ask(self, session, *args):
        """herdr's reply (its `result` when there is one) or None on any failure."""
        herdr = self._herdr or self.herdr()
        if herdr is None: return None
        done = self.local.call([herdr, *(['--session', session] if session else []), *args], timeout=TIMEOUT)
        if done.returncode: return None
        data = _json(done.stdout)
        if not isinstance(data, dict) or 'error' in data: return None
        return data['result'] if isinstance(data.get('result'), dict) else data

    def systemctl(self, *args): return self.local.call(['systemctl', '--user', *args], timeout=TIMEOUT).returncode

    def enabled(self, name): return self.systemctl('is-enabled', '--quiet', unit(name)) == 0
    def active(self, name): return self.systemctl('is-active', '--quiet', unit(name)) == 0

    def sessions(self):
        """{name: {'running', 'socket', 'file'}} for every session herdr saved (it has a session.json); None when herdr cannot list."""
        data = self.ask(None, 'session', 'list', '--json')
        rows = data.get('sessions') if isinstance(data, dict) else None
        if not isinstance(rows, list): return None
        found = {}
        for row in rows:
            name = row.get('name') if isinstance(row, dict) else None
            if not isinstance(name, str) or not SESSION.fullmatch(name): continue
            folder = row.get('session_dir')
            folder = Path(folder) if isinstance(folder, str) and os.path.isabs(folder) else self.saved / name
            if not (folder / 'session.json').is_file(): continue
            socket = row.get('socket_path')
            found[name] = {'running': row.get('running') is True, 'socket': socket if isinstance(socket, str) else None,
                           'file': folder / 'session.json'}
        for file in sorted(self.saved.glob('*/session.json')):
            name = file.parent.name
            if SESSION.fullmatch(name) and name not in found: found[name] = {'running': False, 'socket': None, 'file': file}
        return found

    def foreign(self):
        """{session: unit} for the sessions a unit claude-pool did not write serves (found by its ExecStart)."""
        served = {}
        for path in sorted(self.units.glob('*.service')):
            if path.name.startswith(PREFIX): continue
            try: argv, env = _exec_words(path.read_text())
            except (OSError, UnicodeDecodeError): continue
            session = _serves(argv, env)
            if session == '%i' and path.name.endswith('@.service'):
                stem = path.name[:-len('.service')]
                for link in sorted(self.units.glob('*.wants/' + stem + '*.service')):
                    instance = link.name[len(stem):-len('.service')]
                    if SESSION.fullmatch(instance): served.setdefault(instance, link.name)
            elif session and SESSION.fullmatch(session): served.setdefault(session, path.name)
        return served

    def template_state(self, herdr):
        """'current', 'stale' (ours, older), 'absent' or 'foreign' (a file claude-pool did not write)."""
        path = self.units / TEMPLATE
        try: text = path.read_text()
        except FileNotFoundError: return 'absent'
        except (OSError, UnicodeDecodeError): return 'foreign'
        if not text.startswith(MARK): return 'foreign'
        return 'current' if text == template(herdr, self.home) else 'stale'

    def write_template(self, herdr):
        self.units.mkdir(parents=True, exist_ok=True)
        service.write(self.units / TEMPLATE, template(herdr, self.home).encode())
        self.systemctl('daemon-reload')

    def agents(self, session):
        rows = (self.ask(session, 'agent', 'list') or {}).get('agents')
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else None

    def workspaces(self, session):
        rows = (self.ask(session, 'workspace', 'list') or {}).get('workspaces')
        if not isinstance(rows, list): return None
        return {row.get('workspace_id'): row.get('label') for row in rows if isinstance(row, dict)}

    def _parent(self, pid):
        try: return int((self.proc / str(pid) / 'stat').read_text().rpartition(')')[2].split()[1])
        except (OSError, ValueError, IndexError): return None

    def alive(self, pid):
        try: state = (self.proc / str(pid) / 'stat').read_text().rpartition(')')[2].split()[0]
        except (OSError, IndexError): return False
        return state not in ('Z', 'X')

    def socket_owner(self, path):
        """The pid of the process that listens on the unix socket `path` (the session's server), or None when that is not
        exactly one process. Processes that inherited the socket from it (its children) do not count."""
        try: lines = (self.proc / 'net/unix').read_text().splitlines()[1:]
        except OSError: return None
        inodes = set()
        for line in lines:
            parts = line.split(None, 7)
            try:
                if len(parts) == 8 and parts[7] == path and int(parts[3], 16) & LISTENING: inodes.add('socket:[' + parts[6] + ']')
            except ValueError: continue
        if not inodes: return None
        holders = set()
        for entry in self.proc.iterdir():
            if not entry.name.isdigit(): continue
            try: links = {os.readlink(fd) for fd in (entry / 'fd').iterdir()}
            except OSError: continue
            if links & inodes: holders.add(int(entry.name))
        roots = [pid for pid in holders if self._parent(pid) not in holders]
        return roots[0] if len(roots) == 1 else None

    def is_herdr(self, pid):
        try: first = (self.proc / str(pid) / 'cmdline').read_bytes().split(b'\0')[0]
        except OSError: return False
        return Path(first.decode('utf-8', 'replace')).name == 'herdr'

    def wait(self, done, seconds):
        start = self.clock()
        while True:
            result = done()
            if not result or self.clock() - start >= seconds: return result
            self.sleep(POLL)


def listed():
    """The herdr sessions this device's registry row lists (device add --herdr-session)."""
    try: row = registry.device(registry.load(), registry.self_name()) or {}
    except Exception: return []
    return [name for name in row.get('herdrSessions') or [] if isinstance(name, str)]


def _names(rows): return {row.get(key) for row in rows for key in ('name', 'agent') if isinstance(row.get(key), str)}


def adopt_one(host, name, info):
    """Stop the session's own server (by the pid that owns its socket) and start its unit, then compare what came back."""
    again = VERB + ' --adopt --session ' + name
    def refuse(why, when='once that is fixed'): return Step(name, 'manual', 'enabled for the next boot; not adopted now: ' + why, 'rerun ' + again + ' ' + when)
    agents = host.agents(name)
    if agents is None: return refuse('its agents could not be read')
    busy = [str(row.get('name') or row.get('pane_id')) + ' is ' + row['agent_status'] for row in agents if row.get('agent_status') in BUSY]
    if busy: return refuse(', '.join(busy), 'when its agents are idle')
    pid = host.socket_owner(info['socket']) if info['socket'] else None
    if pid is None: return refuse('no single process owns its socket')
    if not host.is_herdr(pid): return refuse('its socket belongs to pid ' + str(pid) + ', which is not herdr')
    workspaces = host.workspaces(name)
    if workspaces is None: return refuse('its workspaces could not be read')
    wanted = conversations(info['file'])
    host.kill(pid, signal.SIGTERM)
    if host.wait(lambda: host.alive(pid), STOP_WAIT):
        return Step(name, 'failed', 'its server (pid ' + str(pid) + ') did not stop within ' + str(STOP_WAIT) + ' s; its unit was not started',
                    'herdr --session ' + name + ' server stop, then ' + again)
    if host.systemctl('start', unit(name)):
        return Step(name, 'failed', 'its server stopped, but its unit did not start', 'systemctl --user start ' + unit(name))
    def problems():
        found = []
        if host.workspaces(name) != workspaces: found.append('its workspaces differ from before')
        now, back = conversations(info['file']), _names(host.agents(name) or [])
        lost = [agent for agent, conversation in sorted(wanted.items()) if now.get(agent) != conversation or agent not in back]
        if lost: found.append(', '.join(lost) + ' did not come back with the same conversation')
        return found
    left = host.wait(problems, VERIFY_WAIT)
    if left:
        return Step(name, 'failed', 'its unit runs, but after ' + str(VERIFY_WAIT) + ' s ' + '; '.join(left),
                    'attach once with herdr --session ' + name + ' (herdr resumes agents when a client attaches), then claude-pool device check')
    return Step(name, 'ok', 'adopted by ' + unit(name) + '; ' + str(len(workspaces)) + ' workspaces and ' + str(len(wanted)) + ' Claude agents came back')


def apply(host, registered, adopt=False, only=None):
    """`device herdr-units`: one Step per session it looks after (or one Step when it can do nothing here): every saved session
    that runs or that the registry lists for this device. `only` limits it to those sessions (they need not run or be listed)."""
    if service.platform() != 'linux': return [Step(STEP, 'skipped', LINUX_ONLY)]
    herdr = host.herdr()
    if herdr is None: return [Step(STEP, 'skipped', 'herdr is not installed on this machine')]
    host._herdr = herdr
    found = host.sessions()
    if found is None: return [Step(STEP, 'failed', 'herdr could not list its sessions', herdr + ' session list --json')]
    foreign = host.foreign()
    scope = [n for n in sorted(found) if (found[n]['running'] or n in registered or n in (only or ())) and (only is None or n in only)]
    ours = [n for n in scope if n not in foreign]
    steps = {n: Step(n, 'ok', 'served by ' + foreign[n]) for n in scope if n in foreign}
    state = host.template_state(herdr) if ours else 'current'
    if state == 'foreign':
        return [Step(STEP, 'failed', str(host.units / TEMPLATE) + ' is a unit claude-pool did not write',
                     'move it aside, then run ' + VERB)] + [steps[n] for n in scope if n in steps]
    if state != 'current': host.write_template(herdr)
    for n in ours:
        running, active = found[n]['running'], host.active(n)
        if not host.enabled(n) and host.systemctl('enable', unit(n)):
            steps[n] = Step(n, 'failed', 'could not enable ' + unit(n), 'systemctl --user enable ' + unit(n)); continue
        if active: steps[n] = Step(n, 'ok', 'served by ' + unit(n))
        elif not running:
            steps[n] = (Step(n, 'ok', 'started by ' + unit(n)) if host.systemctl('start', unit(n)) == 0 else
                        Step(n, 'failed', 'enabled, but ' + unit(n) + ' did not start', 'systemctl --user status ' + unit(n)))
        elif adopt: steps[n] = adopt_one(host, n, found[n])
        else: steps[n] = Step(n, 'ok', 'enabled; the running server is left alone and ' + unit(n) + ' takes over at the next boot')
    return [steps[n] for n in scope]


def remove(host):
    """`device herdr-units --remove`: disable every claude-pool-herdr@ instance and delete the template if claude-pool wrote it.
    Running servers keep running until the next reboot; no other unit is touched."""
    if service.platform() != 'linux': return Step(STEP, 'skipped', LINUX_ONLY)
    instances = sorted({link.name[len(PREFIX):-len('.service')] for link in host.units.glob('*.wants/' + PREFIX + '*.service')})
    failed = [n for n in instances if SESSION.fullmatch(n) and host.systemctl('disable', unit(n))]
    if failed: return Step(STEP, 'failed', 'could not disable ' + ', '.join(unit(n) for n in failed), 'systemctl --user disable ' + unit(failed[0]))
    path = host.units / TEMPLATE
    try: ours = path.read_text().startswith(MARK)
    except (OSError, UnicodeDecodeError): ours = False
    if ours: path.unlink()
    host.systemctl('daemon-reload')
    return Step(STEP, 'ok', ('units removed for ' + ', '.join(instances) if instances else 'no units to remove') +
                '; running herdr sessions keep running until the next reboot')


def report(host=None):
    """The non-secret facts `device check` decides on, from this device: every saved session, whether it runs, and what serves it."""
    if service.platform() != 'linux': return {'available': False, 'reason': LINUX_ONLY}
    host = host or Host()
    herdr = host.herdr()
    if herdr is None: return {'available': False, 'reason': 'herdr is not installed'}
    host._herdr = herdr
    found = host.sessions()
    if found is None: return {'available': True, 'sessions': None}
    foreign, rows = host.foreign(), []
    for name, info in sorted(found.items()):
        row = {'name': name, 'running': info['running']}
        if name in foreign: row['unit'] = foreign[name]
        else: row.update(enabled=host.enabled(name), active=host.active(name))
        rows.append(row)
    return {'available': True, 'sessions': rows, 'template': host.template_state(herdr)}


def check(part, registered, fix):
    """ok when every saved session that runs or the registry lists has its unit enabled (or a foreign unit serves it)."""
    if not isinstance(part, dict): return Step(STEP, 'failed', 'could not read the herdr sessions on the device', fix)
    if not part.get('available'): return Step(STEP, 'skipped', str(part.get('reason') or 'herdr is not installed'))
    rows = part.get('sessions')
    if not isinstance(rows, list): return Step(STEP, 'failed', 'herdr could not list its sessions there', fix)
    rows = [row for row in rows if isinstance(row, dict) and isinstance(row.get('name'), str)]
    scope = [row for row in rows if row.get('running') or row['name'] in registered]
    if not scope: return Step(STEP, 'ok', 'no saved herdr session runs or is registered')
    if part.get('template') == 'foreign' and any(not row.get('unit') for row in scope):
        return Step(STEP, 'failed', TEMPLATE + ' there is a unit claude-pool did not write', 'move it aside, then: ' + fix)
    current = part.get('template') == 'current'
    missing = [row['name'] for row in scope if not row.get('unit') and not (row.get('enabled') and current)]
    if missing: return Step(STEP, 'missing', 'nothing brings ' + ', '.join(missing) + ' back after a reboot', fix)
    notes = [row['name'] + ' through ' + row['unit'] for row in scope if row.get('unit')]
    later = [row['name'] for row in scope if not row.get('unit') and row.get('running') and not row.get('active')]
    if later: notes.append('its unit takes over ' + ', '.join(later) + ' at the next boot')
    return Step(STEP, 'ok', str(len(scope)) + ' session' + ('' if len(scope) == 1 else 's') + ' come back after a reboot' +
                (' (' + '; '.join(notes) + ')' if notes else ''))


def status_part():
    try: return {'herdrUnits': report()}
    except Exception as error: return {'herdrUnits': {'available': True, 'sessions': None, 'error': type(error).__name__}}


device_sync.STATUS_PARTS.append(status_part)
