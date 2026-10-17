#!/usr/bin/env python3
"""Export source only; runtime state, binaries and generated SDK stay local."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def source_files(root):
    names = {'README.md', 'LICENSE', '.gitignore', '.gitattributes', 'install.py', 'run.py', 'install.sh'}
    for folder in ['gateway', 'backend', 'docs', 'scripts', 'tests']:
        for path in (root / folder).rglob('*'):
            if not path.is_file() or path.is_symlink(): continue
            name = path.relative_to(root).as_posix()
            if '__pycache__' in path.parts or '/.claude-plugin/types/' in name or name.startswith('gateway/bin/'): continue
            if name == 'gateway/preview' or path.suffix in {'.py', '.ts', '.tsx', '.js', '.css', '.html', '.json', '.md', '.patch'} or path.name.startswith('LICENSE-'):
                names.add(name)
    return sorted(names)


def package(root, output):
    files = {name: (root / name).read_bytes() for name in source_files(root)}
    files['SOURCE-MANIFEST.json'] = (json.dumps({name: hashlib.sha256(data).hexdigest() for name, data in files.items()}, indent=2) + '\n').encode()
    with output.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w') as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo('claude-pool/' + name)
            info.size = len(data); info.mode = 0o755 if name == 'gateway/preview' else 0o644
            archive.addfile(info, io.BytesIO(data))
    print(str(output) + ' · ' + str(len(files)) + ' files · source only')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT.parent / 'claude-pool-source.tar.gz')
    package(ROOT, parser.parse_args().output)
