"""Runner part for edits tasks suggest to JARVIS's data files (see data_edits).

Covers keeping, applying and dismissing suggestions (POST /tasks/ID/edits), learning-review suggestions kept as a
hidden review task's edits, and the to-do changes a finished task makes.
"""

from jarvis.core.database import edits
from jarvis.state import activity as bell, memory, todo
from jarvis.tasks import announce, data_edits, goal, learning
from jarvis.tasks.runner_base import RunnerBase

# Report text announced when a learning review leaves edits pending
LEARNED_REPORT = "It read our last conversation for what's worth keeping in memory, the to-do list or skills."


class TaskEdits(RunnerBase):
    """Suggested data-file edits: propose, apply/dismiss, learning reviews, to-do settling."""

    async def propose(self, task_id: int, args: dict, needs_yes: str | None = None) -> dict:
        """Record an edit the task suggests; ValueError (shown to the task) if unusable.

        `needs_yes` forces a user approval reason beyond what data_edits.needs_yes decides. A to-do the list already
        has is refused, or suggested as a rewording of it when it adds detail.
        """
        task = self.store.get(task_id)
        if len(edits(task)) >= data_edits.MAX_EDITS:
            raise ValueError(f"A task can suggest at most {data_edits.MAX_EDITS} edits; list any others in your summary.")
        edit = data_edits.check(args, task["kind"])
        if edit["action"] == "todo_add" and (found := await todo.repeat_of(edit["item"])):
            edit = _merged(edit, found, task["kind"])
        if why := needs_yes or data_edits.needs_yes(edit, task["kind"]):
            edit["needs_yes"] = why
        kept = self.store.add_edit(task_id, edit)
        self.store.log(task_id, "edit", f"suggested edit {kept['n']}: {data_edits.describe(kept)}. {kept['reason']}")
        if kept.get("flagged"):  # possible injection: held for approval, with a bell note
            self.store.log(task_id, "flagged", f"edit {kept['n']} looks like hidden instructions: {kept['flagged']}")
            bell.add(f"Task {task_id} suggested an edit that looks like hidden instructions, held for your yes: "
                     f"{data_edits.describe(kept)}", "warn", task_id)
        return kept

    async def edits(self, task_id: int, action: str, numbers: list[int]) -> dict:
        """Apply or dismiss a task's suggested edits by number. Per-edit outcome; one failure doesn't stop the rest."""
        if action not in ("apply", "dismiss"):
            raise ValueError("action must be apply or dismiss.")
        kept = edits(self._get(task_id))
        by_number = {e["n"]: e for e in kept}
        if not numbers or any(n not in by_number for n in numbers):
            raise ValueError(f"Task {task_id}'s suggested edits are {', '.join(map(str, by_number)) or 'none'}; say which.")
        done = []
        async with self.projects_lock:  # guards projects and memory; to-dos lock in tasks.db
            for n in dict.fromkeys(numbers):
                edit = by_number[n]
                if edit["status"] != data_edits.PENDING:
                    done.append(f"edit {n} was already {edit['status']}")
                    continue
                if action == "dismiss":
                    edit["status"], edit["result"] = data_edits.DISMISSED, "dismissed"
                else:
                    try:
                        edit["result"] = await data_edits.apply(edit)
                        edit["status"] = data_edits.APPLIED
                    except (ValueError, OSError) as e:  # memory.Full message kept whole: it lists facts to make room
                        edit["status"], edit["result"] = data_edits.FAILED, str(e) if isinstance(e, memory.Full) else str(e)[:300]
                done.append(f"edit {n} {edit['status']}: {edit['result']}")
                self.store.log(task_id, f"edit {edit['status']}", f"edit {n}, {data_edits.describe(edit)}: {edit['result']}")
        return self.store.set_edits(task_id, kept) | {"outcome": done}

    async def learned(self, suggested: list[dict], about: str) -> dict:
        """Store a learning review's suggestions as a hidden review task's edits.

        Routine edits are applied now (each noted in the bell); the rest stay pending and are announced.
        With nothing pending the task ends silently.
        """
        task_id = self.store.create(learning.KIND, learning.TITLE, about, None, hidden=True)["id"]
        self.store.log(task_id, "start", about)
        for args in suggested:
            try:
                await self.propose(task_id, args)
            except ValueError as e:
                self.store.log(task_id, "edit", f"left out a suggestion: {e}"[:300])
        if routine := [e["n"] for e in edits(self.store.get(task_id)) if not e.get("needs_yes")]:
            await self.edits(task_id, "apply", routine)
        task = self.store.get(task_id)
        for e in edits(task):
            if e["status"] == data_edits.APPLIED:
                bell.add(f"Learned from our conversation: {data_edits.describe(e)}", "ok", task_id)
        if data_edits.pending(task):
            ending = announce.Ending(learning.TITLE, announce.DONE, LEARNED_REPORT, suggested=announce.suggestions(task))
            self._finish(task_id, "done", await announce.spoken(ending), alert=False, hidden=0)
        else:
            self._finish(task_id, "done", f"Task {task_id}, {learning.TITLE}, is done.", quiet=True, seen=1)
        return self.store.get(task_id)

    async def _settle_todos(self, task: dict, check: goal.Check | None) -> list[str]:
        """Record a finished task's to-do changes as edits, applying immediate ones (ticking off its own item).

        Supersedes pending to-do edits from earlier runs for the same items. Returns phrases for what was applied.
        """
        changes = goal.todo_changes(task, check)
        texts = {c.edit["text"].lower() for c in changes}
        if stale := [e["n"] for e in data_edits.pending(self.store.get(task["id"]))
                     if e["action"] in ("todo_done", "todo_reword") and e["text"].lower() in texts]:
            await self.edits(task["id"], "dismiss", stale)
        done = []
        for change in changes:
            try:
                edit = await self.propose(task["id"], change.edit, change.needs_yes)
            except ValueError as e:
                self.store.log(task["id"], "edit", f"left out a to-do edit: {e}"[:300])
                continue
            if change.at_once and not edit.get("flagged"):
                kept = edits(await self.edits(task["id"], "apply", [edit["n"]]))
                if kept[edit["n"] - 1]["status"] == data_edits.APPLIED:
                    done.append(f"ticked off your to-do \"{edit['text']}\"")
        return done


def _merged(edit: dict, found: todo.Repeat, kind: str) -> dict:
    """In place of a todo_add repeating `found`: a todo_reword taking in its new detail; else ValueError naming it."""
    old = found.item
    if old["done"]:
        raise ValueError(f"{todo.repeated(found)}. If part of it is still to do, suggest only that part.")
    if not found.merged:
        raise ValueError(f"{todo.repeated(found)}.")
    reword = data_edits.check({"action": "todo_reword", "text": old["text"], "item": found.merged,
                               "reason": f"It repeats this to-do, adding detail. {edit['reason']}"}, kind)
    return reword | ({"flagged": edit["flagged"]} if edit.get("flagged") else {})
