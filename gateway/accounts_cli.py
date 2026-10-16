"""Guided subscription enrollment. Shunt owns OAuth, token refresh and routing."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request

from account_login import login
from account_metadata import load
from bridge import atomic_write


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def credential(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 131072:
            raise RuntimeError('Credential must be your private 0600 file')
        value = json.load(file)
    if not isinstance(value, dict): raise RuntimeError('Invalid credential format')
    oauth = value.get('claudeAiOauth', {})
    if not isinstance(oauth, dict): raise RuntimeError('Invalid OAuth credential format')
    if not all(isinstance(oauth.get(k), str) and oauth[k] for k in ('accessToken', 'refreshToken')):
        raise RuntimeError('A refreshable OAuth login is required')
    return value


def profile(path):
    stored = credential(path)
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


def register(config, stores, binary, paths, entry, info, env):
    config.parent.mkdir(parents=True, exist_ok=True)
    with (config.parent / ('.' + config.name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = load(config)
        rows = value.setdefault('accounts', [])
        for row in rows:
            if row['name'] == entry['name']: continue
            other_path = stores / (row['name'] + '.json')
            if not other_path.exists(): continue
            other = credential(other_path)
            if (other.get('shuntAccountUuid'), other.get('shuntOrganizationUuid')) == (info['userId'], info['organizationId']):
                raise RuntimeError('This subscription is already connected as ' + row.get('label', row['name']) + '; a second alias adds no quota.')
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


def cli(argv, *, config, stores, binary, runtime, backend_source=None, backend_env=None, overview=None):
    parser = argparse.ArgumentParser(prog='claude account')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='Show connected names and shortcuts')
    add = sub.add_parser('add', help='Authorize, verify and enroll a subscription')
    add.add_argument('email', nargs='?'); add.add_argument('--name'); add.add_argument('--label')
    add.add_argument('--alias'); add.add_argument('--organization')
    args = parser.parse_args(argv)
    config, stores, binary, runtime = map(Path, (config, stores, binary, runtime))
    if args.command == 'list':
        for row in load(config).get('accounts', []):
            print(f"{row.get('label', row['name'])} · claude {row.get('alias', row['name'])}")
        return 0
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
        path = stores / (name + '.json')
        print('1/4 · ' + ('Using saved login' if path.exists() else 'Opening browser authorization'), flush=True)
        if not path.exists() and not path.is_symlink(): login(binary, stores, name, entry['label'] + ' — ' + email)
        print('2/4 · Verifying account and organization', flush=True)
        info = profile(path); verify_expected(info, email, args.organization)
        print(info['email'] + ' · ' + info['organization'] + ' · ' + info['plan'], flush=True)
        entry['plan'] = info['plan']
        print('3/4 · Registering account and shortcut', flush=True)
        paths = ([Path(backend_source)] if backend_source else []) + [runtime / 'shunt.toml']
        env = {**os.environ, 'POOL_CLIENT_TOKENS':'setup:local-config-validation-token',
               'POOL_ADMIN_TOKENS':'setup:local-config-validation-token', **(backend_env or {}),
               'SHUNT_CLAUDE_ACCOUNTS_DIR': str(stores)}
        register(config, stores, binary, paths, entry, info, env)
        print('4/4 · Checking the running pool', flush=True)
        if overview:
            deadline = time.monotonic() + 330
            announced = False
            while time.monotonic() < deadline:
                try:
                    data = overview()
                    row = next((a for a in data.get('accounts', []) if a.get('name') == name), None)
                except (OSError, ValueError, RuntimeError): row = None
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
        print('Connected ' + entry['label'] + '. Start the pool with claude; shortcut: claude ' + entry['alias'])
        return 0
