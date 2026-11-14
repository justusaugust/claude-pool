from settings import BACKEND_PORT, OPTIONS
"""Read-only presentation of distinct subscription quota windows."""
import http.client
import json
import math
from datetime import datetime

from routing import held, holds


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
    """Wait only on confirmed quota exhaustion, never on auth or network failure. An account Shunt no longer chooses
    (routing.held: a rejected or full window, or its hard reserve reached) is exhausted until the windows holding it reset.
    A wait also says which accounts are held and by what (`holds`, longest window each, earliest reset first), how many need
    sign-in or are paused, and whether the Fable week alone is what holds (`otherModels`)."""
    fable = 'fable' in model.lower()
    windows = ['fiveHour', 'sevenDay'] + (['fableWeekly'] if fable else [])
    candidates = [a for a in accounts if a['status'] not in {'paused', 'reauthenticate'}]
    resets, reasons = [], []
    for account in candidates:
        blocked = held(account, windows, now)
        if not blocked:
            return {'state': 'ready', 'nextResetAt': None, 'checkedAt': now}
        longest = max(blocked, key=lambda h: math.inf if h['resetsAt'] is None else h['resetsAt'])
        reasons.append({'label': account.get('label') or account.get('name'), **longest})
        resets.append(max(h['resetsAt'] for h in blocked) if all(h['resetsAt'] for h in blocked) else None)
    count = lambda status: sum(a['status'] == status for a in accounts)
    return {'state': 'quota_wait' if candidates else 'unavailable',
            'nextResetAt': min(resets) if resets and all(resets) else None, 'checkedAt': now,
            'holds': sorted(reasons, key=lambda h: (h['resetsAt'] is None, h['resetsAt'] or 0)),
            'signIn': count('reauthenticate'), 'paused': count('paused'),
            'otherModels': fable and bool(candidates) and any(not holds(a, windows[:2], now) for a in candidates)}


WINDOW_NAMES = {'fiveHour': '5h', 'sevenDay': 'week', 'fableWeekly': 'Fable week'}
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def wait_text(quota, model, now, zone=None):
    """The one-line reason for a quota wait (`quota` from `capacity`, `now` epoch ms): which accounts are held by which
    window until when, then accounts to sign in, paused ones and whether other models would run; None unless waiting."""
    if quota.get('state') != 'quota_wait': return None
    from zoneinfo import ZoneInfo
    zone = zone or OPTIONS.get('timezone')
    tz = ZoneInfo(zone) if zone else None
    today = datetime.fromtimestamp(now / 1000, tz).date()

    def until(reset):
        if not reset: return ''
        at = datetime.fromtimestamp(reset / 1000, tz)
        return ' until %s%s' % ('' if at.date() == today else DAYS[at.weekday()] + ' ', at.strftime('%H:%M'))

    def describe(hold):
        label, name = hold['label'], WINDOW_NAMES.get(hold['window'])
        if not name: return label + ' at its limit' + until(hold['resetsAt'])
        pct = round(hold['pct'] or 0)
        if hold['reserveAt'] is None or pct >= 100: return '%s %s limit reached%s' % (label, name, until(hold['resetsAt']))
        return '%s %s %d%% (reserved from %d%%)%s' % (label, name, pct, hold['reserveAt'], until(hold['resetsAt']))

    shown = quota.get('holds', [])
    parts = ['Pool waits for Fable quota' if 'fable' in model.lower() else 'Pool waits for quota'] + [describe(h) for h in shown[:3]]
    if len(shown) > 3: parts.append('%d more account%s held' % (len(shown) - 3, '' if len(shown) == 4 else 's'))
    for key, one, many in (('signIn', 'account needs sign-in', 'accounts need sign-in'), ('paused', 'account paused', 'accounts paused')):
        if quota.get(key): parts.append('%d %s' % (quota[key], one if quota[key] == 1 else many))
    if quota.get('otherModels'): parts.append('other models have quota (/model)')
    return ' · '.join(parts)
