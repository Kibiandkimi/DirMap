"""Return large freed buffers to the OS on glibc; elsewhere these are no-ops.

The server is long-lived, while one image analysis or graph build allocates
scratch arrays larger than a small VM's memory. Under glibc's dynamic mmap
threshold, freed blocks up to 32 MiB stay in the heap and are swapped out
instead of reused, so a finished job kept about 1 GiB resident.
"""
import ctypes
import ctypes.util

M_MMAP_THRESHOLD = -3
M_ARENA_MAX = -8
_libc = None


def _glibc():
    global _libc
    if _libc is None:
        try:
            library = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6')
            _libc = library if hasattr(library, 'mallopt') and hasattr(library, 'malloc_trim') else False
        except OSError:
            _libc = False
    return _libc or None


def configure(mmap_threshold=4 << 20, arenas=2):
    """Serve each large array from its own mapping, freed on release."""
    libc = _glibc()
    if libc is not None:
        libc.mallopt(M_MMAP_THRESHOLD, mmap_threshold)
        libc.mallopt(M_ARENA_MAX, arenas)


def release():
    """Give free heap pages back after a job; live data is untouched."""
    libc = _glibc()
    if libc is not None:
        libc.malloc_trim(0)
