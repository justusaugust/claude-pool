"""Own one marked block in a text file; every byte outside the markers stays identical.
Works on bytes (CRLF and non-UTF-8 files survive); unbalanced or repeated markers raise ValueError and nothing is written."""
import os
from pathlib import Path
import tempfile


def _load(path):
    try: return Path(path).read_bytes()
    except FileNotFoundError: return None


def _parse(path, data, start, end):
    """(lines, first, last) of the one block, or (lines, None, None) when there is none."""
    lines = data.splitlines(keepends=True)
    key = lambda line: line.rstrip(b'\r\n')
    starts = [i for i, line in enumerate(lines) if key(line) == start.encode()]
    ends = [i for i, line in enumerate(lines) if key(line) == end.encode()]
    if not starts and not ends: return lines, None, None
    if len(starts) > 1 or len(ends) > 1: raise ValueError(f'{path}: more than one {start!r} block; fix the file by hand')
    if not starts: raise ValueError(f'{path}: {end!r} without a start marker; fix the file by hand')
    if not ends: raise ValueError(f'{path}: {start!r} without an end marker; fix the file by hand')
    if ends[0] < starts[0]: raise ValueError(f'{path}: end marker before the start marker; fix the file by hand')
    return lines, starts[0], ends[0]


def read(path, start, end):
    data = _load(path)
    if data is None: return None
    lines, first, last = _parse(path, data, start, end)
    if first is None: return None
    return '\n'.join(line.rstrip(b'\r\n').decode('utf-8', 'replace') for line in lines[first + 1:last])


def outside(path, start, end):
    """The file's bytes without the block (empty when the file is missing)."""
    data = _load(path)
    if data is None: return b''
    lines, first, last = _parse(path, data, start, end)
    return data if first is None else b''.join(lines[:first] + lines[last + 1:])


def _save(path, data):
    path = Path(os.path.realpath(path))
    try: mode = path.stat().st_mode & 0o777
    except FileNotFoundError: mode = 0o644
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
            file.flush(); os.fsync(file.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    except BaseException:
        try: os.unlink(name)
        except OSError: pass
        raise


def _eol(lines):
    return b'\r\n' if lines and lines[0].endswith(b'\r\n') else b'\n'


def _block(start, text, end, eol, final):
    body = text.replace('\r\n', '\n').split('\n')
    return eol.join([start.encode(), *(line.encode() for line in body), end.encode()]) + (eol if final else b'')


def write(path, start, end, text):
    """Replace, append or (text None) remove the block. True when the file changed."""
    current = _load(path)
    if current is None:
        if text is None: return False
        _save(path, _block(start, text, end, b'\n', True))
        return True
    lines, first, last = _parse(path, current, start, end)
    eol = _eol(lines)
    if first is not None:
        final = lines[last].endswith((b'\n', b'\r'))
        if text is None:
            lo = first
            if last == len(lines) - 1 and first >= 1 and not lines[first - 1].strip() and (first == 1 or lines[first - 2].strip()):
                lo = first - 1
            if not final and lo > 0: new = b''.join(lines[:lo - 1]) + lines[lo - 1].rstrip(b'\r\n')
            else: new = b''.join(lines[:lo]) + b''.join(lines[last + 1:])
        else: new = b''.join(lines[:first]) + _block(start, text, end, eol, final) + b''.join(lines[last + 1:])
    elif text is None: return False
    else:
        if not current: new = _block(start, text, end, eol, True)
        elif current.endswith((b'\n', b'\r')): new = current + eol + _block(start, text, end, eol, True)
        else: new = current + eol + eol + _block(start, text, end, eol, False)
    if new == current: return False
    _save(path, new)
    return True
