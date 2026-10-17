"""Guided subscription enrollment. Shunt owns OAuth, token refresh and routing."""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request

from account_login import login
from account_metadata import load
import long_lived
from long_lived import fingerprint, kind
from private_files import atomic_write, private_directory, read_private, write_private
import registry
from remote import Remote
import routing


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def credential(path):
    value = read_private(path)
    kind(value)
    return value


def refreshable(path):
    value = credential(path)
    if kind(value) != 'refreshable': raise RuntimeError('A refreshable OAuth login is required')
    return value


def profile(path):
    stored = refreshable(path)
    request = urllib.request.Request('https://api.anthropic.com/api/oauth/profile', headers={
        'Authorization': 'Bearer ' + stored['claudeAiOauth']['accessToken'],
        'User-Agent': 'claude-cli/2.1.300 (external, cli)', 'anthropic-beta': 'oauth-2025-04-20'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(262145)
        if len(raw) > 262144: raise RuntimeError('Identity response exceeded its size limit')
        data = json.loads(raw)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Identity verification returned HTTP {error.code}. Login is saved; rerun this command to retry.') from None
    except (OSError, ValueError):
        raise RuntimeError('Identity check unavailable. Login is saved; rerun this command to retry.') from None
    return identity(data, stored)


def identity(data, stored):
    if not isinstance(data, dict): raise RuntimeError('Invalid identity response')
    account, org = data.get('account', {}), data.get('organization', {})
    if not isinstance(account, dict) or not isinstance(org, dict): raise RuntimeError('Invalid identity response')
    user_id, org_id = account.get('uuid'), org.get('uuid')
    email = account.get('email') or account.get('email_address')
    if not all(isinstance(x, str) and x for x in (user_id, org_id, email)):
        raise RuntimeError('Anthropic did not supply a complete account and organization identity')
    if (user_id, org_id) != (stored.get('shuntAccountUuid'), stored.get('shuntOrganizationUuid')):
        raise RuntimeError('Credential and verified organization differ; nothing was enrolled')
    seat = str(org.get('seat_tier', ''))
    if org.get('organization_type') == 'claude_team':
        plan = 'Team Premium' if 'premium' in seat.lower() or '5x' in str(org.get('rate_limit_tier', '')) else 'Team'
    elif account.get('has_claude_max'):
        plan = 'Max 20x' if '20x' in str(org.get('rate_limit_tier', '')) else 'Max'
    elif account.get('has_claude_pro'): plan = 'Pro'
    else: plan = 'Subscription'
    return {'email': email, 'organization': org.get('name') or org_id, 'plan': plan,
            'userId': user_id, 'organizationId': org_id}


IDENTITY_KEYS = ('email', 'organization', 'plan', 'userId', 'organizationId')
OWNER_NEEDED = 'Identity needs the usage owner: run this on the usage owner or make it reachable. The token is saved; rerun to resume.'


def long_lived_identity(value, stores, lookup):
    """A long-lived token has no profile scope: borrow the identity of a refreshable grant with the same UUIDs."""
    ids = (value.get('shuntAccountUuid'), value.get('shuntOrganizationUuid'))
    for path in sorted(Path(stores).glob('*.json')):
        if path.is_symlink() or not path.is_file(): continue
        try: other = credential(path)
        except (OSError, ValueError, RuntimeError): continue
        if kind(other) == 'refreshable' and (other.get('shuntAccountUuid'), other.get('shuntOrganizationUuid')) == ids:
            return profile(path)
    info = lookup(*ids)
    if info is None: raise RuntimeError(OWNER_NEEDED)
    if not isinstance(info, dict) or any(not isinstance(info.get(key), str) or not info[key] for key in IDENTITY_KEYS):
        raise RuntimeError('The usage owner returned an incomplete identity; nothing was enrolled')
    if (info['userId'], info['organizationId']) != ids:
        raise RuntimeError('Credential and verified organization differ; nothing was enrolled')
    return {key: info[key] for key in IDENTITY_KEYS}


def owner_lookup(user_id, org_id, remotes=Remote):
    """Ask the usage owner (over SSH) for the identity of its refreshable grant with these UUIDs."""
    value = registry.load()
    owner = registry.owner(value)
    if owner is None or owner['name'] == registry.self_name() or not owner.get('ssh'): return None
    try: done = remotes(owner['ssh']).pool('device', 'identity', user_id, org_id)
    except ValueError: return None  # an ssh target the transport refuses
    if done.returncode: return None
    try: info = json.loads(done.stdout)
    except ValueError: return None
    return info if isinstance(info, dict) else None


def normalized(value):
    return re.sub('[^a-z0-9]', '', unicodedata.normalize('NFKD', value.lower().replace('æ', 'ae')))


def add_membership(text, name):
    value = tomllib.loads(text)
    rows = value['providers']['anthropic']['accounts']
    if any(row.get('name') == name for row in rows): return text
    if not rows:
        # An inline empty array cannot be extended with TOML array-of-table headers.
        inside, lines = False, []
        for line in text.splitlines(keepends=True):
            if line.lstrip().startswith('['): inside = line.strip() == '[providers.anthropic]'
            if inside and re.fullmatch(r'\s*accounts\s*=\s*\[\s*\]\s*(?:#.*)?', line.strip()): continue
            lines.append(line)
        text = ''.join(lines)
    candidate = text.rstrip() + '\n\n[[providers.anthropic.accounts]]\nname = ' + json.dumps(name) + '\n'
    tomllib.loads(candidate)
    return candidate


def remove_membership(text, name):
    """Inverse of add_membership: drop the [[providers.anthropic.accounts]] block named `name` (with its sub-tables)."""
    before = tomllib.loads(text)
    rows = before['providers']['anthropic'].get('accounts', [])
    if not any(row.get('name') == name for row in rows): return text
    lines = text.splitlines(keepends=True)
    header = '[[providers.anthropic.accounts]]'
    start = None
    for index, line in enumerate(lines):
        if line.strip() != header: continue
        end = index + 1
        while end < len(lines):
            stripped = lines[end].strip()
            if stripped.startswith('[') and not stripped.startswith(('[providers.anthropic.accounts.', '[[providers.anthropic.accounts.')): break
            end += 1
        block = tomllib.loads(''.join(lines[index:end]))['providers']['anthropic']['accounts']
        if block and block[0].get('name') == name:
            start = index; break
    if start is None: raise RuntimeError('The runtime configuration lists ' + name + ' in a form this command cannot edit')
    prefix, suffix = ''.join(lines[:start]), ''.join(lines[end:])
    candidate = prefix + suffix if suffix else prefix.rstrip() + '\n'
    if 'accounts' not in tomllib.loads(candidate).get('providers', {}).get('anthropic', {}):
        # Keep an empty member list so the file stays valid and add_membership can extend it again.
        out, done = [], False
        for line in candidate.splitlines(keepends=True):
            out.append(line)
            if not done and line.strip() == '[providers.anthropic]': out.append('accounts = []\n'); done = True
        candidate = ''.join(out) if done else candidate.rstrip() + '\n\n[providers.anthropic]\naccounts = []\n'
    after = tomllib.loads(candidate)
    expected = copy.deepcopy(before)
    expected['providers']['anthropic']['accounts'] = [row for row in rows if row.get('name') != name]
    if after != expected: raise RuntimeError('Removing ' + name + ' would change other runtime settings; nothing was changed')
    return candidate


def verify_expected(info, email, organization):
    if info['email'].casefold() != email.casefold():
        raise RuntimeError('Signed in as ' + info['email'] + ', expected ' + email + '. Nothing was enrolled.')
    if organization and normalized(info['organization']) != normalized(organization):
        raise RuntimeError('Selected organization is ' + info['organization'] + ', expected ' + organization + '. Nothing was enrolled.')


def refuse_duplicate(rows, stores, name, info):
    """A second name for the same subscription adds no quota."""
    for row in rows:
        if row['name'] == name: continue
        other_path = stores / (row['name'] + '.json')
        if not other_path.exists(): continue
        other = credential(other_path)
        if (other.get('shuntAccountUuid'), other.get('shuntOrganizationUuid')) == (info['userId'], info['organizationId']):
            raise RuntimeError('This subscription is already connected as ' + row.get('label', row['name']) + '; a second alias adds no quota.')


def apply_change(config, binary, paths, env, change_value, change_toml):
    """The one validated write path for membership: candidates are checked before anything is written."""
    config.parent.mkdir(parents=True, exist_ok=True)
    with (config.parent / ('.' + config.name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = load(config)
        change_value(value)
        candidates = [(p, change_toml(p.read_text())) for p in paths if p.exists()]
        with tempfile.NamedTemporaryFile(mode='w', dir=config.parent) as candidate:
            candidate.write(json.dumps(value)); candidate.flush(); load(Path(candidate.name))
        for _, content in candidates:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', dir=config.parent) as candidate:
                candidate.write(content); candidate.flush()
                result = subprocess.run([str(binary), '--config', candidate.name, 'check'], env=env,
                                        capture_output=True, timeout=20)
                if result.returncode: raise RuntimeError('Backend rejected the change; live membership is unchanged')
        # Persist recovery metadata before the watched runtime file activates membership.
        atomic_write(config, json.dumps(value, indent=2) + '\n')
        for path, content in candidates:
            if path.read_text() != content: atomic_write(path, content)


def register(config, stores, binary, paths, entry, info, env):
    def change(value):
        rows = value.setdefault('accounts', [])
        refuse_duplicate(rows, stores, entry['name'], info)
        old = next((r for r in rows if r['name'] == entry['name']), None)
        if old: rows[rows.index(old)] = entry
        else: rows.append(entry)
    apply_change(config, binary, paths, env, change, lambda text: add_membership(text, entry['name']))


def unregister(config, stores, binary, paths, name, env):
    """Drop `name` from the config and every runtime member list. Idempotent: an absent member changes nothing."""
    def change(value):
        value['accounts'] = [row for row in value.get('accounts', []) if row['name'] != name]
    apply_change(config, binary, paths, env, change, lambda text: remove_membership(text, name))


def local_paths_env(runtime, stores, backend_source=None, backend_env=None):
    """The runtime TOML files that list members, and the environment `shunt check` validates them with."""
    paths = ([Path(backend_source)] if backend_source else []) + [Path(runtime) / 'shunt.toml']
    env = {**os.environ, 'POOL_CLIENT_TOKENS':'setup:local-config-validation-token',
           'POOL_ADMIN_TOKENS':'setup:local-config-validation-token', **(backend_env or {}),
           'SHUNT_CLAUDE_ACCOUNTS_DIR': str(stores)}
    return paths, env


def register_local(entry, info):
    import settings
    paths, env = local_paths_env(settings.RUNTIME, settings.STORES)
    register(settings.CONFIG, settings.STORES, settings.HERE / 'bin/shunt', paths, entry, info, env)


def readiness(overview, entry, name, email, long):
    print('4/4 · Checking the running pool', flush=True)
    if not overview:
        print('Connected ' + entry['label'] + '. Start the pool with claude; shortcut: claude ' + entry['alias'])
        return 0
    deadline = time.monotonic() + 330
    announced = False
    while time.monotonic() < deadline:
        try:
            data = overview()
            row = next((a for a in data.get('accounts', []) if a.get('name') == name), None)
        except (OSError, ValueError, RuntimeError): row = None
        if long and row and row.get('status') == 'reauthenticate':
            print(entry['label'] + ' rejected its long-lived token. Fix: claude-pool account add ' + email + ' --long-lived --replace --name ' + name + ', then claude-pool device sync')
            return 2
        if long and row and row.get('status') == 'unknown':
            print('Connected ' + entry['label'] + ' · no reading yet (first request fills it)')
            print('Start a session: claude ' + entry['alias'] + '  |  All accounts: claude')
            return 0
        if row and row.get('status') not in ('unknown', None):
            print(f"Connected {entry['label']} · {row['status']} · {len(data['accounts'])} accounts")
            print('Start a session: claude ' + entry['alias'] + '  |  All accounts: claude')
            return 0
        if not announced:
            print('Waiting for the backend’s first quota reading…', flush=True)
            announced = True
        time.sleep(5)
    print('Login and membership are saved. Readiness is not confirmed yet; rerun this command to resume without another login.')
    return 2


def ids(value): return (value.get('shuntAccountUuid'), value.get('shuntOrganizationUuid'))


def swap_in(path, value, stamp):
    """Install `value` at `path` atomically; the old file stays beside it as <file>.replaced-<stamp> (0600, never deleted)."""
    aside, n = path.with_name(path.name + '.replaced-' + stamp), 1
    while aside.exists() or aside.is_symlink():
        aside, n = path.with_name(path.name + '.replaced-' + stamp + '-' + str(n)), n + 1
    os.link(path, aside)  # same inode: the old token survives the atomic replace below
    write_private(path, json.dumps(value))
    if fingerprint(credential(path)) != fingerprint(value): raise RuntimeError('The new token did not verify after install; the old one is kept as ' + aside.name)


def replacement(args, binary, directory, name, label, registry_path, staging):
    """Mint or read a new token for an enrolled account without touching any saved file; verify it replaces like for like."""
    token = directory / (name + '.json')
    if not (token.exists() or token.is_symlink()):
        raise RuntimeError('No long-lived token is saved for ' + name + '; run this without --replace')
    old = credential(token)
    if kind(old) != 'long-lived': raise RuntimeError('The saved file is not a long-lived token; nothing was replaced')
    if args.from_file:
        print('1/4 · Reading the replacement token', flush=True)
        new = read_private(Path(args.from_file).expanduser())
    else:
        print('1/4 · Opening browser authorization for a replacement token', flush=True)
        private_directory(directory)
        staging.append(Path(tempfile.mkdtemp(prefix='.replace-', dir=directory)))
        login(binary, staging[0], name, label, mode='setup-token')
        new = credential(staging[0] / (name + '.json'))
    if kind(new) != 'long-lived': raise RuntimeError('Only a long-lived token can replace a long-lived token; nothing was replaced')
    if fingerprint(new) == fingerprint(old): raise RuntimeError('The new token is the same as the saved one; nothing was replaced')
    saved = registry.account(registry.load(registry_path), name) or {}
    if ids(new) != ids(old) or (saved.get('userId') is not None and (saved.get('userId'), saved.get('organizationId')) != ids(new)):
        raise RuntimeError('The new token belongs to another account or organization; nothing was replaced')
    return new


def enroll_long_lived(args, email, name, entry, *, config, stores, binary, runtime, backend_source, backend_env,
                      overview, directory, registry_path, self_path, lookup):
    staging = []
    try:
        return enroll_steps(args, email, name, entry, config=config, stores=stores, binary=binary, runtime=runtime,
                            backend_source=backend_source, backend_env=backend_env, overview=overview, directory=directory,
                            registry_path=registry_path, self_path=self_path, lookup=lookup, staging=staging)
    finally:
        # A minted replacement that was not installed is discarded; the saved token stays as it was.
        for folder in staging: shutil.rmtree(folder, ignore_errors=True)


def enroll_steps(args, email, name, entry, *, config, stores, binary, runtime, backend_source, backend_env,
                 overview, directory, registry_path, self_path, lookup, staging):
    token = directory / (name + '.json')
    if args.replace:
        value = replacement(args, binary, directory, name, entry['label'] + ' — ' + email, registry_path, staging)
    else:
        if args.from_file:
            print('1/4 · Saving the staged long-lived token', flush=True)
            long_lived.adopt(Path(args.from_file).expanduser(), directory, name)
        elif token.exists() or token.is_symlink():
            print('1/4 · Using saved long-lived token', flush=True)
        else:
            print('1/4 · Opening browser authorization for a long-lived token', flush=True)
            private_directory(directory)
            login(binary, directory, name, entry['label'] + ' — ' + email, mode='setup-token')
        value = credential(token)
        if kind(value) != 'long-lived': raise RuntimeError('The saved file is not a long-lived token; nothing was enrolled')
    print('2/4 · Verifying account and organization', flush=True)
    info = long_lived_identity(value, stores, lookup or owner_lookup)
    verify_expected(info, email, args.organization)
    print(info['email'] + ' · ' + info['organization'] + ' · ' + info['plan'], flush=True)
    entry['plan'] = info['plan']
    print('3/4 · Registering account and shortcut', flush=True)
    shared = {**{key: info[key] for key in IDENTITY_KEYS}, **entry, 'fingerprint': fingerprint(value),
              'expiresAt': long_lived.expires_at_ms(value)}
    def wanted(current):
        old = registry.account(current, name) or {}
        return {**shared, **({'reserveAt': old['reserveAt']} if 'reserveAt' in old else {})}
    def upsert(current):
        current['accounts'] = [r for r in current['accounts'] if r['name'] != name] + [wanted(current)]
    known = registry.load(registry_path)
    # Refuse a second name for this subscription before anything is written (register() checks again under its lock).
    refuse_duplicate(load(config).get('accounts', []), stores, name, info)
    for row in known['accounts']:
        if row['name'] != name and (row.get('userId'), row.get('organizationId')) == (info['userId'], info['organizationId']):
            raise RuntimeError('This subscription is already connected as ' + (row.get('label') or row['name']) + '; a second alias adds no quota.')
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    if args.replace:
        swap_in(token, value, stamp)
        if args.from_file: Path(args.from_file).expanduser().unlink()
    if registry.account(known, name) != wanted(known): known = registry.update(upsert, registry_path)
    owner = registry.owner(known)
    is_owner = owner is not None and owner['name'] == registry.self_name(self_path)
    store = stores / (name + '.json')
    if store.is_symlink(): raise RuntimeError('Credential file must not be a symlink: ' + store.name)
    current = credential(store) if store.exists() else None
    if current is not None and kind(current) == 'refreshable':
        # A refreshable grant is unique; replacing it would lose it. Switching is a separate, deliberate step.
        if not is_owner: print('This machine keeps its own login for ' + entry['label'] + '; run claude-pool device switch when no task is running.', flush=True)
    elif current is None and is_owner:
        print('This machine is the usage owner and serves ' + entry['label'] + ' from a refreshable login. Connect one with claude-pool account add ' + email + ' --name ' + name + '.')
        return 0
    elif current is not None and args.replace and fingerprint(current) != fingerprint(value):
        swap_in(store, value, stamp)
    elif current is None or fingerprint(current) != fingerprint(value):
        write_private(store, json.dumps(value))
        if fingerprint(credential(store)) != fingerprint(value): raise RuntimeError('The pool copy of the token did not verify; rerun this command')
    paths, env = local_paths_env(runtime, stores, backend_source, backend_env)
    register(config, stores, binary, paths, entry, info, env)
    return readiness(overview, entry, name, email, kind(credential(store)) == 'long-lived')


def stored_kind(path):
    """'refreshable', 'long-lived', or None for a file that is unreadable or not a credential."""
    try: return kind(credential(path))
    except (OSError, ValueError, RuntimeError): return None


def retire(stores, long_lived_dir, name):
    """A refreshable grant is unique: it moves to stores/.retired/<name>-<unix>.json and is never deleted.
    Long-lived replicas are unlinked. Anything unrecognised is moved aside too, never deleted."""
    for path in (Path(stores) / (name + '.json'), Path(long_lived_dir) / (name + '.json')):
        if not (path.exists() or path.is_symlink()): continue
        if not path.is_symlink() and stored_kind(path) == 'long-lived':
            path.unlink(); continue
        folder = Path(stores) / '.retired'
        private_directory(folder)
        stamp, n = str(int(time.time())), 0
        while True:
            target = folder / (name + '-' + stamp + ('-' + str(n) if n else '') + '.json')
            try: os.link(path, target, follow_symlinks=False); break
            except FileExistsError: n += 1
        path.unlink()


def matches_native(native, name, value, stores):
    """Whether account `name` is the Native login (`native` from native_login.parse_auth_status)."""
    if not native: return False
    entry = registry.account(value, name) or {}
    if entry.get('email') and entry.get('organization'):
        email, organization = entry['email'], entry['organization']
    else:
        path = Path(stores) / (name + '.json')
        if not (path.exists() or path.is_symlink()): return False
        if kind(credential(path)) != 'refreshable': return False  # a long-lived token has no profile to compare
        try: info = profile(path)
        except RuntimeError:
            raise RuntimeError('Could not check whether ' + name + ' is the Native login (identity check unavailable); nothing was removed. Rerun when online.') from None
        email, organization = info['email'], info['organization']
    return normalized(native['email']) == normalized(email) and normalized(native.get('organization') or '') == normalized(organization)


def remove_account(name, wait, *, config, stores, binary, runtime, backend_source, backend_env, overview,
                   directory, registry_path, native_identity):
    if not re.fullmatch('[a-z0-9-]{1,80}', name): raise RuntimeError('Usage: claude-pool account remove <name> [--wait]')
    rows = load(config).get('accounts', [])
    row = next((r for r in rows if r['name'] == name), None)
    known = registry.load(registry_path)
    entry = registry.account(known, name)
    files = [stores / (name + '.json'), directory / (name + '.json')]
    if row is None and entry is None and not any(p.exists() or p.is_symlink() for p in files):
        raise RuntimeError('No account named ' + name + ' is connected (see claude-pool account list)')
    for path in files:
        if path.is_symlink(): raise RuntimeError('Credential file must not be a symlink: ' + path.name)
    label = (row or entry or {}).get('label') or name
    private_directory(stores)
    if row is not None and len(rows) == 1:
        raise RuntimeError(label + ' is the only connected account; the pool needs at least one. Nothing was removed.')
    with (stores / ('.' + name + '.add.lock')).open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('This account has a connection in progress; nothing was removed') from None
        if matches_native(native_identity() if native_identity else None, name, known, stores):
            raise RuntimeError(label + ' is the Native login of this machine and must stay connected. '
                               'Sign Native in with another connected account first (claude-native auth login). Nothing was removed.')
        if overview:
            if wait: print('Waiting for ' + label + ' to finish its running requests…', flush=True)
            if not routing.wait_idle(overview, name, wait):
                raise RuntimeError(label + ' is still serving a request' + (' after 30 minutes' if wait else '')
                                   + ' (or the pool did not answer); nothing was removed. '
                                   + ('Rerun when it is idle.' if wait else 'Rerun with --wait to remove it once idle.'))
        kept = any(p.exists() and stored_kind(p) != 'long-lived' for p in files)
        paths, env = local_paths_env(runtime, stores, backend_source, backend_env)
        unregister(config, stores, binary, paths, name, env)
        retire(stores, directory, name)
        if entry is not None:
            registry.update(lambda value: value.update(accounts=[r for r in value['accounts'] if r['name'] != name]), registry_path)
    print('Removed ' + label + ' from the pool.')
    if kept:
        print('Its login is kept in ' + str(stores / '.retired') + ' (a refreshable login cannot be recreated without a browser).')
    aside = sorted({p.name for folder in (stores, directory) for p in folder.glob(name + '.json.replaced-*')})
    if aside:
        print('Earlier replaced tokens are kept, never deleted automatically: ' + ', '.join(aside))
    if entry is not None: print('Other devices drop it after claude-pool device sync.')
    return 0


def cli(argv, *, config, stores, binary, runtime, backend_source=None, backend_env=None, overview=None,
        long_lived=None, registry_path=None, self_path=None, lookup=None, native_identity=None):
    parser = argparse.ArgumentParser(prog='claude account')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='Show connected names and shortcuts')
    add = sub.add_parser('add', help='Authorize, verify and enroll a subscription')
    add.add_argument('email', nargs='?'); add.add_argument('--name'); add.add_argument('--label')
    add.add_argument('--alias'); add.add_argument('--organization')
    add.add_argument('--long-lived', action='store_true', help='Enroll a long-lived token (shared by every device)')
    add.add_argument('--from-file', help='With --long-lived: adopt a staged 0600 token file instead of opening a browser')
    add.add_argument('--replace', action='store_true', help='With --long-lived: install a new token for this account; the old one is kept aside')
    remove = sub.add_parser('remove', help='Disconnect a subscription without interrupting running requests')
    remove.add_argument('name'); remove.add_argument('--wait', action='store_true', help='Wait (up to 30 minutes) until the account serves no request')
    args = parser.parse_args(argv)
    config, stores, binary, runtime = map(Path, (config, stores, binary, runtime))
    directory = Path(long_lived) if long_lived else stores.parent / 'claude-long-lived'
    if args.command == 'remove':
        return remove_account(args.name, args.wait, config=config, stores=stores, binary=binary, runtime=runtime,
                              backend_source=backend_source, backend_env=backend_env, overview=overview, directory=directory,
                              registry_path=registry_path, native_identity=native_identity)
    if args.command == 'list':
        for row in load(config).get('accounts', []):
            print(f"{row.get('label', row['name'])} · claude {row.get('alias', row['name'])}")
        return 0
    if args.from_file and not args.long_lived: raise RuntimeError('--from-file needs --long-lived')
    if args.replace and not args.long_lived: raise RuntimeError('--replace needs --long-lived')
    email = args.email
    if not email and os.isatty(0): email = input('Account email: ').strip()
    if not email or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise RuntimeError('Usage: claude account add email@example.com [--name name] [--label Label]')
    name = args.name or re.sub('[^a-z0-9-]', '-', email.split('@')[0].lower()).strip('-')
    current = load(config); old = next((a for a in current.get('accounts', []) if a['name'] == name), {})
    entry = {'name': name, 'label': args.label or old.get('label') or name.replace('-', ' ').title(),
             'alias': args.alias or old.get('alias') or name}
    # Validate shortcuts and labels before opening a browser or writing a credential.
    tentative = {**current, 'accounts': [r for r in current.get('accounts', []) if r['name'] != name] + [entry]}
    from account_metadata import RESERVED
    if not re.fullmatch('[a-z0-9-]{1,80}', name) or not re.fullmatch('[a-z0-9-]{1,80}', entry['alias']) or entry['alias'] in RESERVED:
        raise RuntimeError('Choose a lowercase account name and an unused shortcut')
    if not 1 <= len(entry['label']) <= 80 or not entry['label'].isprintable(): raise RuntimeError('Invalid display label')
    if any(r.get('alias', r['name']) == entry['alias'] for r in tentative['accounts'] if r['name'] != name):
        raise RuntimeError('That shortcut is already used')
    stores.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = stores.stat()
    if stores.is_symlink() or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
        raise RuntimeError('Credential folder must be your private 0700 directory')
    with (stores / ('.' + name + '.add.lock')).open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('This account already has a connection in progress') from None
        if args.long_lived:
            return enroll_long_lived(args, email, name, entry, config=config, stores=stores, binary=binary, runtime=runtime,
                                     backend_source=backend_source, backend_env=backend_env, overview=overview,
                                     directory=directory, registry_path=registry_path, self_path=self_path, lookup=lookup)
        path = stores / (name + '.json')
        print('1/4 · ' + ('Using saved login' if path.exists() else 'Opening browser authorization'), flush=True)
        if not path.exists() and not path.is_symlink(): login(binary, stores, name, entry['label'] + ' — ' + email)
        print('2/4 · Verifying account and organization', flush=True)
        info = profile(path); verify_expected(info, email, args.organization)
        print(info['email'] + ' · ' + info['organization'] + ' · ' + info['plan'], flush=True)
        entry['plan'] = info['plan']
        print('3/4 · Registering account and shortcut', flush=True)
        paths, env = local_paths_env(runtime, stores, backend_source, backend_env)
        register(config, stores, binary, paths, entry, info, env)
        return readiness(overview, entry, name, email, False)
