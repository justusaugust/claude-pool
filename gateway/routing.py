"""Session demand is a routing preference; Shunt still enforces eligibility."""
import re
import time

ACTIVE_SECONDS = 300
REASONS = {'manual selection', 'cache affinity', 'shared capacity', 'new session',
           'five-hour quota exhausted', 'weekly quota exhausted', 'account paused',
           'account cooling down', 'account needs sign-in', 'quota or upstream failover',
           'upstream failover', 'no eligible account'}


def profile(request):
    model = request.get('model', '')
    model = model if isinstance(model, str) and re.fullmatch(r'(?:claude-(?:opus|sonnet|haiku|fable)[A-Za-z0-9._:-]{0,80}|opus|sonnet|haiku|fable)', model) else 'unknown'
    output = request.get('output_config')
    effort = output.get('effort') if isinstance(output, dict) else None
    thinking = request.get('thinking')
    if not isinstance(effort, str) or effort not in {'low', 'medium', 'high', 'max'}:
        effort = 'thinking' if isinstance(thinking, dict) and isinstance(thinking.get('type'), str) and thinking['type'] in {'enabled', 'adaptive'} else 'standard'
    family = next((v for v in ('opus', 'sonnet', 'haiku') if v in model.lower()), 'other')
    return {'model': model, 'effort': effort, 'workload': family + '/' + effort}


def unavailable(account):
    if not account: return 'upstream failover'
    status = account['status']
    if status == 'paused': return 'account paused'
    if status == 'reauthenticate': return 'account needs sign-in'
    if status == 'cooling': return 'account cooling down'
    if account.get('quotaLimits', {}).get('sevenDay', {}).get('status') == 'rejected': return 'weekly quota exhausted'
    if account.get('sevenDay') and account['sevenDay']['pct'] >= 95: return 'weekly quota exhausted'
    if account.get('fiveHour') and account['fiveHour']['pct'] >= 95: return 'five-hour quota exhausted'
    return 'quota or upstream failover'


def reserve_blocked(account):
    """A reserved account takes no new pool work once any window reaches its reserve limit."""
    limit = account.get('reserveAt') or 0
    return limit > 0 and any(account.get(k) and account[k]['pct'] >= limit for k in ('fiveHour', 'sevenDay', 'fableWeekly'))


def session_rows(sessions, now):
    rows = []
    for key, s in sessions.items():
        if not s.get('inFlight') and now - s.get('requestAt', 0) > ACTIVE_SECONDS and now - s.get('uiAt', 0) > 30: continue
        p = s.get('profile', {})
        rows.append({'session': key, 'project': s.get('project'), 'account': s.get('serving') or s.get('reserved') or s.get('active'),
                     'model': p.get('model', 'unknown'), 'effort': p.get('effort', 'standard'),
                     'workload': p.get('workload', 'other/standard'), 'pin': s.get('pin'),
                     'busy': bool(s.get('inFlight')), 'lastRequestAt': s.get('requestAt', 0) * 1000,
                     'reason': s.get('routing', {}).get('reason', 'awaiting next request' if s.get('active') else 'new session')})
    return sorted(rows, key=lambda r: (not r['busy'], -r['lastRequestAt']))


def in_flight(overview, account=None):
    """True while the pool serves a request: any request at all, or one that may be served by `account`.
    Only main requests with a session id become busy rows; a request the rows do not account for may be on any account."""
    active = (overview.get('health') or {}).get('activeRequests', 0)
    busy = [row for row in overview.get('sessions', []) if row.get('busy')]
    if account is None: return active > 0 or bool(busy)
    return any(row.get('account') == account for row in busy) or active > len(busy)


def wait_idle(overview, account, wait, sleep=time.sleep, clock=time.monotonic, interval=5, limit=1800):
    """True once nothing (or nothing on `account`) is in flight. Polls only with `wait`; an unreadable overview is never idle."""
    deadline = clock() + limit if wait else None
    while True:
        try:
            if not in_flight(overview(), account): return True
        except (OSError, ValueError, RuntimeError, AttributeError, TypeError): pass
        if not wait or clock() >= deadline: return False
        sleep(interval)


def choose(accounts, sessions, session_id, forecast, now):
    state = sessions[session_id]
    rows = [r for r in session_rows({k: v for k, v in sessions.items() if k != session_id}, now)
            if r['busy'] or now - r['lastRequestAt'] / 1000 <= ACTIVE_SECONDS]
    ready = [a for a in accounts if a['status'] == 'ready' and
             all(not a.get(k) or a[k]['pct'] < 95 for k in ('fiveHour', 'sevenDay')) and not reserve_blocked(a)]
    by_name = {a['name']: a for a in accounts}
    loads = {a['name']: sum(r['account'] == a['name'] for r in rows) for a in accounts}
    busy = {a['name']: sum(r['account'] == a['name'] and r['busy'] for r in rows) for a in accounts}
    seconds = {r['name']: r['seconds'] for r in forecast['accounts']}
    pin, previous = state.get('pin'), state.get('active')
    if pin and any(a['name'] == pin for a in ready):
        return {'account': pin, 'reason': 'manual selection', 'peers': loads[pin]}
    if not ready:
        return {'account': pin or previous, 'reason': 'no eligible account', 'peers': 0}
    # Remaining time is comparable across plans; percentages are not.
    def score(a):
        name = a['name']; capacity = seconds.get(name)
        return (bool(a.get('reserveAt')), busy[name], loads[name], -(capacity or 0), name)
    best = min(ready, key=score)['name']
    held = bool(by_name.get(previous, {}).get('reserveAt')) and not by_name[best].get('reserveAt')
    reason = unavailable(by_name.get(pin or previous)) if pin or previous else 'new session'
    if previous and held and not pin and any(a['name'] == previous for a in ready): reason = 'shared capacity'  # a reserved account never keeps a session while an unreserved one is ready
    elif previous and any(a['name'] == previous for a in ready) and not pin:
        remaining = seconds.get(previous)
        crowded = loads[previous] > loads[best] and remaining is not None and remaining / (loads[previous] + 1) < 600
        if not crowded: return {'account': previous, 'reason': 'cache affinity', 'peers': loads[previous]}
        reason = 'shared capacity'
    elif not pin and not previous and any(loads.values()): reason = 'shared capacity'
    return {'account': best, 'reason': reason, 'peers': loads[best]}
