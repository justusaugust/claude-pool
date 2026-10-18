"""Usage readings move from the usage owner to the other devices. Only quota numbers travel, never identities or tokens.
device_sync is imported inside Puller.run_once: bridge -> readings -> device_sync -> accounts_cli -> bridge must not cycle."""
import json
import math
import threading
import time

from private_files import read_private, write_private
import registry
from remote import OUTDATED, Remote

LIMIT = 65536
KEYS = ('fiveHour', 'sevenDay', 'fableWeekly')
ENTRY = {*KEYS, 'seenAt'}


def number(value): return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
def integer(value): return type(value) is int and value >= 0


def clean_window(value):
    """A valid window in its exact shape, or None. Out-of-range or non-finite readings are dropped, not the file."""
    if not isinstance(value, dict) or not number(value.get('pct')) or not 0 <= value['pct'] <= 100: return None
    reset = value.get('resetsAt')
    return {'pct': value['pct'], 'resetsAt': int(reset) if number(reset) and reset >= 0 else None}


def check(value):
    """Return the shared reading in its exact shape, or None when it is not valid."""
    try:
        if not isinstance(value, dict) or set(value) != {'version', 'device', 'at', 'accounts'} or value['version'] != 1: return None
        if not registry.NAME.fullmatch(value['device'] if isinstance(value['device'], str) else '') or not integer(value['at']): return None
        if not isinstance(value['accounts'], dict): return None
        accounts = {}
        for name, entry in value['accounts'].items():
            if not isinstance(name, str) or not isinstance(entry, dict) or set(entry) != ENTRY or not integer(entry['seenAt']): return None
            accounts[name] = {**{key: clean_window(entry[key]) for key in KEYS}, 'seenAt': min(entry['seenAt'], value['at'])}
        return {'version': 1, 'device': value['device'], 'at': value['at'], 'accounts': accounts}
    except (TypeError, KeyError): return None


def export(overview, device, now_ms):
    accounts = {}
    for row in overview.get('accounts', []):
        if not isinstance(row, dict) or not isinstance(row.get('name'), str) or row.get('readingSource'): continue
        seen = row.get('seenAt')
        entry = {}
        for key in KEYS:
            window = row.get(key)
            if isinstance(window, dict) and number(window.get('pct')):
                reset = window.get('resetsAt')
                entry[key] = {'pct': min(100, max(0, window['pct'])), 'resetsAt': int(reset) if number(reset) and reset >= 0 else None}
            else: entry[key] = None
        entry['seenAt'] = int(seen) if number(seen) and seen >= 0 else 0
        accounts[row['name']] = entry
    return {'version': 1, 'device': device, 'at': now_ms, 'accounts': accounts}


def merge(rows, shared, now_ms, max_age_ms=900_000):
    shared = check(shared)
    if not shared or not -60_000 <= now_ms - shared['at'] <= max_age_ms: return rows
    for row in rows:
        entry = shared['accounts'].get(row.get('name'))
        if not entry or entry['seenAt'] <= (row.get('seenAt') or 0): continue
        for key in KEYS: row[key] = entry[key]
        row['seenAt'] = entry['seenAt']
        row['readingSource'] = 'shared:' + shared['device']
    return rows


def load_shared(path):
    try: return check(read_private(path, LIMIT))
    except (OSError, ValueError, RuntimeError): return None


PROBE_EVERY_MS = 900_000
PROBE = ['-p', 'Reply with OK.', '--model', 'haiku']
PROBE_RESULTS = ('ok', 'rejected')


def _store_fingerprint(name):
    """The fingerprint of the long-lived token this device serves for `name`, or None (refreshable, missing, unreadable)."""
    import settings
    from long_lived import fingerprint, kind
    path = settings.STORES / (name + '.json')
    if not registry.NAME.fullmatch(name) or path.is_symlink(): return None
    try:
        value = read_private(path)
        return fingerprint(value) if kind(value) == 'long-lived' else None
    except (OSError, ValueError, RuntimeError): return None


def load_probes(path):
    """name -> {at, fingerprint, result}: the last probe of each long-lived account. Never holds a token."""
    try: value = read_private(path, LIMIT)
    except (OSError, ValueError, RuntimeError): return {}
    if not isinstance(value, dict): return {}
    return {name: entry for name, entry in value.items()
            if isinstance(name, str) and registry.NAME.fullmatch(name) and isinstance(entry, dict) and integer(entry.get('at'))
            and isinstance(entry.get('fingerprint'), str) and entry.get('result') in PROBE_RESULTS}


def reauthenticate(path):
    """Accounts whose served long-lived token the last probe found rejected (a replaced token clears the mark)."""
    return {name for name, entry in load_probes(path).items()
            if entry['result'] == 'rejected' and _store_fingerprint(name) == entry['fingerprint']}


class AuthProbe:
    """Shunt answers a 401 on a setup-token account by cooling it for five minutes and rotating, so a revoked long-lived
    token only ever shows as cooling. On the device that serves it, a long-lived account that is cooling gets the cheap
    probe (one real Claude Code request through the pool, `claude-pool <alias> -p 'Reply with OK.' --model haiku`) at most
    every 15 minutes; a failed probe marks it `reauthenticate` for device check and doctor. A served (ready) account
    clears its mark. The result file holds names, times, fingerprints and the result, never a token."""
    def __init__(self, path, overview=None, run=None, clock=None):
        self.path, self.overview, self.run = path, overview, run
        self.clock = clock or (lambda: int(time.time() * 1000))

    def default_run(self, alias):
        from remote import Local
        return Local().pool(alias, *PROBE, timeout=300)

    def default_overview(self):
        import device_sync
        return device_sync.pool_overview()

    def run_once(self):
        try: value = (self.overview or self.default_overview)()
        except Exception: return {}
        rows = [row for row in value.get('accounts') or [] if isinstance(row, dict) and isinstance(row.get('name'), str)]
        state, changed, now = load_probes(self.path), False, self.clock()
        try:
            from account_metadata import load
            import settings
            aliases = {row['name']: row.get('alias') or row['name'] for row in load(settings.CONFIG).get('accounts', [])}
        except Exception: aliases = {}
        for row in rows:
            name = row['name']
            if row.get('status') in ('ready', 'limited') and name in state:
                del state[name]; changed = True; continue
            if row.get('status') != 'cooling': continue
            fp = _store_fingerprint(name)
            if fp is None: continue
            last = state.get(name)
            if last and last['fingerprint'] == fp and now - last['at'] < PROBE_EVERY_MS: continue
            done = (self.run or self.default_run)(aliases.get(name, name))
            passed = done is not None and done.returncode == 0 and b'OK' in (done.stdout or b'')
            state[name] = {'at': now, 'fingerprint': fp, 'result': 'ok' if passed else 'rejected'}
            changed = True
        if changed: write_private(self.path, json.dumps(state, sort_keys=True))
        return state


class Puller:
    def __init__(self, path, journal, remotes=Remote, interval=300, registry_every=3):
        self.path, self.journal, self.remotes = path, journal, remotes
        self.interval, self.registry_every = interval, registry_every
        self.successes, self.failing = 0, False

    def fail(self, reason):
        if not self.failing: self.journal.record('readings_sync_failed', reason=reason)
        self.failing = True
        return False

    def run_once(self):
        try: value = registry.load()
        except ValueError: return False
        owner, me = registry.owner(value), registry.self_name()
        if owner and me and owner['name'] == me:
            try: self.path.unlink(missing_ok=True)
            except OSError: pass
            return False
        if not owner or not me: return False
        if not owner.get('ssh'): return self.fail('no ssh target')
        remote = self.remotes(owner['ssh'])
        done = remote.device('readings', timeout=60)
        if done.returncode == OUTDATED: return self.fail('outdated')  # journaled once; the owner needs claude-pool device add
        if done.returncode != 0 or len(done.stdout) > LIMIT: return self.fail('pull failed')
        try: shared = check(json.loads(done.stdout))
        except ValueError: shared = None
        if not shared: return self.fail('bad reading')
        write_private(self.path, json.dumps(shared))
        self.failing = False
        self.successes += 1
        if self.successes % self.registry_every == 0:
            try:
                import device_sync
                device_sync.pull(remote)
            except Exception: pass  # sync reports its own problems
        return True

    def start(self, stop, probe=None):
        def loop():
            while True:
                if probe is not None:
                    try: probe.run_once()
                    except Exception: pass  # the probe never stops the readings
                try: self.run_once()
                except Exception: self.fail('error')
                if stop.wait(self.interval): return
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread
