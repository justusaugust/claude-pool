"""Run commands on another machine over ssh. Secrets travel on stdin only, never in argv or output."""
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Iterable, NamedTuple

POOL = '.local/bin/claude-pool'
OUTDATED, NO_LAUNCHER = 86, 87
# Runs `<wrapper> device "$@"` only when the release the wrapper starts has gateway/devices_cli.py (resolved exactly like
# device_install.LAUNCHER_SCRIPT). An older launcher would take the words as a Claude prompt and start its pool, so it is never run.
# $1 is the wrapper path ('' = ~/.local/bin/claude-pool); every argument is positional, never interpolated.
DEVICE_SCRIPT = r'''wrapper=${1:-"$HOME/.local/bin/claude-pool"}; shift
[ -f "$wrapper" ] || exit 87
line=$(grep '^exec ' "$wrapper" | tail -n 1)
rest=${line% \"\$@\"}
case "$rest" in
  *\') run=${rest%\'}; run=${run##*\'} ;;
  *) run=${rest##* } ;;
esac
dir=$(dirname "$run")
if [ -L "$dir/current" ]; then cli="$dir/current/gateway/devices_cli.py"; else cli="$dir/gateway/devices_cli.py"; fi
[ -n "$run" ] && [ -f "$cli" ] || exit 86
exec "$wrapper" device "$@"'''
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


LAUNCHERS = ('claude-pool', 'preview')


def remote_command(target, command):
    """A command for the user or an agent to run on `target` over ssh. Non-interactive ssh skips ~/.profile (and Debian's
    ~/.bashrc returns early), so ~/.local/bin is not on PATH there; ssh starts in the home folder, so the claude-pool and
    preview launchers are named by their home-relative path."""
    first = command.split(' ', 1)[0]
    if first in LAUNCHERS: command = '.local/bin/' + command
    return 'ssh ' + target + ' ' + command


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
            # With no stdin the child gets /dev/null, so ssh never swallows the caller's own stdin.
            feed = {'stdin': subprocess.DEVNULL} if stdin is None else {'input': stdin}
            done = self._run(self._command(argv), capture_output=True, timeout=timeout, **feed)
        except subprocess.TimeoutExpired:
            return Completed(255, b'', b'timed out')
        except OSError as error:
            return Completed(255, b'', type(error).__name__.encode())
        return Completed(done.returncode, done.stdout or b'', done.stderr or b'')

    def pool(self, *args, stdin=None, timeout=120):
        return self.call([POOL, *args], stdin=stdin, timeout=timeout)

    def device(self, *args, stdin=None, timeout=120):
        """`claude-pool device <args>` on the target; exit 86 (outdated launcher) or 87 (none) without running anything."""
        return self.call(['sh', '-c', DEVICE_SCRIPT, 'claude-pool-device', '', *args], stdin=stdin, timeout=timeout)

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

    def device(self, *args, stdin=None, timeout=120):
        return self.call(['sh', '-c', DEVICE_SCRIPT, 'claude-pool-device', self._launcher(), *args], stdin=stdin, timeout=timeout)

    def reachable(self): return True
