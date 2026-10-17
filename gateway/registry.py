"""Non-secret, generation-numbered device registry (devices.json), copied between machines over SSH."""
import copy
import fcntl
import hashlib
import json
import re
import time

from private_files import private_directory, read_private, write_private
import settings

LIMIT = 256 * 1024
NAME = re.compile(r'[a-z0-9-]{1,40}')
TOKEN_PREFIX = 'sk-' + 'ant-'
TOP = {'version', 'generation', 'updatedAt', 'devices', 'accounts', 'conductor'}
DEVICE_KEYS = {'name', 'ssh', 'platform', 'roles', 'tailscaleIp', 'cua', 'herdrSession', 'addedAt'}
ACCOUNT_TEXT = ['label', 'alias', 'plan', 'email', 'organization', 'userId', 'organizationId', 'fingerprint']
ACCOUNT_KEYS = {'name', 'expiresAt', 'reserveAt', *ACCOUNT_TEXT}
CONDUCTOR_KEYS = {'device', 'email', 'organization', 'cwd'}


def EMPTY():
    return {'version': 1, 'generation': 0, 'updatedAt': 0, 'devices': [], 'accounts': [], 'conductor': None}


def is_int(value): return type(value) is int


def optional_text(row, key):
    if row.get(key) is not None and not isinstance(row[key], str): raise ValueError(key + ' must be text or null')


def no_tokens(value):
    if isinstance(value, str):
        if TOKEN_PREFIX in value: raise ValueError('The registry never holds credentials')
    elif isinstance(value, dict):
        for key, item in value.items(): no_tokens(key); no_tokens(item)
    elif isinstance(value, list):
        for item in value: no_tokens(item)


def keys(row, allowed, what):
    if not isinstance(row, dict): raise ValueError(what + ' must be an object')
    if set(row) - allowed: raise ValueError('Unknown ' + what + ' field: ' + ', '.join(sorted(set(row) - allowed)))


def check_device(row):
    keys(row, DEVICE_KEYS, 'device')
    if not isinstance(row.get('name'), str) or not NAME.fullmatch(row['name']): raise ValueError('Device name must match [a-z0-9-]{1,40}')
    if row.get('platform') not in ('darwin', 'linux'): raise ValueError('Device platform must be darwin or linux')
    for key in ['ssh', 'tailscaleIp', 'herdrSession']: optional_text(row, key)
    roles = row.get('roles', [])
    if not isinstance(roles, list) or any(role != 'usage' for role in roles) or len(set(roles)) != len(roles): raise ValueError("Device roles may only be ['usage']")
    if not isinstance(row.get('cua', False), bool): raise ValueError('cua must be true or false')
    if 'addedAt' in row and not is_int(row['addedAt']): raise ValueError('addedAt must be an integer')


def check_account(row):
    keys(row, ACCOUNT_KEYS, 'account')
    if not isinstance(row.get('name'), str) or not row['name']: raise ValueError('Account needs a name')
    for key in ACCOUNT_TEXT: optional_text(row, key)
    if row.get('expiresAt') is not None and not is_int(row['expiresAt']): raise ValueError('expiresAt must be an integer or null')
    if 'reserveAt' in row and not (is_int(row['reserveAt']) and 0 <= row['reserveAt'] <= 99): raise ValueError('reserveAt must be an integer from 0 to 99')


def migrate(value):
    """A registry written before the rename names the Conductor `leitstand`; read it once as `conductor`."""
    if isinstance(value, dict) and 'leitstand' in value:
        old = value.pop('leitstand')
        if value.get('conductor') is None: value['conductor'] = old
    return value


def validate(value):
    migrate(value)
    keys(value, TOP, 'registry')
    if value.get('version') != 1: raise ValueError('Unsupported registry version')
    for key in ['generation', 'updatedAt']:
        if not is_int(value.get(key)) or value[key] < 0: raise ValueError(key + ' must be a non-negative integer')
    if not isinstance(value.get('devices'), list) or not isinstance(value.get('accounts'), list): raise ValueError('devices and accounts must be lists')
    for row in value['devices']: check_device(row)
    for row in value['accounts']: check_account(row)
    for rows, what in [(value['devices'], 'device'), (value['accounts'], 'account')]:
        names = [row['name'] for row in rows]
        if len(set(names)) != len(names): raise ValueError('Duplicate ' + what + ' name')
    if sum('usage' in row.get('roles', []) for row in value['devices']) > 1: raise ValueError('Only one device can be the usage owner')
    entry = value.get('conductor')
    if entry is not None:
        keys(entry, CONDUCTOR_KEYS, 'conductor')
        if set(entry) != CONDUCTOR_KEYS or any(not isinstance(item, str) for item in entry.values()): raise ValueError('conductor needs device, email, organization and cwd as text')
        if entry['device'] not in {row['name'] for row in value['devices']}: raise ValueError('conductor.device must be a registered device')
    no_tokens(value)
    return value


def load(path=None):
    path = path or settings.REGISTRY
    try: size = path.stat().st_size
    except FileNotFoundError: return EMPTY()
    except OSError as error: raise ValueError('Registry file is unreadable: ' + type(error).__name__) from None
    if size > LIMIT: raise ValueError('Registry is larger than 256 KiB')
    try: value = read_private(path, LIMIT)
    except (RuntimeError, OSError, ValueError) as error:
        raise ValueError('Registry file is unreadable (it must be your private 0600 JSON file): ' + type(error).__name__) from None
    return validate(value)


def write(value, path):
    data = json.dumps(validate(value), indent=2, sort_keys=True, ensure_ascii=False) + '\n'
    if len(data.encode()) > LIMIT: raise ValueError('Registry is larger than 256 KiB')
    write_private(path, data)


class locked:
    def __init__(self, path):
        self.path = path.parent / ('.' + path.name + '.lock')
        private_directory(path.parent)

    def __enter__(self):
        self.file = self.path.open('a'); fcntl.flock(self.file, fcntl.LOCK_EX)

    def __exit__(self, *exc): self.file.close()


def update(change, path=None):
    path = path or settings.REGISTRY
    with locked(path):
        value = copy.deepcopy(load(path))
        change(value)
        value['generation'] += 1; value['updatedAt'] = int(time.time())
        write(value, path)
        return value


def replace(value, path=None):
    path = path or settings.REGISTRY
    validate(value)
    with locked(path):
        if value['generation'] <= load(path)['generation']: return False
        write(value, path)
        return True


def merge(local, incoming):
    return copy.deepcopy(incoming if incoming['generation'] > local['generation'] else local)


def digest(value): return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()[:12]


def diverged(a, b):
    if a['generation'] != b['generation']: return None
    first, second = digest(a), digest(b)
    return None if first == second else (first, second)


def self_name(path=None):
    path = path or settings.SELF
    try: name = read_private(path, 4096).get('name')
    except (RuntimeError, OSError, ValueError): return None
    return name if isinstance(name, str) and NAME.fullmatch(name) else None


def set_self(name, path=None):
    path = path or settings.SELF
    if not isinstance(name, str) or not NAME.fullmatch(name): raise ValueError('Device name must match [a-z0-9-]{1,40}')
    write_private(path, json.dumps({'name': name}) + '\n')


def owner(value): return next((row for row in value['devices'] if 'usage' in row.get('roles', [])), None)
def device(value, name): return next((row for row in value['devices'] if row['name'] == name), None)
def account(value, name): return next((row for row in value['accounts'] if row['name'] == name), None)
