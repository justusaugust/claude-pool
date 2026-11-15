"""Pool states that need the Conductor, as lines in its wake.log (agreed with board-design, 22.10.2026).

A start writes `<time> pool <art> an conductor: <sentence>` and wakes the Conductor through its event watcher, at most once
per art and hour; the end of an announced episode writes the routine line `<time> pool <art> vorbei: <sentence>`, which never
wakes. Arts:
backend (three failed probes, agents get 503), konten-limit (no account can answer), anmeldung (one account needs a new
login: an episode, a line and a quiet window per account, naming this device and the `claude-pool login` to run there),
gesperrt (Anthropic refuses an account, the pool paused it) and bremse (brake stage 2, which comes and goes with the forecast:
it wakes only once it has lasted BRAKE_HOLD seconds, at most once in BRAKE_QUIET seconds) and ablauf (a long-lived token
runs out: one wake per account with 30 days left, one more with 7 days left). Nothing is written when the Conductor
folder has no state directory.
A device that is not the Conductor's delivers each start to the Conductor's inbox over ssh instead (`conductor_route`) and each
end to its wake.log (`conductor inbox note`); an episode counts as announced only once delivered, a failed delivery is retried
every RETRY seconds while it lasts. Episode state
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
from long_lived import FAIL_DAYS
from telemetry import capacity

LINE = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} pool [a-z-]+ (an conductor|vorbei): [^\n]{1,300}')
QUIET = 3600            # seconds between two wakes of the same episode (art, or art and account)
BRAKE_HOLD = 600        # seconds brake stage 2 must last before it wakes: the forecast crosses the line for minutes at a time
BRAKE_QUIET = 6 * 3600  # seconds between two brake wakes: the cause (too few accounts carry the load) does not change within hours
EXPIRY_QUIET = 86400    # seconds between two wakes for the same stage of one running-out token
RETRY = 300             # seconds between two tries to deliver a line to the Conductor device
SEND_TIMEOUT = 20
SENTENCE = 300
MODEL = 'claude-opus-5-5'


def _clock(ms):
    return time.strftime('%H:%M', time.localtime(ms / 1000)) if ms else 'unbekannt'


def conductor_route(remote=Remote):
    """A function that gives `send(text, key, wake=True) -> bool` when this device is registered and another device is the Conductor's,
    else None. `send` queues the line in that device's inbox (`conductor inbox add`, which wakes the Conductor), or with `wake=False`
    appends it to that device's wake.log as a routine line (`conductor inbox note`)."""
    def route():
        try:
            value, me = registry.load(), registry.self_name()
            entry = registry.conductor(value)
            row = entry and registry.device(value, entry['device'])
            if not me or not row or not row.get('ssh') or entry['device'] == me: return None
            target = remote(row['ssh'])
        except (OSError, ValueError, KeyError, TypeError): return None
        def send(text, key, wake=True):
            words = ('add', '--key', key, text) if wake else ('note', text)
            try: return target.pool('conductor', 'inbox', *words, timeout=SEND_TIMEOUT).returncode == 0
            except (OSError, ValueError, subprocess.SubprocessError): return False
        return send
    return route


def _append(wake_log, line):
    fd = os.open(wake_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try: os.write(fd, (line + '\n').encode())
    finally: os.close(fd)


def note(line, wake_log):
    """Append the end line another device sends to this device's wake.log. Only a `pool <art> vorbei` line is taken: the file is
    read by the Conductor's watcher, and a start has to pass the inbox."""
    line, wake_log = ' '.join(str(line).split()), Path(wake_log)
    found = LINE.fullmatch(line)
    if not found or found.group(1) != 'vorbei': return False
    if wake_log.parent.is_dir(): _append(wake_log, line)
    return True


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
        """One line to the Conductor: through the route on another device, else into the local wake.log. False when a delivery failed.
        Only a start wakes; an end is a routine line on either way."""
        sentence, send, me = ' '.join(str(sentence).split()), self.route(), self.device()
        if send and me and 'Gerät ' + me not in sentence: sentence = 'Gerät ' + me + ': ' + sentence
        sentence = sentence[:SENTENCE] or art
        line = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(self.clock())) + ' pool ' + art + ' ' + verb + ': ' + sentence
        if send:
            try: sent = bool(send(line, 'pool-' + key.replace(':', '-') + '-' + verb.split()[0], wake=verb != 'vorbei'))
            except Exception: sent = False   # a broken transport never reaches the supervise loop
            if not sent: self._record('alert_forward_failed', reason=art)
            return sent
        if not self.wake_log.parent.is_dir(): return True
        _append(self.wake_log, line)
        return True

    def update(self, art, active, start, end, key=None, hold=0, quiet=QUIET):
        """One reading of one episode (`key`, default the art). It wakes once it has lasted `hold` seconds and the last wake of it
        is `quiet` seconds old, so an episode that ends before that writes nothing and one that outlasts the quiet window is still
        told. A line that could not be delivered is tried again every RETRY seconds while the episode lasts."""
        key = key or art
        row, now = dict(self.state.get(key) or {}), self.clock()
        last = {'woke': row['woke']} if 'woke' in row else {}   # the last wake outlives the episode
        if active:
            new = not row.get('active')
            if new: row = {'active': True, 'announced': False, 'since': now, **last}
            due = (not row.get('announced') and now - row.get('since', 0) >= hold and now - row.get('woke', -quiet) >= quiet
                   and now - row.get('tried', -RETRY) >= RETRY)
            if due:
                row['tried'] = now
                if self._write(art, 'an conductor', start, key): row.update(announced=True, woke=now)
            elif not new: return
        else:
            if not row.get('active'): return
            if row.get('announced'): self._write(art, 'vorbei', end, key)
            row = {'active': False, 'announced': False, **last}
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

    def expiry(self, rows, labels=None):
        """Long-lived tokens that run out (`long_lived.expiring`): per account one wake when WARN_DAYS are left and one more
        when FAIL_DAYS are left, and one routine line once the token is renewed."""
        by_name, first, second = {r['name']: r for r in rows}, 'ablauf:frist:', 'ablauf:bald:'
        known = {k.split(':', 2)[2] for k in self.state if k.startswith((first, second))}
        for name in sorted({*by_name, *known}):
            r, label = by_name.get(name), (labels or {}).get(name) or name
            soon, start = bool(r) and r['days'] <= FAIL_DAYS, ''
            if r:
                left = int(r['days'])
                when = ('ist abgelaufen' if r['days'] <= 0 else 'läuft am ' + time.strftime('%d.%m.%Y', time.localtime(r['expiresAt'] / 1000))
                        + ' ab, noch ' + str(left) + (' Tag' if left == 1 else ' Tage'))
                start = 'Konto ' + label + ': Jahres-Token ' + when + '. Auf der Anmeldeseite erneuern'
            end = 'Konto ' + label + ': Jahres-Token erneuert'
            if soon and not (self.state.get(first + name) or {}).get('active'):
                # First seen with FAIL_DAYS or fewer left: the nearer stage speaks for both.
                self.state[first + name] = {'active': True, 'announced': True, 'since': self.clock(), 'woke': self.clock()}
            else: self.update('ablauf', bool(r), start, end, first + name, quiet=EXPIRY_QUIET)
            if not soon and second + name in self.state:
                self.state[second + name] = {**self.state[second + name], 'announced': False}   # the renewal is said once, by the first stage
            self.update('ablauf', soon, start, end, second + name, quiet=EXPIRY_QUIET)

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
                    'Bremse unter Stufe 2', hold=BRAKE_HOLD, quiet=BRAKE_QUIET)
