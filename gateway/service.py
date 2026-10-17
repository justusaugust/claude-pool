"""Background service files: launchd on macOS, systemd --user on Linux.

The service definition is one dict in launchd vocabulary (Label, ProgramArguments,
WorkingDirectory, EnvironmentVariables, ...). macOS writes it as a plist exactly as
before; Linux renders the same dict as a user unit and parses it back for ownership
checks. No settings import: the installer uses this before any configuration exists.
"""
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

DEFAULTS = {'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10, 'ExitTimeOut': 360,
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}
DESCRIPTION = 'Claude Pool background service '


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


def words(line):
    """Split a unit value into words; the inverse of quote()."""
    result, current, i, quoted, started = [], '', 0, None, False
    while i < len(line):
        char = line[i]
        if quoted:
            if char == '\\' and i + 1 < len(line):
                i += 1; current += {'n': '\n', 't': '\t'}.get(line[i], line[i])
            elif char == quoted: quoted = None
            else: current += char
        elif char in '"\'': quoted = char; started = True
        elif char.isspace():
            if started: result.append(current.replace('%%', '%')); current, started = '', False
        else: current += char; started = True
        i += 1
    if quoted: raise ValueError('Unterminated quote in service unit')
    if started: result.append(current.replace('%%', '%'))
    return result


def render(definition):
    if platform() == 'darwin': return plistlib.dumps(definition)
    lines = ['[Unit]', 'Description=' + DESCRIPTION + quote(definition['Label']), '',
             '[Service]', 'Type=simple', 'ExecStart=' + ' '.join(quote(a) for a in definition['ProgramArguments'])]
    if 'WorkingDirectory' in definition: lines.append('WorkingDirectory=' + quote(definition['WorkingDirectory']))
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
            if key == 'ExecStart': definition['ProgramArguments'] = words(value)
            elif key == 'WorkingDirectory': definition['WorkingDirectory'] = ' '.join(words(value))
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


def unload_hint(label):
    if platform() == 'darwin': return 'launchctl bootout gui/$(id -u)/' + label
    return 'systemctl --user disable --now ' + label + '.service'
