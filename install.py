#!/usr/bin/env python3
"""Install a source-built macOS alpha without replacing Claude or its login."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
MARKER = '# managed by claude-pool installer\n'


def atomic_write(path, content, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(content.encode()); file.flush(); os.fsync(file.fileno())
            temporary.chmod(mode)
            temporary.replace(path)
        finally: temporary.unlink(missing_ok=True)


def copy_release(target):
    def ignore(directory, names):
        return [n for n in names if n == '__pycache__' or n.endswith('.pyc') or (Path(directory).name == '.claude-plugin' and n == 'types')]
    for name in ['gateway', 'README.md', 'LICENSE', 'docs', 'backend', 'scripts', 'install.py', 'run.py']:
        src = SOURCE / name
        shutil.copytree(src, target / name, ignore=ignore) if src.is_dir() else shutil.copyfile(src, target / name)


def build_info(target):
    digest = hashlib.sha256()
    for path in sorted(target.rglob('*')):
        if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts and path.name not in {'build-info.json', 'installation.json'} and path.relative_to(target).parts[0] != 'versions':
            digest.update(path.relative_to(target).as_posix().encode() + b'\0' + path.read_bytes())
    binary = target / 'gateway/bin/shunt'
    return {'version': digest.hexdigest()[:20], 'backendPin': json.loads((target / 'backend/upstream.json').read_text()),
            'backendSha256': hashlib.sha256(binary.read_bytes()).hexdigest() if binary.is_file() else None}


def check_native(native):
    if not native.is_file() or not os.access(native, os.X_OK): raise RuntimeError('Set --native-claude to the installed Native Claude executable')
    if native.name == 'claude-pool' or native.resolve().name == 'claude-pool': raise RuntimeError('Native executable cannot be claude-pool')
    try: version = subprocess.run([str(native), '--version'], capture_output=True, text=True, timeout=10, check=True).stdout
    except (subprocess.SubprocessError, OSError): raise RuntimeError('Could not verify Native Claude version; nothing installed') from None
    parsed = re.match(r'^(\d+)\.(\d+)\.(\d+)\b', version.strip())
    if not parsed or tuple(map(int, parsed.groups())) < (2, 1, 300): raise RuntimeError('This alpha requires Native Claude Code 2.1.300 or newer')


def check_release(target, config):
    subprocess.run([sys.executable, '-m', 'compileall', '-q', str(target)], check=True)
    env = dict(os.environ, CLAUDE_POOL_CONFIG=str(config), PYTHONDONTWRITEBYTECODE='1')
    subprocess.run([sys.executable, str(target / 'gateway/launch.py'), 'help'], env=env, check=True, capture_output=True)


def service_path(receipt):
    label = receipt.get('serviceLabel', 'io.github.claude-pool')
    if not isinstance(label, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+', label): raise RuntimeError('Invalid installed service label')
    return label, Path.home() / ('Library/LaunchAgents/' + label + '.plist')


def install(prefix, bin_dir, config, native, *, skip_backend=False, update=False):
    if sys.platform != 'darwin': raise RuntimeError('This alpha supports macOS only')
    if sys.version_info < (3, 11): raise RuntimeError('Python 3.11 or newer is required')
    if prefix.is_symlink(): raise RuntimeError('Application root cannot be a symlink')
    prefix = prefix.resolve()
    if prefix.exists():
        if not update: raise RuntimeError('Installation already exists; run claude-pool update')
        return update_install(prefix, skip_backend=skip_backend)
    if config.is_relative_to(prefix): raise RuntimeError('Keep configuration outside the application directory so uninstall preserves it')
    for name in ['claude-pool', 'claude-native']:
        if (bin_dir / name).exists() or (bin_dir / name).is_symlink(): raise RuntimeError('Refusing to replace existing launcher: ' + name)
    check_native(native)
    binary = SOURCE / 'gateway/bin/shunt'
    if not skip_backend and (not binary.is_file() or not os.access(binary, os.X_OK)): raise RuntimeError('Backend is missing or not executable. Run python3 scripts/build_backend.py')
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
        'claude-pool': '#!/bin/sh\n' + MARKER + 'export CLAUDE_POOL_CONFIG=' + shlex.quote(str(config)) + '\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(prefix / 'run.py')) + ' "$@"\n',
        'claude-native': '#!/bin/sh\n' + MARKER + 'exec ' + shlex.quote(str(native.absolute())) + ' "$@"\n'}
    with tempfile.TemporaryDirectory(prefix='.claude-pool-install-', dir=prefix.parent) as staging:
        target = Path(staging) / 'app'
        target.mkdir()
        copy_release(target)
        written = {str(bin_dir / name): hashlib.sha256(body.encode()).hexdigest() for name, body in bodies.items()}
        (target / 'installation.json').write_text(json.dumps({'launchers': written, 'config': str(config), 'root': str(prefix), 'native': str(native.absolute())}, indent=2) + '\n')
        (target / 'build-info.json').write_text(json.dumps(build_info(target)) + '\n')
        check_release(target, config)
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


def update_install(prefix, *, skip_backend=False):
    receipt = json.loads((prefix / 'installation.json').read_text())
    if 'root' not in receipt and (prefix / 'run.py').exists(): raise RuntimeError('Existing run.py is not installer-owned; leaving it intact')
    if (prefix / 'current').exists() and not (prefix / 'current').is_symlink(): raise RuntimeError('Invalid release pointer')
    for raw, expected in receipt['launchers'].items():
        path = Path(raw)
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Launcher changed; leaving everything intact: ' + str(path))
    options = json.loads(Path(receipt['config']).read_text())
    check_native(Path(options.get('native_claude', receipt.get('native', ''))))
    current = (prefix / 'current').resolve() if (prefix / 'current').is_symlink() else prefix
    if current != prefix and current.parent != prefix / 'versions': raise RuntimeError('Invalid release pointer')
    label, service = service_path(receipt)
    legacy_entry = receipt.get('legacyEntry', str(prefix / 'gateway/launch.py'))
    future_service = None
    if service.is_symlink(): raise RuntimeError('Service file changed; leaving it intact')
    if service.exists():
        definition = plistlib.loads(service.read_bytes())
        arguments = definition.get('ProgramArguments', [])
        if definition.get('Label') != label or not any(p in arguments for p in [legacy_entry, str(prefix / 'run.py')]):
            raise RuntimeError('Service file belongs to another installation; leaving it intact')
        if legacy_entry in arguments:
            definition['ProgramArguments'] = [str(prefix / 'run.py') if a == legacy_entry else a for a in arguments]
            definition['WorkingDirectory'] = str(prefix)
            definition.setdefault('EnvironmentVariables', {})['CLAUDE_POOL_CONFIG'] = receipt['config']
            future_service = plistlib.dumps(definition).decode()
    versions = prefix / 'versions'
    if versions.is_symlink(): raise RuntimeError('Release directory cannot be a symlink')
    versions.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.staging-', dir=versions) as folder:
        target = Path(folder) / 'app'; target.mkdir()
        copy_release(target)
        if not skip_backend and not os.access(target / 'gateway/bin/shunt', os.X_OK): raise RuntimeError('Backend build is missing or not executable; nothing activated')
        (target / 'installation.json').write_text(json.dumps(receipt | {'root': str(prefix)}) + '\n')
        info = build_info(target)
        (target / 'build-info.json').write_text(json.dumps(info) + '\n')
        # Check the copied files before changing the pointer or any launcher.
        check_release(target, receipt['config'])
        if (current / 'build-info.json').is_file() and json.loads((current / 'build-info.json').read_text())['version'] == info['version'] and build_info(current)['version'] == info['version'] and (skip_backend or os.access(current / 'gateway/bin/shunt', os.X_OK)):
            if future_service:
                atomic_write(service, future_service)
                print('Future service entry prepared; the loaded service was left unchanged.')
            print('Claude Pool is already up to date.'); return
        destination = versions / info['version']
        if destination.exists():
            if build_info(destination)['version'] != info['version'] or (not skip_backend and not os.access(destination / 'gateway/bin/shunt', os.X_OK)): raise RuntimeError('Retained release changed; refusing to reuse it')
        else: target.rename(destination)
        if not (prefix / 'run.py').exists():
            # Legacy source installs retain every old module while adopting the stable launcher.
            atomic_write(prefix / 'run.py', (SOURCE / 'run.py').read_text(), 0o755)
            pool = next(Path(p) for p in receipt['launchers'] if Path(p).name == 'claude-pool')
            body = '#!/bin/sh\n' + MARKER + 'export CLAUDE_POOL_CONFIG=' + shlex.quote(receipt['config']) + '\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(prefix / 'run.py')) + ' "$@"\n'
            atomic_write(pool, body, 0o755)
            receipt['launchers'][str(pool)] = hashlib.sha256(body.encode()).hexdigest()
            receipt['root'] = str(prefix)
            atomic_write(prefix / 'installation.json', json.dumps(receipt) + '\n')
        if future_service: atomic_write(service, future_service)
        temporary = Path(folder) / 'current'; temporary.symlink_to(destination)
        temporary.replace(prefix / 'current')
    print('Updated Claude Pool. New sessions use the new CLI and Mods.')
    print('Running tasks and services were not restarted. Backend changes load on the next service start.')


def prepare_backend(prefix):
    pin = json.loads((SOURCE / 'backend/upstream.json').read_text())
    if hashlib.sha256((SOURCE / 'backend/shunt.patch').read_bytes()).hexdigest() != pin['patchSha256']:
        raise RuntimeError('Backend patch digest changed; nothing activated')
    binary = SOURCE / 'gateway/bin/shunt'
    if binary.is_file(): return
    current = (prefix / 'current').resolve() if (prefix / 'current').is_symlink() else prefix
    info_path = current / 'build-info.json'
    if info_path.is_file():
        info = json.loads(info_path.read_text()); old = current / 'gateway/bin/shunt'
        if pin == info['backendPin'] and old.is_file() and os.access(old, os.X_OK) and hashlib.sha256(old.read_bytes()).hexdigest() == info['backendSha256']:
            binary.parent.mkdir(exist_ok=True)
            shutil.copy2(old, binary)
            print('Reusing the verified backend; no Rust rebuild needed.'); return
    missing = [name for name in ['node', 'npm', 'rustup', 'cargo'] if not shutil.which(name)]
    if missing:
        if not shutil.which('brew'): raise RuntimeError('Install Homebrew or the build tools (' + ', '.join(missing) + '), then rerun. Nothing activated.')
        packages = (['node'] if 'node' in missing or 'npm' in missing else []) + (['rustup'] if 'rustup' in missing or 'cargo' in missing else [])
        subprocess.run(['brew', 'install', *packages], check=True)
        rustup_bin = Path(subprocess.check_output(['brew', '--prefix', 'rustup'], text=True).strip()) / 'bin' if 'rustup' in packages else Path.home() / '.cargo/bin'
        os.environ['PATH'] = str(rustup_bin) + os.pathsep + str(Path.home() / '.cargo/bin') + os.pathsep + os.environ.get('PATH', '')
    subprocess.run(['rustup', 'toolchain', 'install', pin['rustToolchain'], '--profile', 'minimal'], check=True)
    subprocess.run(['nice', '-n', '10', sys.executable, str(SOURCE / 'scripts/build_backend.py')], check=True)


def uninstall(prefix):
    receipt = json.loads((prefix / 'installation.json').read_text())
    label, plist = service_path(receipt)
    job = subprocess.run(['launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + label], capture_output=True)
    if job.returncode == 0: raise RuntimeError('Pool service is loaded. Finish tasks, run claude-pool --stop, then launchctl bootout gui/$(id -u)/' + label + ' before uninstalling')
    for raw, expected in receipt['launchers'].items():
        path = Path(raw)
        if path.is_symlink() or (path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != expected): raise RuntimeError('Launcher changed; leaving everything intact: ' + str(path))
    if plist.is_symlink(): raise RuntimeError('Service file changed; leaving everything intact')
    if plist.exists():
        definition = plistlib.loads(plist.read_bytes())
        if definition.get('Label') != label or not any(entry in definition.get('ProgramArguments', []) for entry in [receipt.get('legacyEntry', str(prefix / 'gateway/launch.py')), str(prefix / 'run.py')]):
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
    parser.add_argument('--bootstrap', action='store_true', help='Build if needed; install or update without restarting tasks')
    args = parser.parse_args()
    if args.uninstall: uninstall(args.prefix.expanduser().resolve())
    else:
        prefix = args.prefix.expanduser().absolute()
        prefix.parent.mkdir(parents=True, exist_ok=True)
        with (prefix.parent / ('.' + prefix.name + '.install.lock')).open('a') as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('Another installation/update is already running') from None
            if args.bootstrap:
                if sys.platform != 'darwin': raise RuntimeError('This alpha supports macOS only')
                if prefix.is_symlink(): raise RuntimeError('Application root cannot be a symlink')
                if prefix.exists() and not (prefix / 'installation.json').is_file(): raise RuntimeError('Existing directory is not a managed installation; nothing changed')
                if not prefix.exists():
                    for name in ['claude-pool', 'claude-native']:
                        path = args.bin_dir.expanduser() / name
                        if path.exists() or path.is_symlink(): raise RuntimeError('Refusing to replace existing launcher: ' + name)
                    check_native(args.native_claude.expanduser().absolute())
                prepare_backend(prefix)
            install(prefix, args.bin_dir.expanduser().resolve(), args.config.expanduser().resolve(), args.native_claude.expanduser().absolute(), update=args.bootstrap)


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error: raise SystemExit(str(error))
