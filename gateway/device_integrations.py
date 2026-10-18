"""herdr machine profiles and the Claude desktop app environment for a device.

herdr: `herdr machine list --json` is a list of {id, label, target, session, enabled, selected}. A device counts as set up when
any profile (enabled or not, whoever named it) has `target` equal to the device's ssh target; existing profiles are never touched.
App (macOS): `~/Library/Application Support/Claude/ssh_configs.json` is {"configs": [{"name", "sshHost", "id"}], "trustedHosts": [...]}.
Only that file is read, never written. Its neighbours (tokens, remote-server state) are never opened. Newer app versions may
not keep the list there, so an absent or unreadable file means "cannot tell", never "not found"."""
import json
from pathlib import Path
import sys

import device_sync
import registry
from device_report import Step
from remote import Local, Remote

APP_DIR = Path.home() / 'Library/Application Support/Claude'
APP_FILE = 'ssh_configs.json'
HERDR_TIMEOUT = 120


def _add_clicks(ssh):
    return f'Code tab, environment menu, Add environment, SSH, choose {ssh}, pick the folder, trust it'


def APP_CLICKS_FOR(ssh):
    return 'In the Claude app: ' + _add_clicks(ssh) + '.'


def APP_LOOK_FOR(ssh):
    """For when the environment list cannot be read: look in the app, add only if absent."""
    return f'In the Claude app: Code tab, New: {ssh} should be listed under Recent environments; if not, add it ({_add_clicks(ssh)}).'


def _list(local):
    """Profile rows, or None when herdr is missing or cannot list."""
    done = local.call(['herdr', 'machine', 'list', '--json'], timeout=HERDR_TIMEOUT)
    if done.returncode: return None
    try: rows = json.loads(done.stdout)
    except ValueError: return None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else None


def herdr_profiles(local):
    return {row['label']: row for row in _list(local) or [] if isinstance(row.get('label'), str)}


def _is_self(device):
    return device.get('name') == registry.self_name()


def ensure_herdr(device, local, reachable):
    rows = _list(local)
    if rows is None: return Step('herdr', 'skipped', 'herdr is not available on this machine')
    ssh = device.get('ssh')
    if _is_self(device) or not ssh: return Step('herdr', 'skipped', 'this machine' if _is_self(device) else 'no ssh target')
    if any(row.get('target') == ssh for row in rows): return Step('herdr', 'ok', 'profile present')
    if not reachable(ssh): return Step('herdr', 'skipped', 'unreachable from this machine')
    argv = ['herdr', 'machine', 'add', ssh, '--label', device['name']]
    if device.get('herdrSession'): argv += ['--remote-session', device['herdrSession']]
    done = local.call(argv, timeout=HERDR_TIMEOUT)
    if done.returncode: return Step('herdr', 'failed', 'could not add the profile', 'herdr machine add ' + ssh + ' --label ' + device['name'])
    return Step('herdr', 'ok', 'profile added')


def check_herdr(device, local):
    """Like ensure_herdr, but only reads: whether a profile for the device's ssh target exists here."""
    rows = _list(local)
    if rows is None: return Step('herdr', 'skipped', 'herdr is not available on this machine')
    ssh = device.get('ssh')
    if _is_self(device) or not ssh: return Step('herdr', 'skipped', 'this machine' if _is_self(device) else 'no ssh target')
    if any(row.get('target') == ssh for row in rows): return Step('herdr', 'ok', 'profile present')
    return Step('herdr', 'missing', 'no herdr profile for ' + ssh, 'herdr machine add ' + ssh + ' --label ' + device['name'])


def remove_herdr(device, local):
    rows = _list(local)
    if rows is None: return Step('herdr', 'skipped', 'herdr is not available on this machine')
    match = next((row for row in rows if row.get('label') == device.get('name') and row.get('target') == device.get('ssh')), None)
    if match is None or not isinstance(match.get('id'), str): return Step('herdr', 'skipped', 'no profile created by claude-pool')
    done = local.call(['herdr', 'machine', 'remove', match['id']], timeout=HERDR_TIMEOUT)
    if done.returncode: return Step('herdr', 'failed', 'could not remove the profile')
    return Step('herdr', 'ok', 'profile removed')


def _app(app_dir):
    """(hosts, trusted) from the app's environment list, or None when it is absent, unparsable or not that shape:
    newer app versions may keep environments elsewhere, and no other file in that folder is ever opened."""
    try: value = json.loads((Path(app_dir) / APP_FILE).read_text())
    except (OSError, ValueError): return None
    if not isinstance(value, dict): return None
    configs = value.get('configs'); trusted = value.get('trustedHosts')
    if not isinstance(configs, list): return None
    hosts = {c['sshHost'] for c in configs if isinstance(c, dict) and isinstance(c.get('sshHost'), str)}
    return hosts, {t for t in trusted if isinstance(t, str)} if isinstance(trusted, list) else set()


def app_environments(app_dir):
    parsed = _app(app_dir)
    return None if parsed is None else parsed[0]


def _same_host(a, b): return a == b or a.rsplit('@', 1)[-1] == b.rsplit('@', 1)[-1]


def check_app(device, app_dir, platform):
    ssh = device.get('ssh')
    if platform != 'darwin' or not ssh: return Step('app', 'skipped', 'the Claude app environment is for macOS only')
    parsed = _app(app_dir)
    if parsed is None: return Step('app', 'manual', "could not read the Claude app's environment list", APP_LOOK_FOR(ssh))
    if not any(_same_host(ssh, h) for h in parsed[0]): return Step('app', 'manual', 'environment not found in the Claude app', APP_CLICKS_FOR(ssh))
    if not any(_same_host(ssh, h) for h in parsed[1]):
        return Step('app', 'manual', 'environment listed but not trusted', f'In the Claude app: open the {ssh} environment once and trust it.')
    return Step('app', 'ok', 'environment listed and trusted')


def apply_hook(value):
    local = Local()
    for device in value.get('devices', []):
        if _is_self(device) or not device.get('ssh'): continue
        try: ensure_herdr(device, local, lambda ssh: Remote(ssh).reachable())
        except Exception: pass


def _status():
    try: return {'herdrProfiles': sorted(herdr_profiles(Local()))}
    except Exception: return {'herdrProfiles': []}


device_sync.INTEGRATION_HOOKS.append(apply_hook)
device_sync.STATUS_PARTS.append(_status)
