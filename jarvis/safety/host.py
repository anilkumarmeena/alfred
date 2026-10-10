"""Run commands on the Mac outside Claude Code's sandbox: task host_run/start_job and voice-shell long jobs.

Task commands run under `sandbox-exec` with `profile()`: allow by default, deny only
  - reading secrets (JARVIS secrets/ and .env, home logins/keys, secret-named files in home; commands.py lists)
  - writing JARVIS's folder, except task worktrees, cache, data/tasks/ and git objects
  - writing home logins, shell start-up files and launch agents; the runner socket; setuid /bin/ps
Network and other writes stay open (Xcode, package caches). `task_env()` strips .env keys and credential-named
vars but keeps SSH_AUTH_SOCK. Approved brew/port installs (commands.own_sandbox) can't nest and run `walls=False`.

  uv run python -m jarvis.safety.host check    runs the profile's own checks on this Mac
"""

import asyncio
import ctypes
import functools
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from jarvis.core import credentials, logins, profile as profiles
from jarvis.core.config import DATA_DIR, DB_FILE, ROOT, RUNNER_SOCKET, TASK_CACHE_DIR, WORKTREES_DIR
from jarvis.core.files import write_atomic
from jarvis.safety.commands import HOME_SECRETS, JARVIS_SECRETS, SECRET_DIRS, secret_file_names

log = logging.getLogger("jarvis.safety.host")

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PS = "/bin/ps"  # setuid root: can show any process's environment
SHELL = ("/bin/zsh", "-lc")  # login shell, for Homebrew's and the user's PATH
TASK_DATA_DIR = DATA_DIR / "tasks"  # research task folders and task jobs
TASK_JOBS_DIR = TASK_DATA_DIR / "jobs"
MAX_OUTPUT = 30_000  # chars of host_run output kept (head and tail)
RUN_TIMEOUT = 600  # seconds; longer work should be a job

# commands.SECRET_DIRS / SECRET_FILES, matched within one path component
_SECRET_DIR_NAMES = "(" + "|".join(re.escape(d) for d in sorted(SECRET_DIRS)) + ")"
_SECRET_FILE_NAMES = "(" + secret_file_names("[^/]", pem=False) + ")"
# .pem files are keys, except CA bundles shipped by packages
_PEM_KEPT = r"/(site-packages|dist-packages|node_modules|Cellar)/|/(ca|cert|certs|cacert|ca-bundle|ca-certificates)\.pem$"
# Home files that could run something later, outside the walls
_STARTUP_FILES = [".zshrc", ".zprofile", ".zshenv", ".zlogin", ".bashrc", ".bash_profile", ".profile", ".gitconfig"]
_STARTUP_DIRS = ["Library/LaunchAgents", ".config/git"]
# Variable names that carry a credential
SENSITIVE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSCODE|API_?KEY|PRIVATE_KEY|MASTER_KEY|CREDENTIAL|AUTH",
                        re.IGNORECASE)
# Kept: the SSH agent signs without exposing keys, so approved git push/ssh works
_KEPT = {"SSH_AUTH_SOCK"}
# The only readable ~/.ssh files (no keys), needed to connect via the agent
_SSH_READABLE = ["known_hosts", "known_hosts2", "config"]


# ── The walls ───────────────────────────────────────────────────


def _real(path: Path | str) -> str:
    """Path as Seatbelt sees it: absolute, symlinks (e.g. /tmp) resolved."""
    return os.path.realpath(os.path.expanduser(str(path)))


def _str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rx(text: str) -> str:
    """Escape for a Seatbelt regex. Quotes can't be escaped in #"...", so they become `.`."""
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", text).replace('"', ".")


def _any(*filters: str) -> str:
    return f"(require-any {' '.join(filters)})"


def profile(home: Path | str | None = None) -> str:
    """Seatbelt profile for a task's host commands."""
    home = _real(home or Path.home())
    root = _real(ROOT)
    secret_paths = [_real(p) for p in JARVIS_SECRETS] + [_real(p.replace("~", home, 1)) for p in HOME_SECRETS]
    reads = _any(
        *(f"(subpath {_str(p)})" for p in dict.fromkeys(secret_paths)),
        f'(regex #"^{_rx(home)}/(.*/)?{_SECRET_DIR_NAMES}(/|$)")',
        f'(regex #"^{_rx(home)}/(.*/)?{_SECRET_FILE_NAMES}$")',
        f'(require-all (regex #"^{_rx(home)}/.*\\.pem$") (require-not (regex #"{_PEM_KEPT}")))',
        f'(regex #"^{_rx(root)}/(.*/)?{_SECRET_FILE_NAMES}$")',
    )
    git = f"{root}/.git"
    writable = _any(*(f"(subpath {_str(_real(p))})" for p in (WORKTREES_DIR, TASK_CACHE_DIR, TASK_DATA_DIR)),
                    f"(subpath {_str(git)})")
    writes = _any(
        f"(require-all (subpath {_str(root)}) (require-not {writable}))",
        f"(subpath {_str(git + '/hooks')})", f"(literal {_str(git + '/config')})",
        *(f"(subpath {_str(p)})" for p in dict.fromkeys(secret_paths)),
        *(f"(literal {_str(f'{home}/{name}')})" for name in _STARTUP_FILES),
        *(f"(subpath {_str(f'{home}/{name}')})" for name in _STARTUP_DIRS),
    )
    readable = _any(f"(subpath {_str(_real(profiles.current().voice_logs))})",
                    *(f"(literal {_str(f'{home}/.ssh/{name}')})" for name in _SSH_READABLE))
    sock = _real(RUNNER_SOCKET)
    return "\n".join([
        "(version 1)",
        "(allow default)",
        f"(deny file-read* (require-all {reads} (require-not {readable})))",
        f"(deny file-write* {writes})",
        f"(deny file-read* file-write* (literal {_str(sock)}))",
        f"(deny network-outbound (remote unix-socket (path-literal {_str(sock)})))",
        # Setuid ps can read other processes' environments (MCP tokens). Seatbelt already blocks setuid; be explicit.
        f"(deny process-exec (literal {_str(PS)}))",
    ])


def env_keys() -> set[str]:
    """Names of JARVIS's own settings and keys: .env plus vault-held vars that get re-exported into the env."""
    return set(dotenv_values(ROOT / ".env")) | {*credentials.VARS, logins.VAR}


def scrubbed_env() -> dict[str, str]:
    """The runner's environment minus JARVIS's own keys."""
    secret = env_keys()
    return {k: v for k, v in os.environ.items() if k not in secret}


def task_env() -> dict[str, str]:
    """scrubbed_env() minus credential-named vars, keeping SSH_AUTH_SOCK."""
    return {k: v for k, v in scrubbed_env().items() if k in _KEPT or not SENSITIVE.search(k)}


def walled(argv: list[str], home: Path | str | None = None) -> list[str]:
    """Wrap argv in the host profile. Raises RuntimeError if sandbox-exec is missing (never runs unwalled)."""
    if not Path(SANDBOX_EXEC).exists():
        raise RuntimeError("sandbox-exec isn't on this Mac, so host commands can't run safely")
    return [SANDBOX_EXEC, "-p", profile(home), *argv]


# ── Commands ────────────────────────────────────────────────────


@dataclass
class Ran:
    code: int | None  # None: killed at the timeout
    output: str  # stdout+stderr, middle elided past MAX_OUTPUT
    seconds: float

    def text(self) -> str:
        how = f"exit code {self.code}" if self.code is not None else "stopped: it ran past its time limit"
        return f"[{how}, {self.seconds:.0f} s]\n{self.output}".rstrip()


class _Output:
    """Bounded output buffer: fixed head plus rolling tail."""

    def __init__(self, limit: int = MAX_OUTPUT) -> None:
        self.limit, self.head, self.tail, self.size = limit, bytearray(), bytearray(), 0

    def add(self, data: bytes) -> None:
        self.size += len(data)
        room = self.limit - len(self.head)
        self.head += data[:max(room, 0)]
        self.tail = (self.tail + data[max(room, 0):])[-self.limit:]

    def text(self) -> str:
        if self.size <= self.limit:
            return bytes(self.head).decode(errors="replace")
        head, tail = self.limit // 3, self.limit - self.limit // 3
        whole = bytes(self.head + self.tail)
        return (f"{whole[:head].decode(errors='replace')}\n… {self.size - self.limit} characters cut …\n"
                f"{whole[-tail:].decode(errors='replace')}")


def _send(pid: int, sig: int, group: bool = True) -> None:
    """Send `sig` to the process group led by `pid` (or `pid` alone); ignore if gone."""
    try:
        (os.killpg if group else os.kill)(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _kill(pid: int) -> None:
    _send(pid, signal.SIGKILL)


async def run(command: str, cwd: Path, timeout: float = RUN_TIMEOUT, walls: bool = True,
              shell: tuple[str, ...] = SHELL, env: dict[str, str] | None = None,
              started: Callable[[int], None] | None = None) -> Ran:
    """Run in a new session; wait for it and anything holding its output, up to `timeout` s, then kill.

    `env` extends task_env(). `started` receives the session id (shell pid) on start. Cancellation kills it.
    """
    argv = [*shell, command]
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *(walled(argv) if walls else argv), cwd=cwd, env=task_env() | (env or {}), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    if started:
        started(proc.pid)
    output = _Output()

    async def read() -> None:
        while data := await proc.stdout.read(65536):
            output.add(data)

    reader = asyncio.ensure_future(read())
    try:
        await asyncio.wait_for(asyncio.shield(reader), timeout)
        code = await proc.wait()
    except TimeoutError:
        _kill(proc.pid)
        try:  # a child in its own session may still hold the output open
            await asyncio.wait_for(reader, 5)
        except TimeoutError:
            pass
        code = None
    except asyncio.CancelledError:
        _kill(proc.pid)
        reader.cancel()
        raise
    return Ran(code, output.text(), time.monotonic() - start)


# ── Which processes are whose ───────────────────────────────────
# Each host command gets its own session. A session is identified by (leader pid, start time), since pids get reused.


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
    if (lib := _libproc()) is None or (count := lib.proc_listallpids(None, 0)) <= 0:
        return []
    buffer = (ctypes.c_int * (count + 64))()
    got = lib.proc_listallpids(buffer, ctypes.sizeof(buffer))
    return [p for p in buffer[:max(got, 0)] if p > 0]


def _start(info: _BsdInfo) -> float:
    return info.start_sec + info.start_usec / 1e6


def started_at(pid: int) -> float | None:
    """Process start time (epoch seconds), or None if not running."""
    return _start(info) if (info := _info(pid)) else None


def _unchanged(pid: int, started: float) -> bool:
    """Whether `pid` is still the process started at `started`, or not running (never a reused pid)."""
    now = started_at(pid)
    return now is None or abs(now - started) <= 2


def in_sessions(pid: int, sessions: Iterable[tuple[int, float]]) -> bool:
    """Whether a running process is in one of `sessions`, each (leader pid, start time)."""
    if pid <= 1:
        return False
    try:
        sid = os.getsid(pid)
    except OSError:
        return False
    # The leader may have exited; if its pid is in use, it must still be the same process
    return any(sid == leader and _unchanged(leader, at) for leader, at in sessions)


_SZOMB = 5  # zombie: ended, not yet reaped (sys/proc.h)


def _session(leader: int) -> Iterator[tuple[int, _BsdInfo]]:
    """(pid, info) for every process in a session, across process groups."""
    for pid in _pids():
        try:
            if os.getsid(pid) != leader:
                continue
        except OSError:
            continue
        if info := _info(pid):
            yield pid, info


def session_members(leader: int) -> list[int]:
    """Live (non-zombie) pids in a session, across process groups."""
    return [pid for pid, info in _session(leader) if info.status != _SZOMB]


def lingering(leader: int) -> list[tuple[int, str]]:
    """(pid, program name) of non-leader processes in a session, oldest first."""
    found = sorted((_start(info), pid, (info.name or info.comm).decode(errors="replace"))
                   for pid, info in _session(leader) if pid != leader)
    return [(pid, name) for _, pid, name in found]


# ── Long jobs ───────────────────────────────────────────────────

# Runs the command, writes its exit status, and posts a Mac notification even if nobody is listening.
# Not `status`: it's read-only in zsh and assigning it would abort before the exit file is written.
_JOB_SCRIPT = """(eval "$JARVIS_JOB_COMMAND")
exit_code=$?
echo "$exit_code" > "$JARVIS_JOB_EXIT"
if [ -n "$JARVIS_JOB_NOTIFY" ]; then
  if [ "$exit_code" -eq 0 ]; then word=finished; else word=failed; fi
  osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title "JARVIS"' -e 'end run' \
    -- "Job $JARVIS_JOB_ID, $JARVIS_JOB_TITLE, $word." >/dev/null 2>&1
fi
exit "$exit_code"
"""
RUNNING = "running"
STOPPED = "stopped"
STOP_GRACE = 5.0  # seconds between SIGTERM and SIGKILL


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

    Files in `folder`: N.json (record), N.log (output), N.exit (status) or N.stopped. Task jobs carry `task` and run
    inside the host profile when `walls`.
    """

    def __init__(self, folder: Path, shell: tuple[str, ...] = SHELL, notify: bool = True, walls: bool = False) -> None:
        self.folder, self.shell, self.notify, self.walls = folder, shell, notify, walls
        self.poll = 2.0  # seconds
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
               report: bool = False, extra: dict | None = None, job_id: int | None = None,
               walls: bool = True) -> dict:
        """Start a job; returns its record.

        Raises ValueError if `cwd` isn't a folder or an explicit `job_id` is taken. `report`: its last output line is
        spoken at the end. `extra` is merged into the record. `walls=False` skips the profile (commands.own_sandbox).
        """
        if not cwd.is_dir():
            raise ValueError(f"{cwd} isn't a folder.")
        self.folder.mkdir(parents=True, exist_ok=True)
        if job_id is None:
            job_id = max((r["id"] for r in self._records()), default=0) + 1
        elif (self.folder / f"{job_id}.json").exists():
            raise ValueError(f"There's already a job {job_id}.")
        base = self.folder / str(job_id)
        env = (task_env() if self.walls else scrubbed_env()) | (env or {}) | {
            "JARVIS_JOB_COMMAND": command, "JARVIS_JOB_EXIT": f"{base}.exit", "JARVIS_JOB_ID": str(job_id),
            "JARVIS_JOB_TITLE": title, "JARVIS_JOB_NOTIFY": "1" if self.notify else "",
        }
        argv = [*self.shell, _JOB_SCRIPT]
        # Popen, not asyncio subprocesses, which kill their child when the loop drops them
        with open(f"{base}.log", "wb") as out:
            proc = subprocess.Popen(walled(argv) if self.walls and walls else argv, cwd=cwd, env=env,
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
            end = self.log_file(job["id"]).read_bytes()[-tail:].decode(errors="replace")
        except OSError:
            end = ""
        return {"id": job["id"], "title": job["title"], "command": job["command"], "folder": job["folder"],
                "state": self._state(job), "minutes": round((now - job["started"]) / 60), "output_tail": end}

    def status(self, task: int | None = None) -> list[dict]:
        """Last day's jobs (optionally one task's), oldest first, each with its output tail."""
        now = time.time()
        return [self._view(job, now) for job in self._records()
                if now - job["started"] <= 86400 and (task is None or job.get("task") == task)]

    def sessions(self, task: int) -> list[tuple[int, float]]:
        """(pid, start time) per job of `task`, as in_sessions takes them."""
        return [(job["pid"], job["started"]) for job in self._records() if job.get("task") == task]

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

    def stop_all(self, task: int) -> list[int]:
        """Kill a task's jobs that are still running; returns their ids."""
        stopped = []
        for job in self._records():
            if job.get("task") == task and self._state(job) == RUNNING:
                _kill(job["pid"])
                stopped.append(job["id"])
        return stopped


# ── Checking the walls on this Mac ──────────────────────────────

def _checks() -> list[tuple[str, str, bool]]:  # (what, command, should succeed)
    return [
        ("reads JARVIS's .env", f"head -c1 {ROOT / '.env'}", False),
        ("reads secrets/", f"ls {JARVIS_SECRETS[0]}", False),
        ("lists ~/.ssh", "ls ~/.ssh", False),
        ("reads an SSH key file", "head -c1 ~/.ssh/id_ed25519 || head -c1 ~/.ssh/id_rsa || head -c1 ~/.ssh/id_ecdsa", False),
        ("reaches the SSH agent", "ssh-add -l >/dev/null 2>&1; test $? -ne 2", True),
        ("signs in to GitHub over SSH, through the agent",
         "ssh -o BatchMode=yes -o ConnectTimeout=15 -T git@github.com 2>&1 | grep -q 'successfully authenticated'", True),
        ("reads the keychain files", "ls ~/Library/Keychains", False),
        ("writes tasks.db", f"touch {DB_FILE}", False),
        ("writes in JARVIS's folder", f"touch {ROOT / '.host-check'} && rm {ROOT / '.host-check'}", False),
        ("reaches the runner's socket", f"curl -s --max-time 5 --unix-socket {RUNNER_SOCKET} http://runner/tasks", False),
        ("keeps JARVIS's keys out of its environment", 'test -z "$JARVIS_PASSCODE$LIVEKIT_API_SECRET"', True),
        ("reads other processes' environments (ps eww)", f"{PS} eww -p 1 >/dev/null", False),
        ("reads the voice session logs", f"ls {profiles.current().voice_logs}", True),
        ("reads tasks.db", f"sqlite3 -readonly {DB_FILE} 'select count(*) from tasks'", True),
        ("writes the task cache", f"touch {TASK_CACHE_DIR / '.host-check'} && rm {TASK_CACHE_DIR / '.host-check'}", True),
        ("reaches PyPI over HTTPS", "curl -sI --max-time 20 https://pypi.org/simple/ | head -1 | grep -q 200", True),
        ("finds the code-signing identities", "security find-identity -v -p codesigning | grep -Eq '^ *[1-9][0-9]* valid'", True),
    ]


async def check() -> bool:
    """Run each check under the profile; True if all behave as expected."""
    good = True
    for what, command, should in _checks():
        ran = await run(command, TASK_CACHE_DIR if TASK_CACHE_DIR.is_dir() else Path.home(), timeout=60)
        worked = ran.code == 0
        ok = worked == should
        good &= ok
        print(f"{'ok  ' if ok else 'FAIL'}  {what}: {'works' if worked else 'blocked'}"
              + ("" if ok else f" (expected {'works' if should else 'blocked'})\n      {ran.output.strip()[:300]}"))
    return good


if __name__ == "__main__":
    if sys.argv[1:] != ["check"]:
        raise SystemExit(__doc__)
    TASK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    raise SystemExit(0 if asyncio.run(check()) else 1)
