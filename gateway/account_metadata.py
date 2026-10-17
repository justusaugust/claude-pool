"""Non-secret account names, labels and shortcuts, read live by the UI."""
import json
from pathlib import Path
import re

NATIVE_COMMANDS = {'agents', 'attach', 'auth', 'auto-mode', 'doctor', 'gateway', 'help', 'import',
                   'install', 'logs', 'mcp', 'plugin', 'plugins', 'purge', 'respawn', 'rm',
                   'setup-token', 'stop', 'kill', 'ultrareview', 'update', 'upgrade'}
RESERVED = NATIVE_COMMANDS | {'pool', 'exec', 'account', 'login', 'usage', 'history', 'forecast-check', 'phone', 'device'}


def load(path):
    path = Path(path)
    if path.is_symlink(): raise ValueError('Account configuration cannot be a symlink')
    if not path.exists(): return {}
    if path.stat().st_size > 65536: raise ValueError('Account configuration is too large')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not isinstance(value.get('accounts', []), list):
        raise ValueError('Invalid account configuration')
    names, aliases = set(), set()
    for row in value.get('accounts', []):
        if not isinstance(row, dict) or set(row) - {'name', 'label', 'alias', 'plan'}: raise ValueError('Invalid account entry')
        name, alias = row.get('name'), row.get('alias', row.get('name'))
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9-]{1,80}', name) or name in names:
            raise ValueError('Account names must be unique lowercase names')
        if not isinstance(alias, str) or not re.fullmatch(r'[a-z0-9-]{1,80}', alias) or alias in aliases or alias in RESERVED:
            raise ValueError('Account shortcut is invalid, reserved or already used')
        for key in ('label', 'plan'):
            if key in row and (not isinstance(row[key], str) or not 1 <= len(row[key]) <= 80 or not row[key].isprintable()):
                raise ValueError('Account ' + key + ' must be printable text, 1–80 characters')
        names.add(name); aliases.add(alias)
    return value


def decorate(data, path):
    rows = {r['name']: r for r in load(path).get('accounts', [])}
    for account in data.get('accounts', []):
        row = rows.get(account.get('name'), {})
        for key in ('label', 'plan'):
            if key in row: account[key] = row[key]
    return data
