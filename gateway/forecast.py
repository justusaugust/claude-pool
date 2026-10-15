"""Quota pace uses fresh same-window observations, not sums of plan percentages."""
from statistics import median
from telemetry import missing_windows

WINDOWS = [('fiveHour', '5h'), ('sevenDay', 'week')]


def pace(points, key, now):
    points = sorted({p['at']: p for p in points}.values(), key=lambda p: p['at'])
    # Ignore one-point reading jitter; larger corrections start a fresh baseline.
    for i in range(len(points) - 1, 0, -1):
        if points[i - 1][key]['pct'] - points[i][key]['pct'] >= 2:
            points = points[i:]
            break
    recent = [p for p in points if now - p['at'] <= 1200000]
    if len(recent) >= 3 and recent[-1]['at'] - recent[0]['at'] >= 600000:
        points = recent
    span = (points[-1]['at'] - points[0]['at']) / 1000 if points else 0
    result = {'ratePerHour': None, 'reason': 'collecting pace', 'samples': len(points), 'spanSeconds': span}
    if len(points) < 3 or span < 600 or now - points[-1]['at'] > 600000:
        return result
    burn = points[-1][key]['pct'] - points[0][key]['pct']
    if burn <= 0:
        return {**result, 'ratePerHour': 0, 'reason': 'no recent burn'}
    if burn < 1:
        return {**result, 'reason': 'low signal'}
    # Five-minute-separated pairs avoid fitting a rounded quota jump over a few seconds.
    rates = sorted((b[key]['pct'] - a[key]['pct']) * 3600000 / (b['at'] - a['at'])
                   for i, a in enumerate(points) for b in points[i + 1:] if b['at'] - a['at'] >= 300000)
    rate = median(rates)
    if rate <= 0:
        return {**result, 'reason': 'low signal'}
    low, high = rates[len(rates) // 4], rates[3 * len(rates) // 4]
    quality = 'low signal' if len(points) < 5 or burn < 2 else 'changing' if high - low > rate * .5 else 'steady'
    return {**result, 'ratePerHour': rate, 'rateLow': low, 'rateHigh': high, 'reason': quality}


def estimate(accounts, observations, now, threshold=95):
    estimates, resets = [], []
    for account in accounts:
        row = {'name': account['name'], 'label': account['label'], 'status': account['status'],
               'seconds': None, 'basis': [], 'windows': {}, 'reason': 'collecting pace'}
        times = []
        for key, label in WINDOWS:
            window = account.get(key)
            reset = (window or account.get('quotaLimits', {}).get(key) or {}).get('resetsAt')
            if reset and reset > now:
                resets.append({'account': account['label'], 'window': label, 'at': reset})
            if not window:
                continue
            if not 0 <= now - account.get('seenAt', 0) <= 600000:
                row['reason'] = 'reading stale'
                row['windows'][key] = {'ratePerHour': None, 'reason': 'reading stale'}
                continue
            if not window.get('resetsAt') or window['resetsAt'] <= now:
                row['windows'][key] = {'ratePerHour': None, 'reason': 'awaiting reset reading'}
                continue
            # Header and usage-probe reset timestamps differ by up to one second.
            points = [p for p in observations.get(account['id'], []) if
                      0 <= now - p['at'] <= 3600000 and p.get(key)
                      and p[key].get('resetsAt') is not None
                      and abs(p[key]['resetsAt'] - window['resetsAt']) <= 1000]
            speed = pace(points, key, now)
            row['windows'][key] = speed
            if speed['ratePerHour'] is None or speed['ratePerHour'] <= 0:
                if row['reason'] != 'reading stale': row['reason'] = speed['reason']
                continue
            remaining = max(0, threshold - window['pct']) * 3600
            seconds = remaining / speed['ratePerHour']
            speed.update(seconds=seconds, secondsLow=remaining / speed['rateHigh'],
                         secondsHigh=remaining / speed['rateLow'] if speed['rateLow'] > 0 else None)
            times.append((seconds, key, window, speed))
            row['basis'].append(label)
        if times and account['status'] == 'ready':
            seconds, key, window, speed = min(times, key=lambda t: t[0])
            upper = [t[3]['secondsHigh'] for t in times if t[3]['secondsHigh'] is not None]
            row.update(seconds=int(seconds), secondsLow=int(min(t[3]['secondsLow'] for t in times)),
                       secondsHigh=int(min(upper)) if upper else None, reason='recent burn',
                       paceQuality=speed['reason'], resetBeforeLimit=seconds * 1000 > window['resetsAt'] - now)
        estimates.append(row)
    eligible = [a for a in estimates if a['status'] == 'ready']
    known = [a for a in eligible if a['seconds'] is not None]
    return {'seconds': sum(a['seconds'] for a in known) if known else None,
            'secondsLow': sum(a['secondsLow'] for a in known) if known else None,
            'secondsHigh': sum(a['secondsHigh'] for a in known) if known and all(a['secondsHigh'] is not None for a in known) else None,
            'paceQuality': 'changing' if any(a['paceQuality'] == 'changing' for a in known) else 'low signal' if any(a['paceQuality'] == 'low signal' for a in known) else 'steady' if known else 'collecting',
            'resetBeforeLimit': any(a['resetBeforeLimit'] for a in known),
            'estimatedAccounts': len(known), 'eligibleAccounts': len(eligible),
            'partial': len(known) != len(eligible) or any(len(a['basis']) < 2 for a in known),
            'threshold': threshold, 'accounts': estimates,
            'missingWindows': missing_windows(accounts),
            'nextReset': min(resets, key=lambda r: r['at']) if resets else None}


def renewal_scenario(accounts, forecast, now, hours=24):
    """Clock-time coverage under unchanged account-wide burn, not promised runtime."""
    speeds = {a['name']: a['windows'] for a in forecast['accounts']}
    segments = []
    resets = sorted([{'account': a['label'], 'window': label, 'at': w['resetsAt']}
                     for a in accounts for key, label in WINDOWS if (w := a.get(key) or a.get('quotaLimits', {}).get(key)) and w.get('resetsAt')
                     and now < w['resetsAt'] <= now + hours * 3600000], key=lambda r: r['at'])
    for minute in range(0, hours * 60, 5):
        at = now + minute * 60000
        available, unknown = [], []
        for a in accounts:
            if a['status'] not in {'ready', 'limited'}: continue
            blocked, uncertain = False, not 0 <= now - a.get('seenAt', 0) <= 600000
            for key, _ in WINDOWS:
                signal = a.get('quotaLimits', {}).get(key) or {}
                if signal.get('status') == 'rejected':
                    reset = signal.get('resetsAt')
                    if reset and at < reset or not reset and at == now: blocked = True
                    if not reset and at > now: uncertain = True
                w = a.get(key); rate = speeds.get(a['name'], {}).get(key, {}).get('ratePerHour')
                if not w or not w.get('resetsAt'):
                    uncertain = True; continue
                reset = w['resetsAt']
                if reset <= now or rate is None:
                    uncertain = True
                    if at < reset and w['pct'] >= forecast['threshold']: blocked = True
                    continue
                if at < reset: pct = w['pct'] + rate * (at - now) / 3600000
                else:
                    # Only the first known renewal. A later rolling anchor is unobserved.
                    lifetime = 5 if key == 'fiveHour' else 168
                    if at >= reset + lifetime * 3600000:
                        uncertain = True; continue
                    pct = rate * (at - reset) / 3600000
                blocked |= pct >= forecast['threshold']
            if blocked: continue
            (unknown if uncertain else available).append(a['name'])
        state = {'available': sorted(available), 'uncertain': sorted(unknown)}
        if segments and all(segments[-1][k] == v for k, v in state.items()): segments[-1]['until'] = at + 300000
        else: segments.append({'at': at, 'until': at + 300000, **state})
    return {'hours': hours, 'segments': segments, 'resets': resets,
            'coveredSeconds': sum((s['until'] - s['at']) / 1000 for s in segments if s['available']),
            'possibleSeconds': sum((s['until'] - s['at']) / 1000 for s in segments if s['available'] or s['uncertain']),
            'partial': any(s['uncertain'] for s in segments),
            'assumption': 'Same account-wide burn; first known resets only; later renewals unknown', 'resolutionMinutes': 5}


def workload_estimates(accounts, observations, traffic, now):
    groups = sorted({r['workload'] for r in traffic if now - r['at'] <= 86400000})
    rows = []
    for group in groups:
        calls = [r for r in traffic if r['workload'] == group and now - r['at'] <= 86400000]
        windows = []
        for a in accounts:
            points = observations.get(a['id'], [])
            # A tagged observation labels its preceding interval; never join mixed intervals.
            block, blocks = [], []
            for i in range(1, len(points)):
                if points[i].get('workload') == group:
                    if not block: block = [points[i - 1]]
                    block.append(points[i])
                elif block:
                    blocks.append(block); block = []
            if block: blocks.append(block)
            block = next((b for b in reversed(blocks) if len(b) >= 3), [])
            if not block: continue
            last = block[-1]['at']
            if now - last > 3600000: continue
            for key, label in WINDOWS:
                w = block[-1].get(key)
                if not w or not w.get('resetsAt'): continue
                same = [p for p in block if p.get(key) and p[key].get('resetsAt') and abs(p[key]['resetsAt'] - w['resetsAt']) <= 1000]
                speed = pace(same, key, last)
                if speed['ratePerHour'] is not None:
                    windows.append({'account': a['name'], 'label': a['label'], 'window': label, 'ratePerHour': speed['ratePerHour'],
                                    'samples': speed['samples'], 'observedAt': last})
        reads = sum(r.get('cacheReadTokens', 0) for r in calls)
        fresh = sum(r.get('inputTokens', 0) + r.get('cacheCreationTokens', 0) for r in calls)
        rows.append({'workload': group, 'requests': len(calls), 'windows': windows,
                     'cacheReadShare': reads / (reads + fresh) if reads + fresh else None,
                     'medianOutputTokens': median(r.get('outputTokens', 0) for r in calls),
                     'medianThinkingTokens': median(thinking) if (thinking := [r['thinkingTokens'] for r in calls if r.get('thinkingTokens') is not None]) else None,
                     'basis': 'Single local workload intervals; other machines may contribute' if windows else 'collecting quota evidence'})
    return rows
