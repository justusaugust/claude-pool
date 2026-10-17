"""Owner-only files and directories: atomic 0600 writes and checked reads."""
import json
import os
from pathlib import Path
import stat
import tempfile


def atomic_write(path, data):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb' if isinstance(data, bytes) else 'w', dir=path.parent,
                                         prefix=path.name + '.', delete=False) as file:
            tmp = Path(file.name)
            file.write(data); file.flush(); os.fsync(file.fileno())
        tmp.replace(path)
    finally:
        if tmp: tmp.unlink(missing_ok=True)


def private_directory(path):
    if path.is_symlink(): raise RuntimeError('Private directory cannot be a symlink: ' + str(path))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Private directory must be owned by you with mode 0700: ' + str(path))


def read_private(path, limit=131072):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
            raise RuntimeError('Credential must be your private 0600 file')
        value = json.load(file)
    if not isinstance(value, dict): raise RuntimeError('Invalid credential format')
    return value


def write_private(path, data):
    private_directory(path.parent)
    atomic_write(path, data)
