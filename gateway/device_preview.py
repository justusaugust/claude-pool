"""Preview links per device: the standalone `preview` tool, packaged unchanged, installed on Linux devices.

`preview <port>` runs a small proxy as a `systemd --user` unit and prints a Tailscale link (HTTPS through Tailscale Serve
when the user may use it, otherwise HTTP on the Tailscale address). The proxy binds 127.0.0.1 or the Tailscale IP only.
The tool is never overwritten when a different `~/.local/bin/preview` is already there; only a copy whose SHA-256 is in
KNOWN_PREVIOUS (an earlier release of this same tool) is updated in place."""
import hashlib
from pathlib import Path
import sys

import device_sync
import managed_block
from device_report import Step
from remote import Local, remote_command

HERE = Path(__file__).resolve().parent
TOOL = HERE / 'preview'
TARGET = '.local/bin/preview'
RULES_START = '<!-- claude-pool:previews start -->'
RULES_END = '<!-- claude-pool:previews end -->'
LINUX_ONLY = 'preview runs its proxy as a systemd user unit; Linux only for now'
# Earlier releases of gateway/preview that install() may replace: the copy live on personal-vps, the first release with
# file shares, the release before the Host header check, the release before `preview cua`, the first `preview cua` (viewer
# key as a query parameter), then `preview cua` on `cua viewer` with its cookie (both installed on personal-vps), then the
# one-link `preview cua` whose units were transient (systemd-run), so a reboot dropped every preview.
KNOWN_PREVIOUS = {'b3e645e7ba9750057b68c12f7dd518d19ff792a98062eeea857115b194358d94',
                  '89c880b1411939c7996239273e28aa51399a1dca1cd56e387754e82e235faabe',
                  '7894d83a893ab63cc07ec07244b910ae8fce1949b080ba355a3b08f6e3903efd',
                  'fc7f0f3162eadf48ca4f1b942aef8b25f6446fba8fdc1e64bafc959c718a5a11',
                  'ce9f107b5620bea133df4972781e0630bc880b2b2d933f6761e3d84f855bd90b',
                  '27adcbcf4e189571c911730040e606e5a961a81debe3080d62bf9d8551b489aa',
                  'c4aaac57cf0bb23e4a64d91e2435d638d87bab1dfb66f14e2102a51a079198f5'}
# Run on the device when `preview doctor` says previews would not come back after a reboot (made by an older release).
RESTORE = 'preview restore'

_HASH = ('if command -v sha256sum >/dev/null 2>&1; then h=$(sha256sum "$f" | cut -d" " -f1); '
         'else h=$(shasum -a 256 "$f" | cut -d" " -f1); fi; ')
# Prints `absent`, `broken` (a symlink to nothing), `notfile` (a directory or other non-file), `link <sha>` (a symlink
# to a file) or `<sha>` (a regular file).
_SHA_SCRIPT = ('cd "$HOME" || exit 1; f="$1"; if [ -L "$f" ] && [ ! -e "$f" ]; then echo broken; '
               'elif [ ! -e "$f" ]; then echo absent; elif [ ! -f "$f" ]; then echo notfile; else ' + _HASH +
               'if [ -L "$f" ]; then echo "link $h"; else echo "$h"; fi; fi')
# Fresh install: a hard link from a temp file, which fails (and replaces nothing) if anything appeared meanwhile.
# -T (GNU; devices are Linux) never treats an existing directory as the place to put the link.
_INSTALL_SCRIPT = ('cd "$HOME" || exit 1; mkdir -p .local/bin && t=$(mktemp .local/bin/.preview.XXXXXX) || exit 1; '
                   'if cat > "$t" && chmod 0755 "$t" && ln -T "$t" "$1"; then rm -f "$t"; exit 0; fi; rm -f "$t"; exit 1')
# Update: replaces $1 only if it is a regular file (not a symlink) whose SHA-256 is one of $2..., checked in this shell.
_UPDATE_SCRIPT = ('cd "$HOME" || exit 1; f="$1"; shift; t=$(mktemp .local/bin/.preview.XXXXXX) || exit 1; '
                  'cat > "$t" && chmod 0755 "$t" || { rm -f "$t"; exit 1; }; '
                  'if [ -L "$f" ] || [ ! -f "$f" ]; then rm -f "$t"; exit 3; fi; ' + _HASH +
                  'ok=; for k in "$@"; do [ "$h" = "$k" ] && ok=1; done; '
                  '[ -n "$ok" ] || { rm -f "$t"; exit 3; }; mv -f -T "$t" "$f" || { rm -f "$t"; exit 1; }')
# Removes the file only when it still is the packaged tool or a known earlier copy; $2... are their SHA-256s.
_UNLINK_SCRIPT = ('cd "$HOME" || exit 1; f="$1"; shift; [ -e "$f" ] || exit 0; ' + _HASH +
                  'for k in "$@"; do [ "$h" = "$k" ] && { rm -f "$f"; exit 0; }; done; exit 0')
# Runs the installed tool from the home folder; the tool's arguments follow.
_RUN_SCRIPT = 'cd "$HOME" && exec .local/bin/preview "$@"'


def _text(data): return (data or b'').decode('utf-8', 'replace').strip()
def _tool_sha(): return hashlib.sha256(TOOL.read_bytes()).hexdigest()
def _accepted(): return [_tool_sha(), *sorted(KNOWN_PREVIOUS)]
def _run_tool(remote, *args, timeout=60): return remote.call(['sh', '-c', _RUN_SCRIPT, 'preview', *args], timeout=timeout)


def _inspect(remote):
    """`absent`, `broken`, `link <sha>`, `<sha>`, or None when the device could not be asked."""
    done = remote.call(['sh', '-c', _SHA_SCRIPT, 'sha', TARGET])
    out = _text(done.stdout)
    return out.splitlines()[0] if done.returncode == 0 and out else None


def _ours(have):
    """True when the installed file is the packaged tool or a known earlier copy (a regular file or a symlink to one)."""
    return have is not None and have.split()[-1] in {_tool_sha(), *KNOWN_PREVIOUS} and have not in ('absent', 'broken')


def _is_mac(remote): return _text(remote.call(['uname', '-s']).stdout) == 'Darwin'


def install(remote):
    if _is_mac(remote): return Step('preview', 'skipped', LINUX_ONLY)
    have = _inspect(remote)
    retry = 'claude-pool device add ' + remote.name
    rename = f'remove or rename ~/.local/bin/preview on {remote.name}, then rerun claude-pool device add {remote.name}'
    if have is None: return Step('preview', 'failed', 'could not inspect ~/.local/bin/preview', retry)
    if have in (_tool_sha(), 'link ' + _tool_sha()): return Step('preview', 'ok', 'preview tool present')
    if have == 'broken': return Step('preview', 'failed', '~/.local/bin/preview is a broken symlink', rename)
    if have == 'notfile': return Step('preview', 'failed', 'not a regular file', rename)
    if have in KNOWN_PREVIOUS:
        done = remote.call(['sh', '-c', _UPDATE_SCRIPT, 'update', TARGET, *_accepted()], stdin=TOOL.read_bytes())
        if done.returncode == 3: return Step('preview', 'failed', '~/.local/bin/preview changed while updating it', rename)
        if done.returncode: return Step('preview', 'failed', 'could not update the preview tool', retry)
        return Step('preview', 'ok', 'updated')
    if have != 'absent':
        return Step('preview', 'failed', 'a different ~/.local/bin/preview is already there', rename)
    done = remote.call(['sh', '-c', _INSTALL_SCRIPT, 'install', TARGET], stdin=TOOL.read_bytes())
    if done.returncode: return Step('preview', 'failed', 'could not install the preview tool', retry)
    return Step('preview', 'ok', 'preview tool installed')


def _backend(output):
    """'https', 'http' or None, read from the `backend` line of `preview doctor`."""
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] == 'backend': return parts[1]
    return None


def _persistent(output):
    """'yes', 'no' or None (an older tool), read from the `persistent` line of `preview doctor`."""
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] == 'persistent': return parts[1].rstrip(':')
    return None


def check(remote, user):
    done = _run_tool(remote, 'doctor')
    if done.returncode in (126, 127): return Step('preview', 'missing', 'preview is not installed', 'claude-pool device add ' + remote.name)
    if done.returncode == 0 and _persistent(_text(done.stdout)) == 'no':
        return Step('preview', 'missing', 'some previews would not come back after a reboot (transient units)',
                    remote_command(remote.name, RESTORE))
    backend = _backend(_text(done.stdout))
    if done.returncode == 0 and backend == 'https': return Step('preview', 'ok', 'HTTPS links through Tailscale Serve')
    if done.returncode == 0 and backend == 'http':
        return Step('preview', 'manual', 'http-only', f'sudo tailscale set --operator={user}')
    return Step('preview', 'failed', 'preview doctor gave no usable answer', remote_command(remote.name, 'preview doctor'))


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
    have = _inspect(remote)
    if _ours(have):
        _run_tool(remote, 'stop', '--all')  # stops only previews and file shares this tool created
        stopped = 'previews stopped'
    elif have in ('absent', 'broken'):
        stopped = 'no preview tool installed'
    else:
        stopped = 'previews not stopped: ~/.local/bin/preview is not this tool, left alone'
    rules = remote.device('preview-rules', 'remove')
    unlink = remote.call(['sh', '-c', _UNLINK_SCRIPT, 'unlink', TARGET, *_accepted()])
    if have is None or rules.returncode or unlink.returncode:
        return Step('preview', 'failed', 'could not remove everything', 'claude-pool device remove ' + remote.name)
    return Step('preview', 'ok', stopped + ', rules block removed')


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
