"""Which release directory each long-running pool process started from, and which of them now run stale code.
`update` restarts nothing, so a process keeps its release until the owner restarts it; this module only reports."""
import json
import os
from pathlib import Path

import service
import settings

POOL = 'pool daemon'
CONDUCTOR = 'Conductor service'
RESTART = {POOL: 'claude-pool --stop && claude-pool --start', CONDUCTOR: 'claude-pool device conductor install --restart'}


def _version(folder):
    try: return json.loads((Path(folder) / 'build-info.json').read_text()).get('sourceVersion')
    except (OSError, ValueError, AttributeError): return None


def running():
    """The release this very process was loaded from (None for a release built before `sourceVersion`)."""
    return _version(settings.HERE.parent)


def current():
    """The release the stable `current` pointer names now."""
    root = service.install_root(settings.HERE.parent)
    return _version(root / 'current' if (root / 'current').is_symlink() else root)


def _alive(pid):
    try: os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError): return False
    return True


def _record(path):
    try: value = json.loads(Path(path).read_text())
    except (OSError, ValueError): return {}
    return value if isinstance(value, dict) else {}


def stale(runtime=None, wanted=None, alive=None):
    """[(unit name, running release, current release, restart command)] for each live unit that runs another release."""
    runtime = Path(runtime or settings.RUNTIME)
    wanted, alive = wanted or current(), alive or _alive
    if not wanted: return []
    owner, conductor = _record(runtime / 'owner.json'), _record(runtime / 'conductor.json')
    seen = [(POOL, owner.get('release'), owner.get('daemon')), (CONDUCTOR, conductor.get('version'), conductor.get('servicePid'))]
    return [(name, version, wanted, RESTART[name]) for name, version, pid in seen if version and version != wanted and alive(pid)]


def describe(name, version, wanted):
    return f'{name} runs version {version[:12]}, the current one is {wanted[:12]}'


def line(unit):
    name, version, wanted, restart = unit
    return describe(name, version, wanted) + f'; restart needed: {restart}'
