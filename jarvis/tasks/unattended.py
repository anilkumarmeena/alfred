"""Scheduled work on the user's own tasks while no conversation is open (schedules kinds resume and todo).

  resume  carries on stopped tasks, named ones or every one stopped (or failed) in the last RESUME_HOURS, through the
          runner's reply, as a follow-up would
  todo    starts tasks for to-do items, named ones or the next `count` open ones no task has taken. One schedule-role
          call turns the items into tasks (kind, project, instruction), passing over those that need the user; each
          task is linked to its item, so the runner settles the item as the task finishes. The job's text, the user's
          standing instructions, ends every instruction as given; a code_change task's also names FINISH_SKILL
Both go through the runner like any task: its slots and queueing, and risky commands still wait for the user's yes.
Each returns a Done, which the scheduler says as one notification.
"""

import json
import logging
from dataclasses import dataclass

from jarvis import prompts
from jarvis.llm.roles import Role
from jarvis.state import memory, todo
from jarvis.storage import profile
from jarvis.tasks import schedules
from jarvis.tasks.store import KINDS

log = logging.getLogger("jarvis.tasks.unattended")
RESUME_HOURS = 7 * 24  # "every stopped task": those stopped in the last week
OPEN_ITEMS = 30  # open items the planner sees when picking the next few
PLAN_SECONDS = 180
FINISH_SKILL = "finish-code-change"  # the JARVIS skill every code_change task ends with
_NOT_RESUMED = {"queued": "is already queued", "running": "is already running",
                "waiting": "is waiting for your answer", "done": "has finished", "discarded": "was discarded"}


@dataclass
class Done:
    """What a run did: `said` for the notification (lock-screen safe), or kept quiet when nothing was to be done."""
    said: str
    quiet: bool = False


def _named(task: dict) -> str:
    return f"{task['id']}, {task['title']}"


def _listed(things: list[str]) -> str:
    return "; ".join(things)


def resume_message(job: dict) -> str:
    """What each resumed task is told."""
    name = profile.name()
    said = (f"JARVIS's scheduler is carrying this task on at the time {name} set, while no conversation is open. Carry "
            "on from where it stopped: first check what your last steps left behind, as they may not have finished, "
            "then end with your summary of the whole task as before.")
    return said + (f"\n\n{name}'s note for it: {job['text']}" if job["text"] else "")


def _resumable(task: dict) -> bool:
    """A model task the user started, not a scheduled run (those retry by themselves) or a command's fix."""
    return task["kind"] in KINDS and task["job"] is None and not task["fixes"]


def resume(runner, job: dict) -> Done:
    """Resume the job's tasks through the runner; say which carried on, and which couldn't."""
    found = schedules.picks(job)
    passed: list[str] = []
    if ids := found.get("tasks"):
        tasks = []
        for task_id in ids:
            if not (task := runner.store.get(task_id)):
                passed.append(f"task {task_id} is gone")
            elif task["status"] in _NOT_RESUMED:
                passed.append(f"task {_named(task)}, {_NOT_RESUMED[task['status']]}")
            else:
                tasks.append(task)
    else:
        statuses = ("stopped", "failed") if found.get("failed") else ("stopped",)
        tasks = [t for t in runner.store.ended_as(statuses, RESUME_HOURS) if _resumable(t)]
    resumed, failed = [], []
    for task in tasks[:schedules.MAX_PICKS]:
        try:
            runner.reply(task["id"], resume_message(job))
        except ValueError as e:
            failed.append(f"task {task['id']}: {str(e).rstrip('.')}")
            continue
        resumed.append(_named(task))
    if left := tasks[schedules.MAX_PICKS:]:
        passed.append(f"{len(left)} more left for later, as one run resumes at most {schedules.MAX_PICKS}")
    said = []
    if resumed:
        said.append(f"Resumed {len(resumed)} task{'s' if len(resumed) > 1 else ''}: {_listed(resumed)}.")
    elif not failed:
        said.append("No stopped " + ("or failed " if found.get("failed") else "") + "tasks to resume.")
    if failed:
        said.append(f"Couldn't resume {_listed(failed)}.")
    if passed:
        said.append(f"Left alone: {_listed(passed)}.")
    return Done(" ".join(said), quiet=not (resumed or failed or ids))


# ── To-do items ─────────────────────────────────────────────────


_PLAN = {
    "type": "object",
    "properties": {
        "tasks": {"type": "array", "items": {
            "type": "object",
            "properties": {"n": {"type": "integer"}, "kind": {"enum": list(KINDS)}, "project": {"type": "string"},
                           "title": {"type": "string"}, "instruction": {"type": "string"}},
            "required": ["n", "kind", "title", "instruction"]}},
        "left": {"type": "array", "items": {
            "type": "object", "properties": {"n": {"type": "integer"}, "why": {"type": "string"}},
            "required": ["n", "why"]}},
    },
    "required": ["tasks"],
}


def _material(items: list[dict], want: int, named: bool, job: dict) -> str:
    from jarvis import projects
    from jarvis.skills import home as skills

    return json.dumps({
        "items": [{"n": n, "text": item["text"]} for n, item in enumerate(items, 1)],
        "tasks_to_start": want,
        "named": named,
        "projects": [{"name": p.name, "about": p.about} for p in projects.load()],
        "skills": skills.listing(skills.usable()),
        "standing_instructions": job["text"],
        "memory": memory.prompt_block(),
    }, ensure_ascii=False)


def system() -> str:
    return prompts.load("todo_plan", user_name=profile.name())


async def plan(items: list[dict], want: int, named: bool, job: dict) -> dict | None:
    """The planner's {"tasks", "left"} for `items`, or None if the call failed."""
    from jarvis.llm import claude

    try:
        found, _ = await claude.ask_json(_material(items, want, named, job), _PLAN, system(), Role.SCHEDULE,
                                      timeout=PLAN_SECONDS)
    except Exception as e:
        log.warning("planning tasks for to-do items failed: %s", type(e).__name__)
        return None
    return found


def instruction(planned: dict, job: dict) -> str:
    """A planned task's instruction, ending with the user's standing instructions as they gave them; a code_change
    task's always names the FINISH_SKILL pass, whatever the plan and the job's text say."""
    from jarvis.skills.home import PLUGIN

    said = planned["instruction"].strip() + (f"\n\n{job['text']}" if job["text"] else "")
    skill = f"{PLUGIN}:{FINISH_SKILL}"
    if planned["kind"] == "code_change" and skill not in said:
        said += (f"\n\nOnce the change works and its tests pass, finish with the {skill} skill: its cleanup pass and "
                 "the full test suite, before reporting the task done.")
    return said


async def start_todos(runner, job: dict) -> Done:
    """Start tasks for the job's to-do items, each linked to its item; say which started and which were left."""
    found, taken = schedules.picks(job), runner.store.todos_taken()
    passed: list[str] = []
    if ids := found.get("todos"):
        items = []
        for item_id in ids:
            item = todo.get(item_id)
            if not item or item["done"]:
                passed.append(f"a to-do since {'ticked off' if item else 'removed'}" + (f": {item['text']}" if item else ""))
            elif item_id in taken:
                passed.append(f"{item['text']}, which a task has already taken on")
            else:
                items.append(item)
        want = len(items)
    else:
        items = [i for i in todo.items() if not i["done"] and i["id"] not in taken][:OPEN_ITEMS]
        want = min(found.get("count") or 1, len(items))
    if not items:
        said = "No open to-do items to start tasks for." + (f" Left alone: {_listed(passed)}." if passed else "")
        return Done(said, quiet=not ids)
    planned = await plan(items, want, bool(ids), job)
    if planned is None:
        return Done("Couldn't start tasks for your to-do items: planning them failed. They're left as they were.")
    started, failed, used = [], [], set()
    for p in planned["tasks"]:
        if len(started) >= want or not 1 <= p["n"] <= len(items) or p["n"] in used:
            continue
        used.add(p["n"])
        item = items[p["n"] - 1]
        try:
            task = runner.start(p["kind"], p["title"], instruction(p, job), p.get("project") or None, todo=item["text"])
        except ValueError as e:
            failed.append(f"{item['text']} ({str(e).rstrip('.')})")
            continue
        started.append(_named(task))
    passed += [f"{items[x['n'] - 1]['text']} ({x['why'].rstrip('.')})" for x in planned.get("left") or []
               if 1 <= x["n"] <= len(items) and x["n"] not in used]
    said = [f"Started {len(started)} task{'s' if len(started) > 1 else ''} from your to-do list: {_listed(started)}."
            if started else "Started no tasks from your to-do list."]
    if failed:
        said.append(f"Couldn't start {_listed(failed)}.")
    if passed:
        said.append(f"Left for you: {_listed(passed)}.")
    return Done(" ".join(said))
