#!/usr/bin/env python3
"""Run fixture tests with an isolated HOME, config, stores and Native stub."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='claude-pool-tests-') as folder:
        home = Path(folder)
        native = home / 'native-claude'
        native.write_text('#!/bin/sh\nexit 0\n'); native.chmod(0o755)
        config = home / 'config.json'
        config.write_text(json.dumps({'native_claude': str(native), 'state_dir': str(home / 'state'),
                                     'credential_dir': str(home / 'credentials')}))
        env = dict(os.environ, HOME=str(home), CLAUDE_POOL_CONFIG=str(config), PYTHONDONTWRITEBYTECODE='1')
        for suite in ['gateway', 'tests']:
            subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', suite, '-p', 'test_*.py'],
                           cwd=ROOT, env=env, check=True)


if __name__ == '__main__': main()
