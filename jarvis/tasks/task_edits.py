"""The task runner's side (jarvis.tasks.runner) of the edits tasks suggest to JARVIS's own data files
(jarvis.tasks.data_edits): kept on the task, then applied or dismissed (POST /tasks/ID/edits); a learning review's
suggestions (jarvis.tasks.learning), kept as a review task's edits; and what a finished task does to the to-do list
(jarvis.tasks.goal).
"""

import asyncio

from jarvis.core.database import edits
from jarvis.state import activity as bell, memory
from jarvis.tasks import announce, data_edits, goal, learning
from jarvis.tasks.runner_base import RunnerBase

# What a learning review that leaves edits for the user's yes reports, for its announcement
LEARNED_REPORT = "It read our last conversation for what's worth keeping in memory, the to-do list or skills."


class TaskEdits(RunnerBase):
    """Suggested edits: kept, applied or dismissed, and the learning review's and the to-do list's."""

    def propose(self, task_id: int, args: dict, needs_yes: str | None = None) -> dict:
        """An edit to JARVIS's data files that the task suggests; raises ValueError, for the task, if it's unusable.
        `needs_yes` says why the user is asked first, where the edit alone doesn't (data_edits.needs_yes)."""
        task = self.store.get(task_id)
        if len(edits(task)) >= data_edits.MAX_EDITS:
            raise ValueError(f"A task can suggest at most {data_edits.MAX_EDITS} edits; list any others in your summary.")
        edit = data_edits.check(args, task["kind"])
        if why := needs_yes or data_edits.needs_yes(edit, task["kind"]):
            edit["needs_yes"] = why
        kept = self.store.add_edit(task_id, edit)
        self.store.log(task_id, "edit", f"suggested edit {kept['n']}: {data_edits.describe(kept)}. {kept['reason']}")
        if kept.get("flagged"):  # hidden instructions: it waits for the user's yes, and the bell says why
            self.store.log(task_id, "flagged", f"edit {kept['n']} looks like hidden instructions: {kept['flagged']}")
            bell.add(f"Task {task_id} suggested an edit that looks like hidden instructions, held for your yes: "
                     f"{data_edits.describe(kept)}", "warn", task_id)
        return kept

    async def edits(self, task_id: int, action: str, numbers: list[int]) -> dict:
        """Apply or dismiss some of the edits a task suggested, by number. Each one's outcome goes in
        its status and result; one that fails leaves the others to go ahead."""
        if action not in ("apply", "dismiss"):
            raise ValueError("action must be apply or dismiss.")
        kept = edits(self._get(task_id))
        by_number = {e["n"]: e for e in kept}
        if not numbers or any(n not in by_number for n in numbers):
            raise ValueError(f"Task {task_id}'s suggested edits are {', '.join(map(str, by_number)) or 'none'}; say which.")
        done = []
        async with self.projects_lock:  # the project list, like register_project; memory as well (to-dos lock themselves, in tasks.db)
            for n in dict.fromkeys(numbers):
                edit = by_number[n]
                if edit["status"] != data_edits.PENDING:
                    done.append(f"edit {n} was already {edit['status']}")
                    continue
                if action == "dismiss":
                    edit["status"], edit["result"] = data_edits.DISMISSED, "dismissed"
                else:
                    try:
                        edit["result"] = await asyncio.to_thread(data_edits.apply, edit)
                        edit["status"] = data_edits.APPLIED
                    except (ValueError, OSError) as e:  # a full memory section's facts are kept whole, to make room
                        edit["status"], edit["result"] = data_edits.FAILED, str(e) if isinstance(e, memory.Full) else str(e)[:300]
                done.append(f"edit {n} {edit['status']}: {edit['result']}")
                self.store.log(task_id, f"edit {edit['status']}", f"edit {n}, {data_edits.describe(edit)}: {edit['result']}")
        return self.store.set_edits(task_id, kept) | {"outcome": done}

    async def learned(self, suggested: list[dict], about: str) -> dict:
        """What a learning review (jarvis.tasks.learning) suggests, kept as a review task's edits, checked like any
        task's: the routine ones applied now, as JARVIS would apply them, each in the bell; the rest left pending and
        announced, for the user's yes. With nothing pending it ends quietly and hidden, saying nothing."""
        task_id = self.store.create(learning.KIND, learning.TITLE, about, None, hidden=True)["id"]
        self.store.log(task_id, "start", about)
        for args in suggested:
            try:
                self.propose(task_id, args)
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
        """Keep what the finished task does to the to-do list (goal.todo_changes) as its suggested edits, making at once
        the one that ticks off the item it was started for; returns what was made at once, for its announcement, which
        mentions those left pending with its other edits. What an earlier run suggested for the same items gives way
        to them."""
        changes = goal.todo_changes(task, check)
        texts = {c.edit["text"].lower() for c in changes}
        if stale := [e["n"] for e in data_edits.pending(self.store.get(task["id"]))
                     if e["action"] in ("todo_done", "todo_reword") and e["text"].lower() in texts]:
            await self.edits(task["id"], "dismiss", stale)
        done = []
        for change in changes:
            try:
                edit = self.propose(task["id"], change.edit, change.needs_yes)
            except ValueError as e:
                self.store.log(task["id"], "edit", f"left out a to-do edit: {e}"[:300])
                continue
            if change.at_once and not edit.get("flagged"):
                kept = edits(await self.edits(task["id"], "apply", [edit["n"]]))
                if kept[edit["n"] - 1]["status"] == data_edits.APPLIED:
                    done.append(f"ticked off your to-do \"{edit['text']}\"")
        return done
