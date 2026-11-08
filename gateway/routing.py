"""Session demand is a routing preference; Shunt still enforces eligibility."""
from datetime import datetime
import re
import time
from zoneinfo import ZoneInfo

ACTIVE_SECONDS = 300
REASONS = {'manual selection', 'cache affinity', 'shared capacity', 'new session',
           'five-hour quota exhausted', 'weekly quota exhausted', 'account paused',
           'account cooling down', 'account needs sign-in', 'quota or upstream failover',
           'upstream failover', 'no eligible account', 'reserved account'}


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


def unavailable(account, now=None):
    if not account: return 'upstream failover'
    status = account['status']
    if status == 'paused': return 'account paused'
    if status == 'reauthenticate': return 'account needs sign-in'
    if status == 'cooling': return 'account cooling down'
    if account.get('quotaLimits', {}).get('sevenDay', {}).get('status') == 'rejected': return 'weekly quota exhausted'
    if account.get('sevenDay') and account['sevenDay']['pct'] >= 95: return 'weekly quota exhausted'
    if account.get('fiveHour') and account['fiveHour']['pct'] >= 95: return 'five-hour quota exhausted'
    if reserve_blocked(account, now): return 'reserved account'
    return 'quota or upstream failover'


def reserve_active(account, now):
    """Whether the reserve window holds at `now` (epoch seconds): always without `reserveHours`, else while
    start <= local hour < end in `reserveTimezone`. A zone this machine cannot load keeps the reserve."""
    hours = account.get('reserveHours')
    if not hours: return True
    try: hour = datetime.fromtimestamp(now, ZoneInfo(account['reserveTimezone'])).hour
    except (KeyError, ValueError, TypeError, OSError): return True
    return hours[0] <= hour < hours[1]


def reserve_limit(account, now):
    """The reserve percent that holds at `now` (epoch seconds): `reserveAt` all day, or only inside its window; 0 otherwise."""
    limit = account.get('reserveAt') or 0
    return limit if limit > 0 and reserve_active(account, now) else 0


def reserve_text(account, now):
    """The reserve as status lines show it: 'reserved from 75%' all day; with a window 'reserved from 90% until 24:00'
    while it holds and 'reserved from 90% from 06:00' otherwise; '' without a reserve."""
    limit = account.get('reserveAt') or 0
    if limit <= 0: return ''
    hours = account.get('reserveHours')
    edge = '' if not hours else ' until %02d:00' % hours[1] if reserve_active(account, now) else ' from %02d:00' % hours[0]
    return 'reserved from ' + str(limit) + '%' + edge


def reserve_blocked(account, now=None):
    """A reserved account takes no new pool work once any window reaches the reserve limit that holds at `now`."""
    limit = reserve_limit(account, time.time() if now is None else now)
    return limit > 0 and any(account.get(k) and account[k]['pct'] >= limit for k in ('fiveHour', 'sevenDay', 'fableWeekly'))


def hard_reserve(account, now):
    """The reserve percent Shunt enforces as a cap at `now` (epoch seconds): a hard reserve that holds, else 0. A soft
    reserve has no cap in Shunt (backend_options)."""
    return 0 if account.get('reserveSoft') else reserve_limit(account, now)


def holds(account, keys, now):
    """What keeps Shunt from choosing `account` for a request that counts the windows `keys`, at `now` (epoch ms): the
    reset (ms, None when unknown) of each window that is rejected, full or at the hard reserve. Shunt's own `capped` flag
    (`cappedFable` for the Fable window) holds the account with an unknown reset when no such window shows. A window whose
    reset has passed holds nothing: the boundary is permission to probe, not proof of fresh quota. Empty: it takes work."""
    limit = hard_reserve(account, now / 1000)
    resets = []
    for key in keys:
        reading = account.get(key) or {}
        signal = (account.get('quotaLimits') or {}).get(key) or {}
        pct = reading.get('pct') or 0
        if signal.get('status') == 'rejected' or pct >= 100 or (limit and pct >= limit):
            reset = signal.get('resetsAt') or reading.get('resetsAt')
            if reset and reset <= now: continue
            resets.append(reset)
    if not resets and (account.get('capped') or ('fableWeekly' in keys and account.get('cappedFable'))): resets.append(None)
    return resets


def open_quota(account):
    """Ready, and both all-model windows under 95 %."""
    return account['status'] == 'ready' and all(not account.get(k) or account[k]['pct'] < 95 for k in ('fiveHour', 'sevenDay'))


def eligible(account, now=None):
    """Takes new pool work: ready, both all-model windows under 95 %, and not held back by its reserve."""
    return open_quota(account) and not reserve_blocked(account, now)


def backend_priority(reserve_at):
    """Shunt's per-account priority (lower is preferred, default 100): a reserved account comes after every other,
    and the larger its reserve the later (99 -> 101, 85 -> 115)."""
    return 200 - reserve_at if reserve_at else 100


SOFT_PRIORITY = 200  # after every hard reserve (backend_priority stays <= 199)


def backend_options(account, now):
    """Shunt's reserve options for `account` at `now`, in the order they are written (None: the key is left out). A hard
    reserve that holds gets its cap and a late priority. A soft one gets no cap, so the pool may send it work once no
    other account can, and SOFT_PRIORITY: Shunt cannot tell it below from above its limit, so it uses it only after every
    other account, hard reserves included. Outside its window it gets neither."""
    limit = reserve_limit(account, now)
    soft = bool(account.get('reserveSoft'))
    return {'max_utilization': limit / 100 if limit and not soft else None,
            'priority': (SOFT_PRIORITY if soft else backend_priority(limit)) if limit else None}


def session_rows(sessions, now):
    rows = []
    for key, s in sessions.items():
        waiting = s.get('brakeWaiting') if isinstance(s.get('brakeWaiting'), dict) else None
        if not s.get('inFlight') and not waiting and now - s.get('requestAt', 0) > ACTIVE_SECONDS and now - s.get('uiAt', 0) > 30: continue
        p = s.get('profile', {})
        rows.append({'session': key, 'project': s.get('project'), 'account': s.get('serving') or s.get('reserved') or s.get('active'),
                     'model': p.get('model', 'unknown'), 'effort': p.get('effort', 'standard'),
                     'workload': p.get('workload', 'other/standard'), 'pin': s.get('pin'),
                     'busy': bool(s.get('inFlight')), 'lastRequestAt': s.get('requestAt', 0) * 1000,
                     'reason': s.get('routing', {}).get('reason', 'awaiting next request' if s.get('active') else 'new session'),
                     **({'brakeWaiting': dict(waiting)} if waiting else {})})
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
    ready = [a for a in accounts if eligible(a, now)]
    if not ready:  # every account is out: a soft reserve goes on above its limit until its quota ends; a hard one never does
        ready = [a for a in accounts if a.get('reserveSoft') and open_quota(a)]
    by_name = {a['name']: a for a in accounts}
    loads = {a['name']: sum(r['account'] == a['name'] for r in rows) for a in accounts}
    busy = {a['name']: sum(r['account'] == a['name'] and r['busy'] for r in rows) for a in accounts}
    seconds = {r['name']: r['seconds'] for r in forecast['accounts']}
    pin, previous = state.get('pin'), state.get('active')
    if pin and any(a['name'] == pin for a in ready):
        return {'account': pin, 'reason': 'manual selection', 'peers': loads[pin]}
    if not ready:
        keep = pin or previous  # a reserved account at its limit is never the preferred account: no header steers to it
        return {'account': None if keep in by_name and reserve_blocked(by_name[keep], now) else keep, 'reason': 'no eligible account', 'peers': 0}
    # Remaining time is comparable across plans; percentages are not.
    def score(a):
        name = a['name']; capacity = seconds.get(name)
        return (backend_priority(reserve_limit(a, now)), busy[name], loads[name], -(capacity or 0), name)
    best = min(ready, key=score)['name']
    held = previous in by_name and backend_priority(reserve_limit(by_name[previous], now)) > backend_priority(reserve_limit(by_name[best], now))
    reason = unavailable(by_name.get(pin or previous), now) if pin or previous else 'new session'
    if previous and held and not pin and any(a['name'] == previous for a in ready): reason = 'shared capacity'  # a reserved account never keeps a session while an earlier choice is ready
    elif previous and any(a['name'] == previous for a in ready) and not pin:
        remaining = seconds.get(previous)
        crowded = loads[previous] > loads[best] and remaining is not None and remaining / (loads[previous] + 1) < 600
        if not crowded: return {'account': previous, 'reason': 'cache affinity', 'peers': loads[previous]}
        reason = 'shared capacity'
    elif not pin and not previous and any(loads.values()): reason = 'shared capacity'
    return {'account': best, 'reason': reason, 'peers': loads[best]}
