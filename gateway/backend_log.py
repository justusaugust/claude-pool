"""The backend's own log: what it writes to stderr (its warnings: a refresh the provider rejects, a 401 after a refresh, an
auth cooldown) goes into one private file next to the other pool state. The backend never logs a token, a request or a
response body at this level. The file is bounded: past LIMIT bytes its newest half moves to `<name>.1` and the file starts
empty; the backend keeps its open handle, which appends."""
import os
from pathlib import Path
import subprocess

LIMIT = 4 * 1024 * 1024


def open_log(path):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try: os.fchmod(fd, 0o600)
    except OSError: os.close(fd); raise
    return fd


def spawn(command, env, path, popen=subprocess.Popen):
    """Start the backend with stderr in the log file; without a writable file it runs as before, with no log."""
    try: fd = open_log(path)
    except OSError: return popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try: return popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=fd)
    finally: os.close(fd)


def trim(path, limit=LIMIT):
    """Past `limit` bytes: keep the newest half as `<name>.1` and empty the file in place. True when it did."""
    path = Path(path)
    try:
        if path.is_symlink() or path.stat().st_size <= limit: return False
        with open(path, 'rb') as source:
            source.seek(-(limit // 2), os.SEEK_END)
            tail = source.read()
        aside = path.with_name(path.name + '.1')
        fd = os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try: os.fchmod(fd, 0o600); os.write(fd, tail)
        finally: os.close(fd)
        os.truncate(path, 0)
        return True
    except OSError: return False
