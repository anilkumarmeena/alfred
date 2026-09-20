"""The task runner: runs JARVIS's background tasks in parallel, apart from any voice session.

  uv run python -m jarvis.tasks.runner                      serve (what launchd runs 24/7)
  uv run python -m jarvis.tasks.runner list                 recent tasks
  uv run python -m jarvis.tasks.runner show 3               one task, with its log
  uv run python -m jarvis.tasks.runner log 3 [20]           the latest lines of its live log, every step included
  uv run python -m jarvis.tasks.runner start KIND PROJECT TITLE INSTRUCTION [ACCESS]   (PROJECT "-" for research)
  uv run python -m jarvis.tasks.runner reply 3 "use option B" [ACCESS]   (to a running task: a nudge it gets mid-run)
                                                            ACCESS: more than its kind has, e.g. artifacts,mac
  uv run python -m jarvis.tasks.runner stop 3
  uv run python -m jarvis.tasks.runner rerun 3              run command task 3's command again, as a task of its own
  uv run python -m jarvis.tasks.runner merge 3              merge a worktree task's branch, then delete it
  uv run python -m jarvis.tasks.runner discard 3            throw a finished task's work away without merging it
  uv run python -m jarvis.tasks.runner edits 3 apply 1 2    apply (or dismiss) data-file edits task 3 suggested
  uv run python -m jarvis.tasks.runner approvals            requests to run risky commands, still waiting for an answer
  uv run python -m jarvis.tasks.runner approve 12           say yes to request 12 (or: deny 12)
  uv run python -m jarvis.tasks.runner approve 12 always    and remember it: session or always, exact (default) or kind

Command tasks (kind "command", jarvis.tasks.command_jobs) are shell commands run with no model and no task slot while
they work: the voice shell's start_job (POST /commands), audio files being made, and each run of a scheduled command.
The runner looks at each every few seconds, stops one past its time limit, and says how it went as it ends: a success
summed up, a failure read by a model that says why and decides whether a task fixes it at once, waits for the user's yes
(the command task waits with that question; a reply is the yes, a stop the no), or not; a fixed command runs once more
(_fixed), as a command task of its own that says what failed, what was fixed and how it went. They keep running while
the runner restarts.

Connection tasks (kind "connection", jarvis.tasks.connection_jobs) are calls on a connection still running after the
voice brain's wait, carried on in the background by the relay making them (jarvis.integrations.mcp_relay), which
starts each (POST /connections) and reports its progress and its end (POST /connections/ID). They take no task slot,
stop like any task, and are said as they end, summed up.

MAX_TASKS run at once and the rest queue. By default a code_change task works in place: in
the project's own folder, on whatever is checked out, leaving its changes uncommitted. Only
one runs at a time per project (the rest queue), and each run first saves any uncommitted
work there under refs/jarvis/, where git gc won't remove it. Projects marked
in_place false in the project list opt out: each of their code_change tasks gets its own git
worktree and branch, and when a run ends the runner commits whatever it changed there (it
never pushes). JARVIS's own repo always works that way (projects.is_jarvis). POST
/tasks/ID/merge merges such a branch into the project's checked-out branch, then removes
the task's worktree and deletes the branch; POST /tasks/ID/discard throws a finished task's
work away instead, removing the worktree and deleting the branch unmerged. A folder that isn't a git repo yet (projects registered with project_register
start that way) can only take in-place tasks, and their changes are reported as the files
created, modified or deleted.
POST /projects registers a project and POST /projects/git sets up git for one (jarvis.tasks.projects).
Tasks can't write JARVIS's own data files, so they suggest edits to them instead (jarvis.tasks.data_edits),
kept on the task; POST /tasks/ID/edits applies or dismisses them when JARVIS decides to.
A task that says it's finished is checked against what it was asked first (jarvis.tasks.goal): one short model call,
which can send it back, in its own session, to finish what it can, a few times at most, before it's announced. The
same check settles the to-do list: the item a task was started for is ticked off once it's finished, or a rewording to
what's left suggested, and open items it finishes unasked are suggested for ticking off (_settle_todos).
Updates go out as events on GET /events: the voice agent speaks them, and the runner shows
a Mac notification as well, plus a push to the phone when JARVIS_PUSH is on (jarvis.notify.push).
The runner also runs the heartbeat (jarvis.tasks.heartbeat), which flags new mail, meetings,
reviews and stuck tasks every few minutes, the same ways (a running task gone quiet for a while is one: the runner
keeps each running task's pulse and shows a stall as `stalled` in its task views, jarvis.tasks.stall), and the
scheduler (jarvis.tasks.scheduler), which says reminders, starts scheduled tasks and checks "watch this for me" loops when they're due. And it runs the learning
review (jarvis.tasks.learning), which reads each conversation once it's over (POST /learn says a voice session ended)
and keeps what it suggests saving to memory as a review task's suggested edits.

A task can be granted access beyond its kind's (jarvis.tasks.access) when it starts or on a reply. One that asks for
more with request_access waits, its question saying what for; the reply that answers it can grant it, and the task
carries on in the same session. A running task granted more is resumed with it once its current turn ends.

Tasks run commands on the Mac itself through the worker's host tools (jarvis.tasks.worker). A risky one waits for the
user's yes: the runner keeps the request in tasks.db (approvals), batching what a task asks
within a few seconds into one, and announces it as an "approval" event, which the voice agent
asks about at the next pause, a Mac notification, and a push with Approve and Deny buttons in the
JARVIS app. The push carries a one-time nonce, which answers it through jarvis.server; only its hash
is kept. The web page's control panel answers it too, through jarvis.server with the passcode (via "panel"). The user can also tell the assistant to answer it later (jarvis.safety.approvals); that answer
names the task and the request's exact commands, and the user's words go in the task's log.
POST /approvals/ID answers a request (the first answer wins), and the task hears it: at
its next tool call if it's running, or resumed if it was waiting for nothing else. A yes lets the
identical command run once. A yes can also be remembered (jarvis.safety.remembered): for the task's session or
always, as rules made from the commands themselves; Reporter.cleared lets a command they cover run without asking,
and a new rule approves the requests still waiting that it covers. A request lapses after JARVIS_TASK_APPROVAL_HOURS. It's withdrawn, and
never asked about again, once nothing waits on it: when the task finishes, fails, is stopped or
discarded, or the user's reply sends it off on a new run. A yes by voice or through the assistant goes for every
request of that task still waiting (whole_task), so a task isn't asked about twice.

The user's own words, sent with a reply or a new task when they order what a risky command does ("merge and push",
"restart now": jarvis.safety.commands.ordered), are kept with the task (`orders`) until it finishes: a command they
order runs without asking (Reporter.cleared), and a request still waiting that they order is approved. Only the voice
brain sends them, from what the user said this turn (the conversation's heard: jarvis.state.conversation), never from a model's words.

launchd's stop (SIGTERM) shuts the runner down cleanly (Runner.shutdown): no new work, and each task queued or running
is noted as interrupted, with any command it was running on the Mac right then, and stopped unannounced. The next start
resumes up to RESUME_TASKS of them in their own sessions (_after_restart), counting running ones left by a runner that
died without a clean shutdown; one cut off mid-command on the Mac or waiting on the user stops instead, to carry on
when the user replies.

The API listens on a Unix socket in data/ (mode 600), not a TCP port, so no web page open
in a browser on this Mac can reach it, and the host profile keeps tasks' commands off it.

Its parts: approvals (jarvis.tasks.approvals), command and connection tasks (jarvis.tasks.command_tasks), git
(jarvis.tasks.git_ops), suggested edits (jarvis.tasks.task_edits), events and announcing (jarvis.tasks.events), and the
HTTP API (jarvis.tasks.api). Here: starting, replying to and stopping tasks, running them, and the runner's own life.
"""

import asyncio
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

from jarvis.brain import retry
from jarvis.core import profile
from jarvis.core.config import (
    GOAL_DAILY_CHECKS,
    HEARTBEAT_ENABLED,
    HEARTBEAT_MINUTES,
    LEARNING_ENABLED,
    MAX_TASKS,
    PUSH_ENABLED,
    RESUME_TASKS,
    RUNNER_SOCKET,
    SCHEDULER_ENABLED,
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
    heartbeat,
    learning,
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
from jarvis.tasks.worker import Outcome, run

# Moved to the runner's parts, and still importable from here
from jarvis.tasks.approvals import NotAllowed, approval_result  # noqa: F401
from jarvis.tasks.events import bell_entry  # noqa: F401

log = logging.getLogger("jarvis.tasks.runner")
# The worker allows web, to-do and reporting tools outright, on purpose; everything else meets the gate.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)
STARTING = "starting"  # a running task's activity until it shows what it's doing
STEP_SECONDS = 1.0  # how often the tasks' steps (each tool call), kept in memory, are written
# A shutdown (launchd's SIGTERM) gives the tasks it cuts off this long to stop, well within launchd's 20 seconds
SHUTDOWN_SECONDS = 10.0
RESTARTED = "JARVIS restarted while it was running. Reply to carry on."
# What a task cut off by a shutdown hears when the next start resumes it, in its own session
RESUMED = ("JARVIS restarted while you were working on this task, cutting your last step off. Carry on with the task "
           "from where you were: first check what that step left behind, as it may not have finished, then end with "
           "your summary of the whole task as before.")
RESUMED_LOG = "resumed after JARVIS restarted"  # the task's log line as it resumes


class Runner(Approvals, CommandTasks, GitOps, TaskEdits, Events):
    """The task runner. Its parts are mixed in (see the module's docstring); here are its own state, a task's start,
    replies and stop, its run, and the runner's restarts and shutdown."""

    def __init__(self, capacity: int = MAX_TASKS) -> None:
        self.store = Store()
        self.capacity = capacity  # tasks running at once
        self.slots = asyncio.Semaphore(capacity)
        self.jobs: dict[int, asyncio.Task] = {}
        self.in_line: set[int] = set()  # tasks holding a slot or taking the next free one, ahead of any launched now
        self.begun: set[int] = set()  # tasks whose run has begun: access granted now reaches them from their next turn
        self.folders: dict[str, asyncio.Lock] = {}  # per project: one change to its folder at a time
        self.snapshots: dict[int, dict] = {}  # in-place tasks in folders without git: the files before
        self.projects_lock = asyncio.Lock()  # one change to the project list or a project's git at a time
        self.listeners: set[asyncio.Queue] = set()
        self._bg: set[asyncio.Task] = set()
        self.scheduler: scheduler.Scheduler | None = None  # set by serve(), when the scheduler is on
        self.learning: learning.Learning | None = None  # set by serve(), when the learning review is on
        self.pulse = stall.Pulse()  # when each running task last showed a sign of life
        self.commands = command_jobs.Runs()  # command tasks' commands
        self.ending: dict[int, asyncio.Task] = {}  # command tasks whose end is being said, summed up or looked into
        self.host_jobs = TaskJobs()  # tasks' long jobs on the Mac, and the sessions their host_run commands ran in
        self.host_jobs.on_end = self._job_ended
        self.checks = goal.Budget(GOAL_DAILY_CHECKS)  # goal checks across every task, in any 24 hours
        self.prompts: dict[int, str] = {}  # what each task queued or running was launched with
        self.services: list[asyncio.Task] = []  # the loops that start work of their own, stopped first at a shutdown
        self.closing = False  # set as a shutdown begins: no new work
        self.resuming = self._after_restart()

    def _after_restart(self) -> list[tuple[int, str]]:
        """Settle the tasks the runner's last run left queued or running (waiting ones carry on when answered; one that
        began waiting before wait_kind was kept gets one). Up to RESUME_TASKS of them, of any kind, are returned,
        running ones first, with the prompt each carries on with, for start_background to resume; the rest stop, to
        carry on when the user replies. So do ones a clean shutdown cut off while running a command on the Mac, ones
        waiting on the user, and, when the runner died without a clean shutdown, queued ones (what they were launched
        with is unknown) and any that died again before doing anything since they were last resumed."""
        resuming, cut = [], []
        for t in self.store.active():
            if t["kind"] in (COMMAND, CONNECTION):  # run apart from the runner: check_commands, check_connections
                continue
            if t["status"] != "waiting":
                cut.append(t)
            elif not t["wait_kind"]:
                self.store.set(t["id"], wait_kind=self._wait_kind_before(t))
        for t in sorted(cut, key=lambda t: t["status"] != "running"):
            why, prompt = self._resumable(t)
            if not why and len(resuming) >= RESUME_TASKS:
                why = f"no more than {RESUME_TASKS} resume at once (JARVIS_RESUME_TASKS)"
            if not why:
                resuming.append((t["id"], prompt))
                continue
            self.store.log(t["id"], "interrupted", f"not resumed on its own: {why}")
            # A hidden run (a watch's check) is left unsaid: the watch checks again when it's next due
            self.store.set(t["id"], status="stopped", activity=None, interrupted=None,
                           error=None if t["hidden"] else RESTARTED)
        return resuming

    def _resumable(self, task: dict) -> tuple[str, str]:
        """Why a task the runner's last run left queued or running can't resume on its own ("" if it can), and the
        prompt it carries on with if it can."""
        # No clean shutdown noted it (a crash, a kill, or a runner older than the shutdown): a running task still
        # carries on in its session, unless it died again before logging anything since it was last resumed
        cut = json.loads(task["interrupted"] or "null") or {}
        if task["hidden"]:
            return "a watch's check, made again when it's next due", ""
        if not cut and [e["text"] for e in self.store.history(task["id"], limit=1)] == [RESUMED_LOG]:
            return "it was resumed after the last restart, and the runner stopped again without a clean shutdown", ""
        if cut.get("on_mac"):
            return f"it was cut off running {few(cut['on_mac'])} on the Mac", ""
        if self.store.approvals(task["id"]):
            return "it's waiting for a go-ahead", ""
        if task["question"] or task["wants"]:
            return "it's waiting for an answer", ""
        if task["status"] == "running" and task["session_id"]:
            return "", RESUMED
        if prompt := cut.get("prompt"):  # it hadn't got going: launched again as it was
            return "", prompt
        return "it has nothing to carry on with" if cut else "the runner stopped without a clean shutdown", ""

    def _wait_kind_before(self, task: dict) -> str:
        """What a task that began waiting before wait_kind was kept waits for: the access it asked for, its requests
        if its question is the one the runner put for them, or else an answer to its own question."""
        if task["wants"]:
            return WAIT_ACCESS
        pending = self.store.approvals(task["id"])
        return WAIT_APPROVAL if pending and task["question"] == approval_question(pending) else WAIT_QUESTION

    # ── Reporter, called by worker.run ─────────────────────
    # (and propose, in jarvis.tasks.task_edits; cleared and request_approval, in jarvis.tasks.approvals)

    def session(self, task_id: int, session_id: str) -> None:
        self.store.set(task_id, session_id=session_id)

    def alive(self, task_id: int, msg: object = None) -> None:
        self.pulse.saw(task_id, msg)

    def activity(self, task_id: int, text: str) -> None:
        self.store.step(task_id, text)  # in memory: _write_steps writes them

    def progress(self, task_id: int, text: str) -> None:
        self.store.log(task_id, "progress", text)
        self._emit("progress", self.store.update(task_id, activity=text), text)

    def subagent(self, task_id: int, text: str) -> None:
        self.store.log(task_id, "subagent", text)  # logged, not announced: nothing is broadcast
        self.store.set(task_id, activity=text)

    def ask(self, task_id: int, question: str) -> None:
        self.store.log(task_id, "question", question)
        self.store.set(task_id, question=question, wait_kind=WAIT_QUESTION)

    def flag(self, task_id: int, text: str) -> None:
        """Something the task's run left out as hidden instructions (jarvis.safety.scan): in its log, the bell and a
        Mac notification, for the user to hear about."""
        self.store.log(task_id, "flagged", text)
        bell.add(f"Task {task_id}: {text}", "warn", task_id)
        self._background(self._notify(f"JARVIS · task {task_id}", text[:200]))

    def take_nudges(self, task_id: int) -> list[str]:
        messages = self.store.take_nudges(task_id)
        for text in messages:
            self.progress(task_id, f"nudge received: {text}")
        return messages

    def request_access(self, task_id: int, names: list[str], reason: str) -> list[str]:
        """Access the task asks for (jarvis.tasks.access), put to JARVIS as its question: it waits, as for any, and
        a reply granting it (or not) resumes it. Returns what it asked for; raises ValueError if it has it all."""
        task = self.store.get(task_id)
        if not (wants := access.new(task, names)):
            raise ValueError("this task has that access already, so use it")
        reason = " ".join(reason.split())[:300]
        self.store.log(task_id, "access", f"asks for {', '.join(wants)}: {reason}")
        self.store.set(task_id, question=f"{ACCESS_QUESTION}{access.describe(wants)}? {reason}".strip(),
                       wait_kind=WAIT_ACCESS, wants=json.dumps(wants))
        return wants

    # ── Alongside the tasks, and shutting down ──────────────────

    async def _write_steps(self, every: float = STEP_SECONDS) -> None:
        """Write the tasks' latest steps every so often, on a thread: one transaction however many tool calls they
        made, and the event loop never waits on the database for them."""
        while True:
            await asyncio.sleep(every)
            await asyncio.to_thread(self.store.write_steps)

    def _job_ended(self, job: dict, code: int) -> None:
        """A task's job ended: it goes in the task's log."""
        if (task_id := job.get("task")) is None or not self.store.get(task_id):
            return
        self.store.log(task_id, "job", f"job {job['id']}, {job['title']}, {host.outcome(code)}")

    def start_background(self) -> None:
        """What runs alongside the tasks: lapsing old requests, and announcing any a restart cut short; and looking after
        the command tasks."""
        self._background(self._expire_approvals())
        self._background(self._watch_commands())
        self._background(self._write_steps())
        for approval in self.store.approvals():
            if not approval["announced"]:
                self._background(self._announce_approval(approval["id"]))
        for task_id, prompt in self.resuming:
            self.store.log(task_id, "interrupted", RESUMED_LOG)
            bell.add(f"Resumed task {task_id} after JARVIS restarted: {self.store.get(task_id)['title']}", "run", task_id)
            self._launch(task_id, prompt)
        self.resuming = []

    def service(self, coro) -> None:
        """Run one of the loops that start work of their own (heartbeat, scheduler, learning review)."""
        self.services.append(self._background(coro))

    async def shutdown(self, wait: float = SHUTDOWN_SECONDS) -> None:
        """launchd's stop: take no new work, and note each task queued or running as `interrupted`, with the commands
        it was running on the Mac, if any, and what it was launched with; then stop their sessions, saying nothing.
        The next start resumes what it can (_after_restart)."""
        self.closing = True
        for loop in self.services:
            loop.cancel()
        cut = dict(self.jobs)
        for task_id in cut:
            on_mac = self.host_jobs.on_mac.get(task_id, [])
            self.store.set(task_id, interrupted=json.dumps(
                {"at": time.time(), "on_mac": on_mac, "prompt": self.prompts.get(task_id)}, ensure_ascii=False))
            self.store.log(task_id, "interrupted", "JARVIS is shutting down" + (
                f", while it ran {few(on_mac)} on the Mac" if on_mac else ""))
        for job in cut.values():
            job.cancel()
        if cut:
            await asyncio.wait(cut.values(), timeout=wait)
        self.store.write_steps()

    # ── Commands ────────────────────────────────────────────────

    def start(self, kind: str, title: str, instruction: str, project: str | None = None, job: int | None = None,
              hidden: bool = False, grants: list[str] | None = None, order: str | None = None,
              fixes: int | None = None, todo: str | None = None) -> dict:
        """A new task. The scheduler's runs name their `job`; a watch's checks start `hidden`, out of the lists and
        the bell, until one has something to say. `grants` is access beyond its kind's (jarvis.tasks.access). `order`
        is the user's own words, which may order what it then does without asking (_order). A task fixing a command
        task that failed names it in `fixes`. `todo` is the open to-do item it works on (its text, or a part found only
        in it), settled as it ends (jarvis.tasks.goal). A command task starts with start_command instead."""
        self._open()
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}"
                             + (": a command starts with start_job, not as a task" if kind == COMMAND else ""))
        if not instruction.strip():
            raise ValueError("instruction is empty")
        grants = [n for n in access.check(grants) if n not in access.defaults(kind)]
        name, in_place = None, False
        if kind != "research":
            if not project:
                raise ValueError(f"A {kind} task needs a project.")
            found = projects.find(project)
            name, in_place = found.name, found.in_place and kind == "code_change"
            if kind == "code_change" and not in_place and (problem := projects.worktree_problem(found)):
                raise ValueError(problem)
        item = todos.find(todo) if todo else None
        title = " ".join(title.split())[:80] or instruction[:60]
        task = self.store.create(kind, title, instruction, name, in_place, job, hidden, grants, fixes=fixes,
                                 todo=item and item["id"])
        self.store.log(task["id"], "start", instruction)
        if item:
            self.store.log(task["id"], "todo", f"works on the to-do: {item['text']}")
        if grants:
            self.store.log(task["id"], "access", f"granted {', '.join(grants)} from the start")
        self._order(task["id"], order)
        if not hidden:
            started = "Started scheduled task" if job is not None else "Started task"
            bell.add(f"{started} {task['id']}: {title}" + (f", on {name}" if name else ""), "run", task["id"])
        return self._launch(task["id"], instruction)

    def reply(self, task_id: int, message: str, grants: list[str] | None = None, order: str | None = None) -> dict:
        """A message for the task, granting it `grants` as well (jarvis.tasks.access): a running one gets them from
        its next turn, which it's resumed for once this one ends (_job); anything else from the run this starts.
        `order` is the user's own words, which may order what it does without asking (_order): requests waiting that
        they order are approved, rather than withdrawn and asked about again."""
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
        if not running and task["worktree"] and not Path(task["worktree"]).exists():
            folder = "project folder" if task["in_place"] else "worktree"
            raise ValueError(f"Task {task_id}'s {folder} has been removed, so it can't carry on.")
        note = ""
        if gained := access.new(task, grants):
            self.store.set(task_id, grants=access.add(task, gained))
            self.store.log(task_id, "access", f"granted {', '.join(gained)}")
            note = f"\n\nYou now also have {access.listing(gained)}."
            if task_id in self.begun:  # else its run begins with it
                note += (" It takes effect from your next turn: once you reach what needs it, end your turn with one "
                         "short line, and you'll carry on in this session with it.")
        self._order(task_id, order)
        if running:
            # A nudge: a queued task gets it with its prompt, a running one after its current tool call
            self.store.log(task_id, "nudge", message)
            self.store.add_nudge(task_id, message + note)
            self._apply_orders(task_id)
            return self.store.get(task_id)
        lead = "Answer" if task["status"] == "waiting" else "Follow-up"
        prompt = f"{lead} from {profile.name()}: {message}{note}"
        if not task["session_id"]:  # it never got going, so start over with the context
            prompt = f"{task['instruction']}\n\n{prompt}"
        self.store.log(task_id, "reply", message)
        self._launch(task_id, prompt)
        # What it asked for before is no longer waited on: the new run goes by the new words, and hears so. What
        # those words order is approved, and the run hears that with them.
        self._apply_orders(task_id)
        self._close_approvals(task_id, "re-queued")
        return self.store.get(task_id)

    async def halt(self, task_id: int) -> dict:
        """Stop a task: a command task's running command as `stop_command` does, anything else as `stop` does."""
        task = self._get(task_id)
        if task["kind"] == COMMAND and task["status"] == "running":
            return await self.stop_command(task_id)
        return self.stop(task_id)

    def stop(self, task_id: int) -> dict:
        task = self._get(task_id)
        if task["kind"] == COMMAND and task["status"] == "running":
            raise ValueError(f"Task {task_id} is running a command: stop it with halt.")
        if stopped := self.host_jobs.stop_all(task_id):  # its long commands on the Mac stop with it
            self.store.log(task_id, "job", f"stopped job{'s' if len(stopped) > 1 else ''} {', '.join(map(str, stopped))}")
        if job := self.jobs.get(task_id):
            job.cancel()  # _job records the stop and commits any work
        elif task["kind"] == CONNECTION and task["status"] == "running":  # its relay cancels the call when it hears
            self._finish(task_id, "stopped", f"Task {task_id}, {task['title']}, stopped.")
        elif task["kind"] == COMMAND and task["status"] == "waiting":  # it asked whether to fix a failure: no
            self.store.log(task_id, "declined", f"{profile.name()} said to leave it unfixed")
            self._finish(task_id, "failed", f"Task {task_id}, {task['title']}, is left as it failed: no fix started.",
                         seen=1, question=None, wait_kind=None)
        elif task["status"] == "waiting":
            self._finish(task_id, "stopped", announce.plain(announce.Ending(task["title"], announce.STOPPED,
                                                                           suggested=announce.suggestions(task))))
        else:
            raise ValueError(f"Task {task_id} isn't running.")
        return self.store.get(task_id)

    # ── Stalls (jarvis.tasks.stall) ─────────────────────────────

    def view(self, task: dict, now: float | None = None) -> dict:
        """A task as the API shows it: with `stalled` (how long it has gone quiet) if it's running and seems stuck. A
        command task's `command` says how it runs and how it went, with the end of its output."""
        if task["kind"] == COMMAND:
            return task | command_jobs.view(task, self.commands, now) | {"stalled": None}
        if task["kind"] == CONNECTION:  # its relay keeps no pulse here
            return task | {"stalled": None}
        return task | {"stalled": stall.check(task, self.store, self.pulse, self.host_jobs, now)}

    def stalls(self, now: float | None = None) -> list[dict]:
        """The running tasks that seem stuck, oldest first, each with its `stalled`."""
        return [t for t in (self.view(t, now) for t in self.store.active()) if t["stalled"]]

    # ── Running ─────────────────────────────────────────────────

    def _launch(self, task_id: int, prompt: str) -> dict:
        """Run the task: running at once when nothing holds it back, else queued, its activity saying what it waits
        on. What it last said is cleared: it's out of date once it carries on."""
        self._open()
        task = self.store.get(task_id)
        if ahead := self._ahead_in_folder(task):
            ids = " and ".join(map(str, ahead))
            status, activity = "queued", (f"queued behind task {ids}: this project's tasks work in its own folder, so "
                                          "they run one at a time")
        elif task["in_place"] and self._folder_lock(task["project"]).locked():
            status, activity = "queued", "waiting for its project's folder, which a merge or git setup is using"
        else:
            status, activity = ("running", STARTING) if self._slot_free(task_id) else ("queued", self._slot_wait())
            self.in_line.add(task_id)
        task = self.store.update(task_id, status=status, question=None, wait_kind=None, wants=None, error=None,
                                 spoken=None, activity=activity, interrupted=None)
        self.prompts[task_id] = prompt
        job = self.jobs[task_id] = asyncio.create_task(self._job(task_id, prompt))
        job.add_done_callback(lambda _: self._never_began(task_id, job))
        return task

    def _never_began(self, task_id: int, job: asyncio.Task) -> None:
        """A job stopped before its first step skips its own clean-up and ending (_job), so they happen here."""
        if self.jobs.get(task_id) is not job:  # it began, and cleaned up itself
            return
        self.jobs.pop(task_id)
        self.prompts.pop(task_id, None)
        self.in_line.discard(task_id)
        if self.closing:  # cut off by a shutdown: left as it is, to resume (_after_restart)
            return
        self._finish(task_id, "stopped", announce.plain(announce.Ending(self.store.get(task_id)["title"],
                                                                       announce.STOPPED)))

    def _slot_free(self, task_id: int) -> bool:
        """Whether the task would get a slot now: fewer than `capacity` others hold one or are in line for one."""
        return len(self.in_line - {task_id}) < self.capacity

    def _slot_wait(self) -> str:
        return f"waiting for a free slot: {self.capacity} {'task runs' if self.capacity == 1 else 'tasks run'} at once"

    def _ahead_in_folder(self, task: dict) -> list[int]:
        """Other in-place tasks on the same project that are running or queued."""
        if not task["in_place"]:
            return []
        return [i for i in self._jobs_on(task["project"], in_place=True) if i != task["id"]]

    def _folder(self, task: dict):
        """Held while an in-place task runs, so two never edit the same folder at once."""
        return self._folder_lock(task["project"]) if task["in_place"] else nullcontext()

    async def _job(self, task_id: int, prompt: str) -> None:
        outcome = None  # stays None when the task is stopped
        had: set[str] | None = None  # the access its run began with
        try:
            task = self.store.get(task_id)
            if task["kind"] == "code_change" and not task["in_place"] and not task["worktree"]:
                await self._make_worktree(task)
            async with self._folder(task):
                if task_id not in self.in_line:  # it waited for its folder: now it's in line for a slot
                    if not self._slot_free(task_id):
                        self.store.set(task_id, activity=self._slot_wait())
                    self.in_line.add(task_id)
                async with self.slots:
                    if task["in_place"]:
                        await self._enter_folder(task)
                    task = self.store.update(task_id, status="running", activity=STARTING)
                    had = access.has(task)
                    self.begun.add(task_id)
                    if early := self.take_nudges(task_id):
                        prompt = with_nudges(prompt, early, started=bool(task["session_id"]))
                        if not task["session_id"]:  # its first run: the messages become part of the brief
                            task = self.store.update(task_id, instruction=with_nudges(task["instruction"], early,
                                                                                      started=False))
                    began = time.time()
                    try:
                        outcome = await run(task, prompt, self)
                    finally:
                        self._spend_approvals(task_id, before=began)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("task %s failed", task_id)
            outcome = Outcome("", f"{type(e).__name__}: {e}"[:200], 0, retry.classify(e))
        finally:
            self.jobs.pop(task_id, None)
            self.prompts.pop(task_id, None)
            self.in_line.discard(task_id)
            self.begun.discard(task_id)
            self.pulse.forget(task_id)
        if self.closing:  # cut off by a shutdown: left as it is, to resume (_after_restart)
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
        if outcome and not outcome.error and not task["question"] and not self.store.approvals(task_id):
            if gained:  # granted while it ran: it carries on in a session that has it
                more = (f"You now have {access.listing(gained)}. Carry on with the task, using it where it's needed, "
                        "then end with your summary of the whole task as before.")
            elif not nudges(self.store.get(task_id)):  # it says it's finished: is it?
                try:
                    reading = await scheduler.read(task, outcome.result)  # a scheduled run's: met, or nothing new
                    check, more = await self._check_goal(task, outcome.result, fields, reading.nothing_new)
                except asyncio.CancelledError:
                    if self.closing:
                        self.store.set(task_id, **fields)
                        return
                    outcome = None  # stopped while it was being checked
        undelivered = False
        if nudges(self.store.get(task_id)):  # sent after the session last checked
            if outcome and not outcome.error and not task["question"]:  # so it carries on with them
                more = nudge_text(self.take_nudges(task_id), finished=True) + (f"\n\n{more}" if more else "")
            else:
                undelivered = True  # said, unless it asks a question: then it goes with the answer
        if more:  # it carries on, unannounced: with those messages, or sent back to finish
            self.store.set(task_id, **fields)
            self._launch(task_id, more)
            return
        left, needs_user = goal.left(check)

        async def said(status: str, report: str = "", lead: str = "", done: list[str] | None = None) -> str:
            """How this end is announced (jarvis.tasks.announce), with the task's edits still pending."""
            return await announce.spoken(announce.Ending(
                task["title"], status, report, lead, left, needs_user, done or [],
                announce.suggestions(self.store.get(task_id)), undelivered))

        if task["fixes"] and outcome and not outcome.error and not task["question"] \
                and not self.store.approvals(task_id):
            # A fix for a command task that failed: when it says the command should now succeed, the command runs
            # again, and that run says it all
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
            # A scheduled run says its result under the job's title, or nothing at all
            if verdict.summary is not None:  # without the notepad it kept for its next run
                fields["summary"] = verdict.summary
            status = "failed" if outcome.error else "done"
            quiet = verdict.quiet and not data_edits.pending(task) and not undelivered
            spoken = verdict.spoken if quiet else await said(status, lead=verdict.spoken)
            self._finish(task_id, status, spoken, quiet=quiet, title=verdict.title,
                         hidden=int(quiet), **({"seen": 1} if quiet else {}),
                         **({"error": outcome.error} if outcome.error else {}), **fields)
            return
        if task["hidden"]:
            fields["hidden"] = 0  # a watch's check that needs the user after all
        if outcome is None:
            self._finish(task_id, "stopped", await said(announce.STOPPED), **fields)
        elif outcome.error:
            self._finish(task_id, "failed", await said(announce.FAILED, outcome.error), error=outcome.error, **fields)
        elif task["question"]:  # its edits are mentioned once it's finished
            self._finish(task_id, "waiting", await announce.spoken(
                announce.Ending(task["title"], announce.QUESTION, task["question"])), **fields)
        elif pending := self.store.approvals(task_id):  # already announced: the wait itself goes unsaid
            self._finish(task_id, "waiting", f"Task {task_id}, {task['title']}, is waiting for your go-ahead to run "
                                             f"{' and '.join(approval_summary(a['commands']) for a in pending)}.",
                         question=approval_question(pending), wait_kind=WAIT_APPROVAL, quiet=True, **fields)
        else:
            done = await self._settle_todos(task, check)
            self._finish(task_id, "done", await said(announce.DONE, outcome.result, done=done), **fields)

    async def _check_goal(self, task: dict, result: str, fields: dict,
                          nothing_new: bool = False) -> tuple[goal.Check | None, str | None]:
        """Check a run that says it's finished against what the task was asked (jarvis.tasks.goal), and log the
        verdict: the check (None if it wasn't made), and the message sending it back to finish, if it goes back. The
        check's cost goes in `fields`. Stopping the task meanwhile raises CancelledError. `nothing_new`: a scheduled
        run with nothing new to report, which isn't checked."""
        task_id = task["id"]
        if goal.skipped(task, nothing_new):
            return None, None
        entries = self.store.history(task_id, goal.LOG_ENTRIES, steps=True)
        self.store.set(task_id, activity="checking its result against what it was asked")
        self.jobs[task_id] = asyncio.current_task()  # so stopping it stops the check
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


async def serve() -> None:
    runner = Runner()
    app = api.app(runner)
    # No TCP keep-alive on a Unix socket: it does nothing there, and on macOS setting it on a connection whose client
    # has already hung up fails with EINVAL, logged as an asyncio callback error. The event streams never end, so a
    # shutdown closes them after a second rather than waiting for them.
    site_runner = web.AppRunner(app, access_log=None, tcp_keepalive=False, shutdown_timeout=1)
    await site_runner.setup()
    RUNNER_SOCKET.unlink(missing_ok=True)
    old = os.umask(0o077)  # the socket is born mode 600
    try:
        await web.UnixSite(site_runner, str(RUNNER_SOCKET)).start()
    finally:
        os.umask(old)
    log.info("task runner on %s, %d at once", RUNNER_SOCKET, MAX_TASKS)
    if runner.resuming:
        log.info("resuming tasks %s, cut off by the last shutdown", ", ".join(str(i) for i, _ in runner.resuming))
    runner.start_background()
    if HEARTBEAT_ENABLED:
        runner.service(heartbeat.make(runner.store, runner._notify, runner.broadcast, push=runner._push,
                                      stalls=runner.stalls).run())
        log.info("heartbeat every %d minutes", HEARTBEAT_MINUTES)
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
    for sig in (signal.SIGTERM, signal.SIGINT):  # launchd's stop, and Ctrl-C as make dev stops it
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
    """'artifacts,mac' on the command line, as a list."""
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
