"""The usage owner keeps an account in the pool when Claude ends its login.

The usage owner serves every account from its own refreshable login, because only that login reads usage. Claude ends
such a login some weeks after the sign-in, and then the account was gone until someone signed in again. The long-lived
token of the same subscription lies on the owner the whole time (the long-lived folder). Once the login is known to be
dead, the owner serves that token: the login moves to stores/.retired (never deleted) and a copy of the token becomes
the store file, which the backend reads with its next request. `claude-pool login <name>` takes the login back.

A login counts as dead when its access token ran out and was not renewed, which the usage poll does every few minutes
for a living login: after MARKED_MS when the backend itself marked the account, else after EXPIRED_MS when the
account's reading is as old and the reading of another login shows that the poll runs. Nothing is stored: which
accounts serve their token is read from the files. Never prints or returns a token."""
import fcntl
import os
from pathlib import Path
import time

import long_lived
from private_files import private_directory, read_private, write_private
import registry

MARKED_MS = 600_000      # the backend says "needs sign-in" and the access token ended this long ago
EXPIRED_MS = 3_600_000   # no verdict from the backend: the access token ended this long ago and was not renewed
FRESH_MS = 900_000       # a reading this young shows that the usage poll reached an account


def _owner(value, self_name):
    try: owner = registry.owner(value)
    except (KeyError, TypeError, ValueError, AttributeError): return False
    return bool(self_name) and owner is not None and owner.get('name') == self_name


def _held(path):
    """(kind, value) of a regular credential file, or (None, None)."""
    try:
        if path.is_symlink() or not path.is_file(): return None, None
        value = read_private(path)
        return long_lived.kind(value), value
    except (OSError, ValueError, RuntimeError): return None, None


def _subscription(value): return value.get('shuntAccountUuid'), value.get('shuntOrganizationUuid')


def _names(folder):
    try: return sorted(path.stem for path in Path(folder).glob('*.json') if not path.stem.startswith('.') and registry.NAME.fullmatch(path.stem))
    except OSError: return []


def _login_with_token(name, stores, directory, now_ms):
    """The login this device serves for `name` when the long-lived folder holds a valid token of the same subscription."""
    kind, login = _held(Path(stores) / (name + '.json'))
    if kind != 'refreshable': return None
    kind, token = _held(Path(directory) / (name + '.json'))
    if kind != 'long-lived' or _subscription(token) != _subscription(login): return None
    left = long_lived.days_left(token, now_ms)
    return None if left is not None and left <= 0 else login


def possible(stores, directory, value, self_name, now_ms):
    """Accounts whose login could be replaced by their long-lived token here, should it end."""
    if not _owner(value, self_name): return []
    return [name for name in _names(stores) if _login_with_token(name, stores, directory, now_ms) is not None]


def active(stores, value, self_name):
    """Accounts the usage owner serves from a long-lived token: their login ended, or nobody ever signed in here."""
    if not _owner(value, self_name): return []
    return [name for name in _names(stores) if _held(Path(stores) / (name + '.json'))[0] == 'long-lived']


def _seen(row):
    seen = row.get('seenAt')
    return seen if type(seen) in (int, float) and seen > 0 else None


def due(rows, stores, directory, value, self_name, now_ms):
    """Names whose login is dead and whose long-lived token can serve instead. `rows` are the pool's account rows."""
    if not _owner(value, self_name): return []
    rows = [row for row in rows or [] if isinstance(row, dict) and isinstance(row.get('name'), str) and registry.NAME.fullmatch(row['name'])]
    logins = {row['name']: _login_with_token(row['name'], stores, directory, now_ms) for row in rows}
    def polled(row):  # the usage poll read this refreshable login a moment ago
        seen = _seen(row)
        return seen is not None and now_ms - seen <= FRESH_MS and _held(Path(stores) / (row['name'] + '.json'))[0] == 'refreshable'
    names = []
    for row in rows:
        login = logins[row['name']]
        ended = long_lived.expires_at_ms(login) if login else None
        if ended is None: continue
        gone = now_ms - ended
        if row.get('status') == 'reauthenticate':
            if gone >= MARKED_MS: names.append(row['name'])
        elif gone >= EXPIRED_MS:
            seen = _seen(row)
            stale = seen is None or now_ms - seen >= FRESH_MS
            if stale and any(polled(other) for other in rows if other is not row): names.append(row['name'])
    return sorted(names)


def _retire(store, folder):
    private_directory(folder)
    stamp, n = str(int(time.time())), 0
    while True:
        target = folder / (store.stem + '-' + stamp + ('-' + str(n) if n else '') + '.json')
        if not target.exists() and not target.is_symlink(): break
        n += 1
    os.replace(store, target)
    return target


def fall_back(name, stores, directory):
    """Serve `name` from its long-lived token. False while a sign-in for the account runs; raises when a file fails."""
    stores, directory = Path(stores), Path(directory)
    store, copy = stores / (name + '.json'), directory / (name + '.json')
    with (stores / ('.' + name + '.add.lock')).open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return False
        data = copy.read_bytes()
        wanted = long_lived.fingerprint(read_private(copy))
        target = _retire(store, stores / '.retired')
        try:
            write_private(store, data)
            if long_lived.fingerprint(read_private(store)) != wanted: raise RuntimeError('The copy of the long-lived token did not verify')
        except Exception:
            os.replace(target, store)
            raise
    return True


def run(rows, stores, directory, value, self_name, now_ms, record=None):
    """One round: every due account falls back; `record(name)` hears each. Never raises: the supervisor calls it."""
    done = []
    try: names = due(rows, stores, directory, value, self_name, now_ms)
    except Exception: return done
    for name in names:
        try:
            if not fall_back(name, stores, directory): continue
        except Exception: continue
        done.append(name)
        if record is not None:
            try: record(name)
            except Exception: pass
    return done
