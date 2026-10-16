#!/usr/bin/env python3
"""Build the exact upstream revision plus the reviewed local patch."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    upstream = json.loads((ROOT / 'backend/upstream.json').read_text())
    patch = ROOT / 'backend/shunt.patch'
    if hashlib.sha256(patch.read_bytes()).hexdigest() != upstream['patchSha256']: raise RuntimeError('Backend patch digest changed; review it before building')
    output = ROOT / 'gateway/bin/shunt'
    if output.exists(): raise RuntimeError('Backend already exists; retain it or move it aside before building')
    with tempfile.TemporaryDirectory(prefix='claude-pool-build-') as folder:
        path = Path(folder)
        def run(*args, cwd=path): subprocess.run(args, cwd=cwd, check=True)
        run('git', 'init', '--quiet')
        run('git', 'remote', 'add', 'origin', upstream['repository'])
        run('git', 'fetch', '--depth', '1', 'origin', upstream['revision'])
        run('git', 'checkout', '--detach', 'FETCH_HEAD')
        run('git', 'apply', '--check', str(patch))
        run('git', 'apply', str(patch))
        run('npm', 'ci', cwd=path / 'ui')
        run('npm', 'run', 'build', cwd=path / 'ui')
        target = Path(os.environ.get('CARGO_TARGET_DIR', path / 'target')).expanduser().resolve()
        env = dict(os.environ, CARGO_BUILD_JOBS='2', CARGO_TARGET_DIR=str(target))
        def clean_backend():
            subprocess.run(['cargo', '+' + upstream['rustToolchain'], 'clean', '--release', '--locked', '--package', 'shunt-gateway'], cwd=path, env=env, check=True)
            if (target / 'release/shunt').exists(): raise RuntimeError('Backend survived Cargo cleanup; refusing shared build cache')
        # A shared target retains dependencies, never an application build.
        if os.environ.get('CARGO_TARGET_DIR'): clean_backend()
        subprocess.run(['cargo', '+' + upstream['rustToolchain'], 'build', '--release', '--locked', '--features', 'ui', '-j', '2'], cwd=path, env=env, check=True)
        output.parent.mkdir(exist_ok=True)
        shutil.copyfile(target / 'release/shunt', output)
        output.chmod(0o755)
        if os.environ.get('CARGO_TARGET_DIR'): clean_backend()
    print('Built pinned Shunt backend: ' + str(output))


if __name__ == '__main__': main()
