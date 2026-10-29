"""Brake stage from the pooled forecast, the hold on background sessions, and per-day ledgers of answers and waits.

Stage 1: the pooled forecast does not reach the next moment an account gains capacity. Stage 2: it does not reach half that
time. Every forecast carries the stage (usage, history, Conductor alerts). No request changes its model by it (the owner
removed the Sonnet switch on 29.10.2026). Instead, while the hold is on (Hold), requests of background sessions wait before
they go upstream (bridge.Handler.brake_wait): a session is background when no message from a person (the owner or a
partner) reached it in the last INTERACTIVE seconds (activity.classify). Its subagents and workflow agents share its session id and wait with it.
The projects in exempt() never wait. Replayed on 22.-29.10.2026 data this avoided about 89 of 321 standstill minutes; background
work waited 35 minutes in the median, 125 at p90.
"""
import json
import math
from collections import defaultdict

NEVER = {'conductor', 'conductor-wecker'}   # COCKPIT_PROJECT (folder name) of Conductor Claude and of its watcher session
CLASSES = {'subagent', 'workflow'}
INTERACTIVE = 90 * 60   # seconds a session counts as interactive after a message from a person
RELEASE = 15 * 60       # seconds the brake must stay off in a row before background work goes again (the stage flickers)
MAX_WAIT = 3 * 3600     # seconds one request waits at most; then it goes and the journal records it
# $ per million tokens: input, output, cache read; a cache write costs 1.25 x input for five minutes, 2 x input for an hour
# (API list prices, October 2026).
PRICES = [('fable', (10, 50, .25)), ('mythos', (10, 50, .25)), ('opus-5-5', (4, 20, .2)), ('opus', (5, 25, .5)),
          ('sonnet-5', (2, 10, .2)), ('sonnet', (3, 15, .3)), ('haiku-5-5', (.1, .5, .01)), ('haiku', (1, 5, .1))]


def returns_at(account, now):
    """When an account next gains capacity: its five-hour reset, or for a full week the weekly reset
    (and the five-hour reset too, if that window is also full)."""
    if account.get('status') not in {'ready', 'limited'}: return None
    five, week = account.get('fiveHour') or {}, account.get('sevenDay') or {}
    at = five.get('resetsAt') or 0
    if week.get('pct', 0) >= 100:
        at = max(week.get('resetsAt') or 0, at) if five.get('pct', 0) >= 100 else (week.get('resetsAt') or 0)
    return at if at > now else None


def stage(forecast, accounts, now):
    """Brake stage from the pooled forecast and the next moment an account gains capacity."""
    seconds = (forecast or {}).get('seconds')
    resets = [at for a in accounts if (at := returns_at(a, now))]
    if seconds is None or not resets: return {'stage': 0, 'ratio': None, 'resetAt': min(resets) if resets else None}
    reset = min(resets)
    ratio = seconds * 1000 / (reset - now)
    return {'stage': 0 if ratio >= 1 else 1 if ratio >= .5 else 2, 'ratio': ratio, 'resetAt': reset}


def standstill(accounts):
    """No account ready and at least one confirmed at its limit; accounts without a reading yet (a fresh start) are none."""
    statuses = [a.get('status') for a in accounts if isinstance(a, dict)]
    return 'ready' not in statuses and 'limited' in statuses


def braking(info):
    """The brake is on: forecast stage 1 or 2, or a standstill (as in the replay the owner chose)."""
    return (info.get('stage') or 0) >= 1 or bool(info.get('standstill'))


class Hold:
    """Whether background work waits. On from the first sample with the brake on; off once it stayed off for `release`
    seconds in a row: one sample with the brake on starts the calm over. Times are wall-clock seconds."""
    def __init__(self, on=False, calm=None, release=RELEASE):
        self.on, self.calm, self.release = on, calm, release

    def update(self, brake_on, now):
        """'on' or 'off' when the hold changed, else None."""
        if brake_on:
            changed = not self.on
            self.on, self.calm = True, None
            return 'on' if changed else None
        if not self.on: return None
        if self.calm is None: self.calm = now
        if now - self.calm < self.release: return None
        self.on, self.calm = False, None
        return 'off'

    def state(self):
        return {'on': self.on, 'calmSince': self.calm}

    @classmethod
    def restore(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get('on'), bool): return cls()
        calm = value.get('calmSince')
        return cls(value['on'], calm if seconds(calm) and value['on'] else None)


def seconds(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def interactive(state, now):
    """A session is interactive within INTERACTIVE seconds of a message from a person, and while the pool has not
    classified any turn of it (safe side: a false interactive only costs quota, a false background holds up the owner)."""
    if not isinstance(state, dict): return True
    owner, auto = state.get('ownerAt'), state.get('autoAt')
    if seconds(owner) and now - owner < INTERACTIVE: return True
    return not (seconds(owner) or seconds(auto))


def exempt(project, urgent=()):
    """Projects whose sessions never wait for the brake: the Conductor, and projects marked urgent (claude-pool conductor urgent)."""
    return project in NEVER or project in urgent


def describe(level):
    return f'stage {level}: forecast ends before ' + ('the next reset' if level < 2 else 'half the time to the next reset')


def tokens(usage, key):
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def usd(model, usage):
    """API-price equivalent; cache writes the usage does not split by TTL count as five-minute writes."""
    price = next((p for key, p in PRICES if key in (model or '')), PRICES[2][1])
    written = usage.get('cache_creation')
    hour, five = tokens(written, 'ephemeral_1h_input_tokens'), tokens(written, 'ephemeral_5m_input_tokens')
    five += max(0, tokens(usage, 'cache_creation_input_tokens') - hour - five)
    return (tokens(usage, 'input_tokens') * price[0] + tokens(usage, 'output_tokens') * price[1] + tokens(usage, 'cache_read_input_tokens') * price[2]
            + five * price[0] * 1.25 + hour * price[0] * 2) / 1e6


def ledger_line(at, session, project, cls, model, account, usage, braked=False, waited=0):
    """One answered request: who, where, which model and account, tokens (cache writes by TTL when reported) and API-price
    equivalent; never content. `braked` with `brakeWaitSeconds`: it waited for the brake before it went upstream."""
    keys = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
    written = usage.get('cache_creation') if isinstance(usage.get('cache_creation'), dict) else {}
    return {'at': at, 'session': session, 'project': project or '', 'class': cls, 'model': model or '', 'account': account or '',
            **{k: usage.get(k, 0) for k in keys if isinstance(usage.get(k, 0), int)},
            **{k: written[k] for k in ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens') if isinstance(written.get(k), int)},
            'usd': round(usd(model, usage), 6), 'braked': bool(braked or waited),
            **({'brakeWaitSeconds': round(waited)} if waited else {})}


def wait_line(at, since, session, project, cls, reason, end):
    """One finished brake wait (times in ms): released, capped, cancelled, draining or failed. Never content."""
    return {'at': at, 'since': since, 'seconds': round(max(0, at - since) / 1000), 'session': session, 'project': project or '',
            'class': cls, 'reason': reason, 'end': end}


def wait_summary(path):
    """A day of brake waits: requests, seconds, by end and by project (seconds)."""
    ends, projects, total, count = defaultdict(int), defaultdict(int), 0, 0
    try: lines = path.read_text().splitlines()
    except OSError: lines = []
    for line in lines:
        try: row = json.loads(line)
        except ValueError: continue
        if not isinstance(row, dict) or not seconds(row.get('seconds')): continue
        count += 1; total += row['seconds']
        ends[str(row.get('end'))] += 1; projects[row.get('project') or '-'] += row['seconds']
    return {'requests': count, 'seconds': total, 'ends': dict(ends), 'projects': dict(projects)}


def today_summary(path):
    """Today's ledger by project, request class and Claude session id (subagents count under their main session); a
    session's project is the one it spent most on."""
    projects, classes, braked, total = defaultdict(float), defaultdict(float), 0, 0.0
    sessions = defaultdict(lambda: {'usd': 0.0, 'classes': defaultdict(float), 'projects': defaultdict(float)})
    try: lines = path.read_text().splitlines()
    except OSError: lines = []
    for line in lines:
        try: row = json.loads(line)
        except ValueError: continue
        if not isinstance(row, dict) or not isinstance(row.get('usd'), (int, float)): continue
        project, cls = row.get('project') or '-', row.get('class') or 'main'
        projects[project] += row['usd']; classes[cls] += row['usd']
        total += row['usd']; braked += bool(row.get('braked'))
        if isinstance(row.get('session'), str) and row['session']:
            one = sessions[row['session']]
            one['usd'] += row['usd']; one['classes'][cls] += row['usd']; one['projects'][project] += row['usd']
    return {'usd': total, 'braked': braked, 'classes': dict(classes),
            'projects': [{'project': p, 'usd': v} for p, v in sorted(projects.items(), key=lambda kv: -kv[1])],
            'sessions': [{'session': k, 'project': max(v['projects'], key=v['projects'].get), 'usd': v['usd'], 'classes': dict(v['classes'])}
                         for k, v in sorted(sessions.items(), key=lambda kv: -kv[1]['usd'])]}
