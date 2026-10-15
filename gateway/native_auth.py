"""Validate native claude.ai callers without reading their credential store."""
import hashlib
import json
import os
import re
import stat
import threading
import time
import urllib.request
import urllib.error

PROFILE_URL = 'https://api.anthropic.com/api/oauth/profile'


class NativeAuthUnavailable(RuntimeError): pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class NativeAuth:
    def __init__(self, stores, names):
        self.identities, self.cache = {}, {}
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(2)
        self.opener = urllib.request.build_opener(NoRedirect())
        for name in names:
            if not re.fullmatch(r'[a-z0-9-]{1,80}', name): continue
            try:
                fd = os.open(stores / (name + '.json'), os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd) as file:
                    info = os.fstat(file.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid() or info.st_size > 1024 * 1024: continue
                    value = json.load(file)
                if not isinstance(value, dict): continue
                user, org = value.get('shuntAccountUuid'), value.get('shuntOrganizationUuid')
                if isinstance(user, str) and user and isinstance(org, str) and org:
                    self.identities[(user, org)] = name
            except (OSError, ValueError, TypeError): continue

    def authenticate(self, authorization):
        # Never send a mistyped local control token to the provider.
        if not re.fullmatch(r'Bearer sk-ant-oat01-[A-Za-z0-9_-]{20,4096}', authorization): return None
        key = hashlib.sha256(authorization.encode()).hexdigest()
        now = time.monotonic()
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > now: return cached[1]
        if not self.slots.acquire(timeout=.5): raise NativeAuthUnavailable()
        name = None
        try:
            with self.lock:
                cached = self.cache.get(key)
                if cached and cached[0] > time.monotonic(): return cached[1]
            request = urllib.request.Request(PROFILE_URL, headers={
                'Authorization': authorization, 'User-Agent': 'claude-cli/2.1.296 (external, cli)',
                'anthropic-beta': 'oauth-2025-04-20'})
            with self.opener.open(request, timeout=3) as response:
                data = response.read(256 * 1024 + 1)
                if len(data) > 256 * 1024: raise NativeAuthUnavailable()
                value = json.loads(data)
            account, org = value.get('account', {}), value.get('organization', {})
            if isinstance(account, dict) and isinstance(org, dict):
                name = self.identities.get((account.get('uuid'), org.get('uuid')))
        except urllib.error.HTTPError as error:
            if error.code not in (401,403): raise NativeAuthUnavailable() from None
        except (OSError, ValueError, TypeError, AttributeError): raise NativeAuthUnavailable() from None
        finally: self.slots.release()
        with self.lock:
            self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
            if len(self.cache) >= 64: self.cache.pop(next(iter(self.cache)))
            # Cache only a token fingerprint, never the credential itself.
            self.cache[key] = (time.monotonic() + (120 if name else 2), name)
        return name
