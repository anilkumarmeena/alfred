"""Task runner: runs JARVIS's background tasks in parallel, independent of any voice session.

  uv run python -m jarvis.tasks.runner                      serve (what launchd runs 24/7)
  uv run python -m jarvis.tasks.runner list                 recent tasks
  uv run python -m jarvis.tasks.runner show 3               one task, with its log
  uv run python -m jarvis.tasks.runner log 3 [20]           latest lines of its live log, every step included
  uv run python -m jarvis.tasks.runner start KIND PROJECT TITLE INSTRUCTION [ACCESS]   (PROJECT "-" for research)
  uv run python -m jarvis.tasks.runner reply 3 "use option B" [ACCESS]   (a running task gets it mid-run)
                                                            ACCESS: extra grants, e.g. artifacts,mac
  uv run python -m jarvis.tasks.runner stop 3
  uv run python -m jarvis.tasks.runner rerun 3              rerun command task 3's command as a new task
  uv run python -m jarvis.tasks.runner merge 3              merge a worktree task's branch, then delete it
  uv run python -m jarvis.tasks.runner discard 3            drop a finished task's work unmerged
  uv run python -m jarvis.tasks.runner edits 3 apply 1 2    apply (or dismiss) data-file edits task 3 suggested
  uv run python -m jarvis.tasks.runner approvals            pending requests to run risky commands
  uv run python -m jarvis.tasks.runner approve 12           approve request 12 (or: deny 12)
  uv run python -m jarvis.tasks.runner approve 12 always    and remember it: session or always, exact (default) or kind

Concepts:
  slots       MAX_TASKS run at once; command and connection tasks take no slot
  in place    default for code_change: edits the project folder, one task per project at a time; uncommitted work is
              first saved under refs/jarvis/. in_place=false (always for JARVIS's own repo): worktree + branch per task,
              committed at run end, never pushed; merged or discarded via the API
  goal check  a "finished" run is checked against its ask (jarvis.tasks.goal) and may be sent back; settles to-dos
  approvals   risky host commands wait for a yes (voice, panel, push with a hashed one-time nonce); first answer wins;
              may be remembered as rules; user `orders` from this turn's speech pre-approve what they name
  access      extra grants at start, on reply, or via request_access (task waits)
  shutdown    SIGTERM marks queued/running tasks `interrupted`; next start resumes up to RESUME_TASKS (_after_restart)
  carrying on a run out of turns carries on in its session, up to TASK_CONTINUES times per user ask (_carried_on);
              one the usage limit cut short waits queued for the reset, then resumes, once per reset (_limit_reset)
Also runs notices, the scheduler and the learning review. API: Unix socket in data/ (mode 600), unreachable from
browsers and from tasks' host commands. Mixins: approvals, command_tasks, git_ops, task_edits, events; HTTP in api.
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import signal
import sys
import time
import warnings
from contextlib import nullcontext
from pathlib import Path

from aiohttp import web
from claude_agent_sdk import CanUseToolShadowedWarning

from jarvis.brain import retry, usage
from jarvis.core import profile
from jarvis.core.config import (
    GOAL_DAILY_CHECKS,
    LEARNING_ENABLED,
    MAX_TASKS,
    NOTICES_ENABLED,
    PUSH_ENABLED,
    RESUME_TASKS,
    RUNNER_SOCKET,
    SCHEDULER_ENABLED,
    TASK_CONTINUES,
    TASK_MODEL,
)
from jarvis.core.database import (
    COMMAND,
    CONNECTION,
    KINDS,
    WAIT_ACCESS,
    WAIT_APPROVAL,
    WAIT_QUESTION,
    Store,
    minutes_since_update,
    nudges,
)
from jarvis.node import machines, placement
from jarvis.node.hub import Hub
from jarvis.node.offload import Offline, Offload
from jarvis.notify.push import task_target
from jarvis.safety import host
from jarvis.state import activity as bell, todo as todos
from jarvis.tasks import (
    access,
    announce,
    api,
    client,
    command_jobs,
    data_edits,
    goal,
    hold,
    learning,
    notices,
    projects,
    scheduler,
    stall,
)
from jarvis.tasks.approvals import ACCESS_QUESTION, Approvals, approval_question, approval_summary
from jarvis.tasks.command_tasks import CommandTasks
from jarvis.tasks.events import Events
from jarvis.tasks.git_ops import GitOps
from jarvis.tasks.host_tools import TaskJobs, few
from jarvis.tasks.reporting import nudge_text, with_nudges
from jarvis.tasks.task_edits import TaskEdits
from jarvis.tasks.worker import STEPS_OUT, Outcome, run

# Re-exported: tests import these from here
from jarvis.tasks.approvals import NotAllowed, approval_result  # noqa: F401
from jarvis.tasks.events import bell_entry  # noqa: F401

log = logging.getLogger("jarvis.tasks.runner")
# The worker deliberately allows web, to-do and reporting tools outright; everything else meets the gate.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)
STARTING = "starting"  # initial activity of a running task
STEP_SECONDS = 1.0  # flush interval for in-memory task steps
# Grace for tasks to stop on SIGTERM; must stay well under launchd's 20 s
SHUTDOWN_SECONDS = 10.0
RESTARTED = "JARVIS restarted while it was running. Reply to carry on."
# Prompt for a task resumed in its own session after a shutdown
RESUMED = ("JARVIS restarted while you were working on this task, cutting your last step off. Carry on with the task "
           "from where you were: first check what that step left behind, as it may not have finished, then end with "
           "your summary of the whole task as before.")
RESUMED_LOG = "resumed after JARVIS restarted"  # log marker; also detects crash loops
CONTINUED = ("You ran out of steps before finishing, so JARVIS has carried you on in this same session, with everything "
             "you did kept. Carry on with the task from where you were, without redoing what's finished, then end with "
             "your summary of the whole task as before.")
CONTINUED_LOG = "continued"  # log kind of each carry-on after running out of steps, counted against TASK_CONTINUES
LIMIT_RESUMED = ("Claude's usage limit stopped you partway through this task, and it has reset now. Carry on with the "
                 "task from where you were: first check what your last step left behind, as it may not have finished, "
                 "then end with your summary of the whole task as before.")
LIMIT_SLACK = 60.0  # seconds past a usage limit's reset before resuming, for clock differences
WAIT_STEP = 300.0  # most seconds a wait for the reset sleeps before checking the wall clock again


def _waits_for_reset(task: dict) -> float | None:
    """The usage-limit reset a queued task waits for (kept in `interrupted` by _launch), passed or not: its wait
    still ends in LIMIT_SLACK and the once-per-reset record."""
    return (json.loads(task["interrupted"] or "null") or {}).get("after")


class Runner(Approvals, CommandTasks, GitOps, TaskEdits, Events):
    """The task runner: mixins plus task start/reply/stop, the run loop, restart and shutdown."""

    def __init__(self, capacity: int = MAX_TASKS) -> None:
        self.store = Store()
        self.capacity = capacity
        self.slots = asyncio.Semaphore(capacity)
        self.jobs: dict[int, asyncio.Task] = {}
        self.in_line: set[int] = set()  # tasks holding or next in line for a slot
        self.begun: set[int] = set()  # runs in progress: new grants apply from the next turn
        self.folders: dict[str, asyncio.Lock] = {}  # per project folder
        self.merges: dict[int, asyncio.Task] = {}  # merges under way, by task id; halt() stops one
        self.snapshots: dict[int, dict] = {}  # pre-run file snapshot for in-place tasks without git
        self.projects_lock = asyncio.Lock()  # project list and project git setup
        self.listeners: set[asyncio.Queue] = set()
        self._bg: set[asyncio.Task] = set()
        self.scheduler: scheduler.Scheduler | None = None  # set by serve() if enabled
        self.learning: learning.Learning | None = None  # set by serve() if enabled
        self.pulse = stall.Pulse()
        self.commands = command_jobs.Runs()
        self.ending: dict[int, asyncio.Task] = {}  # command tasks whose ending is being processed
        self.host_jobs = TaskJobs()
        self.host_jobs.on_end = self._job_ended
        self.checks = goal.Budget(GOAL_DAILY_CHECKS)  # goal checks per rolling 24 h, all tasks
        self.prompts: dict[int, str] = {}  # launch prompt per queued/running task
        self.services: list[asyncio.Task] = []  # notices/scheduler/learning loops
        self.closing = False  # shutdown begun: no new work
        self.hub = Hub()  # links to the user's other machines (jarvis.node)
        self.offload = Offload(self, self.hub)  # sending them work
        self.placing: dict[int, asyncio.Task] = {}  # tasks and commands whose machine is being chosen
        self.placed: dict[int, str] = {}  # what to say about where each new one runs, for its start reply
        self.limit_waits: dict[int, float] = {}  # tasks queued until a usage limit resets -> its reset time
        self.wakes: dict[int, asyncio.Event] = {}  # set to end a task's wait for the reset early (a message came)
        self.limit_resumed: dict[int, float] = {}  # the reset each task last resumed after by itself
        self.resuming = self._after_restart()

    def _after_restart(self) -> list[tuple[int, str]]:
        """Settle tasks left queued or running by the last run; return up to RESUME_TASKS (task_id, prompt) to resume.

        Running ones go first. The rest are stopped (resumable by reply); see _resumable for why.
        """
        resuming, cut, waits = [], [], 0  # waits: tasks only waiting for a usage limit's reset, which cost nothing
        for t in self.store.active():
            if t["kind"] in (COMMAND, CONNECTION):  # tracked separately
                continue
            if t["status"] != "waiting":
                cut.append(t)
        for t in sorted(cut, key=lambda t: t["status"] != "running"):
            why, prompt = self._resumable(t)
            if not why and t["node"] and t["status"] == "running":  # still going on its machine: taken up again
                resuming.append((t["id"], prompt))
                continue
            if not why and _waits_for_reset(t) is not None:
                waits += 1
            elif not why and len(resuming) - waits >= RESUME_TASKS:
                why = f"no more than {RESUME_TASKS} resume at once (JARVIS_RESUME_TASKS)"
            if not why:
                resuming.append((t["id"], prompt))
                continue
            self.store.log(t["id"], "interrupted", f"not resumed on its own: {why}")
            # Hidden runs (watch checks) stay silent: the watch rechecks when next due
            self.store.set(t["id"], status="stopped", activity=None, interrupted=None,
                           error=None if t["hidden"] else RESTARTED)
        return resuming

    def _resumable(self, task: dict) -> tuple[str, str]:
        """(why it can't auto-resume or "", prompt to resume with)."""
        # Without a clean-shutdown note (crash/kill), a running task still resumes in its session unless it already
        # crashed once since its last resume
        cut = json.loads(task["interrupted"] or "null") or {}
        if task["hidden"]:
            return "a watch's check, made again when it's next due", ""
        if not cut and [e["text"] for e in self.store.history(task["id"], limit=1)] == [RESUMED_LOG]:
            return "it was resumed after the last restart, and the runner stopped again without a clean shutdown", ""
        if task["node"] and task["status"] == "running":  # its run went on there: take it up again
            return "", RESUMED
        if cut.get("on_mac"):
            return f"it was cut off running {few(cut['on_mac'])} on the computer", ""
        if self.store.approvals(task["id"]):
            return "it's waiting for a go-ahead", ""
        if task["question"] or task["wants"]:
            return "it's waiting for an answer", ""
        if task["status"] == "running" and task["session_id"]:
            return "", RESUMED
        if prompt := cut.get("prompt"):  # never started: relaunch as it was
            return "", prompt
        return "it has nothing to carry on with" if cut else "the runner stopped without a clean shutdown", ""

    # ── Reporter, called by worker.run ─────────────────────
    # (propose is in task_edits; cleared and request_approval in approvals)

    def session(self, task_id: int, session_id: str) -> None:
        self.store.set(task_id, session_id=session_id)

    def alive(self, task_id: int, msg: object = None) -> None:
        self.pulse.saw(task_id, msg)

    def activity(self, task_id: int, text: str) -> None:
        self.store.step(task_id, text)  # in memory; flushed by _write_steps

    def progress(self, task_id: int, text: str) -> None:
        self.store.log(task_id, "progress", text)
        self._emit("progress", self.store.update(task_id, activity=text), text)

    def subagent(self, task_id: int, text: str) -> None:
        self.store.log(task_id, "subagent", text)  # logged only, not broadcast
        self.store.set(task_id, activity=text)

    def ask(self, task_id: int, question: str) -> None:
        self.store.log(task_id, "question", question)
        self.store.set(task_id, question=question, wait_kind=WAIT_QUESTION)

    def flag(self, task_id: int, text: str) -> None:
        """Report content left out as hidden instructions: task log, bell and desktop notification."""
        self.store.log(task_id, "flagged", text)
        bell.add(f"Task {task_id}: {text}", "warn", task_id)
        self._background(self._notify(f"JARVIS · task {task_id}", text[:200]))

    def take_nudges(self, task_id: int) -> list[str]:
        messages = self.store.take_nudges(task_id)
        for text in messages:
            self.progress(task_id, f"nudge received: {text}")
        return messages

    def request_access(self, task_id: int, names: list[str], reason: str) -> list[str]:
        """Record an access request as the task's question (it then waits for a reply).

        Returns the names asked for; ValueError if it already has them all.
        """
        task = self.store.get(task_id)
        if not (wants := access.new(task, names)):
            raise ValueError("this task has that access already, so use it")
        reason = " ".join(reason.split())[:300]
        self.store.log(task_id, "access", f"asks for {', '.join(wants)}: {reason}")
        self.store.set(task_id, question=f"{ACCESS_QUESTION}{access.describe(wants)}? {reason}".strip(),
                       wait_kind=WAIT_ACCESS, wants=json.dumps(wants))
        return wants

    # ── Background loops and shutdown ───────────────────────────

    async def _write_steps(self, every: float = STEP_SECONDS) -> None:
        """Periodically flush steps in one transaction on a thread, keeping DB writes off the event loop."""
        while True:
            await asyncio.sleep(every)
            await asyncio.to_thread(self.store.write_steps)

    def _job_ended(self, job: dict, code: int) -> None:
        """Log a host job's end on its task."""
        if (task_id := job.get("task")) is None or not self.store.get(task_id):
            return
        self.store.log(task_id, "job", f"job {job['id']}, {job['title']}, {host.outcome(code)}")

    def start_background(self) -> None:
        """Start background loops (approval expiry, commands, step flush); re-announce approvals; resume tasks."""
        self._background(self._expire_approvals())
        self._background(self._watch_commands())
        self._background(self._write_steps())
        self.offload.resume_following()
        for approval in self.store.approvals():
            if not approval["announced"]:
                self._background(self._announce_approval(approval["id"]))
        for task_id, prompt in self.resuming:
            task = self.store.get(task_id)
            if (after := _waits_for_reset(task)) is not None:  # nothing was cut off: it goes on waiting
                self.limit_resumed[task_id] = after
                self._launch(task_id, prompt, after=after)
                continue
            self.store.log(task_id, "interrupted", RESUMED_LOG)
            bell.add(f"Resumed task {task_id} after JARVIS restarted: {task['title']}", "run", task_id)
            self._launch(task_id, prompt)
        self.resuming = []

    def service(self, coro) -> None:
        """Run a self-starting loop (notices, scheduler, learning); cancelled first on shutdown."""
        self.services.append(self._background(coro))

    async def shutdown(self, wait: float = SHUTDOWN_SECONDS) -> None:
        """Stop cleanly on SIGTERM: refuse new work, mark active tasks `interrupted` (with in-flight host commands and
        launch prompt), then cancel them silently. The next start resumes what it can."""
        self.closing = True
        for loop in self.services:
            loop.cancel()
        cut = dict(self.jobs)
        for task_id in cut:
            on_mac = self.host_jobs.on_mac.get(task_id, [])
            self.store.set(task_id, interrupted=json.dumps(
                {"at": time.time(), "on_mac": on_mac, "prompt": self.prompts.get(task_id),
                 "after": self.limit_waits.get(task_id)}, ensure_ascii=False))
            self.store.log(task_id, "interrupted", "JARVIS is shutting down" + (
                f", while it ran {few(on_mac)} on the computer" if on_mac else ""))
        for job in cut.values():
            job.cancel()
        if cut:
            await asyncio.wait(cut.values(), timeout=wait)
        await self.hub.close()
        self.store.write_steps()

    # ── Task control ────────────────────────────────────────────

    def start(self, kind: str, title: str, instruction: str, project: str | None = None, job: int | None = None,
              hidden: bool = False, grants: list[str] | None = None, order: str | None = None,
              fixes: int | None = None, todo: str | None = None, machine: str = "") -> dict:
        """Create and launch a task (command tasks use start_command).

          job      scheduled job id; `hidden` keeps watch checks out of lists and the bell until relevant
          grants   access beyond the kind's defaults
          order    the user's own words, which may pre-approve risky commands (_order)
          fixes    failed command task this task fixes
          todo     open to-do item it works on (text or unique substring), settled at the end
          machine  where the user said it should run ("here" or a machine's name); else Settings › Machines decides
        """
        self._open()
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}"
                             + (": a command starts with start_job, not as a task" if kind == COMMAND else ""))
        if not instruction.strip():
            raise ValueError("instruction is empty")
        grants = [n for n in access.check(grants) if n not in access.defaults(kind)]
        name, in_place, lives = None, False, None
        if kind != "research":
            if not project:
                raise ValueError(f"A {kind} task needs a project.")
            found = projects.find(project)
            name, in_place, lives = found.name, found.in_place and kind == "code_change", found.node
            if kind == "code_change" and not in_place and not lives and (problem := projects.worktree_problem(found)):
                raise ValueError(problem)
        item = todos.find(todo) if todo else None
        title = " ".join(title.split())[:80] or instruction[:60]
        placeable = kind != "research" and not access.has({"kind": kind, "grants": json.dumps(grants)}) & access.LOCAL
        if lives and not placeable:
            raise ValueError(f"{name} lives on {self.offload.name(lives)}, where accounts and other projects aren't: "
                             "start a task here for what needs them.")
        if machine and machine.strip().lower() != machines.HERE and not lives:
            if not placeable:
                raise ValueError(f"A {kind} task that reads accounts or other projects runs on this computer, where they are.")
            machines.find(machine)  # an unknown name is said now, before anything starts
        task = self.store.create(kind, title, instruction, name, in_place, job, hidden, grants, fixes=fixes,
                                 todo=item and item["id"])
        self.store.log(task["id"], "start", instruction)
        if lives:  # it runs where its project lives, whatever the setting
            self._placed(task["id"], placement.Place(lives, self.offload.name(lives),
                                                     f"It's running on {self.offload.name(lives)}, where {name} lives."))
        elif placeable and (machine or machines.paired()):
            self.placing[task["id"]] = placing = asyncio.create_task(self._place_task(task, machine))
            placing.add_done_callback(lambda _: self.placing.pop(task["id"], None))
        if item:
            self.store.log(task["id"], "todo", f"works on the to-do: {item['text']}")
        if grants:
            self.store.log(task["id"], "access", f"granted {', '.join(grants)} from the start")
        self._order(task["id"], order)
        if not hidden:
            started = "Started scheduled task" if job is not None else "Started task"
            bell.add(f"{started} {task['id']}: {title}" + (f", on {name}" if name else ""), "run", task["id"])
        return self._launch(task["id"], instruction)

    def take_up(self, task_id: int) -> dict:
        """Follow a run already going on (or ended on) the task's machine, as after a restart: one a machine began on
        its own while this computer was away (Offload._adopt)."""
        return self._launch(task_id, RESUMED)

    def reply(self, task_id: int, message: str, grants: list[str] | None = None, order: str | None = None) -> dict:
        """Send a message to a task, optionally with extra `grants`.

        A running task gets it as a nudge (grants apply from its next turn); otherwise a new run starts. `order` may
        approve pending requests it covers rather than withdrawing them.
        """
        task = self._get(task_id)
        if task["status"] == "discarded":
            raise ValueError(f"Task {task_id} was discarded, so it can't carry on; start a new task instead.")
        if task["kind"] == COMMAND:
            return self._reply_command(task, message)
        if task["kind"] == CONNECTION:
            raise ValueError(f"Task {task_id} is a call on a connection, which has nothing to carry on; make the call "
                             "again instead.")
        if task["kind"] == learning.KIND:
            raise ValueError(f"Task {task_id} is a {learning.TITLE}, which has nothing to carry on; apply or dismiss "
                             "its suggested edits with task_edits.")
        running = task["status"] in ("queued", "running")
        self.limit_resumed.pop(task_id, None)  # a new word from the user: a usage limit hit after it waits afresh
        if not running and task["worktree"] and not self._lives_on(task) and not Path(task["worktree"]).exists():
            folder = "project folder" if task["in_place"] else "worktree"
            raise ValueError(f"Task {task_id}'s {folder} has been removed, so it can't carry on.")
        note = ""
        if gained := access.new(task, grants):
            if task["node"] and set(gained) & access.LOCAL:
                raise ValueError(f"Task {task_id} runs on {self.offload.name(task['node'])}, where accounts and other "
                                 "projects aren't: start a task here for what needs them.")
            self.store.set(task_id, grants=access.add(task, gained))
            self.store.log(task_id, "access", f"granted {', '.join(gained)}")
            note = f"\n\nYou now also have {access.listing(gained)}."
            if task_id in self.begun:  # otherwise the run starts with it
                note += (" It takes effect from your next turn: once you reach what needs it, end your turn with one "
                         "short line, and you'll carry on in this session with it.")
        self._order(task_id, order)
        if running:
            # Nudge: queued tasks get it with the prompt, running ones after their current tool call
            self.store.log(task_id, "nudge", message)
            self.store.add_nudge(task_id, message + note)
            if wake := self.wakes.get(task_id):  # waiting for a usage limit's reset: the message goes now
                wake.set()
            self._apply_orders(task_id)
            return self.store.get(task_id)
        lead = "Answer" if task["status"] == "waiting" else "Follow-up"
        prompt = f"{lead} from {profile.name()}: {message}{note}"
        if not task["session_id"]:  # never started: include the original instruction
            prompt = f"{task['instruction']}\n\n{prompt}"
        self.store.log(task_id, "reply", message)
        self._launch(task_id, prompt)
        # Earlier requests are withdrawn (the new run follows the new words), except those the order approves
        self._apply_orders(task_id)
        self._close_approvals(task_id, "re-queued")
        return self.store.get(task_id)

    async def halt(self, task_id: int) -> dict:
        """Stop any task: a merge of it under way via stop_merge, running command tasks via stop_command, others via
        stop."""
        if task_id in self.merges:
            return await self.stop_merge(task_id)
        task = self._get(task_id)
        if task["kind"] == COMMAND and task["status"] == "running":
            return await self.stop_command(task_id)
        return self.stop(task_id)

    def stop(self, task_id: int) -> dict:
        task = self._get(task_id)
        if task["kind"] == COMMAND and task["status"] == "running":
            raise ValueError(f"Task {task_id} is running a command: stop it with halt.")
        if stopped := self.host_jobs.stop_all(task_id):  # its host jobs stop too
            self.store.log(task_id, "job", f"stopped job{'s' if len(stopped) > 1 else ''} {', '.join(map(str, stopped))}")
        if job := self.jobs.get(task_id):
            job.cancel()  # _job records the stop and commits work
        elif task["kind"] == CONNECTION and task["status"] == "running":  # its relay cancels the call
            self._finish(task_id, "stopped", f"Task {task_id}, {task['title']}, stopped.")
        elif task["kind"] == COMMAND and task["status"] == "waiting":  # declines the offered fix
            self.store.log(task_id, "declined", f"{profile.name()} said to leave it unfixed")
            self._finish(task_id, "failed", f"Task {task_id}, {task['title']}, is left as it failed: no fix started.",
                         seen=1, question=None, wait_kind=None)
        elif task["status"] == "waiting":
            self._finish(task_id, "stopped", announce.plain(announce.Ending(task["title"], announce.STOPPED,
                                                                           suggested=announce.suggestions(task))))
        else:
            raise ValueError(f"Task {task_id} isn't running.")
        return self.store.get(task_id)

    # ── Stalls ──────────────────────────────────────────────────

    def view(self, task: dict, now: float | None = None) -> dict:
        """Task as the API shows it, with `stalled` set if it seems stuck; command tasks add their `command` view.
        One on another machine names it (`machine`)."""
        if task.get("node"):
            task = task | {"machine": self.offload.name(task["node"])}
        if task["kind"] == COMMAND:
            return task | command_jobs.view(task, self.commands, now) | {"stalled": None}
        if task["kind"] == CONNECTION:  # no pulse kept here
            return task | {"stalled": None}
        return task | {"stalled": stall.check(task, self.store, self.pulse, self.host_jobs, now, remote=self.offload.excuse)}

    def stalls(self, now: float | None = None) -> list[dict]:
        """Running tasks that seem stuck, oldest first."""
        return [t for t in (self.view(t, now) for t in self.store.active()) if t["stalled"]]

    # ── Where it runs ───────────────────────────────────────────

    async def _place_task(self, task: dict, machine: str) -> None:
        """Choose the new task's machine (jarvis.node.placement), before its first run starts."""
        job = {"kind": task["kind"], "title": task["title"], "instruction": task["instruction"][:800],
               "project": task["project"]}
        try:
            place = await self.offload.place(placement.TASKS, job, machine)
        except Exception as e:  # never stops the task: it runs here
            log.warning("task %s: choosing its machine failed: %s", task["id"], e)
            place = placement.HERE
        self._placed(task["id"], place)

    def _placed(self, task_id: int, place: placement.Place) -> None:
        """Record where a new task or command runs, and what to say about it."""
        said = place.said or (f"It's running on {place.name}." if place.node else "")
        if place.node:
            self.store.set(task_id, node=place.node)
        if said:
            self.placed[task_id] = said
            self.store.log(task_id, "machine", said)
            bell.add(f"Task {task_id}: {said}", "run", task_id)

    async def where(self, task_id: int, seconds: float = 30) -> dict:
        """The task once its machine is chosen (waiting up to `seconds`), with `where` saying where it runs."""
        if placing := self.placing.get(task_id):
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(placing), seconds)
        task = self.view(self.store.get(task_id))
        return task | ({"where": said} if (said := self.placed.pop(task_id, "")) else {})

    # ── Running ─────────────────────────────────────────────────

    def _launch(self, task_id: int, prompt: str, after: float | None = None) -> dict:
        """Start or queue a run (activity says what it waits on), clearing stale question/error/spoken fields.

        A task on another machine takes no slot here: it runs at once (as far as this computer's concerned). `after`:
        a usage limit's reset to wait for first, holding no slot; kept in `interrupted` so a crash doesn't lose it."""
        self._open()
        task = self.store.get(task_id)
        cut = None
        if after is not None:
            status, activity = "queued", f"waiting for Claude's usage limit to reset {usage.when(after)}, then carrying on"
            cut = json.dumps({"prompt": prompt, "after": after}, ensure_ascii=False)
            self.limit_waits[task_id] = after
            self.wakes[task_id] = asyncio.Event()  # now, so a reply before the job's first step still wakes it
        elif ahead := self._ahead_in_folder(task):
            ids = " and ".join(map(str, ahead))
            status, activity = "queued", (f"queued behind task {ids}: this project's tasks work in its own folder, so "
                                          "they run one at a time")
        elif task["in_place"] and self._folder_lock(task["project"]).locked():
            status, activity = "queued", "waiting for its project's folder, which a merge or git setup is using"
        elif task["node"]:
            status, activity = "running", STARTING
        else:
            status, activity = ("running", STARTING) if self._slot_free(task_id) else ("queued", self._slot_wait())
            self.in_line.add(task_id)
        task = self.store.update(task_id, status=status, question=None, wait_kind=None, wants=None, error=None,
                                 spoken=None, activity=activity, interrupted=cut)
        self.prompts[task_id] = prompt
        job = self.jobs[task_id] = asyncio.create_task(self._job(task_id, prompt))
        job.add_done_callback(lambda _: self._never_began(task_id, job))
        return task

    def _never_began(self, task_id: int, job: asyncio.Task) -> None:
        """Clean up and announce a job cancelled before _job got to run."""
        if self.jobs.get(task_id) is not job:  # it ran and cleaned up itself
            return
        self.jobs.pop(task_id)
        self.prompts.pop(task_id, None)
        self.in_line.discard(task_id)
        self._waited(task_id)
        if self.closing:  # left for _after_restart
            return
        self._finish(task_id, "stopped", announce.plain(announce.Ending(self.store.get(task_id)["title"],
                                                                       announce.STOPPED)))

    def _slot_free(self, task_id: int) -> bool:
        """Whether fewer than `capacity` other tasks hold or await a slot."""
        return len(self.in_line - {task_id}) < self.capacity

    def _slot_wait(self) -> str:
        return f"waiting for a free slot: {self.capacity} {'task runs' if self.capacity == 1 else 'tasks run'} at once"

    def _ahead_in_folder(self, task: dict) -> list[int]:
        """Other active in-place tasks on the same project."""
        if not task["in_place"]:
            return []
        return [i for i in self._jobs_on(task["project"], in_place=True) if i != task["id"]]

    def _folder(self, task: dict):
        """Per-folder lock for in-place tasks; no-op otherwise."""
        return self._folder_lock(task["project"]) if task["in_place"] else nullcontext()

    async def _still_there(self, task: dict) -> dict:
        """A task for a machine that has gone away since it was placed: one that never ran there runs here instead
        (and says so); one whose session or project is there waits for it (Offload.run_task)."""
        if self.hub.connected(task["node"]) is not None or task["session_id"] or self._lives_on(task):
            return task
        said = f"{self.offload.name(task['node'])} isn't online now, so it's running on this computer instead."
        self.store.log(task["id"], "machine", said)
        bell.add(f"Task {task['id']}: {said}", "warn", task["id"])
        return self.store.update(task["id"], node=None)

    async def _job(self, task_id: int, prompt: str) -> None:
        outcome = None  # None means stopped
        had: set[str] | None = None  # access at run start
        try:
            if (after := self.limit_waits.get(task_id)) is not None:
                await self._wait_for_reset(task_id, after)
            if placing := self.placing.get(task_id):
                await asyncio.shield(placing)
            task = self.store.get(task_id)
            if remote := bool(task["node"]):
                task = await self._still_there(task)
                remote = bool(task["node"])
            if remote and self._lives_on(task):  # its git steps run there too
                await self.offload.reachable(task)
            if task["kind"] == "code_change" and not task["in_place"] and not task["worktree"]:
                await self._make_worktree(task)
            async with self._folder(task):
                if not remote and task_id not in self.in_line:  # waited for the folder; now queue for a slot
                    if not self._slot_free(task_id):
                        self.store.set(task_id, activity=self._slot_wait())
                    self.in_line.add(task_id)
                reattach = remote and prompt == RESUMED  # its run there goes on: taken up, not begun again
                async with nullcontext() if remote else self.slots:  # one on another machine takes no slot here
                    if task["in_place"] and not reattach:
                        await self._enter_folder(task)
                    task = self.store.update(task_id, status="running", activity=STARTING)
                    had = access.has(task)
                    self.begun.add(task_id)
                    if early := self.take_nudges(task_id):
                        prompt = with_nudges(prompt, early, started=bool(task["session_id"]))
                        if not task["session_id"]:  # first run: fold messages into the instruction
                            task = self.store.update(task_id, instruction=with_nudges(task["instruction"], early,
                                                                                      started=False))
                    began = time.time()
                    try:
                        if remote:
                            outcome = await self.offload.run_task(task, prompt, reattach=reattach)
                        else:
                            outcome = await run(task, prompt, self)
                    finally:
                        self._spend_approvals(task_id, before=began)
        except asyncio.CancelledError:
            pass
        except Offline as e:
            outcome = Outcome("", str(e), 0, retry.PASSING)
        except Exception as e:
            log.exception("task %s failed", task_id)
            outcome = Outcome("", f"{type(e).__name__}: {e}"[:200], 0, retry.classify(e))
        finally:
            self.jobs.pop(task_id, None)
            self.prompts.pop(task_id, None)
            self.in_line.discard(task_id)
            self.begun.discard(task_id)
            self.pulse.forget(task_id)
            self._waited(task_id)
        if self.closing:  # left for _after_restart
            return

        task = self.store.get(task_id)
        fields: dict = {}
        if outcome:
            fields["cost"] = task["cost"] + outcome.cost
            if outcome.result:
                fields["summary"] = outcome.result
        if task["in_place"] and task["worktree"]:
            fields["changes"] = await self._uncommitted(task)
        elif task["worktree"]:
            fields["changes"] = await self._commit(task, outcome.result if outcome else "", finished=bool(outcome and not outcome.error))
        check = more = reading = None
        new_access = access.has(task) - had if had is not None else set()
        gained = [n for n in access.GRANTS if n in new_access]
        owes = bool(task["question"] or self.store.approvals(task_id))  # waits on the user, whatever else happened
        if outcome and outcome.kind == retry.LIMIT and not owes and (after := self._limit_reset(task, outcome)):
            self.store.set(task_id, **fields)
            self._wait_out_limit(task, LIMIT_RESUMED if task["session_id"] else prompt, after, bool(outcome.resets))
            return
        if outcome and outcome.error == STEPS_OUT and not owes:
            more, outcome = self._carried_on(task, outcome)
        elif outcome and not outcome.error and not owes:
            if gained:  # granted mid-run: continue with it
                more = (f"You now have {access.listing(gained)}. Carry on with the task, using it where it's needed, "
                        "then end with your summary of the whole task as before.")
            elif not nudges(self.store.get(task_id)):  # claims done: verify
                try:
                    reading = await scheduler.read(task, outcome.result)  # scheduled runs: met / nothing new
                    check, more = await self._check_goal(task, outcome.result, fields, reading.nothing_new)
                except asyncio.CancelledError:
                    if self.closing:
                        self.store.set(task_id, **fields)
                        return
                    outcome = None  # stopped during the check
        undelivered = False
        if nudges(self.store.get(task_id)):  # arrived after the last delivery
            if outcome and not outcome.error and not task["question"]:  # continue with them
                more = nudge_text(self.take_nudges(task_id), finished=True) + (f"\n\n{more}" if more else "")
            else:
                undelivered = True  # mentioned in the announcement
        if more:  # continue silently: nudges or sent back to finish
            self.store.set(task_id, **fields)
            self._launch(task_id, more)
            return
        left, needs_user = goal.left(check)

        async def said(status: str, report: str = "", lead: str = "", done: list[str] | None = None) -> str:
            """Spoken announcement of this ending, including pending edits."""
            return await announce.spoken(announce.Ending(
                task["title"], status, report, lead, left, needs_user, done or [],
                announce.suggestions(self.store.get(task_id)), undelivered))

        if task["fixes"] and outcome and not outcome.error and not task["question"] \
                and not self.store.approvals(task_id):
            # Fix task for a failed command: unless it says the cause remains, rerun it and let that announce
            again = await command_jobs.wants_rerun(outcome.result)
            spoken, quiet = self._fixed(task, outcome.result, again)
            if quiet:
                self._finish(task_id, "done", spoken, quiet=True, seen=1, **fields)
                bell.add(spoken, "ok", task_id)
            else:
                self._finish(task_id, "done", await said(announce.DONE, lead=spoken), title=self._job_title(task),
                             **fields)
            return
        if task["job"] is not None and self.scheduler and outcome and not task["question"] \
                and not self.store.approvals(task_id) \
                and (verdict := self.scheduler.ended(task, outcome.result, outcome.error, reading, outcome.kind)):
            # Scheduled run: announce under the job's title, or stay quiet
            if verdict.summary is not None:  # notepad stripped
                fields["summary"] = verdict.summary
            status = "failed" if outcome.error else "done"
            quiet = verdict.quiet and not data_edits.pending(task) and not undelivered
            spoken = verdict.spoken if quiet else await said(status, lead=verdict.spoken)
            self._finish(task_id, status, spoken, quiet=quiet, title=verdict.title,
                         hidden=int(quiet), **({"seen": 1} if quiet else {}),
                         **({"error": outcome.error} if outcome.error else {}), **fields)
            return
        if task["hidden"]:
            fields["hidden"] = 0  # a watch check that needs the user
        if outcome is None:
            self._finish(task_id, "stopped", await said(announce.STOPPED), **fields)
        elif outcome.error:
            self._finish(task_id, "failed", await said(announce.FAILED, outcome.error), error=outcome.error, **fields)
        elif task["question"]:  # edits are mentioned once it finishes
            self._finish(task_id, "waiting", await announce.spoken(
                announce.Ending(task["title"], announce.QUESTION, task["question"])), **fields)
        elif pending := self.store.approvals(task_id):  # already announced; wait silently
            self._finish(task_id, "waiting", f"Task {task_id}, {task['title']}, is waiting for your go-ahead to run "
                                             f"{' and '.join(approval_summary(a['commands']) for a in pending)}.",
                         question=approval_question(pending), wait_kind=WAIT_APPROVAL, quiet=True, **fields)
        else:
            done = await self._settle_todos(task, check)
            self._finish(task_id, "done", await said(announce.DONE, outcome.result, done=done), **fields)

    async def _check_goal(self, task: dict, result: str, fields: dict,
                          nothing_new: bool = False) -> tuple[goal.Check | None, str | None]:
        """Goal-check a run that claims completion; returns (check or None, send-back message or None).

        Adds the check cost to `fields`. Stopping the task raises CancelledError. `nothing_new` runs are skipped.
        """
        task_id = task["id"]
        if goal.skipped(task, nothing_new):
            return None, None
        entries = self.store.history(task_id, goal.LOG_ENTRIES, steps=True)
        self.store.set(task_id, activity="checking its result against what it was asked")
        self.jobs[task_id] = asyncio.current_task()  # stop() cancels the check
        try:
            proof = await goal.evidence(task, fields.get("changes") or task["changes"] or "")
            done = goal.rounds(entries)
            check = await goal.review(goal.material(task, result, entries, proof, done, goal.todos(task)), self.checks)
        finally:
            self.jobs.pop(task_id, None)
        if check is None:
            self.store.log(task_id, goal.KIND, "couldn't check the result, so it's reported as it is")
            return None, None
        fields["cost"] = fields.get("cost", task["cost"]) + check.cost
        said, more = goal.next_step(check, done, fields["cost"])
        self.store.log(task_id, goal.KIND, said)
        return check, more

    # ── Carrying on by itself ───────────────────────────────────

    def _carried_on(self, task: dict, outcome: Outcome) -> tuple[str | None, Outcome]:
        """(prompt carrying a run that ran out of steps on in its session, or None, and the outcome to report).

        At most TASK_CONTINUES times since the user last asked or sent it a message; past that it fails, saying so.
        A watch's check isn't carried on: the scheduler checks again when it's next due."""
        task_id = task["id"]
        entries = self.store.history(task_id, goal.LOG_ENTRIES)
        asked = max((i for i, e in enumerate(entries) if e["kind"] in ("start", "reply", "nudge")), default=0)
        done = sum(e["kind"] == CONTINUED_LOG for e in entries[asked:])
        if task["hidden"] or not task["session_id"]:
            return None, outcome
        if done < TASK_CONTINUES:
            self.store.log(task_id, CONTINUED_LOG, f"ran out of steps, so JARVIS carried it on by itself "
                                                   f"({done + 1} of {TASK_CONTINUES})")
            bell.add(f"Task {task_id} ran out of steps, so it carries on by itself: {task['title']}", "run", task_id)
            return CONTINUED, outcome
        if not done:
            return None, outcome
        times = f"{done} time{'s' if done != 1 else ''}"
        return None, dataclasses.replace(outcome, error=f"{STEPS_OUT} again after JARVIS carried it on by itself {times}, "
                                                        "the most it does, so it needs a reply to carry on")

    def _limit_reset(self, task: dict, outcome: Outcome) -> float | None:
        """The usage-limit reset a run cut short waits for, or None to report it failed: a scheduled run (the scheduler
        runs it again after the reset), one that hit the limit again before a new reset (once per reset), or a second
        hit with no reset known (a guessed one can't tell, so it can't loop)."""
        if task["job"] is not None:
            return None
        resets = outcome.resets or (None if task["node"] else usage.used_up_until())  # another's account isn't known
        last = self.limit_resumed.get(task["id"])
        if not resets:
            return None if last is not None else time.time() + usage.UNKNOWN_RESET
        return resets if last is None or resets > last else None

    def _wait_out_limit(self, task: dict, prompt: str, resets: float, reported: bool) -> None:
        """Queue the task until the usage limit resets, and say so once (a wait also as a notification and push).

        A limit on its model family alone needn't wait: the next run uses a lighter model (usage.available). That's
        tried once, when Claude `reported` the limit and no limit for every model is used up; a later hit waits."""
        task_id = task["id"]
        now = time.time()
        if (reported and task_id not in self.limit_resumed and not task["node"] and not usage.used_up_until()
                and usage.available(TASK_MODEL) != TASK_MODEL):
            self.limit_resumed[task_id] = now
            after, said = now, (f"Task {task_id}, {task['title']}, hit Claude's usage limit for its model, so it "
                                "carries on with a lighter one.")
        else:
            self.limit_resumed[task_id] = resets
            after, said = resets, (f"Task {task_id}, {task['title']}, hit Claude's usage limit. It carries on by itself "
                                   f"when that resets {usage.when(resets)}.")
        self.store.log(task_id, "limit", said)
        self._launch(task_id, prompt, after=after)
        if task["hidden"]:
            return
        self._emit("status", self.store.update(task_id, spoken=said, seen=0), said)
        if after > time.time() and not hold.held():
            self._background(self._notify(f"JARVIS · task {task_id}", said))
            self._background(self._push(f"Task {task_id} waits for the usage limit", said, *task_target(task_id)))

    async def _wait_for_reset(self, task_id: int, after: float) -> None:
        """Sleep until `after`, and any limit still used up on this computer, has reset, or a message comes. Wakes
        every WAIT_STEP to check the wall clock: the event loop's clock stops while the computer sleeps."""
        # A message sent before the limit was hit doesn't end the wait: it was for that run, and goes with the next
        wake = self.wakes.setdefault(task_id, asyncio.Event())
        task = self.store.get(task_id)
        while not wake.is_set():
            until = self.limit_waits[task_id] = max(after, 0 if task["node"] else usage.used_up_until() or 0)
            if (left := until + LIMIT_SLACK - time.time()) <= 0:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), min(left, WAIT_STEP))
        if wake.is_set():
            self.limit_resumed.pop(task_id, None)  # sent on early: hitting the limit again waits for the reset again
            self.store.log(task_id, "limit", "a message came, so it carries on before the usage limit's reset")
        else:
            self.store.log(task_id, "limit", "Claude's usage limit has reset, so it carries on by itself")
        self._waited(task_id)
        self.store.set(task_id, interrupted=None)

    def _waited(self, task_id: int) -> None:
        """Forget a task's wait for a usage limit's reset: it's over, or its job ended."""
        self.limit_waits.pop(task_id, None)
        self.wakes.pop(task_id, None)


async def serve() -> None:
    runner = Runner()
    app = api.app(runner)
    # tcp_keepalive off: meaningless on a Unix socket and raises EINVAL on macOS for already-closed clients.
    # shutdown_timeout=1: event streams never end on their own.
    site_runner = web.AppRunner(app, access_log=None, tcp_keepalive=False, shutdown_timeout=1)
    await site_runner.setup()
    RUNNER_SOCKET.unlink(missing_ok=True)
    old = os.umask(0o077)  # socket created mode 600
    try:
        await web.UnixSite(site_runner, str(RUNNER_SOCKET)).start()
    finally:
        os.umask(old)
    log.info("task runner on %s, %d at once", RUNNER_SOCKET, MAX_TASKS)
    if runner.resuming:
        log.info("resuming tasks %s, cut off by the last shutdown", ", ".join(str(i) for i, _ in runner.resuming))
    runner.start_background()
    if NOTICES_ENABLED:
        runner.service(notices.make(runner.store, runner._notify, runner.broadcast, push=runner._push,
                                    stalls=runner.stalls, lost=runner.offload.lost).run())
        log.info("notices every %d minutes", notices.MINUTES)
    if SCHEDULER_ENABLED:
        runner.scheduler = scheduler.Scheduler(runner, runner._notify, runner.broadcast, push=runner._push)
        runner.service(runner.scheduler.run())
        log.info("scheduler on")
    if LEARNING_ENABLED:
        runner.learning = learning.Learning(runner.learned)
        runner.service(runner.learning.run())
        log.info("learning review on")
    if PUSH_ENABLED:
        log.info("push notifications on")
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):  # launchd stop; Ctrl-C under make dev
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("task runner shutting down, with %d tasks queued or running", len(runner.jobs))
    await runner.shutdown()
    await site_runner.cleanup()
    log.info("task runner stopped")


# ── Command line ────────────────────────────────────────────────


def _line(t: dict) -> str:
    age = minutes_since_update(t)
    detail = t["question"] or t["activity"] or t["spoken"] or ""
    if stalled := t.get("stalled"):
        detail = f"seems stuck, quiet {stalled['minutes']}m: {detail}"
    return f"{t['id']:>4}  {t['status']:<8} {t['kind']:<13} {t['title'][:40]:<40} {age:>4}m  {detail[:70]}"


def _access(words: list[str]) -> list[str]:
    """Split CLI grants like 'artifacts,mac'."""
    return [n for w in words for n in w.split(",") if n]


async def _cli(args: list[str]) -> None:
    cmd, rest = args[0], args[1:]
    if cmd == "list":
        for t in await client.tasks():
            print(_line(t))
    elif cmd == "show":
        print(json.dumps(await client.task(int(rest[0])), indent=2, default=str))
    elif cmd == "log":
        task = await client.log(int(rest[0]), int(rest[1]) if len(rest) > 1 else None)
        print(_line(task))
        for e in task["log"]:
            print(f"      {time.strftime('%H:%M:%S', time.localtime(e['at']))}  {e['kind']:<9} {e['text'][:200]}")
    elif cmd == "start":
        kind, project, title, instruction, *grants = rest
        print(_line(await client.start(kind, title, instruction, None if project == "-" else project, _access(grants))))
    elif cmd == "reply":
        print(_line(await client.reply(int(rest[0]), rest[1], _access(rest[2:]))))
    elif cmd == "stop":
        print(_line(await client.stop(int(rest[0]))))
    elif cmd == "rerun":
        print(_line(await client.rerun(int(rest[0]))))
    elif cmd == "merge":
        print(_line(await client.merge(int(rest[0]))))
    elif cmd == "discard":
        print(_line(await client.discard(int(rest[0]))))
    elif cmd == "edits":
        out = await client.edits(int(rest[0]), rest[1], [int(n) for n in rest[2:]])
        print("\n".join(out["outcome"]))
    elif cmd == "approvals":
        for a in await client.approvals():
            print(f"{a['id']:>4}  task {a['task']:<4} {a['summary']}")
            for c in a["commands"]:
                print(f"        {c['tool']} in {c['folder']}: {c['command']}")
    elif cmd in ("approve", "deny"):
        scope = rest[1] if len(rest) > 1 else "once"
        out = await client.answer(int(rest[0]), cmd, "cli", remember=scope, breadth=rest[2] if len(rest) > 2 else "exact")
        print(out["outcome"])
        if kept := out.get("remembered"):
            print("\n".join(f"  remembered: rule {r['id']}, {r['covers']}, {r['who']}" for r in kept["rules"])
                  or f"  not remembered: {kept.get('never')}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        asyncio.run(_cli(sys.argv[1:]))
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        asyncio.run(serve())
