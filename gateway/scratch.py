"""Agent scratch on disk instead of a RAM-backed /tmp, and pruning of what ended sessions left behind.

Claude Code keeps scratchpads, task output and edit diffs under `$CLAUDE_CODE_TMPDIR/claude-<uid>` (else the system temp
folder). On a Linux host whose /tmp is a tmpfs those files take RAM: on 22.10.2026 they held 11 GB of 32. `agent_tmpdir`
picks a private folder in the user's cache for every pool start instead; `prune` removes what ended sessions left there
(and in the old /tmp location) once it has not changed for a few days."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

STALE = 3 * 86400       # an ended session's folder goes after three days without a change
DIFF_STALE = 86400      # edit diffs are only needed while the edit is on screen
SESSION = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def _mount_type(path, mounts):
    best, kind = '', None
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3: continue
        point = parts[1].replace('\\040', ' ')
        if (path == point or path.startswith(point.rstrip('/') + '/')) and len(point) > len(best): best, kind = point, parts[2]
    return kind


def _mounts():
    try: return Path('/proc/mounts').read_text()
    except OSError: return None


def agent_tmpdir(environ, home=None, tempdir=None, mounts=False):
    """The folder for CLAUDE_CODE_TMPDIR, or None when the system temp folder is not a tmpfs or the user chose one."""
    if environ.get('CLAUDE_CODE_TMPDIR'): return None
    mounts = _mounts() if mounts is False else mounts
    tempdir = tempdir or tempfile.gettempdir()
    if not mounts or 'tmpfs' not in {_mount_type(tempdir, mounts), _mount_type(os.path.realpath(tempdir), mounts)}: return None
    home = Path(home or Path.home())
    path = Path(environ.get('XDG_CACHE_HOME') or home / '.cache') / 'claude-pool' / 'tmp'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def live_sessions(folder=None):
    """Session ids of running Claude processes, from Claude Code's own registry (<pid>.json with sessionId)."""
    folder, ids = Path(folder or Path.home() / '.claude' / 'sessions'), set()
    for entry in folder.glob('*.json'):
        try:
            os.kill(int(entry.stem), 0)
            value = json.loads(entry.read_text())
        except (ValueError, OSError): continue
        if isinstance(value, dict) and isinstance(value.get('sessionId'), str): ids.add(value['sessionId'])
    return ids


def _newest(path):
    newest = path.lstat().st_mtime
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try: newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
            except OSError: pass
    return newest


def _size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try: total += os.lstat(os.path.join(root, name)).st_size
            except OSError: pass
    return total


def prune(roots, live, now):
    """Remove `<root>/claude-<uid>/<project>/<session-uuid>` folders of sessions not in `live` that have not changed for
    STALE seconds, and edit diffs older than DIFF_STALE. Real directories only; symlinks are never followed or removed."""
    removed = {'folders': 0, 'bytes': 0}
    for root in roots:
        base = Path(root) / ('claude-' + str(os.getuid()))
        try:
            if not stat.S_ISDIR(base.lstat().st_mode): continue
        except OSError: continue
        diffs = base / 'bash-edit-diff'
        if not diffs.is_symlink() and diffs.is_dir():
            for entry in diffs.iterdir():
                try:
                    info = entry.lstat()
                    if stat.S_ISREG(info.st_mode) and now - info.st_mtime > DIFF_STALE: entry.unlink(); removed['bytes'] += info.st_size
                except OSError: pass
        for project in base.iterdir():
            # Project folders are encoded paths ('-home-…'); bundled-skills and the like belong to running versions.
            if project.is_symlink() or not project.is_dir() or not project.name.startswith('-'): continue
            for session in project.iterdir():
                if session.is_symlink() or not session.is_dir() or not SESSION.fullmatch(session.name) or session.name in live: continue
                try:
                    if now - _newest(session) <= STALE: continue
                    size = _size(session)
                    shutil.rmtree(session)
                except OSError: continue
                removed['folders'] += 1; removed['bytes'] += size
    return removed
