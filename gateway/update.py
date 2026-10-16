"""Fetch public source; install beside the version used by active tasks."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

REPOSITORY = 'https://github.com/justusaugust/claude-pool.git'


def installed_root(here):
    receipt = json.loads((Path(here).parent / 'installation.json').read_text())
    root = Path(receipt.get('root', Path(here).parent))
    if not (root / 'installation.json').is_file(): raise RuntimeError('Installation receipt is missing')
    return root


def cli(argv, here):
    if argv: raise RuntimeError('Usage: claude-pool update')
    try: root = installed_root(here)
    except (OSError, ValueError):
        raise RuntimeError('This is a source checkout, not a managed installation. Run the one-command installer from docs/SETUP.md.') from None
    with tempfile.TemporaryDirectory(prefix='claude-pool-update-') as folder:
        source = Path(folder) / 'source'
        print('Downloading Claude Pool…', flush=True)
        try: subprocess.run(['git', 'clone', '--quiet', '--depth', '1', REPOSITORY, str(source)], check=True)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError('Download failed; the installed version and running tasks are unchanged. Rerun claude-pool update.') from None
        try: subprocess.run([sys.executable, str(source / 'install.py'), '--bootstrap', '--prefix', str(root)], check=True)
        except subprocess.SubprocessError:
            raise RuntimeError('Update did not finish. Running tasks were not restarted; rerun claude-pool update to retry.') from None
    return 0
