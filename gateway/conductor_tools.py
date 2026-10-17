"""Cheap tools for Conductor Claude: `claude-pool conductor snapshot | events | tell | journal`.

Conductor Claude keeps the Conductor Board and hands work to the agents in Justus's herdr sessions. These commands give it
one bounded, ANSI-free, redacted view instead of raw tool output. Every herdr call goes through `Tools`, so tests fake it.

Tool shapes (personal-vps 2026-10-17, controller-verified; herdr 0.9.5 usage strings and `herdr api schema --json`):
- `herdr session list --json` -> {"sessions":[{"name","running","default","session_dir","socket_path"}]}
- `herdr --session S agent list` -> {"result":{"agents":[{"agent","agent_status","cwd","name"?,"pane_id","workspace_id",...}]}}
- `herdr --session S workspace list` -> {"result":{"workspaces":[{"workspace_id","label",...}]}}
- `herdr --session S agent read <target> --source recent-unwrapped --lines N` -> {"result":{"read":{"text"}}}
- `herdr --session S agent prompt <target> <text>`; `agent start <name> --kind claude --pane ID` (pane at a shell prompt)
- `herdr --session S workspace create --cwd PATH --label TEXT --no-focus` -> result.workspace.workspace_id, result.root_pane.pane_id
- `herdr --session S pane process-info --pane ID` -> result.process_info.foreground_processes[{pid,name,argv,cmdline}]
- `herdr --session S workspace close <workspace_id>`
- optional: `preview list --json`; `gh pr list --repo O/R --json number,title,state,url,headRefName --limit 20`.
Only running sessions are ever addressed with `--session`; nothing here creates a herdr session or a project.
avd is left out: no read-only JSON listing of its decisions is pinned yet."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from private_files import private_directory, read_private, write_private
import settings

EVENTS = settings.RUNTIME / 'conductor-events.json'  # 0600 last seen state of sessions, agents, previews and PRs
QUEUE = settings.RUNTIME / 'conductor-queue.json'    # 0600 tasks waiting for a busy agent
JOURNAL = Path.home() / 'projects/conductor/journal.md'
POLL = 20               # seconds between `events` polls
PR_EVERY = 3            # ask gh every third poll (about once a minute)
BATCH = 600             # at most one batch line per ten minutes
KEEP_DAYS = 14
MAX_TEXT_LINES, MAX_TEXT_BYTES = 60, 6 * 1024
MAX_LAST = 160          # per-agent last line
MAX_SESSIONS, MAX_AGENTS, MAX_PREVIEWS, MAX_REPOS, MAX_PRS = 20, 40, 20, 10, 10
IDLE = ('idle', 'done')
ATTEMPTS = 3            # delivery attempts per queued task before it is dropped
STATE_LIMIT = 1 << 20

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]')
CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f]')
SECRETS = [(re.compile(r'sk-ant-[A-Za-z0-9_-]+'), '<redacted>'),
           (re.compile(r'(?i)\bBearer\s+\S+'), 'Bearer <redacted>'),
           (re.compile(r'\b[0-9a-fA-F]{40,}\b'), '<redacted>'),
           # base64/base64url runs of 40+ with a digit; slashes split paths, so a long path survives
           (re.compile(r'(?<![A-Za-z0-9+_-])(?=[A-Za-z0-9+_-]*\d)[A-Za-z0-9+_-]{40,}={0,2}'), '<redacted>'),
           # standard base64 (AWS-style keys contain `/`): upper, lower and a digit, and not a path (no leading `/`)
           (re.compile(r'(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/]{40,}={0,2}'),
            lambda m: m.group() if m.group().startswith('/') or not (re.search(r'[A-Z]', m.group()) and re.search(r'[a-z]', m.group())
                                                                      and re.search(r'\d', m.group())) else '<redacted>')]
CHROME = re.compile(r'^(?:[>❯›$#]\s*|\? for shortcuts.*|.*esc to interrupt.*|⏵⏵.*|.*shift\+tab to cycle.*)$', re.I)
GITHUB = re.compile(r'github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$')
AGENT_NAME = re.compile(r'[a-z][a-z0-9_-]{0,31}')


def clean(text, limit=None):
    """One display line: no ANSI or control characters, secrets redacted, whitespace collapsed, at most `limit` chars."""
    text = CONTROL.sub(' ', ANSI.sub('', str(text)))
    for pattern, replacement in SECRETS: text = pattern.sub(replacement, text)
    text = ' '.join(text.split())
    return text[:limit] if limit else text


def last_line(text):
    """The last line of terminal output that says something: no blank, prompt, status-bar or box-drawing lines."""
    for raw in reversed(CONTROL.sub(lambda m: m.group() if m.group() == '\n' else ' ', ANSI.sub('', text or '')).split('\n')):
        line = raw.strip().strip('│|┃ ').strip()
        if sum(c.isalnum() for c in line) < 2 or CHROME.match(line): continue
        return clean(line, MAX_LAST)
    return ''


def run_tool(argv, timeout=15):
    """Run an optional tool quietly; a missing tool, a timeout or a crash reads as a failed run."""
    if not shutil.which(argv[0]): return subprocess.CompletedProcess(argv, 127, '', '')
    env = dict(os.environ, NO_COLOR='1', GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1')
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
                              env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(argv, 255, '', '')


def _json(text):
    try: return json.loads(text)
    except ValueError: return None


class Tools:
    """The only place that runs herdr, preview, gh and git. None means "could not tell", never "nothing there"."""
    def __init__(self, run=run_tool): self.run = run

    def herdr(self, session, *args, timeout=15):
        """The reply on success (exit 0 and no `error` key; plain-text output reads as {}), None on failure."""
        done = self.run(['herdr', *(['--session', session] if session else []), *args], timeout)
        if done.returncode: return None
        data = _json(done.stdout)
        if not isinstance(data, dict): return {}
        if 'error' in data: return None
        return data['result'] if isinstance(data.get('result'), dict) else data

    def sessions(self):
        rows = (self.herdr(None, 'session', 'list', '--json') or {}).get('sessions')
        return [row for row in rows if isinstance(row, dict) and isinstance(row.get('name'), str)] if isinstance(rows, list) else None

    def agents(self, session):
        rows = (self.herdr(session, 'agent', 'list') or {}).get('agents')
        return [row for row in rows if isinstance(row, dict) and isinstance(row.get('pane_id'), str)] if isinstance(rows, list) else None

    def workspaces(self, session):
        rows = (self.herdr(session, 'workspace', 'list') or {}).get('workspaces')
        return {row.get('workspace_id'): row.get('label') or '' for row in rows if isinstance(row, dict)} if isinstance(rows, list) else {}

    def read(self, session, target, lines=40):
        done = self.run(['herdr', '--session', session, 'agent', 'read', target, '--source', 'recent-unwrapped', '--lines', str(lines)], 15)
        if done.returncode: return ''
        data = _json(done.stdout)
        if isinstance(data, dict):
            read = (data.get('result') or {}).get('read') if isinstance(data.get('result'), dict) else None
            return read.get('text', '') if isinstance(read, dict) else ''
        return done.stdout

    def prompt(self, session, target, text):
        return self.herdr(session, 'agent', 'prompt', target, text, timeout=30) is not None

    def create_workspace(self, session, cwd, label):
        result = self.herdr(session, 'workspace', 'create', *(['--cwd', cwd] if cwd else []), '--label', label, '--no-focus') or {}
        workspace, pane = result.get('workspace') or {}, result.get('root_pane') or {}
        return workspace.get('workspace_id'), pane.get('pane_id')

    def start_agent(self, session, name, pane):
        return self.herdr(session, 'agent', 'start', name, '--kind', 'claude', '--pane', pane, timeout=60) is not None

    def processes(self, session, pane):
        info = (self.herdr(session, 'pane', 'process-info', '--pane', pane) or {}).get('process_info') or {}
        rows = info.get('foreground_processes')
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def close_workspace(self, session, workspace):
        return self.herdr(session, 'workspace', 'close', workspace) is not None

    def previews(self):
        done = self.run(['preview', 'list', '--json'], 15)
        data = _json(done.stdout) if done.returncode == 0 else None
        if isinstance(data, dict): data = data.get('previews')
        if not isinstance(data, list): return None
        rows = {}
        for row in data:
            if not isinstance(row, dict): continue
            link = clean(row.get('link') or row.get('url') or row.get('proxy') or '', 200)
            label = clean(row.get('label') or '', 60)
            key = clean(row['port'] if row.get('port') is not None else label or link, 60)
            if key: rows[key] = {'port': row.get('port'), 'label': label, 'link': link}
        return rows

    def repo(self, cwd):
        """{'name': repo directory name, 'slug': owner/repo on GitHub or ''}; the remote URL itself never leaves here."""
        top = self.run(['git', '-C', cwd, 'rev-parse', '--show-toplevel'], 10)
        if top.returncode or not top.stdout.strip(): return {'name': Path(cwd).name, 'slug': ''}
        origin = self.run(['git', '-C', cwd, 'remote', 'get-url', 'origin'], 10)
        match = GITHUB.search(origin.stdout.strip()) if origin.returncode == 0 else None
        return {'name': Path(top.stdout.strip()).name, 'slug': match.group(1) + '/' + match.group(2) if match else ''}

    def prs(self, slug, merged_since=None):
        """{number: row} of open PRs, plus PRs merged since `merged_since` (two calls, so a merge behind twenty newer PRs
        is not missed); None when gh is missing, unauthenticated or fails."""
        fields = ['gh', 'pr', 'list', '--repo', slug, '--json', 'number,title,state,url,headRefName', '--limit', '20']
        calls = [fields]
        if merged_since is not None:
            calls.append(fields + ['--state', 'merged', '--search', 'merged:>=' + time.strftime('%Y-%m-%d', time.gmtime(merged_since))])
        rows = {}
        for argv in calls:
            done = self.run(argv, 30)
            data = _json(done.stdout) if done.returncode == 0 else None
            if not isinstance(data, list): return None
            rows.update({str(row['number']): {'state': str(row.get('state', '')), 'title': clean(row.get('title', ''), 100),
                                              'url': clean(row.get('url', ''), 200), 'branch': clean(row.get('headRefName', ''), 60)}
                         for row in data if isinstance(row, dict) and isinstance(row.get('number'), int)})
        return rows


# --- shared state files -------------------------------------------------------------------------------------------

def _read(path):
    try: return read_private(path, STATE_LIMIT)
    except FileNotFoundError: return {}


def _write(path, value):
    write_private(path, json.dumps(value, ensure_ascii=False, sort_keys=True))


@contextlib.contextmanager
def _locked(path):
    private_directory(path.parent)
    fd = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX); yield
    finally: os.close(fd)


def agent_label(row):
    return row['session'] + '/' + (row.get('name') or row['pane'])


def _agent_rows(tools, sessions):
    rows = []
    for session in sessions:
        if not session.get('running'): continue
        for agent in tools.agents(session['name']) or []:
            rows.append({'session': session['name'], 'pane': agent['pane_id'], 'name': agent.get('name') or '',
                         'status': agent.get('agent_status') or 'unknown', 'cwd': agent.get('cwd') or ''})
    return rows


def _ago(seconds):
    seconds = max(0, int(seconds))
    for unit, size in (('d', 86400), ('h', 3600), ('m', 60)):
        if seconds >= size: return str(seconds // size) + unit
    return str(seconds) + 's'


def _bounded(lines):
    """At most MAX_TEXT_LINES lines and MAX_TEXT_BYTES bytes, ending with a note of what was left out."""
    out, size = [], 0
    for index, line in enumerate(lines):
        rest = len(lines) - index
        note = '... ' + str(rest) + ' more lines (use --json)'
        if len(out) + 1 >= MAX_TEXT_LINES and rest > 1 or size + len(line.encode()) + 1 + len(note) + 1 > MAX_TEXT_BYTES:
            out.append(note); break
        out.append(line); size += len(line.encode()) + 1
    return '\n'.join(out)


# --- snapshot -----------------------------------------------------------------------------------------------------

def snapshot(tools=None, as_json=False, now=None, state_path=None, queue_path=None):
    tools, now = tools or Tools(), time.time() if now is None else now
    seen, queued = {}, []
    with contextlib.suppress(OSError, RuntimeError, ValueError): seen = _read(state_path or EVENTS).get('agents') or {}
    with contextlib.suppress(OSError, RuntimeError, ValueError): queued = _read(queue_path or QUEUE).get('items') or []
    sessions = tools.sessions()
    if sessions is None:
        return json.dumps({'error': 'herdr is not available'}) if as_json else 'herdr is not available on this machine'
    repos, value, shown, present, unknown = {}, {'sessions': [], 'previews': [], 'prs': {}}, 0, set(), set()
    for session in sessions[:MAX_SESSIONS]:
        entry = {'name': clean(session['name'], 60), 'running': bool(session.get('running')), 'workspaces': []}
        value['sessions'].append(entry)
        if not entry['running']: continue
        labels = tools.workspaces(session['name'])
        groups = {}
        agents = tools.agents(session['name'])
        if agents is None: unknown.add(session['name'])
        present.update(session['name'] + '/' + agent['pane_id'] for agent in agents or [])
        for agent in agents or []:
            if shown >= MAX_AGENTS: entry['more'] = entry.get('more', 0) + 1; continue
            shown += 1
            cwd = agent.get('cwd') or ''
            if cwd and cwd not in repos: repos[cwd] = tools.repo(cwd)
            repo = repos.get(cwd) or {'name': '', 'slug': ''}
            key, status = session['name'] + '/' + agent['pane_id'], agent.get('agent_status') or 'unknown'
            known = seen.get(key) or {}
            ws = agent.get('workspace_id') or ''
            groups.setdefault(ws, {'id': ws, 'label': clean(labels.get(ws) or '', 40), 'agents': []})['agents'].append({
                'name': clean(agent.get('name') or agent['pane_id'], 40), 'pane': agent['pane_id'], 'status': clean(status, 20),
                'since': known.get('since') if known.get('status') == status else None,
                'cwd': clean(cwd, 200), 'repo': repo['slug'] or repo['name'],
                'last': last_line(tools.read(session['name'], agent['pane_id'])),
                'queued': sum(1 for item in queued if item.get('session') == session['name'] and item.get('pane') == agent['pane_id'])})
        entry['workspaces'] = list(groups.values())
    value['previews'] = [dict(row) for row in list((tools.previews() or {}).values())[:MAX_PREVIEWS]]
    value['orphaned'] = [{'agent': item.get('session', '') + '/' + (item.get('agent') or item.get('pane', '')), 'task': clean(item.get('task', ''), 100)}
                         for item in queued if item.get('session') not in unknown
                         and item.get('session', '') + '/' + item.get('pane', '') not in present][:MAX_PREVIEWS]
    for slug in sorted({repo['slug'] for repo in repos.values() if repo['slug']})[:MAX_REPOS]:
        rows = tools.prs(slug)
        if rows: value['prs'][slug] = [{'number': int(n), **row} for n, row in list(rows.items())[:MAX_PRS] if row['state'] in ('', 'OPEN')]
    value['prs'] = {slug: rows for slug, rows in value['prs'].items() if rows}
    if as_json: return json.dumps(value, ensure_ascii=False)
    running = sum(1 for s in value['sessions'] if s['running'])
    lines = ['herdr sessions: ' + str(len(value['sessions'])) + ' (' + str(running) + ' running)']
    for entry in value['sessions']:
        lines.append(entry['name'] + (' running' if entry['running'] else ' stopped'))
        for group in entry['workspaces']:
            for row in group['agents']:
                parts = [row['status'] + (' ' + _ago(now - row['since']) if row['since'] else '')]
                if row['repo']: parts.append(row['repo'])
                if row['queued']: parts.append(str(row['queued']) + ' queued')
                line = '  [' + (group['label'] or group['id']) + '] ' + row['name'] + ' ' + ' · '.join(parts)
                lines.append(line + (' | ' + row['last'][:100] if row['last'] else ''))
        if entry.get('more'): lines.append('  ... ' + str(entry['more']) + ' more agents (use --json)')
    if value['previews']:
        lines.append('previews:')
        lines += ['  ' + ' '.join(str(part) for part in (row['label'], row['port'], row['link']) if part not in ('', None))
                  for row in value['previews']]
    if value['orphaned']:
        lines.append('queued for missing agents:')
        lines += ['  ' + row['agent'] + ' ' + row['task'] for row in value['orphaned']]
    if value['prs']:
        lines.append('open pull requests:')
        lines += ['  ' + slug + ' #' + str(row['number']) + ' ' + row['title'] + ' ' + row['url'] for slug, rows in value['prs'].items() for row in rows]
    return _bounded(lines)


# --- events -------------------------------------------------------------------------------------------------------

def _observe(tools, previous, with_prs, now):
    """Current sessions, agents, previews and PRs; parts that could not be read keep their previous value."""
    sessions = tools.sessions()
    if sessions is None: return None
    old_agents = previous.get('agents') or {}
    value = {'sessions': {row['name']: bool(row.get('running')) for row in sessions}, 'agents': {},
             'previews': previous.get('previews'), 'prs': dict(previous.get('prs') or {}), 'repos': dict(previous.get('repos') or {})}
    for name, running in value['sessions'].items():
        if not running: continue
        agents = tools.agents(name)
        if agents is None:
            value['agents'].update({key: dict(row) for key, row in old_agents.items() if row.get('session') == name}); continue
        for agent in agents:
            value['agents'][name + '/' + agent['pane_id']] = {
                'session': name, 'pane': agent['pane_id'], 'name': agent.get('name') or '', 'cwd': agent.get('cwd') or '',
                'status': agent.get('agent_status') or 'unknown'}
    previews = tools.previews()
    if previews is not None: value['previews'] = previews
    if with_prs:
        cwds = {row['cwd'] for row in value['agents'].values() if row['cwd']}
        for cwd in cwds:
            if cwd not in value['repos']: value['repos'][cwd] = tools.repo(cwd)['slug']
        value['repos'] = {cwd: slug for cwd, slug in value['repos'].items() if cwd in cwds}
        slugs = sorted({slug for slug in value['repos'].values() if slug})[:MAX_REPOS]
        value['prs'] = {slug: rows for slug, rows in value['prs'].items() if slug in slugs}
        for slug in slugs:
            rows = tools.prs(slug, merged_since=now - 86400)
            if rows is not None: value['prs'][slug] = rows
    return value


def _project(row, repos):
    slug = repos.get(row.get('cwd'))
    return (slug.split('/')[-1] if slug else Path(row['cwd']).name) if row.get('cwd') else ''


def _preview_text(row):
    return ' '.join(part for part in (row.get('label') or '', row.get('link') or '') if part)


def _changes(old, new, now, first):
    """(important events as (kind, subject, detail), number of unimportant changes); also stamps each agent's `since`."""
    events, minor = [], 0
    old_agents = old.get('agents') or {}
    for key, row in new['agents'].items():
        before = old_agents.get(key)
        row['since'] = before['since'] if before and before.get('status') == row['status'] and before.get('since') else now
        if first: continue
        status, previous = row['status'], before.get('status') if before else None
        if status == previous: continue
        if status == 'blocked': events.append(('agent-blocked', key, row))
        elif status == 'done': events.append(('agent-done', key, row))
        elif status == 'idle' and previous in ('working', 'blocked', 'unknown'): events.append(('agent-waiting', key, row))
        else: minor += 1
    if first: return [], 0
    old_sessions = old.get('sessions') or {}
    for name, running in new['sessions'].items():
        if running and not old_sessions.get(name): events.append(('session-started', name, None))
    for name, running in old_sessions.items():
        if running and not new['sessions'].get(name): events.append(('session-ended', name, None))
    ended = {name for name, running in old_sessions.items() if running and not new['sessions'].get(name)}
    minor += sum(1 for key, row in old_agents.items() if key not in new['agents'] and row.get('session') not in ended)
    if old.get('previews') is not None and new.get('previews') is not None:
        for key, row in new['previews'].items():
            if key not in old['previews']: events.append(('preview-started', key, _preview_text(row)))
        for key, row in old['previews'].items():
            if key not in new['previews']: events.append(('preview-stopped', key, _preview_text(row)))
    for slug, rows in new['prs'].items():
        known = (old.get('prs') or {}).get(slug)
        if known is None: continue
        for number, row in rows.items():
            before = known.get(number)
            subject = slug + '#' + number
            if before is None and row['state'] == 'OPEN': events.append(('pr-opened', subject, row))
            elif row['state'] == 'MERGED' and (before is None or before['state'] == 'OPEN'): events.append(('pr-merged', subject, row))
            elif before and before['state'] != row['state']: minor += 1
    return events, minor


def _event_line(kind, subject, detail, tools, new):
    if kind.startswith('agent-'):
        project = _project(detail, new.get('repos') or {})
        last = last_line(tools.read(detail['session'], detail['pane']))
        text = project + (': ' + last if last else '') if project else last
        return clean('conductor-event ' + kind + ' ' + agent_label(detail) + (' ' + text if text else ''), 300)
    if kind.startswith('pr-'): return clean('conductor-event ' + kind + ' ' + subject + ' ' + detail['title'] + ' ' + detail['url'], 300)
    return clean('conductor-event ' + kind + ' ' + subject + (' ' + detail if detail else ''), 300)


def _deliver(tools, agents, queue_path):
    """Prompt idle agents with their oldest queued task (one per agent per poll). A failed prompt, or a poll on which the
    agent is gone (reported once as orphaned), counts as an attempt; after ATTEMPTS the task is dropped as undeliverable."""
    with _locked(queue_path):
        items = _read(queue_path).get('items') or []
    lines, busy, changes = [], set(), {}
    for item in items:
        key = item.get('session', '') + '/' + item.get('pane', '')
        label = item.get('session', '') + '/' + (item.get('agent') or item.get('pane', ''))
        row = agents.get(key)
        if row is not None and (row['status'] not in IDLE or key in busy):
            if item.get('orphaned'): changes[item['id']] = {**item, 'orphaned': False}
            continue
        if row is None:
            if not item.get('orphaned'): lines.append(clean('conductor-event orphaned ' + label + ' ' + item['task'], 200))
        else:
            busy.add(key)
            if tools.prompt(row['session'], row['pane'], item['task']):
                changes[item['id']] = None
                lines.append(clean('conductor-event delivered ' + agent_label(row) + ' ' + item['task'], 200))
                continue
        attempts = int(item.get('attempts') or 0) + 1
        if attempts >= ATTEMPTS:
            changes[item['id']] = None
            lines.append(clean('conductor-event undeliverable ' + label + ' ' + item['task'], 200))
        else: changes[item['id']] = {**item, 'attempts': attempts, 'orphaned': row is None}
    if changes:
        with _locked(queue_path):
            value = _read(queue_path)
            kept = []
            for other in value.get('items') or []:
                if other.get('id') not in changes: kept.append(other)
                elif changes[other['id']] is not None: kept.append(changes[other['id']])
            value['items'] = kept
            _write(queue_path, value)
    return lines


def tick(state_path=None, queue_path=None, tools=None, now=None, first=False, with_prs=True):
    """One poll: compare with the saved state and return the lines to print. `first` (the first poll after a start) only
    seeds: it prints nothing for what already happened, but keeps each agent's `since`, the batch clock and the queue."""
    return _tick(state_path, queue_path, tools, now, first, with_prs)[0]


def _tick(state_path, queue_path, tools, now, first, with_prs):
    """(lines, observed): observed is False when herdr could not be read and nothing was compared or saved."""
    state_path, queue_path = state_path or EVENTS, queue_path or QUEUE
    tools, now = tools or Tools(), time.time() if now is None else now
    try: old = _read(state_path)
    except (OSError, RuntimeError, ValueError): old = {}  # bad JSON, too big or wrong mode: reseed instead of going silent
    new = _observe(tools, old, with_prs, now)
    if new is None: return [], False
    events, minor = _changes(old, new, now, first or not old)
    new['batchAt'] = old.get('batchAt') or now
    new['pending'] = (old.get('pending') or 0) + minor
    lines = [_event_line(kind, subject, detail, tools, new) for kind, subject, detail in events]
    if new['pending'] and now - new['batchAt'] >= BATCH:
        lines.append('conductor-event batch ' + str(new['pending']) + ' changes')
        new['pending'], new['batchAt'] = 0, now
    _write(state_path, new)
    if not (first or not old) and queue_path.exists(): lines += _deliver(tools, new['agents'], queue_path)
    return lines, True


@contextlib.contextmanager
def _watcher_lock(state_path):
    """Held for a watcher's lifetime; yields False when another watcher holds it."""
    private_directory(state_path.parent)
    fd = os.open(str(state_path) + '.watch.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False; return
        yield True
    finally: os.close(fd)


def events(state_path=None, queue_path=None, interval=POLL, tools=None, stop=None, clock=time.time, write=None):
    """The Monitor watcher: polls until SIGTERM/SIGINT (or `stop`), one line per important event. One per machine."""
    write = write or (lambda line: print(line, flush=True))
    with _watcher_lock(state_path or EVENTS) as held:
        if not held:
            write('conductor-events: another watcher is already running on this machine'); return 1
        return _watch(state_path, queue_path, interval, tools, stop, clock, write)


def _watch(state_path, queue_path, interval, tools, stop, clock, write):
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGTERM, signal.SIGINT): signal.signal(number, lambda *_: stop.set())
    count, first = 0, True
    while not stop.is_set():
        try:
            lines, observed = _tick(state_path, queue_path, tools, clock(), first, count % PR_EVERY == 0)
            for line in lines: write(line)
            if observed: first = False  # seeding waits for the first poll that actually reaches herdr
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            pass  # one bad poll never stops the watcher; the next poll compares against the last good state
        count += 1
        stop.wait(interval)
    return 0


# --- tell ---------------------------------------------------------------------------------------------------------

def _resolve(target, rows, tools):
    if '/' in target:
        session, _, who = target.partition('/')
        found = [row for row in rows if row['session'] == session and who in (row['name'], row['pane'])]
    else:
        found = [row for row in rows if row['name'] == target] or [row for row in rows if row['session'] == target]
        if not found:
            repos = {}
            for row in rows:
                if row['cwd'] and row['cwd'] not in repos: repos[row['cwd']] = tools.repo(row['cwd'])
            found = [row for row in rows if row['cwd'] and target in (repos[row['cwd']]['name'], repos[row['cwd']]['slug'],
                                                                       repos[row['cwd']]['slug'].split('/')[-1])]
    if not found: return None, 'no agent matches ' + target + ' (try `claude-pool conductor snapshot`)'
    if len(found) == 1: return found[0], None
    return None, 'ambiguous ' + target + ': ' + ', '.join(agent_label(row) + ' (' + row['status'] + ')' for row in found)


def runs_pool(processes):
    """True when a foreground process of the pane is the pool launcher (`.../launch.py exec ...`)."""
    for process in processes:
        argv = process.get('argv') or []
        for index, word in enumerate(argv):
            if isinstance(word, str) and word.endswith('launch.py') and 'exec' in argv[index + 1:]: return True
        if re.search(r'launch\.py\s+exec\b', process.get('cmdline') or ''): return True
    return False


def _parallel(row, task, rows, tools, sleep):
    session = row['session']
    base = re.sub(r'[^a-z0-9_-]+', '-', (row['name'] or Path(row['cwd']).name or 'agent').lower()).strip('-')[:28]
    base = base if AGENT_NAME.fullmatch(base or '') else 'agent'
    taken, number = {other['name'] for other in rows if other['session'] == session}, 2
    while base + '-' + str(number) in taken: number += 1
    name = base + '-' + str(number)
    workspace, pane = tools.create_workspace(session, row['cwd'], name)
    if not pane: return 1, 'could not create a workspace in ' + session
    started = False
    for attempt in range(5):
        if tools.start_agent(session, name, pane): started = True; break
        sleep(1)  # a new workspace's shell needs a moment to reach its prompt
    if not started or not runs_pool(tools.processes(session, pane)):
        if workspace: tools.close_workspace(session, workspace)
        if not started: return 1, 'could not start an agent in a new workspace of ' + session + '; closed it'
        return 1, 'stopped ' + session + '/' + name + ': it did not run through the pool (launch.py exec); check the claude shell function'
    if not tools.prompt(session, pane, task): return 1, 'started ' + session + '/' + name + ' through the pool but could not prompt it'
    return 0, 'started ' + session + '/' + name + ' in a new workspace through the pool and prompted it'


def tell(target, task, parallel=False, tools=None, queue_path=None, now=None, sleep=time.sleep):
    """(exit code, one line). Idle agent: prompt it. Busy: start a second pool agent in a new workspace of the same session
    with `parallel`, otherwise queue for `events` to deliver. Several matches fail with the candidates. Never creates
    herdr sessions or projects."""
    code, line = _tell(target, task, parallel, tools, queue_path, now, sleep)
    return code, clean(line, 600)


def _tell(target, task, parallel, tools, queue_path, now, sleep):
    tools, queue_path, now = tools or Tools(), queue_path or QUEUE, time.time() if now is None else now
    task = ' '.join(str(task).split())
    if not task: return 1, 'empty task'
    sessions = tools.sessions()
    if sessions is None: return 1, 'herdr is not available on this machine'
    rows = _agent_rows(tools, sessions)
    row, problem = _resolve(target, rows, tools)
    if problem: return 1, problem
    if row['status'] in IDLE:
        if tools.prompt(row['session'], row['pane'], task): return 0, 'prompted ' + agent_label(row)
        return 1, 'could not prompt ' + agent_label(row) + ' (herdr refused; it may be at a question)'
    if parallel: return _parallel(row, task, rows, tools, sleep)
    with _locked(queue_path):
        value = _read(queue_path)
        value.setdefault('items', []).append({'id': uuid.uuid4().hex[:12], 'session': row['session'], 'pane': row['pane'],
                                              'agent': row['name'], 'task': task, 'at': now})
        _write(queue_path, value)
    return 0, 'queued for ' + agent_label(row)


# --- journal ------------------------------------------------------------------------------------------------------

ENTRY = re.compile(r'^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) ')


def duration(text):
    match = re.fullmatch(r'(\d+)([mhd])', str(text).strip())
    if not match: raise ValueError('Duration must look like 30m, 12h or 14d')
    return int(match.group(1)) * {'m': 60, 'h': 3600, 'd': 86400}[match.group(2)]


def _stamp(line):
    match = ENTRY.match(line)
    if not match: return None
    try: return time.mktime(time.strptime(match.group(1), '%Y-%m-%d %H:%M'))
    except ValueError: return None


@contextlib.contextmanager
def _journal_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.parent / ('.' + path.name + '.lock')), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX); yield
    finally: os.close(fd)


def _replace(path, text):
    """Atomic rewrite that keeps the file's mode (0600 for a new journal)."""
    mode = path.stat().st_mode & 0o7777 if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'w') as file:
            file.write(text); file.flush(); os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def journal_add(path, line, now=None):
    path = Path(path)
    entry = '- ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() if now is None else now)) + ' ' + clean(line, 500)
    with _journal_lock(path):
        text = path.read_text() if path.exists() else ''
        _replace(path, text + ('' if not text or text.endswith('\n') else '\n') + entry + '\n')
    return entry


def journal_show(path, since, now=None, limit=200):
    path, now = Path(path), time.time() if now is None else now
    if not path.exists(): return ''
    lines = [line for line in path.read_text().splitlines() if (_stamp(line) or 0) >= now - since]
    return '\n'.join(lines[-limit:])


def journal_prune(path, now=None, keep_days=KEEP_DAYS):
    path, now = Path(path), time.time() if now is None else now
    if not path.exists(): return 0
    with _journal_lock(path):
        lines = path.read_text().splitlines()
        kept = [line for line in lines if _stamp(line) is None or _stamp(line) >= now - keep_days * 86400]
        if len(kept) != len(lines): _replace(path, '\n'.join(kept) + ('\n' if kept else ''))
    return len(lines) - len(kept)


# --- command line -------------------------------------------------------------------------------------------------

def cli(argv):
    parser = argparse.ArgumentParser(prog='claude-pool conductor', description='Cheap tools for Conductor Claude.')
    commands = parser.add_subparsers(dest='command', required=True)
    shot = commands.add_parser('snapshot', help='every herdr session, agent, last output line, preview link and open PR, bounded')
    shot.add_argument('--json', action='store_true')
    watch = commands.add_parser('events', help='watcher for the Monitor tool: one `conductor-event` line per important change')
    watch.add_argument('--interval', type=float, default=POLL, help=argparse.SUPPRESS)
    told = commands.add_parser('tell', help='prompt the right agent, or queue the task until it is idle')
    told.add_argument('target', help='agent name, session, session/agent or project')
    told.add_argument('task', nargs='+')
    told.add_argument('--parallel', action='store_true', help='busy agent: start a second pool agent in a new workspace of its session')
    journal = commands.add_parser('journal', help='append-only journal (default ' + str(JOURNAL).replace(str(Path.home()), '~') + ')')
    verbs = journal.add_subparsers(dest='verb', required=True)
    add = verbs.add_parser('add'); add.add_argument('line', nargs='+')
    show = verbs.add_parser('show'); show.add_argument('--since', default='1d')
    prune = verbs.add_parser('prune')
    for sub in (add, show, prune): sub.add_argument('--file', type=Path, default=JOURNAL)
    args = parser.parse_args(argv)
    if args.command == 'snapshot': print(snapshot(as_json=args.json)); return 0
    if args.command == 'events': return events(interval=args.interval)
    if args.command == 'tell':
        code, line = tell(args.target, ' '.join(args.task), parallel=args.parallel)
        print(line); return code
    if args.verb == 'add': journal_add(args.file, ' '.join(args.line)); return 0
    if args.verb == 'show':
        try: since = duration(args.since)
        except ValueError as error: parser.error(str(error))
        text = journal_show(args.file, since)
        if text: print(text)
        return 0
    print('pruned ' + str(journal_prune(args.file)) + ' entries'); return 0
