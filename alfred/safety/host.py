"""Long jobs on the host, in their own session, surviving restarts: the voice shell's start_job, scheduled commands
and audio renders (alfred.tasks.command_jobs). They run in a login shell without Alfred's own keys.
"""

import asyncio
import ctypes
import functools
import json
import logging
import os
import pwd
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from alfred.auth import credentials, logins
from alfred.config import DATA_DIR, HOME_DIR
from alfred.core import platform
from alfred.core.files import write_atomic
from alfred.safety.redact import redact
from alfred.storage import profile as profiles

log = logging.getLogger("alfred.safety.host")


def _login_shell() -> tuple[str, str]:
    """zsh on macOS; on Linux the user's own zsh or bash, else bash, else sh. A login shell, for the user's PATH."""
    if platform.MAC:
        return "/bin/zsh", "-lc"
    try:
        mine = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        mine = ""
    for shell in (mine if Path(mine).name in ("zsh", "bash") else "", "/bin/bash", "/usr/bin/bash",
                  shutil.which("bash") or ""):
        if shell and os.access(shell, os.X_OK):
            return shell, "-lc"
    return "/bin/sh", "-lc"


SHELL = _login_shell()
TASK_DATA_DIR = DATA_DIR / "tasks"  # task folders

# Variable names that carry a credential
SENSITIVE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSCODE|API_?KEY|PRIVATE_KEY|MASTER_KEY|CREDENTIAL|AUTH",
                        re.IGNORECASE)


# ── Environment ─────────────────────────────────────────────────


def env_keys() -> set[str]:
    """Names of Alfred's own settings and keys: .env plus vault-held vars that get re-exported into the env."""
    return set(dotenv_values(HOME_DIR / ".env")) | {*credentials.VARS, logins.VAR}


def scrubbed_env() -> dict[str, str]:
    """The runner's environment minus Alfred's own keys."""
    secret = env_keys()
    return {k: v for k, v in os.environ.items() if k not in secret}


# ── Which processes are whose ───────────────────────────────────
# Each job gets its own session. A session is identified by (leader pid, start time), since pids get reused.


class _BsdInfo(ctypes.Structure):
    """macOS struct proc_bsdinfo (sys/proc_info.h), filled by proc_pidinfo; avoids the setuid ps."""
    _fields_ = [(n, ctypes.c_uint32) for n in ("flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid",
                                                "svuid", "svgid", "rfu")] + [
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        *((n, ctypes.c_uint32) for n in ("nfiles", "pgid", "pjobc", "tdev", "tpgid")), ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


_PROC_PIDTBSDINFO = 3


@functools.cache
def _libproc() -> ctypes.CDLL | None:
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
    except OSError:
        return None
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return lib


def _info(pid: int) -> _BsdInfo | None:
    if (lib := _libproc()) is None:
        return None
    info = _BsdInfo()
    got = lib.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    return info if got == ctypes.sizeof(info) else None


def _pids() -> list[int]:
    """Every process's pid."""
    if _procfs():
        try:
            return [int(name) for name in os.listdir(_PROC) if name.isdigit()]
        except OSError:
            return []
    if (lib := _libproc()) is None or (count := lib.proc_listallpids(None, 0)) <= 0:
        return []
    buffer = (ctypes.c_int * (count + 64))()
    got = lib.proc_listallpids(buffer, ctypes.sizeof(buffer))
    return [p for p in buffer[:max(got, 0)] if p > 0]


def _start(info: _BsdInfo) -> float:
    return info.start_sec + info.start_usec / 1e6


# Linux: /proc/<pid>/stat has the session, state, start time and name
_PROC = Path("/proc")


def _procfs() -> bool:
    return platform.LINUX


@dataclass(frozen=True)
class _Process:
    pid: int
    start: float  # epoch seconds
    name: str
    zombie: bool  # ended, not yet reaped


@functools.cache
def _clock_ticks() -> int:
    return os.sysconf("SC_CLK_TCK")


def _boot_time() -> float:
    """Boot time, epoch seconds. NaN if unknown, so no start time ever matches and nothing is signalled."""
    try:
        for line in (_PROC / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError):
        pass
    return float("nan")


def _stat(pid: int) -> tuple[int, _Process] | None:
    """(session id, process) for a running pid on Linux, or None."""
    try:
        raw = (_PROC / str(pid) / "stat").read_text(errors="replace")
        name, rest = raw[raw.index("(") + 1:raw.rindex(")")], raw[raw.rindex(")") + 2:].split()
        state, sid, ticks = rest[0], int(rest[3]), int(rest[19])  # fields 3, 6 and 22 of proc(5)
    except (OSError, ValueError, IndexError):
        return None
    return sid, _Process(pid, _boot_time() + ticks / _clock_ticks(), name, state in ("Z", "X"))


def started_at(pid: int) -> float | None:
    """Process start time (epoch seconds), or None if not running."""
    if _procfs():
        return found[1].start if (found := _stat(pid)) else None
    return _start(info) if (info := _info(pid)) else None


def _unchanged(pid: int, started: float) -> bool:
    """Whether `pid` is still the process started at `started`, or not running (never a reused pid)."""
    now = started_at(pid)
    return now is None or abs(now - started) <= 2



_SZOMB = 5  # zombie: ended, not yet reaped (sys/proc.h)


def _session(leader: int) -> Iterator[_Process]:
    """Every process in a session, across process groups."""
    if _procfs():
        for pid in _pids():
            if (found := _stat(pid)) and found[0] == leader:
                yield found[1]
        return
    for pid in _pids():
        try:
            if os.getsid(pid) != leader:
                continue
        except OSError:
            continue
        if info := _info(pid):
            yield _Process(pid, _start(info), (info.name or info.comm).decode(errors="replace"), info.status == _SZOMB)


def session_members(leader: int) -> list[int]:
    """Live (non-zombie) pids in a session, across process groups."""
    return [p.pid for p in _session(leader) if not p.zombie]


def _send(pid: int, sig: int, group: bool = True) -> None:
    """Send `sig` to the process group led by `pid` (or `pid` alone); ignore if gone."""
    try:
        (os.killpg if group else os.kill)(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


# ── Long jobs ───────────────────────────────────────────────────

# Runs the command, writes its exit status, and posts a desktop notification even if nobody is listening.
# Not `status`: it's read-only in zsh and assigning it would abort before the exit file is written.
_NOTIFY = ("""  osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title (item 2 of argv)' \\
    -e 'end run' -- "Job $ALFRED_JOB_ID, $ALFRED_JOB_TITLE, $word." "$ALFRED_JOB_FROM" >/dev/null 2>&1"""
           if platform.MAC else
           """  command -v notify-send >/dev/null 2>&1 && \\
    notify-send --app-name="$ALFRED_JOB_FROM" -- "$ALFRED_JOB_FROM" "Job $ALFRED_JOB_ID, $ALFRED_JOB_TITLE, $word." \\
    >/dev/null 2>&1""")
_JOB_SCRIPT = f"""(eval "$ALFRED_JOB_COMMAND")
exit_code=$?
echo "$exit_code" > "$ALFRED_JOB_EXIT"
if [ -n "$ALFRED_JOB_NOTIFY" ]; then
  if [ "$exit_code" -eq 0 ]; then word=finished; else word=failed; fi
{_NOTIFY}
fi
exit "$exit_code"
"""
RUNNING = "running"
STOPPED = "stopped"
STOP_GRACE = 5.0  # seconds between SIGTERM and SIGKILL
JOB_POLL = 2.0  # seconds between checks on a running job


def outcome(code: int) -> str:
    """Spoken outcome: 'is done' or 'failed, with exit code N'."""
    return "is done" if code == 0 else f"failed, with exit code {code}"


def alive(pid: int) -> bool:
    """Whether a pid is running (including other users' processes)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


class Jobs:
    """Long commands in their own session, surviving CLI, voice-session and runner restarts.

    Files in `folder`: N.json (record), N.log (output), N.exit (status) or N.stopped. A task's jobs carry `task`.
    """

    def __init__(self, folder: Path, shell: tuple[str, ...] = SHELL, notify: bool = True) -> None:
        self.folder, self.shell, self.notify = folder, shell, notify
        self.poll = JOB_POLL
        self.grace = STOP_GRACE
        self._watchers: set[asyncio.Task] = set()
        self._procs: dict[int, subprocess.Popen] = {}  # jobs started here, so `stop` can reap them

    def _records(self) -> list[dict]:
        records = []
        for file in self.folder.glob("*.json"):
            try:
                records.append(json.loads(file.read_text()))
            except (OSError, ValueError):
                continue
        return sorted(records, key=lambda r: r["id"])

    def launch(self, command: str, title: str, cwd: Path, task: int | None = None, env: dict[str, str] | None = None,
               report: bool = False, extra: dict | None = None, job_id: int | None = None) -> dict:
        """Start a job; returns its record.

        Raises ValueError if `cwd` isn't a folder or an explicit `job_id` is taken. `report`: its last output line is
        spoken at the end. `extra` is merged into the record.
        """
        if not cwd.is_dir():
            raise ValueError(f"{cwd} isn't a folder.")
        self.folder.mkdir(parents=True, exist_ok=True)
        if job_id is None:
            job_id = max((r["id"] for r in self._records()), default=0) + 1
        elif (self.folder / f"{job_id}.json").exists():
            raise ValueError(f"There's already a job {job_id}.")
        base = self.folder / str(job_id)
        env = scrubbed_env() | (env or {}) | {
            "ALFRED_JOB_COMMAND": command, "ALFRED_JOB_EXIT": f"{base}.exit", "ALFRED_JOB_ID": str(job_id),
            "ALFRED_JOB_TITLE": title, "ALFRED_JOB_NOTIFY": "1" if self.notify else "",
            "ALFRED_JOB_FROM": profiles.assistant(),  # the notification's title
        }
        argv = [*self.shell, _JOB_SCRIPT]
        # Popen, not asyncio subprocesses, which kill their child when the loop drops them
        with open(f"{base}.log", "wb") as out:
            proc = subprocess.Popen(argv, cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        job = {"id": job_id, "title": title, "command": command, "folder": str(cwd), "pid": proc.pid, "started": time.time()}
        if task is not None:
            job["task"] = task
        if report:
            job["report"] = True
        job |= extra or {}
        write_atomic(Path(f"{base}.json"), json.dumps(job))
        log.warning("job %s started (pid %s) in %s%s: %s", job_id, proc.pid, cwd,
                    f" for task {task}" if task is not None else "", command)
        self._procs[job_id] = proc
        watcher = asyncio.create_task(self._watch(job, proc))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return job

    async def _watch(self, job: dict, proc: subprocess.Popen) -> None:
        while (code := proc.poll()) is None:
            await asyncio.sleep(self.poll)
        self._procs.pop(job["id"], None)
        if self._stopped(job):  # `stop` reports it
            log.warning("job %s was stopped (%s)", job["id"], code)
            return
        log.warning("job %s ended with %s", job["id"], code)
        self.ended(job, code)

    def ended(self, job: dict, code: int) -> None:
        """Hook: a job started here ended on its own (not via `stop`)."""

    def _stopped(self, job: dict) -> bool:
        return (self.folder / f"{job['id']}.stopped").exists()

    def _state(self, job: dict) -> str:
        if self._stopped(job):
            return STOPPED
        try:
            code = int((self.folder / f"{job['id']}.exit").read_text().strip())
            return "done" if code == 0 else f"failed, exit code {code}"
        except (OSError, ValueError):
            return RUNNING if alive(job["pid"]) else "ended without reporting (stopped or killed)"

    def _view(self, job: dict, now: float, tail: int = 800) -> dict:
        try:
            end = redact(self.log_file(job["id"]).read_bytes()[-tail:].decode(errors="replace"), job["folder"])
        except OSError:
            end = ""
        return {"id": job["id"], "title": job["title"], "command": job["command"], "folder": job["folder"],
                "state": self._state(job), "minutes": round((now - job["started"]) / 60), "output_tail": end}

    def status(self, task: int | None = None) -> list[dict]:
        """Last day's jobs (optionally one task's), oldest first, each with its output tail."""
        now = time.time()
        return [self._view(job, now) for job in self._records()
                if now - job["started"] <= 86400 and (task is None or job.get("task") == task)]

    def get(self, job_id: int, task: int | None = None) -> dict | None:
        return next((j for j in self._records() if j["id"] == job_id and (task is None or j.get("task") == task)), None)

    def _job(self, job_id: int, task: int | None) -> dict:
        """`get`, raising ValueError if missing."""
        if (job := self.get(job_id, task)) is None:
            raise ValueError(f"There's no job {job_id}" + (f" of task {task}." if task is not None else "."))
        return job

    def log_file(self, job_id: int) -> Path:
        return self.folder / f"{job_id}.log"

    async def wait(self, job_id: int, timeout: float, task: int | None = None, tail: int = 3000) -> dict:
        """Wait up to `timeout` s for a job to end; returns its view with output tail."""
        job = self._job(job_id, task)
        deadline = time.monotonic() + timeout
        while self._state(job) == RUNNING and time.monotonic() < deadline:
            await asyncio.sleep(min(self.poll, max(0.0, deadline - time.monotonic())))
        return self._view(job, time.time(), tail)

    # Stopping uses the record's pid checked against its start time, so a reused pid is never signalled.

    def _ours(self, job: dict) -> bool:
        """Whether the job's pid is still its own (or not running, so its session's leftovers are the job's)."""
        return _unchanged(job["pid"], job["started"])

    def _left(self, job: dict) -> list[int]:
        """Live pids in the job's session."""
        if (proc := self._procs.get(job["id"])) is not None:
            proc.poll()  # reap the leader so it doesn't linger as a zombie
        return session_members(job["pid"]) if self._ours(job) else []

    def _signal(self, job: dict, sig: int) -> None:
        """Send `sig` to the job's process group and to session members outside it."""
        if not self._ours(job):
            return
        _send(job["pid"], sig)
        for pid in session_members(job["pid"]):
            _send(pid, sig, group=False)

    async def stop(self, job_id: int, task: int | None = None) -> dict:
        """SIGTERM the whole job, SIGKILL leftovers after `grace` s; writes N.stopped first. Returns its view.

        Raises ValueError if the job is missing or already ended.
        """
        job = self._job(job_id, task)
        state = self._state(job)
        # A non-running job may still have children if only its leader died: stop those too
        if state == STOPPED or (self.folder / f"{job_id}.exit").exists() or not self._left(job):
            raise ValueError(f"job {job_id}, {job['title']}, isn't running"
                             + (f" ({state})." if state != RUNNING else "."))
        write_atomic(self.folder / f"{job_id}.stopped", json.dumps({"at": time.time()}))
        self._signal(job, signal.SIGTERM)
        if not await self._gone(job, self.grace):
            log.warning("job %s still running %s s after SIGTERM: killing it", job_id, self.grace)
            self._signal(job, signal.SIGKILL)
            await self._gone(job, 2)
        log.warning("job %s stopped: %s", job_id, job["command"])
        return self._view(job, time.time())

    async def _gone(self, job: dict, timeout: float) -> bool:
        """Wait up to `timeout` s for the whole job to end; returns whether it did."""
        deadline = time.monotonic() + timeout
        while self._left(job):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True
