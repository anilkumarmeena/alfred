"""The stall watchdog: notices a running task that has gone quiet, so the user can nudge it or stop it.

A running task shows it's alive with everything its session sends (each message, tool call and result, and its
subagents' progress, which the worker hands to Reporter.alive) and with its log lines and updates in tasks.db. One that
has shown none of these for TASK_STALL_MINUTES is stalled, unless its quiet is one it's meant to have:

  a request to run a risky command, waiting for the user's yes
  a question of its own, or access it asked for, waiting for an answer (asked mid-turn, just before it stops to wait)
  Claude's usage limit, used up until it resets: the run saw it rejected, or it's used up for every model
  a command it's running, for as long as that command may take: host_run and Bash up to their time limits, job_wait
  a long job it started (start_job), still running

Only running tasks count: queued ones wait for a slot, and waiting ones for the user, on purpose.

The runner keeps the pulse in memory (a restart stops running tasks anyway) and shows a stalled task's `stalled` in its
task views: task_list, task_detail and the control panel. The heartbeat's tasks source flags it once, by voice, a Mac
notification and a push, offering to nudge it or stop it; again only if it moves on and then stalls afresh. Nothing is
ever stopped on its own: that's the user's call. Checks follow the heartbeat, so a stall is flagged within
HEARTBEAT_MINUTES of reaching TASK_STALL_MINUTES, and not in quiet hours or do not disturb; the views show it at once.
"""

import time
from collections.abc import Callable

from claude_agent_sdk import AssistantMessage, RateLimitEvent, ToolResultBlock, ToolUseBlock, UserMessage

from jarvis.brain import usage
from jarvis.core.config import TASK_STALL_MINUTES
from jarvis.core.database import COMMAND
from jarvis.safety import host
from jarvis.tasks.host_tools import HOST_RUN, JOB_WAIT, call_minutes

GRACE = 120  # seconds beyond a command's own time limit before its quiet counts
BASH_DEFAULT, BASH_MOST = 120, 600  # seconds: the Bash tool's time limit unless it's given one, and its longest
# What a command still running is doing, as the task's excuse for being quiet
DOING = {"Bash": "running a command", HOST_RUN: "running a command on the Mac", JOB_WAIT: "waiting for one of its jobs"}


def lasts(block: ToolUseBlock) -> float:
    """How long a tool call may legitimately go without a word, in seconds: 0 for any but the commands that wait."""
    args = block.input or {}
    if block.name in (HOST_RUN, JOB_WAIT):
        return call_minutes(args) * 60
    if block.name == "Bash" and not args.get("run_in_background"):
        try:  # its timeout is in milliseconds
            return min(float(args.get("timeout") or BASH_DEFAULT * 1000) / 1000, BASH_MOST)
        except (TypeError, ValueError):
            return BASH_DEFAULT
    return 0.0


class Pulse:
    """When each running task last showed a sign of life, the commands it's in the middle of, and the usage limit its
    run hit."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.last: dict[int, float] = {}
        self.calls: dict[int, dict[str, tuple[str, float]]] = {}  # task: tool call id: (what it's doing, until when)
        self.limited: dict[int, float] = {}  # task: when the usage limit its run hit resets

    def saw(self, task_id: int, msg: object = None) -> None:
        """A sign of life: any message from the task's session (or none, just word that it's alive)."""
        now = self.clock()
        self.last[task_id] = now
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock) and (seconds := lasts(block)):
                    self.calls.setdefault(task_id, {})[block.id] = (DOING[block.name], now + seconds + GRACE)
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    self.calls.get(task_id, {}).pop(block.tool_use_id, None)
        elif isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            if info.status == "rejected":
                self.limited[task_id] = float(info.resets_at or now + usage.UNKNOWN_RESET)
            else:
                self.limited.pop(task_id, None)

    def heard(self, task_id: int) -> float:
        return self.last.get(task_id, 0.0)

    def busy(self, task_id: int, now: float) -> str:
        """What the task's command still within its time limit is doing, or ""."""
        return next((what for what, until in self.calls.get(task_id, {}).values() if until > now), "")

    def limit(self, task_id: int, now: float) -> float | None:
        """When the usage limit the task's run hit resets, while it hasn't."""
        resets = self.limited.get(task_id)
        return resets if resets and resets > now else None

    def forget(self, task_id: int) -> None:
        """Its run is over."""
        for kept in (self.last, self.calls, self.limited):
            kept.pop(task_id, None)


def excuse(task: dict, store, pulse: Pulse, jobs: host.Jobs, now: float) -> str:
    """Why a running task may be quiet now without being stuck, or "" if nothing explains it."""
    task_id = task["id"]
    if store.approvals(task_id):
        return "waiting for your go-ahead"
    if task.get("question") or task.get("wants"):
        return "waiting for your answer"
    if pulse.limit(task_id, now) or usage.out(now):
        return "waiting for Claude's usage limit to reset"
    if doing := pulse.busy(task_id, now):
        return doing
    if any(j["state"] == host.RUNNING for j in jobs.status(task=task_id)):
        return "a long job it started is still running"
    return ""


def check(task: dict, store, pulse: Pulse, jobs: host.Jobs, now: float | None = None,
          minutes: int = TASK_STALL_MINUTES) -> dict | None:
    """{"minutes": how long it has been quiet, "since": when it last showed a sign of life} for a running task that
    has stalled; None for any other. A command task (jarvis.tasks.command_jobs) runs with no model, so it has no pulse:
    its quiet is its command at work."""
    if minutes <= 0 or task["status"] != "running" or task.get("kind") == COMMAND:
        return None
    now = now or time.time()
    since = max(pulse.heard(task["id"]), task["updated"], store.last_logged(task["id"]))
    if now - since < minutes * 60 or excuse(task, store, pulse, jobs, now):
        return None
    return {"minutes": int((now - since) // 60), "since": since}
