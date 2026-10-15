"""Causal replay of saved quota readings; no model calls or credential exports."""
import json
import math
from pathlib import Path
from statistics import median
import time
from forecast import estimate, pace
from validation import load

HERE = Path(__file__).resolve().parent
HORIZONS = (5, 10, 20)


def replay(histories):
    folds = []
    for history in histories:
        samples = sorted(history['samples'], key=lambda p: p['at'])
        for key, label in [('fiveHour', '5h'), ('sevenDay', 'week')]:
            for i, origin in enumerate(samples):
                window = origin.get(key)
                if not window or not window.get('resetsAt') or window['pct'] >= 95:
                    continue
                for minutes in HORIZONS:
                    target_at = origin['at'] + minutes * 60000
                    future = next((p for p in samples[i + 1:] if p['at'] >= target_at), None)
                    if not future or future['at'] > target_at + 180000:
                        continue
                    target = future.get(key)
                    if not target or not target.get('resetsAt') or abs(target['resetsAt'] - window['resetsAt']) > 1000 or window['resetsAt'] <= future['at']:
                        continue
                    if window['pct'] - target['pct'] >= 2:
                        continue
                    prefix = [p for p in samples[:i + 1] if origin['at'] - p['at'] <= 3600000 and p.get(key) and p[key].get('resetsAt') is not None]
                    same_window = [p for p in prefix if abs(p[key]['resetsAt'] - window['resetsAt']) <= 1000]
                    speed = pace(same_window, key, origin['at'])
                    old = [p for p in prefix if p[key]['resetsAt'] == window['resetsAt']]
                    old_rate = None
                    if len(old) >= 3 and old[-1]['at'] - old[0]['at'] >= 600000:
                        burn = old[-1][key]['pct'] - old[0][key]['pct']
                        if burn > 0: old_rate = burn * 3600000 / (old[-1]['at'] - old[0]['at'])
                    elapsed = (future['at'] - origin['at']) / 3600000
                    rates = {'new': speed['ratePerHour'], 'previous': old_rate, 'flat': 0}
                    predictions = {name: min(100, window['pct'] + rate * elapsed) if rate is not None else None for name, rate in rates.items()}
                    band = None
                    if speed.get('rateLow') is not None and speed.get('rateHigh') is not None:
                        band = [max(0, window['pct'] + speed['rateLow'] * elapsed), min(100, window['pct'] + speed['rateHigh'] * elapsed)]
                    folds.append({'account': history['label'], 'window': label, 'horizonMinutes': minutes,
                                  'originAt': origin['at'], 'targetAt': future['at'], 'originPct': window['pct'], 'actualPct': target['pct'],
                                  'predictions': predictions, 'band': band, 'paceQuality': speed['reason']})
    return folds


def summarize(folds):
    groups = []
    for account, window, horizon in sorted({(f['account'], f['window'], f['horizonMinutes']) for f in folds}):
        rows = [f for f in folds if (f['account'], f['window'], f['horizonMinutes']) == (account, window, horizon)]
        common = [f for f in rows if all(f['predictions'][k] is not None for k in ['new', 'previous'])]
        metrics = {}
        for method in ['new', 'previous', 'flat']:
            errors = sorted(abs(f['predictions'][method] - f['actualPct']) for f in common)
            metrics[method] = {'mae': sum(errors) / len(errors) if errors else None,
                               'p90Error': errors[max(0, math.ceil(len(errors) * .9) - 1)] if errors else None}
        bands = [f for f in rows if f['band'] is not None]
        groups.append({'account': account, 'window': window, 'horizonMinutes': horizon, 'opportunities': len(rows), 'pairedForecasts': len(common),
                       'forecastCoverage': {k: sum(f['predictions'][k] is not None for f in rows) for k in ['new', 'previous']},
                       'metrics': metrics, 'paceBandSamples': len(bands), 'paceBandCoverage': sum(f['band'][0] <= f['actualPct'] <= f['band'][1] for f in bands) / len(bands) if bands else None,
                       'paceBandCoverageWithOnePointTolerance': sum(f['band'][0] - 1 <= f['actualPct'] <= f['band'][1] + 1 for f in bands) / len(bands) if bands else None})
    return groups


def benchmark(accounts, observations, now, repetitions=30):
    times = []
    for _ in range(repetitions):
        started = time.perf_counter();estimate(accounts, observations, now);times.append((time.perf_counter() - started) * 1000)
    times.sort()
    return {'repetitions': repetitions, 'medianMs': median(times), 'p95Ms': times[math.ceil(len(times) * .95) - 1]}


def collect():
    import launch
    data = json.loads(launch.request(launch.token('.pool-client-token'), path='/pool/overview'))
    stored = json.loads((launch.RUNTIME / 'sessions.json').read_text())
    histories = {a['name']: {'label': a['label'], 'samples': list(stored.get('observations', {}).get(a['id'], []))} for a in data['accounts']}
    sources = ['runtime observations']
    # These three snapshots used real quota; synthetic quota-switch trials are excluded.
    for name in ['integrated-evidence.json', 'opus-thinking-evidence.json', 'opus-recovery-evidence.json']:
        path = HERE / name
        if not path.exists(): continue
        snapshot = json.loads(path.read_text()).get('pool', {})
        used = False
        for a in snapshot.get('accounts', []):
            if a.get('name') in histories and a.get('seenAt', 0) > 0:
                histories[a['name']]['samples'].append({'at': a['seenAt'], 'fiveHour': a.get('fiveHour'), 'sevenDay': a.get('sevenDay')})
                used = True
        if used: sources.append(name)
    saved, _ = load(launch.RUNTIME / 'usage-history')
    if saved: sources.append('durable usage history')
    for name, history in histories.items():
        for snapshot in saved:
            for a in snapshot['accounts']:
                if a['name'] == name:
                    history['samples'].append({'at': a['seenAt'], 'fiveHour': a.get('fiveHour'), 'sevenDay': a.get('sevenDay')})
        history['samples'] = sorted({p['at']: p for p in history['samples']}.values(), key=lambda p: p['at'])
    return data, stored.get('observations', {}), list(histories.values()), sources


def main():
    data, observations, histories, sources = collect()
    folds = replay(histories);groups = summarize(folds)
    now = data['updatedAt']
    synthetic_accounts, synthetic_points = [], {}
    for n in range(7):
        name = 'fixture-' + str(n)
        account = {'id': name, 'name': name, 'label': name, 'seenAt': now, 'status': 'ready',
                   'fiveHour': {'pct': 30, 'resetsAt': now + 3600000}, 'sevenDay': {'pct': 10, 'resetsAt': now + 86400000}}
        synthetic_accounts.append(account)
        synthetic_points[name] = [{'at': now - 3600000 + i * 18000, 'fiveHour': {**account['fiveHour'], 'pct': 10 + i * .1},
                                   'sevenDay': {**account['sevenDay'], 'pct': 5 + i * .025}} for i in range(201)]
    report = {'sources': sources, 'inferenceSent': False, 'futureDataUsedInFit': False, 'history': histories, 'groups': groups, 'folds': folds,
              'benchmarks': {'livePool': benchmark(data['accounts'], observations, now), 'syntheticSevenAccounts201Samples': benchmark(synthetic_accounts, synthetic_points, now)},
              'limits': ['Only saved quota readings; no token-speed claims', 'Rolling folds overlap and are not independent trials',
                         'Targets are 5/10/20 minutes ahead, using the next reading within three minutes',
                         'MAE compares new, previous and flat forecasts only on paired opportunities; coverage is also reported',
                         'Whole-pool exhaustion and reset-cycle forecasts cannot be validated from this short history',
                         'Provider percentage rounding imposes approximately one-point measurement uncertainty',
                         'No synthetic quota signals included in replay; synthetic workload used only for CPU benchmark']}
    (HERE / 'pace-evaluation.json').write_text(json.dumps(report, indent=2) + '\n')
    lines = ['# Quota pace replay and benchmark', '', 'Causal replay: each estimate fits only readings at or before its origin. No inference was sent.', '',
             '| Account | Window | Horizon | Paired / possible | New MAE | Previous MAE | Flat MAE |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for g in groups:
        values = ['—' if g['metrics'][k]['mae'] is None else f"{g['metrics'][k]['mae']:.2f} pp" for k in ['new', 'previous', 'flat']]
        lines.append(f"| {g['account']} | {g['window']} | {g['horizonMinutes']}m | {g['pairedForecasts']} / {g['opportunities']} | " + ' | '.join(values) + ' |')
    lines += ['', 'Errors are percentage points of quota, not relative percentages. Missing forecasts are not scored as zero error. Coverage and per-fold predictions are in [pace-evaluation.json](pace-evaluation.json).', '', '| CPU workload | Median | P95 |', '| --- | --- | --- |']
    for name, values in report['benchmarks'].items():lines.append(f"| {name} | {values['medianMs']:.2f} ms | {values['p95Ms']:.2f} ms |")
    for group in groups:
        if group['account'] == 'Work' and group['window'] == '5h' and group['horizonMinutes'] == 10 and group['paceBandSamples']:
            lines += ['', f"Team’s 10-minute pace band covered {group['paceBandCoverage']:.1%} of {group['paceBandSamples']} later readings, or {group['paceBandCoverageWithOnePointTolerance']:.1%} with a one-point rounding tolerance. This is an observed variability check, not a confidence guarantee."]
    lines += ['', 'History sources: ' + ', '.join(sources) + '.', '', *['- ' + limit for limit in report['limits']]]
    (HERE / 'pace-validation.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'samples': {h['label']:len(h['samples']) for h in histories}, 'groups':groups,'benchmarks':report['benchmarks']},indent=2))


if __name__ == '__main__': main()
