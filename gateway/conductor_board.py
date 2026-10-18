"""What the Conductor Board shows at a glance, for `claude-pool conductor snapshot --json`: machines, accounts and quick
links. Cheap by design. The snapshot never runs a full `device check`: it reads the last check of each device from a 0600
cache that `device check` and `device add` write (record_checks), checks this machine's own pool locally, and asks another
device for its `device status` at most once per PROBE_EVERY seconds, bounded by PROBE_TIMEOUT. A probe is cached whether
or not the device answered, and a device check younger than PROBE_EVERY counts as one. Accounts come from the overview
`claude-pool usage --once` reads (the local pool, which merges the usage owner's shared readings); no call per account."""
import concurrent.futures
import json
import time

from account_metadata import load as load_config
from conductor_tools import _locked, clean
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
LIVE_STEPS = ('reachable', 'service', 'version')  # what a probe measures again; a newer probe replaces these cached steps
RANK = {'red': 0, 'amber': 1}
CUA_SPACES = range(18501, 18600)          # per-desktop shares: they need the ticket the index hands out, never linked directly
APP = {'label': 'Conductor in der Claude-App', 'link': 'https://claude.ai/code', 'kind': 'app'}
WINDOWS = (('fiveHour', 'fiveHour', 'fiveHourPct'), ('sevenDay', 'week', 'weekPct'), ('fableWeekly', 'fableWeek', 'fableWeekPct'))


def _path(name): return settings.RUNTIME / name


def _load(path):
    try: value = read_private(path, LIMIT)
    except (OSError, ValueError, RuntimeError): return {}
    devices = value.get('devices')
    return {name: entry for name, entry in devices.items() if isinstance(entry, dict)} if isinstance(devices, dict) else {}


def _store(path, entries):
    with _locked(path):
        current = _load(path)
        current.update(entries)
        write_private(path, json.dumps({'devices': current}, ensure_ascii=False, sort_keys=True))


def record_checks(results, now=None):
    """Keep the last `device check` / `device add` result per device (redacted, 0600). A device this machine could only
    skip (no ssh target from here) keeps its previous entry. Never raises: the check's own output is what counts."""
    at = int((time.time() if now is None else now) * 1000)
    entries = {}
    for name, steps in results.items():
        steps = [step for step in steps if step is not None]
        if not steps or all(step.status == 'skipped' for step in steps): continue
        entries[name] = {'checkedAt': at, 'steps': [{'name': step.name, 'status': step.status, 'detail': clean(step.detail or '', 200),
                                                     'fix': clean(step.fix or '', 500)} for step in steps]}
    if not entries: return
    try: _store(_path(CHECK_FILE), entries)
    except (OSError, ValueError, RuntimeError): pass


def _from_steps(steps):
    """The live facts a recent device check already measured, or None when it measured too little to skip a probe."""
    found = {step.get('name'): step.get('status') for step in steps}
    reachable = {'ok': True, 'unreachable': False}.get(found.get('reachable'))
    if reachable is False: return {'reachable': False, 'poolRunning': None, 'versionMatches': None}
    if reachable is None or 'service' not in found or found.get('version') not in ('ok', 'missing'): return None
    return {'reachable': True, 'poolRunning': found['service'] == 'ok', 'versionMatches': found['version'] == 'ok'}


class Board:
    """The facts the board shows besides herdr. Every outside call is injectable, so tests use fakes only."""
    def __init__(self, remotes=None, service_state=None, version=None, overview=None, account_rows=None):
        import device_sync
        self.remotes = remotes or Remote
        self.service_state = service_state or device_sync.service_state
        self.version = version or device_sync.build_version
        self.overview = overview or device_sync.pool_overview
        self.account_rows = account_rows or device_sync.account_rows

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
                try: running = self.service_state() == 'running'
                except Exception: running = None
                live[name] = {'reachable': True, 'poolRunning': running, 'versionMatches': True, 'at': now}
                continue
            if not row.get('ssh'): continue
            candidates = []
            probe = probes.get(name)
            if probe and isinstance(probe.get('at'), (int, float)): candidates.append(probe)
            measured = _from_steps(check.get('steps') or []) if isinstance(check.get('checkedAt'), int) else None
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
        return [self._machine(row, row['name'] == me, live.get(row['name']), checks.get(row['name']) or {}) for row in value['devices']]

    def _machine(self, row, mine, live, check):
        name, ssh = row['name'], row.get('ssh')
        checked = check.get('checkedAt') if isinstance(check.get('checkedAt'), int) else None
        steps = [step for step in check.get('steps') or [] if isinstance(step, dict)]
        live = live or {'reachable': None, 'poolRunning': None, 'versionMatches': None}
        if mine or (checked is not None and live.get('at') is not None and live['at'] * 1000 >= checked):
            steps = [step for step in steps if step.get('name') not in LIVE_STEPS]
        issues = []  # (light, reason, fix) in the order they are found; the worst one leads
        if live.get('reachable') is False:
            issues.append(('red', 'Not reachable over ssh.', 'ssh ' + str(ssh) + ' true must work without a password prompt (BatchMode)'))
        elif live.get('note') == 'slow':
            issues.append(('amber', 'It did not answer within ' + str(PROBE_TIMEOUT) + ' s.', 'claude-pool device check ' + name))
        elif not mine and not ssh and live.get('reachable') is None:
            issues.append(('amber', 'Not checked from here: this machine has no ssh to it.', 'claude-pool device check ' + name + ' on that machine'))
        if live.get('poolRunning') is False:
            issues.append(('red', 'The pool is not running.', 'claude-pool --start' if mine else 'claude-pool device add ' + str(ssh)))
        if live.get('versionMatches') is False:
            issues.append(('amber', 'It runs another claude-pool version.', 'claude-pool device add ' + str(ssh)))
        for step in steps:
            status, label, detail = step.get('status'), step.get('name', 'step'), step.get('detail') or ''
            if status in ('failed', 'unreachable'): issues.append(('red', label + ' failed: ' + detail, step.get('fix') or ''))
            elif status == 'missing': issues.append(('amber', label + ' needs work: ' + detail, step.get('fix') or ''))
            elif status == 'manual': issues.append(('amber', label + ' needs you: ' + detail, step.get('fix') or ''))
        if checked is None and not issues and (mine or ssh):
            issues.append(('amber', 'Never checked with claude-pool device check.', 'claude-pool device check ' + name))
        worst = min(issues, key=lambda issue: RANK[issue[0]], default=None)
        return {'name': name, 'platform': row.get('platform') or '', 'self': mine, 'reachable': live.get('reachable'),
                'poolRunning': live.get('poolRunning'), 'versionMatches': live.get('versionMatches'),
                'light': worst[0] if worst else 'green', 'reason': clean(worst[1], 200) if worst else '',
                'fix': clean(worst[2], 500) if worst else '', 'checkedAt': checked}

    # accounts --------------------------------------------------------------------------------------------------------

    def accounts(self, now):
        try: overview = self.overview()
        except Exception: overview = None
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
    """The board keys of the snapshot (without the queue)."""
    try: value = registry.load()
    except ValueError: value = None
    return {'machines': board.machines(now), 'accounts': board.accounts(now), 'quickAccess': quick_access(previews, value)}
