"""Install the pool on a target over SSH: Native check, source install, service, linger and the `claude` shell function.
The archive travels on stdin only. Nothing here runs sudo; a root-only step is reported as a command for the owner."""
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

import device_sync
import managed_block
from device_report import Step
from remote import redact

START, END = '# >>> claude-pool >>>', '# <<< claude-pool <<<'
FUNCTION = 'claude() { command claude-pool exec "$@"; }'
NATIVE_FIX = 'curl -fsSL https://claude.ai/install.sh | bash'
SKIP = ('gateway/bin/', '.superpowers/')

NATIVE_SCRIPT = '''for c in "$HOME/.local/bin/claude" "$(command -v claude 2>/dev/null)"; do
  [ -n "$c" ] && [ -f "$c" ] && [ -x "$c" ] || continue
  grep -q 'managed by claude-pool installer' "$c" 2>/dev/null && continue
  printf '%s\\n' "$c"; exit 0
done
exit 1'''
INSTALL_SCRIPT = '''set -e
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
tar -xzf - -C "$dir"
python3 "$dir/claude-pool/install.py" --bootstrap --native-claude "$1"'''


def _text(data): return (data or b'').decode('utf-8', 'replace').strip()


def _tail(done):
    return redact('\n'.join(_text(done.stderr).splitlines()[-5:]), []) or 'exit ' + str(done.returncode)


def source_archive(root):
    root = Path(root)
    spec = importlib.util.spec_from_file_location('claude_pool_package', root / 'scripts/package.py')
    package = importlib.util.module_from_spec(spec); spec.loader.exec_module(package)
    source_files = package.source_files
    files = {}
    for name in source_files(root):
        if name.startswith(SKIP) or '.pool-client-token' in name or name.endswith('.pyc'): continue
        files[name] = (root / name).read_bytes()
    raw = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w') as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo('claude-pool/' + name)
            info.size = len(data); info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def check_native(remote):
    done = remote.call(['sh', '-c', NATIVE_SCRIPT])
    path = _text(done.stdout).splitlines()[0] if done.returncode == 0 and _text(done.stdout) else None
    if not path: return Step('native-claude', 'missing', 'Native Claude is not installed', NATIVE_FIX), None
    return Step('native-claude', 'ok', path, None), path


def _status(remote):
    done = remote.pool('device', 'status', '--json')
    if done.returncode: return done, None
    try: value = json.loads(_text(done.stdout))
    except ValueError: return done, None
    return done, value if isinstance(value, dict) else None


def check_install(remote):
    done, value = _status(remote)
    if done.returncode == 127: return Step('install', 'missing', 'claude-pool is not installed', 'claude-pool device add ' + remote.name)
    if value is None: return Step('install', 'failed', 'claude-pool did not report its status: ' + _tail(done), 'claude-pool device add ' + remote.name)
    return Step('install', 'ok', 'claude-pool installed', None)


def check_version(remote, version):
    _done, value = _status(remote)
    have = value.get('version') if value else None
    if have == version: return Step('version', 'ok', str(version), None)
    return Step('version', 'missing', f'{have or "unknown"} here, {version} wanted', 'claude-pool device add ' + remote.name)


def install(remote, archive, native):
    done = remote.call(['sh', '-c', INSTALL_SCRIPT, 'install', native], stdin=archive, timeout=3600)
    if done.returncode: return Step('install', 'failed', 'install failed: ' + _tail(done), 'claude-pool device add ' + remote.name)
    return Step('install', 'ok', 'installed from this checkout', None)


def check_service(remote):
    done, value = _status(remote)
    state = value.get('service') if value else None
    if state == 'running': return Step('service', 'ok', 'running', None)
    return Step('service', 'missing', 'service is ' + (state or 'unknown'), 'claude-pool device add ' + remote.name)


def start_service(remote):
    done = remote.pool('--start')
    if done.returncode: return Step('service', 'failed', 'start failed: ' + _tail(done), 'claude-pool --start')
    return check_service(remote)


def check_doctor(remote):
    done = remote.pool('doctor')
    if done.returncode == 0: return Step('doctor', 'ok', 'no findings', None)
    fix = 'claude-pool doctor'
    for line in _text(done.stdout).splitlines():
        if line.startswith('  fail '):
            fix = line.partition(' · fix: ')[2] or fix
            return Step('doctor', 'failed', line.strip(), fix)
    return Step('doctor', 'failed', 'doctor exited ' + str(done.returncode), fix)


def _linger_value(remote):
    done = remote.call(['sh', '-c', 'loginctl show-user "$USER" -p Linger --value'])
    return done, _text(done.stdout)


def check_linger(remote):
    if _text(remote.call(['uname', '-s']).stdout) == 'Darwin': return Step('linger', 'skipped', 'launchd keeps services running', None)
    done, value = _linger_value(remote)
    if done.returncode == 127: return Step('linger', 'skipped', 'no systemd here', None)
    if value == 'yes': return Step('linger', 'ok', 'services survive logout', None)
    return Step('linger', 'missing', 'services stop at logout', 'claude-pool device add ' + remote.name)


def enable_linger(remote):
    step = check_linger(remote)
    if step.status != 'missing': return step
    remote.call(['loginctl', 'enable-linger'])
    step = check_linger(remote)
    if step.status == 'ok': return step
    user = _text(remote.call(['id', '-un']).stdout) or '<user>'
    return Step('linger', 'failed', 'needs root to keep services running after logout', 'sudo loginctl enable-linger ' + user)


def shell_rc(env=os.environ):
    home = Path(env.get('HOME') or Path.home())
    return home / ('.zshrc' if env.get('SHELL', '').endswith('zsh') else '.bashrc')


def _legacy(rc):
    try: text = Path(rc).read_text()
    except OSError: return False
    outside = text
    lines, first, last = managed_block._split(text, START, END)
    if first is not None and last is not None: outside = ''.join(lines[:first] + lines[last + 1:])
    return any(line.strip() == FUNCTION for line in outside.splitlines())


def shell_check(rc):
    return managed_block.read(rc, START, END) is not None or _legacy(rc)


def shell_add(rc):
    return False if shell_check(rc) else managed_block.write(rc, START, END, FUNCTION)


def shell_remove(rc):
    return managed_block.write(rc, START, END, None)


def check_shell(remote):
    if remote.pool('device', 'shell', 'check').returncode == 0: return Step('shell', 'ok', 'claude goes through the pool', None)
    return Step('shell', 'missing', 'no claude function in the shell rc file', 'claude-pool device add ' + remote.name)


def add_shell(remote):
    done = remote.pool('device', 'shell', 'add')
    if done.returncode: return Step('shell', 'failed', 'could not add it: ' + _tail(done), 'claude-pool device add ' + remote.name)
    return check_shell(remote)


def remove_shell(remote):
    return remote.pool('device', 'shell', 'remove').returncode == 0


def _local_status():
    value = {'linger': None, 'shell': False}
    try:
        if os.uname().sysname != 'Darwin':
            done = subprocess.run(['loginctl', 'show-user', os.environ.get('USER', ''), '-p', 'Linger', '--value'], capture_output=True, text=True, timeout=10)
            if done.returncode == 0: value['linger'] = done.stdout.strip() == 'yes'
    except Exception: pass
    try: value['shell'] = shell_check(shell_rc())
    except Exception: pass
    return value


device_sync.STATUS_PARTS.append(_local_status)
