"""Long-lived (setup-token) store files: classification, fingerprint and expiry. Never prints a token."""
import hashlib

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
