"""Whether this computer is short of memory, read cheaply enough to ask before every task starts.

On macOS it's the kernel's own pressure level (kern.memorystatus_vm_pressure_level, what Activity Monitor's Memory
Pressure graph shows): free memory alone says little there, as the file cache and compression fill it. On Linux it's
MemAvailable (psutil). Unreadable counts as not short, so nothing waits on a reading that can't come.
"""

import ctypes
import logging

import psutil

from alfred.core import platform

log = logging.getLogger("alfred.core.memory")

WARN = 2  # macOS pressure levels: 1 normal, 2 warning (compressing and swapping hard), 4 critical
LOW = 0.1  # Linux: short below this share of memory available


def _mac_level() -> int | None:
    try:
        value, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
        found = ctypes.CDLL(None).sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(value),
                                               ctypes.byref(size), None, 0)
    except (OSError, AttributeError):
        return None
    return value.value if found == 0 else None


def short() -> bool:
    """Whether memory is short now: macOS's pressure at warning or worse, or under LOW of Linux's available."""
    try:
        if platform.MAC:
            return (_mac_level() or 0) >= WARN
        found = psutil.virtual_memory()
        return found.available < LOW * found.total
    except Exception as e:  # never stops a task
        log.debug("couldn't read memory pressure: %r", e)
        return False
