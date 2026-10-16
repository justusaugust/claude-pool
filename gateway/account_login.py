"""Run Shunt's registered OAuth flow; keep pasted codes inside the host process."""
import errno
import getpass
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
from urllib.parse import parse_qs, urlsplit


def private_code(label):
    if sys.platform == 'darwin' and shutil.which('osascript'):
        script = '''on run argv
          display dialog (item 1 of argv) default answer "" with hidden answer buttons {"Cancel", "Connect"} default button "Connect" with title "Claude Pool — Connect account"
          return text returned of result
        end run'''
        try:
            result = subprocess.run(['osascript', '-e', script,
                f'Authorize {label} in the browser, then paste the complete code here. It stays out of chat and logs.'],
                capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise RuntimeError('Code entry timed out; run account add again.') from None
        if result.returncode:
            raise RuntimeError('Connection cancelled; existing accounts are unchanged.')
        return result.stdout.strip()
    if not sys.stdin.isatty():
        raise RuntimeError('Secure input unavailable. Run this command in your own terminal.')
    return getpass.getpass('Authorization code (hidden): ').strip()


def validate_code(value, state):
    code, separator, returned = value.partition('#')
    return bool(separator and code and len(value) <= 8192 and returned == state and
                all(33 <= ord(c) <= 126 for c in value))


def login(binary, stores, name, label, code_reader=private_code, timeout=900):
    """Own exactly one child PTY, never echo its raw output or a pasted code."""
    target = Path(stores) / (name + '.json')
    if target.exists() or target.is_symlink():
        raise RuntimeError('Credential already exists; verify it instead of overwriting it.')
    pid, fd = pty.fork()
    if pid == 0:
        attrs = termios.tcgetattr(0)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(0, termios.TCSANOW, attrs)
        env = {**os.environ, 'SHUNT_CLAUDE_ACCOUNTS_DIR': str(stores)}
        os.execve(str(binary), [str(binary), 'login', 'claude', '--name', name,
                               '--mode', 'oauth', '--manual'], env)
    buffer, state, submitted, exited = '', None, False, False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], .2)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    chunk = b''
                if chunk:
                    buffer = (buffer + chunk.decode('utf-8', errors='replace'))[-32768:]
                    if state is None:
                        match = re.search(r'https://claude\.com/cai/oauth/authorize\?[^\s]+', buffer)
                        if match:
                            url = match.group()
                            state = parse_qs(urlsplit(url).query).get('state', [None])[0]
                            print('Browser authorization opened. Sign in to ' + label + '.', flush=True)
                            print('If the browser did not open:\n' + url, flush=True)
                    if not submitted and state and 'input hidden' in buffer:
                        for _ in range(3):
                            value = code_reader(label)
                            if validate_code(value, state):
                                break
                            print('That code belongs to another login or is incomplete. Copy the full code from this browser flow.', flush=True)
                        else:
                            raise RuntimeError('No matching authorization code supplied; nothing was enrolled.')
                        if target.exists() or target.is_symlink():
                            raise RuntimeError('Credential appeared during login; refusing to overwrite it.')
                        # ECHO stays disabled even if the child restores its original terminal settings.
                        os.write(fd, (value + '\n').encode())
                        value = ''
                        submitted = True
                        buffer = ''
            child, status = os.waitpid(pid, os.WNOHANG)
            if child:
                exited = True
                if os.waitstatus_to_exitcode(status) != 0 or not target.is_file():
                    raise RuntimeError('Authorization did not complete. Run account add again; existing accounts are unchanged.')
                return
        raise RuntimeError('Authorization timed out; run account add again.')
    finally:
        os.close(fd)
        if not exited:
            # Only the OAuth child created above can be terminated here.
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
            os.waitpid(pid, 0)
