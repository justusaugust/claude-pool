"""Pool states that need the Conductor, as lines in its wake.log (agreed with board-design, 22.10.2026).

A start writes `<time> pool <art> an conductor: <sentence>` and wakes the Conductor through its event watcher, at most once
per art and hour; the end of an announced episode writes the routine line `<time> pool <art> vorbei: <sentence>`. Arts:
backend (three failed probes, agents get 503), konten-limit (no account can answer), anmeldung (one account needs a new
login: an episode, a line and a quiet window per account, naming this device and the `claude-pool login` to run there),
gesperrt (Anthropic refuses an account, the pool paused it) and bremse (brake stage 2). Nothing is written when the Conductor folder has no state directory.
A device that is not the Conductor's delivers each line to the Conductor's inbox over ssh instead (`conductor_route`); an episode
counts as announced only once delivered, a failed delivery is retried every RETRY seconds while it lasts. Episode state
survives daemon restarts in a 0600 file next to the other pool state."""
import json
import os
from pathlib import Path
import re
import subprocess
import time

import registry
from private_files import read_private, write_private
from remote import Remote
from telemetry import capacity

LINE = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} pool [a-z-]+ (an conductor|vorbei): [^\n]{1,300}')
QUIET = 3600            # seconds between two wakes of the same episode (art, or art and account)
RETRY = 300             # seconds between two tries to deliver a line to the Conductor device
SEND_TIMEOUT = 20
SENTENCE = 300
MODEL = 'claude-opus-5-5'


def _clock(ms):
    return time.strftime('%H:%M', time.localtime(ms / 1000)) if ms else 'unbekannt'


def conductor_route(remote=Remote):
    """A function that gives `send(text, key) -> bool` when this device is registered and another device is the Conductor's, else None.
    `send` queues the line in that device's inbox (`conductor inbox add`, an existing command, so no newer version is needed there)."""
    def route():
        try:
            value, me = registry.load(), registry.self_name()
            entry = registry.conductor(value)
            row = entry and registry.device(value, entry['device'])
            if not me or not row or not row.get('ssh') or entry['device'] == me: return None
            target = remote(row['ssh'])
        except (OSError, ValueError, KeyError, TypeError): return None
        def send(text, key):
            try: return target.pool('conductor', 'inbox', 'add', '--key', key, text, timeout=SEND_TIMEOUT).returncode == 0
            except (OSError, ValueError, subprocess.SubprocessError): return False
        return send
    return route


class Alerts:
    def __init__(self, wake_log, state_path, clock=time.time, device=None, route=None, journal=None):
        self.wake_log, self.state_path, self.clock = Path(wake_log), Path(state_path), clock
        self.device, self.route, self.journal = device or (lambda: None), route or (lambda: None), journal
        try: self.state = read_private(self.state_path)
        except (OSError, RuntimeError, ValueError): self.state = {}

    def _record(self, code, **details):
        try: self.journal and self.journal.record(code, **details)
        except (OSError, RuntimeError, ValueError): pass

    def _write(self, art, verb, sentence, key):
        """One line to the Conductor: through the route on another device, else into the local wake.log. False when a delivery failed."""
        sentence, send, me = ' '.join(str(sentence).split()), self.route(), self.device()
        if send and me and 'Gerät ' + me not in sentence: sentence = 'Gerät ' + me + ': ' + sentence
        sentence = sentence[:SENTENCE] or art
        line = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(self.clock())) + ' pool ' + art + ' ' + verb + ': ' + sentence
        if send:
            try: sent = bool(send(line, 'pool-' + key.replace(':', '-') + '-' + verb.split()[0]))
            except Exception: sent = False   # a broken transport never reaches the supervise loop
            if not sent: self._record('alert_forward_failed', reason=art)
            return sent
        if not self.wake_log.parent.is_dir(): return True
        fd = os.open(self.wake_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try: os.write(fd, (line + '\n').encode())
        finally: os.close(fd)
        return True

    def update(self, art, active, start, end, key=None):
        """One edge of one episode (`key`, default the art): a new episode wakes unless the last wake of it is under QUIET seconds old.
        A line that could not be delivered stays owed and is tried again every RETRY seconds while the episode lasts."""
        key = key or art
        row, now = dict(self.state.get(key) or {}), self.clock()
        if active:
            if not row.get('active'): row.update(active=True, announced=False, owed=now - row.get('woke', -QUIET) >= QUIET)
            elif not row.get('owed') or now - row.get('tried', -RETRY) < RETRY: return
            if row['owed']:
                row['tried'] = now
                if self._write(art, 'an conductor', start, key): row.update(announced=True, owed=False, woke=now)
        else:
            if not row.get('active'): return
            if row.get('announced'): self._write(art, 'vorbei', end, key)
            row.update(active=False, announced=False, owed=False)
        self.state[key] = row
        try: write_private(self.state_path, json.dumps(self.state, sort_keys=True))
        except (OSError, RuntimeError): pass

    def sign_ins(self, rows):
        """One episode per account: it starts when the account needs a new login on this device and ends when it is valid again."""
        me, now = self.device(), {}
        for r in rows: now['anmeldung:' + str(r.get('name') or r['label'])] = r
        self.state.pop('anmeldung', None)   # the one episode for all accounts of earlier versions
        for key in sorted({*now, *(k for k in self.state if k.startswith('anmeldung:'))}):
            r, name = now.get(key), key.split(':', 1)[1]
            label, was = (r or {}).get('label') or name, bool((self.state.get(key) or {}).get('active'))
            active, dev = bool(r) and r.get('status') == 'reauthenticate', (' auf Gerät ' + me) if me else ''
            self.update('anmeldung', active, 'Konto ' + label + ' braucht eine neue Anmeldung' + dev + ': claude-pool login ' + name
                        + (' dort ausführen' if me else ' ausführen'), 'Konto ' + label + dev + ' wieder angemeldet', key)
            if active != was: self._record('account_needs_login' if active else 'account_login_valid', account=name)

    def check(self, server):
        """Read the pool's own view: backend health, the last account sample and the brake stage."""
        down = server.health.get('failures', 0) >= 3
        self.update('backend', down, 'Backend gestört: drei Prüfungen ohne Antwort, Agenten bekommen 503',
                    'Backend antwortet wieder')
        if down: return
        rows, now = list(server.last_accounts or []), int(self.clock() * 1000)
        if rows:
            quota = capacity(rows, MODEL, now)
            free = [r['label'] for r in rows if capacity([r], MODEL, now)['state'] == 'ready']
            waiting = 'Alle Konten am Limit, Anfragen warten' + (' bis ' + _clock(quota['nextResetAt']) if quota['nextResetAt'] else '')
            self.update('konten-limit', quota['state'] != 'ready',
                        waiting if quota['state'] == 'quota_wait' else 'Kein Konto verfügbar: pausiert oder Anmeldung nötig',
                        (', '.join(free) + ' wieder frei') if free else 'Konten wieder frei')
            self.sign_ins(rows)
            blocked = dict(getattr(server, 'blocked', None) or {})
            if blocked:
                name, row = min(blocked.items(), key=lambda item: item[1]['until'])
                label = next((r.get('label') or name for r in rows if r.get('name') == name), name)
                start = ('Konto ' + label + ': Anthropic lässt seine Organisation nicht über Claude Code anmelden (' + row['code']
                         + '), der Pool hat es pausiert bis ' + _clock(row['until']) + ' und versucht es dann neu. Prüfen oder aus dem Pool nehmen')
            else: start = ''
            self.update('gesperrt', bool(blocked), start, 'Kein Konto mehr gesperrt')
        info = server.brake_info() or {}
        self.update('bremse', (info.get('stage') or 0) >= 2,
                    'Bremse Stufe 2: Prognose reicht nicht bis zur Hälfte der Zeit bis zum nächsten Reset um ' + _clock(info.get('resetAt')),
                    'Bremse unter Stufe 2')
