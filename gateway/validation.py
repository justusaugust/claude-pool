"""Saved forecasts are scored against later readings, never refitted with them."""
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
import os
import sqlite3
import hashlib
import threading
import time
from contextlib import closing
from pathlib import Path
from forecast import estimate

WINDOWS = ('fiveHour', 'sevenDay')
FRESH = 600000


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def archive(folder, accounts, forecast, now, source='live'):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    # Explicit fields exclude IDs, conversations, signatures and credentials.
    rows = [{k: a.get(k) for k in ('name', 'label', 'status', 'seenAt', *WINDOWS)} for a in accounts]
    saved_forecast = {k: forecast.get(k) for k in ('seconds', 'threshold', 'partial')}
    saved_forecast['accounts'] = [{'name': a['name'], 'windows': {
        key: {'ratePerHour': a['windows'][key].get('ratePerHour')} for key in WINDOWS if key in a.get('windows', {})}}
        for a in forecast.get('accounts', [])]
    if forecast.get('brake'):
        saved_forecast['brake'] = {k: forecast['brake'].get(k) for k in ('stage', 'ratio', 'resetAt')}
    if forecast.get('renewals'):
        saved_forecast['renewals'] = {'segments': [{k: s[k] for k in ('at', 'until', 'available', 'uncertain')}
                                                   for s in forecast['renewals']['segments']]}
    value = {'at': now, 'source': source, 'accounts': rows, 'forecast': saved_forecast}
    path = folder / (datetime.fromtimestamp(now / 1000, timezone.utc).strftime('%Y-%m') + '.jsonl')
    with path.open('a+b') as file:
        file.seek(0, 2)
        if file.tell():
            file.seek(-1, 2)
            if file.read(1) != b'\n': file.write(b'\n')
        file.write((json.dumps(value, allow_nan=False, separators=(',', ':')) + '\n').encode())
        file.flush(); os.fsync(file.fileno())


def parse_row(line):
    try:
        row = json.loads(line)
        if not (finite(row['at'])): raise ValueError()
        if not (row['source'] in ('live', 'replay') and isinstance(row['forecast'], dict)): raise ValueError()
        forecast = row['forecast']
        if not (forecast['seconds'] is None or (finite(forecast['seconds']) and forecast['seconds'] >= 0)): raise ValueError()
        if not (finite(forecast['threshold']) and 0 < forecast['threshold'] <= 100): raise ValueError()
        if not (isinstance(forecast['partial'], bool) and isinstance(forecast['accounts'], list)): raise ValueError()
        for speed in forecast['accounts']:
            if not (isinstance(speed['name'], str) and isinstance(speed['windows'], dict)): raise ValueError()
            for w in speed['windows'].values():
                if not (w['ratePerHour'] is None or (finite(w['ratePerHour']) and w['ratePerHour'] >= 0)): raise ValueError()
        if not (isinstance(row['accounts'], list) and row['accounts']): raise ValueError()
        for a in row['accounts']:
            if not (isinstance(a['name'], str) and finite(a['seenAt'])): raise ValueError()
            if not (isinstance(a['label'], str) and isinstance(a['status'], str)): raise ValueError()
            for key in WINDOWS:
                w = a.get(key)
                if not (w is None or (finite(w['pct']) and 0 <= w['pct'] <= 100 and (w['resetsAt'] is None or finite(w['resetsAt'])))): raise ValueError()
        if 'renewals' in forecast:
            segments = forecast['renewals']['segments']
            if not isinstance(segments, list) or len(segments) > 288: raise ValueError()
            end = row['at']
            names = {a['name'] for a in row['accounts']}
            for segment in segments:
                if not finite(segment['at']) or not finite(segment['until']) or segment['at'] != end or not end < segment['until'] <= row['at'] + 86400000: raise ValueError()
                for key in ('available', 'uncertain'):
                    if not isinstance(segment[key], list) or not all(isinstance(n, str) and n in names for n in segment[key]): raise ValueError()
                if set(segment['available']) & set(segment['uncertain']): raise ValueError()
                end = segment['until']
        return row
    except (ValueError, TypeError, KeyError): return None


def load(folder):
    rows, rejected = [], 0
    for path in sorted(Path(folder).glob('*.jsonl')):
        with path.open() as file:
            for line in file:
                if not line.strip(): continue
                row = parse_row(line)
                if row is None: rejected += 1
                else: rows.append(row)
    return sorted({(r['at'], r['source']): r for r in rows}.values(), key=lambda r: r['at']), rejected


def seed(folder, evidence):
    if any(Path(folder).glob('*.jsonl')) or not Path(evidence).exists(): return
    histories = json.loads(Path(evidence).read_text())['history']
    observations, accounts = defaultdict(list), {}
    labels = {'Gmail': 'personal', 'Work': 'work'}
    timeline = sorted((p['at'], h['label'], p) for h in histories for p in h['samples'] if h['label'] in labels)
    for now, label, point in timeline:
        name = labels[label]
        observations[name].append(point)
        accounts[name] = {'id': name, 'name': name, 'label': label, 'seenAt': now, 'status': 'unknown',
                          **{key: point.get(key) for key in WINDOWS}}
        # Historical quota snapshots do not establish historical routing availability.
        archive(folder, list(accounts.values()), estimate(list(accounts.values()), observations, now), now, source='replay')


def evaluate(rows, origins=None):
    chosen = rows if origins is None else [r for r in rows if origins[0] <= r["at"] < origins[1]]
    targets, seen = defaultdict(dict), set()
    resets = set()
    for row in rows:
        for a in row['accounts']:
            if 0 <= row['at'] - a['seenAt'] <= FRESH:
                targets[a['name']][a['seenAt']] = a
    indexed = {}
    for name, samples in targets.items():
        times = sorted(samples)
        indexed[name] = (times, samples)
        for t1, t2 in zip(times, times[1:]):
            for key in WINDOWS:
                before, after = samples[t1].get(key), samples[t2].get(key)
                if before and after and before.get('resetsAt') and after.get('resetsAt'):
                    if abs(before['resetsAt'] - after['resetsAt']) > 1000 and t1 < before['resetsAt'] <= t2:
                        resets.add((name, key, before['resetsAt']))
    errors, folds = defaultdict(list), []
    for row in chosen:
        speeds = {a['name']: a.get('windows', {}) for a in row['forecast'].get('accounts', [])}
        for a in row['accounts']:
            if not 0 <= row['at'] - a['seenAt'] <= FRESH: continue
            times, samples = indexed.get(a['name'], ([], {}))
            for key in WINDOWS:
                w = a.get(key)
                rate = speeds.get(a['name'], {}).get(key, {}).get('ratePerHour')
                if not w or not w.get('resetsAt') or w['resetsAt'] <= row['at'] or rate is None: continue
                identity = (row['source'], a['name'], key, a['seenAt'])
                if identity in seen: continue
                seen.add(identity)
                for hours in (1, 5, 24):
                    desired = row['at'] + hours * 3600000
                    i = bisect_left(times, desired)
                    if i == len(times) or times[i] - desired > FRESH: continue
                    at, target = times[i], samples[times[i]].get(key)
                    if not target or not target.get('resetsAt') or target['resetsAt'] <= at: continue
                    renewed = abs(target['resetsAt'] - w['resetsAt']) > 1000
                    if renewed and not w['resetsAt'] <= at: continue
                    if not renewed and w['pct'] - target['pct'] >= 2: continue
                    # Score only the first renewal: later reset anchors are not known at origin.
                    if renewed and at - w['resetsAt'] >= (5 if key == 'fiveHour' else 168) * 3600000: continue
                    elapsed = (at - a['seenAt']) / 3600000
                    frozen = min(100, w['pct'] + rate * elapsed)
                    predicted = min(100, rate * (at - w['resetsAt']) / 3600000) if renewed else frozen
                    flat = 0 if renewed else w['pct']
                    group = (row['source'], a['label'], key, hours, renewed)
                    values = [abs(predicted - target['pct']), abs(frozen - target['pct']), abs(flat - target['pct'])]
                    errors[group].append(values)
                    folds.append({'source': row['source'], 'account': a['label'], 'window': key, 'hours': hours,
                                  'originAt': row['at'], 'targetAt': at, 'crossedReset': renewed,
                                  'prediction': predicted, 'withoutReset': frozen, 'flat': flat, 'actual': target['pct']})

    outcomes, completed = Counter(), []
    live = [r for r in rows if r['source'] == 'live']
    for origin in [r for r in chosen if r['source'] == 'live']:
        i = bisect_left([r['at'] for r in live], origin['at'])
        forecast = origin['forecast']
        if forecast.get('seconds') is None:
            outcomes['insufficient pace'] += 1; continue
        if forecast.get('partial'):
            outcomes['partial forecast'] += 1; continue
        accounts = {a['name']: a for a in origin['accounts']}
        if any(a['status'] not in ('ready', 'limited') or not 0 <= origin['at'] - a['seenAt'] <= FRESH for a in accounts.values()):
            outcomes['incomplete readings'] += 1; continue
        previous_at, outcome = origin['at'], 'pending'
        available = [a['seenAt'] for a in accounts.values() if all(a.get(key) and a[key]['pct'] < forecast['threshold'] for key in WINDOWS)]
        if not available:
            outcomes['incomplete readings'] += 1; continue
        available_at = max(available)
        for future in live[i + 1:]:
            actual = {a['name']: a for a in future['accounts']}
            if set(actual) != set(accounts) or any(a['status'] not in ('ready', 'limited') for a in actual.values()):
                outcome = 'routing changed'; break
            if any(w and w.get('resetsAt') and future['at'] >= w['resetsAt'] for a in accounts.values() for key in WINDOWS if (w := a.get(key))):
                outcome = 'reset censored'; break
            if future['at'] - previous_at > FRESH or any(not 0 <= future['at'] - a['seenAt'] <= FRESH for a in actual.values()):
                outcome = 'sampling gap'; break
            if any(abs(actual[name][key]['resetsAt'] - a[key]['resetsAt']) > 1000 or a[key]['pct'] - actual[name][key]['pct'] >= 2
                   for name, a in accounts.items() for key in WINDOWS if a.get(key) and actual[name].get(key) and a[key].get('resetsAt') and actual[name][key].get('resetsAt')):
                outcome = 'quota correction'; break
            if any(not a.get(key) for a in actual.values() for key in WINDOWS):
                outcome = 'incomplete readings'; break
            # Availability means quota below the switching threshold, not a pause/cooldown.
            if all(any(a[key]['pct'] >= forecast['threshold'] for key in WINDOWS) for a in actual.values()):
                low = max(0, (available_at - origin['at']) / 1000)
                high = max(0, (max(a['seenAt'] for a in actual.values()) - origin['at']) / 1000)
                error = max(low - forecast['seconds'], forecast['seconds'] - high, 0)
                completed.append({'originAt': origin['at'], 'predictedSeconds': forecast['seconds'],
                                  'actualSecondsInterval': [low, high], 'errorSeconds': error})
                outcome = 'observed depletion'; break
            previous_at = future['at']
            available_at = max(a['seenAt'] for a in actual.values() if all(a[key]['pct'] < forecast['threshold'] for key in WINDOWS))
        outcomes[outcome] += 1
    groups = [{'source': g[0], 'account': g[1], 'window': g[2], 'hours': g[3], 'crossedReset': g[4], 'samples': len(v),
               'maePoints': sum(e[0] for e in v) / len(v), 'withoutResetMaePoints': sum(e[1] for e in v) / len(v),
               'flatMaePoints': sum(e[2] for e in v) / len(v)} for g, v in sorted(errors.items())]
    renewal_checks = []
    live_times = [r['at'] for r in live]
    for origin in [r for r in chosen if r['source'] == 'live']:
        scenario = origin['forecast'].get('renewals')
        if not scenario: continue
        for hours in (1, 5, 24):
            target = origin['at'] + hours * 3600000
            span = next((s for s in scenario['segments'] if s['at'] <= target < s['until']), None)
            index = bisect_left(live_times, target)
            future = live[index] if index < len(live) and live[index]['at'] <= target + 300000 else None
            if not span or not future: continue
            original = {a['name']: a for a in origin['accounts']}
            actual = {a['name']: a for a in future['accounts']}
            if set(original) != set(actual): continue
            for name, a in actual.items():
                if name in span['uncertain'] or original[name]['status'] not in ('ready', 'limited') or a['status'] not in ('ready', 'limited'): continue
                if not 0 <= future['at'] - a['seenAt'] <= FRESH or any(not a.get(k) for k in WINDOWS): continue
                observed = all(a[k]['pct'] < origin['forecast']['threshold'] for k in WINDOWS)
                predicted = name in span['available']
                crossed = any(original[name].get(k) and original[name][k].get('resetsAt') and origin['at'] < original[name][k]['resetsAt'] <= target for k in WINDOWS)
                renewal_checks.append({'originAt': origin['at'], 'account': a['label'], 'hours': hours,
                                       'crossedReset': crossed, 'predictedReady': predicted, 'observedReady': observed, 'correct': predicted == observed})
    return {'snapshots': len(chosen), 'liveSnapshots': sum(r['source'] == 'live' for r in chosen), 'observedResets': len(resets), 'renewalAccountChecks': renewal_checks,
            'resetEvents': [{'account': n, 'window': k, 'at': t} for n, k, t in sorted(resets)],
            'groups': groups, 'folds': folds, 'poolOutcomes': dict(outcomes), 'completedPoolForecasts': completed,
            'poolMaeSeconds': sum(f['errorSeconds'] for f in completed) / len(completed) if completed else None}


class HistoryIndex:
    """Append-only ingestion and settled daily scores; JSONL remains the source."""
    DAY = 86400000
    HORIZON = 8 * DAY

    def __init__(self, folder):
        self.folder = Path(folder)
        self.lock = threading.Lock()
        self.bytes_read = 0

    @staticmethod
    def signature(path, offset):
        with path.open('rb') as file:
            file.seek(max(0, offset - 256))
            return hashlib.sha256(file.read(min(256, offset))).hexdigest()

    def ingest(self, db):
        files = sorted(self.folder.glob('*.jsonl'))
        known = {r[0]: r[1:] for r in db.execute('SELECT name, inode, offset, signature, rejected FROM files')}
        reset = set(known) - {p.name for p in files}
        for path in files:
            if path.name in known:
                inode, offset, signature, _ = known[path.name]
                if path.stat().st_ino != inode or path.stat().st_size < offset or self.signature(path, offset) != signature: reset = True
        if reset:
            for table in ('files', 'rows', 'scores'): db.execute('DELETE FROM ' + table)
            known = {}
        added = []
        for path in files:
            _, offset, _, rejected = known.get(path.name, (0, 0, '', 0))
            with path.open('rb') as file:
                file.seek(offset)
                while True:
                    start = file.tell()
                    line = file.readline(2 * 1024 * 1024)
                    self.bytes_read += len(line)
                    if not line: break
                    if not line.endswith(b'\n'):
                        # Incomplete writes remain pending; oversized records are skipped in chunks.
                        if len(line) < 2 * 1024 * 1024: file.seek(start); break
                        while line and not line.endswith(b'\n'): line = file.readline(65536)
                        rejected += 1; continue
                    if not line.strip(): continue
                    row = parse_row(line)
                    if row is None: rejected += 1; continue
                    db.execute('INSERT OR REPLACE INTO rows VALUES (?, ?, ?)', (row['at'], row['source'], json.dumps(row)))
                    added.append(row['at'])
                offset = file.tell()
            db.execute('INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?)',
                       (path.name, path.stat().st_ino, offset, self.signature(path, offset), rejected))
        if added:
            # New targets can settle origins at most eight days earlier.
            db.execute('DELETE FROM scores WHERE day >= ?', ((min(added) - self.HORIZON) // self.DAY * self.DAY,))

    def report(self):
        with self.lock:
            try: return self._report()
            except sqlite3.DatabaseError as error:
                if getattr(error, 'sqlite_errorcode', None) not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB): raise
                path = self.folder / 'validation.sqlite3'
                path.rename(self.folder / f'validation.corrupt-{time.time_ns()}.sqlite3')
                return {**self._report(), 'indexRebuilt': True}

    def _report(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.folder / 'validation.sqlite3', timeout=3)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, inode INTEGER, offset INTEGER, signature TEXT, rejected INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS rows (at INTEGER, source TEXT, payload TEXT, PRIMARY KEY(at, source))')
            db.execute('CREATE TABLE IF NOT EXISTS scores (day INTEGER PRIMARY KEY, payload TEXT)')
            self.bytes_read = 0
            self.ingest(db)
            days = [r[0] for r in db.execute('SELECT DISTINCT CAST(at / ? AS INTEGER) * ? AS day FROM rows ORDER BY day', (self.DAY, self.DAY))]
            latest = db.execute('SELECT MAX(at) FROM rows').fetchone()[0] or 0
            result = evaluate([])
            result.update(poolScored=0, renewalChecks=0, renewalCorrect=0, indexed=True)
            groups, outcomes, resets, error_sum = {}, Counter(), set(), 0
            for day in days:
                cached = db.execute('SELECT payload FROM scores WHERE day = ?', (day,)).fetchone()
                if cached:
                    report = json.loads(cached[0])
                else:
                    values = db.execute('SELECT payload FROM rows WHERE at >= ? AND at <= ? ORDER BY at', (day, day + self.DAY + self.HORIZON)).fetchall()
                    prior = db.execute('SELECT payload FROM rows WHERE at < ? ORDER BY at DESC LIMIT 1', (day,)).fetchone()
                    future = db.execute('SELECT payload FROM rows WHERE at > ? ORDER BY at LIMIT 1', (day + self.DAY + self.HORIZON,)).fetchone()
                    rows = [json.loads(v[0]) for v in ([prior] if prior else []) + values + ([future] if future else [])]
                    report = evaluate(rows, (day, day + self.DAY))
                    report['resetEvents'] = [r for r in report['resetEvents'] if day <= r['at'] < day + self.DAY]
                    if latest >= day + self.DAY + self.HORIZON:
                        pending = report['poolOutcomes'].pop('pending', 0)
                        if pending: report['poolOutcomes']['validation horizon'] = pending
                    for key in ('folds', 'completedPoolForecasts', 'renewalAccountChecks'):
                        if key == 'completedPoolForecasts':
                            report['poolScored'] = len(report[key])
                            report['poolErrorSum'] = sum(r['errorSeconds'] for r in report[key])
                        if key == 'renewalAccountChecks':
                            report['renewalChecks'] = len(report[key])
                            report['renewalCorrect'] = sum(r['correct'] for r in report[key])
                        report[key] = report[key][-200:]
                    db.execute('INSERT OR REPLACE INTO scores VALUES (?, ?)', (day, json.dumps(report)))
                if latest >= day + self.DAY + self.HORIZON and report['poolOutcomes'].get('pending'):
                    report['poolOutcomes']['validation horizon'] = report['poolOutcomes'].pop('pending')
                    db.execute('INSERT OR REPLACE INTO scores VALUES (?, ?)', (day, json.dumps(report)))
                for key in ('snapshots', 'liveSnapshots', 'poolScored', 'renewalChecks', 'renewalCorrect'): result[key] += report.get(key, 0)
                error_sum += report.get('poolErrorSum', 0)
                outcomes.update(report['poolOutcomes'])
                for r in report['resetEvents']: resets.add((r['account'], r['window'], r['at']))
                for g in report['groups']:
                    key = tuple(g[k] for k in ('source', 'account', 'window', 'hours', 'crossedReset'))
                    target = groups.setdefault(key, {**g, 'samples': 0, 'maePoints': 0, 'withoutResetMaePoints': 0, 'flatMaePoints': 0})
                    target['samples'] += g['samples']
                    for metric in ('maePoints', 'withoutResetMaePoints', 'flatMaePoints'): target[metric] += g[metric] * g['samples']
                for key in ('folds', 'completedPoolForecasts', 'renewalAccountChecks'): result[key] = (result[key] + report[key])[-200:]
            result['groups'] = [{**g, **{k: g[k] / g['samples'] for k in ('maePoints', 'withoutResetMaePoints', 'flatMaePoints')}} for g in groups.values()]
            result.update(poolOutcomes=dict(outcomes), observedResets=len(resets), resetEvents=[{'account': a, 'window': w, 'at': t} for a, w, t in sorted(resets)],
                          poolMaeSeconds=error_sum / result['poolScored'] if result['poolScored'] else None,
                          rejectedLines=db.execute('SELECT COALESCE(SUM(rejected), 0) FROM files').fetchone()[0], bytesRead=self.bytes_read)
            return result


def render(report):
    lines = ['Claude pool forecast validation', '',
             f"{report['liveSnapshots']} saved live forecasts · {report['observedResets']} observed resets",
             f"Whole-pool depletion: {report.get('poolScored', len(report['completedPoolForecasts']))} scored forecasts"]
    if report['poolMaeSeconds'] is not None: lines.append(f"Mean error outside observed depletion intervals: {report['poolMaeSeconds'] / 60:.1f}m")
    for name, count in sorted(report['poolOutcomes'].items()): lines.append(f'  {name}: {count}')
    lines += ['', 'Quota projections across resets (error in percentage points):']
    for g in report['groups']:
        if g['crossedReset']:
            lines.append(f"  {g['account']} {g['window']} · {g['hours']}h · {g['samples']} cases · MAE {g['maePoints']:.2f} · no-reset {g['withoutResetMaePoints']:.2f} · flat {g['flatMaePoints']:.2f} · {g['source']}")
    if not any(g['crossedReset'] for g in report['groups']): lines.append('  Waiting for fresh post-reset targets at 1h, 5h and 24h.')
    checks = report.get('renewalAccountChecks', [])
    check_count = report.get('renewalChecks', len(checks))
    correct_count = report.get('renewalCorrect', sum(c['correct'] for c in checks))
    lines += ['', f"Renewal outlook: {check_count} known-account readiness checks; {correct_count} matched.",
              'Uncertain accounts and targets after unobserved renewals are excluded.']
    if report.get('rejectedLines'): lines.append(f"History warning: {report['rejectedLines']} damaged records skipped.")
    if report.get('historyError'): lines.append('History warning: saving readings failed; check filesystem access.')
    lines += ['', 'Forecasts are frozen at origin; later readings only supply outcomes.',
              'Reset projections assume the same account-wide pace after renewal.',
              'Current-quota runtime excludes renewed allowance; resets censor that test.',
              'Partial readings, changed routing and sampling gaps are not successes.',
              'Overlapping cases are not independent; unknown weekly quota stays unknown.']
    if report.get('indexed'): lines += ['Indexed history; settled daily scores cached. Detail lists show the latest 200 cases.', 'Unresolved depletion forecasts close after an 8-day validation horizon.']
    return '\n'.join(lines)
