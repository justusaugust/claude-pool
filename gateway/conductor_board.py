"""What the Conductor Board shows at a glance, for `claude-pool conductor snapshot --json`: machines, accounts and quick
links. Cheap by design. The snapshot never runs a full `device check`: it reads the last check of each device from a 0600
cache that `device check` and `device add` write (record_checks) and that
`device check`, `device add` and `device sync` share with every peer they reach (share_checks, merge_checks: per device
the newest result wins, so a device with no ssh back to the Mac still shows the Mac's own result), checks this machine's own pool locally, and asks another
device for its `device status` at most once per PROBE_EVERY seconds, bounded by PROBE_TIMEOUT. A probe is cached whether
or not the device answered, and a device check younger than PROBE_EVERY counts as one. Accounts come from the overview
`claude-pool usage --once` reads (the local pool, which merges the usage owner's shared readings); no call per account."""
import concurrent.futures
import json
import re
import time

from account_metadata import load as load_config
from conductor_tools import _locked, clean
from device_report import STATUSES
from private_files import read_private, write_private
import readings
import registry
from remote import NO_LAUNCHER, OUTDATED, Remote
import settings

CHECK_FILE = 'device-check-cache.json'   # 0600, in settings.RUNTIME: the last device check result per device
PROBE_FILE = 'device-probe-cache.json'   # 0600, in settings.RUNTIME: the last cheap probe of each other device
PROBE_EVERY = 600                         # seconds between two probes of one device
PROBE_TIMEOUT = 20                        # seconds one `device status` over ssh may take
LIMIT = 256 * 1024
DAY_MS = 86_400_000                       # a shared checkedAt counts as at most one day ahead of this clock
MAX_CHECKED_AT = 2 ** 53 - 1              # larger is no time at all: refused (it would overflow checkedAt / 1000)
LIVE_STEPS = ('reachable', 'service', 'version')  # what a probe measures again; a newer probe replaces these cached steps
RANK = {'red': 0, 'amber': 1, 'grey': 2}
STEP_NAME = re.compile(r'[a-z][a-z-]{0,39}')
CUA_SPACES = range(18501, 18600)          # per-desktop shares: they need the ticket the index hands out, never linked directly
APP = {'label': 'Conductor in der Claude-App', 'link': 'https://claude.ai/code', 'kind': 'app'}
NO_BRAKE = {'hold': None, 'stage': None, 'waiting': []}
WINDOWS = (('fiveHour', 'fiveHour', 'fiveHourPct'), ('sevenDay', 'week', 'weekPct'), ('fableWeekly', 'fableWeek', 'fableWeekPct'))


def _path(name): return settings.RUNTIME / name


def _load(path):
    try: value = read_private(path, LIMIT)
    except (OSError, ValueError, RuntimeError): return {}
    devices = value.get('devices')
    return {name: entry for name, entry in devices.items() if isinstance(entry, dict)} if isinstance(devices, dict) else {}


def _checked_at(entry, cap=MAX_CHECKED_AT):
    """The entry's checkedAt when it is a sane integer time (ms) no later than `cap`, else None."""
    value = entry.get('checkedAt') if isinstance(entry, dict) else None
    return value if type(value) is int and 0 <= value <= cap else None


def _store(path, entries, newer=False, keep=None, cap=MAX_CHECKED_AT):
    """Write `entries` into the cache under its lock; with `newer`, only a device whose entry is newer than the kept one
    (a kept checkedAt beyond `cap` counts as none, so an old poisoned entry cannot win forever). With `keep`, entries of
    any other device are dropped from the cache. Returns the names written."""
    with _locked(path):
        current = _load(path)
        stale = [name for name in current if keep is not None and name not in keep]
        for name in stale: del current[name]
        written = sorted(name for name, entry in entries.items()
                         if not newer or entry['checkedAt'] > (_checked_at(current.get(name), cap) or -1))
        if written or stale:
            current.update({name: entries[name] for name in written})
            write_private(path, json.dumps({'devices': current}, ensure_ascii=False, sort_keys=True))
    return written


def _step(name, status, detail, fix):
    return {'name': name, 'status': status, 'detail': clean(detail or '', 200), 'fix': clean(fix or '', 500)}


def record_checks(results, now=None):
    """Keep the last `device check` / `device add` result per device (redacted, 0600). A device this machine could only
    skip (no ssh target from here) keeps its previous entry. Never raises: the check's own output is what counts."""
    at = int((time.time() if now is None else now) * 1000)
    entries = {}
    for name, steps in results.items():
        steps = [step for step in steps if step is not None]
        if not steps or all(step.status == 'skipped' for step in steps): continue
        entries[name] = {'checkedAt': at, 'steps': [_step(step.name, step.status, step.detail, step.fix) for step in steps]}
    if not entries: return
    try: _store(_path(CHECK_FILE), entries)
    except (OSError, ValueError, RuntimeError): pass


def checked_entries(value, now=None):
    """The `{devices: {name: {checkedAt, steps}}}` another device shares, validated and redacted; ValueError otherwise.
    checkedAt must be an integer from 0 to MAX_CHECKED_AT and is kept at most one day ahead of `now`."""
    ceiling = int((time.time() if now is None else now) * 1000) + DAY_MS
    devices = value.get('devices') if isinstance(value, dict) else None
    if not isinstance(devices, dict) or len(devices) > 64: raise ValueError('checks must be {"devices": {name: entry}}')
    out = {}
    for name, entry in devices.items():
        if not isinstance(name, str) or not registry.NAME.fullmatch(name): raise ValueError('invalid device name in checks')
        if _checked_at(entry) is None: raise ValueError('checks need an integer checkedAt (ms) per device')
        steps = entry.get('steps')
        if not isinstance(steps, list) or len(steps) > 64: raise ValueError('checks need a list of steps per device')
        rows = []
        for step in steps:
            if (not isinstance(step, dict) or not isinstance(step.get('name'), str) or not STEP_NAME.fullmatch(step['name'])
                    or step.get('status') not in STATUSES
                    or not all(isinstance(step.get(key, ''), str) for key in ('detail', 'fix'))):
                raise ValueError('invalid step in checks')
            rows.append(_step(step['name'], step['status'], step.get('detail'), step.get('fix')))
        out[name] = {'checkedAt': min(entry['checkedAt'], ceiling), 'steps': rows}
    return out


def merge_checks(value, now=None):
    """`device receive-checks`: merge the results another device shares; per device the newest checkedAt wins. Only devices
    in this registry are taken, and the cache is pruned to them, so it cannot grow past the registry. Returns the names that
    changed; raises ValueError on input that is not a check result (nothing is written then)."""
    entries = checked_entries(value, now)
    devices = {row['name'] for row in registry.load()['devices']}
    ceiling = int((time.time() if now is None else now) * 1000) + DAY_MS
    return _store(_path(CHECK_FILE), {name: entry for name, entry in entries.items() if name in devices},
                  newer=True, keep=devices, cap=ceiling)


def share_checks(remotes, skip=()):
    """Send this device's whole check cache to every other registry device it has an ssh target for (except `skip`, the
    ones just found unreachable), so a device with no ssh back here still shows this one. Best effort, never raises:
    {device: 'ok' | 'unreachable' | 'failed'}."""
    try:
        value, me = registry.load(), registry.self_name()
        cached = _load(_path(CHECK_FILE))
    except (OSError, ValueError, RuntimeError): return {}
    peers = [row for row in value['devices'] if row['name'] != me and row.get('ssh') and row['name'] not in skip]
    if not cached or not peers: return {}
    payload = json.dumps({'devices': cached}, ensure_ascii=False, sort_keys=True).encode()

    def send(row):
        try:
            done = remotes(row['ssh']).device('receive-checks', stdin=payload, timeout=PROBE_TIMEOUT)
            return 'ok' if done.returncode == 0 else 'unreachable' if done.returncode == 255 else 'failed'
        except Exception: return 'failed'
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
        return dict(zip([row['name'] for row in peers], pool.map(send, peers)))


def _from_steps(steps):
    """The live facts a recent device check already measured, or None when it measured too little to skip a probe."""
    found = {step.get('name'): step.get('status') for step in steps}
    reachable = {'ok': True, 'unreachable': False}.get(found.get('reachable'))
    if reachable is False: return {'reachable': False, 'poolRunning': None, 'versionMatches': None}
    if reachable is None or 'service' not in found or found.get('version') not in ('ok', 'missing'): return None
    return {'reachable': True, 'poolRunning': found['service'] == 'ok', 'versionMatches': found['version'] == 'ok'}


def _facts(steps):
    """What a check result says about reachable, pool and version, each None when it did not say."""
    found = {step.get('name'): step.get('status') for step in steps}
    return {'reachable': {'ok': True, 'unreachable': False}.get(found.get('reachable')),
            'poolRunning': None if 'service' not in found else found['service'] == 'ok',
            'versionMatches': {'ok': True, 'missing': False, 'failed': False}.get(found.get('version'))}


class Board:
    """The facts the board shows besides herdr. Every outside call is injectable, so tests use fakes only."""
    def __init__(self, remotes=None, service_state=None, version=None, overview=None, account_rows=None):
        import device_sync
        self.remotes = remotes or Remote
        self.service_state = service_state or device_sync.service_state
        self.version = version or device_sync.build_version
        self.overview = overview or device_sync.pool_overview
        self.account_rows = account_rows or device_sync.account_rows
        self._seen = (-1.0, None)

    def pool(self):
        """The local pool overview; accounts and brake share one read per snapshot (at most every five seconds)."""
        at, value = self._seen
        if time.monotonic() - at > 5:
            try: value = self.overview()
            except Exception: value = None
            self._seen = (time.monotonic(), value)
        return value

    # machines --------------------------------------------------------------------------------------------------------

    def _probe(self, row, wanted):
        try: remote = self.remotes(row['ssh'])
        except ValueError: return {'reachable': None, 'poolRunning': None, 'versionMatches': None, 'note': 'invalid ssh target'}
        done = remote.device('status', timeout=PROBE_TIMEOUT)
        if done.returncode == 255:
            if (done.stderr or b'').strip() == b'timed out':
                return {'reachable': None, 'poolRunning': None, 'versionMatches': None, 'note': 'slow'}
            return {'reachable': False, 'poolRunning': None, 'versionMatches': None}
        if done.returncode in (OUTDATED, NO_LAUNCHER): return {'reachable': True, 'poolRunning': None, 'versionMatches': False}
        try: value = json.loads(done.stdout) if done.returncode == 0 else None
        except ValueError: value = None
        if not isinstance(value, dict): return {'reachable': True, 'poolRunning': None, 'versionMatches': None}
        have = value.get('version')
        return {'reachable': True, 'poolRunning': value.get('service') == 'running',
                'versionMatches': None if not wanted or not have else have == wanted}

    def machines(self, now):
        try: value = registry.load()
        except ValueError: return []
        me, wanted = registry.self_name(), None
        try: wanted = self.version()
        except Exception: pass
        checks, probes = _load(_path(CHECK_FILE)), _load(_path(PROBE_FILE))
        live, due = {}, []
        for row in value['devices']:
            name = row['name']
            check = checks.get(name) or {}
            if name == me:
                try: running, unknown = self.service_state() == 'running', False
                except Exception: running, unknown = None, True
                live[name] = {'reachable': True, 'poolRunning': running, 'versionMatches': True, 'at': now, 'serviceUnknown': unknown}
                continue
            if not row.get('ssh'): continue
            candidates = []
            probe = probes.get(name)
            if probe and isinstance(probe.get('at'), (int, float)): candidates.append(probe)
            measured = _from_steps(check.get('steps') or []) if _checked_at(check) is not None else None
            if measured: candidates.append({**measured, 'at': check['checkedAt'] / 1000})
            fresh = max(candidates, key=lambda entry: entry['at'], default=None)
            if fresh and now - fresh['at'] < PROBE_EVERY: live[name] = fresh
            else: due.append(row)
        if due:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(due))) as pool:
                answers = dict(zip([row['name'] for row in due], pool.map(lambda row: self._probe(row, wanted), due)))
            entries = {name: {**answer, 'at': now} for name, answer in answers.items()}
            live.update(entries)
            try: _store(_path(PROBE_FILE), entries)
            except (OSError, ValueError, RuntimeError): pass
        entry = registry.conductor(value)
        return [self._machine(row, row['name'] == me, live.get(row['name']), checks.get(row['name']) or {},
                              entry is not None and entry['device'] == row['name']) for row in value['devices']]

    def _machine(self, row, mine, live, check, conductor_here=False):
        name, ssh = row['name'], row.get('ssh')
        checked = _checked_at(check)
        steps = [step for step in check.get('steps') or [] if isinstance(step, dict)]
        if live is None and checked is not None:
            live = _facts(steps)  # no ssh from here: the result the device itself (or another peer) shared is all there is
        if live is not None and checked is not None and (mine or live.get('at') is None or live['at'] * 1000 >= checked):
            steps = [step for step in steps if step.get('name') not in LIVE_STEPS]
        live = live or {'reachable': None, 'poolRunning': None, 'versionMatches': None}
        issues = []  # (light, code, reason, fix) in the order they are found; the worst light leads
        start_fix = 'claude-pool --start' if mine else 'claude-pool device add ' + str(ssh)
        if live.get('reachable') is False:
            issues.append(('red', 'unreachable', 'Not reachable over ssh.', 'ssh ' + str(ssh) + ' true must work without a password prompt (BatchMode)'))
        elif live.get('note') == 'slow':
            issues.append(('grey', 'not-checkable', 'It did not answer within ' + str(PROBE_TIMEOUT) + ' s.', 'claude-pool device check ' + name))
        elif not mine and not ssh and checked is None:
            issues.append(('grey', 'not-checkable', 'Not checked from here: this machine has no ssh to it and it shared no result yet.',
                           'claude-pool device check ' + name + ' on that machine'))
        if live.get('poolRunning') is False: issues.append(('red', 'pool-stopped', 'The pool is not running.', start_fix))
        if mine and live.get('serviceUnknown') and checked is not None:  # unchecked: never-checked below says enough
            issues.append(('grey', 'pool-stopped', 'Could not read whether the pool runs on this machine.', 'claude-pool device check ' + name))
        if live.get('versionMatches') is False:
            issues.append(('amber', 'version-mismatch', 'It runs another claude-pool version.', 'claude-pool device add ' + str(ssh)))
        for step in steps:
            status, label, detail, fix = step.get('status'), step.get('name', 'step'), step.get('detail') or '', step.get('fix') or ''
            if status in ('ok', 'skipped'): continue
            if label == 'native-login' and status == 'manual' and not conductor_here: continue  # older results: not needed here
            if label == 'reachable' and status == 'unreachable': issues.append(('red', 'unreachable', 'Not reachable over ssh.', fix))
            elif label == 'service': issues.append(('red', 'pool-stopped', 'The pool is not running: ' + detail, fix or start_fix))
            elif label == 'version' and status == 'missing': issues.append(('amber', 'version-mismatch', 'It runs another claude-pool version: ' + detail, fix))
            elif status in ('failed', 'unreachable'): issues.append(('red', 'failed:' + label, label + ' failed: ' + detail, fix))
            elif status == 'missing': issues.append(('amber', 'missing:' + label, label + ' needs work: ' + detail, fix))
            elif status == 'manual': issues.append(('amber', 'manual:' + label, label + ' needs you: ' + detail, fix))
        if checked is None and (mine or ssh):
            issues.append(('grey', 'never-checked', 'Never checked with claude-pool device check.', 'claude-pool device check ' + name))
        worst = min(issues, key=lambda issue: RANK[issue[0]], default=None)
        return {'name': name, 'platform': row.get('platform') or '', 'self': mine, 'reachable': live.get('reachable'),
                'poolRunning': live.get('poolRunning'), 'versionMatches': live.get('versionMatches'),
                'light': worst[0] if worst else 'green', 'code': worst[1] if worst else 'ok',
                'reason': clean(worst[2], 200) if worst else '', 'fix': clean(worst[3], 500) if worst else '', 'checkedAt': checked}

    # accounts --------------------------------------------------------------------------------------------------------

    def accounts(self, now):
        overview = self.pool()
        try: config = {row['name']: row for row in load_config(settings.CONFIG).get('accounts', [])}
        except (OSError, ValueError): config = {}
        try: local = {row['name']: row for row in self.account_rows() if isinstance(row, dict) and isinstance(row.get('name'), str)}
        except Exception: local = {}
        try: value = registry.load()
        except ValueError: value = registry.EMPTY()
        try: rejected = readings.reauthenticate(settings.LONG_LIVED_PROBES)
        except Exception: rejected = set()
        sessions = None
        if isinstance(overview, dict) and isinstance(overview.get('accounts'), list):
            rows = [row for row in overview['accounts'] if isinstance(row, dict) and isinstance(row.get('name'), str)]
            sessions = [row for row in overview.get('sessions') or [] if isinstance(row, dict)]
        else:  # the pool does not answer: the configured accounts, with the usage owner's last shared readings if fresh
            rows = [{'name': name, 'label': row.get('label') or name, 'status': 'unknown', 'seenAt': 0, 'fiveHour': None,
                     'sevenDay': None, 'fableWeekly': None} for name, row in config.items()]
            try: readings.merge(rows, readings.load_shared(settings.SHARED_READINGS), int(now * 1000))
            except Exception: pass
        out = []
        for row in rows:
            name = row['name']
            own, held = config.get(name) or {}, local.get(name) or {}
            entry = registry.entry_for(value, held or {'name': name})
            kind = held.get('servingKind') or held.get('kind')
            kind = kind if kind in ('long-lived', 'refreshable') else None
            reserve = row.get('reserveAt') or own.get('reserveAt') or None
            account = {'name': clean(name, 80), 'label': clean(row.get('label') or own.get('label') or name, 80),
                       'alias': clean(own.get('alias') or name, 80),
                       'registryName': clean(entry['name'], 80) if entry and entry['name'] != name else None,
                       'resetsAt': {}, 'reserveAt': reserve if type(reserve) is int else None, 'kind': kind,
                       'expiresAt': held.get('expiresAt') if kind == 'long-lived' and type(held.get('expiresAt')) is int else None,
                       'status': 'reauthenticate' if name in rejected else clean(row.get('status') or 'unknown', 20)}
            for key, short, pct in WINDOWS:
                window = row.get(key) if isinstance(row.get(key), dict) else None
                number = window.get('pct') if window and readings.number(window.get('pct')) else None
                account[pct] = round(number, 1) if number is not None else None
                reset = window.get('resetsAt') if window else None
                account['resetsAt'][short] = int(reset) if readings.number(reset) else None
            if sessions is not None: account['agents'] = sum(1 for session in sessions if session.get('account') == name)
            out.append({key: account[key] for key in ('name', 'label', 'alias', 'registryName', 'fiveHourPct', 'weekPct', 'fableWeekPct',
                                                       'resetsAt', 'reserveAt', 'kind', 'expiresAt', 'status', 'agents') if key in account})
        return out


    # brake -----------------------------------------------------------------------------------------------------------

    def brake(self, now):
        """Whether the brake holds background sessions and which ones wait ("wartet wegen der Bremse"); None when the pool
        does not answer."""
        overview = self.pool()
        if not isinstance(overview, dict): return dict(NO_BRAKE)
        hold = overview.get('brake') if isinstance(overview.get('brake'), dict) else {}
        stage = ((overview.get('forecast') or {}).get('brake') or {}).get('stage')
        waiting = []
        for row in overview.get('sessions') or []:
            mark = row.get('brakeWaiting') if isinstance(row, dict) else None
            if not isinstance(mark, dict) or type(mark.get('since')) is not int: continue
            waiting.append({'session': clean(str(row.get('session') or ''), 80), 'project': clean(str(row.get('project') or ''), 60),
                            'since': mark['since'], 'requests': mark['requests'] if type(mark.get('requests')) is int else 1})
        return {'hold': hold.get('hold') is True, 'stage': stage if type(stage) is int else None,
                'waiting': sorted(waiting, key=lambda row: row['since'])}


def quick_access(previews, value):
    """The Cua desktops index first, then every other preview as `preview list --json` reports it, then the Conductor in
    the Claude app when one is configured. Never a per-desktop share (18501 and up): those need the index's ticket."""
    rows = [row for row in previews or [] if row.get('link')]
    cua = [row for row in rows if row.get('kind') == 'cua']
    other = [row for row in rows if row.get('kind') not in ('cua', 'cua-space') and row.get('port') not in CUA_SPACES]
    out = [{'label': row.get('label') or 'Cua desktops', 'link': row['link'], 'kind': 'cua'} for row in cua]
    out += [{'label': row.get('label') or str(row.get('port') or ''), 'link': row['link'], 'kind': 'preview'} for row in other]
    if value is not None and registry.conductor(value): out.append(dict(APP))
    return out


def facts(board, now, previews):
    """The board keys of the snapshot (without the queue); a part that fails is empty and never blanks the others."""
    def part(read):
        try: return read()
        except Exception: return []
    try: value = registry.load()
    except Exception: value = None
    return {'machines': part(lambda: board.machines(now)), 'accounts': part(lambda: board.accounts(now)),
            'quickAccess': part(lambda: quick_access(previews, value)), 'brake': part(lambda: board.brake(now)) or dict(NO_BRAKE)}
