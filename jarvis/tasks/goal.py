"""Goal loop: check a finished run against what was asked before announcing it.

`review` makes one tool-less goal-role call over instruction, later messages, summary and evidence (diff, build/test
steps). Verdicts: complete; incomplete (sent back in-session, at most GOAL_ROUNDS per user ask, capped by
GOAL_MAX_COST); blocked (needs the user: reported, never sent back). Each check is logged as a "goal" entry. If the
check can't run (off, error, daily budget, bad JSON) the result passes as is. Watches and nothing-new scheduled runs
are skipped. The check also judges to-dos; `todo_changes` turns that into edits (own item ticked at once, others put
to the user).
"""

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from jarvis import prompts
from jarvis.config import GOAL_LOOP, GOAL_MAX_COST, GOAL_ROUNDS
from jarvis.core import proc
from jarvis.llm.roles import Role
from jarvis.safety.commands import SECRET_PATTERNS
from jarvis.state import todo
from jarvis.storage import profile
from jarvis.tasks import schedules

log = logging.getLogger("jarvis.tasks.goal")

COMPLETE, INCOMPLETE, BLOCKED = "complete", "incomplete", "blocked"
KIND = "goal"  # log entry kind
ROUND = "checked, round "  # prefix of entries that sent the task back
CHECK_TIMEOUT = 120
LOG_ENTRIES = 1000  # log entries (steps included) the check scans
# Char caps per input part, keeping the check one short call
MOST = {"instruction": 6000, "messages": 3000, "summary": 8000, "diff": 12000, "log": 3000}
SAID_MOST = 300  # max chars of `left` spoken
OPEN_TODOS = 40  # open to-dos shown when the task has no linked to-do
# Log steps that look like building, testing or checking
_CHECKING = re.compile(r"\b(tests?|pytest|unittest|jest|vitest|playwright|build|make|lint|ruff|mypy|pyright|tsc|"
                       r"typecheck|compileall|xcodebuild|swift|cargo|gradle|npm|pnpm|yarn|go (?:test|build|vet))\b", re.IGNORECASE)
# Excluded from the diff sent to the check
_NO_SECRETS = [f":(exclude,glob){p}" for p in (*SECRET_PATTERNS, "**/secrets/**")]


def system() -> str:
    return prompts.load("goal_check", user_name=profile.name())


# Reply: verdict, one sentence, what to finish (incomplete), and per-to-do status
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
    said: str  # one sentence: what was done or what's missing
    details: str  # INCOMPLETE: bullet list of what to finish
    cost: float
    todos: dict[int, str] = field(default_factory=dict)  # to-do id -> "" if finished, else what's left


class Budget:
    """Global cap of `most` checks per rolling 24h, guarding against a runaway loop."""

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
    """Why a finished run skips the check, or None. `nothing_new`: a silent scheduled run with nothing to report."""
    if not GOAL_LOOP:
        return "off"
    if task["job"] is not None and (job := schedules.get(task["job"])) and job["kind"] == "watch":
        return "a watch's check"
    if nothing_new:
        return "a scheduled run with nothing to report"
    return None


def since_asked(entries: list[dict]) -> list[dict]:
    """Log entries since the task's start or the user's latest reply."""
    start = max((i for i, e in enumerate(entries) if e["kind"] in ("start", "reply")), default=0)
    return entries[start:]


def rounds(entries: list[dict]) -> int:
    """Times the check sent the task back since the user last asked."""
    return sum(e["kind"] == KIND and e["text"].startswith(ROUND) for e in since_asked(entries))


def _clip(text: str, most: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= most else text[:most] + f"\n[... {len(text) - most} more characters left out]"


def _messages(entries: list[dict]) -> str:
    """The user's later messages (replies, answers, nudges), clipped."""
    said = [f"- {e['text']}" for e in entries if e["kind"] in ("reply", "nudge")]
    return _clip("\n".join(said), MOST["messages"])


def _work_log(entries: list[dict]) -> str:
    """Relevant work since the last ask: milestones, subagents, jobs, and build/test/check steps."""
    kept = [f"{e['kind']}: {e['text']}" for e in since_asked(entries)
            if e["kind"] in ("progress", "subagent", "job", KIND) or (e["kind"] == "step" and _CHECKING.search(e["text"]))]
    return _clip("\n".join(kept[-60:]), MOST["log"])


async def _git(folder: str, *args: str) -> str:
    code, out = await proc.run_try("git", "-C", folder, *args, timeout=60)
    return out if code == 0 else ""


async def evidence(task: dict, changes: str) -> str:
    """Evidence for a code_change: `changes`, plus (in git) stat, untracked files and diff vs base, minus secrets."""
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
    """To-dos for the check: the linked item if still open; else (user-started tasks only) all open items."""
    if task.get("todo"):
        item = todo.get(task["todo"])
        return [item] if item and not item["done"] else []
    if task["job"] is not None or task["fixes"]:
        return []
    return [i for i in todo.items() if not i["done"]][:OPEN_TODOS]


def material(task: dict, result: str, entries: list[dict], proof: str, round_: int, items: list[dict] = ()) -> str:
    """The check's prompt; `items` are the to-dos from `todos`."""
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
    """SCHEMA reply as a Check, or None if empty."""
    if not found:
        return None
    details = "\n".join(f"- {line}" for line in found.get("to_finish", []))
    items = {i["id"]: "" if i["done"] else i.get("left", "").strip() for i in found.get("todos", [])}
    return Check(found["verdict"], found["said"].strip(), details, cost, items)


async def ask_model(text: str) -> tuple[dict | None, float]:
    """One tool-less goal-role call: (SCHEMA reply or None, cost)."""
    from jarvis.llm.claude import ask_json

    return await ask_json(text, SCHEMA, system(), Role.GOAL, effort="low")


async def review(text: str, budget: Budget) -> Check | None:
    """Verdict on a finished run; None (result passes) if the call fails or `budget` is spent."""
    if not budget.take():
        log.warning("goal check skipped: %d checks in the last day already", budget.most)
        return None
    try:
        found, cost = await asyncio.wait_for(ask_model(text), CHECK_TIMEOUT)
    except Exception as e:  # log the type only: no task content
        log.warning("goal check failed: %s", type(e).__name__)
        return None
    return parse(found, cost)


def next_step(check: Check, done: int, cost: float) -> tuple[str, str | None]:
    """(log entry, send-back message or None) for a checked run already sent back `done` times at `cost` dollars."""
    if check.verdict == COMPLETE:
        return f"checked: complete: {check.said}", None
    if check.verdict == BLOCKED:
        return f"checked: needs {profile.name()}, so it's reported, not sent back: {check.said}", None
    if done >= GOAL_ROUNDS:
        why = f"it was sent back {done} time{'s' if done != 1 else ''} already" if done else "sending back is off"
        return f"checked: incomplete, but {why}, so it's reported as it is: {check.said}", None
    if GOAL_MAX_COST and cost >= GOAL_MAX_COST:  # 0 means no limit
        return (f"checked: incomplete, but it has cost ${cost:.2f}, over the ${GOAL_MAX_COST:g} limit for sending it back, so "
                f"it's reported as it is: {check.said}"), None
    n = done + 1
    left = f"\n{check.details}" if check.details else ""
    return f"{ROUND}{n} of {GOAL_ROUNDS}: incomplete, so it was sent back to finish: {check.said}{left}", send_back(check, n)


def send_back(check: Check, round_: int) -> str:
    """Message sending the task back to finish, in its own session."""
    left = check.details or f"- {check.said}"
    last = (" This is the last time JARVIS sends it back: if anything is still undone after this, the summary must say "
            "plainly what is done, what isn't, and why.") if round_ >= GOAL_ROUNDS else ""
    return (f"JARVIS checked your result against what {profile.name()} asked, and it isn't finished yet "
            f"(round {round_} of {GOAL_ROUNDS}). Still to do:\n{left}\n\n"
            f"Finish these now, and check your work where the project lets you (builds, tests). If one of them needs "
            f"{profile.name()}'s decision, access or a tool you can't get, don't keep trying: say so plainly. Then end with a "
            f"summary of the whole task as before.{last}")


def left(check: Check | None) -> tuple[str, bool]:
    """(what's missing for the announcement or "", whether it's on the user, i.e. BLOCKED)."""
    if check is None or check.verdict == COMPLETE:
        return "", False
    said = check.said if len(check.said) <= SAID_MOST else check.said[:SAID_MOST].rsplit(" ", 1)[0] + "…"
    return said, check.verdict == BLOCKED


@dataclass
class TodoChange:
    """A to-do edit (propose_data_edit args): applied `at_once`, or left pending, asking first if `needs_yes`."""
    edit: dict
    at_once: bool = False
    needs_yes: str | None = None


def _tick(text: str, reason: str) -> dict:
    return {"action": "todo_done", "text": text, "reason": reason}


def todo_changes(task: dict, check: Check | None) -> list[TodoChange]:
    """To-do edits implied by the final result and its `check` (None if unchecked).

    Linked item: ticked at once if finished, reworded to what's left if partly done, or a tick needing a yes if
    unchecked. Other open items the check finds finished: a tick needing a yes."""
    items = todos(task)
    if task.get("todo"):
        if not items:  # already ticked off or removed
            return []
        text = items[0]["text"]
        if check is None:
            return [TodoChange(_tick(text, f"Task {task['id']} was started for it and says it's finished."),
                               needs_yes="its result couldn't be checked, so make sure it's finished")]
        left = check.todos.get(items[0]["id"], "" if check.verdict == COMPLETE else text)
        if not left:
            return [TodoChange(_tick(text, f"Task {task['id']} was started for it, and JARVIS's check found it finished."),
                               at_once=True)]
        if left.lower() == text.lower():  # no progress
            return []
        return [TodoChange({"action": "todo_reword", "text": text, "item": left,
                            "reason": f"Task {task['id']} was started for it and did part of it; this is what's left."})]
    if check is None:
        return []
    listed = {i["id"]: i["text"] for i in items}
    return [TodoChange(_tick(listed[n], f"JARVIS's check found task {task['id']}'s work finishes it."),
                       needs_yes="the task wasn't started for it, but JARVIS's check found its work finishes it")
            for n, left in check.todos.items() if not left and n in listed]
