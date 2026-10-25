"""The Native Claude login of this machine, and of a machine `device add` sets up.

`claude-native auth status --json` prints one object. Confirmed key names (2026-10-17): analyticsDisabled,
apiProvider, authMethod, configDirectory, email, loggedIn, orgId, orgName, projectsDirectory,
subscriptionType. The account email is `email`, the organization name is `orgName`.

How `claude-native auth login` hands out its authorize URL, probed 2026-10-17 on example-vps (Native 2.x, a
throwaway HOME, cancelled before anything was authorized): it PRINTS a manual-fallback URL whose `redirect_uri` is
`https://platform.claude.com/oauth/code/callback` (the paste-a-code flow), and it hands the browser command
(`$BROWSER`, and `xdg-open` when DISPLAY is set) a different URL whose `redirect_uri` is
`http://localhost:<random port>/callback` (the loopback flow). The port differs per run. The authorize host is
`https://claude.com/cai/oauth/authorize`.

So `login` points `$BROWSER` on the target at a tiny helper that only reports the URL back as one marker line, forwards
the loopback port over the ssh connection and opens the URL in this machine's browser: the Authorize click is the only
thing left to do. The printed platform URL is ignored; no code is ever read or asked for.
"""
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

import accounts_cli
from device_report import Step
import device_sync
from remote import _TARGET
import settings

STEP = 'native-login'
MARKER_WAIT = 60  # seconds the target's Native gets to hand its authorize URL to the browser helper
_MARKER = re.compile(rb'claude-pool-open (\S+)[\r\n]')
_AUTHORIZE_HOSTS = {'claude.com', 'claude.ai'}
_CALLBACK = re.compile(r'http://(?:localhost|127\.0\.0\.1):([0-9]{1,5})/callback')

# Runs on the target as `sh -c REMOTE_SCRIPT claude-pool-login <native>`; the Native path arrives as $1, never in the text.
# The helper reports the URL twice: on /dev/tty, and through "$d/url", which a background loop prints on the script's
# own stdout, so a browser launcher that detaches from the terminal still hands it over. The local side acts once.
REMOTE_SCRIPT = r'''set -u
w=
d=$(mktemp -d) || exit 1
trap 'kill $w 2>/dev/null; rm -rf "$d"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
chmod 700 "$d"
cat > "$d/open" <<'HELPER'
#!/bin/sh
dir=$(dirname "$0")
printf '%s\n' "$1" > "$dir/url.$$" && mv -f "$dir/url.$$" "$dir/url"
{ printf 'claude-pool-open %s\n' "$1" > /dev/tty; } 2>/dev/null || printf 'claude-pool-open %s\n' "$1" >&2
HELPER
chmod 700 "$d/open"
(
  while kill -0 $$ 2>/dev/null && [ -d "$d" ] && [ ! -e "$d/url" ]; do sleep 0.2; done
  [ -e "$d/url" ] && printf 'claude-pool-open %s\n' "$(cat "$d/url")"
) &
w=$!
unset DISPLAY WAYLAND_DISPLAY
BROWSER="$d/open" "$1" auth login
'''


def parse_auth_status(text):
    """{'email', 'organization'} of the signed-in Native login; None only when the status says nobody is signed in.
    Raises ValueError for output it cannot read, so a caller never mistakes a broken status for "signed out"."""
    value = json.loads(text)  # ValueError on non-JSON
    if not isinstance(value, dict) or not isinstance(value.get('loggedIn'), bool): raise ValueError('Unreadable Native auth status')
    if not value['loggedIn']: return None
    email, organization = value.get('email'), value.get('orgName')
    if not isinstance(email, str) or not email: raise ValueError('Native auth status has no email')
    return {'email': email, 'organization': organization if isinstance(organization, str) else ''}


def _unreachable_fix(target):
    return 'ssh ' + target + ' true must work without a password prompt (BatchMode)'


def _served(identity, accounts, stores):
    """Whether a registry account is this login: by its email (normalized; organizations compared only when both sides
    name one), or by the stored profile for a row without an email. A row whose profile cannot be read is skipped."""
    value = {'accounts': [row for row in accounts if isinstance(row, dict) and isinstance(row.get('name'), str)]}
    for row in value['accounts']:
        try:
            if accounts_cli.matches_native(identity, row['name'], value, stores): return True
        except RuntimeError:
            continue
    return False


def check(remote, native: str, accounts: list, stores=None, conductor_here=True) -> Step:
    """Whether `remote` has a Native login, and whether the pool serves that account. A login the pool does not serve only
    fails where the Conductor runs (Remote Control needs it there); elsewhere it is not needed (skipped)."""
    device = remote.name
    done = remote.call([native, 'auth', 'status', '--json'], timeout=30)
    if done.returncode == 255 and not done.stdout.strip():
        return Step(STEP, 'unreachable', 'could not reach ' + device + ' over ssh', _unreachable_fix(device))
    unreadable = Step(STEP, 'failed', 'could not read the Native login on ' + device, 'claude-native auth status')
    try: identity = parse_auth_status(done.stdout.decode('utf-8', 'replace'))
    except ValueError: return unreadable
    if identity is None: return Step(STEP, 'missing', 'no Native login on ' + device, 'claude-pool device add ' + device)
    if done.returncode: return unreadable
    shown = identity['email'] + (' (' + identity['organization'] + ')' if identity['organization'] else '')
    if _served(identity, accounts, settings.STORES if stores is None else stores): return Step(STEP, 'ok', shown)
    if not conductor_here:
        return Step(STEP, 'skipped', shown + ' is not a pool account; not needed, no Conductor runs here', '')
    return Step(STEP, 'failed', shown + ' is not an account the pool serves',
                'log in on ' + device + ' with an account the pool serves: claude-native auth login')


def loopback_port(url):
    """The callback port of a Claude authorize URL (https, host claude.com or claude.ai, path ending in /oauth/authorize)
    whose redirect_uri is exactly http://localhost:<port>/callback or http://127.0.0.1:<port>/callback, else None."""
    try:
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.hostname not in _AUTHORIZE_HOSTS or parts.netloc.lower() != parts.hostname: return None
        if not parts.path.endswith('/oauth/authorize'): return None
        redirects = parse_qs(parts.query).get('redirect_uri', [])
    except ValueError:
        return None
    match = _CALLBACK.fullmatch(redirects[0]) if len(redirects) == 1 else None
    port = int(match.group(1)) if match else 0
    return port if 1 <= port <= 65535 else None


def _manual(target, native):
    return Step(STEP, 'manual', 'the Native login on ' + target + ' cannot finish with the Authorize click alone',
                'Native login on ' + target + ' needs you once: in a terminal on your Mac run ssh -t ' + target + ' '
                + shlex.quote(native) + ' auth login, open the link it prints, click Authorize and finish there. '
                'Then rerun claude-pool device add ' + target + '.')


def _quiet(argv, timeout):
    """Run a local command with no terminal I/O at all; its exit code, 255 when it could not run or timed out."""
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 255


def _reap(pid, seconds):
    """Exit code of the child once it ends within `seconds`; None while it still runs."""
    end = time.monotonic() + seconds
    while True:
        try: done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError: return -1
        if done: return os.waitstatus_to_exitcode(status)
        if time.monotonic() >= end: return None
        time.sleep(0.05)


def _stop(pid):
    """End the child and everything it started (pty.fork makes it a session and group leader)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try: os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            try: os.kill(pid, sig)
            except ProcessLookupError: pass
        if _reap(pid, 2) is not None: return


def _session(target, native, opener, spawn, ssh, socket, timeout):
    command = [ssh, '-S', socket, '-tt', '-o', 'BatchMode=yes', target,
               shlex.join(['sh', '-c', REMOTE_SCRIPT, 'claude-pool-login', native])]
    pid, fd = spawn()
    if pid == 0:  # the child: become the ssh session; never return into the caller's code
        try: os.execvp(command[0], command)
        finally: os._exit(127)
    started = time.monotonic()
    deadline, handoff = started + timeout, started + min(MARKER_WAIT, timeout)
    seen, opened, code = b'', False, None
    try:
        while True:
            now = time.monotonic()
            if not opened and now >= handoff: return _manual(target, native)
            if now >= deadline:
                return Step(STEP, 'failed', 'the Native login on ' + target + ' was not authorized in time',
                            'claude-pool device add ' + target)
            ready, _, _ = select.select([fd], [], [], min((deadline if opened else handoff) - now, 1.0))
            if not ready: continue
            try: chunk = os.read(fd, 4096)
            except OSError: chunk = b''  # EIO once the child side of the pty is closed
            if not chunk: break
            if opened: continue  # child output is drained and dropped, never shown
            seen = (seen + chunk)[-65536:]
            match = _MARKER.search(seen)
            if not match: continue
            url = match.group(1).decode('ascii', 'replace')
            port = loopback_port(url)
            if port is None: return _manual(target, native)
            spec = '127.0.0.1:%d:127.0.0.1:%d' % (port, port)
            if _quiet([ssh, '-S', socket, '-O', 'forward', '-L', spec, target], 30) != 0: return _manual(target, native)
            try: shown = opener(url)
            except Exception: shown = False
            if shown is False: return _manual(target, native)
            opened, seen = True, b''
        code = _reap(pid, max(1.0, deadline - time.monotonic()))
    finally:
        # Hang up the terminal first: a child still writing to an unread pty would block on exit while it drains.
        try: os.close(fd)
        except OSError: pass
        if code is None: _stop(pid)
    if code == 0: return Step(STEP, 'ok', 'logged in on ' + target)
    if code == 255 and not opened: return Step(STEP, 'unreachable', 'could not reach ' + target + ' over ssh', _unreachable_fix(target))
    if not opened: return _manual(target, native)
    return Step(STEP, 'failed', 'the Native login on ' + target + ' did not finish after the browser opened',
                'claude-pool device add ' + target)


def login(target: str, native: str, opener=webbrowser.open, spawn=pty.fork, ssh='ssh', timeout=900) -> Step:
    """Log `target` into Native Claude with one Authorize click in this machine's browser, or say what the user does by hand.
    It never reads, asks for or passes on a code, and never shows the child's output."""
    if not isinstance(target, str) or not _TARGET.fullmatch(target) or target.startswith('-'): raise ValueError('Invalid ssh target')
    if not isinstance(native, str) or not native or '\0' in native: raise ValueError('Invalid Native path')
    folder = tempfile.mkdtemp(prefix='cpl-')  # 0700; holds only the control socket
    socket = os.path.join(folder, 'm')
    try:
        master = [ssh, '-M', '-S', socket, '-fN', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                  '-o', 'ClearAllForwardings=yes', '-o', 'GatewayPorts=no', target]
        if _quiet(master, 30) != 0:
            return Step(STEP, 'unreachable', 'could not open an ssh connection to ' + target, _unreachable_fix(target))
        return _session(target, native, opener, spawn, ssh, socket, timeout)
    finally:
        _quiet([ssh, '-S', socket, '-O', 'exit', target], 15)
        shutil.rmtree(folder, ignore_errors=True)


def local_status():
    """STATUS_PARTS contribution: this machine's Native login, None when signed out or unreadable."""
    try: identity = device_sync.default_native_identity()
    except Exception: identity = None
    if not identity: return {'nativeLogin': None}
    return {'nativeLogin': {'email': identity['email'], 'organization': identity['organization']}}


device_sync.STATUS_PARTS.append(local_status)
