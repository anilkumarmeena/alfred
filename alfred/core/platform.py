"""Which OS Alfred runs on: macOS, or Linux (Windows through WSL2). Imports nothing Unix-only, so it can say so."""

import ctypes
import os
import sys

MAC = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")


def _wsl() -> bool:
    try:
        with open("/proc/sys/kernel/osrelease") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


WSL = LINUX and _wsl()
APPLE_SILICON = MAC and os.uname().machine == "arm64"
# What the model is told it runs on, so it suggests commands that work here
NAME = "macOS" if MAC else "Linux (WSL2 on Windows)" if WSL else "Linux" if LINUX else sys.platform
# systemd user units on Linux, launchd agents on macOS
SERVICE_MANAGER = "launchd" if MAC else "systemd" if LINUX else ""

def sysctl(name: str) -> int | None:
    """An integer kernel value by name on macOS (sysctlbyname), or None: not there, or not macOS."""
    try:
        value, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
        found = ctypes.CDLL(None).sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0)
    except (OSError, AttributeError):
        return None
    return value.value if found == 0 else None


WINDOWS_HINT = ("Alfred doesn't run on Windows directly. Run it inside WSL2 (Ubuntu): "
                "https://learn.microsoft.com/windows/wsl/install")


def unsupported() -> str:
    """Why this OS can't run Alfred; "" on macOS and Linux."""
    if MAC or LINUX:
        return ""
    if sys.platform in ("win32", "cygwin", "msys"):
        return WINDOWS_HINT
    return f"Alfred runs on macOS and Linux, not {sys.platform}."


def require_supported() -> None:
    """Exit with a clear message on native Windows or another unsupported OS."""
    if problem := unsupported():
        raise SystemExit(problem)
