"""Non-secret installation settings. No filesystem writes on import."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get('CLAUDE_POOL_CONFIG', Path.home() / '.config/claude-pool/config.json')).expanduser().resolve()
RESERVED = {'usage', 'history', 'forecast-check', 'doctor', 'phone', 'respawn', 'login', 'help'}


def load(path=CONFIG):
    value = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict): raise ValueError('Pool configuration must be an object')
    allowed = {'accounts', 'native_claude', 'state_dir', 'credential_dir', 'gateway_port', 'backend_port', 'control_port', 'timezone'}
    if set(value) - allowed: raise ValueError('Unknown pool configuration field: ' + ', '.join(sorted(set(value) - allowed)))
    accounts, names, aliases = value.get('accounts', []), set(), set()
    if not isinstance(accounts, list): raise ValueError('accounts must be a list')
    for account in accounts:
        if not isinstance(account, dict) or set(account) - {'name', 'label', 'alias'}: raise ValueError('Invalid account configuration')
        name, alias = account.get('name'), account.get('alias', account.get('name'))
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9-]{1,80}', name) or name in names: raise ValueError('Account names must be unique lowercase names')
        if not isinstance(alias, str) or not re.fullmatch(r'[a-z0-9-]{1,80}', alias) or alias in RESERVED or alias in aliases: raise ValueError('Account aliases must be unique and cannot use a command name')
        label = account.get('label', name)
        if not isinstance(label, str) or not 1 <= len(label) <= 80 or not all(c.isprintable() for c in label): raise ValueError('Account label must be printable text, 1–80 characters')
        names.add(name); aliases.add(alias)
    ports = [value.get(key, default) for key, default in [('gateway_port', 18341), ('backend_port', 18340), ('control_port', 18342)]]
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports) or len(set(ports)) != 3: raise ValueError('Pool ports must be distinct integers from 1024 to 65535')
    for key in ['native_claude', 'state_dir', 'credential_dir', 'timezone']:
        if key in value and (not isinstance(value[key], str) or not value[key]): raise ValueError(key + ' must be a nonempty string')
    if value.get('timezone'):
        from zoneinfo import ZoneInfo
        try: ZoneInfo(value['timezone'])
        except (KeyError, ValueError): raise ValueError('timezone must be an installed IANA timezone') from None
    return value


def private_directory(path):
    if path.is_symlink(): raise RuntimeError('Private directory cannot be a symlink: ' + str(path))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Private directory must be owned by you with mode 0700: ' + str(path))


try: OPTIONS = load()
except (OSError, ValueError) as error: raise SystemExit('Invalid pool configuration: ' + str(error)) from None
RUNTIME = Path(OPTIONS.get('state_dir', Path.home() / '.local/state/claude-pool')).expanduser().resolve()
STORES = Path(OPTIONS.get('credential_dir', Path.home() / '.shunt/accounts/claude')).expanduser().resolve()
NATIVE_CLAUDE = str(Path(OPTIONS.get('native_claude', shutil.which('claude') or Path.home() / '.local/bin/claude')).expanduser().absolute())
GATEWAY_PORT = OPTIONS.get('gateway_port', 18341)
BACKEND_PORT = OPTIONS.get('backend_port', 18340)
CONTROL_PORT = OPTIONS.get('control_port', 18342)
BASE_URL = 'http://127.0.0.1:' + str(GATEWAY_PORT)
CONTROL_URL = 'http://127.0.0.1:' + str(CONTROL_PORT)
ACCOUNTS = OPTIONS.get('accounts', [])
LABELS = {a['name']: a.get('label', a['name']) for a in ACCOUNTS}
ALIASES = {a.get('alias', a['name']): a['name'] for a in ACCOUNTS}


def backend_config(state=RUNTIME):
    if not ACCOUNTS: raise RuntimeError('No accounts configured. Read docs/SETUP.md before starting the pool.')
    lines = [f'[server]\nbind = "127.0.0.1:{BACKEND_PORT}"\ndefault_provider = "anthropic"',
             '[server.auth]\ntokens_env = "POOL_CLIENT_TOKENS"',
             '[server.admin]\ntokens_env = "POOL_ADMIN_TOKENS"\nhide_observed = true',
             '[server.pool]\ndefault_threshold = 0.95\nusage_refresh_seconds = 300\nramp_initial_concurrency = 2\nstate_path = ' + json.dumps(str(state / 'quota.json')),
             '[providers.anthropic]\nkind = "anthropic"\nbase_url = "https://api.anthropic.com"\nauth = "claude_oauth"']
    lines.extend('[[providers.anthropic.accounts]]\nname = ' + json.dumps(a['name']) for a in ACCOUNTS)
    return '\n\n'.join(lines) + '\n'


def connection_ts():
    try: phone = json.loads((RUNTIME / 'phone-config.json').read_text())['host']
    except (OSError, ValueError, KeyError, TypeError): phone = None
    values = {'baseURL': BASE_URL, 'controlURL': CONTROL_URL, 'tokenFile': str(STORES / '.pool-client-token'), 'configFile': str(CONFIG),
              'python': sys.executable, 'suiteHelper': str(HERE / 'heavy_queue.py'),
              'hookPath': str(HERE / 'control_hook.py'), 'phoneURL': 'https://' + phone + '/' if phone else ''}
    return ''.join('export const ' + key + ' = ' + json.dumps(value) + '\n' for key, value in values.items())
