"""Own one marked block in a text file; everything outside the markers stays byte-identical."""
import os
from pathlib import Path
import tempfile


def _split(text, start, end):
    """(lines, first, last) of the block's marker lines, or None."""
    lines = text.splitlines(keepends=True)
    first = next((i for i, line in enumerate(lines) if line.rstrip('\r\n') == start), None)
    if first is None: return lines, None, None
    last = next((i for i in range(first + 1, len(lines)) if lines[i].rstrip('\r\n') == end), None)
    return lines, first, last


def _load(path):
    try: return Path(path).read_text()
    except FileNotFoundError: return None


def read(path, start, end):
    text = _load(path)
    if text is None: return None
    lines, first, last = _split(text, start, end)
    if first is None or last is None: return None
    return ''.join(lines[first + 1:last]).removesuffix('\n')


def _save(path, text):
    path = Path(path)
    try: mode = path.stat().st_mode & 0o777
    except FileNotFoundError: mode = 0o644
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'w') as file:
            file.write(text)
            file.flush(); os.fsync(file.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    except BaseException:
        try: os.unlink(name)
        except OSError: pass
        raise


def write(path, start, end, text):
    """Replace, append or (text None) remove the block. True when the file changed."""
    current = _load(path)
    block = None if text is None else start + '\n' + text + '\n' + end + '\n'
    if current is None:
        if block is None: return False
        _save(path, block)
        return True
    lines, first, last = _split(current, start, end)
    if first is not None and last is not None:
        if block is None:
            lo = first - 1 if first > 0 and lines[first - 1].strip() == '' else first
            new = ''.join(lines[:lo] + lines[last + 1:])
        else:
            new = ''.join(lines[:first]) + block + ''.join(lines[last + 1:])
    elif block is None: return False
    else:
        gap = '' if not current else ('\n' if current.endswith('\n') else '\n\n')
        new = current + gap + block
    if new == current: return False
    _save(path, new)
    return True
