#!/usr/bin/env python3
"""Test the generated Mods with Native; no live pool, credentials or model calls."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-claude', default=shutil.which('claude-native') or shutil.which('claude'))
    args = parser.parse_args()
    if not args.native_claude: parser.error('Install Native Claude or pass --native-claude /path/to/claude')
    with tempfile.TemporaryDirectory(prefix="claude-pool-mods ' ") as folder:
        home = Path(folder)
        config = home / 'config.json'
        config.write_text(json.dumps({'state_dir': str(home / 'state'), 'credential_dir': str(home / 'credentials')}))
        env = dict(os.environ, CLAUDE_POOL_CONFIG=str(config), PYTHONDONTWRITEBYTECODE='1')
        code = ('import json, launch; print(json.dumps([str(launch.configured_plugin(n)) for n in '
                '["cockpit", "session-control", "phone-hub", "respawn-continuation"]] + [str(launch.background_plugin(None))]))')
        for phone in [False, True]:
            if phone:
                (home / 'state/phone-config.json').write_text(json.dumps({'host': 'phone.invalid'}))
            generated = subprocess.run([sys.executable, '-c', code], cwd=ROOT / 'gateway', env=env,
                                       text=True, capture_output=True, check=True)
            for plugin in json.loads(generated.stdout):
                if Path(plugin).parent.name == 'background-plugins':
                    shutil.copyfile(ROOT / 'gateway/background-bootstrap/hooks/register.test.ts', Path(plugin) / 'hooks/register.test.ts')
                subprocess.run([args.native_claude, 'plugin', 'test', plugin], env=env, check=True)


if __name__ == '__main__': main()
