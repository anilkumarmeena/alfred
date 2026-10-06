"""How much memory the voice agent's processes take: logged now and then, and on demand.

  uv run python -m jarvis.voice.ram    the running agent's processes, one line each, and their total

Each process is measured by its footprint, what Activity Monitor's Memory column shows: what it has in RAM plus what
macOS compressed or swapped out of it, and the GPU (Metal) buffers MLX holds, which resident size can miss. Where the
footprint can't be read, resident size stands in. The worker's main process logs one line for the whole tree every
JARVIS_VOICE_MEMORY_LOG_MINUTES (jarvis.voice.agent), and each job process logs its own tree at debug level after
every reply.
"""

import ctypes
import ctypes.util
import logging
import os
import threading
import time

import psutil

log = logging.getLogger("jarvis.voice.ram")

MB = 2**20


class _RUsageInfoV0(ctypes.Structure):
    """libproc's rusage_info_v0: the start of every later version, and all a footprint needs."""
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size", "resident_size",
        "phys_footprint", "proc_start_abstime", "proc_exit_abstime")]


def _libproc():
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_RUsageInfoV0)]
        return lib
    except (OSError, AttributeError):  # not macOS
        return None


_LIBPROC = _libproc()


def footprint(pid: int) -> int | None:
    """A process's footprint in bytes (macOS's phys_footprint); None where it can't be read."""
    info = _RUsageInfoV0()
    if _LIBPROC is None or _LIBPROC.proc_pid_rusage(pid, 0, ctypes.byref(info)) != 0:
        return None
    return info.phys_footprint


def size(proc: psutil.Process) -> int:
    """Bytes a process takes: its footprint, or its resident size where that can't be read."""
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
    """The worker's processes, each named by what it most likely is. LiveKit starts the shared speech process
    (jarvis.voice.speech) before any job process, so it's the oldest Python child; a job process with a call on has
    the brain's claude CLI under it ("call"); the others are warm spares."""
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
    """(role, pid, bytes) for the worker's main process and everything under it."""
    out = []
    for role, proc in roles(main or psutil.Process()):
        try:
            out.append((role, proc.pid, size(proc)))
        except psutil.Error:  # gone meanwhile
            continue
    return out


def describe(procs: list[tuple[str, int, int]]) -> str:
    """One line: the total, then each process's size."""
    parts = [f"{role} {n / MB:.0f} MB (pid {pid})" for role, pid, n in procs]
    return f"voice memory {sum(n for *_, n in procs) / MB:.0f} MB: " + ", ".join(parts)


def log_tree(level: int = logging.INFO, main: psutil.Process | None = None) -> None:
    try:
        log.log(level, describe(tree(main)))
    except Exception as e:  # measuring must never break a call
        log.debug("couldn't measure the voice processes: %r", e)


def log_job(level: int = logging.DEBUG) -> None:
    """From a job process: the whole worker tree, from its parent, the main process."""
    if log.isEnabledFor(level):
        log_tree(level, psutil.Process(os.getppid()))


def watch(minutes: float, first: float = 90) -> threading.Thread | None:
    """Log the tree from a daemon thread in the worker's main process: `first` seconds after it starts, once the
    models have loaded, then every `minutes`. None when `minutes` is 0 (off)."""
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
    """The running voice agent's main process: the one started with jarvis.voice.agent whose parent isn't one too."""
    agents = {p.pid: p for p in psutil.process_iter(["cmdline"])
              if "jarvis.voice.agent" in " ".join(p.info["cmdline"] or [])}
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
