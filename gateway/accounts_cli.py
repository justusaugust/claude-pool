"""Guided subscription enrollment. Shunt owns OAuth, token refresh and routing."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
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


def register(config, stores, binary, paths, entry, info, env):
    config.parent.mkdir(parents=True, exist_ok=True)
    with (config.parent / ('.' + config.name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = load(config)
        rows = value.setdefault('accounts', [])
        refuse_duplicate(rows, stores, entry['name'], info)
        old = next((r for r in rows if r['name'] == entry['name']), None)
        if old: rows[rows.index(old)] = entry
        else: rows.append(entry)
        candidates = [(p, add_membership(p.read_text(), entry['name'])) for p in paths if p.exists()]
        with tempfile.NamedTemporaryFile(mode='w', dir=config.parent) as candidate:
            candidate.write(json.dumps(value)); candidate.flush(); load(Path(candidate.name))
        for _, content in candidates:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', dir=config.parent) as candidate:
                candidate.write(content); candidate.flush()
                result = subprocess.run([str(binary), '--config', candidate.name, 'check'], env=env,
                                        capture_output=True, timeout=20)
                if result.returncode: raise RuntimeError('Backend rejected enrollment; live membership is unchanged')
        # Persist recovery metadata before the watched runtime file activates membership.
        atomic_write(config, json.dumps(value, indent=2) + '\n')
        for path, content in candidates:
            if path.read_text() != content: atomic_write(path, content)


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
            print(entry['label'] + ' rejected its long-lived token. Fix: claude-pool account add ' + email + ' --long-lived --name ' + name + ', then claude-pool device sync')
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


def enroll_long_lived(args, email, name, entry, *, config, stores, binary, runtime, backend_source, backend_env,
                      overview, directory, registry_path, self_path, lookup):
    token = directory / (name + '.json')
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
    elif current is None or fingerprint(current) != fingerprint(value):
        write_private(store, json.dumps(value))
        if fingerprint(credential(store)) != fingerprint(value): raise RuntimeError('The pool copy of the token did not verify; rerun this command')
    paths, env = local_paths_env(runtime, stores, backend_source, backend_env)
    register(config, stores, binary, paths, entry, info, env)
    return readiness(overview, entry, name, email, kind(credential(store)) == 'long-lived')


def cli(argv, *, config, stores, binary, runtime, backend_source=None, backend_env=None, overview=None,
        long_lived=None, registry_path=None, self_path=None, lookup=None):
    parser = argparse.ArgumentParser(prog='claude account')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='Show connected names and shortcuts')
    add = sub.add_parser('add', help='Authorize, verify and enroll a subscription')
    add.add_argument('email', nargs='?'); add.add_argument('--name'); add.add_argument('--label')
    add.add_argument('--alias'); add.add_argument('--organization')
    add.add_argument('--long-lived', action='store_true', help='Enroll a long-lived token (shared by every device)')
    add.add_argument('--from-file', help='With --long-lived: adopt a staged 0600 token file instead of opening a browser')
    args = parser.parse_args(argv)
    config, stores, binary, runtime = map(Path, (config, stores, binary, runtime))
    if args.command == 'list':
        for row in load(config).get('accounts', []):
            print(f"{row.get('label', row['name'])} · claude {row.get('alias', row['name'])}")
        return 0
    if args.from_file and not args.long_lived: raise RuntimeError('--from-file needs --long-lived')
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
            directory = Path(long_lived) if long_lived else stores.parent / 'claude-long-lived'
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
