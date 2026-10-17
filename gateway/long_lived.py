"""Long-lived (setup-token) store files: classification, fingerprint and expiry. Never prints a token."""
import hashlib
import json
from pathlib import Path

from private_files import read_private, write_private
import registry

DAY_MS = 86_400_000


def _text(value): return isinstance(value, str) and bool(value)


def _oauth(value):
    oauth = value.get('claudeAiOauth') if isinstance(value, dict) else None
    return oauth if isinstance(oauth, dict) else None


def kind(value):
    oauth = _oauth(value)
    if oauth is None: raise RuntimeError('Unsupported credential format')
    if oauth.get('shuntCredentialKind') == 'setup_token':
        # A setup token never carries a refresh token; one that does is not ours to copy or serve.
        if (_text(oauth.get('accessToken')) and 'refreshToken' not in oauth
                and _text(value.get('shuntAccountUuid')) and _text(value.get('shuntOrganizationUuid'))):
            return 'long-lived'
        raise RuntimeError('Unsupported credential format')
    if _text(oauth.get('accessToken')) and _text(oauth.get('refreshToken')): return 'refreshable'
    raise RuntimeError('Unsupported credential format')


def fingerprint(value):
    token = (_oauth(value) or {}).get('accessToken')
    if not _text(token): raise RuntimeError('Unsupported credential format')
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def expires_at_ms(value):
    expires = (_oauth(value) or {}).get('expiresAt')
    return expires if type(expires) is int and expires > 0 else None


def days_left(value, now_ms):
    expires = expires_at_ms(value)
    return None if expires is None else (expires - now_ms) / DAY_MS


def adopt(source, directory, name):
    """Move a staged long-lived token file into the long-lived folder. Verifies the copy before removing the source."""
    source, target = Path(source), Path(directory) / (name + '.json')
    value = read_private(source)
    if kind(value) != 'long-lived': raise RuntimeError('Only a long-lived token file can be adopted; nothing was changed')
    if source.resolve() == target.resolve(): return target
    if target.exists() or target.is_symlink():
        if fingerprint(read_private(target)) != fingerprint(value):
            raise RuntimeError('A different long-lived token is already saved for ' + name + '; nothing was changed. Add --replace to install the new one.')
    write_private(target, json.dumps(value))
    if fingerprint(read_private(target)) != fingerprint(value):
        raise RuntimeError('The saved token did not verify; the staged file was kept')
    source.unlink()
    return target


def _kind_of(path):
    try: return kind(read_private(path))
    except (OSError, ValueError, RuntimeError): return None


def switch_pending(stores, directory, value, self_name):
    """Accounts this device should serve from the synced long-lived token but does not yet: its store is its own
    refreshable login, or an older long-lived token of the same subscription. Derived from the files, never stored.
    The usage owner keeps its refreshable logins, so it has none."""
    owner = registry.owner(value)
    if owner is not None and owner['name'] == self_name: return []
    names = []
    for path in sorted(Path(directory).glob('*.json')):
        name = path.stem
        if name.startswith('.') or _kind_of(path) != 'long-lived': continue
        store = Path(stores) / (name + '.json')
        if store.is_symlink(): continue
        held = _kind_of(store)
        if held == 'refreshable': names.append(name)
        elif held == 'long-lived':
            new, old = read_private(path), read_private(store)
            same = (new.get('shuntAccountUuid'), new.get('shuntOrganizationUuid')) == (old.get('shuntAccountUuid'), old.get('shuntOrganizationUuid'))
            if same and fingerprint(new) != fingerprint(old): names.append(name)
    return names
