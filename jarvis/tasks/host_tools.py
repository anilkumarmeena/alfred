"""A task's tools on the Mac itself (jarvis.tasks.worker): host_run for a command of up to 10 minutes, start_job,
job_status and job_wait for longer ones. They run in the runner, under jarvis.safety.host's Seatbelt profile and without
JARVIS's keys in their environment, and each host_run and start_job call meets `_host_gate` first: refused, run, run
once as agreed, or put to the user (Reporter.request_approval).

TaskJobs keeps every task's long jobs, and the sessions its host_run commands ran in, so what they left running counts
as the task's own (`_own`); task_temp is each task's own temp folder.
"""

import asyncio
import functools
import json
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, create_sdk_mcp_server, tool as sdk_tool

from jarvis.brain.sdk import READS
from jarvis.core import profile
from jarvis.core.config import TASK_CACHE_DIR
from jarvis.safety import commands, host, remembered

if TYPE_CHECKING:
    from jarvis.tasks.reporting import Reporter

log = logging.getLogger("jarvis.tasks.worker")

HOST_RUN, START_JOB, JOB_STATUS, JOB_WAIT = (f"mcp__host__{n}" for n in ("host_run", "start_job", "job_status", "job_wait"))
HOST_COMMANDS = {HOST_RUN: "host_run", START_JOB: "start_job"}  # what meets _host_gate; the job tools only read
MAX_MINUTES = 10  # the longest host_run runs, and job_wait waits, per call


class TaskJobs(host.Jobs):
    """Every task's long jobs, in data/tasks/jobs/, each marked with its task and run inside the host profile, and the
    sessions each task's host_run commands ran in (host.in_sessions), newest last: what they left running is the
    task's own. The runner keeps one (Runner.host_jobs, which tasks reach as their Reporter's) and sets `on_end` to
    hear when a job ends."""

    def __init__(self, folder: Path | None = None, **options) -> None:
        super().__init__(folder or host.TASK_JOBS_DIR, **{"notify": False, "walls": True} | options)
        self.on_end: Callable[[dict, int], None] | None = None
        self.ran: dict[int, list[tuple[int, float]]] = {}
        # The host_run commands each task is running on the Mac right now: one a shutdown cuts off isn't resumed on
        # its own, as what the command did is unknown (Runner.shutdown). A long job runs on through a restart, so
        # isn't one.
        self.on_mac: dict[int, list[str]] = {}

    def ended(self, job: dict, code: int) -> None:
        if self.on_end:
            self.on_end(job, code)

    def started(self, task_id: int, pid: int) -> None:
        """A task's host_run command started, leading session `pid`."""
        kept = self.ran.setdefault(task_id, [])
        kept.append((pid, time.time()))
        del kept[:-KEEP_SESSIONS]

    def task_sessions(self, task_id: int) -> list[tuple[int, float]]:
        """Every session the task's host_run commands and jobs ran in."""
        return [*self.ran.get(task_id, []), *self.sessions(task_id)]

    @contextmanager
    def running(self, task_id: int, command: str) -> Iterator[None]:
        """While the task runs `command` on the Mac: kept in `on_mac`."""
        self.on_mac.setdefault(task_id, []).append(command)
        try:
            yield
        finally:
            self.on_mac[task_id].remove(command)
            if not self.on_mac[task_id]:
                del self.on_mac[task_id]


KEEP_SESSIONS = 200  # host_run sessions remembered per task


def task_temp(task_id: int) -> Path:
    """A task's own temp folder, in the cache folder, where its sandboxed commands may write as well."""
    return TASK_CACHE_DIR / "tmp" / f"task-{task_id}"


def few(names: list[str]) -> str:
    """The first ten, and how many more."""
    return ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")


def _resolve(path: str, cwd: Path) -> Path:
    p = Path(path).expanduser()
    return (p if p.is_absolute() else cwd / p).resolve()


def _inside(path: str, cwd: Path, roots: list[Path]) -> bool:
    p = _resolve(path, cwd)
    return any(p.is_relative_to(r.resolve()) for r in roots)


def _still_running(leader: int) -> str:
    """What a host_run command left running, for the task to stop when it's done with it."""
    if not (left := host.lingering(leader)):
        return ""
    listed = few([f"{pid} ({name})" for pid, name in left])
    return (f"\n[still running from this command: {listed}. They're yours: stop them with kill and these pids once "
            "you're done, which needs no go-ahead]")


def call_minutes(args: dict) -> float:
    """How long a host_run or job_wait call may take, in minutes, from its `minutes`."""
    try:
        return min(max(float(args.get("minutes") or MAX_MINUTES), 0.1), MAX_MINUTES)
    except (TypeError, ValueError):
        return MAX_MINUTES


def _host_server(task: dict, cwd: Path, jobs: TaskJobs):
    return create_sdk_mcp_server("host", tools=_host_tools(task, cwd, jobs))


def _host_tools(task: dict, cwd: Path, jobs: TaskJobs) -> list:
    """host_run and the job tools. host_run and start_job only run once `_host_gate` has let them through,
    and they check commands.refused again themselves, as a second wall. An approved install that sandboxes itself
    (commands.own_sandbox) runs outside the host profile."""
    task_id, in_place = task["id"], bool(task["in_place"])

    def folder(args: dict) -> Path:
        return _resolve(args.get("folder") or str(cwd), cwd)

    def text(body: str, error: bool = False) -> dict:
        return {"content": [{"type": "text", "text": body}]} | ({"is_error": True} if error else {})

    def temp_env() -> dict[str, str]:
        temp = task_temp(task_id)
        temp.mkdir(parents=True, exist_ok=True)
        return {"TMPDIR": f"{temp}/"}

    @sdk_tool(
        "host_run",
        "Run one shell command on the Mac itself, outside the sandbox, and get its output (the start and end, if it's "
        "long) and exit code. For what the sandbox blocks: signed Xcode builds, devicectl, the keychain's signing "
        "identities, Homebrew. Secrets stay unreadable. At most 10 minutes; use start_job for longer.",
        {"type": "object", "properties": {
            "command": {"type": "string", "description": "The shell command, as you'd give it to Bash (zsh)."},
            "folder": {"type": "string", "description": "Where to run it: your folder (the default) or a folder inside it."},
            "minutes": {"type": "number", "description": "Time limit in minutes, up to 10 (the default)."},
        }, "required": ["command"]},
    )
    async def host_run(args: dict) -> dict:
        command = args.get("command", "").strip()
        if why := commands.refused(command, in_place):
            return text(f"Not run: {why}.", error=True)
        walls = not commands.own_sandbox(command)
        log.warning("task %s runs on the Mac%s, in %s: %s", task_id, "" if walls else " outside the host profile", folder(args), command)
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
        "Start a long shell command (a full build, a big download, a long test run) on the Mac, outside the sandbox, "
        "apart from this session: it keeps running on its own. Follow it with job_wait or job_status, and read its log.",
        {"type": "object", "properties": {
            "command": {"type": "string", "description": "The shell command, as you'd give it to Bash (zsh)."},
            "title": {"type": "string", "description": "A few words for it, e.g. 'the iPhone release build'."},
            "folder": {"type": "string", "description": "Where to run it: your folder (the default) or a folder inside it."},
        }, "required": ["command", "title"]},
    )
    async def start_job(args: dict) -> dict:
        command = args.get("command", "").strip()
        if why := commands.refused(command, in_place):
            return text(f"Not started: {why}.", error=True)
        try:
            job = jobs.launch(command, args.get("title") or command[:40], folder(args), task=task_id, env=temp_env(),
                              walls=not commands.own_sandbox(command))
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
    """What's the task's own for a host command run in `folder`: its folder, its temp folder, what it started.
    Neither folder counts once it's been swapped for a link (say by an earlier command) to somewhere else."""
    task_id = task["id"]
    temp = task_temp(task_id)
    roots = [cwd] if not cwd.is_symlink() else []
    if not temp.is_symlink() and temp.resolve() == TASK_CACHE_DIR.resolve() / "tmp" / temp.name:
        roots.append(temp)

    @functools.cache
    def sessions() -> list[tuple[int, float]]:  # read once per command, when a pid needs checking
        return jobs.task_sessions(task_id)

    def process(pid: int) -> bool:
        return host.in_sessions(pid, sessions())

    return commands.Own(folder, tuple(roots), process)


def approval_item(tool: str, args: dict, cwd: Path, why: str, own: commands.Own | None = None) -> dict:
    """A host command as an approval covers it: the exact text, the tool and the folder (and why it's risky,
    from commands.describe), with what it offers to be remembered (remembered.pieces), worked out here, with what's
    the task's own, and kept with the request."""
    command = args.get("command", "").strip()
    return {"tool": tool, "command": command, "folder": str(_resolve(args.get("folder") or str(cwd), cwd)), "why": why,
            "remember": remembered.pieces(command, own)}


def _host_gate(task: dict, cwd: Path, reporter: "Reporter"):
    """Decides host_run and start_job calls: refused, run, run once as agreed, or put to the user. Deleting the
    task's own files and stopping its own processes run without asking (commands.Own)."""
    task_id, in_place = task["id"], bool(task["in_place"])
    roots = [cwd, TASK_CACHE_DIR]

    def gate(name: str, args: dict, ctx) -> PermissionResultAllow | PermissionResultDeny:
        tool = HOST_COMMANDS[name]
        command = args.get("command", "").strip()
        if not command:
            return PermissionResultDeny(message="Give the command to run.")
        if why := commands.refused(command, in_place):
            log.warning("task %s refused on the Mac (%s): %s", task_id, why, command)
            return PermissionResultDeny(message=f"Not run: {why}. If the work needs it, end with the exact command "
                                                f"for {profile.name()} to run themselves.")
        if not _inside(args.get("folder") or str(cwd), cwd, roots):
            return PermissionResultDeny(message=f"{tool} runs in {cwd} or a folder inside it (or {TASK_CACHE_DIR}).")
        if not commands.risky(command):
            return PermissionResultAllow()
        own = _own(task, cwd, _resolve(args.get("folder") or str(cwd), cwd), reporter.host_jobs)
        if not (why := commands.describe(command, own)):
            log.warning("task %s runs on the Mac, deleting or stopping only its own: %s", task_id, command)
            return PermissionResultAllow()
        item = approval_item(tool, args, cwd, why, own)
        if reporter.cleared(task_id, item):  # a yes to exactly this, or a rule the user made (jarvis.safety.remembered)
            log.warning("task %s runs on the Mac, as agreed (%s): %s", task_id, item["why"], command)
            return PermissionResultAllow()
        if getattr(ctx, "agent_id", None):
            return PermissionResultDeny(message=f"Not run: it {item['why']}, so it needs {profile.name()}'s go-ahead, and only "
                                                "the task's own agent can ask for that. Report the exact command back instead.")
        approval = reporter.request_approval(task_id, item)
        return PermissionResultDeny(message=(
            f"Not run: it {item['why']}, so it needs {profile.name()}'s go-ahead. JARVIS has asked them (request "
            f"{approval['id']}). Carry on with anything that doesn't depend on it; if nothing's left, end your turn with "
            f"one short line. When they answer you'll get a message; if it's a yes, run the identical command with {tool}."))

    return gate
