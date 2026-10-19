"""Memory used by the voice agent's processes, logged periodically and on demand.

  uv run python -m alfred.voice.ram    per-process sizes of the running agent, plus total

On macOS size is phys_footprint (Activity Monitor's Memory column): RAM plus compressed/swapped pages and MLX's Metal
buffers, which RSS misses. On Linux, and if that can't be read, it's RSS (VmRSS). The main process logs every
ALFRED_VOICE_MEMORY_LOG_MINUTES; job processes log at debug after each reply.
"""

import ctypes
import ctypes.util
import logging
import os
import threading
import time

import psutil

from alfred.core import platform

log = logging.getLogger("alfred.voice.ram")

MB = 2**20


class _RUsageInfoV0(ctypes.Structure):
    """libproc's rusage_info_v0: a prefix of every later version, enough for phys_footprint."""
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size", "resident_size",
        "phys_footprint", "proc_start_abstime", "proc_exit_abstime")]


def _libproc():
    if not platform.MAC:
        return None
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_RUsageInfoV0)]
        return lib
    except (OSError, AttributeError):
        return None


_LIBPROC = _libproc()


def footprint(pid: int) -> int | None:
    """phys_footprint in bytes; None if unreadable."""
    info = _RUsageInfoV0()
    if _LIBPROC is None or _LIBPROC.proc_pid_rusage(pid, 0, ctypes.byref(info)) != 0:
        return None
    return info.phys_footprint


def size(proc: psutil.Process) -> int:
    """Footprint in bytes, falling back to RSS."""
    return footprint(proc.pid) or proc.memory_info().rss


def _program(proc: psutil.Process) -> str:
    try:
        cmd = proc.cmdline()
    except psutil.Error:
        return "?"
    if any("spawn_main" in arg for arg in cmd):
        return "python"
    if any("resource_tracker" in arg for arg in cmd):
        return "tracker"
    return os.path.basename((cmd or ["?"])[0])


def roles(main: psutil.Process) -> list[tuple[str, psutil.Process]]:
    """Worker processes labelled by likely role.

    The shared speech process starts first, so it's the oldest Python child. A job with a claude CLI child is a
    "call"; other jobs are warm "spare"s."""
    named = [("main", main)]
    children = main.children()
    pythons = sorted((c for c in children if _program(c) == "python"), key=lambda c: c.pid)
    for child in children:
        if child in pythons:
            below = child.children(recursive=True)
            role = "speech" if child is pythons[0] else "call" if below else "spare"
            named.append((role, child))
            named += [(_program(p), p) for p in below]
        else:
            named.append((_program(child), child))
    return named


def tree(main: psutil.Process | None = None) -> list[tuple[str, int, int]]:
    """(role, pid, bytes) for the main process and all descendants."""
    out = []
    for role, proc in roles(main or psutil.Process()):
        try:
            out.append((role, proc.pid, size(proc)))
        except psutil.Error:  # gone meanwhile
            continue
    return out


def describe(procs: list[tuple[str, int, int]]) -> str:
    """One line: total, then each process."""
    parts = [f"{role} {n / MB:.0f} MB (pid {pid})" for role, pid, n in procs]
    return f"voice memory {sum(n for *_, n in procs) / MB:.0f} MB: " + ", ".join(parts)


def log_tree(level: int = logging.INFO, main: psutil.Process | None = None) -> None:
    try:
        log.log(level, describe(tree(main)))
    except Exception as e:  # never break a call over this
        log.debug("couldn't measure the voice processes: %r", e)


def log_job(level: int = logging.DEBUG) -> None:
    """Log the whole worker tree from a job process (via its parent)."""
    if log.isEnabledFor(level):
        log_tree(level, psutil.Process(os.getppid()))


def watch(minutes: float, first: float = 90) -> threading.Thread | None:
    """Log the tree from a daemon thread: after `first` seconds (models loaded), then every `minutes`. None if off."""
    if minutes <= 0:
        return None

    def run() -> None:
        time.sleep(min(first, minutes * 60))
        while True:
            log_tree()
            time.sleep(minutes * 60)

    thread = threading.Thread(target=run, name="voice-memory", daemon=True)
    thread.start()
    return thread


def _agent_main() -> psutil.Process | None:
    """Root alfred.voice.agent process (its parent isn't one)."""
    agents = {p.pid: p for p in psutil.process_iter(["cmdline"])
              if "alfred.voice.agent" in " ".join(p.info["cmdline"] or [])}
    for p in agents.values():
        if p.ppid() not in agents:
            return p
    return None


if __name__ == "__main__":
    if (main := _agent_main()) is None:
        raise SystemExit("the voice agent isn't running")
    procs = tree(main)
    for role, pid, n in procs:
        print(f"{role:8} {pid:>7} {n / MB:8.0f} MB")
    print(f"{'total':8} {'':>7} {sum(n for *_, n in procs) / MB:8.0f} MB")
