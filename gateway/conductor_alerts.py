"""Pool states that need the Conductor, as lines in its wake.log (agreed with board-design, 22.10.2026).

A start writes `<time> pool <art> an conductor: <sentence>` and wakes the Conductor through its event watcher, at most once
per art and hour; the end of an announced episode writes the routine line `<time> pool <art> vorbei: <sentence>`. Arts:
backend (three failed probes, agents get 503), konten-limit (no account can answer), anmeldung (an account needs a new
login) and bremse (brake stage 2). Nothing is written when the Conductor folder has no state directory. Episode state
survives daemon restarts in a 0600 file next to the other pool state."""
import json
import os
from pathlib import Path
import re
import time

from private_files import read_private, write_private
from telemetry import capacity

LINE = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} pool [a-z-]+ (an conductor|vorbei): [^\n]{1,300}')
QUIET = 3600            # seconds between two wakes of the same art
SENTENCE = 300
MODEL = 'claude-opus-5-5'


def _clock(ms):
    return time.strftime('%H:%M', time.localtime(ms / 1000)) if ms else 'unbekannt'


class Alerts:
    def __init__(self, wake_log, state_path, clock=time.time):
        self.wake_log, self.state_path, self.clock = Path(wake_log), Path(state_path), clock
        try: self.state = read_private(self.state_path)
        except (OSError, RuntimeError, ValueError): self.state = {}

    def _write(self, art, verb, sentence):
        if not self.wake_log.parent.is_dir(): return
        sentence = ' '.join(str(sentence).split())[:SENTENCE] or art
        line = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(self.clock())) + ' pool ' + art + ' ' + verb + ': ' + sentence + '\n'
        fd = os.open(self.wake_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try: os.write(fd, line.encode())
        finally: os.close(fd)

    def update(self, art, active, start, end):
        """One edge of one art: a new episode wakes unless the last wake of this art is under QUIET seconds old."""
        row, now = dict(self.state.get(art) or {}), self.clock()
        if active == bool(row.get('active')): return
        if active:
            row.update(active=True, announced=now - row.get('woke', -QUIET) >= QUIET)
            if row['announced']:
                row['woke'] = now
                self._write(art, 'an conductor', start)
        else:
            if row.get('announced'): self._write(art, 'vorbei', end)
            row.update(active=False, announced=False)
        self.state[art] = row
        try: write_private(self.state_path, json.dumps(self.state, sort_keys=True))
        except (OSError, RuntimeError): pass

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
            relogin = [r['label'] for r in rows if r.get('status') == 'reauthenticate']
            who = 'Konto ' + relogin[0] + ' braucht' if len(relogin) == 1 else 'Konten ' + ', '.join(relogin) + ' brauchen'
            self.update('anmeldung', bool(relogin), who + ' eine neue Anmeldung', 'Anmeldungen wieder gültig')
        info = server.brake_info() or {}
        self.update('bremse', (info.get('stage') or 0) >= 2,
                    'Bremse Stufe 2: Prognose reicht nicht bis zur Hälfte der Zeit bis zum nächsten Reset um ' + _clock(info.get('resetAt')),
                    'Bremse unter Stufe 2')
