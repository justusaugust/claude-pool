"""Non-secret account names, labels and shortcuts, read live by the UI."""
import json
from pathlib import Path
import re
import time

NATIVE_COMMANDS = {'agents', 'attach', 'auth', 'auto-mode', 'doctor', 'gateway', 'help', 'import',
                   'install', 'logs', 'mcp', 'plugin', 'plugins', 'purge', 'respawn', 'rm',
                   'setup-token', 'stop', 'kill', 'ultrareview', 'update', 'upgrade'}
RESERVED = NATIVE_COMMANDS | {'pool', 'exec', 'account', 'login', 'usage', 'history', 'forecast-check', 'phone', 'device'}
RESERVE_WINDOW = ('reserveHours', 'reserveTimezone', 'reserveSoft')


def reserve_hours_ok(value):
    """[start, end] in whole local hours, 0 <= start < end <= 24: the reserve holds while start <= hour < end."""
    return isinstance(value, list) and len(value) == 2 and all(type(hour) is int for hour in value) and 0 <= value[0] < value[1] <= 24


def time_zone_ok(value):
    """An IANA time zone name this machine can load."""
    if not isinstance(value, str) or not value: return False
    from zoneinfo import ZoneInfo  # not at import: the control hook loads this module on every turn
    try: ZoneInfo(value)
    except (KeyError, ValueError, OSError): return False
    return True


def check_reserve(row):
    """The reserve fields of a config.json or registry account row (both validate with this): `reserveAt` 0-99,
    `reserveHours` with its `reserveTimezone` (neither without the other), `reserveSoft` true or false."""
    if 'reserveAt' in row and (type(row['reserveAt']) is not int or not 0 <= row['reserveAt'] <= 99):
        raise ValueError('Account reserveAt must be an integer from 0 to 99')
    if ('reserveHours' in row) != ('reserveTimezone' in row): raise ValueError('Account reserveHours and reserveTimezone go together')
    if 'reserveHours' in row and not reserve_hours_ok(row['reserveHours']):
        raise ValueError('Account reserveHours must be [start, end] in whole hours with 0 <= start < end <= 24')
    if 'reserveTimezone' in row and not time_zone_ok(row['reserveTimezone']):
        raise ValueError('Account reserveTimezone must be an installed IANA time zone')
    if 'reserveSoft' in row and type(row['reserveSoft']) is not bool: raise ValueError('Account reserveSoft must be true or false')


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
        if not isinstance(row, dict) or set(row) - {'name', 'label', 'alias', 'plan', 'reserveAt', *RESERVE_WINDOW}: raise ValueError('Invalid account entry')
        name, alias = row.get('name'), row.get('alias', row.get('name'))
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9-]{1,80}', name) or name in names:
            raise ValueError('Account names must be unique lowercase names')
        if not isinstance(alias, str) or not re.fullmatch(r'[a-z0-9-]{1,80}', alias) or alias in aliases or alias in RESERVED:
            raise ValueError('Account shortcut is invalid, reserved or already used')
        for key in ('label', 'plan'):
            if key in row and (not isinstance(row[key], str) or not 1 <= len(row[key]) <= 80 or not row[key].isprintable()):
                raise ValueError('Account ' + key + ' must be printable text, 1–80 characters')
        check_reserve(row)
        names.add(name); aliases.add(alias)
    return value


def decorate(data, path, now=None):
    """Overview rows get the label, plan and reserve of their config row; a reserve window also its fields and the
    `reserveText` for `now` (epoch seconds) that status lines show (routing.reserve_text)."""
    rows = {r['name']: r for r in load(path).get('accounts', [])}
    for account in data.get('accounts', []):
        row = rows.get(account.get('name'), {})
        for key in ('label', 'plan'):
            if key in row: account[key] = row[key]
        if row.get('reserveAt', 0) > 0: account['reserveAt'] = row['reserveAt']
        window = row.get('reserveAt', 0) > 0 and 'reserveHours' in row
        for key in (*RESERVE_WINDOW, 'reserveText'): account.pop(key, None)
        if window:
            from routing import reserve_text
            account.update({key: row[key] for key in RESERVE_WINDOW if key in row},
                           reserveText=reserve_text(row, time.time() if now is None else now))
    return data
