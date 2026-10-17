"""Bounded operational metadata, never provider messages or request payloads."""
from collections import deque
import json
import re
import threading
import time

CODES = {
    'service_started': 'Background service started',
    'service_draining': 'Finishing active responses before stopping',
    'backend_unresponsive': 'Backend is not responding',
    'backend_recovered': 'Backend is responding again',
    'backend_exited': 'Backend process exited',
    'backend_restarted': 'Backend restarted',
    'restart_deferred': 'Restart deferred until active responses finish',
    'restart_failed': 'Backend restart failed; backing off',
    'request_rejected': 'Request capacity reached; retry shortly',
    'upstream_unavailable': 'Backend connection failed',
    'upstream_http_error': 'Provider request failed',
    'stream_error': 'Provider reported a stream error',
    'stream_interrupted': 'Response ended before completion',
    'client_disconnected': 'Claude connection closed; upstream cancelled',
    'request_idle_timeout': 'Response stalled; connection closed',
    'history_index_rebuilt': 'History index rebuilt from the preserved archive',
    'readings_sync_failed': 'Shared usage readings could not be fetched',
    'storage_failed': 'Recovery metadata could not be saved',
}


class Journal:
    def __init__(self, path, writer):
        self.path, self.writer = path, writer
        self.lock = threading.RLock()
        self.rows = deque(maxlen=256)
        self.error = False
        if path:
            try:
                if path.stat().st_size > 256 * 1024: raise ValueError()
                for row in json.loads(path.read_text())[-256:]:
                    clean = self.clean(row)
                    if clean: self.rows.append(clean)
            except FileNotFoundError: pass
            except (OSError, ValueError, TypeError, KeyError): self.error = True

    @staticmethod
    def clean(row):
        if not isinstance(row, dict) or row.get('code') not in CODES: return None
        stamp = row.get('at')
        if not isinstance(stamp, int) or stamp < 0: return None
        result = {'at': stamp, 'code': row['code']}
        for key in ('status', 'restarts', 'requests'):
            value = row.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000000: result[key] = value
        value = row.get('account')
        if isinstance(value, str) and re.fullmatch(r'[a-z0-9-]{1,80}', value): result['account'] = value
        value = row.get('reason')
        if isinstance(value, str) and re.fullmatch(r'[a-z0-9 :_.-]{1,80}', value): result['reason'] = value
        return result

    def record(self, code, **details):
        row = self.clean({'at': int(time.time() * 1000), 'code': code, **details})
        if not row: return
        with self.lock:
            # Coalesce bursts instead of writing the same outage on every poll.
            if self.rows and {k: v for k, v in self.rows[-1].items() if k != 'at'} == {k: v for k, v in row.items() if k != 'at'} and row['at'] - self.rows[-1]['at'] < 30000: return
            self.rows.append(row)
            if self.path:
                try:
                    self.writer(self.path, json.dumps(list(self.rows)))
                    self.error = False
                except (OSError, ValueError): self.error = True

    def snapshot(self):
        with self.lock:
            return [{**r, 'message': CODES[r['code']]} for r in reversed(self.rows)]
