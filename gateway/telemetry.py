from settings import BACKEND_PORT
"""Read-only presentation of distinct subscription quota windows."""
import http.client
import json
import math


def fable_windows(admin):
    windows = {}
    for provider in admin.get('providers', []):
        if provider.get('auth') != 'claude_oauth': continue
        for account in provider.get('accounts', []):
            pct, reset = account.get('utilization_7d_oi'), account.get('reset_7d_oi')
            valid = isinstance(pct, (int, float)) and not isinstance(pct, bool) and math.isfinite(pct * 100) and pct >= 0
            reset_valid = isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset) and reset > 0
            windows[(provider.get('provider'), account.get('name'), account.get('account_ref'))] = (
                {'pct': pct * 100, 'resetsAt': reset * 1000 if reset_valid else None} if valid else None)
    return windows


def missing_windows(accounts):
    return [{'name': a.get('name'), 'label': a['label'], 'window': key}
            for a in accounts if a['status'] == 'ready'
            for key in ['fiveHour', 'sevenDay'] if not a.get(key)]


def coverage_note(accounts):
    labels = [a['label'] for a in accounts if a['status'] == 'ready' and not a.get('sevenDay')]
    return 'Weekly all models not reported: ' + ', '.join(labels) + '. Capacity may run out sooner.' if labels else None


def enrich(data, admin):
    windows = fable_windows(admin)
    for account in data.get('accounts', []):
        key = (account.get('provider'), account.get('name'), account.get('id'))
        if key in windows: account['fableWeekly'] = windows[key]
    if forecast := data.get('forecast'):
        forecast['missingWindows'] = missing_windows(data['accounts'])
        forecast['partial'] = forecast.get('partial', False) or bool(forecast['missingWindows'])
    return data


def read_admin(token):
    conn = http.client.HTTPConnection('127.0.0.1', BACKEND_PORT, timeout=2)
    try:
        conn.request('GET', '/admin/api/pool', headers={'x-shunt-admin-token': token})
        response = conn.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 1024 * 1024: raise RuntimeError('Quota telemetry unavailable')
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get('providers'), list): raise ValueError('Invalid quota telemetry')
        if any(not isinstance(p, dict) or not isinstance(p.get('accounts', []), list)
               or any(not isinstance(a, dict) for a in p.get('accounts', [])) for p in value['providers']):
            raise ValueError('Invalid quota telemetry')
        return value
    finally: conn.close()


def capacity(accounts, model, now):
    """Wait only on confirmed quota exhaustion, never on auth or network failure."""
    windows = ['fiveHour', 'sevenDay'] + (['fableWeekly'] if 'fable' in model.lower() else [])
    candidates = [a for a in accounts if a['status'] not in {'paused', 'reauthenticate'}]
    resets = []
    for account in candidates:
        blocked = []
        for key in windows:
            reading = account.get(key) or {}
            signal = account.get('quotaLimits', {}).get(key) or {}
            if signal.get('status') == 'rejected' or reading.get('pct', 0) >= 100:
                reset = signal.get('resetsAt') or reading.get('resetsAt')
                # An elapsed boundary is permission to probe, not proof of fresh quota.
                if reset and reset <= now:
                    continue
                blocked.append(reset)
        if not blocked:
            return {'state': 'ready', 'nextResetAt': None, 'checkedAt': now}
        resets.append(max(blocked) if all(blocked) else None)
    return {'state': 'quota_wait' if candidates else 'unavailable',
            'nextResetAt': min(resets) if resets and all(resets) else None, 'checkedAt': now}
