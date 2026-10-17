#!/usr/bin/env python3
"""Run fixture tests with an isolated HOME, config, stores and Native stub."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-k', dest='patterns', action='append', default=[],
                        help='Only run tests matching this unittest -k pattern (repeatable)')
    patterns = [arg for pattern in parser.parse_args().patterns for arg in ('-k', pattern)]
    with tempfile.TemporaryDirectory(prefix='claude-pool-tests-') as folder:
        home = Path(folder)
        native = home / 'native-claude'
        native.write_text('#!/bin/sh\nexit 0\n'); native.chmod(0o755)
        config = home / 'config.json'
        config.write_text(json.dumps({'native_claude': str(native), 'state_dir': str(home / 'state'),
                                     'credential_dir': str(home / 'credentials')}))
        env = dict(os.environ, HOME=str(home), CLAUDE_POOL_CONFIG=str(config), PYTHONDONTWRITEBYTECODE='1')
        ran = False
        for suite in ['gateway', 'tests']:
            code = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', suite, '-p', 'test_*.py', *patterns],
                                  cwd=ROOT, env=env).returncode
            # unittest exits 5 when a -k pattern selects nothing in this suite; that is fine for one suite.
            if code == 5 and patterns: continue
            if code: sys.exit(code)
            ran = True
        if not ran: sys.exit('No tests matched ' + ' '.join(patterns[1::2]))


if __name__ == '__main__': main()
