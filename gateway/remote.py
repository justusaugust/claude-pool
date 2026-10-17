"""Run commands on another machine over ssh. Secrets travel on stdin only, never in argv or output."""
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Iterable, NamedTuple

POOL = '.local/bin/claude-pool'
_TARGET = re.compile(r'[A-Za-z0-9._@:-]{1,253}')
_RECEIPT = Path(__file__).resolve().parent.parent / 'installation.json'


class Completed(NamedTuple):
    returncode: int
    stdout: bytes
    stderr: bytes


def redact(text, secrets: Iterable[str]):
    for secret in secrets:
        if secret: text = text.replace(secret, '[redacted]')
    return text


def require_pipe(stream):
    if stream.isatty(): raise RuntimeError('Refusing to write a credential to a terminal')


class Remote:
    def __init__(self, target, run=subprocess.run):
        if not isinstance(target, str) or not _TARGET.fullmatch(target) or target.startswith('-'):
            raise ValueError('Invalid ssh target')
        self.name = target
        self._run = run

    def _command(self, argv):
        return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-T', self.name, shlex.join(argv)]

    def call(self, argv, stdin=None, timeout=120):
        try:
            done = self._run(self._command(argv), input=stdin, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return Completed(255, b'', b'timed out')
        except OSError as error:
            return Completed(255, b'', type(error).__name__.encode())
        return Completed(done.returncode, done.stdout or b'', done.stderr or b'')

    def pool(self, *args, stdin=None, timeout=120):
        return self.call([POOL, *args], stdin=stdin, timeout=timeout)

    def reachable(self):
        return self.call(['true'], timeout=15).returncode == 0


class Local(Remote):
    def __init__(self, run=subprocess.run, receipt=_RECEIPT):
        self.name = 'local'
        self._run = run
        self._receipt = Path(receipt)

    def _command(self, argv): return list(argv)

    def _launcher(self):
        try: launchers = json.loads(self._receipt.read_text()).get('launchers', {})
        except (OSError, ValueError): launchers = {}
        for path in launchers:
            if Path(path).name == 'claude-pool': return path
        return str(Path.home() / POOL)

    def pool(self, *args, stdin=None, timeout=120):
        return self.call([self._launcher(), *args], stdin=stdin, timeout=timeout)

    def reachable(self): return True
