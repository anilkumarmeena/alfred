"""Stall watchdog: flag a running task that has gone quiet so the user can nudge or stop it.

Signs of life: any session message (incl. subagent progress via Reporter.alive), log lines and updates in tasks.db.
A running task with none for TASK_STALL_MINUTES is stalled unless the quiet is expected:
  pending approval of a risky command, an open question or access request, Claude's usage limit hit,
  a command still within its time limit (host_run, Bash, job_wait), or a running start_job job.
Queued and waiting tasks never count.

The pulse is in memory only. Task views show `stalled` at once; jarvis.tasks.notices says it once per stall (voice,
desktop notification, push), within notices.MINUTES and not in quiet hours or do-not-disturb. Never stops a task itself.
"""

import time
from collections.abc import Callable

from jarvis.config import TASK_STALL_MINUTES
from jarvis.llm import usage
from jarvis.llm.sdk import AssistantMessage, RateLimitEvent, ToolResultBlock, ToolUseBlock, UserMessage
from jarvis.safety import host
from jarvis.tasks.host_tools import HOST_RUN, JOB_WAIT, call_minutes
from jarvis.tasks.store import COMMAND

GRACE = 120  # seconds of slack past a command's own time limit
BASH_DEFAULT, BASH_MOST = 120, 600  # seconds: Bash tool default and maximum timeout
# Excuse shown for a quiet task, per still-running command tool
DOING = {"Bash": "running a command", HOST_RUN: "running a command on the computer",
         JOB_WAIT: "waiting for one of its jobs"}


def lasts(block: ToolUseBlock) -> float:
    """Seconds a tool call may legitimately stay silent; 0 for tools that don't block."""
    args = block.input or {}
    if block.name in (HOST_RUN, JOB_WAIT):
        return call_minutes(args) * 60
    if block.name == "Bash" and not args.get("run_in_background"):
        try:  # timeout is in ms
            return min(float(args.get("timeout") or BASH_DEFAULT * 1000) / 1000, BASH_MOST)
        except (TypeError, ValueError):
            return BASH_DEFAULT
    return 0.0


class Pulse:
    """Per running task: last sign of life, in-flight blocking commands, and usage-limit reset time."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.last: dict[int, float] = {}
        self.calls: dict[int, dict[str, tuple[str, float]]] = {}  # task -> tool_use id -> (doing, deadline)
        self.limited: dict[int, float] = {}  # task -> usage-limit reset time

    def saw(self, task_id: int, msg: object = None) -> None:
        """Record a sign of life, tracking blocking tool calls and rate-limit events in `msg` if given."""
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
        """Label for an in-flight command still within its time limit, or ""."""
        return next((what for what, until in self.calls.get(task_id, {}).values() if until > now), "")

    def limit(self, task_id: int, now: float) -> float | None:
        """Reset time of a usage limit the run hit, if not yet reset."""
        resets = self.limited.get(task_id)
        return resets if resets and resets > now else None

    def forget(self, task_id: int) -> None:
        """Drop all state for a finished run."""
        for kept in (self.last, self.calls, self.limited):
            kept.pop(task_id, None)


def excuse(task: dict, store, pulse: Pulse, jobs: host.Jobs, now: float,
           remote: Callable[[dict, float], str] | None = None) -> str:
    """Why a running task may legitimately be quiet now, or "". `remote` gives the excuse of a task on another
    machine (unreachable, or busy there)."""
    task_id = task["id"]
    if remote and (away := remote(task, now)):
        return away
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
          minutes: int = TASK_STALL_MINUTES, remote: Callable[[dict, float], str] | None = None) -> dict | None:
    """{"minutes", "since"} for a stalled running task, else None.

    Command tasks are never stalled: they have no model session, so silence is expected.
    """
    if minutes <= 0 or task["status"] != "running" or task.get("kind") == COMMAND:
        return None
    now = now or time.time()
    since = max(pulse.heard(task["id"]), task["updated"], store.last_logged(task["id"]))
    if now - since < minutes * 60 or excuse(task, store, pulse, jobs, now, remote):
        return None
    return {"minutes": int((now - since) // 60), "since": since}
