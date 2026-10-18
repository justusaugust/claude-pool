"""Usage readings move from the usage owner to the other devices. Only quota numbers travel, keyed by the owner's account
names with each account's non-secret UUIDs (userId, organizationId, as in the registry), so a device that names the same
subscription differently files them under its own name (localize). Never emails, profiles or tokens.
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
IDS = {'userId', 'organizationId'}  # optional: both or neither


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
            if not isinstance(name, str) or not isinstance(entry, dict) or set(entry) not in (ENTRY, ENTRY | IDS) or not integer(entry['seenAt']): return None
            accounts[name] = {**{key: clean_window(entry[key]) for key in KEYS}, 'seenAt': min(entry['seenAt'], value['at'])}
            if IDS <= set(entry):
                if registry.ids(entry) is None: return None
                accounts[name].update(userId=entry['userId'], organizationId=entry['organizationId'])
        return {'version': 1, 'device': value['device'], 'at': value['at'], 'accounts': accounts}
    except (TypeError, KeyError): return None


def export(overview, device, now_ms, ids=None):
    """`ids`: account name -> (userId, organizationId) of this device's accounts, attached so others can match them."""
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
        pair = (ids or {}).get(row['name'])
        if pair and registry.ids({'userId': pair[0], 'organizationId': pair[1]}): entry.update(userId=pair[0], organizationId=pair[1])
        accounts[row['name']] = entry
    return {'version': 1, 'device': device, 'at': now_ms, 'accounts': accounts}


def localize(shared, rows):
    """`shared` with each account filed under the name this device holds its subscription under (`rows`: status account
    rows). An entry without UUIDs, or of a subscription this device does not hold, keeps its name; a matched entry wins
    over one that only shares the name."""
    matched, kept = {}, {}
    for name, entry in shared['accounts'].items():
        mine = registry.local_name({'name': name, **{key: entry[key] for key in IDS if key in entry}}, rows) if registry.ids(entry) else None
        if mine: matched.setdefault(mine, entry)
        else: kept[name] = entry
    return {**shared, 'accounts': {**{name: entry for name, entry in kept.items() if name not in matched}, **matched}}


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
PROBE_RESULTS = ('ok', 'rejected', 'unknown')


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
    """Accounts whose served long-lived token the probe found rejected (a replaced token clears the mark)."""
    return {name for name, entry in load_probes(path).items()
            if entry['result'] == 'rejected' and _store_fingerprint(name) == entry['fingerprint']}


def verdict(name, outcomes):
    """'ok' when the probed account itself answered with success, 'rejected' when it answered 401 itself, else None.
    Shunt answers a 401 on a setup token by cooling the account and rotating to the next one, so a success from another
    account, a 5xx or 529, a network error or no answer at all says nothing about this token."""
    own = [row for row in outcomes if row.get('account') == name and isinstance(row.get('status'), int)]
    if any(200 <= row['status'] < 300 for row in own): return 'ok'
    if any(row['status'] == 401 for row in own): return 'rejected'
    return None


class AuthProbe:
    """On the device that serves it, a long-lived account the overview shows cooling gets the cheap probe (one real Claude
    Code request through the pool, `claude-pool <alias> -p 'Reply with OK.' --model haiku`) at most every 15 minutes,
    however its status changes in between. The bridge reports which account answered each upstream request
    (`outcomes(since_ms)`, from x-shunt-account). Only a 401 answered by the probed account marks it `rejected`
    (reauthenticate); only a later success answered by that account, or a new token fingerprint, clears the mark; every
    other outcome keeps the previous state. The result file holds names, times, fingerprints and results, never a token."""
    def __init__(self, path, overview=None, run=None, clock=None, outcomes=None):
        self.path, self.overview, self.run, self.outcomes = path, overview, run, outcomes
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
        state, changed = load_probes(self.path), False
        try:
            from account_metadata import load
            import settings
            aliases = {row['name']: row.get('alias') or row['name'] for row in load(settings.CONFIG).get('accounts', [])}
        except Exception: aliases = {}
        for row in rows:
            name = row['name']
            fp = _store_fingerprint(name)
            if fp is None: continue
            last = state.get(name)
            if last and last['fingerprint'] != fp: last = None  # a new token: the old verdict does not apply
            if row.get('status') != 'cooling' or self.outcomes is None: continue
            started = self.clock()
            if last and started - last['at'] < PROBE_EVERY_MS: continue
            try: (self.run or self.default_run)(aliases.get(name, name))
            except Exception: pass
            try: found = verdict(name, self.outcomes(started))
            except Exception: found = None
            state[name] = {'at': started, 'fingerprint': fp, 'result': found or (last or {}).get('result', 'unknown')}
            changed = True
        if changed: write_private(self.path, json.dumps(state, sort_keys=True))
        return state


def default_local_accounts():
    import device_sync
    return device_sync.account_rows()


class Puller:
    def __init__(self, path, journal, remotes=Remote, interval=300, registry_every=3, local_accounts=None):
        self.path, self.journal, self.remotes = path, journal, remotes
        self.local_accounts = local_accounts or default_local_accounts
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
        if any(registry.ids(entry) for entry in shared['accounts'].values()):
            try: shared = localize(shared, self.local_accounts())
            except Exception: pass  # unreadable local accounts: the names stay as the owner sent them
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
