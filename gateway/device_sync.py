"""Move long-lived accounts and the registry between devices. Tokens travel only on ssh stdin or an ssh stdout pipe.
Receiving never stops or starts the service; only `switch` does, and only while nothing is in flight."""
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

import accounts_cli
import long_lived
from long_lived import fingerprint, kind
from private_files import private_directory, read_private, write_private
import registry
from remote import NO_LAUNCHER, OUTDATED, Remote, redact, require_pipe
import routing
import service
import settings

LIMIT = 131072
NAME = re.compile(r'[a-z0-9-]{1,80}')
STATUS_PARTS = []       # callables returning extra non-secret keys for `status()`
INTEGRATION_HOOKS = [accounts_cli.reserve_hook]  # callables run with the new registry after it replaced the local one
switch_pending = long_lived.switch_pending


class Unreachable(Exception): pass
class Failed(Exception): pass
class Outdated(Exception): pass  # the target's launcher has no `device` verb; it is never handed one
class Missing(Exception): pass   # no claude-pool launcher on the target


def check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name): raise ValueError('Invalid account name')
    return name


def replica(name): return settings.LONG_LIVED / (check_name(name) + '.json')


def is_owner(value):
    owner = registry.owner(value)
    return owner is not None and owner['name'] == registry.self_name()


def config_names():
    from account_metadata import load
    return [row['name'] for row in load(settings.CONFIG).get('accounts', [])]


def _receipt_label():
    """The `serviceLabel` of installation.json (the default when absent); None when it cannot be read or is not a valid label."""
    try: receipt = json.loads((settings.HERE.parent / 'installation.json').read_text())
    except FileNotFoundError: return service.DEFAULT_LABEL
    label = receipt.get('serviceLabel', service.DEFAULT_LABEL) if isinstance(receipt, dict) else None
    return label if isinstance(label, str) and re.fullmatch(r'[a-zA-Z0-9.-]+', label) else None


def service_label():
    """The installed pool service label another device disables at removal; the default label when the receipt names none."""
    try: return _receipt_label() or service.DEFAULT_LABEL
    except (OSError, ValueError): return service.DEFAULT_LABEL


def service_state():
    """'running', 'stopped' (installed but not loaded) or 'missing' (no service file)."""
    try:
        receipt = json.loads((settings.HERE.parent / 'installation.json').read_text())
        label = receipt.get('serviceLabel', 'io.github.claude-pool')
        if not isinstance(label, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+', label): return 'missing'
        if not service.service_file(label).exists() and not settings.RUNTIME.exists(): return 'missing'
        return 'running' if service.loaded(label) else 'stopped'
    except (OSError, ValueError, RuntimeError): return 'missing'


def pool_overview():
    token = (settings.STORES / '.pool-client-token').read_text().strip()
    request = urllib.request.Request(settings.BASE_URL + '/pool/overview', headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request, timeout=5) as response: return json.loads(response.read(4 * 1024 * 1024))


def local_overview(overview):
    """The local gateway's overview. A gateway that is not there (refused connection, no token file) serves nothing,
    so it reads as idle; any other failure propagates and counts as busy. Never asks the service manager."""
    def read():
        try: return (overview or pool_overview)()
        except (ConnectionRefusedError, FileNotFoundError): return {}
        except urllib.error.URLError as error:
            if isinstance(error.reason, ConnectionRefusedError): return {}
            raise
    return read


def default_native_identity():
    from launch import native_identity
    return native_identity()


def build_version():
    """The release's source digest (`sourceVersion`): equal on every device that runs the same sources, because the
    backend binary each platform builds itself is not part of it. None for a release built before it existed."""
    try: return json.loads((settings.HERE.parent / 'build-info.json').read_text()).get('sourceVersion')
    except (OSError, ValueError, AttributeError): return None


def account_rows():
    members, rows = set(config_names()), []
    names = set(members) | {p.stem for p in settings.LONG_LIVED.glob('*.json') if not p.stem.startswith('.')}
    # A long-lived store file with no membership and no replica is listed too (`stray`): it may be the only copy.
    strays = {p.stem for p in settings.STORES.glob('*.json') if not p.stem.startswith('.') and p.stem not in names
              and not p.is_symlink() and accounts_cli.stored_kind(p) == 'long-lived'}
    for name in sorted(names | strays):
        replica_path, store = settings.LONG_LIVED / (name + '.json'), settings.STORES / (name + '.json')
        have = accounts_cli.stored_kind(replica_path) == 'long-lived'
        path = replica_path if have else store
        try: value = read_private(path)
        except (OSError, ValueError, RuntimeError): continue
        try:
            row = {'name': name, 'kind': 'long-lived' if have else kind(value), 'fingerprint': fingerprint(value),
                   'expiresAt': long_lived.expires_at_ms(value), 'active': name in members, 'servingFingerprint': None}
            if name in strays: row['stray'] = True
            if name in members and store.exists() and not store.is_symlink():
                try: row['servingFingerprint'] = fingerprint(read_private(store))
                except (OSError, ValueError, RuntimeError): pass
        except RuntimeError: continue
        rows.append(row)
    return rows


def status():
    """Non-secret state of this device: fingerprints only, never a token."""
    known = registry.load()
    value = {'name': registry.self_name(), 'version': build_version(), 'platform': service.platform(), 'service': service_state(),
             'accounts': account_rows(), 'switchPending': switch_pending(settings.STORES, settings.LONG_LIVED, known, registry.self_name()),
             'registryGeneration': known['generation'], 'registryHash': registry.digest(known), 'serviceLabel': service_label()}
    for part in STATUS_PARTS: value.update(part())
    return value


def send_account(name, out=None):
    out = out or sys.stdout.buffer
    require_pipe(out)
    path = replica(name)
    if long_lived.kind(read_private(path)) != 'long-lived': raise RuntimeError('Only a long-lived token can be sent')
    out.write(path.read_bytes()); out.flush()


def receive_account(name, data, journal=None):
    path = replica(name)
    if not isinstance(data, bytes) or len(data) > LIMIT: raise ValueError('Account file is larger than 128 KiB')
    try: value = json.loads(data)
    except ValueError: raise RuntimeError('Unsupported credential format') from None
    if kind(value) != 'long-lived': raise RuntimeError('Only a long-lived token can be received')
    fp = fingerprint(value)
    known = registry.load()
    have = fingerprint(read_private(path)) if accounts_cli.stored_kind(path) == 'long-lived' else None
    want = (registry.account(known, name) or {}).get('fingerprint')
    # A replica that already is the token the registry names is never overwritten by an older one.
    if want and have == want and fp != want: return 'unchanged'
    same = have == fp
    if not same:
        # The replica it replaces may be the only copy of that token (enrolled here while another device enrolled a newer
        # one); it is moved aside, never overwritten.
        if path.exists() or path.is_symlink(): retire_store(path, settings.STORES / '.retired')
        write_private(path, data)
    held = 'unchanged' if same else 'staged'
    if is_owner(known) or registry.account(known, name) is None: return held
    store = settings.STORES / path.name
    if store.is_symlink(): raise RuntimeError('Credential file must not be a symlink: ' + store.name)
    if store.exists():
        if accounts_cli.stored_kind(store) != 'long-lived' or fingerprint(read_private(store)) != fp: return held
        if name in config_names(): return held
    else: write_private(store, data)
    row = registry.account(known, name) or {}
    entry = {'name': name, 'label': row.get('label') or name.replace('-', ' ').title(), 'alias': row.get('alias') or name}
    if row.get('plan'): entry['plan'] = row['plan']
    info = {'userId': value['shuntAccountUuid'], 'organizationId': value['shuntOrganizationUuid']}
    accounts_cli.register_local(entry, info)
    if (row.get('reserveAt') or 0) > 0:
        try:
            config, binary, paths, env = accounts_cli.local_context()
            accounts_cli.reserve(config, binary, paths, env, name, row['reserveAt'], registry_path=False)
        except Exception:
            # The install stands; the failure is recorded for diagnostics, never in the return value.
            if journal is not None:
                try: journal.record('reserve_not_applied', account=name)
                except Exception: pass
    return 'installed'


def local_fingerprint(name):
    for path in (settings.LONG_LIVED / (name + '.json'), settings.STORES / (name + '.json')):
        try: return fingerprint(read_private(path))
        except (OSError, ValueError, RuntimeError): continue
    return None


def long_lived_fingerprint(name):
    """The fingerprint of this device's long-lived copy of `name` (the synced replica first), or None when it holds none."""
    for path in (settings.LONG_LIVED / (name + '.json'), settings.STORES / (name + '.json')):
        if not path.is_symlink() and accounts_cli.stored_kind(path) == 'long-lived': return fingerprint(read_private(path))
    return None


class Busy(Exception): pass


def remove_local(name):
    """Drop `name` from this device. Every file is moved aside to stores/.retired, never unlinked: a removal that arrives
    from another device must never destroy a token this device might hold the only copy of."""
    private_directory(settings.STORES)
    with (settings.STORES / ('.' + name + '.add.lock')).open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise Busy() from None
        paths, env = accounts_cli.local_paths_env(settings.RUNTIME, settings.STORES)
        accounts_cli.unregister(settings.CONFIG, settings.STORES, settings.HERE / 'bin/shunt', paths, name, env)
        accounts_cli.retire(settings.STORES, settings.LONG_LIVED, name, keep_long_lived=True)


def receive_registry(data, overview=None, native_identity=None, elsewhere=()):
    """Merge another device's registry into this one (registry.merge: per row, tombstones, never a whole-list replace),
    then apply the removals the tombstones name. An account goes only when a tombstone names it and the long-lived
    token this device holds is the one the tombstone names (or `elsewhere`, the fingerprints the sending device holds,
    includes it); its files are moved aside, never unlinked. A removal that has to wait (busy) is retried on the next
    receive, because the tombstone stays."""
    if not isinstance(data, bytes) or len(data) > registry.LIMIT: raise ValueError('Registry is larger than 256 KiB')
    try: incoming = registry.validate(json.loads(data))
    except ValueError as error: raise ValueError('Invalid registry: ' + str(error)) from None
    previous, now = registry.receive(incoming)
    report = {'result': 'unchanged', 'removed': [], 'kept': [], 'kept_busy': [], 'kept_native': [], 'failed': {}, 'hooks': {}}
    if now != previous: report['result'] = 'replaced' if now == registry.canonical(incoming) else 'merged'
    listed = {row['name'] for row in now['accounts']}
    tombs = {row['name']: row for row in now.get('removed', []) if row['kind'] == 'account' and row['name'] not in listed}
    native = {'done': False}
    for name, tomb in sorted(tombs.items()):
        if not NAME.fullmatch(name): continue
        try:
            if name not in config_names() and not any((folder / (name + '.json')).exists() for folder in (settings.STORES, settings.LONG_LIVED)): continue
            held = long_lived_fingerprint(name)
            if held is not None and held != tomb.get('fingerprint') and held not in set(elsewhere):
                report['kept'].append(name)  # another token than the one removed, and maybe its only copy
                continue
            if not routing.wait_idle(local_overview(overview), name, False):
                report['kept_busy'].append(name); continue
            if not native['done']:
                native['value'] = (native_identity or default_native_identity)(); native['done'] = True
            row = next((r for r in previous['accounts'] if r['name'] == name), {})
            if not row.get('email') and accounts_cli.stored_kind(settings.STORES / (name + '.json')) == 'refreshable':
                raise RuntimeError('Cannot tell whether ' + name + ' is the Native login of this machine; it was not removed')
            if accounts_cli.matches_native(native['value'], name, {'accounts': [row]}, settings.STORES):
                report['kept_native'].append(name)
                report['failed'][name] = ((row.get('label') or name) + ' is the Native login of this machine and must stay connected. '
                                          'Sign Native in with another connected account first (claude-native auth login). Nothing was removed.')
                continue
            if name in config_names() and len(config_names()) == 1:
                report['failed'][name] = 'it is the last account on this device'; continue
            remove_local(name); report['removed'].append(name)
        except Busy: report['kept_busy'].append(name)
        except Exception as error:
            report['failed'][name] = type(error).__name__ + ': ' + str(error)
    if now != previous:
        for hook in list(INTEGRATION_HOOKS):
            label = getattr(hook, '__name__', repr(hook))
            try: hook(now); report['hooks'][label] = 'ok'
            except Exception as error: report['hooks'][label] = 'failed: ' + type(error).__name__
    return report


def secrets():
    tokens = []
    for folder in (settings.LONG_LIVED, settings.STORES):
        for path in folder.glob('*.json'):
            try: tokens.append(read_private(path)['claudeAiOauth']['accessToken'])
            except (OSError, ValueError, RuntimeError, KeyError, TypeError): pass
    return tokens


def peer(remote, *args, stdin=None):
    """`claude-pool device <args>` on another device, only through a launcher that has device support."""
    done = remote.device(*args, stdin=stdin)
    if done.returncode == 255: raise Unreachable()
    if done.returncode == OUTDATED: raise Outdated()
    if done.returncode == NO_LAUNCHER: raise Missing()
    if done.returncode: raise Failed('claude-pool device ' + ' '.join(args[0:1]) + ' exited ' + str(done.returncode))
    return done.stdout


def peer_status(remote):
    try: value = json.loads(peer(remote, 'status'))
    except ValueError: raise Failed('the other device sent an unreadable status') from None
    if not isinstance(value, dict): raise Failed('the other device sent an unreadable status')
    rows = value.get('accounts') if isinstance(value.get('accounts'), list) else []
    value['accounts'] = {row['name']: row for row in rows if isinstance(row, dict) and isinstance(row.get('name'), str)}
    return value


def guarded(work, target=''):
    try: work(); return 'ok'
    except Unreachable: return 'unreachable'
    except Outdated: return 'outdated: run claude-pool device add ' + target
    except Missing: return 'missing'
    except (Failed, ValueError, RuntimeError, OSError) as error:
        return 'failed: ' + redact(str(error), secrets())[:200]


def held_fingerprints(theirs):
    return {row.get('fingerprint') for row in theirs['accounts'].values() if row.get('kind') == 'long-lived' and row.get('fingerprint')}


def merge_from(remote, theirs):
    """Fetch the peer's registry and merge it here first, so what is pushed back already holds the peer's changes."""
    receive_registry(peer(remote, 'registry'), elsewhere=held_fingerprints(theirs))


def push(remote):
    def work():
        theirs = peer_status(remote)
        if theirs.get('registryHash') != registry.digest(registry.load()):
            merge_from(remote, theirs)
            known = registry.load()
            if theirs.get('registryHash') != registry.digest(known):
                peer(remote, 'receive-registry', stdin=json.dumps(known).encode())
        known = registry.load()
        for row in known['accounts']:
            path = settings.LONG_LIVED / (row['name'] + '.json')
            if accounts_cli.stored_kind(path) != 'long-lived': continue
            data = path.read_bytes()
            mine = fingerprint(json.loads(data))
            if row.get('fingerprint') and mine != row['fingerprint']: continue  # not the token the registry names; never push an older one
            if theirs['accounts'].get(row['name'], {}).get('fingerprint') == mine: continue
            peer(remote, 'receive-account', row['name'], stdin=data)
    return guarded(work, remote.name)


def pull(remote):
    def work():
        theirs = peer_status(remote)
        merge_from(remote, theirs)
        known = {row['name']: row for row in registry.load()['accounts']}
        for name, row in sorted(theirs['accounts'].items()):
            path = settings.LONG_LIVED / (name + '.json')
            if row.get('kind') != 'long-lived' or row.get('stray') or name not in known or not NAME.fullmatch(name): continue
            if known[name].get('fingerprint') and row.get('fingerprint') != known[name]['fingerprint']: continue
            if accounts_cli.stored_kind(path) == 'long-lived' and fingerprint(read_private(path)) == row.get('fingerprint'): continue
            receive_account(name, peer(remote, 'send-account', name))
    return guarded(work, remote.name)


def sync_one(remote):
    """Push, then pull: both registries end merged and each side holds the other's long-lived tokens."""
    pushed = push(remote)
    if pushed == 'unreachable' or pushed == 'missing' or pushed.startswith('outdated'): return pushed
    pulled = pull(remote)
    return pushed if pushed != 'ok' else pulled


def sync_all(remotes=Remote):
    known, me, results = registry.load(), registry.self_name(), {}
    if me is None:
        print('This device has no name in the registry yet, so nothing was synced.')
        return {}
    for row in known['devices']:
        if row['name'] == me: continue
        if not row.get('ssh'): results[row['name']] = 'unreachable'; continue
        try: remote = remotes(row['ssh'])
        except ValueError: results[row['name']] = 'failed: invalid ssh target'; continue
        results[row['name']] = sync_one(remote)
    return results


def retire_store(store, folder):
    private_directory(folder)
    stamp, n = str(int(time.time())), 0
    while True:
        target = folder / (store.stem + '-' + stamp + ('-' + str(n) if n else '') + '.json')
        if not target.exists() and not target.is_symlink(): break
        n += 1
    os.replace(store, target)
    return target


def switch(wait, overview, pool):
    if not routing.wait_idle(overview, None, wait):
        print('Claude tasks are running here; run claude-pool device switch --wait or rerun later.')
        return 3
    pending = switch_pending(settings.STORES, settings.LONG_LIVED, registry.load(), registry.self_name())
    if not pending:
        print('Nothing to switch: every account here already serves its synced long-lived token.')
        return 0
    if pool('--stop'):
        print('The pool service did not stop; nothing was changed. Rerun claude-pool device switch.')
        return 1
    done, error = [], None
    try:
        for name in pending:
            store, copy = settings.STORES / (name + '.json'), settings.LONG_LIVED / (name + '.json')
            data = copy.read_bytes()
            target = retire_store(store, settings.STORES / '.retired')
            try: write_private(store, data)
            except Exception:
                os.replace(target, store); raise
            done.append('Switched ' + name + ' to its synced long-lived token; the old one is kept in ' + str(target.parent))
    except Exception as caught: error = caught
    started = pool('--start')
    if started: print('The pool service did not restart. Run: claude-pool --start')
    if error is not None: raise error
    if started: return 1
    for line in done: print(line)
    return 0
