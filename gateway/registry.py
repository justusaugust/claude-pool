"""Non-secret, generation-numbered device registry (devices.json), copied between machines over SSH.

Machines merge it row by row, so a change made while a peer was unreachable is never undone by the peer's later changes:
every device and account row carries the `generation` it last changed in, a removal leaves a tombstone
(`removed`: kind, name, generation and, for an account, the token fingerprint), the newer of row and tombstone wins
(the row on a tie), and ties between two rows go to the larger digest. The merge is commutative, associative and
idempotent, so every order of syncs between any number of devices ends with the same registry."""
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
HERDR_SESSION = re.compile(r'(?!\.{1,2}$)[A-Za-z0-9._-]{1,64}')
TOKEN_PREFIX = 'sk-' + 'ant-'
TOP = {'version', 'generation', 'updatedAt', 'devices', 'accounts', 'conductor', 'conductorGeneration', 'removed'}
DEVICE_KEYS = {'name', 'ssh', 'platform', 'roles', 'tailscaleIp', 'cua', 'cuaTunnels', 'herdrSession', 'herdrSessions', 'addedAt', 'generation'}
ACCOUNT_TEXT = ['label', 'alias', 'plan', 'email', 'organization', 'userId', 'organizationId', 'fingerprint']
ACCOUNT_KEYS = {'name', 'expiresAt', 'reserveAt', 'generation', *ACCOUNT_TEXT}
TOMBSTONE_KEYS = {'kind', 'name', 'generation', 'fingerprint'}
KINDS = {'device': 'devices', 'account': 'accounts'}
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
    sessions = row.get('herdrSessions', [])  # herdr sessions on this device that come back after a reboot (device_herdr)
    if (not isinstance(sessions, list) or any(not isinstance(s, str) or not HERDR_SESSION.fullmatch(s) for s in sessions)
            or len(set(sessions)) != len(sessions)): raise ValueError('herdrSessions must be a list of distinct herdr session names')
    tunnels = row.get('cuaTunnels', {})  # peer device name -> loopback port of the SSH tunnel this device reaches its Cua host through
    if not isinstance(tunnels, dict) or any(not isinstance(peer, str) or not NAME.fullmatch(peer) or not is_int(port) or not 1024 <= port <= 65535
                                            for peer, port in tunnels.items()):
        raise ValueError('cuaTunnels must map device names to ports')
    if 'addedAt' in row and not is_int(row['addedAt']): raise ValueError('addedAt must be an integer')
    check_generation(row)


def check_generation(row):
    if 'generation' in row and not (is_int(row['generation']) and row['generation'] >= 0): raise ValueError('generation must be a non-negative integer')


def check_tombstone(row):
    keys(row, TOMBSTONE_KEYS, 'tombstone')
    if row.get('kind') not in KINDS: raise ValueError('Tombstone kind must be device or account')
    if not isinstance(row.get('name'), str) or not row['name']: raise ValueError('Tombstone needs a name')
    if not is_int(row.get('generation')) or row['generation'] < 0: raise ValueError('Tombstone generation must be a non-negative integer')
    optional_text(row, 'fingerprint')


def check_account(row):
    keys(row, ACCOUNT_KEYS, 'account')
    if not isinstance(row.get('name'), str) or not row['name']: raise ValueError('Account needs a name')
    for key in ACCOUNT_TEXT: optional_text(row, key)
    if row.get('expiresAt') is not None and not is_int(row['expiresAt']): raise ValueError('expiresAt must be an integer or null')
    if 'reserveAt' in row and not (is_int(row['reserveAt']) and 0 <= row['reserveAt'] <= 99): raise ValueError('reserveAt must be an integer from 0 to 99')
    check_generation(row)


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
    if not isinstance(value.get('removed', []), list): raise ValueError('removed must be a list')
    for row in value.get('removed', []): check_tombstone(row)
    if len({(row['kind'], row['name']) for row in value.get('removed', [])}) != len(value.get('removed', [])): raise ValueError('Duplicate tombstone')
    if 'conductorGeneration' in value and not (is_int(value['conductorGeneration']) and value['conductorGeneration'] >= 0):
        raise ValueError('conductorGeneration must be a non-negative integer')
    entry = value.get('conductor')
    if entry is not None:
        keys(entry, CONDUCTOR_KEYS, 'conductor')
        if set(entry) != CONDUCTOR_KEYS or any(not isinstance(item, str) for item in entry.values()): raise ValueError('conductor needs device, email, organization and cwd as text')
    no_tokens(value)
    return value


def strict(value):
    """What every local change must keep: one usage owner and the Conductor on a registered device. A merged registry can
    break both for a while (two devices changed them while apart); owner() and conductor() resolve that the same way on
    every device, and the next local change writes the resolution (resolve)."""
    if sum('usage' in row.get('roles', []) for row in value['devices']) > 1: raise ValueError('Only one device can be the usage owner')
    entry = value.get('conductor')
    if entry is not None and entry['device'] not in {row['name'] for row in value['devices']}:
        raise ValueError('conductor.device must be a registered device')
    return value


def resolve(value):
    """Write the merged resolution into `value` before a local change: only the winning owner keeps the role, and a
    Conductor entry whose device is gone is dropped."""
    keep = owner(value)
    for row in value['devices']:
        if row is not keep and 'usage' in row.get('roles', []): row['roles'] = [r for r in row['roles'] if r != 'usage']
    if value.get('conductor') is not None and conductor(value) is None: value['conductor'] = None
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


def gen(row): return row.get('generation', 0) if row else 0


def _without_generation(row): return {key: item for key, item in row.items() if key != 'generation'}


def canonical(value):
    """Rows sorted by name and tombstones by kind and name; the empty extras left out, so equal content has one digest."""
    out = copy.deepcopy(value)
    for field in KINDS.values(): out[field] = sorted(out[field], key=lambda row: row['name'])
    removed = sorted(out.pop('removed', []), key=lambda row: (row['kind'], row['name']))
    if removed: out['removed'] = removed
    if not out.get('conductorGeneration'): out.pop('conductorGeneration', None)
    return out


def stamp(before, after, generation):
    """Mark what `after` changed against `before` with `generation`: changed or new rows, a tombstone per removed row
    (an account's carries its fingerprint), the Conductor entry. A row that comes back drops its tombstone."""
    tombs = {(row['kind'], row['name']): row for row in after.get('removed', [])}
    for kind, field in KINDS.items():
        old = {row['name']: row for row in before[field]}
        names = set()
        for row in after[field]:
            names.add(row['name'])
            if row['name'] not in old or _without_generation(row) != _without_generation(old[row['name']]):
                row['generation'] = generation
            tomb = tombs.get((kind, row['name']))
            if tomb is not None and tomb['generation'] <= gen(row): del tombs[(kind, row['name'])]
        for name, row in old.items():
            if name in names: continue
            tomb = {'kind': kind, 'name': name, 'generation': generation}
            if kind == 'account': tomb['fingerprint'] = row.get('fingerprint')
            tombs[(kind, name)] = tomb
    after['removed'] = list(tombs.values())
    if before.get('conductor') != after.get('conductor'): after['conductorGeneration'] = generation


def update(change, path=None):
    path = path or settings.REGISTRY
    with locked(path):
        before = load(path)
        value = resolve(copy.deepcopy(before))
        change(value)
        strict(validate(value))
        generation = before['generation'] + 1
        stamp(before, value, generation)
        value['generation'] = generation; value['updatedAt'] = int(time.time())
        value = canonical(validate(value))
        write(value, path)
        return value


def _key(row): return (gen(row), len(row.get('roles') or []), digest(row))


def _pick(rows):
    best = {}
    for row in rows:
        if row['name'] not in best or _key(row) > _key(best[row['name']]): best[row['name']] = row
    return best


def merge(local, incoming):
    """The join of two registries (see the module docstring). Never infers a removal from a missing row."""
    a, b = validate(copy.deepcopy(local)), validate(copy.deepcopy(incoming))
    tombs = {}
    for row in a.get('removed', []) + b.get('removed', []):
        key = (row['kind'], row['name'])
        if key not in tombs or (row['generation'], digest(row)) > (tombs[key]['generation'], digest(tombs[key])): tombs[key] = row
    out = {'version': 1, 'generation': max(a['generation'], b['generation']), 'updatedAt': max(a['updatedAt'], b['updatedAt'])}
    for kind, field in KINDS.items():
        rows = []
        for name, row in _pick(a[field] + b[field]).items():
            tomb = tombs.get((kind, name))
            if tomb is not None and tomb['generation'] > gen(row): continue  # removed after this row last changed
            tombs.pop((kind, name), None)  # the row is newer: it was added or changed again after the removal
            rows.append(row)
        out[field] = rows
    # Rows are never rewritten here: two devices that took the usage role while apart both keep it in their rows, and
    # owner() picks the newest move; a Conductor entry is kept even while its device is absent (conductor() hides it).
    # Rewriting either would lose information a third registry could still need, and the merge would stop being associative.
    entries = [(value.get('conductorGeneration', 0), value.get('conductor')) for value in (a, b)]
    generation, entry = max(entries, key=lambda item: (item[0], (item[1] or {}).get('device', ''), digest(item[1])))
    out['conductor'] = entry
    if generation: out['conductorGeneration'] = generation
    out['removed'] = list(tombs.values())
    return canonical(validate(out))


def receive(incoming, path=None):
    """Merge `incoming` into the registry at `path` under its lock; writes only when the merge changed it.
    Returns (previous, merged)."""
    path = path or settings.REGISTRY
    validate(copy.deepcopy(incoming))
    with locked(path):
        previous = load(path)
        merged = merge(previous, incoming)
        if merged != previous: write(merged, path)
        return previous, merged


def force(peers, path=None):
    """`device sync --force-registry`: this device's list wins on every device that has one of `peers`' registries.
    Every local row is stamped above every generation seen; a row only a peer has gets a tombstone (with the peer's
    fingerprint), so receivers retire only the exact token it names and never unlink it."""
    path = path or settings.REGISTRY
    with locked(path):
        value = resolve(copy.deepcopy(load(path)))
        generation = max([value['generation']] + [peer['generation'] for peer in peers]) + 1
        tombs = {(row['kind'], row['name']): row for row in value.get('removed', [])}
        for kind, field in KINDS.items():
            names = {row['name'] for row in value[field]}
            for row in value[field]:
                row['generation'] = generation; tombs.pop((kind, row['name']), None)
            for peer in peers:
                for row in peer[field]:
                    if row['name'] in names: continue
                    tomb = {'kind': kind, 'name': row['name'], 'generation': generation}
                    if kind == 'account': tomb['fingerprint'] = row.get('fingerprint')
                    tombs[(kind, row['name'])] = tomb
        value['removed'] = list(tombs.values())
        value['conductorGeneration'] = generation
        value['generation'] = generation; value['updatedAt'] = int(time.time())
        value = canonical(validate(value))
        write(value, path)
        return value


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


def owner(value):
    """The usage owner: the device with the role; when two took it while apart, the newest move (highest row key)."""
    return max((row for row in value['devices'] if 'usage' in row.get('roles', [])), key=_key, default=None)


def is_owner(value, row): return row is not None and (owner(value) or {}).get('name') == row['name']


def conductor(value):
    """The Conductor entry, or None while the device it names is not registered (removed while it was being moved)."""
    entry = value.get('conductor')
    return entry if entry is not None and device(value, entry['device']) is not None else None
def device(value, name): return next((row for row in value['devices'] if row['name'] == name), None)
def account(value, name): return next((row for row in value['accounts'] if row['name'] == name), None)


# One subscription, one identity: (userId, organizationId). Each device may hold it under its own local name (machines
# enrolled by hand named the same subscription differently), so whenever one side reasons about another side's accounts it
# matches by UUIDs and falls back to the name only when a side has no UUIDs.

def ids(row):
    """(userId, organizationId) of a registry row or a `device status` account row, or None when either is missing."""
    user, org = (row or {}).get('userId'), (row or {}).get('organizationId')
    return (user, org) if isinstance(user, str) and user and isinstance(org, str) and org else None


def same_subscription(a, b):
    first, second = ids(a), ids(b)
    if first and second: return first == second
    return a.get('name') == b.get('name')


def local_name(entry, rows):
    """The local name `rows` (status account rows: name, userId, organizationId, active) hold the subscription of `entry` (a
    registry row) under, or None. A served member wins over a copy that is not a member, then the registry's own name."""
    rows = list(rows.values()) if isinstance(rows, dict) else list(rows or [])
    found = [row for row in rows if isinstance(row, dict) and isinstance(row.get('name'), str) and same_subscription(entry, row)]
    if not found: return None
    return min(found, key=lambda row: (not row.get('active', True), row['name'] != entry.get('name'), row['name']))['name']


def entry_for(value, row):
    """The registry row of the subscription a local account `row` holds, or None (UUIDs first, the name only without them)."""
    matches = [entry for entry in value['accounts'] if same_subscription(entry, row)]
    return min(matches, key=lambda entry: (entry['name'] != row.get('name'), entry['name']), default=None)


def describe(entry, local):
    """`team (AI Værk Team, registry: ai-vaerk-team-premium)` when the local name differs from the registry name."""
    if not local or local == entry['name']: return entry['name']
    return local + ' (' + (entry.get('label') or entry['name']) + ', registry: ' + entry['name'] + ')'
