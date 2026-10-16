#!/usr/bin/env python3
"""Stable entry point; each process keeps one physical release directory."""
import json
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parent
    receipt = json.loads((root / 'installation.json').read_text())
    if (root / 'current').exists() and not (root / 'current').is_symlink():
        raise RuntimeError('Installed release pointer is invalid')
    current = (root / 'current').resolve() if (root / 'current').is_symlink() else root
    if current != root and (current.parent != root / 'versions' or not current.is_dir()):
        raise RuntimeError('Installed release pointer is invalid')
    target = current / 'gateway/launch.py'
    if not target.is_file(): raise RuntimeError('Installed release is incomplete')
    os.environ['CLAUDE_POOL_CONFIG'] = receipt['config']
    os.execv(sys.executable, [sys.executable, str(target), *sys.argv[1:]])


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, RuntimeError) as error: raise SystemExit(str(error))
