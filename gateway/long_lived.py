"""Long-lived (setup-token) store files: classification, fingerprint and expiry. Never prints a token."""
import hashlib
import json
from pathlib import Path

from private_files import read_private, write_private

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
            raise RuntimeError('A different long-lived token is already saved for ' + name + '; nothing was changed')
    write_private(target, json.dumps(value))
    if fingerprint(read_private(target)) != fingerprint(value):
        raise RuntimeError('The saved token did not verify; the staged file was kept')
    source.unlink()
    return target
