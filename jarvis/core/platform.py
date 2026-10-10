"""Which OS JARVIS runs on: macOS, or Linux (Windows through WSL2). Imports nothing Unix-only, so it can say so."""

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

WINDOWS_HINT = ("JARVIS doesn't run on Windows directly. Run it inside WSL2 (Ubuntu): "
                "https://learn.microsoft.com/windows/wsl/install")


def unsupported() -> str:
    """Why this OS can't run JARVIS; "" on macOS and Linux."""
    if MAC or LINUX:
        return ""
    if sys.platform in ("win32", "cygwin", "msys"):
        return WINDOWS_HINT
    return f"JARVIS runs on macOS and Linux, not {sys.platform}."


def require_supported() -> None:
    """Exit with a clear message on native Windows or another unsupported OS."""
    if problem := unsupported():
        raise SystemExit(problem)
