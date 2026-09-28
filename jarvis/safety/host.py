"""Commands run on the Mac itself, outside Claude Code's sandbox: background tasks' host_run and start_job
(jarvis.tasks.worker), and the voice shell's long jobs (jarvis.safety.shell).

A task's host command runs under `sandbox-exec` with `profile()`, a Seatbelt profile that allows by
default and closes only what must stay closed, whatever the command or a script it wrote tries:

  - reading secrets: JARVIS's secrets/ (all but the voice session logs) and .env, the home folder's
    logins and keys (~/.ssh but its known_hosts and config, ~/.aws, ~/Library/Keychains, ...), and
    .env files, keys and credentials anywhere in the home folder (the lists in jarvis.safety.commands)
  - writing JARVIS's own files: everything in its folder except the task worktrees, the task cache
    and task records (data/tasks/), and git's objects; so tasks.db (the project list and the
    to-do list among it), the push files, memory, secrets/ and JARVIS's code stay as they are
  - writing logins, shell start-up files and launch agents in the home folder
  - connecting to the runner's socket (data/runner.sock)
  - running the setuid /bin/ps, which could read other processes' environments, MCP servers' tokens among them

The one exception is a package install that sandboxes its own steps (commands.own_sandbox: brew install, upgrade,
uninstall), which macOS won't start inside this profile ("sandbox_apply: Operation not permitted"). Such a line is
risky, so a task runs it only with the user's yes, and nothing else may ride along in it; then it runs with
`walls=False`, as the voice shell's jobs and scheduled commands do, still without JARVIS's keys (`task_env()`).

The network is open, and writes elsewhere are allowed: Xcode's DerivedData, devicectl and package
caches need them. Nor does the command get JARVIS's keys: `task_env()` leaves out everything in .env,
and anything named like a token, secret or password. It keeps the SSH agent (SSH_AUTH_SOCK), which
signs without showing a key, so an approved git push or ssh works; both are risky commands, so each
one still needs the user's yes.

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
PS = "/bin/ps"  # setuid root, so it can show any process's environment
SHELL = ("/bin/zsh", "-lc")  # a login shell, so Homebrew's and the user's PATH apply
TASK_DATA_DIR = DATA_DIR / "tasks"  # research tasks' folders, and task jobs
TASK_JOBS_DIR = TASK_DATA_DIR / "jobs"
MAX_OUTPUT = 30_000  # characters of a host_run's output the task sees: the start and the end
RUN_TIMEOUT = 600  # the longest a host_run may take; longer work is a job

# In the home folder, secret wherever they are: commands.SECRET_DIRS and SECRET_FILES, within one path component
_SECRET_DIR_NAMES = "(" + "|".join(re.escape(d) for d in sorted(SECRET_DIRS)) + ")"
_SECRET_FILE_NAMES = "(" + secret_file_names("[^/]", pem=False) + ")"
# .pem files are keys too, except the certificate bundles TLS needs, which packages carry
_PEM_KEPT = r"/(site-packages|dist-packages|node_modules|Cellar)/|/(ca|cert|certs|cacert|ca-bundle|ca-certificates)\.pem$"
# Home-folder files a command could use to run something later, outside any wall
_STARTUP_FILES = [".zshrc", ".zprofile", ".zshenv", ".zlogin", ".bashrc", ".bash_profile", ".profile", ".gitconfig"]
_STARTUP_DIRS = ["Library/LaunchAgents", ".config/git"]
# Names of variables that carry a credential, whatever set them
SENSITIVE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSCODE|API_?KEY|PRIVATE_KEY|MASTER_KEY|CREDENTIAL|AUTH",
                        re.IGNORECASE)
# Kept all the same: the SSH agent signs with the user's keys without ever showing them, so an approved
# git push or ssh works while the key files stay unreadable
_KEPT = {"SSH_AUTH_SOCK"}
# In ~/.ssh, what ssh needs to connect through the agent: which hosts are known, and the host settings.
# No key is in them; the key files, and everything else there, stay unreadable
_SSH_READABLE = ["known_hosts", "known_hosts2", "config"]


# ── The walls ───────────────────────────────────────────────────


def _real(path: Path | str) -> str:
    """A path as Seatbelt sees it: absolute, with symlinks (like /tmp) resolved."""
    return os.path.realpath(os.path.expanduser(str(path)))


def _str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rx(text: str) -> str:
    """Escapes a path for a Seatbelt regex. A quote can't be escaped in #"...", so it matches any character."""
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", text).replace('"', ".")


def _any(*filters: str) -> str:
    return f"(require-any {' '.join(filters)})"


def profile(home: Path | str | None = None) -> str:
    """The Seatbelt profile a task's host commands run under."""
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
        # The setuid ps is the one way to read another process's environment, where an MCP server's token is.
        # Seatbelt won't start a setuid program anyway; this says so, whatever a later macOS does.
        f"(deny process-exec (literal {_str(PS)}))",
    ])


def env_keys() -> set[str]:
    """The names of JARVIS's own settings and keys: everything in .env, and the variables of the keys it moved from
    there into the vault, which LiveKit's worker puts back into its process's environment."""
    return set(dotenv_values(ROOT / ".env")) | {*credentials.VARS, logins.VAR}


def scrubbed_env() -> dict[str, str]:
    """The runner's environment without JARVIS's own keys (everything in .env)."""
    secret = env_keys()
    return {k: v for k, v in os.environ.items() if k not in secret}


def task_env() -> dict[str, str]:
    """What a task's host command gets: scrubbed_env() without anything else that looks like a credential,
    except the SSH agent's socket."""
    return {k: v for k, v in scrubbed_env().items() if k in _KEPT or not SENSITIVE.search(k)}


def walled(argv: list[str], home: Path | str | None = None) -> list[str]:
    """argv, run inside the host profile. Raises RuntimeError where sandbox-exec is missing, rather than
    running the command without its walls."""
    if not Path(SANDBOX_EXEC).exists():
        raise RuntimeError("sandbox-exec isn't on this Mac, so host commands can't run safely")
    return [SANDBOX_EXEC, "-p", profile(home), *argv]


# ── Commands ────────────────────────────────────────────────────


@dataclass
class Ran:
    code: int | None  # None: stopped at the timeout
    output: str  # stdout and stderr together, the middle cut out past MAX_OUTPUT
    seconds: float

    def text(self) -> str:
        how = f"exit code {self.code}" if self.code is not None else "stopped: it ran past its time limit"
        return f"[{how}, {self.seconds:.0f} s]\n{self.output}".rstrip()


class _Output:
    """A command's output as it comes: the start, and a rolling end, so a flood of it can't fill memory."""

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
    """Sends `sig` to the process group led by `pid` (or to `pid` alone), if it's still there to get it."""
    try:
        (os.killpg if group else os.kill)(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _kill(pid: int) -> None:
    _send(pid, signal.SIGKILL)


async def run(command: str, cwd: Path, timeout: float = RUN_TIMEOUT, walls: bool = True,
              shell: tuple[str, ...] = SHELL, env: dict[str, str] | None = None,
              started: Callable[[int], None] | None = None) -> Ran:
    """Run a command on the Mac, in its own session, and wait for it and whatever it started that keeps its
    output open (at most `timeout` seconds, after which they're killed). `env` adds to its environment, and
    `started` hears the session's id (the shell's pid) as it starts."""
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
        try:  # something it started in a session of its own may still hold the output open
            await asyncio.wait_for(reader, 5)
        except TimeoutError:
            pass
        code = None
    except asyncio.CancelledError:  # the task stopped: so does its command
        _kill(proc.pid)
        reader.cancel()
        raise
    return Ran(code, output.text(), time.monotonic() - start)


# ── Which processes are whose ───────────────────────────────────
# Every host command runs in a session of its own, and whatever it starts stays in that session unless it leaves
# on purpose. A session is known by its leader's pid and when that started, since a pid can come back as another
# process's once its own has gone.


class _BsdInfo(ctypes.Structure):
    """macOS's struct proc_bsdinfo (sys/proc_info.h), as proc_pidinfo fills it in: ps without the setuid ps."""
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
    """When a process started, in seconds since the epoch, or None if it isn't running."""
    return _start(info) if (info := _info(pid)) else None


def _unchanged(pid: int, started: float) -> bool:
    """Whether `pid` is still the process that started at `started`, or isn't running at all: never another one that
    has been given its pid since."""
    now = started_at(pid)
    return now is None or abs(now - started) <= 2


def in_sessions(pid: int, sessions: Iterable[tuple[int, float]]) -> bool:
    """Whether a running process is in one of these sessions, each (its leader's pid, when it started)."""
    if pid <= 1:
        return False
    try:
        sid = os.getsid(pid)
    except OSError:
        return False
    # The leader may have ended, leaving the rest; if a process has its pid now, it must be the same one
    return any(sid == leader and _unchanged(leader, at) for leader, at in sessions)


_SZOMB = 5  # a process that has ended but not yet been reaped (sys/proc.h)


def _session(leader: int) -> Iterator[tuple[int, _BsdInfo]]:
    """Each process in a session, whatever process group it's in, with what proc_pidinfo says of it."""
    for pid in _pids():
        try:
            if os.getsid(pid) != leader:
                continue
        except OSError:
            continue
        if info := _info(pid):
            yield pid, info


def session_members(leader: int) -> list[int]:
    """The live processes in a session (its leader among them, while it runs), whatever process group they're in."""
    return [pid for pid, info in _session(leader) if info.status != _SZOMB]


def lingering(leader: int) -> list[tuple[int, str]]:
    """The processes still running in a session whose leader has ended: (pid, program name) for each, oldest first."""
    found = sorted((_start(info), pid, (info.name or info.comm).decode(errors="replace"))
                   for pid, info in _session(leader) if pid != leader)
    return [(pid, name) for _, pid, name in found]


# ── Long jobs ───────────────────────────────────────────────────

# Runs the command, records its exit status, and puts up a Mac notification, even when nobody's listening any more.
# Not `status`: that's read-only in zsh, and assigning it would end the script before the exit file is written.
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
STOP_GRACE = 5.0  # seconds a stopped job has to end after SIGTERM, before whatever is left gets SIGKILL


def outcome(code: int) -> str:
    """How a job that ended with exit status `code` went, for saying: 'is done', or 'failed, with exit code 2'."""
    return "is done" if code == 0 else f"failed, with exit code {code}"


def alive(pid: int) -> bool:
    """Whether a process with this pid is running (one of another user's counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


class Jobs:
    """Long commands, started in their own session apart from whoever started them: the CLI restarting,
    a voice session ending or the runner restarting leaves them running. Each has a record (N.json),
    its output (N.log) and, once it's done, its exit status (N.exit) in `folder`, or N.stopped if `stop`
    ended it. A task's jobs carry its id (`task`) and run inside the host profile (`walls`)."""

    def __init__(self, folder: Path, shell: tuple[str, ...] = SHELL, notify: bool = True, walls: bool = False) -> None:
        self.folder, self.shell, self.notify, self.walls = folder, shell, notify, walls
        self.poll = 2.0  # seconds between checks on a running job
        self.grace = STOP_GRACE
        self._watchers: set[asyncio.Task] = set()
        self._procs: dict[int, subprocess.Popen] = {}  # the jobs this process started, so `stop` can reap them

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
        """Start a job; returns its record. Raises ValueError if `cwd` isn't a folder, or `job_id`, the number it's
        given rather than the next free one, is taken. `env` adds to its environment. A job that `report`s ends its
        output with a line saying what it made, which is said as it ends. `extra` goes in its record as it is.
        `walls` False runs this one outside the host profile, for an approved install that sandboxes itself
        (commands.own_sandbox), still with the environment these jobs get."""
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
        # Popen rather than asyncio's subprocesses, which kill their child when the event loop lets go of them
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
        if self._stopped(job):  # whoever stopped it has said so
            log.warning("job %s was stopped (%s)", job["id"], code)
            return
        log.warning("job %s ended with %s", job["id"], code)
        self.ended(job, code)

    def ended(self, job: dict, code: int) -> None:
        """Called as a job this process started ends on its own, not by `stop`."""

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
        """The jobs of the last day (only the task's, if one is given), newest last, each with the end of its output."""
        now = time.time()
        return [self._view(job, now) for job in self._records()
                if now - job["started"] <= 86400 and (task is None or job.get("task") == task)]

    def sessions(self, task: int) -> list[tuple[int, float]]:
        """The sessions a task's jobs run in, as in_sessions takes them: each job's pid, and when it started."""
        return [(job["pid"], job["started"]) for job in self._records() if job.get("task") == task]

    def get(self, job_id: int, task: int | None = None) -> dict | None:
        return next((j for j in self._records() if j["id"] == job_id and (task is None or j.get("task") == task)), None)

    def _job(self, job_id: int, task: int | None) -> dict:
        """`get`, but ValueError for a job that isn't there."""
        if (job := self.get(job_id, task)) is None:
            raise ValueError(f"There's no job {job_id}" + (f" of task {task}." if task is not None else "."))
        return job

    def log_file(self, job_id: int) -> Path:
        return self.folder / f"{job_id}.log"

    async def wait(self, job_id: int, timeout: float, task: int | None = None, tail: int = 3000) -> dict:
        """Wait for a job to end, at most `timeout` seconds; returns how it stands, with the end of its output."""
        job = self._job(job_id, task)
        deadline = time.monotonic() + timeout
        while self._state(job) == RUNNING and time.monotonic() < deadline:
            await asyncio.sleep(min(self.poll, max(0.0, deadline - time.monotonic())))
        return self._view(job, time.time(), tail)

    # A job is stopped by its record alone: its session's leader is the pid the record holds, checked against when the
    # job started, so a pid since reused by some other process is never signalled.

    def _ours(self, job: dict) -> bool:
        """Whether the job's pid is still its own: running since the job started, or not running at all (so what's
        left of its session is the job's)."""
        return _unchanged(job["pid"], job["started"])

    def _left(self, job: dict) -> list[int]:
        """What's still running of a job: the live processes in its session."""
        if (proc := self._procs.get(job["id"])) is not None:
            proc.poll()  # reaps the leader once it ends, so it doesn't linger as a zombie
        return session_members(job["pid"]) if self._ours(job) else []

    def _signal(self, job: dict, sig: int) -> None:
        """Sends `sig` to the job's whole process group, and to anything of its session that left the group."""
        if not self._ours(job):
            return
        _send(job["pid"], sig)
        for pid in session_members(job["pid"]):
            _send(pid, sig, group=False)

    async def stop(self, job_id: int, task: int | None = None) -> dict:
        """Stop a running job: SIGTERM to all of it, then SIGKILL to whatever is left after `grace` seconds. It's
        marked stopped (N.stopped), so its watcher doesn't report it as failed. Returns how it stands now. Raises
        ValueError for a job that isn't there or has already ended."""
        job = self._job(job_id, task)
        state = self._state(job)
        # Not "running" may still leave something behind if its leader alone was killed: that's stopped too
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
        """Waits at most `timeout` seconds for all of a job to end; whether it has."""
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

def _checks() -> list[tuple[str, str, bool]]:  # (what, command, whether it should work)
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
    """Runs each check under the profile; True if every one came out as it should."""
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
