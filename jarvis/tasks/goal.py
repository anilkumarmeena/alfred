"""The goal loop: a background task that says it's finished is checked against what it was asked before it's announced.

When a run ends with a result (not stopped, failed, asking a question or waiting for a go-ahead), the runner asks
`review` for a verdict: one short call to GOAL_MODEL, with no tools, that reads the instruction and the user's later
messages, the task's summary, and the evidence of what it did: for a code_change, its diff and the builds, tests and jobs
in its log. It replies in JSON (SCHEMA), its verdict one of

  complete    everything asked for is done and backed up (or what's left is only what the user must decide)
  incomplete  something the task could do itself is missing or unverified: it's sent back, in the same session (as a
              reply would be), with what to finish
  blocked     what's left needs the user's decision, access, credentials or a tool the task can't get: reported, never
              sent back

A task is sent back at most GOAL_ROUNDS times for each thing the user asked (its start, and each reply), and, when
GOAL_MAX_COST is set, never once it has cost that many dollars; the last round tells it to say plainly what's done and
what isn't. Only the final result is announced, with what the check still found missing; every check goes in the task's
log as a "goal" entry, so task_detail and task_log show them. A check that can't be made (off, the call failing,
GOAL_DAILY_CHECKS reached, a reply that isn't JSON matching SCHEMA) lets the result through as it is.

Not checked: a watch's checks, whose met or not yet is the whole answer, and scheduled runs found to have nothing new
to report (jarvis.tasks.scheduler.read), which come often and are what the instruction asks for when nothing's new.

The check settles the to-do list too. A task started for one of the user's to-do items (task_start's `todo`) has that
item put to the check, which says whether the work finishes it or what's left of it (its reply's `todos`); a task the
user asked for without one has their open items put to it instead, and it names any the work clearly finishes.
`todo_changes` turns that into the task's suggested edits (jarvis.tasks.data_edits): its own item is ticked off
at once, a part done is a rewording put to the user, and an item it wasn't started for is a tick-off put to them, each
said in its announcement. The model decides what matches; nothing matches on words.
"""

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from jarvis import prompts
from jarvis.core import proc, profile
from jarvis.core.config import GOAL_LOOP, GOAL_MAX_COST, GOAL_MODEL, GOAL_ROUNDS
from jarvis.safety.commands import SECRET_PATTERNS
from jarvis.state import todo
from jarvis.tasks import schedules

log = logging.getLogger("jarvis.tasks.goal")

COMPLETE, INCOMPLETE, BLOCKED = "complete", "incomplete", "blocked"
KIND = "goal"  # its entries in the task's log
ROUND = "checked, round "  # how an entry that sent the task back opens
CHECK_TIMEOUT = 120
LOG_ENTRIES = 1000  # how much of a task's log, steps included, the check looks through
# The most of each part the check reads: one short call, however much the task did
MOST = {"instruction": 6000, "messages": 3000, "summary": 8000, "diff": 12000, "log": 3000}
SAID_MOST = 300  # what's said aloud of what's missing
OPEN_TODOS = 40  # the most open to-dos put to the check of a task not started for one
# Steps in a task's log that look like building, testing or checking its work
_CHECKING = re.compile(r"\b(tests?|pytest|unittest|jest|vitest|playwright|build|make|lint|ruff|mypy|pyright|tsc|"
                       r"typecheck|compileall|xcodebuild|swift|cargo|gradle|npm|pnpm|yarn|go (?:test|build|vet))\b", re.IGNORECASE)
# Never sent to the check, even in a task's own diff
_NO_SECRETS = [f":(exclude,glob){p}" for p in (*SECRET_PATTERNS, "**/secrets/**")]


def system() -> str:
    return prompts.load("goal_check", user_name=profile.name())


# The check's reply: its verdict, a sentence on it, what to finish (for incomplete) and its word on each to-do item
SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"enum": [COMPLETE, INCOMPLETE, BLOCKED]},
        "said": {"type": "string", "minLength": 1},
        "to_finish": {"type": "array", "items": {"type": "string"}},
        "todos": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "done": {"type": "boolean"}, "left": {"type": "string"}},
            "required": ["id", "done"],
        }},
    },
    "required": ["verdict", "said"],
}


@dataclass
class Check:
    verdict: str  # COMPLETE, INCOMPLETE or BLOCKED
    said: str  # one plain sentence: what was done, or what's missing
    details: str  # for INCOMPLETE, the list of what to finish
    cost: float
    todos: dict[int, str] = field(default_factory=dict)  # to-do item: "" when the work finishes it, else what's left


class Budget:
    """At most `most` checks in any 24 hours, across every task: a guard should a loop ever run away."""

    def __init__(self, most: int) -> None:
        self.most, self.times = most, deque()

    def take(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        while self.times and self.times[0] < now - 86400:
            self.times.popleft()
        if len(self.times) >= self.most:
            return False
        self.times.append(now)
        return True


def skipped(task: dict, nothing_new: bool = False) -> str | None:
    """Why a run that finished isn't checked, or None if it is. `nothing_new`: a scheduled run found to have nothing new
    to report (jarvis.tasks.scheduler.read), which stays silent."""
    if not GOAL_LOOP:
        return "off"
    if task["job"] is not None and (job := schedules.get(task["job"])) and job["kind"] == "watch":
        return "a watch's check"
    if nothing_new:
        return "a scheduled run with nothing to report"
    return None


def since_asked(entries: list[dict]) -> list[dict]:
    """A task's log entries since the user last asked it something: its start, or their latest reply."""
    start = max((i for i, e in enumerate(entries) if e["kind"] in ("start", "reply")), default=0)
    return entries[start:]


def rounds(entries: list[dict]) -> int:
    """How many times the check has sent the task back since the user last asked it something."""
    return sum(e["kind"] == KIND and e["text"].startswith(ROUND) for e in since_asked(entries))


def _clip(text: str, most: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= most else text[:most] + f"\n[... {len(text) - most} more characters left out]"


def _messages(entries: list[dict]) -> str:
    """The user's messages to the task after its instruction: replies, answers and nudges."""
    said = [f"- {e['text']}" for e in entries if e["kind"] in ("reply", "nudge")]
    return _clip("\n".join(said), MOST["messages"])


def _work_log(entries: list[dict]) -> str:
    """What the task did since it was last asked, as far as checking its work goes: its milestones, subagents and jobs,
    and the steps that built, tested or checked something."""
    kept = [f"{e['kind']}: {e['text']}" for e in since_asked(entries)
            if e["kind"] in ("progress", "subagent", "job", KIND) or (e["kind"] == "step" and _CHECKING.search(e["text"]))]
    return _clip("\n".join(kept[-60:]), MOST["log"])


async def _git(folder: str, *args: str) -> str:
    code, out = await proc.run_try("git", "-C", folder, *args, timeout=60)
    return out if code == 0 else ""


async def evidence(task: dict, changes: str) -> str:
    """What a code_change changed: `changes` (its shortstat, or the files a folder without git changed) and, in git, the
    files and the diff against its base, secrets left out."""
    if task["kind"] != "code_change":
        return ""
    parts = [f"Changes: {changes or 'none recorded'}"]
    folder = task["worktree"]
    if task["base"] and folder and Path(folder).exists():
        stat = await _git(folder, "diff", "--stat", task["base"], "--", ".", *_NO_SECRETS)
        untracked = await _git(folder, "ls-files", "--others", "--exclude-standard", "--", ".", *_NO_SECRETS)
        diff = await _git(folder, "diff", task["base"], "--", ".", *_NO_SECRETS)
        parts += [f"Files changed:\n{stat or 'none'}"]
        if untracked:
            parts += [f"Untracked files:\n{_clip(untracked, 1500)}"]
        parts += [f"Diff against where it began:\n{_clip(diff, MOST['diff']) or 'empty'}"]
    return "\n\n".join(parts)


def todos(task: dict) -> list[dict]:
    """The to-do items put to the check: the one the task was started for while it's open, or, for a task the user
    asked for without one (not a scheduled run or a fix), their open items."""
    if task.get("todo"):
        item = todo.get(task["todo"])
        return [item] if item and not item["done"] else []
    if task["job"] is not None or task["fixes"]:
        return []
    return [i for i in todo.items() if not i["done"]][:OPEN_TODOS]


def material(task: dict, result: str, entries: list[dict], proof: str, round_: int, items: list[dict] = ()) -> str:
    """What the check reads; `items` are the to-dos put to it (todos)."""
    kinds = {"code_change": "change code", "code_question": "answer a question about code", "research": "research"}
    where = f" in the project {task['project']}" if task["project"] else ""
    first = next((e["text"] for e in entries if e["kind"] == "start"), "") or task["instruction"]
    parts = [f"Task {task['id']}, \"{task['title']}\": a task to {kinds.get(task['kind'], task['kind'])}{where}."
             + (f" It has been sent back {round_} time{'s' if round_ > 1 else ''} already to finish." if round_ else ""),
             f"<instruction>\n{_clip(first, MOST['instruction'])}\n</instruction>"]
    if messages := _messages(entries):
        parts.append(f"{profile.name()}'s later messages to it, oldest first:\n<later_messages>\n{messages}\n</later_messages>")
    parts.append(f"<summary>\n{_clip(result, MOST['summary'])}\n</summary>")
    if proof:
        parts.append(f"<changes>\n{proof}\n</changes>")
    if work := _work_log(entries):
        parts.append(f"<log of its work, latest last>\n{work}\n</log>")
    if items:
        listed = "\n".join(f"{i['id']}. {i['text']}" for i in items)
        parts.append(f"The task was started for this item on {profile.name()}'s to-do list:\n<todo>\n{listed}\n</todo>"
                     if task.get("todo") else f"{profile.name()}'s open to-do items:\n<open_todos>\n{listed}\n</open_todos>")
    return "\n\n".join(parts)


def parse(found: dict | None, cost: float = 0.0) -> Check | None:
    """The check's reply, JSON matching SCHEMA, as a Check; None if it gave none."""
    if not found:
        return None
    details = "\n".join(f"- {line}" for line in found.get("to_finish", []))
    items = {i["id"]: "" if i["done"] else i.get("left", "").strip() for i in found.get("todos", [])}
    return Check(found["verdict"], found["said"].strip(), details, cost, items)


async def ask_model(text: str) -> tuple[dict | None, float]:
    """One short call to GOAL_MODEL with no tools: its reply, JSON matching SCHEMA (None if it isn't), and what it
    cost."""
    from jarvis.brain.sdk import ask_json

    return await ask_json(text, SCHEMA, system(), GOAL_MODEL, effort="low")


async def review(text: str, budget: Budget) -> Check | None:
    """The verdict on a finished run, or None if the check couldn't be made (which lets the result through) or
    `budget` (the runner's) has none left."""
    if not budget.take():
        log.warning("goal check skipped: %d checks in the last day already", budget.most)
        return None
    try:
        found, cost = await asyncio.wait_for(ask_model(text), CHECK_TIMEOUT)
    except Exception as e:  # the type alone: nothing of the task's reaches the log
        log.warning("goal check failed: %s", type(e).__name__)
        return None
    return parse(found, cost)


def next_step(check: Check, done: int, cost: float) -> tuple[str, str | None]:
    """What becomes of a checked run, sent back `done` times already and costing `cost` so far: its log entry, and the
    message that sends it back to finish (None when it's reported as it is)."""
    if check.verdict == COMPLETE:
        return f"checked: complete: {check.said}", None
    if check.verdict == BLOCKED:
        return f"checked: needs {profile.name()}, so it's reported, not sent back: {check.said}", None
    if done >= GOAL_ROUNDS:
        why = f"it was sent back {done} time{'s' if done != 1 else ''} already" if done else "sending back is off"
        return f"checked: incomplete, but {why}, so it's reported as it is: {check.said}", None
    if GOAL_MAX_COST and cost >= GOAL_MAX_COST:  # 0 is no limit
        return (f"checked: incomplete, but it has cost ${cost:.2f}, over the ${GOAL_MAX_COST:g} limit for sending it back, so "
                f"it's reported as it is: {check.said}"), None
    n = done + 1
    left = f"\n{check.details}" if check.details else ""
    return f"{ROUND}{n} of {GOAL_ROUNDS}: incomplete, so it was sent back to finish: {check.said}{left}", send_back(check, n)


def send_back(check: Check, round_: int) -> str:
    """The message that sends a task back to finish, in its own session."""
    left = check.details or f"- {check.said}"
    last = (" This is the last time JARVIS sends it back: if anything is still undone after this, the summary must say "
            "plainly what is done, what isn't, and why.") if round_ >= GOAL_ROUNDS else ""
    return (f"JARVIS checked your result against what {profile.name()} asked, and it isn't finished yet "
            f"(round {round_} of {GOAL_ROUNDS}). Still to do:\n{left}\n\n"
            f"Finish these now, and check your work where the project lets you (builds, tests). If one of them needs "
            f"{profile.name()}'s decision, access or a tool you can't get, don't keep trying: say so plainly. Then end with a "
            f"summary of the whole task as before.{last}")


def left(check: Check | None) -> tuple[str, bool]:
    """What the check found still missing, for the announcement ("" if nothing), and whether it waits on the user
    (BLOCKED) rather than being the task's to finish."""
    if check is None or check.verdict == COMPLETE:
        return "", False
    said = check.said if len(check.said) <= SAID_MOST else check.said[:SAID_MOST].rsplit(" ", 1)[0] + "…"
    return said, check.verdict == BLOCKED


@dataclass
class TodoChange:
    """What a finished task does to the to-do list: an edit, as propose_data_edit's arguments (jarvis.tasks.data_edits),
    made at once, or left for JARVIS, asking the user first for `needs_yes`. Its announcement says which."""
    edit: dict
    at_once: bool = False
    needs_yes: str | None = None


def _tick(text: str, reason: str) -> dict:
    return {"action": "todo_done", "text": text, "reason": reason}


def todo_changes(task: dict, check: Check | None) -> list[TodoChange]:
    """The to-do edits a task's final result calls for, by its check (None when it wasn't checked). The item it was
    started for is ticked off at once when the check finds it finished (or, saying nothing of it, the task complete),
    and a rewording to what's left is suggested when the work does part of it; unchecked, ticking it off is put to the
    user. An open item it wasn't started for that the check finds finished is a tick-off put to the user."""
    items = todos(task)
    if task.get("todo"):
        if not items:  # ticked off, or removed, meanwhile
            return []
        text = items[0]["text"]
        if check is None:
            return [TodoChange(_tick(text, f"Task {task['id']} was started for it and says it's finished."),
                               needs_yes="its result couldn't be checked, so make sure it's finished")]
        left = check.todos.get(items[0]["id"], "" if check.verdict == COMPLETE else text)
        if not left:
            return [TodoChange(_tick(text, f"Task {task['id']} was started for it, and JARVIS's check found it finished."),
                               at_once=True)]
        if left.lower() == text.lower():  # no headway on it
            return []
        return [TodoChange({"action": "todo_reword", "text": text, "item": left,
                            "reason": f"Task {task['id']} was started for it and did part of it; this is what's left."})]
    if check is None:
        return []
    listed = {i["id"]: i["text"] for i in items}
    return [TodoChange(_tick(listed[n], f"JARVIS's check found task {task['id']}'s work finishes it."),
                       needs_yes="the task wasn't started for it, but JARVIS's check found its work finishes it")
            for n, left in check.todos.items() if not left and n in listed]
