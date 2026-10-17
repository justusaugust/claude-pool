"""Usage readings move from the usage owner to the other devices. Only quota numbers travel, never identities or tokens.
device_sync is imported inside Puller.run_once: bridge -> readings -> device_sync -> accounts_cli -> bridge must not cycle."""
import json
import math
import threading
import time

from private_files import read_private, write_private
import registry
from remote import Remote

LIMIT = 65536
KEYS = ('fiveHour', 'sevenDay', 'fableWeekly')
ENTRY = {*KEYS, 'seenAt'}


def number(value): return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
def integer(value): return type(value) is int and value >= 0


def clean_window(value):
    if value is None: return None
    if not isinstance(value, dict) or not number(value.get('pct')) or not 0 <= value['pct'] <= 100: raise ValueError('bad window')
    reset = value.get('resetsAt')
    if reset is not None and not number(reset): raise ValueError('bad window')
    return {'pct': value['pct'], 'resetsAt': reset}


def check(value):
    """Return the shared reading in its exact shape, or None when it is not valid."""
    try:
        if not isinstance(value, dict) or set(value) != {'version', 'device', 'at', 'accounts'} or value['version'] != 1: return None
        if not registry.NAME.fullmatch(value['device'] if isinstance(value['device'], str) else '') or not integer(value['at']): return None
        if not isinstance(value['accounts'], dict): return None
        accounts = {}
        for name, entry in value['accounts'].items():
            if not isinstance(name, str) or not isinstance(entry, dict) or set(entry) != ENTRY or not integer(entry['seenAt']): return None
            accounts[name] = {**{key: clean_window(entry[key]) for key in KEYS}, 'seenAt': entry['seenAt']}
        return {'version': 1, 'device': value['device'], 'at': value['at'], 'accounts': accounts}
    except (ValueError, TypeError, KeyError): return None


def export(overview, device, now_ms):
    accounts = {}
    for row in overview.get('accounts', []):
        if not isinstance(row, dict) or not isinstance(row.get('name'), str) or row.get('readingSource'): continue
        seen = row.get('seenAt')
        entry = {}
        for key in KEYS:
            window = row.get(key)
            entry[key] = {'pct': window['pct'], 'resetsAt': window.get('resetsAt')} if isinstance(window, dict) and 'pct' in window else None
        entry['seenAt'] = seen if type(seen) is int and seen >= 0 else 0
        accounts[row['name']] = entry
    return {'version': 1, 'device': device, 'at': now_ms, 'accounts': accounts}


def merge(rows, shared, now_ms, max_age_ms=900_000):
    shared = check(shared)
    if not shared or not 0 <= now_ms - shared['at'] <= max_age_ms: return rows
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


class Puller:
    def __init__(self, path, journal, remotes=Remote, interval=300, registry_every=3):
        self.path, self.journal, self.remotes = path, journal, remotes
        self.interval, self.registry_every = interval, registry_every
        self.successes, self.failing = 0, False

    def fail(self, reason):
        if not self.failing: self.journal.record('readings_sync_failed')
        self.failing = True
        return False

    def run_once(self):
        try: value = registry.load()
        except ValueError: return False
        owner, me = registry.owner(value), registry.self_name()
        if not owner or not me or owner['name'] == me: return False
        if not owner.get('ssh'): return self.fail('no ssh target')
        remote = self.remotes(owner['ssh'])
        done = remote.pool('device', 'readings', timeout=60)
        if done.returncode != 0 or len(done.stdout) > LIMIT: return self.fail('pull failed')
        try: shared = check(json.loads(done.stdout))
        except ValueError: shared = None
        if not shared: return self.fail('bad reading')
        write_private(self.path, json.dumps(shared))
        self.failing = False
        self.successes += 1
        if self.successes % self.registry_every == 0:
            import device_sync
            try: device_sync.pull(remote)
            except Exception: pass  # sync reports its own problems
        return True

    def start(self, stop):
        def loop():
            while True:
                try: self.run_once()
                except Exception: self.fail('error')
                if stop.wait(self.interval): return
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread
