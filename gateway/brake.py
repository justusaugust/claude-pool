"""Brake for background model work when the pooled forecast does not reach the next reset, and a per-day usage ledger.

Stage 1 (forecast below the time until an account next gains capacity): new subagent and workflow conversations start on
Sonnet 5.5. Stage 2 (below half that time): their effort is also capped at high. The model is chosen once, at the first
request of an agent, and kept for its lifetime: caches and thinking blocks belong to one model. An agent counts as new
while its messages hold no assistant turn; one first seen later (after a pool restart) keeps its model. Main sessions, the
Conductor, compaction and projects marked urgent are never braked. Replayed on 21.10.2026 this removed a 65-minute stall.
"""
import json
import time
from collections import defaultdict

SONNET = 'claude-sonnet-5-5'
BRAKED = ('opus', 'fable', 'mythos')
NEVER = {'conductor-wecker'}           # COCKPIT_PROJECT of Conductor Claude
CLASSES = {'subagent', 'workflow'}
STICKY_LIMIT = 4096
# $ per million tokens: input, output, cache read; a cache write costs 1.25 x input (API list prices, October 2026).
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


class Brake:
    """Per-agent model decisions; an agent first seen mid-conversation keeps the model it asked for."""
    def __init__(self):
        self.agents = {}

    def decide(self, headers, request, info, project, urgent=()):
        cls, agent = headers.get('x-claude-code-request-class', 'main'), headers.get('x-claude-code-agent-id', '')
        model = request.get('model')
        if cls not in CLASSES or not agent or not isinstance(model, str) or not any(m in model for m in BRAKED):
            return None, None
        decision = self.agents.get(agent)
        if decision is None:
            # Fresh means no model has answered yet. Counting messages missed every agent: Claude Code 2.1.312 sends the
            # first request with the prompt and a system message announcing tools (0 braked rows, 22.-29.10.).
            messages = request.get('messages') if isinstance(request.get('messages'), list) else []
            fresh = not any(isinstance(m, dict) and m.get('role') == 'assistant' for m in messages)
            level = info.get('stage', 0) if fresh and project not in NEVER and project not in urgent else 0
            decision = {'model': SONNET if level else None, 'capEffort': level >= 2, 'stage': level, 'at': time.time()}
            if len(self.agents) >= STICKY_LIMIT:
                for old in sorted(self.agents, key=lambda k: self.agents[k]['at'])[:STICKY_LIMIT // 4]: del self.agents[old]
            self.agents[agent] = decision
        if not decision['model']: return None, decision
        new = {k: v for k, v in request.items() if k != 'speed'}
        new['model'] = decision['model']
        if decision['capEffort'] and isinstance(new.get('output_config'), dict) and new['output_config'].get('effort') in {'xhigh', 'max'}:
            new['output_config'] = {**new['output_config'], 'effort': 'high'}
        return new, decision


def usd(model, usage):
    price = next((p for key, p in PRICES if key in (model or '')), PRICES[2][1])
    number = lambda k: usage.get(k) if isinstance(usage.get(k), (int, float)) and not isinstance(usage.get(k), bool) else 0
    return (number('input_tokens') * price[0] + number('output_tokens') * price[1] + number('cache_read_input_tokens') * price[2]
            + number('cache_creation_input_tokens') * price[0] * 1.25) / 1e6


def ledger_line(at, session, project, cls, model, account, usage, braked=False):
    """One answered request: who, where, which model and account, tokens and API-price equivalent; never content."""
    keys = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
    return {'at': at, 'session': session, 'project': project or '', 'class': cls, 'model': model or '', 'account': account or '',
            **{k: usage.get(k, 0) for k in keys if isinstance(usage.get(k, 0), int)}, 'usd': round(usd(model, usage), 6), 'braked': bool(braked)}


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
