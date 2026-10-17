"""Preview links per device: the standalone `preview` tool, packaged unchanged, installed on Linux devices.

`preview <port>` runs a small proxy as a `systemd --user` unit and prints a Tailscale link (HTTPS through Tailscale Serve
when the user may use it, otherwise HTTP on the Tailscale address). The proxy binds 127.0.0.1 or the Tailscale IP only.
The tool is never overwritten when a different `~/.local/bin/preview` is already there."""
import hashlib
from pathlib import Path
import sys

import device_sync
import managed_block
from device_report import Step
from remote import Local

HERE = Path(__file__).resolve().parent
TOOL = HERE / 'preview'
TARGET = '.local/bin/preview'
RULES_START = '<!-- claude-pool:previews start -->'
RULES_END = '<!-- claude-pool:previews end -->'
LINUX_ONLY = 'preview runs its proxy as a systemd user unit; Linux only for now'

_SHA_SCRIPT = ('cd "$HOME" || exit 1; f="$1"; if [ ! -e "$f" ]; then echo absent; '
               'elif command -v sha256sum >/dev/null 2>&1; then sha256sum "$f" | cut -d" " -f1; '
               'else shasum -a 256 "$f" | cut -d" " -f1; fi')
_INSTALL_SCRIPT = ('cd "$HOME" || exit 1; mkdir -p .local/bin && t=$(mktemp .local/bin/.preview.XXXXXX) || exit 1; '
                   'cat > "$t" && chmod 0755 "$t" && mv "$t" "$1" || { rm -f "$t"; exit 1; }')
# Removes the file only when it still is the packaged tool; $2 is its SHA-256.
_UNLINK_SCRIPT = ('cd "$HOME" || exit 1; f="$1"; [ -e "$f" ] || exit 0; '
                  'if command -v sha256sum >/dev/null 2>&1; then h=$(sha256sum "$f" | cut -d" " -f1); '
                  'else h=$(shasum -a 256 "$f" | cut -d" " -f1); fi; [ "$h" = "$2" ] && rm -f "$f"; exit 0')


def _text(data): return (data or b'').decode('utf-8', 'replace').strip()
def _tool_sha(): return hashlib.sha256(TOOL.read_bytes()).hexdigest()


def _is_mac(remote): return _text(remote.call(['uname', '-s']).stdout) == 'Darwin'


def install(remote):
    if _is_mac(remote): return Step('preview', 'skipped', LINUX_ONLY)
    done = remote.call(['sh', '-c', _SHA_SCRIPT, 'sha', TARGET])
    have = _text(done.stdout).splitlines()[0] if done.returncode == 0 and _text(done.stdout) else None
    if have is None: return Step('preview', 'failed', 'could not inspect ~/.local/bin/preview', 'claude-pool device add ' + remote.name)
    if have == _tool_sha(): return Step('preview', 'ok', 'preview tool present')
    if have != 'absent':
        return Step('preview', 'failed', 'a different ~/.local/bin/preview is already there',
                    f'remove or rename ~/.local/bin/preview on {remote.name}, then rerun claude-pool device add {remote.name}')
    done = remote.call(['sh', '-c', _INSTALL_SCRIPT, 'install', TARGET], stdin=TOOL.read_bytes())
    if done.returncode: return Step('preview', 'failed', 'could not install the preview tool', 'claude-pool device add ' + remote.name)
    return Step('preview', 'ok', 'preview tool installed')


def _backend(output):
    """'https', 'http' or None, read from the `backend` line of `preview doctor`."""
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] == 'backend': return parts[1]
    return None


def check(remote, user):
    done = remote.call([TARGET, 'doctor'], timeout=60)
    if done.returncode in (126, 127): return Step('preview', 'missing', 'preview is not installed', 'claude-pool device add ' + remote.name)
    backend = _backend(_text(done.stdout))
    if done.returncode == 0 and backend == 'https': return Step('preview', 'ok', 'HTTPS links through Tailscale Serve')
    if done.returncode == 0 and backend == 'http':
        return Step('preview', 'manual', 'http-only', f'sudo tailscale set --operator={user}')
    return Step('preview', 'failed', 'preview doctor gave no usable answer', f'ssh {remote.name} preview doctor')


def rules_block():
    return '\n'.join([
        '## Previews',
        '',
        'After you start or change a web frontend, run `preview <port>` and send the user the link it prints.',
        'To share a file or folder, run `preview file <path>` and send the link.',
        'Stop previews with `preview stop <port>` or `preview stop --all`.',
    ])


def rules_path(): return Path.home() / '.claude' / 'CLAUDE.md'


def rules_verb(action, path=None):
    """Target-side `device preview-rules add|remove|check`. Exit code: 0 done or present, 1 absent or refused, 2 bad action."""
    path = Path(path) if path else rules_path()
    try:
        if action == 'check': return 0 if managed_block.read(path, RULES_START, RULES_END) is not None else 1
        if action == 'add':
            path.parent.mkdir(parents=True, exist_ok=True)
            managed_block.write(path, RULES_START, RULES_END, rules_block())
            return 0
        if action == 'remove':
            managed_block.write(path, RULES_START, RULES_END, None)
            return 0
    except (ValueError, OSError) as error:
        print(f'preview rules: {error}', file=sys.stderr)
        return 1
    print('usage: device preview-rules add|remove|check', file=sys.stderr)
    return 2


def remove(remote):
    if _is_mac(remote): return Step('preview', 'skipped', LINUX_ONLY)
    remote.call([TARGET, 'stop', '--all'], timeout=60)
    rules = remote.pool('device', 'preview-rules', 'remove')
    unlink = remote.call(['sh', '-c', _UNLINK_SCRIPT, 'unlink', TARGET, _tool_sha()])
    if rules.returncode or unlink.returncode:
        return Step('preview', 'failed', 'could not remove everything', 'claude-pool device remove ' + remote.name)
    return Step('preview', 'ok', 'previews stopped, rules block removed')


def _status():
    path = Path.home() / TARGET
    value = {'installed': path.is_file(), 'https': None}
    if value['installed']:
        try:
            done = Local().call([str(path), 'doctor'], timeout=15)
            backend = _backend(_text(done.stdout)) if done.returncode == 0 else None
            if backend: value['https'] = backend == 'https'
        except Exception: pass
    return {'preview': value}


device_sync.STATUS_PARTS.append(_status)
