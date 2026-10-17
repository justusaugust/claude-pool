"""Shared offline fixtures. Token-shaped values are built at runtime so no source file contains one."""
import json
import os
from pathlib import Path


def fixture_token(tag='a'):
    return ''.join(['sk', '-ant', '-oat01', '-fixture-', tag * 32])


def _store(directory, name, value):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (name + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as file: json.dump(value, file)
    path.chmod(0o600)
    return path


def long_lived_file(directory, name, user='user-a', org='team-a', token=None, expires_ms=None):
    oauth = {'accessToken': token or fixture_token(), 'shuntCredentialKind': 'setup_token'}
    if expires_ms is not None: oauth['expiresAt'] = expires_ms
    return _store(directory, name, {'claudeAiOauth': oauth, 'shuntAccountUuid': user, 'shuntOrganizationUuid': org})


def refreshable_file(directory, name, user, org):
    oauth = {'accessToken': fixture_token('r'), 'refreshToken': 'fixture-refresh-' + name, 'expiresAt': 4_000_000_000_000}
    return _store(directory, name, {'claudeAiOauth': oauth, 'shuntAccountUuid': user, 'shuntOrganizationUuid': org})


class FakeRemote:
    """Records calls instead of running ssh. The handler answers each call."""
    def __init__(self, name, handler):
        self.name, self.handler, self.calls = name, handler, []

    def call(self, argv, stdin=None, timeout=120):
        self.calls.append((list(argv), stdin))
        return self.handler(list(argv), stdin)

    def pool(self, *args, stdin=None, timeout=120):
        return self.call(['.local/bin/claude-pool', *args], stdin=stdin, timeout=timeout)

    def reachable(self): return True


class SecretWatch:
    """Asserts that none of the watched secrets leaks into argv, text or files."""
    def __init__(self, *secrets):
        self.secrets = [secret for secret in secrets if secret]

    def _check(self, text, where):
        for secret in self.secrets:
            if secret in text: raise AssertionError('A secret appeared in ' + where)

    def argv(self, calls):
        for argv, _stdin in calls:
            for word in argv: self._check(str(word), 'argv')

    def text(self, *strings):
        for string in strings: self._check(string if isinstance(string, str) else bytes(string).decode('utf-8', 'replace'), 'text')

    def tree(self, root, allowed):
        allowed = {Path(path).resolve() for path in allowed}
        for path in Path(root).rglob('*'):
            if not path.is_file() or path.resolve() in allowed: continue
            self._check(path.read_bytes().decode('utf-8', 'replace'), 'file ' + path.name)
