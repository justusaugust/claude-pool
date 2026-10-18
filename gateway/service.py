"""Background service files: launchd on macOS, systemd --user on Linux.

The service definition is one dict in launchd vocabulary (Label, ProgramArguments,
WorkingDirectory, EnvironmentVariables, ...). macOS writes it as a plist exactly as
before; Linux renders the same dict as a user unit and parses it back for ownership
checks. No settings import: the installer uses this before any configuration exists.
"""
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile

DEFAULTS = {'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10, 'ExitTimeOut': 360,
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}
DESCRIPTION = 'Claude Pool background service '
DEFAULT_LABEL = 'io.github.claude-pool'


def receipt(app_root):
    """The installation receipt next to the gateway, {} when the checkout is not installed."""
    path = Path(app_root) / 'installation.json'
    return json.loads(path.read_text()) if path.is_file() else {}


def installed_label(value):
    label = value.get('serviceLabel', DEFAULT_LABEL)
    if not isinstance(label, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+', label): raise RuntimeError('Invalid installed service label')
    return label


def install_root(app_root):
    """The stable install root that holds run.py (the receipt's `root`), else the checkout itself."""
    value = receipt(app_root)
    return Path(value.get('root', app_root)) if value else Path(app_root)


def python():
    return '/opt/homebrew/bin/python3' if Path('/opt/homebrew/bin/python3').exists() else sys.executable


def platform():
    if sys.platform == 'darwin': return 'darwin'
    if sys.platform.startswith('linux'): return 'linux'
    raise RuntimeError('This alpha supports macOS and Linux only')


def service_file(label, home=None):
    home = Path(home) if home else Path.home()
    if platform() == 'darwin': return home / ('Library/LaunchAgents/' + label + '.plist')
    return home / ('.config/systemd/user/' + label + '.service')


def quote(value):
    """One systemd unit word; `%` is a specifier and must be doubled."""
    value = str(value).replace('%', '%%')
    if re.fullmatch(r'[A-Za-z0-9_./:=+@,%-]+', value): return value
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def exec_quote(value):
    """An ExecStart word: systemd expands $VAR even inside quotes, so `$` is doubled too."""
    return quote(str(value).replace('$', '$$'))


def directory(value):
    """WorkingDirectory is never quoted (systemd wants a bare absolute path); only `%` is doubled."""
    value = str(value)
    if not value.startswith('/') or any(c.isspace() and c != ' ' for c in value): raise ValueError('WorkingDirectory must be an absolute path without line breaks or tabs')
    return value.replace('%', '%%')


def words(line, exec=False):
    """Split a unit value into words; the inverse of quote() (exec: of exec_quote())."""
    result, current, i, quoted, started = [], '', 0, None, False
    undo = (lambda w: w.replace('%%', '%').replace('$$', '$')) if exec else (lambda w: w.replace('%%', '%'))
    while i < len(line):
        char = line[i]
        if quoted:
            if char == '\\' and i + 1 < len(line):
                i += 1; current += {'n': '\n', 't': '\t'}.get(line[i], line[i])
            elif char == quoted: quoted = None
            else: current += char
        elif char in '"\'': quoted = char; started = True
        elif char.isspace():
            if started: result.append(undo(current)); current, started = '', False
        else: current += char; started = True
        i += 1
    if quoted: raise ValueError('Unterminated quote in service unit')
    if started: result.append(undo(current))
    return result


def render(definition):
    if platform() == 'darwin': return plistlib.dumps(definition)
    lines = ['[Unit]', 'Description=' + DESCRIPTION + quote(definition['Label']), '',
             '[Service]', 'Type=simple', 'ExecStart=' + ' '.join(exec_quote(a) for a in definition['ProgramArguments'])]
    if 'WorkingDirectory' in definition: lines.append('WorkingDirectory=' + directory(definition['WorkingDirectory']))
    for key, value in definition.get('EnvironmentVariables', {}).items(): lines.append('Environment=' + quote(key + '=' + value))
    if definition.get('KeepAlive') == {'SuccessfulExit': False}: lines.append('Restart=on-failure')
    if 'ThrottleInterval' in definition: lines.append('RestartSec=' + str(int(definition['ThrottleInterval'])))
    if 'ExitTimeOut' in definition: lines.append('TimeoutStopSec=' + str(int(definition['ExitTimeOut'])))
    # SIGTERM reaches only the daemon; it drains the bridge and stops the backend itself.
    lines += ['KillMode=mixed', 'StandardOutput=null', 'StandardError=null']
    if definition.get('RunAtLoad'): lines += ['', '[Install]', 'WantedBy=default.target']
    return ('\n'.join(lines) + '\n').encode()


def load(data):
    if platform() == 'darwin': return plistlib.loads(data)
    definition, section = {}, None
    for raw in data.decode().splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')): continue
        if line.startswith('['): section = line; continue
        key, _, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if section == '[Unit]' and key == 'Description' and value.startswith(DESCRIPTION):
            definition['Label'] = ' '.join(words(value[len(DESCRIPTION):]))
        elif section == '[Service]':
            if key == 'ExecStart': definition['ProgramArguments'] = words(value, exec=True)
            elif key == 'WorkingDirectory': definition['WorkingDirectory'] = value.replace('%%', '%')
            elif key == 'Environment':
                for word in words(value):
                    name, _, content = word.partition('=')
                    definition.setdefault('EnvironmentVariables', {})[name] = content
            elif key == 'Restart' and value == 'on-failure': definition['KeepAlive'] = {'SuccessfulExit': False}
            elif key == 'RestartSec': definition['ThrottleInterval'] = int(value)
            elif key == 'TimeoutStopSec': definition['ExitTimeOut'] = int(value)
            elif key == 'StandardOutput' and value == 'null': definition['StandardOutPath'] = '/dev/null'
            elif key == 'StandardError' and value == 'null': definition['StandardErrorPath'] = '/dev/null'
        elif section == '[Install]' and key == 'WantedBy' and value == 'default.target': definition['RunAtLoad'] = True
    return definition


def loaded(label, run=subprocess.run):
    """True while the service is registered with the user's service manager."""
    if platform() == 'darwin':
        return run(['launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + label], capture_output=True).returncode == 0
    return any(run(['systemctl', '--user', check, '--quiet', label + '.service'], capture_output=True).returncode == 0
               for check in ['is-active', 'is-enabled'])


def launchd_job(text):
    """The job as launchd holds it, from `launchctl print` output: {'running': bool, 'arguments': [...] or None}.
    Only top-level lines count (nested blocks such as endpoints have their own `state`); an unknown format gives None
    arguments, which never triggers a reload."""
    running, arguments, block = False, None, None
    for line in text.splitlines():
        if block is not None:
            if line == '\t}': arguments, block = block, None
            elif line.startswith('\t\t'): block.append(line[2:])
            else: block = None
        elif line == '\tstate = running': running = True
        elif line == '\targuments = {': block = []
    return {'running': running, 'arguments': arguments}


def needs_reload(label, run=subprocess.run):
    """True when the user manager holds an older copy of this unit than the file."""
    result = run(['systemctl', '--user', 'show', '-p', 'NeedDaemonReload', '--value', label + '.service'], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == 'yes'


def reload(run=subprocess.run):
    """Linux only: forget a removed unit. launchd needs nothing after bootout."""
    if platform() == 'linux': run(['systemctl', '--user', 'daemon-reload'], capture_output=True)


def write(path, data):
    """Atomic service file, world-readable like any unit, independent of the umask; nothing secret goes in."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(data); file.flush(); os.fsync(file.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def unload_hint(label, system=None):
    """The command that unloads the service; `system` ('darwin' or 'linux') names another machine's platform."""
    if (system or platform()) == 'darwin': return 'launchctl bootout gui/$(id -u)/' + label
    return 'systemctl --user disable --now ' + label + '.service'
