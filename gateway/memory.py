"""glibc malloc tuning: fewer arenas and returning freed heap memory; a no-op without glibc (macOS)."""
import ctypes

M_ARENA_MAX = -8


def _libc():
    try: libc = ctypes.CDLL(None)
    except (OSError, TypeError): return None
    return libc if hasattr(libc, 'mallopt') and hasattr(libc, 'malloc_trim') else None


LIBC = _libc()


def limit_arenas(n):
    """Caps malloc arenas; call before threads start, since each thread may otherwise get its own 64 MB arena."""
    return LIBC is not None and LIBC.mallopt(M_ARENA_MAX, n) == 1


def trim():
    """Hands free pages in every arena back to the system."""
    if LIBC is None: return False
    LIBC.malloc_trim(0)
    return True
