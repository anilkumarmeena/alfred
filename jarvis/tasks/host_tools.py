"""Task tools that run on the host (macOS or Linux) outside the task sandbox (MCP server "host").

  host_run     one command, up to MAX_MINUTES
  start_job    long command that runs detached; follow with job_status / job_wait
They run in the runner inside the host walls (Seatbelt on macOS, bubblewrap on Linux), without JARVIS's keys.
host_run and start_job pass `_host_gate` first: refused, allowed, allowed by a prior yes or rule, or sent to the user
for approval. The gate reads the scripts a command runs; the tool runs it only if they're unchanged since
(`TaskJobs.vet`), and a yes covers them only as they were when asked.
TaskJobs tracks each task's jobs and host_run sessions so processes they leave behind count as the task's own.
"""

import asyncio
import functools
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, create_sdk_mcp_server, tool as sdk_tool

from jarvis.brain.sdk import READS
from jarvis.core import platform, profile
from jarvis.core.config import TASK_CACHE_DIR
from jarvis.safety import commands, host, remembered

if TYPE_CHECKING:
    from jarvis.tasks.reporting import Reporter

log = logging.getLogger("jarvis.tasks.worker")

HOST_RUN, START_JOB, JOB_STATUS, JOB_WAIT = (f"mcp__host__{n}" for n in ("host_run", "start_job", "job_status", "job_wait"))
HOST_COMMANDS = {HOST_RUN: "host_run", START_JOB: "start_job"}  # gated tools; the job tools only read
MAX_MINUTES = 10  # per-call limit for host_run and job_wait


class TaskJobs(host.Jobs):
    """All tasks' long jobs (data/tasks/jobs/) plus each task's host_run session leaders, newest last.

    Processes in those sessions count as the task's own. The runner holds one (Runner.host_jobs) and sets `on_end`.
    """

    def __init__(self, folder: Path | None = None, **options) -> None:
        super().__init__(folder or host.TASK_JOBS_DIR, **{"notify": False, "walls": True} | options)
        self.on_end: Callable[[dict, int], None] | None = None
        self.ran: dict[int, list[tuple[int, float]]] = {}
        # host_run commands in flight per task. A task cut off mid-command isn't auto-resumed on restart, since the
        # command's effect is unknown. Long jobs survive restarts and aren't tracked here.
        self.on_mac: dict[int, list[str]] = {}
        self.vetted: dict[tuple[int, str, str], dict[str, str]] = {}  # (task, command, folder) -> scripts as read

    def vet(self, task_id: int, command: str, folder: Path) -> None:
        """Note the scripts a command the gate let through runs, as it read them."""
        self.vetted[(task_id, command, str(folder))] = commands.seen(command, folder).files
        while len(self.vetted) > KEEP_VETTED:
            del self.vetted[next(iter(self.vetted))]

    def changed(self, task_id: int, command: str, folder: Path) -> bool:
        """Whether a script the command runs changed since the gate read it. The check and the run are a moment apart:
        a change in that moment still slips through."""
        files = self.vetted.get((task_id, command, str(folder)))
        return files is not None and files != commands.seen(command, folder).files

    def ended(self, job: dict, code: int) -> None:
        if self.on_end:
            self.on_end(job, code)

    def started(self, task_id: int, pid: int) -> None:
        """Record a host_run command's session leader `pid`."""
        kept = self.ran.setdefault(task_id, [])
        kept.append((pid, time.time()))
        del kept[:-KEEP_SESSIONS]

    def task_sessions(self, task_id: int) -> list[tuple[int, float]]:
        """All sessions the task's host_run commands and jobs ran in."""
        return [*self.ran.get(task_id, []), *self.sessions(task_id)]

    @contextmanager
    def running(self, task_id: int, command: str) -> Iterator[None]:
        """Track `command` in `on_mac` while it runs."""
        self.on_mac.setdefault(task_id, []).append(command)
        try:
            yield
        finally:
            self.on_mac[task_id].remove(command)
            if not self.on_mac[task_id]:
                del self.on_mac[task_id]


KEEP_SESSIONS = 200  # host_run sessions remembered per task
KEEP_VETTED = 200  # gate decisions kept for the tools to re-check

# What host commands are for, beyond the task sandbox, as the tool descriptions say it
MACHINE = "this computer"
_FOR = ("signed Xcode builds, devicectl, the keychain's signing identities, Homebrew" if platform.MAC else
        "Docker, a phone or other device over USB (adb), emulators, tools that write outside your folder")
_SHELL = Path(host.SHELL[0]).name
_EXAMPLE = "the iPhone release build" if platform.MAC else "the release build"


def unwalled(command: str) -> bool:
    """Whether an approved command runs outside the host walls: a bare Homebrew/MacPorts install, on macOS only.

    macOS won't start Homebrew's own sandbox inside the host profile; on Linux nothing needs to leave the walls.
    """
    return platform.MAC and commands.own_sandbox(command)


def task_temp(task_id: int) -> Path:
    """Per-task temp folder in the cache dir; sandboxed commands may write there too."""
    return TASK_CACHE_DIR / "tmp" / f"task-{task_id}"


T = TypeVar("T")


async def answer(value: T | Awaitable[T]) -> T:
    """A Reporter's answer, whether given now (the runner) or awaited (over the link, on another machine)."""
    return await value if inspect.isawaitable(value) else value  # type: ignore[return-value]


def few(names: list[str]) -> str:
    """First ten names, plus a count of the rest."""
    return ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")


def _resolve(path: str, cwd: Path) -> Path:
    p = Path(path).expanduser()
    return (p if p.is_absolute() else cwd / p).resolve()


def _inside(path: str, cwd: Path, roots: list[Path]) -> bool:
    p = _resolve(path, cwd)
    return any(p.is_relative_to(r.resolve()) for r in roots)


def _still_running(leader: int) -> str:
    """Note listing processes a host_run command left running, or ""."""
    if not (left := host.lingering(leader)):
        return ""
    listed = few([f"{pid} ({name})" for pid, name in left])
    return (f"\n[still running from this command: {listed}. They're yours: stop them with kill and these pids once "
            "you're done, which needs no go-ahead]")


def call_minutes(args: dict) -> float:
    """Clamped `minutes` for a host_run or job_wait call."""
    try:
        return min(max(float(args.get("minutes") or MAX_MINUTES), 0.1), MAX_MINUTES)
    except (TypeError, ValueError):
        return MAX_MINUTES


def _host_server(task: dict, cwd: Path, jobs: TaskJobs):
    return create_sdk_mcp_server("host", tools=_host_tools(task, cwd, jobs))


def _host_tools(task: dict, cwd: Path, jobs: TaskJobs) -> list:
    """Build the host tools. host_run/start_job re-check commands.refused after the gate as a second wall.

    Commands that sandbox themselves (`unwalled`) run outside the host walls.
    """
    task_id, in_place = task["id"], bool(task["in_place"])

    def folder(args: dict) -> Path:
        return _resolve(args.get("folder") or str(cwd), cwd)

    def text(body: str, error: bool = False) -> dict:
        return {"content": [{"type": "text", "text": body}]} | ({"is_error": True} if error else {})

    def changed(args: dict) -> str:
        if jobs.changed(task_id, args.get("command", "").strip(), folder(args)):
            return ("a script it runs changed after it was checked. Run the identical command again to have it checked "
                    "as it is now")
        return ""

    def temp_env() -> dict[str, str]:
        temp = task_temp(task_id)
        temp.mkdir(parents=True, exist_ok=True)
        return {"TMPDIR": f"{temp}/"}

    @sdk_tool(
        "host_run",
        f"Run one shell command on {MACHINE} itself, outside the sandbox, and get its output (the start and end, if "
        f"it's long) and exit code. For what the sandbox blocks: {_FOR}. Secrets stay unreadable. At most 10 minutes; "
        "use start_job for longer.",
        {"type": "object", "properties": {
            "command": {"type": "string", "description": f"The shell command, as you'd give it to Bash ({_SHELL})."},
            "folder": {"type": "string", "description": "Where to run it: your folder (the default) or a folder inside it."},
            "minutes": {"type": "number", "description": "Time limit in minutes, up to 10 (the default)."},
        }, "required": ["command"]},
    )
    async def host_run(args: dict) -> dict:
        command = args.get("command", "").strip()
        if why := commands.refused(command, in_place, folder(args)) or changed(args):
            return text(f"Not run: {why}.", error=True)
        walls = not unwalled(command)
        log.warning("task %s runs on %s%s, in %s: %s", task_id, MACHINE, "" if walls else " outside the host walls",
                    folder(args), command)
        leaders: list[int] = []

        def started(pid: int) -> None:
            leaders.append(pid)
            jobs.started(task_id, pid)

        try:
            with jobs.running(task_id, command):
                ran = await host.run(command, folder(args), call_minutes(args) * 60, walls=walls, env=temp_env(),
                                     started=started)
        except (OSError, RuntimeError) as e:
            return text(f"Couldn't run it: {e}", error=True)
        left = await asyncio.to_thread(_still_running, leaders[0]) if leaders else ""
        return text(ran.text() + left, error=ran.code != 0)

    @sdk_tool(
        "start_job",
        f"Start a long shell command (a full build, a big download, a long test run) on {MACHINE}, outside the "
        "sandbox, apart from this session: it keeps running on its own. Follow it with job_wait or job_status, and read "
        "its log.",
        {"type": "object", "properties": {
            "command": {"type": "string", "description": f"The shell command, as you'd give it to Bash ({_SHELL})."},
            "title": {"type": "string", "description": f"A few words for it, e.g. '{_EXAMPLE}'."},
            "folder": {"type": "string", "description": "Where to run it: your folder (the default) or a folder inside it."},
        }, "required": ["command", "title"]},
    )
    async def start_job(args: dict) -> dict:
        command = args.get("command", "").strip()
        if why := commands.refused(command, in_place, folder(args)) or changed(args):
            return text(f"Not started: {why}.", error=True)
        try:
            job = jobs.launch(command, args.get("title") or command[:40], folder(args), task=task_id, env=temp_env(),
                              walls=not unwalled(command))
        except (ValueError, OSError, RuntimeError) as e:
            return text(f"Not started: {e}", error=True)
        return text(f"Started job {job['id']}, {job['title']}. It runs on its own; its whole output goes to "
                    f"{jobs.log_file(job['id'])}. Wait for it with job_wait, or check on it with job_status.")

    @sdk_tool("job_status", "Your jobs from the last day: each one's state and the end of its output.",
              {"type": "object", "properties": {}}, READS)
    async def job_status(args: dict) -> dict:
        listed = jobs.status(task=task_id)
        return text(json.dumps(listed) if listed else "No jobs in the last day.")

    @sdk_tool(
        "job_wait",
        "Wait until one of your jobs ends, or at most 10 minutes; returns its state and the end of its output. "
        "Call it again to keep waiting.",
        {"type": "object", "properties": {
            "id": {"type": "integer"},
            "minutes": {"type": "number", "description": "How long to wait at most, up to 10 (the default)."},
        }, "required": ["id"]},
    )
    async def job_wait(args: dict) -> dict:
        try:
            view = await jobs.wait(int(args["id"]), call_minutes(args) * 60, task=task_id)
        except (ValueError, TypeError, KeyError) as e:
            return text(str(e), error=True)
        return text(json.dumps(view))

    return [host_run, start_job, job_status, job_wait]


def _own(task: dict, cwd: Path, folder: Path, jobs: TaskJobs) -> commands.Own:
    """Task-owned scope for a command in `folder`: its folder, its temp folder, processes it started.

    A folder replaced by a symlink no longer counts as owned.
    """
    task_id = task["id"]
    temp = task_temp(task_id)
    roots = [cwd] if not cwd.is_symlink() else []
    if not temp.is_symlink() and temp.resolve() == TASK_CACHE_DIR.resolve() / "tmp" / temp.name:
        roots.append(temp)

    @functools.cache
    def sessions() -> list[tuple[int, float]]:  # lazy, once per command
        return jobs.task_sessions(task_id)

    def process(pid: int) -> bool:
        return host.in_sessions(pid, sessions())

    return commands.Own(folder, tuple(roots), process)


def approval_item(tool: str, args: dict, cwd: Path, why: str, own: commands.Own | None = None) -> dict:
    """Approval request for a host command: exact command, tool, folder, risk reason, rememberable pieces and the
    fingerprints of the scripts it runs (a yes covers them as they are now)."""
    command, folder = args.get("command", "").strip(), _resolve(args.get("folder") or str(cwd), cwd)
    item = {"tool": tool, "command": command, "folder": str(folder), "why": why,
            "remember": remembered.pieces(command, own, folder)}
    return item | ({"scripts": files} if (files := commands.seen(command, folder).files) else {})


def _host_gate(task: dict, cwd: Path, reporter: "Reporter"):
    """Permission gate for host_run/start_job: deny, allow, allow as agreed, or request approval.

    Deleting the task's own files or killing its own processes needs no approval (commands.Own).
    """
    task_id, in_place = task["id"], bool(task["in_place"])
    roots = [cwd, TASK_CACHE_DIR]

    async def gate(name: str, args: dict, ctx) -> PermissionResultAllow | PermissionResultDeny:
        tool = HOST_COMMANDS[name]
        command = args.get("command", "").strip()
        if not command:
            return PermissionResultDeny(message="Give the command to run.")
        folder = _resolve(args.get("folder") or str(cwd), cwd)
        if why := commands.refused(command, in_place, folder):
            log.warning("task %s refused on %s (%s): %s", task_id, MACHINE, why, command)
            return PermissionResultDeny(message=f"Not run: {why}. If the work needs it, end with the exact command "
                                                f"for {profile.name()} to run themselves.")
        if not _inside(args.get("folder") or str(cwd), cwd, roots):
            return PermissionResultDeny(message=f"{tool} runs in {cwd} or a folder inside it (or {TASK_CACHE_DIR}).")

        def allow() -> PermissionResultAllow:
            reporter.host_jobs.vet(task_id, command, folder)
            return PermissionResultAllow()

        if not commands.risky(command, cwd=folder):
            return allow()
        own = _own(task, cwd, folder, reporter.host_jobs)
        if not (why := commands.describe(command, own)):
            log.warning("task %s runs on %s, deleting or stopping only its own: %s", task_id, MACHINE, command)
            return allow()
        item = approval_item(tool, args, cwd, why, own)
        if await answer(reporter.cleared(task_id, item)):  # a prior yes to exactly this, or a remembered rule
            log.warning("task %s runs on %s, as agreed (%s): %s", task_id, MACHINE, item["why"], command)
            return allow()
        if getattr(ctx, "agent_id", None):
            return PermissionResultDeny(message=f"Not run: it {item['why']}, so it needs {profile.name()}'s go-ahead, and only "
                                                "the task's own agent can ask for that. Report the exact command back instead.")
        approval = await answer(reporter.request_approval(task_id, item))
        return PermissionResultDeny(message=(
            f"Not run: it {item['why']}, so it needs {profile.name()}'s go-ahead. JARVIS has asked them (request "
            f"{approval['id']}). Carry on with anything that doesn't depend on it; if nothing's left, end your turn with "
            f"one short line. When they answer you'll get a message; if it's a yes, run the identical command with {tool}."))

    return gate
