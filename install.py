#!/usr/bin/env python3
"""Install a source-built macOS alpha without replacing Claude or its login."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
MARKER = '# managed by claude-pool installer\n'


def install(prefix, bin_dir, config, native, *, skip_backend=False):
    if sys.platform != 'darwin': raise RuntimeError('This alpha supports macOS only')
    if sys.version_info < (3, 11): raise RuntimeError('Python 3.11 or newer is required')
    if prefix.exists() or prefix.is_symlink(): raise RuntimeError('Installation already exists; keep it until active tasks finish, then uninstall before updating')
    if config.is_relative_to(prefix): raise RuntimeError('Keep configuration outside the application directory so uninstall preserves it')
    if not native.is_file() or not os.access(native, os.X_OK): raise RuntimeError('Set --native-claude to the installed Native Claude executable')
    if native.name == 'claude-pool' or native.resolve().name == 'claude-pool': raise RuntimeError('Native executable cannot be claude-pool')
    for name in ['claude-pool', 'claude-native']:
        if (bin_dir / name).exists() or (bin_dir / name).is_symlink(): raise RuntimeError('Refusing to replace existing launcher: ' + name)
    try: version = subprocess.run([str(native), '--version'], capture_output=True, text=True, timeout=10, check=True).stdout
    except (subprocess.SubprocessError, OSError): raise RuntimeError('Could not verify Native Claude version; nothing installed') from None
    parsed = re.match(r'^(\d+)\.(\d+)\.(\d+)\b', version.strip())
    if not parsed or tuple(map(int, parsed.groups())) < (2, 1, 300): raise RuntimeError('This alpha requires Native Claude Code 2.1.300 or newer')
    binary = SOURCE / 'gateway/bin/shunt'
    if not skip_backend and not binary.is_file(): raise RuntimeError('First run: python3 scripts/build_backend.py')
    sys.path.insert(0, str(SOURCE / 'gateway'))
    from settings import load
    load(config)
    # Configuration is non-secret, but keep paths and labels private by default.
    if config.exists():
        value = json.loads(config.read_text())
        if not isinstance(value, dict): raise RuntimeError('Existing config is invalid')
    else:
        value = {'native_claude': str(native.absolute()), 'accounts': []}
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not config.exists():
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out: out.write(json.dumps(value, indent=2) + '\n')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    bodies = {
        'claude-pool': '#!/bin/sh\n' + MARKER + 'export CLAUDE_POOL_CONFIG=' + shlex.quote(str(config)) + '\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(prefix / 'gateway/launch.py')) + ' "$@"\n',
        'claude-native': '#!/bin/sh\n' + MARKER + 'exec ' + shlex.quote(str(native.absolute())) + ' "$@"\n'}
    with tempfile.TemporaryDirectory(prefix='.claude-pool-install-', dir=prefix.parent) as staging:
        target = Path(staging) / 'app'
        def ignore(directory, names):
            return [n for n in names if n == '__pycache__' or n.endswith('.pyc') or (Path(directory).name == '.claude-plugin' and n == 'types')]
        shutil.copytree(SOURCE / 'gateway', target / 'gateway', ignore=ignore)
        for name in ['README.md', 'LICENSE', 'docs', 'backend']:
            src = SOURCE / name
            shutil.copytree(src, target / name) if src.is_dir() else shutil.copyfile(src, target / name)
        written = {str(bin_dir / name): hashlib.sha256(body.encode()).hexdigest() for name, body in bodies.items()}
        (target / 'installation.json').write_text(json.dumps({'launchers': written, 'config': str(config)}, indent=2) + '\n')
        created = []
        try:
            for name, body in bodies.items():
                path = bin_dir / name
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
                created.append((path, body.encode()))
                with os.fdopen(fd, 'w') as out: out.write(body)
            target.rename(prefix)
        except BaseException:
            for path, body in created:
                if path.is_file() and not path.is_symlink() and body.startswith(path.read_bytes()): path.unlink()
            raise
    print('Installed to ' + str(prefix))
    print('Connect an account: claude-pool account add you@example.com')
    print('Native login is separate; keep its account among the configured pool identities.')
    print('To make claude use the pool, add this shell function:')
    print('claude() { command claude-pool "$@"; }')


def uninstall(prefix):
    receipt = json.loads((prefix / 'installation.json').read_text())
    job = subprocess.run(['launchctl', 'print', 'gui/' + str(os.getuid()) + '/io.github.claude-pool'], capture_output=True)
    if job.returncode == 0: raise RuntimeError('Pool service is loaded. Finish tasks, run claude-pool --stop, then launchctl bootout gui/$(id -u)/io.github.claude-pool before uninstalling')
    for raw, expected in receipt['launchers'].items():
        path = Path(raw)
        if path.is_symlink() or (path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != expected): raise RuntimeError('Launcher changed; leaving everything intact: ' + str(path))
    plist = Path.home() / 'Library/LaunchAgents/io.github.claude-pool.plist'
    if plist.is_symlink(): raise RuntimeError('Service file changed; leaving everything intact')
    if plist.exists():
        definition = plistlib.loads(plist.read_bytes())
        if definition.get('Label') != 'io.github.claude-pool' or str(prefix / 'gateway/launch.py') not in definition.get('ProgramArguments', []):
            raise RuntimeError('Service file belongs to another installation; leaving everything intact')
    for raw in receipt['launchers']: Path(raw).unlink(missing_ok=True)
    plist.unlink(missing_ok=True)
    shutil.rmtree(prefix)
    print('Removed the application and unchanged launchers. Config, credentials, history and Native Claude were retained.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local/share/claude-pool')
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/claude-pool/config.json')
    parser.add_argument('--native-claude', type=Path, default=Path(shutil.which('claude') or Path.home() / '.local/bin/claude'))
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    if args.uninstall: uninstall(args.prefix.expanduser().resolve())
    else: install(args.prefix.expanduser().resolve(), args.bin_dir.expanduser().resolve(), args.config.expanduser().resolve(), args.native_claude.expanduser().absolute())


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, RuntimeError) as error: raise SystemExit(str(error))
