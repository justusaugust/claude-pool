"""Move long-lived accounts and the registry between devices. Tokens travel only on ssh stdin or an ssh stdout pipe.
Receiving never stops or starts the service; only `switch` does, and only while nothing is in flight."""
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.request

import accounts_cli
import long_lived
from long_lived import fingerprint, kind
from private_files import private_directory, read_private, write_private
import registry
from remote import Remote, redact, require_pipe
import routing
import service
import settings

LIMIT = 131072
NAME = re.compile(r'[a-z0-9-]{1,80}')
STATUS_PARTS = []       # callables returning extra non-secret keys for `status()`
INTEGRATION_HOOKS = []  # callables run with the new registry after it replaced the local one
switch_pending = long_lived.switch_pending


class Unreachable(Exception): pass
class Failed(Exception): pass


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


def build_version():
    try: return json.loads((settings.HERE.parent / 'build-info.json').read_text()).get('version')
    except (OSError, ValueError, AttributeError): return None


def account_rows():
    members, rows = set(config_names()), []
    names = set(members) | {p.stem for p in settings.LONG_LIVED.glob('*.json') if not p.stem.startswith('.')}
    for name in sorted(names):
        replica_path, store = settings.LONG_LIVED / (name + '.json'), settings.STORES / (name + '.json')
        have = accounts_cli.stored_kind(replica_path) == 'long-lived'
        path = replica_path if have else store
        try: value = read_private(path)
        except (OSError, ValueError, RuntimeError): continue
        try: row = {'name': name, 'kind': 'long-lived' if have else kind(value), 'fingerprint': fingerprint(value),
                    'expiresAt': long_lived.expires_at_ms(value), 'active': name in members}
        except RuntimeError: continue
        rows.append(row)
    return rows


def status():
    """Non-secret state of this device: fingerprints only, never a token."""
    known = registry.load()
    value = {'name': registry.self_name(), 'version': build_version(), 'platform': service.platform(), 'service': service_state(),
             'accounts': account_rows(), 'switchPending': switch_pending(settings.STORES, settings.LONG_LIVED, known, registry.self_name()),
             'registryGeneration': known['generation'], 'registryHash': registry.digest(known)}
    for part in STATUS_PARTS: value.update(part())
    return value


def send_account(name, out=None):
    out = out or sys.stdout.buffer
    require_pipe(out)
    path = replica(name)
    if long_lived.kind(read_private(path)) != 'long-lived': raise RuntimeError('Only a long-lived token can be sent')
    out.write(path.read_bytes()); out.flush()


def receive_account(name, data):
    path = replica(name)
    if not isinstance(data, bytes) or len(data) > LIMIT: raise ValueError('Account file is larger than 128 KiB')
    try: value = json.loads(data)
    except ValueError: raise RuntimeError('Unsupported credential format') from None
    if kind(value) != 'long-lived': raise RuntimeError('Only a long-lived token can be received')
    fp = fingerprint(value)
    known = registry.load()
    same = accounts_cli.stored_kind(path) == 'long-lived' and fingerprint(read_private(path)) == fp
    if not same: write_private(path, data)
    held = 'unchanged' if same else 'staged'
    if is_owner(known): return held
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
    return 'installed'


def pending_file(): return settings.REGISTRY.parent / 'device-pending-removals.json'


def read_pending():
    try: names = json.loads(pending_file().read_text())
    except (OSError, ValueError): return []
    return [n for n in names if isinstance(n, str) and NAME.fullmatch(n)] if isinstance(names, list) else []


def remove_local(name):
    paths, env = accounts_cli.local_paths_env(settings.RUNTIME, settings.STORES)
    accounts_cli.unregister(settings.CONFIG, settings.STORES, settings.HERE / 'bin/shunt', paths, name, env)
    accounts_cli.retire(settings.STORES, settings.LONG_LIVED, name)


def receive_registry(data, overview=None):
    if not isinstance(data, bytes) or len(data) > registry.LIMIT: raise ValueError('Registry is larger than 256 KiB')
    try: incoming = registry.validate(json.loads(data))
    except ValueError as error: raise ValueError('Invalid registry: ' + str(error)) from None
    previous = registry.load()
    report = {'result': 'unchanged', 'removed': [], 'kept_busy': [], 'failed': {}, 'hooks': {}}
    if registry.diverged(previous, incoming): report['result'] = 'merged'
    replaced = incoming['generation'] > previous['generation'] and registry.replace(incoming)
    if replaced: report['result'] = 'replaced'
    now = registry.load()
    listed = {row['name'] for row in now['accounts']}
    wanted = [row['name'] for row in previous['accounts'] if row['name'] not in listed] if replaced else []
    pending = sorted((set(read_pending()) | set(wanted)) - listed)
    left = []
    for name in pending:
        try:
            if name not in config_names() and not any((folder / (name + '.json')).exists() for folder in (settings.STORES, settings.LONG_LIVED)): continue
            if service_state() == 'running' and not routing.wait_idle(overview or pool_overview, name, False):
                report['kept_busy'].append(name); left.append(name); continue
            if name in config_names() and len(config_names()) == 1:
                report['failed'][name] = 'it is the last account on this device'; left.append(name); continue
            remove_local(name); report['removed'].append(name)
        except Exception as error:
            report['failed'][name] = type(error).__name__ + ': ' + str(error); left.append(name)
    if left != read_pending():
        if left: write_private(pending_file(), json.dumps(left))
        else: pending_file().unlink(missing_ok=True)
    if replaced:
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
    done = remote.pool(*args, stdin=stdin)
    if done.returncode == 255: raise Unreachable()
    if done.returncode: raise Failed('claude-pool device ' + ' '.join(args[1:2]) + ' exited ' + str(done.returncode))
    return done.stdout


def peer_status(remote):
    try: value = json.loads(peer(remote, 'device', 'status'))
    except ValueError: raise Failed('the other device sent an unreadable status') from None
    if not isinstance(value, dict): raise Failed('the other device sent an unreadable status')
    rows = value.get('accounts') if isinstance(value.get('accounts'), list) else []
    value['accounts'] = {row['name']: row for row in rows if isinstance(row, dict) and isinstance(row.get('name'), str)}
    return value


def guarded(work):
    try: work(); return 'ok'
    except Unreachable: return 'unreachable'
    except (Failed, ValueError, RuntimeError, OSError) as error:
        return 'failed: ' + redact(str(error), secrets())[:200]


def push(remote):
    def work():
        theirs = peer_status(remote)
        known = registry.load()
        generation = theirs.get('registryGeneration')
        if not isinstance(generation, int) or known['generation'] > generation:
            peer(remote, 'device', 'receive-registry', stdin=json.dumps(known).encode())
        for row in known['accounts']:
            path = settings.LONG_LIVED / (row['name'] + '.json')
            if accounts_cli.stored_kind(path) != 'long-lived': continue
            data = path.read_bytes()
            if theirs['accounts'].get(row['name'], {}).get('fingerprint') == fingerprint(json.loads(data)): continue
            peer(remote, 'device', 'receive-account', row['name'], stdin=data)
    return guarded(work)


def pull(remote):
    def work():
        theirs = peer_status(remote)
        receive_registry(peer(remote, 'device', 'registry'))
        known = {row['name'] for row in registry.load()['accounts']}
        for name, row in sorted(theirs['accounts'].items()):
            path = settings.LONG_LIVED / (name + '.json')
            if row.get('kind') != 'long-lived' or name not in known or not NAME.fullmatch(name): continue
            if accounts_cli.stored_kind(path) == 'long-lived' and fingerprint(read_private(path)) == row.get('fingerprint'): continue
            receive_account(name, peer(remote, 'device', 'send-account', name))
    return guarded(work)


def sync_all(remotes=Remote):
    known, me, results = registry.load(), registry.self_name(), {}
    for row in known['devices']:
        if row['name'] == me: continue
        if not row.get('ssh'): results[row['name']] = 'unreachable'; continue
        try: remote = remotes(row['ssh'])
        except ValueError: results[row['name']] = 'failed: invalid ssh target'; continue
        pushed = push(remote)
        if pushed == 'unreachable': results[row['name']] = pushed; continue
        pulled = pull(remote)
        results[row['name']] = pushed if pushed != 'ok' else pulled
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
        print('Nothing to switch: no account here still uses its own refreshable login.')
        return 0
    pool('--stop')
    try:
        for name in pending:
            store, copy = settings.STORES / (name + '.json'), settings.LONG_LIVED / (name + '.json')
            data = copy.read_bytes()
            target = retire_store(store, settings.STORES / '.retired')
            try: write_private(store, data)
            except Exception:
                os.replace(target, store); raise
            print('Switched ' + name + ' to its long-lived token; the old login is kept in ' + str(target.parent))
    finally:
        pool('--start')
    return 0
