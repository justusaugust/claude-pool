#!/usr/bin/env python3
"""Exercise the real bootstrap and updater without accounts or a running pool."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile

from package import source_files

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'https://github.com/justusaugust/claude-pool.git'


def smoke(native):
    with tempfile.TemporaryDirectory(prefix="claude-pool smoke ' ") as folder:
        root = Path(folder).resolve()
        home = root / 'empty home'
        home.mkdir()
        source = root / 'release source'
        source.mkdir()
        for name in source_files(ROOT):
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, destination)
        # Local checks can reuse a built source binary; a clean CI checkout builds it.
        binary = ROOT / 'gateway/bin/shunt'
        if binary.exists():
            (source / 'gateway/bin').mkdir()
            shutil.copy2(binary, source / 'gateway/bin/shunt')
        env = {key: os.environ[key] for key in ['PATH', 'TMPDIR', 'LANG', 'CARGO_TARGET_DIR', 'NPM_CONFIG_CACHE'] if key in os.environ}
        env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / '.claude'),
                   GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(home / '.gitconfig'),
                   GIT_TERMINAL_PROMPT='0', PYTHONDONTWRITEBYTECODE='1',
                   DISABLE_AUTOUPDATER='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                   CARGO_HOME=os.environ.get('CARGO_HOME', str(Path.home() / '.cargo')),
                   RUSTUP_HOME=os.environ.get('RUSTUP_HOME', str(Path.home() / '.rustup')))

        def run(*args, capture=False):
            result = subprocess.run([str(arg) for arg in args], cwd=root, env=env,
                                    text=True, capture_output=capture, timeout=2400)
            if capture:
                print(result.stdout, end='', flush=True)
                print(result.stderr, end='', file=sys.stderr, flush=True)
            result.check_returncode()
            return result.stdout if capture else None

        def commit(message):
            run('git', '-C', source, 'add', '.')
            run('git', '-C', source, '-c', 'user.name=Smoke Test', '-c',
                'user.email=smoke@example.invalid', 'commit', '--quiet', '-m', message)

        run('git', 'init', '--quiet', source)
        if binary.exists(): run('git', '-C', source, 'add', '-f', 'gateway/bin/shunt')
        commit('First release')
        # Only this disposable HOME redirects the production URL to the candidate.
        run('git', 'config', '--global', 'url.' + source.as_uri() + '.insteadOf', REPOSITORY)
        app = home / '.local/share/claude-pool'
        bins = home / '.local/bin'
        config = home / '.config/claude-pool/config.json'
        run('sh', ROOT / 'install.sh', '--native-claude', native)
        assert app.is_dir() and not (app / 'current').exists()
        assert json.loads(config.read_text()) == {'native_claude': str(native), 'accounts': []}
        assert config.stat().st_mode & 0o777 == 0o600
        installed_binary = app / 'gateway/bin/shunt'
        assert 'Usage: shunt' in run(installed_binary, '--help', capture=True)
        cli = bins / 'claude-pool'
        assert 'account add' in run(cli, 'help', capture=True)
        run(cli, 'account', 'list')
        assert run(bins / 'claude-native', '--version', capture=True) == run(cli, 'exec', '--version', capture=True)
        preserved = [config, bins / 'claude-pool', bins / 'claude-native',
                     app / 'installation.json', app / 'README.md', installed_binary]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in preserved}
        worker = subprocess.Popen([sys.executable, '-u', '-c',
            'import pathlib,sys; p=pathlib.Path(sys.argv[1]); before=p.read_bytes(); '
            'print("ready",flush=True); sys.stdin.readline(); '
            'assert p.read_bytes()==before; print("retained release survived")',
            str(app / 'README.md')], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            assert select.select([worker.stdout], [], [], 10)[0], 'Worker did not start'
            assert worker.stdout.readline().strip() == 'ready'
            readme = source / 'README.md'
            readme.write_text(readme.read_text() + '\nInstall/update smoke release.\n')
            if binary.exists(): (source / 'gateway/bin/shunt').unlink()
            commit('Second release')
            output = run(cli, 'update', capture=True)
            assert 'Updated Claude Pool' in output and 'Reusing the verified backend' in output
            current = (app / 'current').resolve(strict=True)
            assert current.parent == app / 'versions'
            assert current.joinpath('README.md').read_text().endswith('Install/update smoke release.\n')
            assert worker.poll() is None, 'Update stopped an existing worker'
            assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in preserved}
            assert hashlib.sha256((current / 'gateway/bin/shunt').read_bytes()).hexdigest() == before[installed_binary]
            assert str(current / 'docs/SETUP.md') in run(cli, 'help', capture=True)
            run(cli, 'account', 'list')
            assert 'already up to date' in run(cli, 'update', capture=True)
            assert (app / 'current').resolve() == current
            patch = source / 'backend/shunt.patch'
            patch.write_text(patch.read_text() + '\nUnreviewed smoke fixture\n')
            commit('Invalid release must not activate')
            try:
                run(cli, 'update', capture=True)
                raise AssertionError('An invalid backend patch was accepted')
            except subprocess.CalledProcessError as error:
                assert 'Backend patch digest changed' in error.stderr, error.stderr
            assert (app / 'current').resolve() == current
            assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in preserved}
            assert worker.poll() is None, 'Rejected update stopped an existing worker'
            stdout, stderr = worker.communicate('\n', timeout=10)
            assert worker.returncode == 0 and 'retained release survived' in stdout, stderr
            for path in ['Library/LaunchAgents', '.local/state/claude-pool', '.shunt', '.claude']:
                assert not (home / path).exists(), 'Unexpected runtime or login state: ' + path
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.communicate(timeout=10)
        print('PASS: clean bootstrap, CLI update, backend reuse, repeat update, invalid release rejection, retained worker and configuration.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-claude', type=Path, required=True)
    args = parser.parse_args()
    smoke(args.native_claude.expanduser().absolute())
